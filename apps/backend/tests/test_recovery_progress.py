"""Bounded recovery and graph routing invariants."""

import importlib
import subprocess
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from portage_agent.agent.graph import _after_integrate, _after_plan, _after_verify
from portage_agent.agent.nodes.common import create_cut_checkpoint
from portage_agent.agent.nodes.recover import (
    _rollback_file,
    circular_import_target,
    contract_failure_owner,
    contract_failure_target,
    missing_unplanned_test_compat,
    repeated_failure_count,
    targeted_contract_repair_count,
    unique_traceback_leaf_target,
)
from portage_agent.agent.nodes.verify import failure_fingerprint
from portage_agent.sandbox import parse_junit_xml

verify_module = importlib.import_module("portage_agent.agent.nodes.verify")
recover_module = importlib.import_module("portage_agent.agent.nodes.recover")


def test_failure_fingerprint_ignores_line_numbers_timings_and_ansi():
    first = failure_fingerprint(
        "\x1b[31mboom line 42 at 0xffff1234 in 1.20s", "same diff", ["a.py"],
    )
    second = failure_fingerprint(
        "boom line 99 at 0xabc567 in 4s", "same diff", ["a.py"],
    )
    assert first == second
    assert first != failure_fingerprint("different", "same diff", ["a.py"])


def test_repeat_count_includes_current_occurrence():
    actions = [{"fingerprint": "same"}, {"fingerprint": "other"}]
    assert repeated_failure_count(actions, "same") == 2


def test_runtime_targeted_contract_repair_has_an_independent_ledger():
    attempts = [
        {"action": "migrate"},
        {"action": "contract_repair"},
        {"action": "contract_repair", "scope": "runtime_targeted"},
    ]
    assert targeted_contract_repair_count(attempts) == 1


def test_only_exact_unplanned_test_compat_import_is_replanable():
    path = "_portage_fastapi_test_compat.py"
    exact = (
        "ERROR collecting tests/conftest.py\n"
        "ModuleNotFoundError: No module named '_portage_fastapi_test_compat'"
    )
    assert missing_unplanned_test_compat(exact, path, {"tests/conftest.py"})
    assert not missing_unplanned_test_compat(exact, path, {path, "tests/conftest.py"})
    assert not missing_unplanned_test_compat(
        "ModuleNotFoundError: No module named 'customer_module'",
        path,
        {"tests/conftest.py"},
    )
    assert not missing_unplanned_test_compat(exact, "", {"tests/conftest.py"})


def test_runtime_contract_failures_map_only_to_a_unique_frozen_owner():
    manifest = {
        "pkg/compat.py::CompatApp": {
            "module": "pkg/compat.py", "symbol": "CompatApp",
            "members": ["test_client"], "provenance": "planned_create",
        },
        "pkg/db.py::get_db_dep": {
            "module": "pkg/db.py", "symbol": "get_db_dep", "members": [],
        },
    }
    batch = {"pkg/compat.py", "pkg/db.py", "pkg/app.py"}

    assert contract_failure_owner(
        "ModuleNotFoundError: No module named 'pkg.compat'", manifest, batch,
    ) == "pkg/compat.py"
    assert contract_failure_owner(
        "ImportError: cannot import name 'get_db_dep' from 'pkg.db'", manifest, batch,
    ) == "pkg/db.py"
    assert contract_failure_owner(
        "AttributeError: 'CompatApp' object has no attribute 'test_client'",
        manifest,
        batch,
    ) == "pkg/compat.py"
    assert contract_failure_owner(
        "AttributeError: 'X' object has no attribute 'unknown'", manifest, batch,
    ) is None


def test_ambiguous_member_ownership_refuses_targeted_repair():
    manifest = {
        f"pkg/{name}.py::Compat": {
            "module": f"pkg/{name}.py", "symbol": "Compat",
            "members": ["test_client"], "provenance": "planned_create",
        }
        for name in ("one", "two")
    }
    assert contract_failure_owner(
        "AttributeError: 'Compat' object has no attribute 'test_client'",
        manifest,
        {"pkg/one.py", "pkg/two.py"},
    ) is None


