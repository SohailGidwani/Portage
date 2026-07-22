"""Execute node — migrate each pending file task with the LLM, on the git worktree.

For every *pending* file Task (in dependency order): gather context (the behavioural test
contract, the framework-agnostic callees, and any already-migrated siblings), prompt the
model for a full rewrite, write it into the worktree, and record content hash + per-file
diff + an attempts_log entry in Postgres.

Durability & recovery (Phase 3):
  * Idempotent / resume-safe — a `done` task whose worktree file still hashes to the
    recorded value is skipped, so a crash mid-Execute resumes without re-calling the model;
    `skipped` tasks (recovery gave up on them) are never retried.
  * Targeted regeneration — Recover rolls back exactly the files it wants redone and resets
    those tasks to pending; Execute regenerates only those, with the failing test output as
    added context.
  * **Model escalation (measured)** — a task's first `escalate_after_attempts` attempts use
    the driver model; later attempts use the escalation model. Every attempt is recorded in
    attempts_log with its tier/model, so "how often does escalation rescue a task?" is a
    queryable fact, not an anecdote.
  * Fault injection (config `inject_fault`, Phase 3 DoD + Phase 4 harness): `bad_patch`
    corrupts the first attempt of the first task; `bad_patch_until_escalation` corrupts
    every driver-tier attempt of the first task, so only escalation can rescue it.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import logging
import re
import symtable
import sys
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath

from portage_agent.config import settings
from portage_agent.core.interfaces import LLMMessage
from portage_agent.db import task_store
from portage_agent.db.models import TaskStatus
from portage_agent.llm import get_llm
from portage_agent.recipes import get_recipe
from portage_agent.recipes.base import PlannedFile, Subtask

from ..state import GraphState
from .artifact_plan import artifact_planned_files
from .common import (
    _module_names,
    _resolve_module,
    _shape_facts,
    content_hash,
    create_cut_checkpoint,
    extract_code,
    file_diff,
    imported_bindings,
    iter_py_files,
    load_cut_checkpoint,
    non_python_context,
    non_python_listing,
    planned_artifact_topology_violations,
    read_file,
    restore_cut_checkpoint,
    run_git,
    worktree_diff,
    write_file,
)
from .oracle import (
    NON_REWRITE_TEST_STRATEGIES,
    apply_sanctioned_normalizations,
    oracle_violations,
)
from .redaction import is_denied_path, scrub

log = logging.getLogger("portage.agent")

_FAULT_PAYLOAD = "\n\n<<< portage fault-injection: deliberately invalid python >>>\n"
_CLUSTER_BLOCK = re.compile(
    r"<<<PORTAGE_FILE:(?P<path>[^>\n]+)>>>\s*"
    r"```(?:python)?\n(?P<body>.*?)```\s*<<<PORTAGE_END_FILE>>>",
    re.DOTALL,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def _restore_rejected_batch(
    worktree: str, batch_paths: list[str], checkpoint: dict, snapshots: list,
) -> list[str]:
    """Restore an executable cut when any member was rejected before Verify."""
    by_path = {task.target_path: task for task in snapshots if task.target_path}
    rejected = {
        path: by_path[path]
        for path in batch_paths
        if path in by_path
        and by_path[path].status in {TaskStatus.skipped.value, TaskStatus.failed.value}
    }
    if not rejected or not checkpoint:
        return []

    records = checkpoint.get("files", {})
    restored = restore_cut_checkpoint(worktree, checkpoint)
    for path in restored:
        task = by_path.get(path)
        record = records.get(path, {})
        if task is None:
            continue
        if not record.get("existed"):
            await run_git("reset", "--", path, cwd=worktree)
        baseline_done = record.get("status") == TaskStatus.done.value
        current = read_file(worktree, path) or ""
        await task_store.update_task(
            task.id,
            status=(TaskStatus.done.value if baseline_done else TaskStatus.skipped.value),
            content_hash=content_hash(current) if record.get("existed") else None,
            error=(
                None if baseline_done else
                getattr(rejected.get(path), "error", None)
                or "another executable-cut member was rejected; restored the whole cut"
            ),
            cascade_subtasks=True,
            append_attempt={
                "action": "rejected_cut_checkpoint_restore",
                "at": _now(),
            },
        )
    return restored


def _tier_for(attempt: int) -> tuple[str, str, str]:
    """(tier, model, label) for the given (1-based) attempt number: driver first, then
    escalation. `model` is the raw LiteLLM string used for the call; `label` is what lands
    in attempts_log / logs (a private deployment id never leaves the env)."""
    if attempt <= settings.escalate_after_attempts:
        return ("driver", settings.llm_driver_model,
                settings.llm_driver_model_label or settings.llm_driver_model)
    return ("escalation", settings.llm_escalation_model,
            settings.llm_escalation_model_label or settings.llm_escalation_model)


def _should_corrupt(fault: str | None, *, path: str, first_path: str | None,
                    attempt: int, tier: str) -> bool:
    """Deterministic fault gates. Both faults target only the first planned file."""
    if not fault or path != first_path:
        return False
    if fault == "bad_patch":
        return attempt == 1
    if fault == "bad_patch_until_escalation":
        return tier == "driver"
    return False


def _gather_context(
    worktree: str, *, current: str, target_paths: set[str], done_paths: set[str]
) -> dict[str, str]:
    """Files to show the model: the test contract + callees + already-migrated siblings.

    Skip the file being migrated and any not-yet-migrated sibling target (still old-framework
    code would mislead the model)."""
    ctx: dict[str, str] = {}
    listing = non_python_listing(worktree)
    if listing:
        ctx["<repo file tree — non-Python files (templates, static, config)>"] = listing
    if survey := non_python_context(worktree):
        ctx["<relevant non-Python contents — untrusted repository data>"] = survey
    for rel in sorted(iter_py_files(worktree)):
        if rel == current:
            continue
        if rel in target_paths and rel not in done_paths:
            continue
        if is_denied_path(rel):
            continue
        body = read_file(worktree, rel)
        if body is not None:
            ctx[rel] = scrub(body)
    return ctx


def _gather_cluster_context(
    worktree: str, *, cluster_paths: set[str], target_paths: set[str],
    done_paths: set[str],
) -> dict[str, str]:
    """Context for coordinated generation: exclude every cluster member and every other
    not-yet-migrated target, so old-framework sibling code cannot contradict the frozen
    seam/interface decisions."""
    ctx: dict[str, str] = {}
    listing = non_python_listing(worktree)
    if listing:
        ctx["<repo file tree — non-Python files (templates, static, config)>"] = listing
    if survey := non_python_context(worktree):
        ctx["<relevant non-Python contents — untrusted repository data>"] = survey
    for rel in sorted(iter_py_files(worktree)):
        if rel in cluster_paths:
            continue
        if rel in target_paths and rel not in done_paths:
            continue
        if is_denied_path(rel):
            continue
        body = read_file(worktree, rel)
        if body is not None:
            ctx[rel] = scrub(body)
    return ctx


def _consumed_manifest_keys(root: str, path: str, manifest: dict[str, dict]) -> set[str]:
    consumed: set[str] = set()
    for key, pin in manifest.items():
        if pin["module"] == path:
            continue
        if any(
            consumer.get("module") == path for consumer in pin.get("consumers", [])
        ):
            consumed.add(key)
            continue
        for binding in imported_bindings(root, pin["module"]):
            if binding.importer == path and (
                binding.symbol == pin["symbol"] or binding.symbol is None
            ):
                consumed.add(key)
                break
    return consumed


def _fmt_pin(p: dict) -> str:
    kind = p.get("target_kind", p.get("kind"))
    line = f"  - {p['symbol']}  (required kind: {kind}; was: {p['original']}"
    if p.get("notes"):
        line += f"; {p['notes']}"
    line += f")\n      TARGET: {p['target_note']}"
    for s in p.get("call_sites", []):
        line += f"\n      current call site: {s}"
    if p.get("additional_exports"):
        line += "\n      REQUIRED ADDITIONAL EXPORTS: " + ", ".join(p["additional_exports"])
    if p.get("members"):
        line += "\n      REQUIRED CLASS MEMBERS: " + ", ".join(p["members"])
    if p.get("member_shapes"):
        line += "\n      REQUIRED CLASS MEMBER SHAPES: " + ", ".join(
            f"{member}={shape}"
            for member, shape in sorted(p["member_shapes"].items())
        )
    if (p.get("shape") or {}).get("returns_nested_function"):
        line += "\n      REQUIRED SHAPE: return a locally defined wrapper/decorator function"
    return line


def contract_sections(manifest: dict[str, dict], path: str,
                      consumed: set[str] | None = None) -> str:
    """DEFINES/CALLS interface-decision sections for one file's prompt. `consumed` =
    manifest keys this file uses (computed by the caller via imported_bindings)."""
    defines = [p for k, p in manifest.items() if p["module"] == path]
    calls = [p for k, p in manifest.items()
             if k in (consumed or set()) and p["module"] != path]
    parts: list[str] = []
    if defines:
        parts.append("INTERFACE DECISIONS — DEFINES (this file owns these; implement "
                     "each TARGET exactly):\n" + "\n".join(_fmt_pin(p) for p in defines))
    if calls:
        parts.append("INTERFACE DECISIONS — CALLS (this file consumes these; invoke "
                     "each TARGET exactly as decided; already-migrated definitions in "
                     "the context files are the authority):\n"
                     + "\n".join(_fmt_pin(p) for p in calls))
    class_calls = [
        pin for pin in calls
        if pin.get("provenance") == "planned_create"
        and pin.get("target_kind") == "class" and pin.get("members")
        and "direct_test_surface" in pin.get("capabilities", [])
        and path in pin.get("factory_consumers", [])
    ]
    if class_calls:
        parts.append(
            "PLANNED CLASS WIRING — import each exact owner class below, construct it "
            "in this consumer, and return it from the public factory or assign it to the "
            "public export. Pass consumer-owned runtime objects into the provider through "
            "constructor/factory arguments; the provider must never import this consumer:\n"
            + "\n".join(
                f"  - {pin['module']}::{pin['symbol']} owns "
                + ", ".join(pin["members"])
                for pin in class_calls
            )
        )
    planned = [p for p in defines if p.get("provenance") == "planned_create"]
    if planned:
        consumers = sorted({
            consumer["module"]
            for pin in planned for consumer in pin.get("consumers", [])
        })
        dependencies = sorted({
            dependency for pin in planned for dependency in pin.get("depends_on", [])
        })
        parts.append(
            "PROVIDER-FIRST ARTIFACT TOPOLOGY — this file must not import any declared "
            f"consumer ({', '.join(consumers) or 'none'}). Its declared lower-level "
            f"dependencies are: {', '.join(dependencies) or 'none'}. Keep shared state "
            "in this provider or a declared dependency so imports remain acyclic. Receive "
            "consumer-owned runtime objects through constructor/function arguments; never "
            "construct or import a consumer, including inside a function body."
        )
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def seam_sections(seam_plan: dict, path: str) -> str:
    """Framework-owned capability decisions relevant to one file, plus its unit."""
    decisions = [
        decision for decision in seam_plan.get("decisions", {}).values()
        if path in decision.get("files", [])
    ]
    unit = next(
        (u for u in seam_plan.get("units", []) if path in u.get("paths", [])), None
    )
    execution_cut = next(
        (cut for cut in seam_plan.get("execution_cuts", [])
         if path in cut.get("paths", [])),
        None,
    )
    parts: list[str] = []
    if decisions:
        lines = [
            f"  - {d['kind']}: {d['instruction']}"
            for d in sorted(decisions, key=lambda d: d["kind"])
        ]
        parts.append(
            "FRAMEWORK SEAM DECISIONS — these are frozen capabilities, not suggestions:\n"
            + "\n".join(lines)
        )
    if unit:
        parts.append(
            f"COUPLED MIGRATION UNIT {unit['id']} ({unit['reason']}): "
            + ", ".join(unit["paths"])
            + ". Keep configuration, lifecycle, factory construction, and test setup "
              "coherent across every member."
        )
    if execution_cut:
        parts.append(
            f"EXECUTABLE VERIFICATION CUT {execution_cut['id']} "
            f"({execution_cut['reason']}, mode={execution_cut['mode']}): "
            + ", ".join(execution_cut["paths"])
            + ". Every member is generated before this cut is sandbox-verified; do not "
              "assume a mixed Flask/FastAPI intermediate state is runnable."
        )
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def extract_cluster_files(text: str, paths: list[str]) -> dict[str, str]:
    """Strictly parse one complete fenced block per requested cluster member."""
    wanted = set(paths)
    found: dict[str, str] = {}
    for match in _CLUSTER_BLOCK.finditer(text):
        path = match.group("path").strip()
        if path not in wanted:
            raise ValueError(f"cluster response contains unexpected path: {path}")
        if path in found:
            raise ValueError(f"cluster response contains duplicate path: {path}")
        found[path] = match.group("body").strip() + "\n"
    missing = wanted - set(found)
    if missing:
        raise ValueError(f"cluster response missing paths: {sorted(missing)}")
    return found


class ClusterOutputError(ValueError):
    """Malformed model output whose already-spent usage must remain accountable."""

    def __init__(self, message: str, usage: dict):
        super().__init__(message)
        self.usage = usage


def _manifest_module_matches(module: str, pin: dict) -> bool:
    return module in _module_names(pin["module"])


def caller_contract_violations(
    content: str, manifest: dict[str, dict], path: str,
) -> list[str]:
    """Check only statically-obvious direct calls to preserved imported functions.

    This intentionally stops far short of whole-program compatibility analysis: aliases,
    relative imports, and direct module attributes are resolved; dynamic dispatch,
    star-args, and reassignments are skipped rather than guessed. The narrow gate catches
    observed `get_db(app)`-style drift without turning Plan into a type checker.
    """
    pins = [
        pin for pin in manifest.values()
        if pin.get("module") != path and pin.get("kind") == "function"
        and pin.get("preserve_shape") and pin.get("shape")
    ]
    if not pins:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []  # definition gate reports the parse failure once

    direct: dict[str, dict] = {}
    modules: list[tuple[str, dict]] = []
    bound_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _resolve_module(node.module, node.level, path)
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound_names.add(alias.asname or alias.name)
                for pin in pins:
                    if _manifest_module_matches(module, pin) and alias.name == pin["symbol"]:
                        direct[alias.asname or alias.name] = pin
                    candidate = f"{module}.{alias.name}".lstrip(".")
                    if _manifest_module_matches(candidate, pin):
                        modules.append((alias.asname or alias.name, pin))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound_names.add(alias.asname or alias.name.split(".")[0])
                for pin in pins:
                    if _manifest_module_matches(alias.name, pin):
                        modules.append((alias.asname or alias.name, pin))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound_names.add(node.name)
        elif isinstance(node, ast.Assign):
            bound_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound_names.add(node.target.id)

    calls: list[tuple[ast.Call, dict, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in direct:
            calls.append((node, direct[node.func.id], node.func.id))
            continue
        if isinstance(node.func, ast.Attribute):
            owner = ast.unparse(node.func.value)
            for local, pin in modules:
                if owner == local and node.func.attr == pin["symbol"]:
                    calls.append((node, pin, f"{owner}.{node.func.attr}"))
                    break

    out: list[str] = []
    # Frozen consumer bindings catch a deleted import followed by the old local call —
    # something generated-import scanning alone cannot see because the evidence was
    # removed by the draft itself.
    for pin in pins:
        for consumer in pin.get("consumers", []):
            if consumer.get("module") != path:
                continue
            local = consumer["local"]
            if consumer.get("binding") == "symbol":
                used = any(
                    isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id == local for node in ast.walk(tree)
                )
                if used and local not in bound_names:
                    out.append(
                        f"{local}: original binding for {pin['symbol']} was removed in "
                        f"{path}, but the generated file still references it"
                    )
            else:
                used = any(
                    isinstance(node, ast.Attribute)
                    and ast.unparse(node.value) == local
                    and node.attr == pin["symbol"] for node in ast.walk(tree)
                )
                bound = any(name == local and candidate is pin for name, candidate in modules)
                if used and not bound:
                    out.append(
                        f"{local}: original module binding for {pin['symbol']} was removed "
                        f"in {path}, but the generated file still references it"
                    )
    for call, pin, display in calls:
        shape = pin["shape"]
        # A starred positional/keyword argument makes the exact arity unknowable. Do not
        # trade false positives for superficial coverage.
        if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
            kw.arg is None for kw in call.keywords
        ):
            continue
        positional = len(call.args)
        capacity = shape.get("positional_capacity")
        if (capacity is not None and not shape.get("accepts_varargs", False)
                and positional > capacity):
            out.append(
                f"{display} call at line {call.lineno}: too many positional arguments "
                f"for pinned {pin['original']} ({positional} > {capacity})"
            )
            continue
        keyword_names = {kw.arg for kw in call.keywords if kw.arg is not None}
        if not shape.get("accepts_varkw", False):
            unexpected = keyword_names - set(shape.get("keyword_names", []))
            if unexpected:
                out.append(
                    f"{display} call at line {call.lineno}: unexpected keyword arguments "
                    f"{sorted(unexpected)} for pinned {pin['original']}"
                )
                continue
        required_names = shape.get("required_positional_names")
        if required_names is not None:
            supplied = set(required_names[:positional]) | keyword_names
            missing = set(required_names) - supplied
            missing |= set(shape.get("required_keyword_only", [])) - keyword_names
            if missing:
                out.append(
                    f"{display} call at line {call.lineno}: missing required arguments "
                    f"{sorted(missing)} for pinned {pin['original']}"
                )
    return out


def planned_provider_import_violations(
    content: str, manifest: dict[str, dict], path: str,
) -> list[str]:
    """Keep consumer imports inside the frozen surface of planned providers."""
    providers: dict[str, set[str]] = {}
    for pin in manifest.values():
        if pin.get("provenance") == "planned_create" and pin.get("module") != path:
            providers.setdefault(pin["module"], set()).add(pin["symbol"])
    if not providers:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    def provider_for(module: str) -> str | None:
        matches = [owner for owner in providers if module in _module_names(owner)]
        return matches[0] if len(matches) == 1 else None

    module_bindings: dict[str, str] = {}
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _resolve_module(node.module, node.level, path)
            owner = provider_for(module)
            for alias in node.names:
                if owner and alias.name != "*" and alias.name not in providers[owner]:
                    out.append(
                        f"{path}:{node.lineno}: imports undeclared `{alias.name}` from "
                        f"planned provider {owner}; declare it in that provider's frozen "
                        "exports or consume an existing export"
                    )
                    continue
                candidate = f"{module}.{alias.name}".lstrip(".")
                if candidate_owner := provider_for(candidate):
                    module_bindings[alias.asname or alias.name] = candidate_owner
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if owner := provider_for(alias.name):
                    module_bindings[alias.asname or alias.name.split(".")[0]] = owner
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in module_bindings
        ):
            continue
        owner = module_bindings[node.value.id]
        if node.attr not in providers[owner]:
            out.append(
                f"{path}:{node.lineno}: reads undeclared `{node.attr}` from planned "
                f"provider {owner}; declare it in that provider's frozen exports or "
                "consume an existing export"
            )
    return list(dict.fromkeys(out))


def planned_capability_consumer_violations(
    content: str, manifest: dict[str, dict], path: str,
) -> list[str]:
    """Require declared consumers to actually construct owned class capabilities."""
    pins = [
        pin for pin in manifest.values()
        if pin.get("provenance") == "planned_create" and pin.get("members")
        and "direct_test_surface" in pin.get("capabilities", [])
        and path in pin.get("factory_consumers", [])
    ]
    if not pins:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    out: list[str] = []
    public_symbols = {
        pin["symbol"] for pin in manifest.values() if pin.get("module") == path
    }
    for pin in pins:
        direct_names: set[str] = set()
        module_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = _resolve_module(node.module, node.level, path)
                if _manifest_module_matches(module, pin):
                    direct_names.update(
                        alias.asname or alias.name for alias in node.names
                        if alias.name == pin["symbol"]
                    )
            elif isinstance(node, ast.Import):
                module_names.update(
                    alias.asname or alias.name for alias in node.names
                    if _manifest_module_matches(alias.name, pin)
                )
        def is_constructor(
            node: ast.AST | None,
            direct: set[str] = direct_names,
            modules: set[str] = module_names,
            symbol: str = pin["symbol"],
        ) -> bool:
            return bool(
                isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id in direct
                or isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func.value) in modules
                and node.func.attr == symbol
            )
            )

        local_factories = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def factory_returns_constructor(
            name: str, factories: dict = local_factories,
        ) -> bool:
            function = factories.get(name)
            if function is None:
                return False
            constructed = {
                target.id
                for statement in ast.walk(function)
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and is_constructor(statement.value)
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            }
            return any(
                isinstance(node, ast.Return) and (
                    is_constructor(node.value)
                    or isinstance(node.value, ast.Name) and node.value.id in constructed
                )
                for node in ast.walk(function)
            )

        exported = any(
            isinstance(statement, (ast.Assign, ast.AnnAssign))
            and (
                is_constructor(statement.value)
                or isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and factory_returns_constructor(statement.value.func.id)
            )
            and any(
                isinstance(target, ast.Name)
                and (not public_symbols or target.id in public_symbols)
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
            for statement in tree.body
        )
        returned = False
        for function in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (not public_symbols or node.name in public_symbols)
        ):
            constructed_names = {
                target.id
                for statement in function.body
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and is_constructor(statement.value)
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            }
            returned = any(
                isinstance(node, ast.Return) and (
                    is_constructor(node.value)
                    or isinstance(node.value, ast.Name)
                    and node.value.id in constructed_names
                )
                for node in ast.walk(function)
            )
            if returned:
                break
        if not direct_names and not module_names:
            out.append(
                f"{path}: declared consumer must import owned capability "
                f"{pin['module']}::{pin['symbol']}"
            )
        elif not (exported or returned):
            out.append(
                f"{path}: declared consumer imports owned capability {pin['symbol']} "
                "but never returns it from a public factory or assigns it to a public export"
            )
    return out


def _direct_body_nodes(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    """Walk one method body without confusing nested ``self`` scopes with its own."""
    found: list[ast.AST] = []

    class Collector(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802
            return None

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):  # noqa: N802
            return None

        def generic_visit(self, node):
            found.append(node)
            super().generic_visit(node)

    visitor = Collector()
    for statement in method.body:
        visitor.visit(statement)
    return found


def _constructed_surface_members(tree: ast.Module, symbol: str) -> set[str]:
    """Members statically exposed by one module-level constructed facade."""
    value = next((
        statement.value
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == symbol
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
        )
    ), None)
    members = {
        target.attr
        for statement in tree.body if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for target in (
            statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        )
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name) and target.value.id == symbol
    }
    if not isinstance(value, (ast.Call, ast.Name)):
        return members
    class_name = (
        value.func.id if isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
        else value.id if isinstance(value, ast.Name) else ""
    )
    if isinstance(value, ast.Call):
        members.update(keyword.arg for keyword in value.keywords if keyword.arg)
    class_node = next((
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ), None)
    if class_node is None:
        return members
    members.update(
        node.name for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    members.update(
        target.id
        for node in class_node.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    )
    members.update(
        node.target.id for node in class_node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    )
    initializer = next((
        node for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "__init__"
    ), None)
    if initializer is not None:
        members.update(
            target.attr
            for node in ast.walk(initializer)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name) and target.value.id == "self"
        )
    return members


def _constructed_callable_members(tree: ast.Module, symbol: str) -> set[str]:
    """Callable subset of a module-level provider's statically realized surface."""
    functions = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    members = {
        target.attr
        for statement in tree.body if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for target in (
            statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        )
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name) and target.value.id == symbol
        and (
            isinstance(statement.value, ast.Lambda)
            or isinstance(statement.value, ast.Name) and statement.value.id in functions
        )
    }
    value = next((
        statement.value
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == symbol
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
        )
    ), None)
    class_name = (
        value.func.id
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
        else ""
    )
    provider_class = next((
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ), None)
    if provider_class is not None:
        members.update(
            node.name for node in provider_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    return members


def framework_seam_violations(
    content: str, seam_plan: dict, path: str, manifest: dict[str, dict] | None = None,
) -> list[str]:
    """Reject capabilities that no frozen plan-owned artifact implements."""
    decisions = list(seam_plan.get("decisions", {}).values())
    relevant = [d for d in decisions if path in d.get("files", [])]
    owned_pins = [
        pin for pin in (manifest or {}).values()
        if pin.get("provenance") == "planned_create"
        and pin.get("target_kind") == "class"
        and (
            path == pin.get("module")
            or any(consumer.get("module") == path for consumer in pin.get("consumers", []))
        )
    ]
    owned_members = {
        member
        for pin in owned_pins
        for member in pin.get("members", [])
    }
    if (
        not relevant and not owned_members and not seam_plan.get("project_modules")
        and "allowed_import_roots" not in seam_plan
    ):
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    module_defs = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    out: list[str] = []

    for decision in (
        item for item in relevant
        if item.get("kind") == "provider_protocol"
        and item.get("provider") == path
    ):
        missing = (
            set(decision.get("decorator_members", []))
            | set(decision.get("callable_members", []))
        ) - (
            _constructed_callable_members(tree, decision["symbol"])
        )
        if missing:
            out.append(
                f"{path}: provider `{decision['symbol']}` is missing callable "
                f"source-decorator members {sorted(missing)}"
            )
        missing_attributes = set(decision.get("attribute_members", [])) - (
            _constructed_surface_members(tree, decision["symbol"])
        )
        if missing_attributes:
            out.append(
                f"{path}: provider `{decision['symbol']}` is missing source-assigned "
                f"members {sorted(missing_attributes)}"
            )
        assigned_values = {}
        for statement in tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == decision["symbol"]
                ):
                    continue
                try:
                    assigned_values[target.attr] = ast.literal_eval(statement.value)
                except (TypeError, ValueError):
                    pass
        provider_assignment = next((
            statement for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == decision["symbol"]
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ), None)
        provider_class_name = (
            provider_assignment.value.func.id
            if provider_assignment is not None
            and isinstance(provider_assignment.value, ast.Call)
            and isinstance(provider_assignment.value.func, ast.Name)
            else ""
        )
        provider_class = next((
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == provider_class_name
        ), None)
        for statement in provider_class.body if provider_class else []:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    try:
                        assigned_values.setdefault(
                            target.id, ast.literal_eval(statement.value),
                        )
                    except (TypeError, ValueError):
                        pass
        for member, expected in decision.get("attribute_values", {}).items():
            if assigned_values.get(member) != expected:
                out.append(
                    f"{path}: provider `{decision['symbol']}.{member}` must preserve "
                    f"the source literal value {expected!r}"
                )
        for callback in decision.get("callbacks", []):
            expected_tree = ast.parse(callback["source"])
            actual = next((
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == callback["function"]
            ), None)
            if actual is None or ast.dump(actual) != ast.dump(expected_tree.body[0]):
                out.append(
                    f"{path}: preserve source-neutral provider callback "
                    f"`{callback['function']}` exactly"
                )

    for decision in (
        item for item in relevant
        if item.get("kind") == "authentication_runtime"
        and path in item.get("consumer_bindings", {})
    ):
        bindings = decision["consumer_bindings"][path]
        provider = decision["provider"]
        imported = {
            (alias.name, alias.asname or alias.name)
            for statement in tree.body if isinstance(statement, ast.ImportFrom)
            and _resolve_module(statement.module, statement.level, path)
            in _module_names(provider)
            for alias in statement.names
        }
        expected = {(item["symbol"], item["local"]) for item in bindings}
        missing = expected - imported
        if missing:
            out.append(
                f"{path}: authentication consumer must import frozen provider names "
                f"{sorted(missing)} from {provider}"
            )
        top_level = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        duplicated = top_level & {item["local"] for item in bindings}
        if duplicated:
            out.append(
                f"{path}: authentication consumer duplicates provider-owned names "
                f"{sorted(duplicated)}"
            )
        for binding in bindings:
            allowed = {
                (shape["positional"], tuple(shape["keywords"]))
                for shape in binding.get("call_shapes", [])
            }
            if not allowed:
                continue
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == binding["local"]
                and not any(isinstance(argument, ast.Starred) for argument in node.args)
                and all(keyword.arg is not None for keyword in node.keywords)
            ]
            if not calls:
                out.append(
                    f"{path}: missing source-observed call to authentication provider "
                    f"`{binding['local']}`"
                )
            for call in calls:
                shape = (
                    len(call.args),
                    tuple(sorted(keyword.arg for keyword in call.keywords if keyword.arg)),
                )
                if shape not in allowed:
                    out.append(
                        f"{path}:{call.lineno}: authentication call `{binding['local']}` "
                        f"has shape {shape}, expected one of {sorted(allowed)}"
                    )

    for decision in (
        item for item in relevant
        if item.get("kind") == "template_runtime"
        and path in item.get("provider_files", [])
        and "current_user" in item.get("context_globals", [])
    ):
        auth_provider = decision.get("authentication_provider", "")
        imported = any(
            isinstance(statement, ast.ImportFrom)
            and _resolve_module(statement.module, statement.level, path)
            in _module_names(auth_provider)
            and any(alias.name == "current_user" for alias in statement.names)
            for statement in tree.body
        )
        injected = any(
            isinstance(node, ast.Dict)
            and any(
                isinstance(key, ast.Constant) and key.value == "current_user"
                for key in node.keys
            )
            for node in ast.walk(tree)
        )
        if not imported or not injected:
            out.append(
                f"{path}: template provider must inject the frozen `current_user` "
                f"proxy from {auth_provider}"
            )

    for decision in (
        item for item in relevant
        if item.get("kind") == "template_context_processors"
    ):
        if path in decision.get("factory_files", []):
            for contract in (
                item for item in decision.get("processors", [])
                if item["provider"] == path
            ):
                factory = next((
                    node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == contract["factory"]
                ), None)
                callback = next((
                    node for node in (factory.body if factory else [])
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == contract["function"]
                ), None)
                if callback is None:
                    out.append(
                        f"{path}: template context processor "
                        f"`{contract['function']}` is missing from its source factory"
                    )
                    continue
                if contract.get("source"):
                    expected = ast.parse(contract["source"]).body[0]
                    expected.decorator_list = []
                    if ast.dump(callback) != ast.dump(expected):
                        out.append(
                            f"{path}: preserve source-neutral template context "
                            f"processor `{contract['function']}` exactly"
                        )
                target = (
                    f"{contract['receiver']}.state._portage_context_processors"
                )
                registered = any(
                    isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and any(
                        ast.unparse(candidate) == target
                        for candidate in (
                            statement.targets if isinstance(statement, ast.Assign)
                            else [statement.target]
                        )
                    )
                    and isinstance(statement.value, (ast.Tuple, ast.List))
                    and contract["function"] in {
                        item.id for item in statement.value.elts
                        if isinstance(item, ast.Name)
                    }
                    for statement in factory.body
                )
                if not registered:
                    out.append(
                        f"{path}: register source template context processor "
                        f"`{contract['function']}` on the target application"
                    )
        if path in decision.get("template_provider_files", []):
            has_registry_read = any(
                isinstance(node, ast.Constant)
                and node.value == "_portage_context_processors"
                for node in ast.walk(tree)
            )
            has_mapping_merge = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "values"
                and node.func.attr == "update"
                and node.args and isinstance(node.args[0], ast.Call)
                for node in ast.walk(tree)
            )
            if not has_registry_read or not has_mapping_merge:
                out.append(
                    f"{path}: template provider must merge all frozen source "
                    "context-processor mappings at request time"
                )

    for decision in (
        item for item in relevant
        if item.get("kind") == "returned_lifecycle"
        and item.get("provider") == path
    ):
        owner = next((
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == decision.get("class")
        ), None)
        for contract in decision.get("contracts", []):
            factory = next((
                node for node in (owner.body if owner else [])
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == contract["factory_member"]
            ), None)
            returned_class = next((
                node.value.func.id
                for node in (ast.walk(factory) if factory is not None else ())
                if isinstance(node, ast.Return)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
            ), "")
            lifecycle_class = next((
                node for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == returned_class
            ), None)
            realized = {
                node.name for node in (lifecycle_class.body if lifecycle_class else [])
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            required = {
                contract["entry_member"], contract["exit_member"],
                "__enter__", "__exit__",
            }
            if factory is None or not required <= realized:
                out.append(
                    f"{path}: `{contract['factory_member']}` must return a lifecycle "
                    f"object with callable members {sorted(required)}"
                )

    for decision in (
        item for item in relevant
        if item.get("kind") == "extension_provider"
        and item.get("provider") == path
    ):
        symbol = decision["symbol"]
        missing = set(decision.get("members", [])) - _constructed_surface_members(
            tree, symbol,
        )
        if missing:
            out.append(
                f"{path}: SQLAlchemy provider `{symbol}` is missing source-exercised "
                f"members {sorted(missing)}"
            )
        consumers = decision.get("consumers", [])
        provider_index = next((
            index for index, statement in enumerate(tree.body)
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == symbol
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ), None)
        for index, node in enumerate(tree.body):
            modules = []
            if isinstance(node, ast.ImportFrom):
                base = _resolve_module(node.module, node.level, path)
                modules = [base, *(
                    f"{base}.{alias.name}".lstrip(".") for alias in node.names
                )]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            consumer = next((
                consumer for consumer in consumers
                if any(module in _module_names(consumer) for module in modules)
            ), None)
            if consumer and (provider_index is None or index < provider_index):
                out.append(
                    f"{path}:{node.lineno}: extension provider `{symbol}` imports "
                    f"consumer {consumer} before provider initialization; construct "
                    "the provider first"
                )

    for _decision in (
        item for item in relevant
        if item.get("kind") == "application_factory"
        and item.get("factory") == path and item.get("config_from_objects")
    ):
        for node in ast.walk(tree):
            copied = None
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "vars" and node.args
            ):
                copied = "vars()"
            elif isinstance(node, ast.Attribute) and node.attr == "__dict__":
                copied = "__dict__"
            if copied:
                out.append(
                    f"{path}:{node.lineno}: {copied} drops inherited Flask "
                    "config.from_object settings; copy uppercase dir()/getattr() values"
                )

    if "allowed_import_roots" in seam_plan:
        allowed_imports = {
            *sys.stdlib_module_names,
            *seam_plan["allowed_import_roots"],
            *seam_plan.get("original_import_roots", {}).get(path, []),
            *seam_plan.get("project_roots", []),
            *(path.split("/", 1)[:1] if "/" in path else []),
        }
        for node in tree.body:
            roots = (
                [node.module.split(".")[0]]
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                else [alias.name.split(".")[0] for alias in node.names]
                if isinstance(node, ast.Import) else []
            )
            for root in roots:
                if root not in allowed_imports:
                    out.append(
                        f"{path}:{node.lineno}: import `{root}` is unavailable in the "
                        "network-off migration sandbox"
                    )

    direct_classes = {
        pin["symbol"] for pin in owned_pins if pin.get("module") == path
    }
    module_aliases: dict[str, set[str]] = {}
    for pin in owned_pins:
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                module = _resolve_module(node.module, node.level, path)
                if _manifest_module_matches(module, pin):
                    direct_classes.update(
                        alias.asname or alias.name
                        for alias in node.names if alias.name == pin["symbol"]
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _manifest_module_matches(alias.name, pin):
                        module_aliases.setdefault(alias.asname or alias.name, set()).add(
                            pin["symbol"]
                        )

    def is_owned_constructor(node: ast.AST | None) -> bool:
        return bool(
            isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id in direct_classes
                or isinstance(node.func, ast.Attribute)
                and node.func.attr in module_aliases.get(ast.unparse(node.func.value), set())
            )
        )

    owned_instances = {
        target.id
        for statement in ast.walk(tree)
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and is_owned_constructor(statement.value)
        for target in (
            statement.targets if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if isinstance(target, ast.Name)
    }

    def valid_owned_receiver(node: ast.Attribute) -> bool:
        if node.attr not in owned_members:
            return False
        if isinstance(node.value, ast.Name):
            return node.value.id in owned_instances or (
                node.value.id == "self"
                and any(pin.get("module") == path for pin in owned_pins)
            )
        return is_owned_constructor(node.value)

    router_names = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "fastapi"
        for alias in node.names if alias.name == "APIRouter"
    }
    fastapi_modules = {
        alias.asname or alias.name
        for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names if alias.name == "fastapi"
    }

    def is_router_constructor(node: ast.AST | None) -> bool:
        return bool(
            isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id in router_names
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "APIRouter"
                and ast.unparse(node.func.value) in fastapi_modules
            )
        )

    router_instances = {
        target.id
        for statement in ast.walk(tree)
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and is_router_constructor(statement.value)
        for target in (
            statement.targets if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {
                "middleware", "add_middleware", "exception_handler", "errorhandler",
            }
            and isinstance(node.value, ast.Name)
            and node.value.id in router_instances
        ):
            out.append(
                f"{path}:{node.lineno}: APIRouter `{node.value.id}` has no "
                f"`{node.attr}` API; middleware and exception handlers belong to the "
                "FastAPI application"
            )
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).split(".")[-1] == "exception_handler"
            for decorator in node.decorator_list
        )
    ):
        if any(
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Tuple)
            and len(node.value.elts) == 2
            and isinstance(node.value.elts[1], ast.Constant)
            and isinstance(node.value.elts[1].value, int)
            for node in ast.walk(function)
        ):
            out.append(
                f"{path}:{function.lineno}: FastAPI exception handlers must return a "
                "Response carrying status_code, not a Flask (response, status) tuple"
            )

    def _exception_names(node: ast.AST | None) -> set[str]:
        if isinstance(node, ast.Tuple):
            return {name for item in node.elts for name in _exception_names(item)}
        if isinstance(node, (ast.Name, ast.Attribute)):
            return {ast.unparse(node).split(".")[-1]}
        return set()

    def _matches_error_response(call: ast.Call, contract: dict) -> bool:
        status = next((
            keyword.value for keyword in call.keywords
            if keyword.arg == "status_code"
        ), None)
        content = next((
            keyword.value for keyword in call.keywords if keyword.arg == "content"
        ), None)
        if not isinstance(status, ast.Constant) or not isinstance(content, ast.Dict):
            return False
        keys = sorted(
            key.value for key in content.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        )
        return status.value == contract["status_code"] and keys == contract["json_keys"]

    for decision in (
        item for item in relevant if item.get("kind") == "error_handler_ownership"
    ):
        handlers = decision.get("handlers", [])
        if decision.get("owner") == path:
            for contract in handlers:
                functions = [
                    function for function in ast.walk(tree)
                    if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and any(
                        isinstance(decorator, ast.Call) and decorator.args
                        and ast.unparse(decorator.func).split(".")[-1]
                        == "exception_handler"
                        and contract["exception_name"]
                        in _exception_names(decorator.args[0])
                        for decorator in function.decorator_list
                    )
                ]
                responses = [
                    call for function in functions for call in ast.walk(function)
                    if isinstance(call, ast.Call)
                    and ast.unparse(call.func).split(".")[-1] == "JSONResponse"
                ]
                realized = any(
                    _matches_error_response(call, contract) for call in responses
                )
                if not realized:
                    out.append(
                        f"{path}: exception handler `{contract['exception']}` must return "
                        f"status {contract['status_code']} with top-level JSON keys "
                        f"{contract['json_keys']}"
                    )

        owned_names = {item["exception_name"] for item in handlers}
        for function_name in decision.get("route_functions", {}).get(path, []):
            function = next((
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ), None)
            caught = {
                name for node in (ast.walk(function) if function is not None else [])
                if isinstance(node, ast.ExceptHandler)
                for name in _exception_names(node.type)
            }
            if swallowed := caught & owned_names:
                out.append(
                    f"{path}: route `{function_name}` catches app-owned exceptions "
                    f"{sorted(swallowed)}; let them propagate to the frozen response "
                    "envelope handlers"
                )

    for decision in (
        item for item in relevant if item.get("kind") == "blueprint_error_handlers"
    ):
        handlers = decision.get("handlers", [])
        if decision.get("handler_path") == path:
            for contract in handlers:
                function = next((
                    node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == contract["function"]
                ), None)
                if function is None:
                    out.append(
                        f"{path}: blueprint error handler `{contract['function']}` is missing"
                    )
                    continue
                invalid_decorators = [
                    ast.unparse(decorator)
                    for decorator in function.decorator_list
                    if isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in {
                        "errorhandler", "app_errorhandler", "exception_handler",
                        "route", "get", "post", "put", "patch", "delete",
                    }
                ]
                if invalid_decorators:
                    out.append(
                        f"{path}: blueprint handler `{contract['function']}` must be an "
                        f"undecorated app-owned function, got {invalid_decorators}"
                    )
                calls = {
                    ast.unparse(node.func).split(".")[-1]
                    for node in ast.walk(function) if isinstance(node, ast.Call)
                }
                if missing := set(contract.get("response_helpers", [])) - calls:
                    out.append(
                        f"{path}: blueprint handler `{contract['function']}` omits "
                        f"source response helpers {sorted(missing)}"
                    )
                constants = {
                    node.value for node in ast.walk(function)
                    if isinstance(node, ast.Constant)
                }
                missing_status = set(contract.get("status_codes", [])) - constants
                missing_literals = set(contract.get("response_literals", [])) - constants
                if missing_status or missing_literals:
                    out.append(
                        f"{path}: blueprint handler `{contract['function']}` changed "
                        f"frozen response status/body literals; missing "
                        f"{sorted({*missing_status, *missing_literals}, key=str)}"
                    )
                positional = [
                    argument.arg
                    for argument in [*function.args.posonlyargs, *function.args.args]
                ]
                if len(positional) < 2 or positional[0] != "request":
                    out.append(
                        f"{path}: app-owned handler `{contract['function']}` must accept "
                        "request and exception arguments"
                    )

            for helper in (
                helper for contract in handlers
                for helper in contract.get("payload_helpers", [])
            ):
                function = next((
                    node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == helper["function"]
                ), None)
                if function is None:
                    out.append(
                        f"{path}: response helper `{helper['function']}` is missing"
                    )
                    continue
                constants = {
                    node.value for node in ast.walk(function)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                }
                missing = set(helper.get("string_literals", [])) - constants
                returned = {
                    node.elts[1].id
                    for node in ast.walk(function)
                    if isinstance(node, ast.Tuple) and len(node.elts) >= 2
                    and isinstance(node.elts[1], ast.Name)
                }
                missing_status = (
                    set(helper.get("returned_status_parameters", [])) - returned
                )
                if missing or missing_status:
                    out.append(
                        f"{path}: response helper `{helper['function']}` changed frozen "
                        f"payload/status facts; missing literals {sorted(missing)} and "
                        f"status parameters {sorted(missing_status)}"
                    )

        if path in decision.get("factory_files", []):
            top_level_handler_imports = []
            for contract in handlers:
                handler_module = contract["handler_path"].removesuffix(".py").replace(
                    "/", ".",
                )
                top_level_handler_imports.extend(
                    alias.name
                    for node in tree.body if isinstance(node, ast.ImportFrom)
                    and _resolve_module(node.module, node.level, path) == handler_module
                    for alias in node.names if alias.name == contract["function"]
                )
            if top_level_handler_imports:
                out.append(
                    f"{path}: blueprint handlers must be imported inside the application "
                    f"factory to avoid provider cycles, got {top_level_handler_imports}"
                )
            registrations = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_exception_handler" and len(node.args) >= 2
            ]
            for contract in handlers:
                registration = contract["registration"]
                if registration["kind"] == "status":
                    registered = any(
                        isinstance(call.args[0], ast.Constant)
                        and call.args[0].value == registration["value"]
                        for call in registrations
                    )
                elif registration["kind"] == "builtin":
                    registered = any(
                        isinstance(call.args[0], ast.Name)
                        and call.args[0].id == registration["symbol"]
                        for call in registrations
                    )
                else:
                    exception_refs = {
                        alias.asname or alias.name
                        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                        and _resolve_module(node.module, node.level, path)
                        == registration["module"]
                        for alias in node.names if alias.name == registration["symbol"]
                    }
                    registered = any(
                        isinstance(call.args[0], ast.Name)
                        and call.args[0].id in exception_refs
                        for call in registrations
                    )
                handler_module = contract["handler_path"].removesuffix(".py").replace(
                    "/", ".",
                )
                handler_refs = {
                    alias.asname or alias.name
                    for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                    and _resolve_module(node.module, node.level, path) == handler_module
                    for alias in node.names if alias.name == contract["function"]
                }
                wrapper_names = {
                    call.args[1].id for call in registrations
                    if isinstance(call.args[1], ast.Name)
                }
                forwards = any(
                    isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and function.name in wrapper_names
                    and any(
                        isinstance(node, ast.Name) and node.id in handler_refs
                        for node in ast.walk(function)
                    )
                    for function in ast.walk(tree)
                )
                if not registered or not handler_refs or not forwards:
                    out.append(
                        f"{path}: application factory must register source handler "
                        f"{handler_module}::{contract['function']} for "
                        f"{registration.get('source', registration.get('value'))}"
                    )

    for decision in (
        item for item in relevant
        if item.get("kind") == "test_harness" and item.get("path") == path
    ):
        for function_name in decision.get("direct_json_return_functions", []):
            functions = [
                node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ]
            direct = any(
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in {"get_json", "json"}
                for function in functions for node in ast.walk(function)
            )
            unwraps_detail = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "detail"
                for function in functions for node in ast.walk(function)
            )
            if not direct or unwraps_detail:
                out.append(
                    f"{path}: test helper `{function_name}` must return response JSON "
                    "directly; application error envelopes cannot be unwrapped in tests"
                )

    def _attribute_path(node: ast.AST) -> tuple[str, ...]:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return tuple(reversed(parts))

    def _is_state_config(node: ast.AST) -> bool:
        parts = _attribute_path(node)
        return len(parts) >= 3 and parts[-2:] == ("state", "config")

    def _state_config_written_keys() -> tuple[set[str], list[int], dict[str, list[int]]]:
        keys: set[str] = set()
        writes: list[int] = []
        updates: dict[str, list[int]] = {}
        local_configs = {
            node.value.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(node.value, ast.Name)
            and any(
                _is_state_config(target)
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                for target in targets:
                    if _is_state_config(target):
                        writes.append(node.lineno)
                        if isinstance(value, ast.Dict):
                            keys.update(
                                key.value for key in value.keys
                                if isinstance(key, ast.Constant)
                                and isinstance(key.value, str)
                            )
                    elif isinstance(target, ast.Name) and target.id in local_configs:
                        if isinstance(value, ast.Dict):
                            keys.update(
                                key.value for key in value.keys
                                if isinstance(key, ast.Constant)
                                and isinstance(key.value, str)
                            )
                    elif (
                        isinstance(target, ast.Subscript)
                        and (
                            _is_state_config(target.value)
                            or isinstance(target.value, ast.Name)
                            and target.value.id in local_configs
                        )
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        writes.append(node.lineno)
                        keys.add(target.slice.value)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and (
                    _is_state_config(node.func.value)
                    or isinstance(node.func.value, ast.Name)
                    and node.func.value.id in local_configs
                )
            ):
                writes.append(node.lineno)
                if node.args and isinstance(node.args[0], ast.Dict):
                    keys.update(
                        key.value for key in node.args[0].keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
                if node.args and isinstance(node.args[0], ast.Name):
                    updates.setdefault(node.args[0].id, []).append(node.lineno)
        return keys, writes, updates

    def _imported_callable_refs(
        module_path: str, symbol: str,
        function: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
    ) -> set[str]:
        refs: set[str] = set()
        wanted = _module_names(module_path)
        tails = {name.split(".")[-1] for name in wanted}
        for node in [*tree.body, *(function.body if function else [])]:
            if isinstance(node, ast.ImportFrom):
                module = _resolve_module(node.module, node.level, path)
                if module in wanted or module.split(".")[-1] in tails:
                    refs.update(
                        alias.asname or alias.name
                        for alias in node.names if alias.name == symbol
                    )
                for alias in node.names:
                    candidate = f"{module}.{alias.name}".lstrip(".")
                    if candidate in wanted:
                        refs.add(f"{alias.asname or alias.name}.{symbol}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in wanted or alias.name.split(".")[-1] in tails:
                        refs.add(f"{alias.asname or alias.name}.{symbol}")
        return refs

    for decision in (
        item for item in relevant
        if item.get("kind") == "application_factory" and item.get("factory") == path
    ):
        application_factory = next((
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "create_app"
        ), None)
        for contract in decision.get("local_imports", []):
            top_level = any(
                isinstance(node, ast.ImportFrom)
                and _resolve_module(node.module, node.level, path) == contract["module"]
                and any(alias.name == contract["symbol"] for alias in node.names)
                for node in tree.body
            )
            if top_level:
                out.append(
                    f"{path}: source-local import {contract['module']}::"
                    f"{contract['symbol']} must remain inside create_app to avoid "
                    "provider cycles"
                )
        config_keys, config_writes, config_updates = _state_config_written_keys()
        required_keys = set(decision.get("config_keys", []))
        object_sources = set(decision.get("config_from_objects", []))
        copies_object_config = any(
            isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                _is_state_config(target)
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
            and any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "dir"
                and len(node.args) == 1
                and ast.unparse(node.args[0]) in object_sources
                for node in ast.walk(statement.value)
            )
            for statement in ast.walk(application_factory or tree)
        )
        if copies_object_config:
            config_keys.update(required_keys)
        if required_keys and not config_writes:
            out.append(
                f"{path}: application factory must create a plain app.state.config dict "
                "before initializing providers"
            )
        missing_keys = required_keys - config_keys
        if missing_keys:
            out.append(
                f"{path}: app.state.config omits original default keys "
                f"{sorted(missing_keys)}"
            )
        for parameter in decision.get("override_parameters", []):
            if parameter not in config_updates:
                out.append(
                    f"{path}: application factory must apply `{parameter}` with "
                    "app.state.config.update(...) before provider initialization"
                )
        ready_line = max(
            [*config_writes, *(
                line for parameter in decision.get("override_parameters", [])
                for line in config_updates.get(parameter, [])
            )],
            default=0,
        )
        for initializer in decision.get("initializers", []):
            refs = _imported_callable_refs(
                initializer["provider"], initializer["symbol"], application_factory,
            )
            calls = [
                node.lineno for node in ast.walk(application_factory or tree)
                if isinstance(node, ast.Call) and ast.unparse(node.func) in refs
            ]
            if not calls:
                out.append(
                    f"{path}: application factory must call "
                    f"{initializer['provider']}::{initializer['symbol']}"
                )
            elif min(calls) <= ready_line:
                out.append(
                    f"{path}: provider initializer {initializer['symbol']} must run "
                    "after defaults and test configuration overrides"
                )

        owner_calls = [
            node for node in ast.walk(tree) if is_owned_constructor(node)
        ]
        # Only a frozen plan-owned facade accepts cleanup callbacks. Raw FastAPI apps
        # adapted in test plumbing have no such constructor contract.
        for callback in decision.get("cleanup_callbacks", []) if owner_calls else []:
            for function_name in callback.get("functions", []):
                refs = _imported_callable_refs(
                    callback["provider"], function_name, application_factory,
                )
                wired = any(
                    any(
                        ast.unparse(node) in refs for node in ast.walk(keyword.value)
                    )
                    for call in owner_calls for keyword in call.keywords
                    if keyword.arg == "cleanup_callbacks"
                )
                if not wired:
                    out.append(
                        f"{path}: planned application facade must receive cleanup "
                        f"callback {callback['provider']}::{function_name} for "
                        "app-context exit"
                    )

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "copy"
                and ".routes" in ast.unparse(node.func.value)
            ):
                out.append(
                    f"{path}:{node.lineno}: preserve endpoint aliases with a supported "
                    "named route registration; APIRoute objects must not be copied"
                )
        named_routes = {
            (
                node.args[0].value,
                next(
                    (keyword.value.value for keyword in node.keywords
                     if keyword.arg == "name"
                     and isinstance(keyword.value, ast.Constant)
                     and isinstance(keyword.value.value, str)),
                    "",
                ),
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {
                "add_api_route", "add_route", "api_route", "get", "post", "put",
                "patch", "delete", "options", "head",
            }
            and node.args and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        for alias in decision.get("endpoint_aliases", []):
            if (alias["path"], alias["name"]) not in named_routes:
                out.append(
                    f"{path}: application factory must preserve reverse-URL alias "
                    f"{alias['name']!r} for path {alias['path']!r} with a supported "
                    "named target route"
                )

        for call in (
            node for node in ast.walk(tree) if isinstance(node, ast.Call)
        ):
            lifespan = next(
                (keyword.value for keyword in call.keywords
                 if keyword.arg == "lifespan"),
                None,
            )
            if not isinstance(lifespan, ast.Name):
                continue
            function = module_defs.get(lifespan.id)
            if not isinstance(function, ast.AsyncFunctionDef):
                continue
            shape = _shape_facts(function)
            decorated = any(
                ast.unparse(decorator).removesuffix("()")
                .endswith("asynccontextmanager")
                for decorator in function.decorator_list
            )
            if shape["is_generator"] and not decorated:
                out.append(
                    f"{path}:{function.lineno}: lifespan `{function.name}` is a bare "
                    "async generator; decorate it with @contextlib.asynccontextmanager "
                    "before passing it to FastAPI"
                )

        static_mount = decision.get("static_mount") or {}
        if static_mount:
            mounted = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "mount"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == static_mount["path"]
                and any(
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == static_mount["name"]
                    for keyword in node.keywords
                )
                and any(
                    isinstance(part, ast.Call)
                    and (
                        isinstance(part.func, ast.Name)
                        and part.func.id == "StaticFiles"
                        or isinstance(part.func, ast.Attribute)
                        and part.func.attr == "StaticFiles"
                    )
                    for part in ast.walk(node)
                )
                for node in ast.walk(tree)
            )
            has_package_path = any(
                isinstance(node, ast.Name) and node.id == "__file__"
                for node in ast.walk(tree)
            ) and any(
                isinstance(node, ast.Constant)
                and node.value == PurePosixPath(static_mount["directory"]).name
                for node in ast.walk(tree)
            )
            if not mounted or not has_package_path:
                out.append(
                    f"{path}: preserve template static endpoint by mounting StaticFiles "
                    f"at {static_mount['path']!r} with name={static_mount['name']!r} "
                    "and a package directory resolved from __file__"
                )

        fastapi_names = {
            alias.asname or alias.name
            for node in tree.body if isinstance(node, ast.ImportFrom)
            and node.module == "fastapi"
            for alias in node.names if alias.name == "FastAPI"
        }
        raw_apps = {
            target.id
            for statement in ast.walk(tree)
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id in fastapi_names
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if (
                is_owned_constructor(node)
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in raw_apps
            ):
                out.append(
                    f"{path}:{node.lineno}: planned FastAPI facade must be constructed "
                    "as the application itself, not wrapped around a separately-created "
                    "FastAPI positional argument"
                )
            if (
                "testing" in owned_members and is_owned_constructor(node)
                and not any(keyword.arg == "testing" for keyword in node.keywords)
            ):
                out.append(
                    f"{path}:{node.lineno}: planned application facade must receive "
                    "`testing=` from the original factory test configuration"
                )
            if "testing" in owned_members and is_owned_constructor(node):
                testing_value = next(
                    (keyword.value for keyword in node.keywords
                     if keyword.arg == "testing"),
                    None,
                )
                for parameter in decision.get("optional_parameters", []):
                    if testing_value is None:
                        continue
                    direct_get = any(
                        isinstance(candidate, ast.Call)
                        and isinstance(candidate.func, ast.Attribute)
                        and candidate.func.attr == "get"
                        and isinstance(candidate.func.value, ast.Name)
                        and candidate.func.value.id == parameter
                        for candidate in ast.walk(testing_value)
                    )
                    guarded = any(
                        isinstance(candidate, ast.BoolOp)
                        and isinstance(candidate.op, ast.And)
                        and any(
                            isinstance(part, ast.Name) and part.id == parameter
                            for part in ast.walk(candidate)
                        )
                        or isinstance(candidate, ast.IfExp)
                        and any(
                            isinstance(part, ast.Name) and part.id == parameter
                            for part in ast.walk(candidate.test)
                        )
                        for candidate in ast.walk(testing_value)
                    )
                    if direct_get and not guarded:
                        out.append(
                            f"{path}:{node.lineno}: optional config `{parameter}` may be "
                            "None; guard it before deriving the facade `testing` value"
                        )

    for decision in (
        item for item in relevant if item.get("kind") == "route_names"
    ):
        def route_shape(value: str) -> str:
            shaped = re.sub(r"\{[^}]+\}", "{}", value)
            return shaped.rstrip("/") or "/"

        functions = {
            node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        router_prefixes: dict[str, set[str]] = {}
        for route in decision.get("routes", []):
            route_path = route.get("path")
            route_functions = [
                function for function in functions.values()
                if function.name == route["function"] or route_path and any(
                    isinstance(decorator, ast.Call)
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                    and route_shape(decorator.args[0].value)
                    == route_shape(route_path)
                    for decorator in function.decorator_list
                )
            ]
            if route.get("prefix"):
                receivers = {
                    decorator.func.value.id
                    for function in route_functions
                    for decorator in function.decorator_list
                    if isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                }
                receivers.update(
                    call.func.value.id
                    for call in ast.walk(tree)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.attr == "add_api_route"
                    and len(call.args) >= 2
                    and isinstance(call.args[1], ast.Name)
                    and (
                        call.args[1].id == route["function"]
                        or route_path and isinstance(call.args[0], ast.Constant)
                        and isinstance(call.args[0].value, str)
                        and route_shape(call.args[0].value) == route_shape(route_path)
                    )
                )
                for receiver in receivers:
                    router_prefixes.setdefault(receiver, set()).add(route["prefix"])
            names = {
                keyword.value.value
                for function in route_functions
                for decorator in function.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in {
                    "api_route", "get", "post", "put", "patch", "delete",
                    "options", "head",
                }
                for keyword in decorator.keywords
                if keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            }
            names.update(
                keyword.value.value
                for call in ast.walk(tree)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "add_api_route"
                and len(call.args) >= 2
                and isinstance(call.args[1], ast.Name)
                and (
                    call.args[1].id == route["function"]
                    or route_path and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                    and route_shape(call.args[0].value) == route_shape(route_path)
                )
                for keyword in call.keywords
                if keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            )
            if route["name"] not in names:
                out.append(
                    f"{path}: route `{route['function']}` must preserve reverse-URL "
                    f"name {route['name']!r}"
                )
        for receiver, expected in router_prefixes.items():
            router = next((
                statement.value
                for statement in tree.body
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and isinstance(statement.value, ast.Call)
                and ast.unparse(statement.value.func).split(".")[-1] == "APIRouter"
                and any(
                    isinstance(target, ast.Name) and target.id == receiver
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                )
            ), None)
            actual = {
                keyword.value.value
                for keyword in (router.keywords if router else [])
                if keyword.arg == "prefix"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            }
            if len(expected) == 1 and actual != expected:
                out.append(
                    f"{path}: router `{receiver}` must preserve source URL prefix "
                    f"{next(iter(expected))!r}"
                )

    for decision in (
        item for item in relevant if item.get("kind") == "view_decorators"
    ):
        for contract in decision.get("decorators", []):
            function = module_defs.get(contract["function"])
            nested = [
                node for node in (function.body if function else [])
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            returned = {
                node.value.id for node in (ast.walk(function) if function else ())
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
            }
            wrapper = next(
                (node for node in nested if node.name == contract["wrapper"]),
                next((node for node in nested if node.name in returned), None),
            )
            wrapped = wrapper is not None and any(
                isinstance(decorator, ast.Call)
                and ast.unparse(decorator.func).split(".")[-1] == "wraps"
                and decorator.args
                and isinstance(decorator.args[0], ast.Name)
                and decorator.args[0].id == contract["parameter"]
                for decorator in wrapper.decorator_list
            )
            if not wrapped:
                out.append(
                    f"{path}: view decorator `{contract['function']}` must preserve "
                    f"the wrapped endpoint signature with functools.wraps"
                )
            if wrapper is None:
                continue
            wrapper_parameters = {
                argument.arg for argument in [
                    *wrapper.args.posonlyargs, *wrapper.args.args,
                    *wrapper.args.kwonlyargs,
                ]
            }
            if any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == contract["parameter"]
                and any(
                    isinstance(argument, ast.Name)
                    and argument.id in wrapper_parameters
                    for argument in call.args
                )
                for call in ast.walk(wrapper)
            ):
                out.append(
                    f"{path}: view decorator `{contract['function']}` must forward "
                    "explicit wrapper parameters by keyword so endpoint parameter order "
                    "is preserved"
                )

    for decision in (
        item for item in relevant
        if item.get("kind") == "response_shape" and item.get("path") == path
    ):
        for function_name in decision.get("plain_string_functions", []):
            function = next((
                node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ), None)
            explicit_response = function is not None and any(
                isinstance(decorator, ast.Call)
                and any(
                    keyword.arg == "response_class"
                    and ast.unparse(keyword.value).split(".")[-1]
                    in {"HTMLResponse", "PlainTextResponse"}
                    for keyword in decorator.keywords
                )
                for decorator in function.decorator_list
            )
            if function is not None and not explicit_response and any(
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                for node in ast.walk(function)
            ):
                out.append(
                    f"{path}:{function.lineno}: FastAPI route `{function_name}` must "
                    "wrap its original plain string in HTMLResponse/PlainTextResponse; "
                    "a bare string changes the response bytes to JSON"
                )

    for _decision in (
        item for item in relevant
        if item.get("kind") == "planned_test_surface" and item.get("provider") == path
    ):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_middleware"
            ):
                out.append(
                    f"{path}:{node.lineno}: planned application-surface provider must "
                    "not install middleware; the application factory owns middleware "
                    "configuration exactly once"
                )
        cleanup_required = any(
            item.get("kind") == "resource_lifecycle"
            and item.get("cleanup_functions")
            for item in relevant
        )
        if cleanup_required:
            provider_classes = [
                node for node in tree.body if isinstance(node, ast.ClassDef)
            ]
            stored_attrs: set[str] = set()
            context_nodes: list[ast.AST] = []
            for cls in provider_classes:
                init = next((
                    child for child in cls.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == "__init__"
                ), None)
                if init is not None and any(
                    arg.arg == "cleanup_callbacks"
                    for arg in [*init.args.posonlyargs, *init.args.args, *init.args.kwonlyargs]
                ):
                    stored_attrs.update(
                        target.attr
                        for node in ast.walk(init)
                        if isinstance(node, (ast.Assign, ast.AnnAssign))
                        for target in (
                            node.targets if isinstance(node, ast.Assign) else [node.target]
                        )
                        if isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and any(
                            isinstance(value, ast.Name)
                            and value.id == "cleanup_callbacks"
                            for value in ast.walk(node.value)
                        )
                    )
                method = next((
                    child for child in cls.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == "app_context"
                ), None)
                if method is None:
                    continue

                context_nodes.extend(_direct_body_nodes(method))
            used_attrs = {
                node.attr for node in context_nodes
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name) and node.value.id == "self"
            }
            loop_callbacks = {
                node.target.id
                for node in context_nodes
                if isinstance(node, (ast.For, ast.AsyncFor))
                and isinstance(node.target, ast.Name)
                and any(
                    isinstance(part, ast.Attribute) and part.attr in stored_attrs
                    for part in ast.walk(node.iter)
                )
            }
            invokes_cleanup = any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in loop_callbacks
                for node in context_nodes
            )
            stores_callbacks = bool(stored_attrs and stored_attrs & used_attrs)
            if not stores_callbacks or not invokes_cleanup:
                out.append(
                    f"{path}: app_context must store `cleanup_callbacks` supplied by the "
                    "factory and invoke each callback when the context exits. Required "
                    "shape: `__init__(..., cleanup_callbacks=())` stores "
                    "`self._cleanup_callbacks = tuple(cleanup_callbacks)`; a "
                    "`@contextmanager app_context(self)` finally-loop calls each stored "
                    "callback. Do not put callbacks on app_context itself or a nested "
                    "context-manager receiver"
                )

    for decision in (
        item for item in relevant
        if item.get("kind") == "planned_test_surface"
        and item.get("provider") != path and path in item.get("files", [])
    ):
        classes = decision.get("classes", [])
        if len(classes) != 1:
            continue
        class_name = classes[0]["name"]
        provider = decision["provider"]
        refs = {
            alias.asname or alias.name
            for node in tree.body if isinstance(node, ast.ImportFrom)
            and _resolve_module(node.module, node.level, path) in _module_names(provider)
            for alias in node.names if alias.name == class_name
        }
        returned = {
            node.value.id for function in tree.body
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            and function.name == "create_app"
            for node in ast.walk(function)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        }
        constructed = {
            target.id
            for scope in [tree, *(function for function in tree.body
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                and function.name == "create_app")]
            for statement in scope.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id in refs
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
            and (scope is tree or target.id in returned)
        }
        if not refs or not constructed:
            out.append(
                f"{path}: public application must construct planned facade "
                f"{provider}::{class_name}, not a raw FastAPI instance"
            )

    forbidden_attrs = {
        "app_context", "test_client", "test_cli_runner", "container",
        "open_resource", "cli_runner", "instance_path", "testing",
    }
    if any(decision.get("kind") == "test_compatibility" for decision in relevant):
        # These Flask-shaped names are real capabilities of the deterministic facade;
        # conftest may keep using them after wrapping the FastAPI app with adapt_app.
        forbidden_attrs -= {
            "app_context", "test_client", "test_cli_runner", "instance_path",
        }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in forbidden_attrs
            and not valid_owned_receiver(node)
        ):
            expr = ast.unparse(node)
            if node.attr in owned_members:
                out.append(
                    f"{path}:{getattr(node, 'lineno', '?')}: owned capability `{expr}` "
                    "is used on an unverified receiver; construct the frozen owner class "
                    "and use the member on that instance"
                )
                continue
            out.append(
                f"{path}:{getattr(node, 'lineno', '?')}: invented/Flask-only framework "
                f"capability `{expr}` is forbidden by the seam plan"
            )

    # Relative imports and imports under this repository's own package roots must resolve
    # to either an original module or a frozen planned artifact. This catches the exact
    # "useful compatibility-module shape, but file defined nowhere" failure before pytest.
    known_modules = set(seam_plan.get("project_modules", []))
    project_roots = set(seam_plan.get("project_roots", []))
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.ImportFrom):
            resolved = _resolve_module(node.module, node.level, path)
            if resolved:
                modules.append(resolved)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.level and node.module is None:
            resolved = _resolve_module(None, node.level, path)
            for alias in node.names:
                local = alias.asname or alias.name
                if any(
                    isinstance(part, ast.Attribute)
                    and isinstance(part.value, ast.Name)
                    and part.value.id == local
                    for part in ast.walk(tree)
                ):
                    modules.append(f"{resolved}.{alias.name}".lstrip("."))
        for module in modules:
            is_project = bool(
                module.split(".")[0] in project_roots
                or isinstance(node, ast.ImportFrom) and node.level > 0
            )
            if is_project and module not in known_modules:
                out.append(
                    f"{path}:{getattr(node, 'lineno', '?')}: project module `{module}` "
                    "is imported but neither exists in the source tree nor is owned by "
                    "the frozen artifact plan"
                )

    resource_decisions = [
        d for d in relevant
        if d.get("kind") == "resource_lifecycle" and d.get("module") == path
    ]
    ambient_runtime_providers = {
        provider
        for decision in decisions
        if decision.get("kind") == "ambient_context_runtime"
        for provider in decision.get("runtime_providers", [])
    }
    ambient_runtime_modules = {
        module
        for provider in ambient_runtime_providers
        for module in _module_names(provider)
    }
    planned_ambient_g_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and _resolve_module(statement.module, statement.level, path)
        in ambient_runtime_modules
        for alias in statement.names
        if alias.name == "g"
    }
    for decision in (
        item for item in relevant
        if item.get("kind") == "mixed_form_routes" and item.get("path") == path
    ):
        for route in decision.get("routes", []):
            function = module_defs.get(route.get("function", ""))
            if function is None:
                continue
            signature = function.args
            if any(
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name) and node.func.id == "Form"
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "Form"
                )
                for node in ast.walk(signature)
            ):
                out.append(
                    f"{path}:{function.lineno}: mixed GET+POST route "
                    f"`{function.name}` must not require FastAPI Form parameters; "
                    "parse request.form only inside its POST branch"
                )

    for decision in (
        item for item in relevant
        if item.get("kind") == "request_hooks" and item.get("path") == path
    ):
        for hook in decision.get("hooks", []):
            name = hook.get("function", "")
            function = module_defs.get(name)
            if function is None:
                out.append(
                    f"{path}: original pre-request hook `{name}` must remain as real "
                    "FastAPI request wiring"
                )
                continue
            wired = any(
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name) and node.func.id == "Depends"
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "Depends"
                )
                and node.args and isinstance(node.args[0], ast.Name)
                and node.args[0].id == name
                for node in ast.walk(tree)
            )
            if not wired and set(decision.get("files", [])) <= {path}:
                out.append(
                    f"{path}:{function.lineno}: pre-request hook `{name}` is defined but "
                    "not wired through Depends before route handlers"
                )

    for decision in (
        item for item in relevant if item.get("kind") == "ambient_context_runtime"
    ):
        runtime_providers = decision.get("runtime_providers", [])
        test_providers = decision.get("test_providers", [])
        if path in runtime_providers:
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_middleware"
                ):
                    out.append(
                        f"{path}:{node.lineno}: ambient runtime provider must not install "
                        "middleware; the application factory owns ordered installation"
                    )
            if not any(
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name) and node.func.id == "ContextVar"
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "ContextVar"
                )
                for node in ast.walk(tree)
            ):
                out.append(
                    f"{path}: ambient runtime provider must own the shared ContextVar "
                    "state used by request and test-context proxies"
                )
            runtime_classes = set(
                decision.get("runtime_classes", {}).get(path, [])
            ) or {
                pin["symbol"] for pin in (manifest or {}).values()
                if pin.get("module") == path and pin.get("target_kind") == "class"
            }
            for class_name in runtime_classes:
                cls = next((
                    node for node in tree.body
                    if isinstance(node, ast.ClassDef) and node.name == class_name
                ), None)
                if cls is not None and not any(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in {"dispatch", "__call__"}
                    for node in cls.body
                ):
                    out.append(
                        f"{path}:{cls.lineno}: ambient context class `{class_name}` must "
                        "implement request middleware (`dispatch` or `__call__`)"
                    )
                    continue
                middleware = next((
                    node for node in cls.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in {"dispatch", "__call__"}
                ), None) if cls is not None else None
                if middleware is None:
                    continue
                positional = [
                    *middleware.args.posonlyargs, *middleware.args.args,
                ]
                request_name = next((
                    argument.arg for argument in positional
                    if argument.arg != "self"
                ), "")
                request_bound = any(
                    isinstance(node, ast.Dict)
                    and any(
                        isinstance(key, ast.Constant) and key.value == "request"
                        and isinstance(value, ast.Name) and value.id == request_name
                        for key, value in zip(node.keys, node.values, strict=True)
                    )
                    for node in ast.walk(middleware)
                ) or any(
                    isinstance(node, (ast.Assign, ast.AnnAssign))
                    and any(
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "request"
                        for target in (
                            node.targets if isinstance(node, ast.Assign)
                            else [node.target]
                        )
                    )
                    and isinstance(node.value, ast.Name)
                    and node.value.id == request_name
                    for node in ast.walk(middleware)
                )
                if request_name and not request_bound:
                    out.append(
                        f"{path}:{middleware.lineno}: ambient context middleware must "
                        "retain the active request for request-backed compatibility "
                        "providers"
                    )
        if path in test_providers and path not in runtime_providers:
            imported_runtime = any(
                isinstance(node, ast.ImportFrom)
                and any(
                    _resolve_module(node.module, node.level, path) in _module_names(provider)
                    for provider in runtime_providers
                )
                for node in tree.body
            )
            owns_parallel_context = any(
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Name) and node.func.id == "ContextVar"
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "ContextVar"
                )
                for node in ast.walk(tree)
            )
            if not imported_runtime or owns_parallel_context:
                out.append(
                    f"{path}: test-context provider must import/re-export the runtime "
                    "provider's proxies and must not create parallel ContextVars"
                )
        if path in decision.get("factory_files", []):
            runtime_classes = {
                pin["symbol"] for pin in (manifest or {}).values()
                if pin.get("module") in runtime_providers
                and pin.get("target_kind") == "class"
            }
            middleware_calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_middleware" and node.args
            ]
            context_positions = [
                node.lineno for node in middleware_calls
                if isinstance(node.args[0], ast.Name)
                and node.args[0].id in runtime_classes
            ]
            session_positions = [
                node.lineno for node in middleware_calls
                if isinstance(node.args[0], ast.Name)
                and node.args[0].id == "SessionMiddleware"
            ]
            inline_middleware_positions = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "middleware"
                    for decorator in node.decorator_list
                )
            ]
            constructor_orders = []
            for keyword in (
                keyword
                for call in ast.walk(tree) if isinstance(call, ast.Call)
                for keyword in call.keywords
                if keyword.arg == "middleware"
                and isinstance(keyword.value, (ast.List, ast.Tuple))
            ):
                contexts = [
                    index for index, item in enumerate(keyword.value.elts)
                    if isinstance(item, ast.Call) and item.args
                    and isinstance(item.args[0], ast.Name)
                    and item.args[0].id in runtime_classes
                ]
                sessions = [
                    index for index, item in enumerate(keyword.value.elts)
                    if isinstance(item, ast.Call) and item.args
                    and isinstance(item.args[0], ast.Name)
                    and item.args[0].id == "SessionMiddleware"
                ]
                constructor_orders.append((contexts, sessions))
            constructor_context = any(contexts for contexts, _ in constructor_orders)
            wrong_constructor_order = any(
                contexts and sessions and max(sessions) >= min(contexts)
                for contexts, sessions in constructor_orders
            )
            if runtime_classes and not context_positions and not constructor_context:
                out.append(
                    f"{path}: factory must install the frozen ambient-context owner "
                    f"{sorted(runtime_classes)} as request middleware"
                )
            elif (
                context_positions and session_positions
                and max(context_positions) >= min(session_positions)
                or inline_middleware_positions and session_positions
                and max(inline_middleware_positions) >= min(session_positions)
                or wrong_constructor_order
            ):
                out.append(
                    f"{path}: install ambient-context middleware before SessionMiddleware "
                    "when using add_middleware so SessionMiddleware is outermost; with "
                    "a constructor middleware list, put SessionMiddleware first"
                )
            for call in (
                node for node in ast.walk(tree) if isinstance(node, ast.Call)
            ):
                for keyword in call.keywords:
                    lifespan_context = next((
                        node.id for node in ast.walk(keyword.value)
                        if isinstance(node, ast.Name) and node.id in runtime_classes
                    ), None) if keyword.arg == "lifespan" else None
                    if lifespan_context:
                        out.append(
                            f"{path}:{call.lineno}: ambient request-context owner "
                            f"`{lifespan_context}` is middleware, not an "
                            "application lifespan"
                        )

    for decision in resource_decisions:
        function = module_defs.get(decision.get("symbol", ""))
        if function is None:
            continue  # the interface presence gate reports this more precisely
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in {
                "app", "application", "request", "current_app", "g",
            } and node.id not in planned_ambient_g_names:
                out.append(
                    f"{decision['symbol']}:{getattr(node, 'lineno', '?')}: direct resource "
                    f"helper reads `{node.id}`; pinned helpers must use module-owned or "
                    "frozen runtime-owned context and keep their no-context call shape"
                )
        unsupported_g_members = {
            node.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in planned_ambient_g_names
        } - set(decision.get("context_cache_members", []))
        if unsupported_g_members:
            out.append(
                f"{decision['symbol']}:{function.lineno}: frozen `g` may cache only "
                f"source-observed resource members "
                f"{sorted(decision.get('context_cache_members', []))}; move "
                f"{sorted(unsupported_g_members)} to module-owned configuration copied "
                "by the provider initializer"
            )

        if decision.get("context_cache_members"):
            opens_directly = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                for node in ast.walk(function)
            )
            if opens_directly and not any(
                isinstance(node, ast.If) for node in ast.walk(function)
            ):
                out.append(
                    f"{decision['symbol']}:{function.lineno}: original helper cached a "
                    "resource in the active context; an unconditional connect/open on "
                    "every call breaks resource identity"
                )
            context_vars = {
                target.id
                for statement in tree.body
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and isinstance(statement.value, ast.Call)
                and (
                    isinstance(statement.value.func, ast.Name)
                    and statement.value.func.id == "ContextVar"
                    or isinstance(statement.value.func, ast.Attribute)
                    and statement.value.func.attr == "ContextVar"
                )
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            }
            connections = {
                target.id
                for statement in ast.walk(function)
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Attribute)
                and isinstance(statement.value.func.value, ast.Name)
                and statement.value.func.value.id == "sqlite3"
                and statement.value.func.attr == "connect"
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in context_vars
                    and node.args and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in connections
                ):
                    out.append(
                        f"{decision['symbol']}:{node.lineno}: do not store a live "
                        "sqlite3 connection directly in ContextVar; cache it inside a "
                        "mutable context-state mapping so cleanup in a copied request "
                        "context clears the parent-visible entry"
                    )

        initializer_name = decision.get("initializer")
        initializer = module_defs.get(initializer_name) if initializer_name else None
        if initializer_name and initializer is None:
            out.append(
                f"{path}: resource provider must preserve initializer `{initializer_name}`"
            )
        elif initializer is not None:
            argument_names = {
                arg.arg for arg in [*initializer.args.posonlyargs, *initializer.args.args]
            }
            configured = {
                node.slice.value
                for node in ast.walk(initializer)
                if isinstance(node, ast.Subscript)
                and _is_state_config(node.value)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            }
            configured.update(
                node.args[0].value
                for node in ast.walk(initializer)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and _is_state_config(node.func.value)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            )
            if any(
                (
                    isinstance(node, (ast.Assign, ast.AnnAssign))
                    and (
                        _is_state_config(node.value)
                        or isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute)
                        and node.value.func.attr == "copy"
                        and _is_state_config(node.value.func.value)
                    )
                )
                or (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "update"
                    and node.args and _is_state_config(node.args[0])
                )
                for node in ast.walk(initializer)
            ):
                configured.update(decision.get("config_keys", []))
            missing = set(decision.get("config_keys", [])) - configured
            if missing:
                out.append(
                    f"{path}:{initializer.lineno}: `{initializer_name}` must copy "
                    f"configuration keys {sorted(missing)} from app.state.config"
                )
            cleanup_names = set(decision.get("cleanup_functions", []))
            for node in ast.walk(initializer):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append"
                    and any(
                        ast.unparse(node.func.value).startswith(f"{name}.state.")
                        for name in argument_names
                    )
                    and node.args and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in cleanup_names
                ):
                    continue
                out.append(
                    f"{path}:{node.lineno}: provider initializer must not invent "
                    "app.state cleanup storage; the frozen app facade owns this "
                    f"cleanup callback `{node.args[0].id}`"
                )
            for node in ast.walk(initializer):
                if not isinstance(node, ast.Attribute):
                    continue
                parts = _attribute_path(node)
                if len(parts) >= 2 and parts[-2] in argument_names and node.attr in {
                    "cli", "config", "open_resource", "teardown_appcontext",
                }:
                    out.append(
                        f"{path}:{node.lineno}: FastAPI provider initializer cannot use "
                        f"Flask-only `{ast.unparse(node)}`; copy configuration only and "
                        "keep CLI/context wiring separate"
                    )

        for cleanup_name in decision.get("cleanup_functions", []):
            cleanup = module_defs.get(cleanup_name)
            if cleanup is None:
                out.append(
                    f"{path}: context-cached resource must preserve cleanup function "
                    f"`{cleanup_name}`"
                )
            elif not any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "close"
                for node in ast.walk(cleanup)
            ):
                out.append(
                    f"{path}:{cleanup.lineno}: cleanup function `{cleanup_name}` must "
                    "close the cached resource"
                )
            elif decision.get("context_cache_members") and not any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and (
                    node.func.attr == "pop"
                    or node.func.attr in {"set", "reset"} and node.args and (
                        node.func.attr == "reset"
                        or isinstance(node.args[0], ast.Constant)
                        and node.args[0].value is None
                    )
                )
                or isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(node.value, ast.Constant) and node.value.value is None
                or isinstance(node, ast.Delete)
                for node in ast.walk(cleanup)
            ):
                out.append(
                    f"{path}:{cleanup.lineno}: cleanup function `{cleanup_name}` must "
                    "clear the module-owned cached resource after closing it"
                )

        if decision.get("sqlite_cross_thread"):
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "sqlite3" and node.func.attr == "connect"
                ):
                    continue
                setting = next(
                    (keyword.value for keyword in node.keywords
                     if keyword.arg == "check_same_thread"),
                    None,
                )
                if not (
                    isinstance(setting, ast.Constant) and setting.value is False
                ):
                    out.append(
                        f"{path}:{node.lineno}: sqlite3 connection used by FastAPI must "
                        "set check_same_thread=False across dependency/endpoint execution"
                    )

        resource_files = set(decision.get("resource_files", []))
        for with_node in (
            node for node in ast.walk(tree) if isinstance(node, ast.With)
        ):
            for item in with_node.items:
                call = item.context_expr
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "open"
                    and call.args
                    and isinstance(item.optional_vars, ast.Name)
                ):
                    continue
                resource_arg = call.args[0]
                if (
                    isinstance(resource_arg, ast.Constant)
                    and isinstance(resource_arg.value, str)
                    and any(resource_arg.value.endswith(name) for name in resource_files)
                ):
                    out.append(
                        f"{path}:{call.lineno}: package resource path must be resolved "
                        "from __file__ (or importlib.resources), not the process cwd"
                    )
                mode_node = (
                    call.args[1] if len(call.args) > 1 else
                    next((kw.value for kw in call.keywords if kw.arg == "mode"), None)
                )
                binary = (
                    isinstance(mode_node, ast.Constant)
                    and isinstance(mode_node.value, str)
                    and "b" in mode_node.value
                )
                decoded = any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "decode"
                    and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Attribute)
                    and node.func.value.func.attr == "read"
                    and isinstance(node.func.value.func.value, ast.Name)
                    and node.func.value.func.value.id == item.optional_vars.id
                    for statement in with_node.body for node in ast.walk(statement)
                )
                if decoded and not binary:
                    out.append(
                        f"{path}:{call.lineno}: `{item.optional_vars.id}.read().decode(...)` "
                        "requires opening the resource in binary mode"
                    )

    for decision in (
        item for item in relevant
        if item.get("kind") == "resource_lifecycle"
        and item.get("module") != path and item.get("dependency")
    ):
        refs = _imported_callable_refs(
            decision["module"], decision["dependency"],
        )
        for call in (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) in refs
        ):
            out.append(
                f"{path}:{call.lineno}: yield dependency `{decision['dependency']}` "
                "must be passed to Depends without calling it; ordinary code calls "
                f"the pinned direct helper `{decision['symbol']}`"
            )

    session_decisions = [
        decision for decision in relevant if decision.get("kind") == "session_runtime"
    ]
    if session_decisions:
        imported_names: set[str] = set()
        imported_modules: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == (
                "starlette.middleware.sessions"
            ):
                imported_names.update(
                    alias.asname or alias.name
                    for alias in node.names if alias.name == "SessionMiddleware"
                )
            elif isinstance(node, ast.Import):
                imported_modules.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "starlette.middleware.sessions"
                )

        def _session_middleware_ref(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Name) and node.id in imported_names
            ) or (
                isinstance(node, ast.Attribute)
                and node.attr == "SessionMiddleware"
                and ast.unparse(node.value) in imported_modules
            )

        direct_instances = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _session_middleware_ref(node.func)
        ]
        for node in direct_instances:
            out.append(
                f"{path}:{node.lineno}: SessionMiddleware is ASGI middleware, not a "
                "session/proxy object; install the class with app.add_middleware(...) "
                "and expose request-backed session access separately"
            )

        factory_files = {
            factory
            for decision in session_decisions
            for factory in decision.get("factory_files", [])
        }
        if path in factory_files:
            installed = any(
                isinstance(node, ast.Call)
                and bool(node.args) and (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_middleware"
                    and _session_middleware_ref(node.args[0])
                    or ast.unparse(node.func).split(".")[-1] == "Middleware"
                    and _session_middleware_ref(node.args[0])
                )
                for node in ast.walk(tree)
            )
            if not installed:
                out.append(
                    f"{path}: session runtime requires app.add_middleware("
                    "SessionMiddleware, secret_key=...) or an equivalent constructor "
                    "Middleware entry in the application factory"
                )
        provider_files = {
            provider
            for decision in session_decisions
            for provider in decision.get("provider_files", [])
        }
        if path in provider_files:
            has_request_session = any(
                isinstance(node, ast.Attribute) and node.attr == "session"
                for node in ast.walk(tree)
            )
            if not has_request_session:
                out.append(
                    f"{path}: session capability provider must read/mutate the active "
                    "request.session mapping; response cookies are owned by SessionMiddleware"
                )
            original_cookie_writers = {
                writer
                for decision in session_decisions
                for writer in decision.get("original_cookie_writer_files", [])
            }
            if path not in original_cookie_writers:
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"set_cookie", "delete_cookie"}
                    ):
                        out.append(
                            f"{path}:{node.lineno}: session provider must mutate "
                            "request.session, not synthesize raw response cookies"
                        )

    for decision in (
        item for item in relevant if item.get("kind") == "template_runtime"
    ):
        if path not in decision.get("provider_files", []):
            continue
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module in {"starlette.responses", "fastapi.responses"}
            and any(alias.name == "TemplateResponse" for alias in node.names)
            for node in tree.body
        ):
            out.append(
                f"{path}: TemplateResponse is produced by a configured "
                "Jinja2Templates instance; it is not importable from response modules"
            )
        request_args: set[str] = set()
        for function_name in decision.get("provider_functions", {}).get(path, []):
            function = module_defs.get(function_name)
            if function is not None and _shape_facts(function)["required_positional"] > 2:
                out.append(
                    f"{path}:{function.lineno}: shared template helper `{function_name}` "
                    "must keep template context optional; only request and template name "
                    "may be required positional arguments"
                )
            if function is not None:
                positional = [*function.args.posonlyargs, *function.args.args]
                if positional:
                    request_args.add(positional[0].arg)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "TemplateResponse":
                out.append(
                    f"{path}:{node.lineno}: call TemplateResponse through the configured "
                    "Jinja2Templates instance"
                )
                continue
            if not (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "TemplateResponse"
            ):
                continue
            has_request_keyword = any(
                keyword.arg == "request" for keyword in node.keywords
            )
            request_first = bool(
                node.args and isinstance(node.args[0], ast.Name)
                and node.args[0].id in request_args
            )
            if not request_first and not has_request_keyword:
                out.append(
                    f"{path}:{node.lineno}: use request-first "
                    "TemplateResponse(request, name, context); the old two-argument "
                    "form emits a deprecation warning"
                )
    for decision in (d for d in relevant if d.get("kind") == "standalone_cli"):
        commands = decision.get("commands", {})
        command_bindings = decision.get("command_bindings", [])
        router_types = {
            alias.asname or alias.name
            for statement in tree.body
            if isinstance(statement, ast.ImportFrom)
            and statement.module == "fastapi"
            for alias in statement.names if alias.name == "APIRouter"
        }
        routers = {
            target.id
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id in router_types
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }
        for binding in (
            item for item in command_bindings if item.get("module") == path
        ):
            command_function = module_defs.get(binding["function"])
            if command_function is None:
                continue  # interface-presence validation owns the missing-symbol message
            decorator_names = {
                decorator.args[0].value
                for decorator in command_function.decorator_list
                if isinstance(decorator, ast.Call)
                and (
                    isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "command"
                    or isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "command"
                )
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            }
            if binding["name"] not in decorator_names:
                out.append(
                    f"{path}:{command_function.lineno}: `{binding['function']}` must "
                    f"remain the real Click command named `{binding['name']}`"
                )
            called_names = {
                node.func.id for node in ast.walk(command_function)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            missing_handlers = set(binding.get("handlers", [])) - called_names
            if missing_handlers:
                out.append(
                    f"{path}:{command_function.lineno}: Click command must call original "
                    f"module-level handlers {sorted(missing_handlers)} by name so "
                    "monkeypatching remains observable"
                )

        if path in decision.get("factory_files", []):
            forbidden_functions = {
                binding["function"] for binding in command_bindings
            }
            used = {
                node.id for node in ast.walk(tree)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id in forbidden_functions
            }
            if used:
                out.append(
                    f"{path}: application factory must not own/register Click commands "
                    f"{sorted(used)}; pass them only from test compatibility wiring"
                )
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "main"
                    and isinstance(node.value, ast.Name) and node.value.id in commands):
                out.append(
                    f"{path}:{node.lineno}: low-level Click `{node.value.id}.main()` "
                    "raises/exits instead of returning the existing runner Result; use "
                    "click.testing.CliRunner.invoke"
                )
            if (
                isinstance(node, ast.Attribute) and node.attr == "cli"
                and isinstance(node.value, ast.Name)
                and node.value.id in {"app", "application", *routers}
            ):
                out.append(
                    f"{path}:{node.lineno}: FastAPI has no `{ast.unparse(node)}`; keep "
                    "Click commands standalone and wire them only through the test facade"
                )
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id in {"app", "application"}
                    and node.value.attr == "state"
                    and any(word in node.attr.lower() for word in ("command", "runner", "cli"))):
                out.append(
                    f"{path}:{node.lineno}: CLI capability `{ast.unparse(node)}` must not "
                    "be stored on FastAPI state; import/use the real Click command directly"
                )
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "invoke" and node.args
                    and isinstance(node.args[0], ast.Name)):
                continue
            command_name = commands.get(node.args[0].id)
            if not command_name:
                continue
            args_kw = next((kw.value for kw in node.keywords if kw.arg == "args"), None)
            repeats_command = (
                isinstance(args_kw, (ast.List, ast.Tuple)) and bool(args_kw.elts)
                and isinstance(args_kw.elts[0], ast.Constant)
                and args_kw.elts[0].value == command_name
            )
            forwards_unstripped_args = isinstance(args_kw, ast.Name) and args_kw.id == "args"
            if repeats_command or forwards_unstripped_args:
                out.append(
                    f"{path}:{node.lineno}: CliRunner invokes `{node.args[0].id}` but "
                    "forwards the old command token in args; dispatch it, then pass only "
                    "the remaining command arguments"
                )

    if any(decision.get("kind") == "test_compatibility" for decision in relevant):
        compatibility_modules = {
            decision.get("module") for decision in relevant
            if decision.get("kind") == "test_compatibility" and decision.get("module")
        }
        adapter_names = {
            alias.asname or alias.name
            for node in tree.body if isinstance(node, ast.ImportFrom)
            and node.module in compatibility_modules
            for alias in node.names if alias.name == "adapt_app"
        }
        if not adapter_names:
            out.append(
                f"{path}: compatibility wiring must import adapt_app from the "
                "deterministic Portage facade"
            )

        def _call_name(call: ast.Call) -> str:
            return ast.unparse(call.func)

        facade_attrs = {"app_context", "test_client", "test_cli_runner"}
        adapter_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) in adapter_names
        ]
        for function in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            raw_apps: set[str] = set()
            for statement in function.body:
                for node in ast.walk(statement):
                    if (isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id in raw_apps
                            and node.attr in facade_attrs):
                        out.append(
                            f"{path}:{node.lineno}: `{node.value.id}` is still the raw "
                            f"FastAPI factory result when `{node.attr}` is used; call "
                            "adapt_app immediately after the factory returns"
                        )
                    if (isinstance(node, (ast.Return, ast.Yield))
                            and isinstance(node.value, ast.Name)
                            and node.value.id in raw_apps):
                        out.append(
                            f"{path}:{node.lineno}: raw FastAPI app `{node.value.id}` "
                            "escapes the fixture; yield/return the adapted app"
                        )
                if not (isinstance(statement, (ast.Assign, ast.AnnAssign))):
                    continue
                targets = (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                names = {target.id for target in targets if isinstance(target, ast.Name)}
                value = statement.value
                if not isinstance(value, ast.Call):
                    continue
                called = _call_name(value)
                if called == "create_app" or called.endswith(".create_app"):
                    raw_apps.update(names)
                elif called in adapter_names:
                    raw_apps.difference_update(names)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and ast.unparse(node.func) in {"CliRunner", "click.testing.CliRunner"}):
                out.append(
                    f"{path}:{node.lineno}: raw Click CliRunner bypasses the facade's "
                    "Flask-compatible command dispatch; use adapted_app.test_cli_runner()"
                )
        if any(decision.get("kind") == "standalone_cli" for decision in relevant):
            command_values = [
                keyword.value
                for call in adapter_calls for keyword in call.keywords
                if keyword.arg == "commands"
            ]
            if not command_values:
                out.append(
                    f"{path}: adapt_app must receive the real exported Click commands "
                    "so test_cli_runner().invoke(args=[command, ...]) can dispatch them"
                )
            assignments = {
                target.id: node.value
                for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if isinstance(target, ast.Name)
            }

            def _command_mapping(value: ast.AST) -> dict[str, str]:
                if isinstance(value, ast.Name) and value.id in assignments:
                    value = assignments[value.id]
                if not isinstance(value, ast.Dict):
                    return {}
                return {
                    key.value: ast.unparse(item)
                    for key, item in zip(value.keys, value.values, strict=True)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }

            mappings = [_command_mapping(value) for value in command_values]
            for cli_decision in (
                item for item in relevant if item.get("kind") == "standalone_cli"
            ):
                for binding in cli_decision.get("command_bindings", []):
                    refs = _imported_callable_refs(
                        binding["module"], binding["function"],
                    )
                    if not any(
                        mapping.get(binding["name"]) in refs for mapping in mappings
                    ):
                        out.append(
                            f"{path}: adapt_app commands must map `{binding['name']}` to "
                            f"the real export {binding['module']}::{binding['function']}"
                        )
    return list(dict.fromkeys(out))


