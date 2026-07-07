"""Corpus manifest — the pinned set of repos the harness evaluates against.

TOML (stdlib tomllib, no new dependency), one ``[[repos]]`` entry per corpus member. The
bundled fixture is entry #1; curated OSS Flask apps are added with a pinned ``ref`` so the
benchmark set is reproducible (plan §11: "version and pin").
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CorpusRepo:
    name: str
    repo_url: str  # local path (bundled) or git URL (curated, with ref pinned)
    recipe: str
    ref: str = ""  # git SHA/tag for remote repos; empty for bundled fixtures
    # The app lives in this subdirectory of the repo (e.g. pallets/flask examples/tutorial);
    # Ingest lifts it out as the workspace root.
    subdir: str = ""
    # The sanctioned test paths (pytest targets). Empty = the whole repo suite. Set it for
    # repos whose tree carries tests that can't run offline (Selenium, load tests).
    test_args: tuple[str, ...] = ()
    # Extra env for the sandboxed test run (e.g. TEST_DATABASE_URI=sqlite:///... so a
    # DB-backed suite runs under --network none).
    test_env: dict[str, str] | None = None
    tier: str = ""  # baseline | structural | framework | heavy | caveated
    stresses: tuple[str, ...] = ()  # taxonomy hooks this repo exercises
    source: str = ""  # "bundled" | "github" | ...
    notes: str = ""

    def job_config(self, scenario_config: dict) -> dict:
        """The job config for one harness run: fault scenario + this repo's pinning/scope."""
        cfg = dict(scenario_config)
        if self.ref:
            cfg["repo_ref"] = self.ref
        if self.subdir:
            cfg["repo_subdir"] = self.subdir
        if self.test_args:
            cfg["test_args"] = list(self.test_args)
        if self.test_env:
            cfg["test_env"] = dict(self.test_env)
        return cfg


def load_corpus(path: str | Path) -> list[CorpusRepo]:
    """Load + validate the manifest. Unknown keys are ignored; missing required keys fail
    loudly — a silently skipped corpus entry would corrupt the benchmark."""
    data = tomllib.loads(Path(path).read_text())
    repos: list[CorpusRepo] = []
    for i, entry in enumerate(data.get("repos", [])):
        missing = [k for k in ("name", "repo_url", "recipe") if not entry.get(k)]
        if missing:
            raise ValueError(f"corpus entry #{i} missing required keys: {missing}")
        if entry["repo_url"].startswith(("http://", "https://", "git@")) and not entry.get("ref"):
            raise ValueError(
                f"corpus entry {entry['name']!r} is remote but has no pinned ref "
                "(reproducibility rule: never track a moving branch)"
            )
        repos.append(
            CorpusRepo(
                name=entry["name"],
                repo_url=entry["repo_url"],
                recipe=entry["recipe"],
                ref=entry.get("ref", ""),
                subdir=entry.get("subdir", ""),
                test_args=tuple(entry.get("test_args", [])),
                test_env={str(k): str(v) for k, v in entry.get("test_env", {}).items()} or None,
                tier=entry.get("tier", ""),
                stresses=tuple(entry.get("stresses", [])),
                source=entry.get("source", ""),
                notes=entry.get("notes", ""),
            )
        )
    if not repos:
        raise ValueError(f"corpus manifest {path} has no [[repos]] entries")
    return repos