def test_traceback_leaf_attribution_requires_one_application_artifact():
    output = (
        "tests/test_db.py:8: in test_init\n"
        "pkg/db.py:31: in init_db\n"
        "E   AttributeError: 'str' object has no attribute 'decode'\n"
        "tests/test_factory.py:7: in test_config\n"
        "E   AttributeError: 'FastAPI' object has no attribute 'testing'\n"
    )
    eligible = {"pkg/db.py", "pkg/app.py"}
    assert unique_traceback_leaf_target(output, eligible) == "pkg/db.py"

    ambiguous = output + (
        "tests/test_app.py:4: in test_app\n"
        "pkg/app.py:12: in create_app\n"
        "E   RuntimeError: boom\n"
    )
    assert unique_traceback_leaf_target(ambiguous, eligible) is None


@pytest.mark.asyncio
async def test_unique_traceback_leaf_gets_runtime_targeted_repair(
    tmp_path, monkeypatch,
):
    tasks = [
        SimpleNamespace(
            id=uuid.uuid4(), target_path=path, type=role, status="done", attempts=3,
            attempts_log=[], verify_spec={"action": "rewrite"},
        )
        for path, role in (
            ("pkg/db.py", "support"),
            ("pkg/app.py", "app_factory"),
            ("tests/test_factory.py", "test_harness"),
        )
    ]
    update = AsyncMock()
    monkeypatch.setattr(recover_module.task_store, "load_tasks", AsyncMock(return_value=tasks))
    monkeypatch.setattr(recover_module.task_store, "update_task", update)
    monkeypatch.setattr(recover_module, "file_diff", AsyncMock(return_value="db diff"))
    monkeypatch.setattr(recover_module, "_rollback_file", AsyncMock())
    output = (
        "tests/test_db.py:8: in test_init\n"
        "pkg/db.py:31: in init_db\n"
        "E   AttributeError: 'str' object has no attribute 'decode'\n"
        "tests/test_factory.py:7: in test_config\n"
        "E   AttributeError: 'FastAPI' object has no attribute 'testing'\n"
    )

    result = await recover_module.recover_node({
        "job_id": "00000000-0000-0000-0000-000000000001",
        "worktree": str(tmp_path),
        "last_verify_errors": output,
        "last_failure_fingerprint": "decode-leaf",
        "current_batch_paths": [task.target_path for task in tasks],
        "interface_manifest": {},
        "recovery_actions": [],
    })

    assert result["recover_route"] == "execute"
    assert result["contract_repair_owner"] == "pkg/db.py"
    assert result["recovery_actions"][0]["classification"] == "traceback_leaf"
    assert result["recovery_actions"][0]["action"] == "targeted_contract_repair"
    assert result["recovery_actions"][0]["targets"] == ["pkg/db.py"]
    assert result["recover_budget_used"] == 0
    assert result["recovery_actions"][0]["budget_charged"] is False
    pending = [
        call for call in update.await_args_list if call.kwargs.get("status") == "pending"
    ]
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_failed_targeted_repair_restores_and_reverifies_the_whole_cut(
    tmp_path, monkeypatch,
):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "db.py").write_text("VALUE = 'original-db'\n")
    (package / "app.py").write_text("VALUE = 'original-app'\n")
    tasks = [
        SimpleNamespace(
            id=uuid.uuid4(), target_path=path, type=role, status="done", attempts=3,
            attempts_log=(
                [{"action": "contract_repair", "scope": "runtime_targeted"}]
                if path == "pkg/db.py" else []
            ),
            verify_spec={"action": "rewrite"}, content_hash=None,
        )
        for path, role in (("pkg/db.py", "support"), ("pkg/app.py", "app_factory"))
    ]
    checkpoint = create_cut_checkpoint(
        str(tmp_path),
        ["pkg/db.py", "pkg/app.py"],
        {
            "pkg/db.py": {"status": "pending", "action": "rewrite"},
            "pkg/app.py": {"status": "pending", "action": "rewrite"},
        },
    )
    (package / "db.py").write_text("VALUE = 'broken-repair'\n")
    (package / "app.py").write_text("VALUE = 'generated-app'\n")
    update = AsyncMock()
    monkeypatch.setattr(recover_module.task_store, "load_tasks", AsyncMock(return_value=tasks))
    monkeypatch.setattr(recover_module.task_store, "update_task", update)
    monkeypatch.setattr(recover_module, "file_diff", AsyncMock(return_value="failed repair"))

    result = await recover_module.recover_node({
        "job_id": "00000000-0000-0000-0000-000000000001",
        "worktree": str(tmp_path),
        "last_verify_errors": (
            "tests/test_db.py:8: in test_db\n"
            "pkg/db.py:1: in load\n"
            "E   RuntimeError: broken\n"
        ),
        "last_failure_fingerprint": "repair-failed",
        "current_batch_paths": ["pkg/db.py", "pkg/app.py"],
        "current_batch_checkpoint": checkpoint,
        "interface_manifest": {},
        "recovery_actions": [],
    })

    assert result["recover_route"] == "verify"
    assert result["cut_restore_pending_verification"] is True
    assert result["recovery_actions"][0]["action"] == "restore_cut_reverify"
    assert (package / "db.py").read_text() == "VALUE = 'original-db'\n"
    assert (package / "app.py").read_text() == "VALUE = 'original-app'\n"
    skipped = [
        call for call in update.await_args_list if call.kwargs.get("status") == "skipped"
    ]
    assert len(skipped) == 2


