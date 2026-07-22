"""Dependency batches are deterministic and keep coupled seams atomic."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import portage_agent.agent.nodes.execute as execute_module
from portage_agent.agent.nodes.common import create_cut_checkpoint
from portage_agent.agent.nodes.executable_cut import build_executable_cut_analysis
from portage_agent.agent.nodes.execute import (
    _restore_rejected_batch,
    expand_to_verifiable_batch,
    is_initial_cluster,
    runtime_contract_repair_attempt,
    select_execution_batch,
)
from portage_agent.recipes.base import PlannedFile, Subtask


def _task(
    path: str, order: int, status: str = "pending", tests: list[str] | None = None,
    role: str = "support",
) -> SimpleNamespace:
    return SimpleNamespace(
        target_path=path, order_index=order, status=status, type=role,
        verify_spec={"affected_tests": tests or []}, attempts=3, attempts_log=[],
    )


def test_next_dependency_file_is_a_single_batch():
    tasks = [_task("db.py", 0), _task("app.py", 10), _task("test_app.py", 20)]
    assert select_execution_batch(tasks, []) == ["db.py"]


def test_coupled_unit_runs_atomically_when_its_first_dependency_is_next():
    tasks = [_task("compat.py", 0, "done"), _task("db.py", 10), _task("app.py", 20)]
    units = [{"id": "seam", "paths": ["db.py", "app.py"]}]
    assert select_execution_batch(tasks, units) == ["db.py", "app.py"]


def test_later_unit_does_not_jump_an_earlier_independent_dependency():
    tasks = [_task("models.py", 10), _task("app.py", 20), _task("conftest.py", 30)]
    units = [{"id": "seam", "paths": ["app.py", "conftest.py"]}]
    assert select_execution_batch(tasks, units) == ["models.py"]


def test_empty_blast_radius_accumulates_until_a_real_test_boundary():
    tasks = [
        _task("db.py", 10), _task("views.py", 20), _task("app.py", 30),
        _task("conftest.py", 40), _task("test_app.py", 50, tests=["test_app.py"]),
    ]
    units = [{"id": "seam", "paths": ["db.py", "app.py", "conftest.py"]}]
    assert expand_to_verifiable_batch(tasks, units, ["db.py", "app.py", "conftest.py"]) == [
        "db.py", "views.py", "app.py", "conftest.py", "test_app.py",
    ]


def test_deterministic_adapter_remains_its_own_foundation_batch():
    tasks = [
        _task("compat.py", 0, role="test_compat"),
        _task("app.py", 10, tests=["test_app.py"]),
    ]
    assert expand_to_verifiable_batch(tasks, [], ["compat.py"]) == ["compat.py"]


@pytest.mark.asyncio
async def test_rejected_member_restores_the_entire_executable_cut(
    tmp_path, monkeypatch,
):
    source = tmp_path / "pkg" / "app.py"
    created = tmp_path / "pkg" / "runtime.py"
    source.parent.mkdir()
    source.write_text("APP = 'original'\n")
    checkpoint = create_cut_checkpoint(
        str(tmp_path), ["pkg/runtime.py", "pkg/app.py"], {
            "pkg/runtime.py": {"status": "pending", "action": "create"},
            "pkg/app.py": {"status": "pending", "action": "rewrite"},
        },
    )
    created.write_text("RUNTIME = 'generated'\n")
    source.write_text("APP = 'generated'\n")
    tasks = [
        SimpleNamespace(
            id=uuid.uuid4(), target_path="pkg/runtime.py", status="done", error=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(), target_path="pkg/app.py", status="skipped",
            error="persistent contract violation",
        ),
    ]
    update = AsyncMock()
    monkeypatch.setattr(execute_module.task_store, "update_task", update)
    monkeypatch.setattr(execute_module, "run_git", AsyncMock(return_value=(0, "")))

    restored = await _restore_rejected_batch(
        str(tmp_path), ["pkg/runtime.py", "pkg/app.py"], checkpoint, tasks,
    )

    assert restored == ["pkg/runtime.py", "pkg/app.py"]
    assert not created.exists()
    assert source.read_text() == "APP = 'original'\n"
    assert all(
        call.kwargs["status"] == "skipped" for call in update.await_args_list
    )
    assert all(
        call.kwargs["append_attempt"]["action"]
        == "rejected_cut_checkpoint_restore"
        for call in update.await_args_list
    )


def test_executable_cut_schedules_provider_and_incompatible_consumer_together():
    tasks = [
        _task("views.py", 10),
        _task("app.py", 20, tests=["test_app.py"]),
        _task("other.py", 30, tests=["test_other.py"]),
    ]
    cuts = [{
        "id": "executable-cut-1",
        "paths": ["views.py", "app.py"],
        "mode": "coordinated",
    }]

    assert select_execution_batch(tasks, cuts) == ["views.py", "app.py"]
    assert expand_to_verifiable_batch(tasks, cuts, ["views.py", "app.py"]) == [
        "views.py", "app.py",
    ]


def test_called_support_provider_shares_the_factory_verification_cut():
    files = {
        "pkg/errors.py": "from flask import render_template\ndef register(app):\n    pass\n",
        "pkg/__init__.py": (
            "from flask import Flask\nfrom .errors import register\n"
            "def create_app():\n    app = Flask(__name__)\n    register(app)\n    return app\n"
        ),
    }
    planned = [
        PlannedFile(
            "pkg/errors.py", "support",
            subtasks=[Subtask("templates_render", "templates", "")],
        ),
        PlannedFile("pkg/__init__.py", "app_factory"),
    ]

    analysis = build_executable_cut_analysis(files, planned, {}, {})

    assert analysis["cuts"][0]["paths"] == ["pkg/errors.py", "pkg/__init__.py"]
    assert analysis["cuts"][0]["edge_kinds"] == ["factory_provider_call"]


def test_targeted_consumer_repair_retains_its_full_cut_verification_boundary():
    tasks = [
        _task("compat.py", 10, "done"),
        _task("errors.py", 20, "done"),
        _task("app.py", 30, tests=["tests/test_app.py"]),
    ]
    cuts = [{
        "id": "executable-cut-1",
        "paths": ["compat.py", "errors.py", "app.py"],
        "mode": "coordinated",
    }]

    selected = select_execution_batch(tasks, cuts)
    assert selected == ["compat.py", "errors.py", "app.py"]
    assert expand_to_verifiable_batch(tasks, cuts, selected) == selected
    assert [task.target_path for task in tasks if task.status == "pending"] == ["app.py"]


def test_runtime_contract_repair_ordinal_does_not_reuse_task_attempts():
    task = _task("app.py", 10)
    task.attempts_log = [
        {"action": "migrate"},
        {"action": "contract_repair", "scope": "runtime_targeted"},
    ]

    assert runtime_contract_repair_attempt(task, "app.py") == 2
    assert runtime_contract_repair_attempt(task, "other.py") is None
    assert task.attempts == 3


def test_large_executable_cut_is_one_verification_batch():
    tasks = [_task(f"view{i}.py", i * 10) for i in range(5)]
    tasks.append(_task("app.py", 50, tests=["test_app.py"]))
    paths = [task.target_path for task in tasks]
    cuts = [{"id": "large", "paths": paths, "mode": "batch_only"}]

    assert select_execution_batch(tasks, cuts) == paths
    assert expand_to_verifiable_batch(tasks, cuts, paths) == paths


def test_only_untouched_cut_uses_coordinated_generation():
    tasks = [_task("db.py", 10), _task("app.py", 20)]
    by_path = {task.target_path: task for task in tasks}
    for task in tasks:
        task.attempts = 0

    assert is_initial_cluster(["db.py", "app.py"], by_path)
    tasks[0].attempts = 1
    assert not is_initial_cluster(["db.py", "app.py"], by_path)
