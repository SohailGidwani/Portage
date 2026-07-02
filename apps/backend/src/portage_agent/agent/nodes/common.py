"""Shared helpers for the agent nodes: workspaces, the migration git worktree, file IO,
content hashing, and parsing the LLM's fenced-code output.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path

from portage_agent.config import settings

log = logging.getLogger("portage.agent")

# Dirs we never treat as project source (graph artifacts, vcs, caches, the worktree itself).
_SKIP_DIRS = {".git", ".code-review-graph", "__pycache__", ".pytest_cache", ".portage-worktree"}

_FENCE = re.compile(r"```(?:[\w+-]*)\n(.*?)```", re.DOTALL)


def workspace_for(job_id: str) -> str:
    return f"{settings.workspaces_mount}/{job_id}"


def worktree_for(job_id: str) -> str:
    # Sibling of the workspace, on the same shared volume so the sandbox can mount + run it.
    return f"{settings.workspaces_mount}/{job_id}-migrated"


def iter_py_files(root: str) -> dict[str, str]:
    """Map repo-relative path -> source for every .py file, skipping artifact dirs."""
    base = Path(root)
    out: dict[str, str] = {}
    for p in base.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.relative_to(base).parts):
            continue
        try:
            out[str(p.relative_to(base))] = p.read_text()
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
    return out


def read_file(root: str, rel: str, *, limit: int = 8000) -> str | None:
    p = Path(root) / rel
    if not p.exists():
        return None
    text = p.read_text(errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n# … (truncated)\n"


def write_file(root: str, rel: str, content: str) -> str:
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return content_hash(content)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def extract_code(text: str) -> str:
    """Pull the migrated file out of the model's reply: first fenced block, else the whole
    reply. Models are told to emit exactly one ```python block."""
    m = _FENCE.search(text)
    return (m.group(1) if m else text).strip() + "\n"


async def run_git(*args: str, cwd: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def ensure_worktree(workspace: str, worktree: str) -> None:
    """Create the migration worktree at the workspace HEAD, idempotently (resume-safe)."""
    if (Path(worktree) / ".git").exists():
        log.info("worktree already present — reusing %s", worktree)
        return
    code, out = await run_git("worktree", "add", "--detach", "--force", worktree, "HEAD",
                              cwd=workspace)
    if code != 0:
        raise RuntimeError(f"git worktree add failed: {out[:400]}")
    log.info("created migration worktree %s", worktree)


async def worktree_diff(worktree: str) -> str:
    """The migration diff = tracked changes in the worktree vs its clean HEAD."""
    _, out = await run_git("diff", cwd=worktree)
    return out


async def file_diff(worktree: str, rel: str) -> str:
    """One file's migration diff vs the worktree's clean HEAD."""
    _, out = await run_git("diff", "--", rel, cwd=worktree)
    return out
