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


async def materialize_repo(
    repo_url: str, workspace: str, *, ref: str = "", subdir: str = ""
) -> None:
    ws = Path(workspace)
    if (ws / ".git").exists():
        log.info("workspace already materialized (.git present) — skipping clone: %s", workspace)
        return

    ws.parent.mkdir(parents=True, exist_ok=True)

    if _is_git_url(repo_url):
        if ws.exists():
            await asyncio.to_thread(shutil.rmtree, ws)
        # `subdir`: the app lives in a subdirectory of a larger repo (e.g. pallets/flask's
        # examples/tutorial). Clone aside, lift the subdir out as the workspace root, and
        # snapshot it as a fresh git repo — same shape as the local-path flow, so the
        # worktree/rollback machinery sees a normal repo either way.
        target = f"{workspace}.clone-tmp" if subdir else workspace
        if ref:
            # Pinned corpus clone: fetch exactly the SHA/tag so eval K-runs are
            # reproducible even if the upstream branch moves (plan §11: version and pin).
            log.info("git fetch %s @ %s -> %s", repo_url, ref, target)
            Path(target).mkdir(parents=True)
            await _run("git", "init", "-q", cwd=target)
            await _run("git", "remote", "add", "origin", repo_url, cwd=target)
            await _run("git", "fetch", "--depth", "1", "origin", ref, cwd=target)
            await _run("git", "checkout", "-q", "--detach", "FETCH_HEAD", cwd=target)
        else:
            log.info("git clone %s -> %s", repo_url, target)
            await _run("git", "clone", "--depth", "1", repo_url, target)
        if subdir:
            src = Path(target) / subdir
            if not src.is_dir():
                await asyncio.to_thread(shutil.rmtree, target)
                raise FileNotFoundError(f"subdir {subdir!r} not found in {repo_url}")
            log.info("lifting subdir %s -> %s", subdir, workspace)
            await asyncio.to_thread(shutil.copytree, src, ws)
            await asyncio.to_thread(shutil.rmtree, target)
            await _run("git", "init", "-q", cwd=workspace)
            await _run("git", "add", "-A", cwd=workspace)
            await _run(
                "git", "-c", "user.email=portage@local", "-c", "user.name=portage",
                "commit", "-qm", "portage: initial workspace snapshot", cwd=workspace,
            )
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