def _is_type_checking_test(test: ast.expr) -> bool:
    """`if TYPE_CHECKING:` / `if typing.TYPE_CHECKING:` — a guard that never runs at
    import time, so defs inside its body are NOT runtime-importable and must stay
    flagged as missing (F2 note)."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _module_level_stmts(tree: ast.Module):
    """Yield statements that are effectively "module level" for definition-presence
    purposes: `tree.body` plus, recursively, the bodies of module-level `if`/`try`
    compound statements (a common `try/except ImportError:` compat shim or
    `if sys.version_info ...:` conditional def is still importable at runtime — F2).

    `if TYPE_CHECKING:` bodies are skipped (never execute at import time) — everything
    else about a compound statement's branches is included so a def in either arm of an
    `if/else` counts. Deliberately NOT `ast.walk`, which would also descend into
    function/class bodies and wrongly treat local names as module-level defs."""

    def _walk(stmts: list[ast.stmt]):
        for node in stmts:
            yield node
            if isinstance(node, ast.If):
                if not _is_type_checking_test(node.test):
                    yield from _walk(node.body)
                yield from _walk(node.orelse)
            elif isinstance(node, ast.Try):
                yield from _walk(node.body)
                for handler in node.handlers:
                    yield from _walk(handler.body)
                yield from _walk(node.orelse)
                yield from _walk(node.finalbody)

    yield from _walk(tree.body)


def contract_violations(
    content: str, manifest: dict[str, dict], path: str, seam_plan: dict | None = None,
) -> list[str]:
    """Export-presence + pinned-shape check for one generated file (R1). Presence: every
    DEFINES symbol exists at module level (def/class/assign/import re-export), including
    inside module-level `if`/`try` conditional blocks (compat shims — F2), but NOT inside
    `if TYPE_CHECKING:`. Shape (only when the pin kept the original shape, using the
    manifest's machine-readable `shape` facts — NEVER re-parsed from prose/signature
    strings, v2 finding #3): required positional args must not grow, no new required
    keyword-only args, and async/generator-ness must not flip. Deliberately NOT full
    caller-compatibility analysis; Verify owns that."""
    pins = [p for p in manifest.values() if p["module"] == path]
    caller_violations = caller_contract_violations(content, manifest, path)
    capability_violations = planned_capability_consumer_violations(
        content, manifest, path,
    )
    provider_import_violations = planned_provider_import_violations(
        content, manifest, path,
    )
    seam_violations = framework_seam_violations(
        content, seam_plan or {}, path, manifest,
    )
    topology_violations = planned_artifact_topology_violations(
        content, manifest, path,
    )
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        return [f"{path} unparseable ({exc.msg} line {exc.lineno}) — generated file "
                f"cannot satisfy its interface contract"]
    if not pins:
        return [
            *caller_violations, *capability_violations, *provider_import_violations,
            *seam_violations,
            *topology_violations,
        ]
    defined: dict[str, ast.stmt] = {}
    for node in _module_level_stmts(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined[t.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined[node.target.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                defined[(a.asname or a.name).split(".")[0]] = node
    context_vars = {
        target.id
        for node in _module_level_stmts(tree) if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func).split(".")[-1] == "ContextVar"
        for target in node.targets if isinstance(target, ast.Name)
    }

    out: list[str] = []
    for p in pins:
        name = p["symbol"]
        node = defined.get(name)
        if node is None:
            out.append(f"{name}: missing — other files import it; it must be defined "
                       f"at module level (TARGET: {p['target_note']})")
            continue

        expected_kind = p.get("target_kind")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            actual_kind = "function"
        elif isinstance(node, ast.ClassDef):
            actual_kind = "class"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            actual_kind = "variable"
        else:
            actual_kind = None  # an import re-export's underlying kind is unknowable here
        if expected_kind and actual_kind and actual_kind != expected_kind:
            out.append(f"{name}: target kind changed ({expected_kind} -> {actual_kind})")
        if (
            expected_kind == "variable"
            and set(p.get("capabilities", [])) & {
                "request_context", "session_and_flash", "test_context_surface",
            }
            and isinstance(node, (ast.Assign, ast.AnnAssign))
        ):
            value = node.value
            local_function_alias = (
                isinstance(value, ast.Name)
                and isinstance(
                    defined.get(value.id), (ast.FunctionDef, ast.AsyncFunctionDef),
                )
            )
            if value is None or local_function_alias or isinstance(
                value, (ast.Constant, ast.Dict, ast.List, ast.Set, ast.Tuple, ast.Lambda),
            ) or (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get"
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id in context_vars
            ):
                out.append(
                    f"{name}: test-context export must be a runtime-backed proxy, "
                    "not a literal, function alias, process-global container, or "
                    "one-time ContextVar snapshot"
                )

        required_members = p.get("members", [])
        if required_members:
            if not isinstance(node, ast.ClassDef):
                out.append(
                    f"{name}: planned capability members {required_members} require a class"
                )
            else:
                methods = {
                    child.name: child for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                class_members = {*methods, *(
                    child.name for child in node.body if isinstance(child, ast.ClassDef)
                )}
                assigned_members = {
                    target.id
                    for child in node.body if isinstance(child, ast.Assign)
                    for target in child.targets if isinstance(target, ast.Name)
                }
                assigned_members.update(
                    child.target.id for child in node.body
                    if isinstance(child, ast.AnnAssign)
                    and isinstance(child.target, ast.Name)
                )
                class_members.update(assigned_members)
                init = next((
                    child for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == "__init__"
                ), None)
                if init is not None:
                    positional = [*init.args.posonlyargs, *init.args.args]
                    receiver = positional[0].arg if positional else "self"
                    for child in ast.walk(init):
                        targets: list[ast.expr] = []
                        if isinstance(child, ast.Assign):
                            targets = child.targets
                        elif isinstance(child, ast.AnnAssign):
                            targets = [child.target]
                        initialized = {
                            target.attr for target in targets
                            if isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == receiver
                        }
                        assigned_members.update(initialized)
                        class_members.update(initialized)
                missing_members = set(required_members) - class_members
                if missing_members:
                    out.append(
                        f"{name}: missing planned capability members "
                        f"{sorted(missing_members)}"
                    )
                properties = {
                    member for member, definition in methods.items()
                    if any(
                        isinstance(decorator, ast.Name) and decorator.id == "property"
                        or isinstance(decorator, ast.Attribute)
                        and decorator.attr == "property"
                        for decorator in definition.decorator_list
                    )
                }
                for member, shape in p.get("member_shapes", {}).items():
                    if member not in class_members:
                        continue
                    if shape in {"method", "context_manager"} and (
                        member not in methods or member in properties
                    ):
                        out.append(
                            f"{name}.{member}: consumer calls this member; implement it "
                            "as a method, not an attribute or property"
                        )
                    elif shape == "context_manager" and member in methods:
                        method = methods[member]
                        yields = any(
                            isinstance(node, (ast.Yield, ast.YieldFrom))
                            for node in ast.walk(method)
                        )
                        decorated = any(
                            isinstance(decorator, ast.Name)
                            and decorator.id in {"contextmanager", "asynccontextmanager"}
                            or isinstance(decorator, ast.Attribute)
                            and decorator.attr in {"contextmanager", "asynccontextmanager"}
                            for decorator in method.decorator_list
                        )
                        if yields and not decorated:
                            out.append(
                                f"{name}.{member}: consumer enters this result with `with`; "
                                "a generator implementation must use @contextmanager "
                                "(or return a real context-manager object)"
                            )
                    elif shape == "attribute" and (
                        member in methods and member not in properties
                        and member not in assigned_members
                    ):
                        out.append(
                            f"{name}.{member}: consumer reads this member without calling "
                            "it; implement it as an attribute or property, not a method"
                        )

        for extra in p.get("additional_exports", []):
            if extra not in defined:
                out.append(f"{extra}: missing required companion export for {name}")

        orig = p.get("shape") or {}
        if (p.get("preserve_shape", p["target_note"] == "keep the original shape") and orig
                and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            new = _shape_facts(node)  # import from .common — same fact extractor
            if new["required_positional"] > orig["required_positional"]:
                out.append(f"{name}: required arg count grew "
                           f"({orig['required_positional']} -> "
                           f"{new['required_positional']}) but the pin says keep the "
                           f"original shape — callers break")
            added_kw = set(new["required_keyword_only"]) - set(orig["required_keyword_only"])
            if added_kw:
                out.append(f"{name}: new required keyword-only args {sorted(added_kw)} "
                           f"but the pin says keep the original shape")
            if new["is_async"] != orig["is_async"]:
                out.append(f"{name}: async-ness changed but the pin says keep the "
                           f"original shape")
            if new["is_generator"] != orig["is_generator"]:
                out.append(f"{name}: generator-ness changed (was "
                           f"{'generator' if orig['is_generator'] else 'plain'}) but "
                           f"the pin says keep the original shape")
            if (
                orig.get("returns_nested_function")
                and not new["returns_nested_function"]
            ):
                out.append(
                    f"{name}: no longer returns a locally defined wrapper/decorator "
                    "function but the pin says keep the original shape"
                )
    return [
        *out, *caller_violations, *capability_violations,
        *provider_import_violations, *seam_violations, *topology_violations,
    ]


def all_generation_violations(
    content: str, manifest: dict[str, dict], path: str, seam_plan: dict | None = None,
    oracle_entry: dict | None = None,
) -> list[str]:
    out = contract_violations(content, manifest, path, seam_plan)
    out.extend(_undefined_global_violations(content, path))
    if oracle_entry is not None:
        out.extend(f"oracle: {item}" for item in oracle_violations(oracle_entry, content))
    return out


def _validated_deterministic_artifacts(
    planned: dict[str, PlannedFile], renderer, worktree: str,
    manifest: dict[str, dict], seam_plan: dict, oracle_manifest: dict,
    normalizer=None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Accept valid recipe renders; make renderer defects ordinary task evidence."""
    rendered: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    if renderer is None:
        return rendered, rejected
    for path, planned_file in planned.items():
        if planned_file.action != "create":
            continue
        try:
            content = renderer(planned_file, worktree)
        except Exception as exc:
            rejected[path] = [
                f"{type(exc).__name__}: {scrub(str(exc))[:1000]}"
            ]
            continue
        if content is None:
            continue
        if normalizer is not None:
            content = normalizer(path, content, seam_plan)
        broken = all_generation_violations(
            content, manifest, path, seam_plan, oracle_manifest.get(path),
        )
        if broken:
            rejected[path] = broken
        else:
            rendered[path] = content
    return rendered, rejected


def _undefined_global_violations(content: str, path: str) -> list[str]:
    """Catch names that would deterministically raise at import or call time."""
    try:
        tree = ast.parse(content)
        table = symtable.symtable(content, path, "exec")
    except SyntaxError:
        return []
    if any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
        for node in tree.body
    ):
        return []
    bound = {
        symbol.get_name() for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }
    tables = [table]
    for current in tables:
        tables.extend(current.get_children())
        bound.update(
            symbol.get_name() for symbol in current.get_symbols()
            if symbol.is_global() and symbol.is_assigned()
        )
    allowed = {
        *dir(builtins), "__annotations__", "__builtins__", "__file__", "__loader__",
        "__name__", "__package__", "__spec__",
    }
    undefined = sorted({
        symbol.get_name()
        for current in tables
        for symbol in current.get_symbols()
        if symbol.is_referenced() and symbol.is_global()
        and symbol.get_name() not in bound | allowed
    })
    return [
        f"{path}: undefined global name `{name}`; import or define it"
        for name in undefined
    ]


