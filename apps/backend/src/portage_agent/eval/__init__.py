"""Eval harness (Phase 4) — recipe-agnostic reliability measurement.

`python -m portage_agent.eval` drives (corpus × scenarios × K) through the real queue +
worker and writes `runs`/`metrics` rows — the contract the leaderboard reads. See
harness.py for the metric definitions and corpus.py for the manifest format.
"""

from .corpus import CorpusRepo, load_corpus
from .harness import SCENARIOS, HarnessConfig, format_metrics_table, run_suite

__all__ = [
    "CorpusRepo",
    "load_corpus",
    "SCENARIOS",
    "HarnessConfig",
    "run_suite",
    "format_metrics_table",
]
