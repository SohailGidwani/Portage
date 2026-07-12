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
import logging
import re
import uuid
from datetime import UTC, datetime

from portage_agent.config import settings
from portage_agent.core.interfaces import LLMMessage
from portage_agent.db import task_store
from portage_agent.db.models import TaskStatus
from portage_agent.llm import get_llm
from portage_agent.recipes import get_recipe
from portage_agent.recipes.base import PlannedFile, Subtask

from ..state import GraphState
from .common import (
    _module_names,
    _resolve_module,
    _shape_facts,
    content_hash,
    extract_code,
    file_diff,
    imported_bindings,
    iter_py_files,
    non_python_listing,
    read_file,
    run_git,
    worktree_diff,
    write_file,
)
from .oracle import oracle_violations
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
        for binding in imported_bindings(root, pin["module"]):
            if binding.importer == path and (
                binding.symbol == pin["symbol"] or binding.symbol is None
            ):
                consumed.add(key)
                break
    return consumed


def _fmt_pin(p: dict) -> str:
    line = f"  - {p['symbol']}  (was: {p['original']}"
    if p.get("notes"):
        line += f"; {p['notes']}"
    line += f")\n      TARGET: {p['target_note']}"
    for s in p.get("call_sites", []):
        line += f"\n      current call site: {s}"
    if p.get("additional_exports"):
        line += "\n      REQUIRED ADDITIONAL EXPORTS: " + ", ".join(p["additional_exports"])
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


