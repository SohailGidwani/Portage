"""FastAPI control plane (Phase 0): health + the jobs endpoints.

Thin by design — it enqueues and reads. The worker owns execution. The API never runs
the graph and never touches the checkpointer.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import bindparam, select, text

from portage_agent.db import task_store
from portage_agent.db.models import EvalRun, Job
from portage_agent.db.session import AsyncSessionLocal
from portage_agent.logging_conf import setup_logging
from portage_agent.worker.queue import PostgresJobQueue

from .schemas import JobCreate, JobOut, TaskOut

setup_logging()
log = logging.getLogger("portage.api")

app = FastAPI(title="Portage API", version="0.0.0")

# The browser (frontend at :3000) calls this API at localhost:8000 directly, so CORS is
# required. Container-to-container calls use the `api` service name and don't need it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

queue = PostgresJobQueue()


async def _get_job_or_404(job_id: uuid.UUID) -> Job:
    async with AsyncSessionLocal() as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/health")
async def health() -> dict:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}") from exc
    return {"status": "ok", "db": "ok"}


@app.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(body: JobCreate) -> Job:
    job_id = await queue.enqueue(
        repo_url=body.repo_url,
        migration_recipe=body.migration_recipe,
        config=body.config,
    )
    log.info("enqueued job=%s recipe=%s", job_id, body.migration_recipe)
    return await _get_job_or_404(job_id)


@app.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID) -> Job:
    return await _get_job_or_404(job_id)


@app.get("/jobs", response_model=list[JobOut])
async def list_jobs(limit: int = 50) -> list[Job]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(select(Job).order_by(Job.created_at.desc()).limit(limit))
        ).scalars().all()
    return list(rows)


@app.get("/eval/leaderboard")
async def eval_leaderboard(suites: str = "") -> dict:
    """The proof-page aggregate: per (corpus repo × scenario) over the `runs` table —
    green rate, test-pass mean±variance, cost, wall, recovery/escalation. Aggregate
    numbers only (rev-C isolation rule: no user repo contents on shared surfaces).
    ``suites``: optional comma-separated filter; response lists distinct suites so the
    client can offer a selector."""
    wanted = [s.strip() for s in suites.split(",") if s.strip()]
    where = "WHERE suite IN :suites" if wanted else ""
    sql = text(f"""
        SELECT corpus_name, scenario,
               count(*)                                    AS runs,
               count(*) FILTER (WHERE status = 'green')    AS green,
               avg(tests_passed::float / nullif(tests_total, 0))      AS test_pass_mean,
               variance(tests_passed::float / nullif(tests_total, 0)) AS test_pass_variance,
               avg(cost_usd)                               AS cost_mean,
               avg(wall_seconds)                           AS wall_mean,
               avg(recover_visits)                         AS recover_visits_mean,
               sum(escalation_attempted)                   AS escalation_attempted,
               sum(escalation_rescued)                     AS escalation_rescued
          FROM runs {where}
      GROUP BY corpus_name, scenario
      ORDER BY scenario, corpus_name
    """)
    if wanted:
        sql = sql.bindparams(bindparam("suites", expanding=True))
    async with AsyncSessionLocal() as session:
        params = {"suites": wanted} if wanted else {}
        rows = (await session.execute(sql, params)).mappings().all()
        suite_rows = (
            await session.execute(
                text("SELECT suite, max(created_at) AS latest FROM runs "
                     "GROUP BY suite ORDER BY latest DESC")
            )
        ).mappings().all()

    # Tier/notes come from the corpus manifest when it's mounted (compose mounts
    # ./corpus into api); the endpoint degrades to tierless rows without it.
    tiers: dict[str, str] = {}
    try:
        from portage_agent.eval.corpus import load_corpus

        tiers = {r.name: r.tier for r in load_corpus("/corpus/corpus.toml")}
    except Exception:  # noqa: BLE001 - manifest optional here
        pass

    return {
        "suites": [s["suite"] for s in suite_rows],
        "rows": [
            {
                "corpus_name": r["corpus_name"],
                "tier": tiers.get(r["corpus_name"], ""),
                "scenario": r["scenario"],
                "runs": r["runs"],
                "green": r["green"],
                "green_rate": round(r["green"] / r["runs"], 3) if r["runs"] else 0.0,
                "test_pass_mean": round(float(r["test_pass_mean"] or 0), 3),
                "test_pass_variance": round(float(r["test_pass_variance"] or 0), 5),
                "cost_mean": round(float(r["cost_mean"] or 0), 4),
                "wall_mean": round(float(r["wall_mean"] or 0), 1),
                "recover_visits_mean": round(float(r["recover_visits_mean"] or 0), 1),
                "escalation_attempted": int(r["escalation_attempted"] or 0),
                "escalation_rescued": int(r["escalation_rescued"] or 0),
            }
            for r in rows
        ],
    }


@app.get("/eval/runs")
async def list_eval_runs(limit: int = 25, scenario: str = "") -> list[dict]:
    """Latest harness runs (the `runs` table) — the dashboard's eval panel.
    ``scenario=faults`` filters to fault-injection runs (the chaos-recovery view)."""
    async with AsyncSessionLocal() as session:
        q = select(EvalRun).order_by(EvalRun.created_at.desc()).limit(limit)
        if scenario == "faults":
            q = q.where(EvalRun.scenario != "baseline")
        elif scenario:
            q = q.where(EvalRun.scenario == scenario)
        rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": str(r.id), "suite": r.suite, "corpus_name": r.corpus_name,
            "scenario": r.scenario, "k_index": r.k_index,
            "job_id": str(r.job_id) if r.job_id else None, "status": r.status,
            "tests_passed": r.tests_passed, "tests_total": r.tests_total,
            "recover_visits": r.recover_visits, "cost_usd": r.cost_usd,
            "wall_seconds": r.wall_seconds, "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@app.get("/jobs/{job_id}/tasks", response_model=list[TaskOut])
async def get_job_tasks(job_id: uuid.UUID) -> list[dict]:
    """The job's migration plan: file tasks with per-file diffs and the per-attempt
    tier/model/recovery log — what the dashboard's task tree + recovery timeline render."""
    await _get_job_or_404(job_id)
    snapshots = await task_store.load_tasks(job_id)
    return [s.to_dict() for s in snapshots]


@app.get("/jobs/{job_id}/report")
async def get_job_report(job_id: uuid.UUID) -> dict:
    """The structured run report (report.json) — includes the full migration diff and the
    recovery summary. Served off the shared workspaces volume (mounted read-only here)."""
    job = await _get_job_or_404(job_id)
    if not job.report_path:
        raise HTTPException(status_code=404, detail="no report for this job (yet)")
    try:
        return json.loads(Path(job.report_path).read_text())
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"report file missing: {job.report_path}"
        ) from exc
