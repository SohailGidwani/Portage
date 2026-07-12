"""Bounded recovery and graph routing invariants."""

import importlib
from types import SimpleNamespace

import pytest

from portage_agent.agent.graph import _after_integrate, _after_verify
from portage_agent.agent.nodes.recover import repeated_failure_count
from portage_agent.agent.nodes.verify import failure_fingerprint
from portage_agent.sandbox import parse_junit_xml

verify_module = importlib.import_module("portage_agent.agent.nodes.verify")


def test_failure_fingerprint_ignores_line_numbers_timings_and_ansi():
    first = failure_fingerprint("\x1b[31mboom line 42 in 1.20s", "same diff", ["a.py"])
    second = failure_fingerprint("boom line 99 in 4s", "same diff", ["a.py"])
    assert first == second
    assert first != failure_fingerprint("different", "same diff", ["a.py"])


def test_repeat_count_includes_current_occurrence():
    actions = [{"fingerprint": "same"}, {"fingerprint": "other"}]
    assert repeated_failure_count(actions, "same") == 2


def test_verify_continues_batches_before_integrating():
    assert _after_verify({"verify_passed": True, "migrate": True,
                          "has_pending_tasks": True}) == "execute"
    assert _after_verify({"verify_passed": True, "migrate": True,
                          "has_pending_tasks": False}) == "integrate"


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
