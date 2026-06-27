"""Graph state for the trivial Phase 0 graph.

`step_log` accumulates (operator.add reducer) across checkpoints — that's the artifact
that makes "resume vs restart" observable: after a kill mid-`work`, the resumed run
loads a state that already contains ``"start"`` and the original ``run_marker``.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class GraphState(TypedDict, total=False):
    # set on enqueue / first invoke
    job_id: str
    repo_url: str
    migration_recipe: str

    # stamped by the `start` node — its presence on resume proves we didn't restart
    run_marker: str
    started_at: str

    # produced later in the graph
    work_done: bool
    finished_at: str

    # accumulates ["start"] -> ["start","work"] -> ["start","work","end"]
    step_log: Annotated[list[str], operator.add]
