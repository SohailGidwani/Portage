"""MCPRetrievalProvider — structural retrieval via code-review-graph's MCP server.

Implements `core.Retrieval`. Spawns `code-review-graph serve --repo <workspace>` as a stdio
MCP server (CRG is installed isolated in the worker image; only the lightweight `mcp` client
SDK lives in our venv) and calls its tools:
  * build_or_update_graph_tool  -> build the persistent graph (SQLite under .code-review-graph/)
  * get_impact_radius_tool      -> blast radius for changed files

CRG requires the repo to look like a project root (a `.git` / `.svn` / `.code-review-graph`
dir). The Ingest node guarantees this by cloning (or git-init'ing) the workspace.

Each method opens its own short-lived MCP session. The graph is persisted to disk by `build`,
so a later session's query reads it back without rebuilding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from portage_agent.config import settings
from portage_agent.core.interfaces import GraphSummary

log = logging.getLogger("portage.retrieval")

# Only expose the tools we use (server-side trim).
_TOOLS = "build_or_update_graph_tool,get_impact_radius_tool,query_graph_tool"


class MCPRetrievalProvider:
    """core.Retrieval adapter over code-review-graph (MCP, stdio)."""

    def __init__(self, repo_root: str, *, command: str | None = None) -> None:
        self.repo_root = repo_root
        self.command = command or settings.crg_command

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        # CRG_PARSE_EXECUTOR=thread is load-bearing. CRG parses repos with >=8 files in
        # parallel, defaulting to ProcessPoolExecutor on Linux; forking from its stdio
        # server (a threaded asyncio process whose pipe FDs the children inherit) deadlocks
        # the build — CRG's own fix for this (issues #46/#136) only auto-applies on
        # Windows. Thread mode is upstream's documented override and keeps the parallelism
        # (tree-sitter releases the GIL). Symptom if lost: every repo with >=8 Python
        # files times out in Ingest and degrades to no-graph mode.
        env = {**os.environ}
        env.setdefault("CRG_PARSE_EXECUTOR", "thread")
        params = StdioServerParameters(
            command=self.command,
            args=["serve", "--repo", self.repo_root, "--tools", _TOOLS],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    @staticmethod
    async def _call(session: ClientSession, tool: str, args: dict) -> dict:
        result = await session.call_tool(tool, args)
        # CRG returns a single JSON text content block.
        text = next(
            (t for c in result.content if (t := getattr(c, "text", None))), None
        )
        if text is None:
            raise RuntimeError(f"{tool}: empty MCP result")
        payload = json.loads(text)
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise RuntimeError(f"{tool}: {payload.get('error')}")
        return payload

    async def build(self, *, full_rebuild: bool = True) -> GraphSummary:
        # wait_for around the WHOLE session: a wedged CRG subprocess (observed in the
        # wild on a corpus repo) must never hang Ingest — the worker's heartbeat keeps
        # the job leased, so a hang here would livelock the queue forever.
        async def _do() -> dict:
            async with self._session() as session:
                return await self._call(
                    session, "build_or_update_graph_tool", {"full_rebuild": full_rebuild}
                )

        d = await asyncio.wait_for(_do(), timeout=settings.crg_timeout_seconds)
        summary = GraphSummary(
            files_parsed=d.get("files_parsed", 0),
            total_nodes=d.get("total_nodes", 0),
            total_edges=d.get("total_edges", 0),
            flows_detected=d.get("flows_detected", 0),
            communities_detected=d.get("communities_detected", 0),
            build_type=d.get("build_type", ""),
            errors=list(d.get("errors", []) or []),
        )
        log.info(
            "graph built: %s files, %s nodes, %s edges (%s)",
            summary.files_parsed, summary.total_nodes, summary.total_edges, summary.build_type,
        )
        return summary

    async def blast_radius(self, changed_files: list[str], *, max_depth: int = 2) -> dict:
        async def _do() -> dict:
            async with self._session() as session:
                return await self._call(
                    session,
                    "get_impact_radius_tool",
                    {"changed_files": changed_files, "max_depth": max_depth},
                )

        return await asyncio.wait_for(_do(), timeout=settings.crg_timeout_seconds)
