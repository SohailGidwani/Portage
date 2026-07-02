"""Graph state for the Phase 2 Ingest → Plan → Execute → Verify → Integrate → Report graph.

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

    # --- Execute / Verify retry loop ---
    verify_attempts: int  # bumped by Verify each run; bounds the retry loop
    verify_passed: bool
    last_verify_errors: str  # failing-test output fed back into Execute on retry

    # --- results ---
    test_summary: dict  # Verify's (affected-subset) result; the Phase-1 contract field
    integrate_summary: dict  # full-suite result (authoritative for migration runs)
    diff: str  # the migration diff (git diff in the worktree)
    report_path: str

    # accumulates ["ingest","plan","execute","verify","integrate","report"]
    step_log: Annotated[list[str], operator.add]