@pytest.mark.asyncio
async def test_failed_restored_cut_reverification_stops_without_another_mutation(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(recover_module.task_store, "load_tasks", AsyncMock(return_value=[]))

    result = await recover_module.recover_node({
        "job_id": "00000000-0000-0000-0000-000000000001",
        "worktree": str(tmp_path),
        "last_verify_errors": "still red",
        "last_failure_fingerprint": "restored-red",
        "cut_restore_pending_verification": True,
        "recovery_actions": [],
    })

    assert result["recover_route"] == "integrate"
    assert result["integration_recovery_visits"] == 1
    assert result["recover_budget_used"] == 1
    assert result["recovery_actions"][0]["classification"] == (
        "restored_cut_reverify_failed"
    )


@pytest.mark.asyncio
async def test_successful_restored_cut_reverification_clears_checkpoint_without_credit(
    tmp_path, monkeypatch,
):
    target = tmp_path / "pkg.py"
    target.write_text("VALUE = 1\n")
    checkpoint = create_cut_checkpoint(
        str(tmp_path), ["pkg.py"],
        {"pkg.py": {"status": "pending", "action": "rewrite"}},
    )
    summary = {
        "total": 1, "passed": 1, "failed": 0, "errors": 0,
        "skipped": 0, "cases": [],
    }
    monkeypatch.setattr(
        verify_module, "_run_tests",
        AsyncMock(return_value=(summary, SimpleNamespace(stdout="", stderr=""))),
    )

    result = await verify_module.verify_node({
        "job_id": "00000000-0000-0000-0000-000000000001",
        "migrate": True,
        "worktree": str(tmp_path),
        "graph_summary": {},
        "config": {"verify_delay_seconds": 0},
        "current_batch_paths": ["pkg.py"],
        "current_batch_tests": ["tests/test_pkg.py"],
        "current_batch_checkpoint": checkpoint,
        "cut_restore_pending_verification": True,
    })

    assert result["verify_passed"] is True
    assert result["cut_restore_pending_verification"] is False
    assert result["current_batch_checkpoint"] == {}
    assert "verified_batches" not in result
    assert not (tmp_path / ".portage-cut-checkpoint").exists()


def test_implemented_provider_moves_member_repair_to_unique_consumer(tmp_path):
    owner = tmp_path / "pkg" / "compat.py"
    owner.parent.mkdir()
    owner.write_text(
        "class CompatApp:\n"
        "    def test_client(self):\n"
        "        return object()\n"
    )
    manifest = {
        "pkg/compat.py::CompatApp": {
            "module": "pkg/compat.py", "symbol": "CompatApp",
            "members": ["test_client"], "provenance": "planned_create",
            "consumers": [{"module": "pkg/app.py"}],
        },
    }
    output = "AttributeError: 'FastAPI' object has no attribute 'test_client'"

    assert contract_failure_target(
        output, manifest, {"pkg/compat.py", "pkg/app.py"}, str(tmp_path),
    ) == "pkg/app.py"

    owner.write_text("class CompatApp:\n    pass\n")
    assert contract_failure_target(
        output, manifest, {"pkg/compat.py", "pkg/app.py"}, str(tmp_path),
    ) == "pkg/compat.py"


@pytest.mark.asyncio
async def test_circular_import_targets_provider_importing_its_consumer(
    tmp_path, monkeypatch,
):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "context.py").write_text("from .db import get_db\ng = object()\n")
    (package / "db.py").write_text("from .context import g\ndef get_db(): pass\n")
    manifest = {
        "pkg/context.py::g": {
            "module": "pkg/context.py", "symbol": "g", "members": [],
            "provenance": "planned_create", "consumers": [{"module": "pkg/db.py"}],
        },
        "pkg/db.py::get_db": {
            "module": "pkg/db.py", "symbol": "get_db", "members": [],
            "provenance": "original", "consumers": [],
        },
    }
    output = (
        "pkg/db.py:1: in <module>\n    from .context import g\n"
        "pkg/context.py:1: in <module>\n    from .db import get_db\n"
        "ImportError: cannot import name 'get_db' from partially initialized module "
        "'pkg.db' (most likely due to a circular import)"
    )
    batch = {"pkg/context.py", "pkg/db.py"}
    assert circular_import_target(output, manifest, batch, str(tmp_path)) == "pkg/context.py"

    tasks = [
        SimpleNamespace(
            id=uuid.uuid4(), target_path=path, status="done", attempts=3,
            attempts_log=[], verify_spec={"action": action},
        )
        for path, action in (("pkg/context.py", "create"), ("pkg/db.py", "rewrite"))
    ]
    update = AsyncMock()
    monkeypatch.setattr(recover_module.task_store, "load_tasks", AsyncMock(return_value=tasks))
    monkeypatch.setattr(recover_module.task_store, "update_task", update)
    monkeypatch.setattr(recover_module, "file_diff", AsyncMock(return_value="failing diff"))
    monkeypatch.setattr(recover_module, "_rollback_file", AsyncMock())

    result = await recover_module.recover_node({
        "job_id": "00000000-0000-0000-0000-000000000001",
        "worktree": str(tmp_path),
        "last_verify_errors": output,
        "last_failure_fingerprint": "cycle",
        "current_batch_paths": sorted(batch),
        "interface_manifest": manifest,
        "recovery_actions": [],
    })

    assert result["recover_route"] == "execute"
    assert result["contract_repair_owner"] == "pkg/context.py"
    assert result["recovery_actions"][0]["targets"] == ["pkg/context.py"]
    assert result["recovery_actions"][0]["action"] == "targeted_contract_repair"


