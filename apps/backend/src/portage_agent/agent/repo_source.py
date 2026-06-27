"""Materialize a repo into a job workspace.

The clone is the trusted, network-on step (untrusted code only runs later in the sandbox).
code-review-graph requires a VCS marker (`.git`), so a local-path/file:// source is copied
and `git init`'d. Idempotent: a workspace that already has `.git` is left as-is so a resumed
Ingest doesn't re-clone.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("portage.agent")


def _is_git_url(repo_url: str) -> bool:
    return (
        repo_url.startswith(("http://", "https://", "git@", "ssh://"))
        or repo_url.endswith(".git")
    )


async def _run(*args: str, cwd: str | None = None) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        detail = out.decode(errors="replace")[:500]
        raise RuntimeError(f"{' '.join(args)} failed ({proc.returncode}): {detail}")


async def materialize_repo(repo_url: str, workspace: str) -> None:
    ws = Path(workspace)
    if (ws / ".git").exists():
        log.info("workspace already materialized (.git present) — skipping clone: %s", workspace)
        return

    ws.parent.mkdir(parents=True, exist_ok=True)

    if _is_git_url(repo_url):
        log.info("git clone %s -> %s", repo_url, workspace)
        if ws.exists():
            await asyncio.to_thread(shutil.rmtree, ws)
        await _run("git", "clone", "--depth", "1", repo_url, workspace)
        return

    # Local path (optionally file://). Copy the tree and make it a git repo for CRG.
    src = urlparse(repo_url).path if repo_url.startswith("file://") else repo_url
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(f"local repo source not found: {src_path}")
    log.info("copy local repo %s -> %s (+ git init)", src_path, workspace)
    if ws.exists():
        await asyncio.to_thread(shutil.rmtree, ws)
    await asyncio.to_thread(shutil.copytree, src_path, ws)
    await _run("git", "init", "-q", cwd=workspace)
    await _run("git", "add", "-A", cwd=workspace)
    await _run(
        "git", "-c", "user.email=portage@local", "-c", "user.name=portage",
        "commit", "-qm", "portage: initial workspace snapshot", cwd=workspace,
    )
