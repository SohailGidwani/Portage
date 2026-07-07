"""Report node — emit the structured run report and persist the final result.

For a migration run the authoritative test result is Integrate's full-suite summary (the DoD
gate), so that is what lands on the job row as `test_summary`. The richer migration detail
(the task tree, the affected-test subset, the diff) goes into report.json (and the tasks
table) for the dashboard.
"""

from __future__ import annotations

import json
import logging
import uuid

from portage_agent.db import task_store
from portage_agent.storage import LocalStorage

from ..state import GraphState

log = logging.getLogger("portage.agent")


async def report_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    migrate = bool(state.get("migrate"))
    verify_summary = state.get("test_summary") or {}
    integrate_summary = state.get("integrate_summary") or {}
    final = integrate_summary if migrate else verify_summary

    # Reload the plan from Postgres — the truth. The state copy is Execute's LAST output
    # and goes stale when a later Recover skips tasks (observed: a fully-rolled-back run
    # reporting 5/6 done). The DB rows are updated by every node that touches a task.
    if migrate:
        snapshots = await task_store.load_tasks(uuid.UUID(job_id))
        plan = [s.to_state_dict() for s in snapshots]
    else:
        plan = state.get("plan") or []
    tasks_done = sum(1 for t in plan if t.get("status") == "done")

    # Phase 3: the measured recovery/escalation record. "Rescued" = the task ended done and
    # its successful path needed an escalation-tier attempt — the eval-facing number.
    escalation_tasks = [
        t for t in plan
        if any(a.get("tier") == "escalation" for a in t.get("attempts_log", []))
    ]
    recovery = {
        "visits": state.get("recover_visits", 0),
        "actions": state.get("recovery_actions", []),
        "tasks_skipped": sum(1 for t in plan if t.get("status") == "skipped"),
        "escalation_attempted": len(escalation_tasks),
        "escalation_rescued": sum(1 for t in escalation_tasks if t.get("status") == "done"),
    }

    # Job-level LLM usage, summed over every attempt of every task (retries included) —
    # cost-per-migration is a first-class eval metric (plan §11).
    attempts = [a for t in plan for a in t.get("attempts_log", [])]
    llm_usage = {
        "calls": sum(1 for a in attempts if a.get("action") == "migrate"),
        "prompt_tokens": sum(a.get("prompt_tokens", 0) for a in attempts),
        "completion_tokens": sum(a.get("completion_tokens", 0) for a in attempts),
        "cost_usd": round(sum(a.get("cost_usd", 0.0) for a in attempts), 6),
    }

    report = {
        "job_id": job_id,
        "repo_url": state.get("repo_url"),
        "migration_recipe": state.get("migration_recipe"),
        "migrated": migrate,
        "graph_summary": state.get("graph_summary"),
        "blast_radius_sample": state.get("blast_radius_sample"),
        "tasks": plan,
        "tasks_total": len(plan),
        "tasks_done": tasks_done,
        "affected_tests": state.get("affected_tests", []),
        "recovery": recovery,
        "llm_usage": llm_usage,
        "verify_summary": verify_summary,
        "integrate_summary": integrate_summary,
        "test_summary": final,
        "diff": state.get("diff", ""),
    }
    path = await LocalStorage().put(
        f"{job_id}/report.json",
        json.dumps(report, indent=2).encode(),
        content_type="application/json",
    )
    log.info(
        "REPORT node | job=%s migrated=%s tasks=%s/%s tests=%s/%s -> %s",
        job_id, migrate, tasks_done, len(plan),
        final.get("passed"), final.get("total"), path,
    )
    # Overwrite test_summary with the authoritative (full-suite) result for persistence.
    return {"report_path": path, "test_summary": final, "step_log": ["report"]}