def _consumer_contract_fixture(tmp_path, *, targeted_repairs: int = 0):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "compat.py").write_text(
        "class CompatApp:\n"
        "    def test_client(self):\n"
        "        return object()\n"
    )
    (package / "app.py").write_text("app = object()\n")
    task = SimpleNamespace(
        id=uuid.uuid4(),
        target_path="pkg/app.py",
        status="done",
        attempts=3,
        attempts_log=[
            *({"action": "migrate"} for _ in range(3)),
            *(
                {"action": "contract_repair", "scope": "runtime_targeted"}
                for _ in range(targeted_repairs)
            ),
        ],
        verify_spec={"action": "rewrite"},
    )
    manifest = {
        "pkg/compat.py::CompatApp": {
            "module": "pkg/compat.py",
            "symbol": "CompatApp",
            "members": ["test_client"],
            "provenance": "planned_create",
            "consumers": [{"module": "pkg/app.py"}],
        },
    }
    return task, manifest


@pytest.mark.asyncio
async def test_contract_target_gets_repair_after_ordinary_attempts_exhausted(
    tmp_path, monkeypatch,
):
    task, manifest = _consumer_contract_fixture(tmp_path)
    update = AsyncMock()
    monkeypatch.setattr(recover_module.task_store, "load_tasks", AsyncMock(return_value=[task]))
    monkeypatch.setattr(recover_module.task_store, "update_task", update)
    monkeypatch.setattr(recover_module, "file_diff", AsyncMock(return_value="app diff"))
    monkeypatch.setattr(recover_module, "_rollback_file", AsyncMock())

    result = await recover_module.recover_node({
        "job_id": "00000000-0000-0000-0000-000000000001",
        "worktree": str(tmp_path),
        "last_verify_errors": (
            "AttributeError: 'FastAPI' object has no attribute 'test_client'"
        ),
        "last_failure_fingerprint": "missing-test-client",
        "current_batch_paths": ["pkg/compat.py", "pkg/app.py"],
        "interface_manifest": manifest,
        "recovery_actions": [],
    })

    assert result["recover_route"] == "execute"
    assert result["contract_repair_owner"] == "pkg/app.py"
    assert result["recovery_actions"][0]["action"] == "targeted_contract_repair"
    assert any(call.kwargs.get("status") == "pending" for call in update.await_args_list)