def framework_seam_violations(content: str, seam_plan: dict, path: str) -> list[str]:
    """Reject framework capabilities the deterministic seam plan says do not exist."""
    relevant = [
        d for d in seam_plan.get("decisions", {}).values()
        if path in d.get("files", [])
    ]
    if not relevant:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    out: list[str] = []
    forbidden_attrs = {
        "app_context", "test_client", "test_cli_runner", "container",
        "open_resource", "cli_runner", "instance_path",
    }
    if any(decision.get("kind") == "test_compatibility" for decision in relevant):
        # These Flask-shaped names are real capabilities of the deterministic facade;
        # conftest may keep using them after wrapping the FastAPI app with adapt_app.
        forbidden_attrs -= {
            "app_context", "test_client", "test_cli_runner", "instance_path",
        }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
            expr = ast.unparse(node)
            out.append(
                f"{path}:{getattr(node, 'lineno', '?')}: invented/Flask-only framework "
                f"capability `{expr}` is forbidden by the seam plan"
            )

    resource_decisions = [
        d for d in relevant
        if d.get("kind") == "resource_lifecycle" and d.get("module") == path
    ]
    module_defs = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for decision in resource_decisions:
        function = module_defs.get(decision.get("symbol", ""))
        if function is None:
            continue  # the interface presence gate reports this more precisely
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in {
                "app", "application", "request", "current_app", "g",
            }:
                out.append(
                    f"{decision['symbol']}:{getattr(node, 'lineno', '?')}: direct resource "
                    f"helper reads `{node.id}`; pinned helpers must use module-owned "
                    "configuration and keep their no-context call shape"
                )
    for decision in (d for d in relevant if d.get("kind") == "standalone_cli"):
        commands = decision.get("commands", {})
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == "main"
                    and isinstance(node.value, ast.Name) and node.value.id in commands):
                out.append(
                    f"{path}:{node.lineno}: low-level Click `{node.value.id}.main()` "
                    "raises/exits instead of returning the existing runner Result; use "
                    "click.testing.CliRunner.invoke"
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
            has_commands = any(
                any(keyword.arg == "commands" for keyword in call.keywords)
                for call in adapter_calls
            )
            if not has_commands:
                out.append(
                    f"{path}: adapt_app must receive the real exported Click commands "
                    "so test_cli_runner().invoke(args=[command, ...]) can dispatch them"
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
    seam_violations = framework_seam_violations(content, seam_plan or {}, path)
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        return [f"{path} unparseable ({exc.msg} line {exc.lineno}) — generated file "
                f"cannot satisfy its interface contract"]
    if not pins:
        return [*caller_violations, *seam_violations]
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
    return [*out, *caller_violations, *seam_violations]


def all_generation_violations(
    content: str, manifest: dict[str, dict], path: str, seam_plan: dict | None = None,
    oracle_entry: dict | None = None,
) -> list[str]:
    out = contract_violations(content, manifest, path, seam_plan)
    if oracle_entry is not None:
        out.extend(f"oracle: {item}" for item in oracle_violations(oracle_entry, content))
    return out


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
                        seam_plan: dict | None = None) -> tuple[str, dict]:
    """Call the model for one file; return (migrated content, usage) — content not yet
    written. Usage feeds the attempts_log entry (cost-per-migration is an eval metric)."""
    source = scrub(read_file(worktree, path, limit=20000) or "")
    planned = PlannedFile(path=path, role=role, subtasks=subtasks)
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
    return extract_code(resp.text), usage


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
    return extract_cluster_files(resp.text, [file.path for file in planned_files]), usage


def _cluster_violations(
    contents: dict[str, str], manifest: dict[str, dict], seam_plan: dict,
    oracle_manifest: dict,
) -> dict[str, list[str]]:
    return {
        path: broken for path, content in contents.items()
        if (broken := all_generation_violations(
            content, manifest, path, seam_plan, oracle_manifest.get(path),
        ))
    }


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


async def _execute_initial_cluster(
    *, recipe, unit: dict, tasks_by_path: dict, original_planned: dict[str, PlannedFile],
    worktree: str, binding_root: str, target_paths: set[str], done_paths: set[str],
    manifest: dict, seam_plan: dict, fault: str | None, first_path: str | None,
    delay: int, verify_errors: str, oracle_manifest: dict,
) -> float:
    """Coordinated generation for a seam; retries keep every active member coherent."""
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
        contents, usage = await _migrate_cluster(
            recipe, planned_files=planned_files, sources=sources, context=context,
            model=model, manifest=manifest, seam_plan=seam_plan,
            binding_root=binding_root, verify_errors=feedback,
        )
        call_cost = usage.get("cost_usd", 0.0)
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
                contents, _ = _choose_cluster_draft(
                    contents, broken, repaired, manifest, seam_plan, oracle_manifest,
                )
            except ValueError:
                log.warning("cluster repair output invalid for %s — keeping first draft",
                            unit["id"])
                usage2 = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
            await task_store.update_task(coordinator.id, amend_last_attempt=usage2)
            call_cost += usage2.get("cost_usd", 0.0)
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}

        if _should_corrupt(fault, path=paths[0], first_path=first_path,
                           attempt=attempt, tier=tier):
            contents[paths[0]] += _FAULT_PAYLOAD
        for index, (task, path) in enumerate(zip(members, paths, strict=True)):
            h = write_file(worktree, path, contents[path])
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
        and strategies.get(task.target_path) not in {"adapter", "unchanged"}
    ), None)
    log.info("EXECUTE node | job=%s tasks=%s pending=%s fault=%s", job_id, len(file_tasks),
             sum(t.status == TaskStatus.pending.value for t in file_tasks), fault or "-")

    # Rev-C demo protection: a retry/escalation spiral that crosses the per-job cost
    # ceiling stops migrating and skips the remaining tasks — the run finishes with an
    # honest red report instead of draining the demo's LLM quota.
    ceiling = settings.job_cost_ceiling_usd
    spent = sum(
        a.get("cost_usd", 0.0) for t in file_tasks for a in t.attempts_log
    )

    # R1: the frozen target-interface manifest built at Plan time.
    manifest = state.get("interface_manifest") or {}
    seam_plan = state.get("seam_plan") or {}
    oracle_manifest = state.get("oracle_manifest") or {}
    test_strategy = strategies
    unsupported_test_seams = list(state.get("unsupported_test_seams") or [])
    # Persisted subtasks intentionally store stable ids/titles only. Rehydrate their full
    # recipe instructions from the ORIGINAL workspace plan; reconstructing them with an
    # empty instruction silently discarded the recipe's most valuable guidance.
    original_files = iter_py_files(state.get("workspace") or worktree)
    original_planned = {pf.path: pf for pf in recipe.plan_files(original_files)}
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
        and (
            max(tasks_by_path[path].attempts for path in unit["paths"])
            < settings.max_task_attempts
            or state.get("recover_source") == "integrate"
        )
    }
    units = list(active_units.values())
    batch_paths = expand_to_verifiable_batch(
        file_tasks, units, select_execution_batch(file_tasks, units),
    )
    batch_path_set = set(batch_paths)
    batch_tests = sorted({
        test
        for task in file_tasks if task.target_path in batch_path_set
        for test in task.verify_spec.get("affected_tests", [])
    })
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

        if t.type == "test_harness" and strategy == "unsupported_test_seam":
            detail = {"path": path, "reason": "unsupported Flask test seam"}
            if detail not in unsupported_test_seams:
                unsupported_test_seams.append(detail)
            await task_store.update_task(
                t.id, status=TaskStatus.skipped.value, cascade_subtasks=True,
                error=detail["reason"],
                append_attempt={"action": "unsupported_test_seam", "at": _now()},
            )
            continue

        attempt = t.attempts + 1
        tier, model, model_label = _tier_for(attempt)
        await task_store.update_task(
            t.id, status=TaskStatus.running.value, attempts=attempt, cascade_subtasks=True,
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
                seam_plan=seam_plan,
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
                )
                await task_store.update_task(t.id, amend_last_attempt=usage2)
                call_cost += usage2.get("cost_usd", 0.0)
                # Keep the better draft: the repair may only win if it's syntactically
                # valid; among parseable candidates fewer violations wins, tie -> the
                # repair (it saw the feedback). v1 kept draft 1 on persistent violation —
                # wrong; an unparseable repair unconditionally winning on count was F1.
                content, _ = _pick_draft(
                    content, broken, content2, manifest, path, seam_plan,
                    oracle_manifest.get(path),
                )
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
                # both calls' usage is already amended onto their own log entries; the
                # done-update below must not double-amend (zeroed usage = no-op amend)
            if _should_corrupt(fault, path=path, first_path=first_path,
                               attempt=attempt, tier=tier):
                log.warning("  FAULT %s | corrupting %s (attempt=%s tier=%s)",
                            fault, path, attempt, tier)
                content += _FAULT_PAYLOAD
            h = write_file(worktree, path, content)
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
        "unsupported_test_seams": unsupported_test_seams,
        "oracle_results": batch_oracle_results,
        "current_batch_paths": batch_paths,
        "current_batch_tests": batch_tests,
        "has_pending_tasks": has_pending_tasks,
        "step_log": ["execute"],
    }
