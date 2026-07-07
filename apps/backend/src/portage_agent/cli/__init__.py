"""portage — the dev front door for autonomous mode (Phase 5a).

A deliberately thin REST client over the control-plane API: the CLI never touches the DB,
the queue, or the graph — the same boundary the dashboard respects. Commands:

    portage migrate <repo> [--recipe ...] [--ref SHA] [--subdir DIR] [--watch]
    portage status  <job-id>
    portage jobs    [--limit N]
    portage report  <job-id> [--diff]

`--watch` streams node/task progress until the job is terminal and sets the exit code
(0 = full suite green AND every task done — the same honesty bar the eval harness scores).
`PORTAGE_API` (or --api) points at the control plane; default http://localhost:8000.
"""

from .main import main

__all__ = ["main"]