@pytest.mark.asyncio
async def test_contract_target_stops_when_separate_repair_allowance_is_spent(
    tmp_path, monkeypatch,
):
    task, manifest = _consumer_contract_fixture(tmp_path, targeted_repairs=1)
    update = AsyncMock()
    monkeypatch.setattr(recover_module.task_store, "load_tasks", AsyncMock(return_value=[task]))
    monkeypatch.setattr(recover_module.task_store, "update_task", update)
    monkeypatch.setattr(recover_module, "file_diff", AsyncMock(return_value="app diff"))
    monkeypatch.setattr(recover_module, "_rollback_file", AsyncMock())

    result = await recover_module.recover_node({
        "job_id": "00000000-0000-0000-0000-000000000001",
        "worktree": str(tmp_path),
        "last_verify_errors": (
            "AttributeError: 'FastAPI' object has no attribute 'test_client'"
        ),
        "last_failure_fingerprint": "missing-test-client",
        "current_batch_paths": ["pkg/compat.py", "pkg/app.py"],
        "interface_manifest": manifest,
        "recovery_actions": [],
    })

    assert result["recover_route"] == "integrate"
    assert result["recovery_actions"][0]["action"] == "give_up"
    skipped = next(
        call for call in update.await_args_list if call.kwargs.get("status") == "skipped"
    )
    assert "targeted contract repair allowance exhausted" in skipped.kwargs["error"]


@pytest.mark.asyncio
async def test_create_rollback_removes_intent_to_add_artifact(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "base.py").write_text("BASE = True\n")
    subprocess.run(["git", "add", "base.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    created = tmp_path / "pkg" / "compat.py"
    created.parent.mkdir()
    created.write_text("CREATED = True\n")
    subprocess.run(["git", "add", "-N", "--", "pkg/compat.py"],
                   cwd=tmp_path, check=True)

    await _rollback_file(str(tmp_path), "pkg/compat.py", action="create")

    assert not created.exists()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "pkg/compat.py"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    assert status.stdout == ""


@pytest.mark.asyncio
async def test_missing_unplanned_adapter_routes_recover_to_plan(tmp_path, monkeypatch):
    missing = (
        "ERROR collecting tests/conftest.py\n"
        "ModuleNotFoundError: No module named '_portage_fastapi_test_compat'"
    )
    monkeypatch.setattr(
        recover_module.task_store,
        "load_tasks",
        AsyncMock(return_value=[SimpleNamespace(target_path="tests/conftest.py")]),
    )

    result = await recover_module.recover_node({
        "job_id": "00000000-0000-0000-0000-000000000001",
        "worktree": str(tmp_path),
        "last_verify_errors": missing,
        "last_failure_fingerprint": "missing-adapter",
        "test_compat_path": "_portage_fastapi_test_compat.py",
        "recovery_actions": [],
    })

    assert result["recover_route"] == "plan"
    assert result["replan_requested"] is True
    assert result["recovery_actions"][0]["classification"] == "missing_test_compat"


def test_verify_continues_batches_before_integrating():
    assert _after_verify({"verify_passed": True, "migrate": True,
                          "has_pending_tasks": True}) == "execute"
    assert _after_verify({"verify_passed": True, "migrate": True,
                          "has_pending_tasks": False}) == "integrate"


def test_plan_only_jobs_use_the_real_plan_then_report():
    assert _after_plan({"config": {"plan_only": True}}) == "report"
    assert _after_plan({"config": {}}) == "execute"


def test_integration_recovery_is_bounded_to_one_visit():
    failed = {"migrate": True, "integration_passed": False,
              "integration_recovery_visits": 0}
    assert _after_integrate(failed) == "recover"
    failed["integration_recovery_visits"] = 1
    assert _after_integrate(failed) == "report"


def test_junit_failure_details_are_preserved_for_repair_prompts():
    report = parse_junit_xml(
        '<testsuite><testcase name="test_x" classname="tests.test_x">'
        '<failure message="boom">Traceback\nValueError: exact cause</failure>'
        "</testcase></testsuite>"
    ).to_dict()
    assert report["cases"][0]["details"] == "Traceback\nValueError: exact cause"


@pytest.mark.asyncio
async def test_each_sandbox_run_removes_stale_junit_before_execution(tmp_path, monkeypatch):
    stale = tmp_path / ".portage-report.xml"
    stale.write_text('<testsuite><testcase name="old_green"/></testsuite>')

    class Sandbox:
        async def run(self, command, *, workdir, env):
            assert not stale.exists()
            return SimpleNamespace(exit_code=2, stdout="", stderr="collection crashed")

    monkeypatch.setattr(verify_module, "DockerSandbox", Sandbox)
    summary, _ = await verify_module._run_tests(str(tmp_path), [])
    assert summary["passed"] == 0
    assert "no report produced" in summary["error"]
