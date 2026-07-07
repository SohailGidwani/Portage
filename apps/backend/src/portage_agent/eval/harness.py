"""The eval harness — recipe-agnostic reliability measurement (plan §11).

Drives the REAL pipeline: for every (corpus repo × scenario × k) it enqueues a normal job
on the Postgres queue, lets the running worker execute it (checkpointing, sandbox, recovery
and all), then harvests the job row + report.json into one `runs` row. After the batch it
aggregates mean±variance per metric into `metrics` rows. Those two tables are the contract
the leaderboard reads — the harness never prints numbers it didn't persist.

Scenarios are job-config fault injections; `baseline` is fault-free. The three faults are
the phase3_check scenarios promoted into standing eval cases. Runs are sequential (one
worker), so wall-clock per run stays honest.

Fault-recovery rate — the headline metric — is `suite_green` on a fault scenario: the run
counts as recovered only if the injected fault still ended in a fully green suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from portage_agent.config import settings
from portage_agent.db.models import EvalMetric, EvalRun, Job
from portage_agent.db.session import AsyncSessionLocal
from portage_agent.worker.queue import PostgresJobQueue

from .corpus import CorpusRepo

log = logging.getLogger("portage.eval")

# Scenario name -> job config. The faults are deterministic (see agent/nodes/execute.py
# and plan.py); kill/resume stays in scripts/dod_check.sh — the harness can't SIGKILL the
# worker it depends on.
SCENARIOS: dict[str, dict] = {
    "baseline": {},
    "bad_patch": {"inject_fault": "bad_patch"},
    "bad_patch_until_escalation": {"inject_fault": "bad_patch_until_escalation"},
    "drop_task": {"inject_fault": "drop_task"},
}

# Metrics aggregated over the K runs of one (repo, scenario) cell.
_METRICS = (
    "suite_green",
    "test_pass_rate",
    "completion_rate",
    "recover_visits",
    "escalation_rescued",
    "llm_calls",
    "cost_usd",
    "wall_seconds",
)


@dataclass(slots=True)
class RunResult:
    """Flat per-run record; mirrors one `runs` row."""

    corpus_name: str
    scenario: str
    k_index: int
    job_id: uuid.UUID | None
    status: str  # green | red | error | timeout
    tests_total: int = 0
    tests_passed: int = 0
    tasks_total: int = 0
    tasks_done: int = 0
    tasks_skipped: int = 0
    recover_visits: int = 0
    escalation_attempted: int = 0
    escalation_rescued: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0

    def metric(self, name: str) -> float:
        if name == "suite_green":
            return 1.0 if self.status == "green" else 0.0
        if name == "test_pass_rate":
            return self.tests_passed / self.tests_total if self.tests_total else 0.0
        if name == "completion_rate":
            return self.tasks_done / self.tasks_total if self.tasks_total else 0.0
        return float(getattr(self, name))


@dataclass(slots=True)
class HarnessConfig:
    suite: str
    k: int = 2
    scenarios: list[str] = field(default_factory=lambda: ["baseline"])
    job_timeout_seconds: int = 900
    poll_seconds: float = 3.0


async def _wait_for_job(job_id: uuid.UUID, cfg: HarnessConfig) -> Job | None:
    """Poll until the job is terminal; None on timeout (the worker may still finish it
    later — the run is recorded as `timeout` either way, so the batch keeps moving)."""
    deadline = asyncio.get_running_loop().time() + cfg.job_timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with AsyncSessionLocal() as session:
            job = (
                await session.execute(select(Job).where(Job.id == job_id))
            ).scalar_one_or_none()
        if job is not None and job.status in ("done", "failed"):
            return job
        await asyncio.sleep(cfg.poll_seconds)
    return None


def _harvest(repo: CorpusRepo, scenario: str, k_index: int,
             job: Job | None, job_id: uuid.UUID) -> RunResult:
    """Fold the job row + its report.json into one flat run record."""
    r = RunResult(corpus_name=repo.name, scenario=scenario, k_index=k_index, job_id=job_id,
                  status="timeout")
    if job is None:
        return r
    if job.status == "failed":
        r.status = "error"
    ts = job.test_summary or {}
    r.tests_total = int(ts.get("total") or 0)
    r.tests_passed = int(ts.get("passed") or 0)
    r.wall_seconds = round((job.updated_at - job.created_at).total_seconds(), 1)

    report: dict = {}
    if job.report_path:
        try:
            report = json.loads(Path(job.report_path).read_text())
        except OSError as exc:  # pragma: no cover - volume not mounted / file gone
            log.warning("run %s/%s k=%s: report unreadable: %s",
                        repo.name, scenario, k_index, exc)
    r.tasks_total = int(report.get("tasks_total") or 0)
    r.tasks_done = int(report.get("tasks_done") or 0)
    rec = report.get("recovery") or {}
    r.tasks_skipped = int(rec.get("tasks_skipped") or 0)
    r.recover_visits = int(rec.get("visits") or 0)
    r.escalation_attempted = int(rec.get("escalation_attempted") or 0)
    r.escalation_rescued = int(rec.get("escalation_rescued") or 0)
    usage = report.get("llm_usage") or {}
    r.llm_calls = int(usage.get("calls") or 0)
    r.prompt_tokens = int(usage.get("prompt_tokens") or 0)
    r.completion_tokens = int(usage.get("completion_tokens") or 0)
    r.cost_usd = float(usage.get("cost_usd") or 0.0)

    # Green = the MIGRATION succeeded, not merely "tests passed": every planned task done
    # (none skipped/rolled back) AND the full suite green. Skip-and-continue can roll the
    # whole worktree back to original sources, whose suite then passes — that is an honest
    # failure report, and it must never score as a green migration (observed on flaskr).
    if job.status == "done":
        suite_ok = r.tests_total > 0 and r.tests_passed == r.tests_total
        fully_migrated = r.tasks_total > 0 and r.tasks_done == r.tasks_total
        r.status = "green" if (suite_ok and fully_migrated) else "red"
    return r


def _labels() -> tuple[str, str]:
    driver = settings.llm_driver_model_label or settings.llm_driver_model
    escalation = settings.llm_escalation_model_label or settings.llm_escalation_model
    return driver, escalation


async def _persist_run(cfg: HarnessConfig, repo: CorpusRepo, r: RunResult) -> None:
    driver, escalation = _labels()
    async with AsyncSessionLocal() as session, session.begin():
        session.add(EvalRun(
            id=uuid.uuid4(), suite=cfg.suite, corpus_name=r.corpus_name,
            repo_url=repo.repo_url, recipe=repo.recipe, scenario=r.scenario,
            k_index=r.k_index, job_id=r.job_id, driver_model=driver,
            escalation_model=escalation, status=r.status,
            tests_total=r.tests_total, tests_passed=r.tests_passed,
            tasks_total=r.tasks_total, tasks_done=r.tasks_done,
            tasks_skipped=r.tasks_skipped, recover_visits=r.recover_visits,
            escalation_attempted=r.escalation_attempted,
            escalation_rescued=r.escalation_rescued, llm_calls=r.llm_calls,
            prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens,
            cost_usd=r.cost_usd, wall_seconds=r.wall_seconds,
        ))


async def _persist_metrics(cfg: HarnessConfig, corpus_name: str, scenario: str,
                           results: list[RunResult]) -> list[EvalMetric]:
    driver, _ = _labels()
    rows: list[EvalMetric] = []
    for name in _METRICS:
        values = [r.metric(name) for r in results]
        mean = statistics.mean(values)
        variance = statistics.variance(values) if len(values) > 1 else 0.0
        rows.append(EvalMetric(
            id=uuid.uuid4(), suite=cfg.suite, corpus_name=corpus_name, scenario=scenario,
            driver_model=driver, metric=name, k=len(values),
            mean=round(mean, 4), variance=round(variance, 6),
        ))
    async with AsyncSessionLocal() as session, session.begin():
        session.add_all(rows)
    return rows


async def run_suite(repos: list[CorpusRepo], cfg: HarnessConfig) -> list[EvalMetric]:
    """Run the full (repos × scenarios × K) grid sequentially; persist every run row as it
    lands (a crashed harness loses nothing already measured) and the metric rows at the
    end of each cell. Returns all metric rows for display."""
    unknown = [s for s in cfg.scenarios if s not in SCENARIOS]
    if unknown:
        raise ValueError(f"unknown scenarios {unknown}; known: {sorted(SCENARIOS)}")

    queue = PostgresJobQueue()
    all_metrics: list[EvalMetric] = []
    total = len(repos) * len(cfg.scenarios) * cfg.k
    n = 0
    log.info("suite=%s | %s repos x %s scenarios x k=%s = %s runs",
             cfg.suite, len(repos), len(cfg.scenarios), cfg.k, total)

    for repo in repos:
        for scenario in cfg.scenarios:
            cell: list[RunResult] = []
            for k_index in range(1, cfg.k + 1):
                n += 1
                job_id = await queue.enqueue(
                    repo_url=repo.repo_url, migration_recipe=repo.recipe,
                    config=repo.job_config(SCENARIOS[scenario]),
                )
                log.info("[%s/%s] %s/%s k=%s -> job=%s",
                         n, total, repo.name, scenario, k_index, job_id)
                job = await _wait_for_job(job_id, cfg)
                result = _harvest(repo, scenario, k_index, job, job_id)
                await _persist_run(cfg, repo, result)
                log.info("[%s/%s] %s | tests=%s/%s cost=$%.4f wall=%.0fs recover=%s",
                         n, total, result.status.upper(), result.tests_passed,
                         result.tests_total, result.cost_usd, result.wall_seconds,
                         result.recover_visits)
                cell.append(result)
            all_metrics.extend(await _persist_metrics(cfg, repo.name, scenario, cell))

    return all_metrics


def format_metrics_table(metrics: list[EvalMetric]) -> str:
    """Markdown mean±variance table, one row per (repo, scenario, metric)."""
    lines = [
        "| corpus repo | scenario | metric | k | mean | variance |",
        "|---|---|---|---|---|---|",
    ]
    for m in metrics:
        lines.append(
            f"| {m.corpus_name} | {m.scenario} | {m.metric} | {m.k} "
            f"| {m.mean:.4f} | {m.variance:.6f} |"
        )
    return "\n".join(lines)


def default_suite_name() -> str:
    return "eval-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
