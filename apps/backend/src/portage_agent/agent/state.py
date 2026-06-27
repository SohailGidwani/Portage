"""Graph state for the Phase 1 Ingest → Verify → Report graph.

`step_log` (operator.add reducer) accumulates across checkpoints and makes resume
observable: after a kill mid-Verify, the resumed run loads a state that already contains
the Ingest output (`graph_summary`) and `["ingest"]` in the log — Ingest does NOT re-run.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class GraphState(TypedDict, total=False):
    # seeded on first invoke
    job_id: str
    repo_url: str
    migration_recipe: str
    config: dict

    # Ingest output
    workspace: str
    graph_summary: dict
    blast_radius_sample: dict

    # Verify output
    test_summary: dict

    # Report output
    report_path: str

    # accumulates ["ingest"] -> ["ingest","verify"] -> ["ingest","verify","report"]
    step_log: Annotated[list[str], operator.add]
