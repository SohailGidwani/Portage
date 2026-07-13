"""Graph state for the Ingest → Plan → Execute → Verify → (Recover) → Integrate → Report graph.

`step_log` (operator.add reducer) accumulates across checkpoints and makes resume observable.
The graph is recipe-dispatched: when no recipe matches (or it finds no files), `migrate` is
False and Execute/Integrate degrade to the Phase-1 ingest→verify→report behaviour, so the
older fixtures (and dod_check / phase1_check) keep working unchanged.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class GraphState(TypedDict, total=False):
    # --- seeded on first invoke ---
    job_id: str
    repo_url: str
    migration_recipe: str
    config: dict

    # --- Ingest ---
    workspace: str
    graph_summary: dict
    blast_radius_sample: dict

    # --- Plan ---
    migrate: bool  # True iff a recipe matched and produced tasks
    plan: list[dict]  # TaskSnapshot dicts (visibility / report)
    worktree: str  # migration worktree path (only when migrate)
    affected_tests: list[str]  # blast-radius-selected test files ([] => run all)
    interface_manifest: dict  # R1: frozen target-interface decisions, keyed "path::symbol"
    # R1.1+: framework-capability decisions, bounded coordinated generation units,
    # executable verification cuts/edges, and large-cut diagnostics.
    seam_plan: dict
    oracle_manifest: dict  # original test names/assertions/decorators, frozen at Plan
    # path -> unchanged|adapter|adapter_wiring|guarded_rewrite|unsupported_test_seam
    test_strategy: dict
    # Recipe-owned, frozen exact-line test plumbing rewrites, keyed by test path.
    test_normalizations: dict
    test_compat_path: str  # deterministic repo-root compatibility module, or empty
    artifact_plan: list[dict]  # frozen validated create-artifact proposals
    unsupported_test_seams: list[dict]
    oracle_results: Annotated[list[dict], operator.add]

    # --- Execute / Verify / Recover loop ---
    current_batch_paths: list[str]
    current_batch_tests: list[str]
    has_pending_tasks: bool
    verified_batches: Annotated[list[dict], operator.add]
    # Filesystem-backed pre-cut snapshot metadata. Source bytes stay in the worktree,
    # not in checkpoint JSON, and are discarded after a successful Verify.
    current_batch_checkpoint: dict
    cut_restore_pending_verification: bool
    verify_attempts: int  # bumped by Verify each run (diagnostic)
    verify_passed: bool
    last_verify_errors: str  # failing-test output; Recover classifies it, Execute retries with it
    last_failure_fingerprint: str
    diagnostic_repair_requested: bool
    # Uniquely attributed provider/consumer target for a bounded target-only repair.
    # Historical field name retained for checkpoint compatibility.
    contract_repair_owner: str
    recover_source: str  # "verify" | "integrate"

    # --- Recover (Phase 3) ---
    recover_visits: int  # total audit count for every Recover entry
    recover_budget_used: int  # only non-progress/whole-cut failures debit this budget
    recover_route: str  # "execute" | "plan" | "integrate" — Recover's routing decision
    replan_requested: bool  # set by Recover, consumed by Plan (append missed tasks)
    recovery_actions: Annotated[list[dict], operator.add]  # audit log for report/frontend
    integration_recovery_visits: int

    # --- results ---
    test_summary: dict  # Verify's (affected-subset) result; the Phase-1 contract field
    integrate_summary: dict  # full-suite result (authoritative for migration runs)
    integration_passed: bool
    last_integrate_errors: str
    integration_fault_injected: bool
    diff: str  # the migration diff (git diff in the worktree)
    report_path: str

    # accumulates ["ingest","plan","execute","verify","integrate","report"]
    step_log: Annotated[list[str], operator.add]