def _pick_draft(content1: str, broken1: list[str], content2: str,
                manifest: dict[str, dict], path: str,
                seam_plan: dict | None = None,
                oracle_entry: dict | None = None) -> tuple[str, list[str]]:
    """Choose between the original draft (`content1`, already known to violate `broken1`)
    and the repair draft (`content2`) produced from that feedback.

    F1/F1b fix: violation COUNTS are not comparable across the parse boundary —
    `contract_violations` collapses ANY unparseable file (prose, a refusal,
    truncated/unfenced output) to a single-item violation list, which under a raw count
    comparison made a garbage repair look "better" than a valid multi-violation draft
    (F1) and an unparseable draft 1 look "better" than a parseable repair carrying >=2
    real violations (F1b). So parseability dominates, symmetrically: an unparseable
    repair never beats a parseable draft 1; a parseable repair beats an unparseable
    draft 1 UNCONDITIONALLY, whatever its violation count (a shape violation might
    still pass Verify; an unparseable file crashes it, guaranteed); both unparseable →
    keep draft 1 (nothing gained by swapping). Both parseable → fewer violations wins;
    a tie goes to the repair (it saw the feedback)."""
    def _parses(src: str) -> bool:
        try:
            ast.parse(src)
        except SyntaxError:
            return False
        return True

    if not _parses(content2):
        return content1, broken1
    broken2 = all_generation_violations(
        content2, manifest, path, seam_plan, oracle_entry,
    )
    if not _parses(content1):
        return content2, broken2
    if len(broken2) <= len(broken1):
        return content2, broken2
    return content1, broken1


