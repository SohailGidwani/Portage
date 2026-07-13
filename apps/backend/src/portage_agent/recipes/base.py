"""Recipe interface + registry.

A *recipe* is a pluggable migration definition. It knows how to (a) decide whether it
applies to a repo, (b) pick the files to change and classify the per-file transformations
("subtasks"), and (c) prompt the LLM to rewrite one file while preserving behaviour. The
graph is recipe-dispatched: an unknown recipe yields no files, so Execute/Integrate no-op
and the run degrades to Phase-1 ingest→verify→report.

The recipe owns *what* to change and *how to ask*; the agent nodes own *durability* (task
persistence, worktree, checkpoints, sandbox verification). Keeping that line clean is what
lets a second recipe drop in without touching the graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

MAX_CREATED_ARTIFACTS = 4


@dataclass(frozen=True, slots=True)
class Subtask:
    """One behaviour-preserving transformation to apply within a file (a Plan leaf)."""

    type: str  # stable id, e.g. "route_to_endpoint"
    title: str
    instruction: str  # guidance fed to the LLM for this transformation


@dataclass(frozen=True, slots=True)
class PinRule:
    """A symbol-aware target-interface rule. Applies to a symbol iff the DEFINING file's
    task carries `subtask` AND `applies(contract)` is true — a file with several idiom
    subtasks pins each symbol by what it IS. `note` (may use `{name}`) is the SAME string
    object the corresponding Subtask instruction embeds: single source of truth."""

    subtask: str
    applies: object  # Callable[[SymbolContract], bool] — kept loose to avoid import cycle
    note: str
    preserve_shape: bool = False
    target_kind: str | None = None
    additional_exports: tuple[str, ...] = ()


@dataclass(slots=True)
class PlannedFile:
    """A file the recipe wants migrated, with its applicable subtasks (a Plan Task)."""

    path: str  # repo-relative path
    role: str  # "router" | "app_factory" | "test_harness" | ...
    subtasks: list[Subtask] = field(default_factory=list)
    # Lower runs first. Lets depended-upon files migrate before their importers so the
    # already-migrated version can be shown as context.
    order: int = 100
    # Provenance is explicit so fault injection and accounting can distinguish recipe
    # decisions from deterministic Portage execution infrastructure without knowing every
    # protected role by name.
    origin: str = "recipe"  # "recipe" | "infrastructure"
    # Artifact-producing plans keep rewrites backward-compatible while allowing a recipe
    # to request a path that does not exist in the source repository. The contract is a
    # JSON-safe, frozen Plan artifact (exports/members/consumers/dependencies/instructions).
    action: str = "rewrite"  # "rewrite" | "create"
    purpose: str = ""
    artifact_contract: dict = field(default_factory=dict)

    def verify_spec(self) -> dict:
        """Static part of the per-task verify spec (Plan adds the affected tests at runtime)."""
        return {
            "kind": "pytest",
            "role": self.role,
            "origin": self.origin,
            "action": self.action,
            "purpose": self.purpose,
            "artifact_contract": self.artifact_contract,
            "subtasks": [s.type for s in self.subtasks],
        }


@runtime_checkable
class Recipe(Protocol):
    name: str
    source_framework: str
    target_framework: str
    # Third-party packages available in the (network-off) sandbox; the LLM is told to use
    # only these + the stdlib + the project's own modules.
    sandbox_packages: list[str]

    def matches(self, files: dict[str, str]) -> bool:
        """True if this recipe applies to the repo (``files`` maps rel-path → source)."""
        ...

    def plan_files(self, files: dict[str, str]) -> list[PlannedFile]:
        """Pick + classify the files to migrate, in dependency order."""
        ...

    def system_prompt(self) -> str:
        ...

    def build_user_prompt(
        self, *, file: PlannedFile, source: str, context: dict[str, str]
    ) -> str:
        ...


_REGISTRY: dict[str, Recipe] = {}


def register(recipe: Recipe) -> Recipe:
    _REGISTRY[recipe.name] = recipe
    return recipe


def get_recipe(name: str) -> Recipe | None:
    return _REGISTRY.get(name)


def known_recipes() -> list[str]:
    return sorted(_REGISTRY)
