"""Deterministic executable migration-cut analysis.

Import ordering answers "what should be generated first?" but not "when is it safe to
run the application?" A Flask registrar cannot consume a migrated ``APIRouter`` and a
FastAPI route cannot consume a still-Flask request-context helper. This module identifies
those framework-contract edges and condenses their connected components into verification
cuts. It uses syntax, task roles/subtasks, and frozen interface pins only—never repository
names or expected test values.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from portage_agent.recipes.base import PlannedFile

from .common import ModuleBinding, imported_bindings_from_sources
from .oracle import NON_REWRITE_TEST_STRATEGIES

_ROUTER_REGISTRARS = {
    "add_namespace", "include_router", "register_blueprint", "register_blueprints",
}
_FLASK_HARNESS_APIS = re.compile(
    r"\.(?:app_context|session_transaction|test_cli_runner|test_client)\s*\("
)
_MIDDLEWARE_STATE = re.compile(
    r"\b(?:flash|session|current_user|login_required|render_template|url_for)\b"
)
_STATEFUL_SUBTASKS = {
    "auth_login", "request_context", "sessions_flash", "sqlalchemy_plain",
}


@dataclass(frozen=True, slots=True)
class ExecutableEdge:
    provider: str
    consumer: str
    kind: str
    operation: str
    evidence: str

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "consumer": self.consumer,
            "kind": self.kind,
            "operation": self.operation,
            "evidence": self.evidence,
        }


def _expr_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (ValueError, TypeError):  # pragma: no cover - defensive for exotic ASTs
        return ""


def _matches_binding(node: ast.AST, binding: ModuleBinding) -> bool:
    text = _expr_text(node)
    return text == binding.local or text.startswith(binding.local + ".")


def _node_uses_binding(node: ast.AST, binding: ModuleBinding) -> bool:
    return any(
        _matches_binding(candidate, binding)
        for candidate in ast.walk(node)
        if isinstance(candidate, (ast.Name, ast.Attribute))
    )


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return _expr_text(call.func)


def _call_uses_binding(call: ast.Call, binding: ModuleBinding) -> bool:
    if _matches_binding(call.func, binding):
        return True
    return any(
        _node_uses_binding(value, binding)
        for value in [*call.args, *(kw.value for kw in call.keywords)]
    )


def _binding_is_called(tree: ast.AST, binding: ModuleBinding) -> bool:
    return any(
        isinstance(node, ast.Call) and _call_uses_binding(node, binding)
        for node in ast.walk(tree)
    )


def _binding_is_referenced(tree: ast.AST, binding: ModuleBinding) -> bool:
    return any(
        _matches_binding(node, binding)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    )


def _evidence(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node) or _expr_text(node)
    return " ".join(segment.split())[:200]


def _has_click_command(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Name) and target.id == "command"
                or isinstance(target, ast.Attribute) and target.attr == "command"
            ):
                return True
    return False


def _resource_symbols(manifest: dict[str, dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for pin in manifest.values():
        if not pin.get("additional_exports"):
            continue
        out.setdefault(pin["module"], set()).add(pin["symbol"])
        out[pin["module"]].update(pin["additional_exports"])
    return out


def _binding_targets_symbols(binding: ModuleBinding, symbols: set[str]) -> bool:
    # A module binding may access any of the resource exports. Exact attribute use is
    # confirmed by _binding_is_called below; a direct symbol binding is known immediately.
    return binding.symbol in symbols if binding.symbol else bool(symbols)


def _call_targets_symbols(
    call: ast.Call, binding: ModuleBinding, symbols: set[str],
) -> bool:
    if binding.symbol:
        return binding.symbol in symbols and _call_uses_binding(call, binding)
    function = _expr_text(call.func)
    return any(function == f"{binding.local}.{symbol}" for symbol in symbols)


def _edge_candidates(
    files: dict[str, str], planned: list[PlannedFile], manifest: dict[str, dict],
    test_strategy: dict[str, str],
) -> list[ExecutableEdge]:
    by_path = {item.path: item for item in planned}
    resource_symbols = _resource_symbols(manifest)
    edges: dict[tuple[str, str, str, str], ExecutableEdge] = {}

    def add(provider: str, consumer: str, kind: str, operation: str, evidence: str) -> None:
        if provider == consumer:
            return
        key = (provider, consumer, kind, operation)
        edges[key] = ExecutableEdge(provider, consumer, kind, operation, evidence)

    # A created artifact has no original import sites by definition. Its frozen proposal
    # supplies the missing provider/consumer edges so it is generated and verified with
    # the code that will begin importing it.
    for provider in planned:
        if provider.origin != "recipe" or provider.action != "create":
            continue
        contract = provider.artifact_contract or {}
        for consumer in contract.get("consumers", []):
            if consumer in by_path:
                add(
                    provider.path, consumer, "planned_artifact", "consume",
                    provider.purpose or "planned target architecture artifact",
                )
        for dependency in contract.get("depends_on", []):
            if dependency in by_path:
                add(
                    dependency, provider.path, "artifact_dependency", "depends_on",
                    provider.purpose or "planned target architecture artifact",
                )

    for provider in planned:
        if provider.origin != "recipe":
            continue
        provider_subtasks = {subtask.type for subtask in provider.subtasks}
        provider_stateful = bool(provider_subtasks & _STATEFUL_SUBTASKS)
        provider_has_cli = _has_click_command(files.get(provider.path, ""))

        for binding in imported_bindings_from_sources(files, provider.path):
            consumer = by_path.get(binding.importer)
            if consumer is None or consumer.origin != "recipe":
                continue
            if test_strategy.get(consumer.path) in NON_REWRITE_TEST_STRATEGIES:
                continue
            source = files.get(consumer.path, "")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
            binding_called = _binding_is_called(tree, binding)
            binding_referenced = _binding_is_referenced(tree, binding)
            registrar_calls = [
                call for call in calls if _call_name(call) in _ROUTER_REGISTRARS
            ]

            # Framework object registration is the canonical hard edge: once the provider
            # becomes an APIRouter/Namespace equivalent, every active registrar must move.
            for call in calls:
                operation = _call_name(call)
                if operation in _ROUTER_REGISTRARS and any(
                    _node_uses_binding(value, binding)
                    for value in [*call.args, *(kw.value for kw in call.keywords)]
                ):
                    add(
                        provider.path, consumer.path, "router_registration", operation,
                        _evidence(source, call),
                    )

                # Flask extensions and lifecycle modules conventionally export init_app.
                # Both ``db.init_app(app)`` and ``from db import init_app; init_app(app)``
                # are binding-aware here.
                if operation == "init_app" and _matches_binding(call.func, binding):
                    add(
                        provider.path, consumer.path, "extension_initialization",
                        operation, _evidence(source, call),
                    )

                if operation == "add_command" and any(
                    _node_uses_binding(value, binding)
                    for value in [*call.args, *(kw.value for kw in call.keywords)]
                ):
                    add(
                        provider.path, consumer.path, "cli_registration", operation,
                        _evidence(source, call),
                    )

            # Common factories collect imported blueprints in a list/dict and register a
            # loop variable. The call argument then no longer names the import directly;
            # a router binding referenced in the same factory plus a registrar call is the
            # conservative, binding-aware fallback.
            has_registrar_edge = any(
                edge.provider == provider.path and edge.consumer == consumer.path
                and edge.kind == "router_registration"
                for edge in edges.values()
            )
            if (
                provider.role == "router"
                and consumer.role == "app_factory"
                and binding_referenced
                and registrar_calls
                and not has_registrar_edge
            ):
                call = registrar_calls[0]
                add(
                    provider.path, consumer.path, "router_registration",
                    _call_name(call) + "_indirect", _evidence(source, call),
                )

            resource_calls = [
                call for call in calls
                if _call_targets_symbols(
                    call, binding, resource_symbols.get(provider.path, set()),
                )
            ]
            if (
                provider.path in resource_symbols
                and _binding_targets_symbols(binding, resource_symbols[provider.path])
                and resource_calls
            ):
                add(
                    provider.path, consumer.path, "resource_lifecycle", "call",
                    _evidence(source, resource_calls[0]),
                )

            if provider_stateful and binding_referenced:
                add(
                    provider.path, consumer.path, "framework_state",
                    "call" if binding_called else "reference",
                    next(
                        (_evidence(source, call) for call in calls
                         if _call_uses_binding(call, binding)),
                        binding.local,
                    ),
                )

            # A planned Flask support module called by the application factory is an
            # app-owned registrar/initializer even when its function has a project-
            # specific name. Migrating the provider alone creates an impossible mixed
            # framework state, so verify it with the consumer that invokes it.
            if (
                provider.role == "support"
                and consumer.role == "app_factory"
                and binding_called
            ):
                add(
                    provider.path, consumer.path, "factory_provider_call", "call",
                    next(
                        (_evidence(source, call) for call in calls
                         if _call_uses_binding(call, binding)),
                        binding.local,
                    ),
                )

            if (
                provider.role == "app_factory"
                and consumer.role == "test_harness"
                and binding_called
                and (
                    test_strategy.get(consumer.path) == "adapter_wiring"
                    or _FLASK_HARNESS_APIS.search(source)
                )
            ):
                add(
                    provider.path, consumer.path, "factory_harness", "create_app",
                    next(
                        (_evidence(source, call) for call in calls
                         if _call_uses_binding(call, binding)),
                        binding.local,
                    ),
                )

            if provider_has_cli and consumer.role == "test_harness":
                add(
                    provider.path, consumer.path, "cli_harness", "invoke",
                    binding.local,
                )

            # Session/template/auth consumers require their app factory's middleware and
            # configuration in the same cut. The normal router-registration edge connects
            # them; this extra typed edge records why the cut cannot be split later.
            if (
                provider.role == "router"
                and consumer.role == "app_factory"
                and _MIDDLEWARE_STATE.search(files.get(provider.path, ""))
                and any(edge.provider == provider.path and edge.consumer == consumer.path
                        and edge.kind == "router_registration" for edge in edges.values())
            ):
                add(
                    provider.path, consumer.path, "middleware_configuration",
                    "session/template/auth", "route requires app-owned middleware/config",
                )

    return sorted(
        edges.values(),
        key=lambda edge: (
            edge.provider, edge.consumer, edge.kind, edge.operation, edge.evidence,
        ),
    )


def _components(paths: list[str], edges: list[ExecutableEdge]) -> list[set[str]]:
    parent = {path: path for path in paths}

    def find(path: str) -> str:
        while parent[path] != path:
            parent[path] = parent[parent[path]]
            path = parent[path]
        return path

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for edge in edges:
        union(edge.provider, edge.consumer)
    grouped: dict[str, set[str]] = {}
    for path in paths:
        grouped.setdefault(find(path), set()).add(path)
    return [members for members in grouped.values() if len(members) >= 2]


def build_executable_cut_analysis(
    files: dict[str, str], planned: list[PlannedFile], manifest: dict[str, dict],
    test_strategy: dict[str, str], *, coordinated_limit: int = 4,
) -> dict:
    """Return JSON-safe framework edges and disjoint executable verification cuts.

    ``mode=coordinated`` cuts fit in one bounded multi-file generation prompt. Larger
    components are ``batch_only``: all members are generated before Verify, but individual
    prompts remain bounded. Precise recovery/bisection of those large components is a
    separate scheduler concern and is surfaced in diagnostics rather than hidden.
    """
    eligible = [
        item.path for item in planned
        if item.origin == "recipe"
        and test_strategy.get(item.path) not in NON_REWRITE_TEST_STRATEGIES
    ]
    order = {item.path: index for index, item in enumerate(planned)}
    edges = _edge_candidates(files, planned, manifest, test_strategy)
    eligible_set = set(eligible)
    active_edges = [
        edge for edge in edges
        if edge.provider in eligible_set and edge.consumer in eligible_set
    ]
    cuts: list[dict] = []
    diagnostics: list[dict] = []
    for members in _components(eligible, active_edges):
        paths = sorted(members, key=lambda path: (order[path], path))
        component_edges = [
            edge for edge in active_edges
            if edge.provider in members and edge.consumer in members
        ]
        kinds = sorted({edge.kind for edge in component_edges})
        mode = "coordinated" if len(paths) <= coordinated_limit else "batch_only"
        cut = {
            "id": f"executable-cut-{len(cuts) + 1}",
            "paths": paths,
            "reason": "executable framework contracts: " + ", ".join(kinds),
            "edge_kinds": kinds,
            "mode": mode,
        }
        cuts.append(cut)
        if mode == "batch_only":
            diagnostics.append({
                "kind": "large_executable_cut",
                "cut_id": cut["id"],
                "paths": paths,
                "size": len(paths),
                "coordinated_limit": coordinated_limit,
                "next_need": "bounded component recovery or deterministic coexistence bridge",
            })
    return {
        "version": 1,
        "edges": [edge.to_dict() for edge in edges],
        "cuts": cuts,
        "diagnostics": diagnostics,
    }


def merge_small_cuts_into_units(
    units: list[dict], cuts: list[dict], *, coordinated_limit: int = 4,
) -> list[dict]:
    """Make small executable cuts coordinated prompts while keeping units disjoint."""
    merged = [dict(unit, paths=list(unit.get("paths", []))) for unit in units]
    for cut in cuts:
        if cut.get("mode") != "coordinated":
            continue
        overlaps = [unit for unit in merged if set(unit["paths"]) & set(cut["paths"])]
        combined = set(cut["paths"])
        for unit in overlaps:
            combined.update(unit["paths"])
        if len(combined) > coordinated_limit:
            continue
        merged = [unit for unit in merged if unit not in overlaps]
        # Cut order follows dependency_order and is authoritative. Any legacy seam-only
        # member not already represented is appended deterministically.
        ordered = list(dict.fromkeys([
            *cut["paths"],
            *(path for unit in overlaps for path in unit["paths"]),
        ]))
        merged.append({
            "id": cut["id"] + "-coordinated",
            "paths": [path for path in ordered if path in combined],
            "reason": cut["reason"],
        })
    for index, unit in enumerate(merged, 1):
        unit["id"] = f"coordinated-unit-{index}"
    return merged
