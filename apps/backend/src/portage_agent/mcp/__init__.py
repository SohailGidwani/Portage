"""Portage MCP server (Phase 5b) — the co-pilot interface over the verified core.

Exposes the engine's proven primitives as MCP tools so external agents (Claude Code,
Cursor) can test their own changes BEFORE writing them to the user's tree:

  * verify_patch_in_sandbox — apply a diff to a copy of the repo, run the tests in the
    ephemeral network-off Docker sandbox, return the structured pass/fail result. The
    same sandbox + JUnit parsing the autonomous mode's eval numbers were measured on —
    that's the credibility transfer (plan §14a: the eval proves the loop, the tool
    inherits the trust).
  * repo_graph — build/refresh the structural knowledge graph (code-review-graph) and
    return its summary.
  * blast_radius — the impact set for a proposed change: which files/tests are affected.

Run over stdio:  python -m portage_agent.mcp   (see .mcp.json at the repo root).
Requires: Docker + the portage-sandbox image for verify; code-review-graph on PATH for
the graph tools (each degrades with a clear error instead of crashing).
"""

from .server import mcp

__all__ = ["mcp"]
