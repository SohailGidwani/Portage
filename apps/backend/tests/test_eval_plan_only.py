"""Plan-only evaluation uses normal persisted jobs and classifies architect outcomes."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from portage_agent.eval.corpus import CorpusRepo
from portage_agent.eval.harness import (
    HarnessConfig,
    RunResult,
    _harvest,
    classify_architect_rejection,
    run_suite,
)


def test_plan_only_harvest_reports_acceptance_and_stable_rejection_classes(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({
        "tasks_total": 3,
        "tasks_done": 0,
        "tasks": [{
            "type": "artifact_architect",
            "status": "skipped",
            "error": "artifact architecture rejected: artifact 1 must declare at least one export",
        }],
        "artifact_plan": {
            "architect": {"status": "skipped", "repairs": 1},
        },
        "recovery": {},
        "llm_usage": {"calls": 2, "cost_usd": 0.031},
        "oracle_integrity": {"integrity_rate": 1.0},
        "migration_outcome": "plan_rejected",
    }))
    now = datetime.now(UTC)
    job = SimpleNamespace(
        status="done", test_summary={}, report_path=str(report),
        created_at=now, updated_at=now + timedelta(seconds=4),
    )
    repo = CorpusRepo(
        name="sample", repo_url="/sample", recipe="flask_to_fastapi",
        tier="structural", stresses=[], source="bundled",
    )

    result = _harvest(repo, "baseline", 1, job, uuid.uuid4(), plan_only=True)

    assert result.status == "red"
    assert result.architect_accepted is False
    assert result.architect_repairs == 1
    assert result.architect_rejection_class == "schema_missing_export"
    assert result.metric("rejection_schema_missing_export") == 1.0

    assert classify_architect_rejection(
        "artifact policy violations: direct_test_surface requires exactly one owner artifact, got 0"
    ) == "policy_missing_owner"


def test_plan_only_acceptance_metric_is_independent_of_test_counts():
    result = RunResult(
        corpus_name="sample", scenario="baseline", k_index=1, job_id=None,
        status="green", architect_accepted=True,
    )
    assert result.metric("architect_acceptance_rate") == 1.0


@pytest.mark.asyncio
async def test_replay_suite_requires_diagnostic_name_before_enqueue():
    with pytest.raises(ValueError, match="replay suite names"):
        await run_suite([], HarnessConfig(suite="headline", replay_plan=[{"path": "x.py"}]))