async def _migrate_file(recipe, worktree: str, *, path: str, role: str, model: str,
                        subtasks: list[Subtask], context: dict[str, str],
                        verify_errors: str, prior_attempt: str = "",
                        manifest: dict | None = None,
                        consumed: set[str] | None = None,
                        seam_plan: dict | None = None,
                        planned_file: PlannedFile | None = None,
                        source_override: str | None = None) -> tuple[str, dict]:
    """Call the model for one file; return (migrated content, usage) — content not yet
    written. Usage feeds the attempts_log entry (cost-per-migration is an eval metric)."""
    source = scrub(
        source_override
        if source_override is not None
        else read_file(worktree, path, limit=20000) or ""
    )
    planned = planned_file or PlannedFile(path=path, role=role, subtasks=subtasks)
    user = recipe.build_user_prompt(file=planned, source=source, context=context)
    # R1: the frozen target-interface manifest, stated explicitly as DEFINES/CALLS.
    # Cross-file naming/shape breaks are a measured top failure mode (corpus finding
    # #2) — don't leave the interface for the model to infer from context files.
    user += contract_sections(manifest or {}, path, consumed)
    user += seam_sections(seam_plan or {}, path)
    if prior_attempt:
        user += (
            "\n\nYOUR PREVIOUS ATTEMPT at this file FAILED verification and was rolled "
            "back. Its diff is below — debug it: keep what was right, fix what the test "
            f"failure shows is wrong.\n{prior_attempt}"
        )
    if verify_errors:
        if role == "test_harness":
            user += (
                "\n\nA previous attempt produced these test failures. Fix only this "
                "test file's framework plumbing while preserving every assertion and its "
                f"meaning:\n{verify_errors[:2500]}"
            )
        else:
            user += (
                "\n\nA previous attempt produced these test failures — fix the migration so "
                f"they pass (do not change behavioural tests):\n{verify_errors[:2500]}"
            )
    messages = [
        LLMMessage(role="system", content=recipe.system_prompt()),
        LLMMessage(role="user", content=user),
    ]
    resp = await get_llm().complete(messages, model=model)
    usage = {
        "prompt_tokens": resp.prompt_tokens,
        "completion_tokens": resp.completion_tokens,
        "cost_usd": round(resp.cost_usd, 6),
    }
    content = extract_code(resp.text)
    if normalizer := getattr(recipe, "normalize_generated", None):
        content = normalizer(path, content, seam_plan)
    return content, usage


