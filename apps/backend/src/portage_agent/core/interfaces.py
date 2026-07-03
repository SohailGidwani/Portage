"""Core swappable interfaces (the only abstractions Phase 0 introduces).

These are the seams that keep AWS non-load-bearing (plan §3): each has a local/default
adapter today and a cloud adapter later, chosen by config — never by `import boto3` in
core logic. They are typed **Protocols**: structural contracts, no behaviour here. Real
adapters arrive in the phases noted.

  * StorageBackend — artifacts (diffs, reports).      local | s3       (Phase 4)
  * JobQueue       — durable work queue.              postgres | sqs   (Phase 0: postgres)
  * Sandbox        — run untrusted repo tests safely. docker | fargate (Phase 1)
  * LLM            — model-ladder completions.        litellm          (Phase 2)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


# --------------------------------------------------------------------------- storage
@runtime_checkable
class StorageBackend(Protocol):
    """Blob storage for run artifacts. `local` writes to disk; `s3` later."""

    async def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        """Store ``data`` at ``key``; return a retrievable reference (path or URI)."""
        ...

    async def get(self, key: str) -> bytes:
        ...


# ----------------------------------------------------------------------------- queue
@dataclass(slots=True)
class ClaimedJob:
    """A job a worker has leased off the queue."""

    id: uuid.UUID
    repo_url: str
    migration_recipe: str
    config: dict


@runtime_checkable
class JobQueue(Protocol):
    """Durable job queue. Phase 0 adapter = Postgres ``FOR UPDATE SKIP LOCKED`` with a
    heartbeat lease (see ``worker/queue.py``). `sqs` is the scale swap."""

    async def enqueue(self, *, repo_url: str, migration_recipe: str, config: dict) -> uuid.UUID:
        ...

    async def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
        """Atomically claim one queued (or lease-expired) job, or return None."""
        ...

    async def heartbeat(self, job_id: uuid.UUID, *, worker_id: str) -> None:
        """Extend the lease on a claimed job to prove the worker is still alive."""
        ...

    async def complete(self, job_id: uuid.UUID) -> None:
        ...

    async def fail(self, job_id: uuid.UUID, *, error: str) -> None:
        ...


# --------------------------------------------------------------------------- sandbox
@dataclass(slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class Sandbox(Protocol):
    """Ephemeral, network-off, resource-capped execution for an untrusted repo's tests.
    `docker` adapter in Phase 1; `fargate` is the production isolation upgrade."""

    async def run(self, command: list[str], *, workdir: str, timeout: int = 600) -> SandboxResult:
        ...


# ------------------------------------------------------------------------------- llm
@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Best-effort USD cost of this call (0.0 when the provider can't price the model).
    # Cost-per-migration is a first-class eval metric (plan §11), so it's tracked from
    # the response on down rather than re-derived later.
    cost_usd: float = 0.0


@dataclass(slots=True)
class LLMMessage:
    role: str
    content: str


@runtime_checkable
class LLM(Protocol):
    """Model-ladder completions behind LiteLLM (driver / escalation / cheap tiers).
    Pluggability is itself the eval story (per-model metrics). Adapter arrives Phase 2."""

    async def complete(
        self, messages: list[LLMMessage], *, model: str, **kwargs: object
    ) -> LLMResponse:
        ...


# -------------------------------------------------------------------------- retrieval
@dataclass(slots=True)
class GraphSummary:
    """Result of building the structural knowledge graph for a repo."""

    files_parsed: int = 0
    total_nodes: int = 0
    total_edges: int = 0
    flows_detected: int = 0
    communities_detected: int = 0
    build_type: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.total_nodes > 0 and not self.errors


@runtime_checkable
class Retrieval(Protocol):
    """Structural retrieval over a repo (graph + blast-radius). Phase 1 adapter =
    `MCPRetrievalProvider` over code-review-graph's MCP server. Lean surface: build the
    graph, plus one query (blast-radius). The fuller query surface lands in Phase 2 when
    Plan consumes it."""

    async def build(self, *, full_rebuild: bool = True) -> GraphSummary:
        ...

    async def blast_radius(self, changed_files: list[str], *, max_depth: int = 2) -> dict:
        """Impact of changing `changed_files`: the affected callers/dependents/tests."""
        ...


__all__ = [
    "StorageBackend",
    "JobQueue",
    "ClaimedJob",
    "Sandbox",
    "SandboxResult",
    "LLM",
    "LLMMessage",
    "LLMResponse",
    "Retrieval",
    "GraphSummary",
]
