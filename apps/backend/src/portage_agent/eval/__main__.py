"""Harness entrypoint — run inside the compose network, next to a live worker:

    docker compose run --rm worker python -m portage_agent.eval \
        --corpus /corpus/corpus.toml --k 2 --scenarios baseline,bad_patch

The harness enqueues jobs; the `worker` service executes them. Results land in the
`runs`/`metrics` tables (and are printed as a markdown table).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid

from portage_agent.logging_conf import setup_logging

from .corpus import load_corpus
from .harness import (
    SCENARIOS,
    HarnessConfig,
    default_suite_name,
    format_metrics_table,
    load_replay_plan,
    run_suite,
)

log = logging.getLogger("portage.eval")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="portage-eval", description="Portage eval harness")
    p.add_argument("--corpus", default="/corpus/corpus.toml", help="corpus manifest (TOML)")
    p.add_argument("--k", type=int, default=2, help="runs per (repo, scenario) cell")
    p.add_argument("--scenarios", default="baseline",
                   help=f"comma-separated; known: {','.join(sorted(SCENARIOS))}")
    p.add_argument("--repos", default="",
                   help="comma-separated corpus names to include (default: all)")
    p.add_argument("--suite", default="", help="suite label (default: timestamped)")
    p.add_argument("--timeout", type=int, default=900, help="per-job timeout, seconds")
    p.add_argument(
        "--plan-only", action="store_true",
        help="stop real jobs after Plan and measure architect acceptance",
    )
    p.add_argument(
        "--replay-plan-job", default="",
        help="diagnostic: reuse the accepted artifact plan from this job UUID",
    )
    return p.parse_args()


async def main() -> None:
    setup_logging()
    args = _parse_args()

    repos = load_corpus(args.corpus)
    if args.repos:
        wanted = {r.strip() for r in args.repos.split(",") if r.strip()}
        missing = wanted - {r.name for r in repos}
        if missing:
            raise SystemExit(f"unknown corpus repos: {sorted(missing)}")
        repos = [r for r in repos if r.name in wanted]

    replay_source = None
    replay_plan = None
    if args.replay_plan_job:
        if args.plan_only:
            raise SystemExit("--replay-plan-job cannot be combined with --plan-only")
        if len(repos) != 1:
            raise SystemExit("--replay-plan-job requires exactly one selected corpus repo")
        try:
            replay_source = uuid.UUID(args.replay_plan_job)
            replay_plan = await load_replay_plan(replay_source)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    suite = args.suite or (
        default_suite_name().replace("eval-", "eval-replay-", 1)
        if replay_plan is not None else default_suite_name()
    )

    cfg = HarnessConfig(
        suite=suite,
        k=args.k,
        scenarios=[s.strip() for s in args.scenarios.split(",") if s.strip()],
        job_timeout_seconds=args.timeout,
        plan_only=args.plan_only,
        replay_plan=replay_plan,
        replay_source_job=replay_source,
    )
    metrics = await run_suite(repos, cfg)
    print(f"\nsuite: {cfg.suite}\n")
    print(format_metrics_table(metrics))


if __name__ == "__main__":
    asyncio.run(main())