async def _migrate_cluster(
    recipe, *, planned_files: list[PlannedFile], sources: dict[str, str],
    context: dict[str, str], model: str, manifest: dict, seam_plan: dict,
    binding_root: str, verify_errors: str = "",
) -> tuple[dict[str, str], dict]:
    """Generate one small framework seam in one LLM call and parse every member."""
    builder = getattr(recipe, "build_cluster_prompt", None)
    if builder is None:
        raise TypeError(f"recipe {recipe.name} has no cluster prompt builder")
    user = builder(files=planned_files, sources=sources, context=context)
    for file in planned_files:
        consumed = _consumed_manifest_keys(binding_root, file.path, manifest)
        user += f"\n\n### Frozen decisions for {file.path}"
        user += contract_sections(manifest, file.path, consumed)
        user += seam_sections(seam_plan, file.path)
    if verify_errors:
        user += (
            "\n\nThe previous coordinated draft failed these exact checks. Return every "
            "file again in the required marker format and fix the violations without "
            f"changing unrelated behaviour:\n{verify_errors[:16000]}"
        )
    system = recipe.system_prompt() + (
        "\n\nCOUPLED UNIT OVERRIDE: when the user explicitly requests a coupled migration "
        "unit, the one-file output rule is replaced by the user's exact PORTAGE_FILE marker "
        "format. Return every requested file exactly once and no prose."
    )
    resp = await get_llm().complete([
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ], model=model)
    usage = {
        "prompt_tokens": resp.prompt_tokens,
        "completion_tokens": resp.completion_tokens,
        "cost_usd": round(resp.cost_usd, 6),
    }
    try:
        contents = extract_cluster_files(
            resp.text, [file.path for file in planned_files],
        )
    except ValueError as exc:
        raise ClusterOutputError(str(exc), usage) from exc
    if normalizer := getattr(recipe, "normalize_generated", None):
        contents = {
            path: normalizer(path, content, seam_plan)
            for path, content in contents.items()
        }
    return contents, usage


