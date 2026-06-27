"""Worker loop: claim a job, run/resume its graph, mark it done. Repeat.

The heartbeat runs as its own task (its own DB connection) and is cancelled in `finally`.
Crash-safety: if the process dies, the heartbeat stops, the lease expires, and any worker
(including the restarted one) re-claims the job; ``run_job`` then resumes from checkpoint.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from portage_agent.agent import build_graph, open_checkpointer, run_job
from portage_agent.config import settings
from portage_agent.core.interfaces import ClaimedJob
from portage_agent.logging_conf import setup_logging
from portage_agent.worker.queue import PostgresJobQueue

log = logging.getLogger("portage.worker")


async def _heartbeat_loop(queue: PostgresJobQueue, job: ClaimedJob) -> None:
    while True:
        await asyncio.sleep(settings.heartbeat_interval_seconds)
        try:
            await queue.heartbeat(job.id, worker_id=settings.worker_id)
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("heartbeat failed for job=%s", job.id)


async def _process(queue: PostgresJobQueue, graph, job: ClaimedJob) -> None:
    log.info("claimed job=%s recipe=%s", job.id, job.migration_recipe)
    hb = asyncio.create_task(_heartbeat_loop(queue, job))
    try:
        final = await run_job(
            graph,
            job_id=job.id,
            repo_url=job.repo_url,
            migration_recipe=job.migration_recipe,
            job_config=job.config,
        )
        await queue.finish(
            job.id,
            report_path=final.get("report_path"),
            test_summary=final.get("test_summary"),
            graph_summary=final.get("graph_summary"),
        )
        log.info("job=%s COMPLETE", job.id)
    except Exception as exc:
        log.exception("job=%s FAILED", job.id)
        await queue.fail(job.id, error=repr(exc))
    finally:
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass


async def main() -> None:
    setup_logging()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    queue = PostgresJobQueue()
    async with open_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)
        log.info(
            "worker '%s' polling (lease=%ss heartbeat=%ss)",
            settings.worker_id,
            settings.job_lease_seconds,
            settings.heartbeat_interval_seconds,
        )
        while not stop.is_set():
            job = await queue.claim(
                worker_id=settings.worker_id, lease_seconds=settings.job_lease_seconds
            )
            if job is None:
                # Idle: wait one poll interval, but wake immediately on shutdown.
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=settings.worker_poll_interval_seconds
                    )
                except TimeoutError:
                    pass
                continue
            await _process(queue, graph, job)

    log.info("worker '%s' shutting down", settings.worker_id)


if __name__ == "__main__":
    asyncio.run(main())
