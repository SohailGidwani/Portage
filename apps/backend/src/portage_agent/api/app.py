"""FastAPI control plane: auth (Phase 7), jobs, and the eval proof endpoints.

Thin by design — it enqueues and reads. The worker owns execution. The API never runs
the graph and never touches the checkpointer. Every /jobs* route is ownership-checked
(rev-C: users see only their own jobs; `admin` sees all; the eval endpoints are public
aggregate-only).
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import bindparam, func, select, text

from portage_agent.auth import auth_router, current_user
from portage_agent.config import settings
from portage_agent.db import task_store
from portage_agent.db.models import EvalRun, Job, User
from portage_agent.db.session import AsyncSessionLocal
from portage_agent.logging_conf import setup_logging
from portage_agent.worker.queue import PostgresJobQueue

from .schemas import JobCreate, JobOut, TaskOut

setup_logging()
log = logging.getLogger("portage.api")

# root_path: hosted deployments serve the API behind a proxy prefix (Caddy strips /api
# before proxying). Routing is unchanged; docs URLs, url_for (the OAuth redirect_uri) and
# the refresh-cookie path pick the prefix up from here.
app = FastAPI(title="Portage API", version="0.0.0", root_path=settings.api_root_path)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS allowlist from env (rev-C: hosted mode must not run "*").
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,  # the /auth refresh cookie
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)

queue = PostgresJobQueue()


def _is_admin(user: User) -> bool:
    return user.role == "admin"


async def _get_job_or_404(job_id: uuid.UUID, user: User) -> Job:
    """Ownership-or-admin: a job either belongs to you, or you're admin, or it's 404
    (not 403 — don't confirm the UUID exists)."""
    async with AsyncSessionLocal() as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if not _is_admin(user) and job.user_id != user.id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


async def _enforce_demo_limits(user: User) -> None:
    """Rev-C demo-uptime protection: per-user concurrency + daily quota, global daily
    spend cap. Admin bypasses the per-user quotas, not the global cap."""
    async with AsyncSessionLocal() as session:
        if not _is_admin(user):
            running = (
                await session.execute(
                    select(func.count()).select_from(Job).where(
                        Job.user_id == user.id, Job.status.in_(("queued", "running"))
                    )
                )
            ).scalar_one()
            if running >= settings.max_concurrent_jobs_per_user:
                raise HTTPException(
                    status_code=429,
                    detail=f"limit: {settings.max_concurrent_jobs_per_user} concurrent "
                           "job(s) — wait for your current run to finish",
                )
            today = (
                await session.execute(
                    select(func.count()).select_from(Job).where(
                        Job.user_id == user.id,
                        Job.created_at >= func.date_trunc("day", func.now()),
                    )
                )
            ).scalar_one()
            if today >= settings.max_jobs_per_day_per_user:
                raise HTTPException(
                    status_code=429,
                    detail=f"limit: {settings.max_jobs_per_day_per_user} jobs/day — "
                           "come back tomorrow",
                )
        if settings.global_daily_spend_cap_usd > 0:
            # True platform spend today: every attempt's recorded cost across all jobs
            # (attempts_log is the ledger the eval numbers are built from too).
            spent = (
                await session.execute(text("""
                    SELECT coalesce(sum((a->>'cost_usd')::float), 0)
                      FROM tasks t
                      JOIN jobs j ON j.id = t.job_id,
                           jsonb_array_elements(t.attempts_log) a
                     WHERE j.created_at >= date_trunc('day', now())
                       AND a ? 'cost_usd'
                """))
            ).scalar_one()
            if float(spent) >= settings.global_daily_spend_cap_usd:
                raise HTTPException(
                    status_code=503,
                    detail="demo at capacity for today — please try again tomorrow",
                )


@app.get("/health")
async def health() -> dict:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}") from exc
    return {"status": "ok", "db": "ok"}


@app.post("/jobs", response_model=JobOut, status_code=201)
@limiter.limit("10/minute")
async def create_job(
    request: Request, body: JobCreate, user: User = Depends(current_user)
) -> Job:
    await _enforce_demo_limits(user)
    job_id = await queue.enqueue(
        repo_url=body.repo_url,
        migration_recipe=body.migration_recipe,
        config=body.config,
        user_id=user.id,
    )
    log.info("enqueued job=%s recipe=%s user=%s", job_id, body.migration_recipe, user.login)
    return await _get_job_or_404(job_id, user)


@app.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, user: User = Depends(current_user)) -> Job:
    return await _get_job_or_404(job_id, user)


@app.get("/jobs", response_model=list[JobOut])
async def list_jobs(limit: int = 50, user: User = Depends(current_user)) -> list[Job]:
    async with AsyncSessionLocal() as session:
        q = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if not _is_admin(user):
            q = q.where(Job.user_id == user.id)
        rows = (await session.execute(q)).scalars().all()
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
               avg(tasks_done::float / nullif(tasks_total, 0))        AS completion_mean,
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
                "completion_mean": round(float(r["completion_mean"] or 0), 3),
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
            "tasks_total": r.tasks_total, "tasks_done": r.tasks_done,
            "tasks_skipped": r.tasks_skipped,
            "recover_visits": r.recover_visits, "cost_usd": r.cost_usd,
            "wall_seconds": r.wall_seconds, "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@app.get("/jobs/{job_id}/tasks", response_model=list[TaskOut])
async def get_job_tasks(job_id: uuid.UUID, user: User = Depends(current_user)) -> list[dict]:
    """The job's migration plan: file tasks with per-file diffs and the per-attempt
    tier/model/recovery log — what the dashboard's task tree + recovery timeline render."""
    await _get_job_or_404(job_id, user)
    snapshots = await task_store.load_tasks(job_id)
    return [s.to_dict() for s in snapshots]


@app.get("/jobs/{job_id}/report")
async def get_job_report(job_id: uuid.UUID, user: User = Depends(current_user)) -> dict:
    """The structured run report (report.json) — includes the full migration diff and the
    recovery summary. Served off the shared workspaces volume (mounted read-only here).
    Ownership-or-admin (this used to be readable by anyone holding the UUID)."""
    job = await _get_job_or_404(job_id, user)
    if not job.report_path:
        raise HTTPException(status_code=404, detail="no report for this job (yet)")
    try:
        return json.loads(Path(job.report_path).read_text())
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"report file missing: {job.report_path}"
        ) from exc