def _cluster_violations(
    contents: dict[str, str], manifest: dict[str, dict], seam_plan: dict,
    oracle_manifest: dict,
) -> dict[str, list[str]]:
    broken = {
        path: broken for path, content in contents.items()
        if (broken := all_generation_violations(
            content, manifest, path, seam_plan, oracle_manifest.get(path),
        ))
    }
    trees = {}
    for path, content in contents.items():
        try:
            trees[path] = ast.parse(content)
        except SyntaxError:
            trees[path] = None
    for decision in seam_plan.get("decisions", {}).values():
        if decision.get("kind") != "request_hooks":
            continue
        files = set(decision.get("files", []))
        if not files or not files <= set(contents):
            continue
        for hook in decision.get("hooks", []):
            name = hook.get("function", "")
            wired = any(
                tree is not None and any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, (ast.Name, ast.Attribute))
                    and (
                        isinstance(node.func, ast.Name) and node.func.id == "Depends"
                        or isinstance(node.func, ast.Attribute)
                        and node.func.attr == "Depends"
                    )
                    and node.args and ast.unparse(node.args[0]).split(".")[-1] == name
                    for node in ast.walk(tree)
                )
                for tree in trees.values()
            )
            if not wired:
                owner = decision.get("path", sorted(files)[0])
                broken.setdefault(owner, []).append(
                    f"{owner}: pre-request hook `{name}` is not wired through Depends "
                    "in its frozen provider/consumer cut"
                )
                continue
            if hook.get("scope") != "before_app_request":
                continue
            globally_wired = any(
                candidate_path != decision.get("path")
                and tree is not None
                and any(
                    isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and isinstance(statement.value, ast.Call)
                    and any(
                        keyword.arg == "dependencies"
                        and any(
                            isinstance(call, ast.Call)
                            and isinstance(call.func, (ast.Name, ast.Attribute))
                            and (
                                isinstance(call.func, ast.Name)
                                and call.func.id == "Depends"
                                or isinstance(call.func, ast.Attribute)
                                and call.func.attr == "Depends"
                            )
                            and call.args
                            and ast.unparse(call.args[0]).split(".")[-1] == name
                            for call in ast.walk(keyword.value)
                        )
                        for keyword in statement.value.keywords
                    )
                    for statement in ast.walk(tree)
                )
                for candidate_path, tree in trees.items() if candidate_path in files
            )
            if not globally_wired:
                owner = decision.get("path", sorted(files)[0])
                broken.setdefault(owner, []).append(
                    f"{owner}: before_app_request hook `{name}` must be a global "
                    "application dependency, not scoped to one included router"
                )
    return broken


