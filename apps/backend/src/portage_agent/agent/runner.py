"""Run (or resume) the graph for a job. This is where resume-vs-restart is decided.

The discriminator is the checkpoint, looked up by ``thread_id = job_id``:
  * no checkpoint            -> fresh start: ``ainvoke(initial_state, config)``
  * checkpoint with pending  -> RESUME:      ``ainvoke(None, config)``  (do NOT re-pass
    the input — that would re-seed state instead of continuing)
  * checkpoint, nothing next -> already done: idempotent no-op
"""

from __future__ import annotations

import logging
import uuid

log = logging.getLogger("portage.agent")


async def run_job(
    graph,
    *,
    job_id: uuid.UUID,
    repo_url: str,
    migration_recipe: str,
    job_config: dict | None = None,
) -> dict:
    config = {"configurable": {"thread_id": str(job_id)}}
    snapshot = await graph.aget_state(config)

    if snapshot.created_at is None:
        log.info("job=%s no prior checkpoint -> FRESH START", job_id)
        initial = {
            "job_id": str(job_id),
            "repo_url": repo_url,
            "migration_recipe": migration_recipe,
            "config": job_config or {},
        }
        return await graph.ainvoke(initial, config)

    if snapshot.next:
        log.info(
            "job=%s RESUME from checkpoint | next=%s loaded_step_log=%s loaded_graph_nodes=%s",
            job_id,
            snapshot.next,
            snapshot.values.get("step_log"),
            (snapshot.values.get("graph_summary") or {}).get("total_nodes"),
        )
        return await graph.ainvoke(None, config)

    log.info("job=%s checkpoint is terminal -> idempotent no-op", job_id)
    return snapshot.values
