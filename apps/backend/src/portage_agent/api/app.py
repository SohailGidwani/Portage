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
from sqlalchemy import select, text

from portage_agent.db import task_store
from portage_agent.db.models import Job
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