def _choose_cluster_draft(
    first: dict[str, str], first_broken: dict[str, list[str]],
    repair: dict[str, str], manifest: dict[str, dict], seam_plan: dict,
    oracle_manifest: dict,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """All parsed members or nothing; fewer aggregate violations wins, repair on tie."""
    repair_broken = _cluster_violations(repair, manifest, seam_plan, oracle_manifest)
    if sum(map(len, repair_broken.values())) <= sum(map(len, first_broken.values())):
        return repair, repair_broken
    return first, first_broken


def select_execution_batch(file_tasks: list, seam_units: list[dict]) -> list[str]:
    """Choose the next durable verification batch.

    A coupled seam is atomic. Otherwise the next dependency-ordered pending file is one
    batch. This deliberately derives scheduling from persisted task status on every visit,
    so crash resume and recovery cannot diverge from the database truth.
    """
    pending = [
        task for task in file_tasks
        if task.target_path and task.status == TaskStatus.pending.value
    ]
    if not pending:
        return []
    by_path = {task.target_path: task for task in file_tasks if task.target_path}
    candidates = [
        unit for unit in seam_units
        if unit.get("paths")
        and any(path in {task.target_path for task in pending} for path in unit["paths"])
        and all(path in by_path for path in unit["paths"])
        and all(by_path[path].status != TaskStatus.skipped.value for path in unit["paths"])
    ]
    if candidates:
        unit = min(
            candidates,
            key=lambda item: min(by_path[path].order_index for path in item["paths"]),
        )
        if min(by_path[path].order_index for path in unit["paths"]) <= pending[0].order_index:
            return list(unit["paths"])
    return [pending[0].target_path]


def expand_to_verifiable_batch(
    file_tasks: list, seam_units: list[dict], initial_paths: list[str],
) -> list[str]:
    """Accumulate atomic work until the batch owns at least one concrete test target.

    Static blast radius is best-effort. An empty target set does *not* mean "run the full
    suite against a knowingly mixed Flask/FastAPI intermediate state"; it means keep
    moving through dependency order until a testable boundary is reached. If the graph
    found no targets anywhere, the conservative result is one full migration batch and
    Verify then runs the full suite.
    """
    by_path = {task.target_path: task for task in file_tasks if task.target_path}
    if any(by_path[path].type == "test_compat" for path in initial_paths):
        return initial_paths
    selected = set(initial_paths)
    pending = [
        task for task in file_tasks
        if task.target_path and task.status == TaskStatus.pending.value
    ]

    def has_tests() -> bool:
        return any(
            by_path[path].verify_spec.get("affected_tests", []) for path in selected
        )

    while not has_tests():
        task = next((item for item in pending if item.target_path not in selected), None)
        if task is None:
            break
        unit = next(
            (item for item in seam_units if task.target_path in item.get("paths", [])),
            None,
        )
        selected.update(unit["paths"] if unit else [task.target_path])
    return [
        task.target_path for task in file_tasks
        if task.target_path in selected
    ]


def runtime_contract_repair_attempt(task, target: str) -> int | None:
    """Return the next separate repair ordinal when this task is the unique target."""
    if not target or task.target_path != target:
        return None
    return 1 + sum(
        entry.get("action") == "contract_repair"
        and entry.get("scope") == "runtime_targeted"
        for entry in task.attempts_log
    )


def is_initial_cluster(paths: list[str], tasks_by_path: dict) -> bool:
    """Only untouched tasks are safe to generate as one coordinated draft."""
    return all(tasks_by_path[path].attempts == 0 for path in paths)


async def _execute_initial_cluster(
    *, recipe, unit: dict, tasks_by_path: dict, original_planned: dict[str, PlannedFile],
    worktree: str, binding_root: str, target_paths: set[str], done_paths: set[str],
    manifest: dict, seam_plan: dict, fault: str | None, first_path: str | None,
    delay: int, verify_errors: str, oracle_manifest: dict,
) -> float:
    """Generate an untouched seam in one coordinated call."""
    paths = unit["paths"]
    members = [tasks_by_path[path] for path in paths]
    planned_files = [original_planned[path] for path in paths]
    attempt = max(task.attempts for task in members) + 1
    tier, model, model_label = _tier_for(attempt)
    coordinator = members[0]
    for index, task in enumerate(members):
        await task_store.update_task(
            task.id, status=TaskStatus.running.value, attempts=attempt,
            cascade_subtasks=True,
            append_attempt={
                "attempt": attempt,
                "tier": tier,
                "model": model_label,
                "action": "migrate" if index == 0 else "cluster_member",
                "cluster_id": unit["id"],
                "cluster_paths": paths,
                "at": _now(),
            },
        )
    if delay:
        await asyncio.sleep(delay)

    try:
        sources = {
            path: scrub(read_file(worktree, path, limit=20000) or "") for path in paths
        }
        context = _gather_cluster_context(
            worktree, cluster_paths=set(paths), target_paths=target_paths,
            done_paths=done_paths,
        )
        if not any(file.role == "test_harness" for file in planned_files):
            context.pop(getattr(recipe, "test_compat_path", ""), None)
        prior_diffs = [
            entry.get("failing_diff", "")
            for task in members
            for entry in reversed(task.attempts_log)
            if entry.get("action") == "rollback_regenerate" and entry.get("failing_diff")
        ]
        feedback = verify_errors
        if prior_diffs:
            feedback += (
                "\n\nPREVIOUS REJECTED MEMBER DIFFS — coordinate the correction:\n"
                + "\n\n".join(prior_diffs[:len(members)])
            )
        try:
            contents, usage = await _migrate_cluster(
                recipe, planned_files=planned_files, sources=sources, context=context,
                model=model, manifest=manifest, seam_plan=seam_plan,
                binding_root=binding_root, verify_errors=feedback,
            )
            call_cost = usage.get("cost_usd", 0.0)
        except ClusterOutputError as exc:
            usage = exc.usage
            call_cost = usage.get("cost_usd", 0.0)
            await task_store.update_task(coordinator.id, amend_last_attempt=usage)
            await task_store.update_task(
                coordinator.id,
                append_attempt={
                    "attempt": attempt,
                    "tier": tier,
                    "model": model_label,
                    "action": "format_repair",
                    "cluster_id": unit["id"],
                    "violations": [str(exc)],
                    "at": _now(),
                },
            )
            contents, usage = await _migrate_cluster(
                recipe, planned_files=planned_files, sources=sources, context=context,
                model=model, manifest=manifest, seam_plan=seam_plan,
                binding_root=binding_root,
                verify_errors=(
                    feedback + "\n\nINVALID OUTPUT FORMAT: " + str(exc)
                    + "\nReturn every requested file exactly once."
                ),
            )
            call_cost += usage.get("cost_usd", 0.0)
        broken = _cluster_violations(contents, manifest, seam_plan, oracle_manifest)
        if broken:
            await task_store.update_task(coordinator.id, amend_last_attempt=usage)
            flattened = [
                f"{path}: {violation}"
                for path, violations in broken.items() for violation in violations
            ]
            await task_store.update_task(
                coordinator.id,
                append_attempt={
                    "attempt": attempt,
                    "tier": tier,
                    "model": model_label,
                    "action": "contract_repair",
                    "cluster_id": unit["id"],
                    "violations": flattened[:20],
                    "at": _now(),
                },
            )
            rejected = "\n\n".join(
                f"<<<PORTAGE_FILE:{path}>>>\n```python\n{content[:12000]}\n```\n"
                "<<<PORTAGE_END_FILE>>>"
                for path, content in contents.items()
            )
            repair_errors = (
                "VIOLATIONS:\n- " + "\n- ".join(flattened)
                + "\n\nREJECTED COORDINATED DRAFT:\n" + rejected
            )
            try:
                repaired, usage2 = await _migrate_cluster(
                    recipe, planned_files=planned_files, sources=sources, context=context,
                    model=model, manifest=manifest, seam_plan=seam_plan,
                    binding_root=binding_root, verify_errors=repair_errors,
                )
                contents, remaining = _choose_cluster_draft(
                    contents, broken, repaired, manifest, seam_plan, oracle_manifest,
                )
            except ValueError:
                log.warning("cluster repair output invalid for %s — keeping first draft",
                            unit["id"])
                usage2 = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
                remaining = broken
            await task_store.update_task(coordinator.id, amend_last_attempt=usage2)
            call_cost += usage2.get("cost_usd", 0.0)
            if remaining:
                flattened = [
                    f"{path}: {violation}"
                    for path, violations in remaining.items()
                    for violation in violations
                ]
                await task_store.update_task(
                    coordinator.id,
                    append_attempt={
                        "attempt": attempt,
                        "repair_attempt": 2,
                        "tier": tier,
                        "model": model_label,
                        "action": "contract_repair",
                        "cluster_id": unit["id"],
                        "violations": flattened[:20],
                        "at": _now(),
                    },
                )
                rejected = "\n\n".join(
                    f"<<<PORTAGE_FILE:{path}>>>\n```python\n{content[:12000]}\n```\n"
                    "<<<PORTAGE_END_FILE>>>"
                    for path, content in contents.items()
                )
                try:
                    if len(remaining) == 1:
                        repair_path = next(iter(remaining))
                        planned_file = next(
                            file for file in planned_files if file.path == repair_path
                        )
                        repaired_content, usage3 = await _migrate_file(
                            recipe, binding_root, path=repair_path,
                            role=planned_file.role, model=model,
                            subtasks=planned_file.subtasks,
                            context={
                                **context,
                                **{
                                    path: content for path, content in contents.items()
                                    if path != repair_path
                                },
                            },
                            verify_errors=(
                                "SECOND AND FINAL CONTRACT REPAIR. VIOLATIONS:\n- "
                                + "\n- ".join(flattened)
                            ),
                            manifest=manifest,
                            consumed=_consumed_manifest_keys(
                                binding_root, repair_path, manifest,
                            ),
                            seam_plan=seam_plan,
                            planned_file=planned_file,
                            source_override=contents[repair_path],
                        )
                        repaired = {**contents, repair_path: repaired_content}
                    else:
                        repaired, usage3 = await _migrate_cluster(
                            recipe, planned_files=planned_files, sources=sources,
                            context=context, model=model, manifest=manifest,
                            seam_plan=seam_plan, binding_root=binding_root,
                            verify_errors=(
                                "SECOND AND FINAL CONTRACT REPAIR. VIOLATIONS:\n- "
                                + "\n- ".join(flattened)
                                + "\n\nCURRENT REJECTED DRAFT:\n" + rejected
                            ),
                        )
                    contents, _ = _choose_cluster_draft(
                        contents, remaining, repaired, manifest, seam_plan,
                        oracle_manifest,
                    )
                except ValueError:
                    log.warning(
                        "second cluster repair output invalid for %s — keeping best draft",
                        unit["id"],
                    )
                    usage3 = {
                        "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                    }
                await task_store.update_task(
                    coordinator.id, amend_last_attempt=usage3,
                )
                call_cost += usage3.get("cost_usd", 0.0)
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

        final_broken = _cluster_violations(
            contents, manifest, seam_plan, oracle_manifest,
        )
        if final_broken:
            message = "; ".join(
                violation
                for violations in final_broken.values()
                for violation in violations
            )
            for task in members:
                await task_store.update_task(
                    task.id, status=TaskStatus.skipped.value,
                    cascade_subtasks=True,
                    error=f"persistent contract violation: {message[:2000]}",
                    append_attempt={
                        "attempt": attempt,
                        "tier": tier,
                        "model": model_label,
                        "action": "contract_rejected",
                        "violations": final_broken.get(task.target_path, [])[:10],
                        "at": _now(),
                    },
                )
            return call_cost

        if _should_corrupt(fault, path=paths[0], first_path=first_path,
                           attempt=attempt, tier=tier):
            contents[paths[0]] += _FAULT_PAYLOAD
        for index, (task, path, planned_file) in enumerate(zip(
            members, paths, planned_files, strict=True,
        )):
            h = write_file(worktree, path, contents[path])
            if planned_file.action == "create":
                await run_git("add", "-N", "--", path, cwd=worktree)
            diff = await file_diff(worktree, path)
            await task_store.update_task(
                task.id, status=TaskStatus.done.value, content_hash=h, diff=diff,
                cascade_subtasks=True,
                amend_last_attempt=(usage if index == 0 and any(usage.values()) else None),
            )
        log.info("  migrated coupled unit %s | paths=%s model=%s", unit["id"], paths,
                 model_label)
        return call_cost
    except Exception as exc:
        for task in members:
            await task_store.update_task(
                task.id, status=TaskStatus.failed.value, error=repr(exc),
                cascade_subtasks=True,
            )
        raise


async def execute_node(state: GraphState) -> GraphState:
    job_id = state["job_id"]
    if not state.get("migrate"):
        log.info("EXECUTE node | job=%s no migration tasks -> skip", job_id)
        return {"step_log": ["execute"]}

    worktree = state["worktree"]
    recipe = get_recipe(state["migration_recipe"])
    verify_errors = state.get("last_verify_errors") or ""
    cfg = state.get("config") or {}
    fault = cfg.get("inject_fault")
    delay = int(cfg.get("execute_delay_seconds", settings.execute_task_delay_seconds))

    tasks = await task_store.load_tasks(uuid.UUID(job_id))
    file_tasks = [t for t in tasks if t.target_path]
    target_paths = {t.target_path for t in file_tasks}
    strategies = state.get("test_strategy") or {}
    first_path = next((
        task.target_path for task in file_tasks
        if task.type != "test_compat"
        and strategies.get(task.target_path) not in NON_REWRITE_TEST_STRATEGIES
    ), None)
    log.info("EXECUTE node | job=%s tasks=%s pending=%s fault=%s", job_id, len(file_tasks),
             sum(t.status == TaskStatus.pending.value for t in file_tasks), fault or "-")

    # Rev-C demo protection: a retry/escalation spiral that crosses the per-job cost
    # ceiling stops migrating and skips the remaining tasks — the run finishes with an
    # honest red report instead of draining the demo's LLM quota.
    ceiling = settings.job_cost_ceiling_usd
    spent = sum(a.get("cost_usd", 0.0) for t in tasks for a in t.attempts_log)

    # R1: the frozen target-interface manifest built at Plan time.
    manifest = state.get("interface_manifest") or {}
    seam_plan = state.get("seam_plan") or {}
    oracle_manifest = state.get("oracle_manifest") or {}
    test_strategy = strategies
    test_normalizations = state.get("test_normalizations") or {}
    unsupported_test_seams = list(state.get("unsupported_test_seams") or [])
    # Persisted subtasks intentionally store stable ids/titles only. Rehydrate their full
    # recipe instructions from the ORIGINAL workspace plan; reconstructing them with an
    # empty instruction silently discarded the recipe's most valuable guidance.
    original_files = iter_py_files(state.get("workspace") or worktree)
    original_planned = {pf.path: pf for pf in recipe.plan_files(original_files)}
    original_planned.update({
        pf.path: pf for pf in artifact_planned_files(state.get("artifact_plan") or [])
    })
    artifact_renderer = getattr(recipe, "render_created_artifact", None)
    deterministic_artifacts, deterministic_rejections = (
        _validated_deterministic_artifacts(
            original_planned, artifact_renderer, worktree, manifest, seam_plan,
            oracle_manifest, getattr(recipe, "normalize_generated", None),
        )
    )
    compat_module = (state.get("test_compat_path") or "_portage_fastapi_test_compat.py")
    compat_module = compat_module.removesuffix(".py").replace("/", ".")
    for path, strategy in test_strategy.items():
        if strategy != "adapter_wiring" or path not in original_planned:
            continue
        original = original_planned[path]
        original_planned[path] = PlannedFile(
            path=path,
            role=original.role,
            order=original.order,
            subtasks=[Subtask(
                "test_adapter_wiring",
                "Wire the deterministic test compatibility facade",
                (
                    "Preserve every fixture name and behavioral test unchanged. Import "
                    f"adapt_app from {compat_module}. Immediately after the "
                    "real FastAPI factory returns, rebind its result with adapt_app before "
                    "using app_context/test_client/test_cli_runner or yielding it. Pass a "
                    "commands mapping of real exported Click commands and the original "
                    "instance_path when those capabilities are consumed. Do not construct "
                    "TestClient or CliRunner directly and do not rewrite assertions."
                ),
            )],
        )
    binding_root = state.get("workspace") or worktree
    tasks_by_path = {t.target_path: t for t in file_tasks if t.target_path}
    for path, errors in deterministic_rejections.items():
        task = tasks_by_path.get(path)
        if task is None or any(
            attempt.get("action") == "deterministic_artifact_rejected"
            and attempt.get("errors") == errors
            for attempt in task.attempts_log
        ):
            continue
        await task_store.update_task(
            task.id,
            append_attempt={
                "tier": "deterministic", "model": "none",
                "action": "deterministic_artifact_rejected", "errors": errors[:10],
                "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                "at": _now(),
            },
        )
        log.warning("  deterministic renderer declined %s: %s", path, errors[:3])
    contract_repair_owner = state.get("contract_repair_owner") or ""
    active_units = {
        unit["paths"][0]: unit
        for unit in seam_plan.get("units", [])
        if unit.get("paths")
        and getattr(recipe, "build_cluster_prompt", None) is not None
        and all(path in tasks_by_path and path in original_planned for path in unit["paths"])
        and any(tasks_by_path[path].status == TaskStatus.pending.value
                for path in unit["paths"])
        and all(tasks_by_path[path].status != TaskStatus.skipped.value
                for path in unit["paths"])
        and not any(path in deterministic_artifacts for path in unit["paths"])
        and contract_repair_owner not in unit["paths"]
        and is_initial_cluster(unit["paths"], tasks_by_path)
    }
    units = list(active_units.values())
    # Executable cuts own verification boundaries. Small cuts normally overlap one
    # coordinated generation unit; large cuts deliberately remain sequential generation
    # inside one batch until bounded component recovery lands. Retain any legacy recipe
    # unit not covered by the analyzer for other recipes/backward-compatible checkpoints.
    schedule_units = list(seam_plan.get("execution_cuts", []))
    covered = [set(unit.get("paths", [])) for unit in schedule_units]
    for unit in units:
        paths = set(unit.get("paths", []))
        if paths and not any(paths & existing for existing in covered):
            schedule_units.append(unit)
            covered.append(paths)
    batch_paths = expand_to_verifiable_batch(
        file_tasks, schedule_units, select_execution_batch(file_tasks, schedule_units),
    )
    batch_path_set = set(batch_paths)
    batch_tests = sorted({
        test
        for task in file_tasks if task.target_path in batch_path_set
        for test in task.verify_spec.get("affected_tests", [])
    })
    checkpoint = state.get("current_batch_checkpoint") or load_cut_checkpoint(worktree)
    if batch_paths and not (
        checkpoint and set(batch_paths) <= set(checkpoint.get("paths", []))
    ):
        checkpoint = create_cut_checkpoint(
            worktree,
            batch_paths,
            {
                path: {
                    "status": tasks_by_path[path].status,
                    "action": tasks_by_path[path].verify_spec.get("action", "rewrite"),
                }
                for path in batch_paths
            },
        )
    log.info("EXECUTE batch | job=%s paths=%s tests=%s", job_id, batch_paths, batch_tests)
    if state.get("diagnostic_repair_requested") and batch_paths:
        coordinator = tasks_by_path[batch_paths[0]]
        _, diagnostic_model, diagnostic_label = (
            "escalation", settings.llm_escalation_model,
            settings.llm_escalation_model_label or settings.llm_escalation_model,
        )
        prior_diffs = "\n\n".join(
            attempt.get("failing_diff", "")[:4000]
            for path in batch_paths
            for attempt in reversed(tasks_by_path[path].attempts_log)
            if attempt.get("action") == "rollback_regenerate"
            and attempt.get("failing_diff")
        )
        await task_store.update_task(
            coordinator.id,
            append_attempt={
                "attempt": coordinator.attempts, "tier": "escalation",
                "model": diagnostic_label, "action": "diagnose", "at": _now(),
            },
        )
        try:
            response = await get_llm().complete([
                LLMMessage(
                    role="system",
                    content=(
                        "Diagnose a repeated software-migration failure. Identify the "
                        "specific root cause and the smallest general correction. Do not "
                        "write code and do not weaken or change tests."
                    ),
                ),
                LLMMessage(
                    role="user",
                    content=(
                        f"Batch: {batch_paths}\n\nFailure:\n{verify_errors[:6000]}"
                        f"\n\nRejected diff:\n{prior_diffs[:8000]}"
                    ),
                ),
            ], model=diagnostic_model)
            diagnostic_usage = {
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "cost_usd": round(response.cost_usd, 6),
            }
            await task_store.update_task(
                coordinator.id, amend_last_attempt=diagnostic_usage,
            )
            spent += diagnostic_usage["cost_usd"]
            verify_errors += "\n\nESCALATION DIAGNOSIS:\n" + scrub(response.text[:5000])
        except Exception:
            log.exception("diagnostic call failed for batch %s; continuing", batch_paths)

    # A checkpoint resume starts a fresh Python invocation. Reconstruct the completed
    # context set from the durable task hashes so later files see already-migrated
    # siblings without re-running their model calls.
    done_paths: set[str] = {
        t.target_path for t in file_tasks
        if t.target_path and t.status == TaskStatus.done.value
        and t.content_hash == content_hash(read_file(worktree, t.target_path) or "")
    }
    cluster_completed: set[str] = set()
    for t in file_tasks:
        path = t.target_path
        assert path is not None
        if path not in batch_path_set:
            continue
        if path in cluster_completed:
            continue
        if t.status == TaskStatus.skipped.value:
            continue
        if ceiling > 0 and spent >= ceiling and t.status != TaskStatus.done.value:
            log.warning("  cost ceiling $%.2f reached (spent $%.4f) — skipping %s",
                        ceiling, spent, path)
            await task_store.update_task(
                t.id, status=TaskStatus.skipped.value, cascade_subtasks=True,
                error=f"job cost ceiling ${ceiling:.2f} reached",
                append_attempt={"action": "cost_ceiling_skip", "at": _now()},
            )
            continue
        if unit := active_units.get(path):
            call_cost = await _execute_initial_cluster(
                recipe=recipe, unit=unit, tasks_by_path=tasks_by_path,
                original_planned=original_planned, worktree=worktree,
                binding_root=binding_root, target_paths=target_paths,
                done_paths=done_paths, manifest=manifest, seam_plan=seam_plan,
                fault=fault, first_path=first_path, delay=delay,
                verify_errors=verify_errors,
                oracle_manifest=oracle_manifest,
            )
            spent += call_cost
            done_paths.update(unit["paths"])
            cluster_completed.update(unit["paths"])
            continue
        current = read_file(worktree, path, limit=20000) or ""
        # Idempotent skip (resume): already migrated and unchanged on disk.
        if t.status == TaskStatus.done.value and t.content_hash == content_hash(current):
            log.info("  skip %s — already migrated (content-hash match)", path)
            done_paths.add(path)
            continue

        # The compatibility module is recipe-owned deterministic infrastructure, not
        # application code for an LLM to improvise. Refuse to overwrite a pre-existing
        # user module unless it carries Portage's ownership marker.
        if t.type == "test_compat":
            marker = "# Generated by Portage."
            if current and not current.startswith(marker):
                detail = {
                    "path": path,
                    "reason": "compatibility module path collides with a user-owned file",
                }
                if detail not in unsupported_test_seams:
                    unsupported_test_seams.append(detail)
                await task_store.update_task(
                    t.id, status=TaskStatus.skipped.value, cascade_subtasks=True,
                    error=detail["reason"],
                    append_attempt={"action": "unsupported_test_seam", "at": _now()},
                )
                continue
            renderer = getattr(recipe, "render_test_compat", None)
            if renderer is None:
                raise TypeError(f"recipe {recipe.name} cannot render test compatibility")
            generated = renderer()
            attempt = t.attempts + 1
            await task_store.update_task(
                t.id, status=TaskStatus.running.value, attempts=attempt,
                cascade_subtasks=True,
                append_attempt={
                    "attempt": attempt, "tier": "deterministic", "model": "none",
                    "action": "deterministic_generate", "at": _now(),
                },
            )
            h = write_file(worktree, path, generated)
            # Intent-to-add makes a new generated file visible to `git diff`, while
            # leaving the user's index contents untouched.
            await run_git("add", "-N", "--", path, cwd=worktree)
            diff = await file_diff(worktree, path)
            await task_store.update_task(
                t.id, status=TaskStatus.done.value, content_hash=h, diff=diff,
                cascade_subtasks=True,
            )
            done_paths.add(path)
            log.info("  generated deterministic test adapter %s", path)
            continue

        if path in deterministic_artifacts:
            generated = deterministic_artifacts[path]
            attempt = t.attempts + 1
            await task_store.update_task(
                t.id, status=TaskStatus.running.value, attempts=attempt,
                cascade_subtasks=True,
                append_attempt={
                    "attempt": attempt, "tier": "deterministic", "model": "none",
                    "action": "deterministic_artifact", "at": _now(),
                },
            )
            if _should_corrupt(
                fault, path=path, first_path=first_path, attempt=attempt,
                tier="deterministic",
            ):
                generated += _FAULT_PAYLOAD
            h = write_file(worktree, path, generated)
            await run_git("add", "-N", "--", path, cwd=worktree)
            diff = await file_diff(worktree, path)
            await task_store.update_task(
                t.id, status=TaskStatus.done.value, content_hash=h, diff=diff,
                cascade_subtasks=True,
            )
            done_paths.add(path)
            log.info("  generated deterministic artifact %s", path)
            continue

        strategy = test_strategy.get(path)
        if t.type == "test_harness" and strategy in {"adapter", "unchanged"}:
            # These tests are the behavioral oracle. The generated adapter plus the
            # conftest wiring absorbs the framework API difference; direct test files
            # remain byte-for-byte identical.
            attempt = t.attempts + 1
            await task_store.update_task(
                t.id, status=TaskStatus.done.value, attempts=attempt,
                content_hash=content_hash(current), diff=await file_diff(worktree, path),
                cascade_subtasks=True,
                append_attempt={
                    "attempt": attempt, "tier": "deterministic", "model": "none",
                    "action": "adapter_preserve", "at": _now(),
                },
            )
            done_paths.add(path)
            log.info("  preserved oracle test %s (strategy=%s)", path, strategy)
            continue

        if t.type == "test_harness" and strategy == "sanctioned_normalization":
            normalization = test_normalizations.get(path)
            if not normalization:
                raise ValueError(f"missing frozen sanctioned normalization for {path}")
            attempt = t.attempts + 1
            audit = [
                {
                    "line": replacement["line"],
                    "symbols": replacement.get("symbols", []),
                    "target_module": normalization["target_module"],
                }
                for replacement in normalization.get("replacements", [])
            ]
            await task_store.update_task(
                t.id, status=TaskStatus.running.value, attempts=attempt,
                cascade_subtasks=True,
                append_attempt={
                    "attempt": attempt,
                    "tier": "deterministic",
                    "model": "none",
                    "action": "sanctioned_normalization",
                    "normalizations": audit,
                    "at": _now(),
                },
            )
            generated = apply_sanctioned_normalizations(
                current, normalization.get("replacements", []),
            )
            h = write_file(worktree, path, generated)
            diff = await file_diff(worktree, path)
            await task_store.update_task(
                t.id, status=TaskStatus.done.value, content_hash=h, diff=diff,
                cascade_subtasks=True,
            )
            done_paths.add(path)
            log.info("  applied sanctioned test normalization %s: %s", path, audit)
            continue

        if t.type == "test_harness" and strategy == "unsupported_test_seam":
            detail = {"path": path, "reason": "unsupported Flask test seam"}
            if not any(seam.get("path") == path for seam in unsupported_test_seams):
                unsupported_test_seams.append(detail)
            await task_store.update_task(
                t.id, status=TaskStatus.skipped.value, cascade_subtasks=True,
                error=detail["reason"],
                append_attempt={"action": "unsupported_test_seam", "at": _now()},
            )
            continue

        repair_attempt = runtime_contract_repair_attempt(t, contract_repair_owner)
        if repair_attempt is not None:
            # Model escalation still reflects all preceding generation work, but the
            # ordinary task-attempt counter is intentionally not incremented. Recover
            # owns a separate, bounded allowance for this uniquely attributed repair.
            attempt = t.attempts
            tier, model, model_label = _tier_for(t.attempts + repair_attempt)
            await task_store.update_task(
                t.id, status=TaskStatus.running.value, cascade_subtasks=True,
                append_attempt={
                    "attempt": attempt,
                    "repair_attempt": repair_attempt,
                    "tier": tier,
                    "model": model_label,
                    "action": "contract_repair",
                    "scope": "runtime_targeted",
                    "at": _now(),
                },
            )
        else:
            attempt = t.attempts + 1
            tier, model, model_label = _tier_for(attempt)
            await task_store.update_task(
                t.id, status=TaskStatus.running.value, attempts=attempt,
                cascade_subtasks=True,
                append_attempt={"attempt": attempt, "tier": tier, "model": model_label,
                                "action": "migrate", "at": _now()},
            )
        if delay:
            log.info("  EXECUTE pre-migrate delay %ss (kill window) | job=%s task=%s",
                     delay, job_id, path)
            await asyncio.sleep(delay)

        try:
            context = _gather_context(worktree, current=path, target_paths=target_paths,
                                      done_paths=done_paths)
            if t.type != "test_harness":
                context.pop(getattr(recipe, "test_compat_path", ""), None)
            planned_file = original_planned.get(path)
            subtasks = (planned_file.subtasks if planned_file else
                        [Subtask(s.type, s.title, "") for s in t.subtasks])
            prior = next(
                (a.get("failing_diff", "") for a in reversed(t.attempts_log)
                 if a.get("action") == "rollback_regenerate" and a.get("failing_diff")),
                "",
            )
            consumed = _consumed_manifest_keys(binding_root, path, manifest)
            content, usage = await _migrate_file(
                recipe, worktree, path=path, role=t.type, model=model,
                subtasks=subtasks, context=context, verify_errors=verify_errors,
                prior_attempt=prior, manifest=manifest, consumed=consumed,
                seam_plan=seam_plan, planned_file=planned_file,
            )
            call_cost = usage.get("cost_usd", 0.0)
            broken = all_generation_violations(
                content, manifest, path, seam_plan, oracle_manifest.get(path),
            )
            if broken:
                # First-class accounting: close out the migrate attempt's usage FIRST,
                # then log the repair as its own entry with its own usage — llm_calls
                # and cost stay per-call truthful (v1 review finding #5).
                await task_store.update_task(t.id, amend_last_attempt=usage)
                log.warning("  contract violation in %s: %s — one repair call",
                            path, "; ".join(broken)[:300])
                await task_store.update_task(
                    t.id, append_attempt={"attempt": attempt, "tier": tier,
                                          "model": model_label,
                                          "action": "contract_repair", "at": _now(),
                                          "violations": broken[:10]},
                )
                repair_note = (
                    "\n\nYOUR REJECTED DRAFT (below) VIOLATED THE INTERFACE DECISIONS:\n- "
                    + "\n- ".join(broken)
                    + "\n\nRejected draft:\n```python\n" + content[:12000] + "\n```\n"
                    "Return the corrected COMPLETE file; fix exactly these violations, "
                    "change nothing else."
                )
                content2, usage2 = await _migrate_file(
                    recipe, worktree, path=path, role=t.type, model=model,
                    subtasks=subtasks, context=context,
                    verify_errors=verify_errors + repair_note, prior_attempt=prior,
                    manifest=manifest, consumed=consumed, seam_plan=seam_plan,
                    planned_file=planned_file,
                )
                await task_store.update_task(t.id, amend_last_attempt=usage2)
                call_cost += usage2.get("cost_usd", 0.0)
                # Keep the better draft: the repair may only win if it's syntactically
                # valid; among parseable candidates fewer violations wins, tie -> the
                # repair (it saw the feedback). v1 kept draft 1 on persistent violation —
                # wrong; an unparseable repair unconditionally winning on count was F1.
                content, remaining = _pick_draft(
                    content, broken, content2, manifest, path, seam_plan,
                    oracle_manifest.get(path),
                )
                if remaining:
                    await task_store.update_task(
                        t.id,
                        append_attempt={
                            "attempt": attempt,
                            "repair_attempt": 2,
                            "tier": tier,
                            "model": model_label,
                            "action": "contract_repair",
                            "at": _now(),
                            "violations": remaining[:10],
                        },
                    )
                    repair_note = (
                        "\n\nSECOND AND FINAL CONTRACT REPAIR. THE CURRENT DRAFT STILL "
                        "VIOLATES:\n- " + "\n- ".join(remaining)
                        + "\n\nCurrent rejected draft:\n```python\n"
                        + content[:12000] + "\n```\nReturn the corrected COMPLETE file; "
                        "fix these violations and change nothing else."
                    )
                    content3, usage3 = await _migrate_file(
                        recipe, worktree, path=path, role=t.type, model=model,
                        subtasks=subtasks, context=context,
                        verify_errors=verify_errors + repair_note,
                        prior_attempt=prior, manifest=manifest, consumed=consumed,
                        seam_plan=seam_plan, planned_file=planned_file,
                    )
                    await task_store.update_task(t.id, amend_last_attempt=usage3)
                    call_cost += usage3.get("cost_usd", 0.0)
                    content, _ = _pick_draft(
                        content, remaining, content3, manifest, path, seam_plan,
                        oracle_manifest.get(path),
                    )
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
                # both calls' usage is already amended onto their own log entries; the
                # done-update below must not double-amend (zeroed usage = no-op amend)
            final_broken = all_generation_violations(
                content, manifest, path, seam_plan, oracle_manifest.get(path),
            )
            if final_broken:
                await task_store.update_task(
                    t.id, status=TaskStatus.skipped.value,
                    cascade_subtasks=True,
                    error="persistent contract violation: " + "; ".join(final_broken),
                    append_attempt={
                        "attempt": attempt,
                        "tier": tier,
                        "model": model_label,
                        "action": "contract_rejected",
                        "violations": final_broken[:10],
                        "at": _now(),
                    },
                )
                spent += call_cost
                continue
            if _should_corrupt(fault, path=path, first_path=first_path,
                               attempt=attempt, tier=tier):
                log.warning("  FAULT %s | corrupting %s (attempt=%s tier=%s)",
                            fault, path, attempt, tier)
                content += _FAULT_PAYLOAD
            h = write_file(worktree, path, content)
            if planned_file and planned_file.action == "create":
                await run_git("add", "-N", "--", path, cwd=worktree)
            diff = await file_diff(worktree, path)
            await task_store.update_task(
                t.id, status=TaskStatus.done.value, content_hash=h, diff=diff,
                cascade_subtasks=True,
                amend_last_attempt=usage if any(usage.values()) else None,
            )
            spent += call_cost
            done_paths.add(path)
            log.info("  migrated %s (attempt=%s tier=%s model=%s, %s chars)",
                     path, attempt, tier, model_label, len(content))
        except Exception as exc:
            log.exception("  migrate FAILED for %s", path)
            await task_store.update_task(t.id, status=TaskStatus.failed.value, error=repr(exc))
            raise

    diff = await worktree_diff(worktree)
    snapshots = await task_store.load_tasks(uuid.UUID(job_id))
    restored_rejected_cut = await _restore_rejected_batch(
        worktree, batch_paths, checkpoint, snapshots,
    )
    if restored_rejected_cut:
        snapshots = await task_store.load_tasks(uuid.UUID(job_id))
        checkpoint = {}
        diff = await worktree_diff(worktree)
    has_pending_tasks = any(
        snapshot.status == TaskStatus.pending.value for snapshot in snapshots
    )
    batch_oracle_results = []
    for path in batch_paths:
        if entry := oracle_manifest.get(path):
            generated = read_file(worktree, path) or ""
            batch_oracle_results.append({
                "path": path,
                "strategy": test_strategy.get(path, "unchanged"),
                "violations": oracle_violations(entry, generated),
                "unsupported": any(
                    seam.get("path") == path for seam in unsupported_test_seams
                ),
            })
    return {
        "plan": [s.to_state_dict() for s in snapshots],
        "diff": diff,
        "last_verify_errors": "",  # consumed
        "diagnostic_repair_requested": False,
        "contract_repair_owner": "",
        "unsupported_test_seams": unsupported_test_seams,
        "oracle_results": batch_oracle_results,
        "current_batch_paths": batch_paths,
        "current_batch_tests": batch_tests,
        "current_batch_checkpoint": checkpoint,
        "cut_restore_pending_verification": bool(restored_rejected_cut),
        "migration_tree_state": (
            "restored_coherent" if restored_rejected_cut else
            state.get("migration_tree_state", "migrated")
        ),
        "recovery_actions": ([{
            "classification": "generation_contract_rejection",
            "action": "restore_rejected_cut_reverify",
            "targets": restored_rejected_cut,
            "at": _now(),
        }] if restored_rejected_cut else []),
        "has_pending_tasks": has_pending_tasks,
        "step_log": ["execute"],
    }
