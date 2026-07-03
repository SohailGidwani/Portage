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
    source: str = ""  # "bundled" | "github" | ...
    notes: str = ""


def load_corpus(path: str | Path) -> list[CorpusRepo]:
    """Load + validate the manifest. Unknown keys are ignored; missing required keys fail
    loudly — a silently skipped corpus entry would corrupt the benchmark."""
    data = tomllib.loads(Path(path).read_text())
    repos: list[CorpusRepo] = []
    for i, entry in enumerate(data.get("repos", [])):
        missing = [k for k in ("name", "repo_url", "recipe") if not entry.get(k)]
        if missing:
            raise ValueError(f"corpus entry #{i} missing required keys: {missing}")
        repos.append(
            CorpusRepo(
                name=entry["name"],
                repo_url=entry["repo_url"],
                recipe=entry["recipe"],
                ref=entry.get("ref", ""),
                source=entry.get("source", ""),
                notes=entry.get("notes", ""),
            )
        )
    if not repos:
        raise ValueError(f"corpus manifest {path} has no [[repos]] entries")
    return repos
