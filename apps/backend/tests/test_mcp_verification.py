"""The MCP verification result must come from this execution, with passing tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from portage_agent.mcp import server


@pytest.mark.parametrize("outcome", ["crash", "skipped", "nonzero", "passed"])
async def test_mcp_verification_uses_fresh_report_and_process_outcome(
    tmp_path, monkeypatch, outcome,
):
    stale = tmp_path / ".portage-report.xml"
    original = '<testsuite><testcase name="old_green"/></testsuite>'
    stale.write_text(original)
    workdirs = []

    class Sandbox:
        def __init__(self, *, volume, mount):
            self.root = Path(volume)
            workdirs.append(self.root)

        async def run(self, command, *, workdir, timeout):
            report = self.root / ".portage-report.xml"
            assert not report.exists()
            if outcome != "crash":
                skipped = '<skipped message="not exercised"/>' if outcome == "skipped" else ""
                report.write_text(
                    f'<testsuite><testcase name="current">{skipped}</testcase></testsuite>'
                )
            return SimpleNamespace(
                exit_code=2 if outcome in {"crash", "nonzero"} else 0,
                stdout="", stderr="collection crashed" if outcome == "crash" else "",
            )

    monkeypatch.setattr(server, "DockerSandbox", Sandbox)
    result = await server.verify_patch_in_sandbox(str(tmp_path))

    if outcome == "crash":
        assert result["ok"] is False
        assert "no test report produced" in result["error"]
    else:
        assert result["ok"] is True
        assert result["passed"] is (outcome == "passed")
        assert result["tests"]["passed"] == (0 if outcome == "skipped" else 1)
    assert stale.read_text() == original
    assert all(not path.exists() for path in workdirs)
