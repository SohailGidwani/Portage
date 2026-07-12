from __future__ import annotations

import httpx
from rich.console import Console

from portage_agent.cli.main import (
    _client,
    _diff_stats,
    _honest_outcome,
    _summary_view,
    _write_patch,
)


def test_honest_outcome_uses_report_instead_of_passing_tests() -> None:
    job = {"status": "done", "test_summary": {"passed": 4, "total": 4}}

    assert _honest_outcome(job, {"migration_outcome": "failed"}) == "failed"


def test_diff_stats_ignore_patch_metadata() -> None:
    diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1,2 @@
-old
+new
+more
"""

    assert _diff_stats(diff) == (1, 2, 1)


def test_write_patch_creates_parent_directory(tmp_path) -> None:
    output = tmp_path / "review" / "migration.patch"

    path = _write_patch("+change\n", str(output))

    assert path == output
    assert output.read_text() == "+change\n"


def test_client_reads_api_key_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("PORTAGE_API_KEY", "pk_test")

    with _client("http://portage.test") as client:
        assert client.headers["Authorization"] == "Bearer pk_test"


def test_summary_marks_rollback_false_green_as_failed() -> None:
    job_id = "00000000-0000-0000-0000-000000000001"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tasks"):
            return httpx.Response(
                200,
                json=[
                    {
                        "target_path": "app.py",
                        "status": "skipped",
                        "attempts": 2,
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "migration_outcome": "failed",
                "test_summary": {"passed": 4, "total": 4},
                "recovery": {"visits": 1},
            },
        )

    client = httpx.Client(base_url="http://portage.test", transport=httpx.MockTransport(handler))
    view, green = _summary_view(
        client,
        {
            "id": job_id,
            "status": "done",
            "migration_recipe": "flask_to_fastapi",
            "repo_url": "/repo",
            "test_summary": {"passed": 4, "total": 4},
        },
    )
    output = Console(record=True, width=100)
    output.print(view)
    rendered = output.export_text()

    assert green is False
    assert "FAILED" in rendered
    assert "1 skipped" in rendered
