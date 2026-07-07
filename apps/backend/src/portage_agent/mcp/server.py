"""Portage MCP tools — implementation.

Design constraints:
  * The caller's repo is NEVER mutated: verify copies it to a scratch workspace, applies
    the diff there, and bind-mounts that copy into the network-off sandbox.
  * Tools return structured dicts (never raise) — an MCP tool error should be a readable
    result the calling agent can act on, not a protocol failure.
  * No control-plane coupling: these tools reuse the core adapters (DockerSandbox,
    MCPRetrievalProvider, parse_junit_xml) directly; no API/DB/queue involvement.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from portage_agent.retrieval import MCPRetrievalProvider
from portage_agent.sandbox import DockerSandbox, parse_junit_xml

mcp = FastMCP(
    "portage",
    instructions=(
        "Portage's verified migration core as tools. Before writing risky multi-file "
        "changes to disk, call verify_patch_in_sandbox with your proposed unified diff "
        "to run the repo's tests against it in an isolated, network-off sandbox."
    ),
)

_REPORT_FILE = ".portage-report.xml"


def _err(msg: str, **extra: object) -> dict:
    return {"ok": False, "error": msg, **extra}


def _root_error(exc: BaseException) -> str:
    """The innermost real error — anyio TaskGroups wrap everything in ExceptionGroups,
    which read as noise in a tool result."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def _graph_precondition(repo_path: str) -> dict | None:
    p = Path(repo_path)
    if not p.is_dir():
        return _err(f"repo_path is not a directory: {repo_path}")
    if not any((p / m).exists() for m in (".git", ".svn", ".code-review-graph")):
        return _err(
            "repo has no .git/.svn marker — code-review-graph requires a project root. "
            "Pass the repository root (not a subdirectory), or `git init` first."
        )
    return None


async def _run(*args: str, cwd: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


def _copy_repo(repo_path: str, dest: str) -> None:
    shutil.copytree(
        repo_path, dest, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".venv", "node_modules",
            ".code-review-graph", ".portage-worktree",
        ),
    )


@mcp.tool()
async def verify_patch_in_sandbox(
    repo_path: str,
    diff: str = "",
    test_args: list[str] | None = None,
    timeout_seconds: int = 600,
) -> dict:
    """Apply a unified diff to a COPY of the repo and run its tests in an ephemeral,
    network-off Docker sandbox. The original repo is never touched.

    Args:
        repo_path: Absolute path of the repo to verify.
        diff: Unified diff to apply (as from `git diff`). Empty = verify the repo as-is.
        test_args: Optional pytest targets (e.g. ["tests/test_api.py"]). Empty = full suite.
        timeout_seconds: Wall-clock cap for the test run.

    Returns:
        {ok, applied, tests: {total, passed, failed, errors, skipped}, passed: bool,
         failing: [names], output_tail} — or {ok: false, error} on setup problems.
    """
    src = Path(repo_path)
    if not src.is_dir():
        return _err(f"repo_path is not a directory: {repo_path}")

    scratch = tempfile.mkdtemp(prefix="portage-verify-")
    try:
        workdir = str(Path(scratch) / "repo")
        await asyncio.to_thread(_copy_repo, str(src), workdir)

        applied = False
        if diff.strip():
            patch_file = Path(scratch) / "proposed.patch"
            patch_file.write_text(diff if diff.endswith("\n") else diff + "\n")
            # git apply works in plain directories too (no .git needed).
            code, out = await _run("git", "apply", "--whitespace=nowarn",
                                   str(patch_file), cwd=workdir)
            if code != 0:
                return _err("diff does not apply", detail=out[-800:])
            applied = True

        sandbox = DockerSandbox(volume=workdir, mount="/repo")
        result = await sandbox.run(
            ["run-tests", *(test_args or [])], workdir="/repo",
            timeout=timeout_seconds,
        )

        report_path = Path(workdir) / _REPORT_FILE
        if not report_path.exists():
            return _err(
                f"no test report produced (sandbox exit {result.exit_code}) — "
                "is the portage-sandbox image built? "
                "(docker compose --profile tools build sandbox)",
                output_tail=(result.stderr or result.stdout)[-800:],
            )
        report = parse_junit_xml(report_path.read_text())
        failing = [f"{c.classname}::{c.name}" for c in report.cases
                   if c.outcome in ("failed", "error")][:20]
        return {
            "ok": True,
            "applied": applied,
            "tests": {k: getattr(report, k) for k in
                      ("total", "passed", "failed", "errors", "skipped")},
            "passed": report.ok,
            "failing": failing,
            "output_tail": (result.stdout or "")[-1200:],
        }
    except FileNotFoundError as exc:  # docker/git missing on host
        return _err(f"missing prerequisite on host: {exc}")
    finally:
        await asyncio.to_thread(shutil.rmtree, scratch, True)


@mcp.tool()
async def repo_graph(repo_path: str) -> dict:
    """Build (or refresh) the structural knowledge graph for a repo and return its
    summary (files parsed, nodes, edges). Requires `code-review-graph` on PATH and a
    `.git` directory in the repo (pass the repository root)."""
    if pre := _graph_precondition(repo_path):
        return pre
    try:
        # First build must be full (incremental diffs against a prior graph state that
        # doesn't exist yet and yields an empty graph); refreshes stay incremental.
        first_build = not (Path(repo_path) / ".code-review-graph").exists()
        summary = await MCPRetrievalProvider(repo_path).build(full_rebuild=first_build)
    except FileNotFoundError:
        return _err("code-review-graph is not installed on this host "
                    "(uv tool install code-review-graph)")
    except Exception as exc:  # noqa: BLE001 - readable tool result over protocol error
        return _err(f"graph build failed: {_root_error(exc)}")
    # An incremental refresh with no changes reports 0 nodes for the *action*; the graph
    # itself is present and current — that is ok, not a failure.
    refreshed_unchanged = not first_build and not summary.errors
    return {
        "ok": summary.ok or refreshed_unchanged,
        "build": "full" if first_build else "incremental",
        "files_parsed": summary.files_parsed,
        "total_nodes": summary.total_nodes,
        "total_edges": summary.total_edges,
        "errors": summary.errors[:5],
    }


@mcp.tool()
async def blast_radius(repo_path: str, changed_files: list[str], max_depth: int = 2) -> dict:
    """The impact set of changing `changed_files` (repo-relative paths): affected
    callers/dependents/tests from the knowledge graph. Build the graph first via
    repo_graph."""
    if pre := _graph_precondition(repo_path):
        return pre
    try:
        return await MCPRetrievalProvider(repo_path).blast_radius(
            changed_files, max_depth=max_depth
        )
    except FileNotFoundError:
        return _err("code-review-graph is not installed on this host "
                    "(uv tool install code-review-graph)")
    except Exception as exc:  # noqa: BLE001
        return _err(f"blast-radius failed: {_root_error(exc)}")
