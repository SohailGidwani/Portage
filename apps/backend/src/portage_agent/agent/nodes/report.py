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
from hashlib import sha256
from pathlib import Path

from portage_agent.db import task_store
from portage_agent.storage import LocalStorage

from ..state import GraphState
from .common import read_file
from .oracle import oracle_violations
from .redaction import scrub

log = logging.getLogger("portage.agent")


def _checkpoint_tree_state(worktree: str, checkpoint: dict) -> str | None:
    """Classify the measured cut against its pre-cut snapshot."""
    if not checkpoint or not worktree:
        return None
    root = Path(checkpoint.get("root", ""))
    matches: list[bool] = []
    for path, record in checkpoint.get("files", {}).items():
        target = Path(worktree, path)
        if not record.get("existed"):
            matches.append(not target.exists())
            continue
        snapshot = root / record.get("snapshot", "")
        try:
            matches.append(target.read_bytes() == snapshot.read_bytes())
        except OSError:
            return None
    if not matches:
        return None
    if all(matches):
        return "restored_coherent"
    return "hybrid" if any(matches) else "migrated"


async def report_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    migrate = bool(state.get("migrate"))
    verify_summary = state.get("test_summary") or {}
    integrate_summary = state.get("integrate_summary") or {}
    final = dict(integrate_summary if migrate else verify_summary)
    tree_state = (
        _checkpoint_tree_state(
            state.get("worktree", ""), state.get("current_batch_checkpoint") or {},
        )
        or state.get("migration_tree_state")
        or ("migrated" if migrate else "original")
    )
    final["tree_state"] = tree_state

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
        "budget_used": state.get("recover_budget_used", 0),
        "actions": state.get("recovery_actions", []),
        "tasks_skipped": sum(1 for t in plan if t.get("status") == "skipped"),
        "escalation_attempted": len(escalation_tasks),
        "escalation_rescued": sum(1 for t in escalation_tasks if t.get("status") == "done"),
        "integration_visits": state.get("integration_recovery_visits", 0),
        "no_progress_retries": sum(
            1 for action in state.get("recovery_actions", [])
            if "no_progress" in action.get("classification", "")
        ),
        "last_classification": (
            (state.get("recovery_actions") or [{}])[-1].get("classification")
        ),
    }

    oracle_manifest = state.get("oracle_manifest") or {}
    oracle_files: list[dict] = []
    oracle_breaks: list[dict] = []
    oracle_root = state.get("worktree") or state.get("workspace") or ""
    for oracle_path, entry in oracle_manifest.items():
        current = read_file(oracle_root, oracle_path) or ""
        violations = oracle_violations(entry, current)
        result = {
            "path": oracle_path,
            "strategy": (state.get("test_strategy") or {}).get(oracle_path, "unchanged"),
            "byte_preserved": sha256(current.encode()).hexdigest() == entry.get("sha256"),
            "violations": violations,
        }
        oracle_files.append(result)
        if violations:
            oracle_breaks.append({"path": oracle_path, "violations": violations})
    protected = len(oracle_manifest)
    clean = sum(1 for result in oracle_files if not result["violations"])
    oracle_integrity = {
        "protected_files": protected,
        "checked_files": len(oracle_files),
        "clean_files": clean,
        "integrity_rate": clean / protected if protected else 1.0,
        "violations": oracle_breaks,
        "files": oracle_files,
        "attempt_results": state.get("oracle_results", []),
        "sanctioned_normalizations": [
            {
                "path": path,
                "kind": normalization.get("kind"),
                "owner_path": normalization.get("owner_path"),
                "target_module": normalization.get("target_module"),
                "lines": [
                    {
                        "line": replacement.get("line"),
                        "symbols": replacement.get("symbols", []),
                    }
                    for replacement in normalization.get("replacements", [])
                ],
            }
            for path, normalization in sorted(
                (state.get("test_normalizations") or {}).items()
            )
        ],
    }

    suite_ok = (
        final.get("passed", 0) > 0
        and final.get("failed", 0) == 0
        and final.get("errors", 0) == 0
    )
    unsupported = list(state.get("unsupported_test_seams") or [])
    config = state.get("config") or {}
    plan_only = bool(config.get("plan_only"))
    replay = config.get("frozen_artifact_plan") is not None
    architect_task = next(
        (task for task in plan if task.get("type") == "artifact_architect"), None,
    )
    if plan_only:
        migration_outcome = (
            "plan_accepted"
            if architect_task is None or architect_task.get("status") == "done"
            else "plan_rejected"
        )
    elif unsupported:
        migration_outcome = "unsupported"
    elif migrate and suite_ok and tasks_done == len(plan) and not oracle_breaks:
        migration_outcome = "success"
    elif migrate:
        migration_outcome = "failed"
    else:
        migration_outcome = "not_applicable"

    # Job-level LLM usage, summed over every attempt of every task (retries included) —
    # cost-per-migration is a first-class eval metric (plan §11).
    attempts = [a for t in plan for a in t.get("attempts_log", [])]
    llm_usage = {
        "calls": sum(
            1 for a in attempts
            if a.get("action") in (
                "architect", "architect_repair", "migrate", "contract_repair", "diagnose",
            )
        ),
        "prompt_tokens": sum(a.get("prompt_tokens", 0) for a in attempts),
        "completion_tokens": sum(a.get("completion_tokens", 0) for a in attempts),
        "cost_usd": round(sum(a.get("cost_usd", 0.0) for a in attempts), 6),
    }
    seam_plan = state.get("seam_plan") or {}
    executable_cut_analysis = {
        # Evidence snippets remain checkpoint-internal; the public report needs the
        # scheduling decision and diagnostics, not arbitrary source fragments.
        "edge_count": len(seam_plan.get("executable_edges", [])),
        "cuts": seam_plan.get("execution_cuts", []),
        "diagnostics": seam_plan.get("cut_diagnostics", []),
    }
    artifact_plan = state.get("artifact_plan") or []
    architect_attempts = (
        architect_task.get("attempts_log", []) if architect_task else []
    )
    artifact_analysis = {
        "architect": ({
            "status": architect_task.get("status"),
            "attempts": architect_task.get("attempts"),
            "calls": sum(
                attempt.get("action") in {"architect", "architect_repair"}
                for attempt in architect_attempts
            ),
            "repairs": sum(
                attempt.get("action") == "architect_repair"
                for attempt in architect_attempts
            ),
            "model": next((
                attempt.get("model") for attempt in architect_attempts
                if attempt.get("action") == "architect"
            ), None),
        } if architect_task else None),
        "contract_completion": (
            architect_task.get("verify_spec", {}).get("contract_completion", [])
            if architect_task else []
        ),
        "created": [
            {
                "path": item["path"], "purpose": item["purpose"],
                "exports": item["exports"],
                "capabilities": item.get("capabilities", []),
                "consumers": item["consumers"],
                "depends_on": item["depends_on"],
                "status": next((
                    task.get("status") for task in plan
                    if task.get("target_path") == item["path"]
                ), None),
            }
            for item in artifact_plan
        ],
        "contract_attributed_recoveries": sum(
            action.get("classification") == "contract_failure"
            for action in state.get("recovery_actions", [])
        ),
    }

    report = {
        "job_id": job_id,
        "repo_url": state.get("repo_url"),
        "migration_recipe": state.get("migration_recipe"),
        "evaluation_mode": "plan_only" if plan_only else "replay" if replay else "full",
        "replay_source_job": config.get("replay_source_job"),
        "migrated": migrate,
        "graph_summary": state.get("graph_summary"),
        "blast_radius_sample": state.get("blast_radius_sample"),
        "tasks": plan,
        "tasks_total": len(plan),
        "tasks_done": tasks_done,
        "affected_tests": state.get("affected_tests", []),
        "recovery": recovery,
        "migration_outcome": migration_outcome,
        "tree_state": tree_state,
        "unsupported_test_seams": unsupported,
        "oracle_integrity": oracle_integrity,
        "verified_batches": state.get("verified_batches", []),
        "executable_cut_analysis": executable_cut_analysis,
        "artifact_plan": artifact_analysis,
        "llm_usage": llm_usage,
        "verify_summary": verify_summary,
        "integrate_summary": integrate_summary,
        "test_summary": final,
        # Scrubbed (Phase 7): the diff's removed lines carry original file content, and
        # the report is served over the API.
        "diff": scrub(state.get("diff", "")),
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
