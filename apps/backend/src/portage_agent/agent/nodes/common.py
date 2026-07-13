"""Shared helpers for the agent nodes: workspaces, the migration git worktree, file IO,
content hashing, and parsing the LLM's fenced-code output.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from portage_agent.config import settings

log = logging.getLogger("portage.agent")

# Dirs we never treat as project source (graph artifacts, vcs, caches, the worktree itself).
_SKIP_DIRS = {
    ".git", ".code-review-graph", "__pycache__", ".pytest_cache",
    ".portage-worktree", ".portage-cut-checkpoint",
}

_FENCE = re.compile(r"```(?:[\w+-]*)\n(.*?)```", re.DOTALL)


def workspace_for(job_id: str) -> str:
    return f"{settings.workspaces_mount}/{job_id}"


def worktree_for(job_id: str) -> str:
    # Sibling of the workspace, on the same shared volume so the sandbox can mount + run it.
    return f"{settings.workspaces_mount}/{job_id}-migrated"


def iter_py_files(root: str) -> dict[str, str]:
    """Map repo-relative path -> source for every .py file, skipping artifact dirs."""
    base = Path(root)
    out: dict[str, str] = {}
    for p in base.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.relative_to(base).parts):
            continue
        try:
            out[str(p.relative_to(base))] = p.read_text()
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
    return out


def non_python_listing(root: str, *, limit: int = 80) -> str:
    """Relative paths of the repo's non-Python files (templates, static, config).

    Shown to the model as context: template-rendering migrations need to know where the
    templates directory actually is and what files it holds — that never appears in the
    .py context files. Credential-shaped paths are omitted (Phase 7 redaction)."""
    from .redaction import is_denied_path

    base = Path(root)
    out: list[str] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix == ".py":
            continue
        rel = p.relative_to(base)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if is_denied_path(str(rel)):
            continue
        out.append(str(rel))
        if len(out) >= limit:
            out.append("… (truncated)")
            break
    return "\n".join(out)


def non_python_context(
    root: str, *, limit: int = 24000,
    suffixes: frozenset[str] = frozenset({
        ".html", ".jinja", ".jinja2", ".sql", ".toml", ".yaml", ".yml",
    }),
) -> str:
    """Bounded, redacted repository data that changes migration semantics.

    Paths alone cannot reveal Jinja globals, fixture data formats, or warning policy.
    Keep the survey deliberately small and deterministic; binary/static assets remain a
    listing only.
    """
    return "\n\n".join(
        f"--- {path} ---\n{body}"
        for path, body in non_python_sources(
            root, content_limit=limit, suffixes=suffixes,
        ).items()
        if body
    )


def non_python_sources(
    root: str, *, content_limit: int = 24000, path_limit: int = 500,
    suffixes: frozenset[str] = frozenset({
        ".html", ".jinja", ".jinja2", ".sql", ".toml", ".yaml", ".yml",
    }),
) -> dict[str, str]:
    """Bounded non-Python path inventory with text only for semantic file types."""
    from .redaction import is_denied_path, scrub

    base = Path(root)
    out: dict[str, str] = {}
    remaining = content_limit
    for path in sorted(base.rglob("*")):
        if len(out) >= path_limit:
            break
        if not path.is_file() or path.suffix == ".py":
            continue
        rel = path.relative_to(base)
        if any(part in _SKIP_DIRS for part in rel.parts) or is_denied_path(str(rel)):
            continue
        body = ""
        if remaining > 0 and path.suffix.lower() in suffixes:
            try:
                body = scrub(path.read_text())[:remaining]
            except (OSError, UnicodeDecodeError):
                pass
            remaining -= len(body)
        out[str(rel)] = body
    return out


def read_file(root: str, rel: str, *, limit: int = 8000) -> str | None:
    p = Path(root) / rel
    if not p.exists():
        return None
    text = p.read_text(errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n# … (truncated)\n"


def write_file(root: str, rel: str, content: str) -> str:
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return content_hash(content)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_cut_checkpoint(worktree: str) -> dict:
    manifest = Path(worktree, ".portage-cut-checkpoint", "manifest.json")
    try:
        value = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def create_cut_checkpoint(
    worktree: str, paths: list[str], baseline: dict[str, dict],
) -> dict:
    """Snapshot a cut before generation; the on-disk copy survives worker resumes."""
    existing = load_cut_checkpoint(worktree)
    if existing and set(paths) <= set(existing.get("paths", [])):
        return existing

    root = Path(worktree, ".portage-cut-checkpoint")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    files: dict[str, dict] = {}
    for path in paths:
        source = Path(worktree, path)
        record = {**baseline[path], "existed": source.exists()}
        if source.exists():
            snapshot = hashlib.sha256(path.encode()).hexdigest() + ".snapshot"
            Path(root, snapshot).write_bytes(source.read_bytes())
            record["snapshot"] = snapshot
        files[path] = record
    checkpoint = {"root": str(root), "paths": list(paths), "files": files}
    Path(root, "manifest.json").write_text(json.dumps(checkpoint))
    return checkpoint


def restore_cut_checkpoint(worktree: str, checkpoint: dict) -> list[str]:
    """Restore the pre-cut files and remove the snapshot. Git index cleanup stays async."""
    root = Path(checkpoint.get("root") or Path(worktree, ".portage-cut-checkpoint"))
    restored: list[str] = []
    for path, record in checkpoint.get("files", {}).items():
        target = Path(worktree, path)
        if record.get("existed"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(Path(root, record["snapshot"]).read_bytes())
        elif target.exists():
            target.unlink()
        restored.append(path)
    shutil.rmtree(root, ignore_errors=True)
    return restored


def discard_cut_checkpoint(checkpoint: dict) -> None:
    if root := checkpoint.get("root"):
        shutil.rmtree(root, ignore_errors=True)


def extract_code(text: str) -> str:
    """Pull the migrated file out of the model's reply: first fenced block, else the whole
    reply. Models are told to emit exactly one ```python block."""
    m = _FENCE.search(text)
    return (m.group(1) if m else text).strip() + "\n"


async def run_git(*args: str, cwd: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def ensure_worktree(workspace: str, worktree: str) -> None:
    """Create the migration worktree at the workspace HEAD, idempotently (resume-safe)."""
    if (Path(worktree) / ".git").exists():
        log.info("worktree already present — reusing %s", worktree)
        return
    code, out = await run_git("worktree", "add", "--detach", "--force", worktree, "HEAD",
                              cwd=workspace)
    if code != 0:
        raise RuntimeError(f"git worktree add failed: {out[:400]}")
    log.info("created migration worktree %s", worktree)


async def worktree_diff(worktree: str) -> str:
    """The migration diff = tracked changes in the worktree vs its clean HEAD."""
    _, out = await run_git("diff", cwd=worktree)
    return out


async def file_diff(worktree: str, rel: str) -> str:
    """One file's migration diff vs the worktree's clean HEAD."""
    _, out = await run_git("diff", "--", rel, cwd=worktree)
    return out


def _module_names(rel: str) -> set[str]:
    """Module names under which `rel` can be imported (absolute or relative)."""
    parts = Path(rel).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    names = set()
    for i in range(len(parts)):
        names.add(".".join(parts[i:]))
    return names


@dataclass(frozen=True, slots=True)
class ModuleBinding:
    """How one importer file binds `target`: a symbol (`from m import x [as y]`) or the
    whole module (`from . import m`, `import a.m [as z]`). `local` is the name that
    appears in the importer's source — call-site scanning must use it, not the symbol."""

    importer: str
    symbol: str | None  # None => module binding
    local: str


def _resolve_module(node_module: str | None, level: int, importer: str) -> str:
    """Absolute dotted module for an ImportFrom, resolving relative levels."""
    base = Path(importer).parts[: -level] if level else ()
    parts = [*base, *(node_module.split(".") if node_module else [])]
    return ".".join(parts)


def _package_reexports_symbol(
    sources: dict[str, str], module: str, name: str,
) -> bool:
    """Whether `module.__init__` binds `name` as a symbol/object export.

    This disambiguates `from pkg import app`: when pkg/__init__.py says
    `from .app import app`, consumers receive the exported object, not the pkg.app module.
    A `from . import db` package export remains a module binding and returns False.
    """
    package_source = next((
        src for rel, src in sources.items()
        if Path(rel).name == "__init__.py" and module in _module_names(rel)
    ), None)
    if package_source is None:
        return False
    try:
        tree = ast.parse(package_source)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if any((alias.asname or alias.name) == name for alias in node.names):
                return True
    return False


def imported_bindings_from_sources(
    sources: dict[str, str], target: str,
) -> list[ModuleBinding]:
    """Every binding of ``target`` in an in-memory source map.

    Keeping this pure lets Plan-time graph analyzers query the exact frozen source set
    without creating fixture directories or re-reading a workspace. Unparseable importers
    are skipped and star imports are deliberately ignored.
    """
    wanted = _module_names(target)
    out: list[ModuleBinding] = []
    for rel, src in sources.items():
        if rel == target:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = _resolve_module(node.module, node.level, rel)
                if node.module is None:
                    # `from . import db` / `from .pkg import db` — names ARE modules.
                    for a in node.names:
                        if f"{mod}.{a.name}".lstrip(".") in wanted or a.name in {
                            w.split(".")[-1] for w in wanted
                        }:
                            out.append(ModuleBinding(rel, None, a.asname or a.name))
                elif mod in wanted or mod.split(".")[-1] in {w.split(".")[-1] for w in wanted}:
                    for a in node.names:
                        if a.name != "*":
                            out.append(ModuleBinding(rel, a.name, a.asname or a.name))
                else:
                    # `from pkg import db` / `from .pkg import db` imports the target
                    # MODULE as a name from its parent package. The direct-symbol branch
                    # above cannot see this because `mod` is only the parent (`pkg`).
                    for a in node.names:
                        candidate = f"{mod}.{a.name}".lstrip(".")
                        if candidate in wanted and not _package_reexports_symbol(
                            sources, mod, a.name,
                        ):
                            out.append(ModuleBinding(rel, None, a.asname or a.name))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in wanted or a.name.split(".")[-1] in {
                        w.split(".")[-1] for w in wanted
                    }:
                        # Unaliased `import a.b` binds the name `a` in Python, but the
                        # source text accesses attributes on the full dotted path `a.b`
                        # (`a.b.symbol()`) — keep the whole path so _module_attrs_used
                        # resolves the real symbol instead of the submodule tail.
                        out.append(ModuleBinding(rel, None, a.asname or a.name))
    return out


def imported_bindings(root: str, target: str) -> list[ModuleBinding]:
    """Filesystem wrapper around :func:`imported_bindings_from_sources`."""
    return imported_bindings_from_sources(iter_py_files(root), target)


def binding_call_sites(src: str, b: ModuleBinding, *, limit: int = 3) -> list[str]:
    """Up to `limit` one-line usage snippets for a binding, scanned via its LOCAL name."""
    needles = ([f"{b.local}(", f"with {b.local}", f"@{b.local}"] if b.symbol
               else [f"{b.local}."])
    hits: list[str] = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("def ", "class ", "from ", "import ", "#")):
            continue
        if any(n in s for n in needles):
            hits.append(s[:160])
            if len(hits) >= limit:
                break
    return hits


def _module_attrs_used(src: str, local: str) -> set[str]:
    """Attribute names accessed on a module binding (`db.get_db` -> {'get_db'}). `local`
    may itself be a dotted path (unaliased `import a.b` keeps the full `a.b` in
    ModuleBinding.local), so match on the unparsed accessed expression rather than a
    bare `ast.Name` — that's the only way `a.b.symbol()` resolves to `symbol`."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    return {
        n.attr for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and ast.unparse(n.value) == local
    }


def export_contract(root: str, target: str) -> list[str]:
    """Names other files import (or use as module attributes) FROM `target` — the
    interface a migration must keep exporting. Superset of the pre-R1 behavior."""
    sources = iter_py_files(root)
    names: set[str] = set()
    for b in imported_bindings(root, target):
        if b.symbol:
            names.add(b.symbol)
        else:
            names |= _module_attrs_used(sources.get(b.importer, ""), b.local)
    return sorted(names)


@dataclass(frozen=True, slots=True)
class SymbolContract:
    """One cross-file symbol: original kind/shape + how importers actually use it.
    `shape` holds MACHINE-READABLE facts for validation — never recovered by parsing
    the human-facing `signature`/`notes` strings (v2 review finding #3)."""

    name: str
    kind: str  # "function" | "class" | "variable"
    signature: str  # human-facing, for prompts
    notes: str      # human-facing, for prompts
    call_sites: tuple[str, ...]
    shape: dict     # JSON-safe callable facts used by definition + direct-caller checks;
                    # functions only, otherwise {}.


def _has_own_yield(node: ast.AST) -> bool:
    """True if `node`'s OWN scope contains a `yield`/`yield from` — unlike `ast.walk`,
    does not descend into nested `def`/`async def`/`lambda` scopes, each of which is its
    own (potential) generator and must not mark the enclosing function as one."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if _has_own_yield(child):
            return True
    return False


def _returns_nested_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether this callable returns a function defined in its own local scope.

    This is the smallest structural fact that distinguishes decorator factories from
    ordinary request handlers.  Like ``_has_own_yield``, it deliberately refuses to
    descend through a nested callable's scope.
    """
    nested_names = {
        child.name
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if not nested_names:
        return False

    def _has_return(stmts: list[ast.stmt]) -> bool:
        for stmt in stmts:
            if (
                isinstance(stmt, ast.Return)
                and isinstance(stmt.value, ast.Name)
                and stmt.value.id in nested_names
            ):
                return True
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, ast.stmt) and _has_return([child]):
                    return True
        return False

    return _has_return(node.body)


def _shape_facts(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    a = node.args
    positional = [*a.posonlyargs, *a.args]
    required_count = len(positional) - len(a.defaults)
    return {
        "required_positional": required_count,
        "required_positional_names": [arg.arg for arg in positional[:required_count]],
        "required_keyword_only": [
            kw.arg for kw, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is None
        ],
        "positional_capacity": len(positional),
        # Positional-only parameters deliberately excluded: passing them by keyword is
        # invalid even though their names appear in the source signature.
        "keyword_names": [arg.arg for arg in a.args] + [arg.arg for arg in a.kwonlyargs],
        "accepts_varargs": a.vararg is not None,
        "accepts_varkw": a.kwarg is not None,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "is_generator": _has_own_yield(node),
        "returns_nested_function": _returns_nested_function(node),
    }


def _def_contract(node: ast.stmt) -> tuple[str, str, str, dict] | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        shape = _shape_facts(node)
        prefix = "async def" if shape["is_async"] else "def"
        sig = f"{prefix} {node.name}({ast.unparse(node.args)})"
        notes = [f"@{ast.unparse(d)}" for d in node.decorator_list]
        if shape["is_generator"]:
            notes.append("generator (context-manager / yield-dependency shape)")
        return "function", sig, "; ".join(notes), shape
    if isinstance(node, ast.ClassDef):
        init = next((i for i in node.body
                     if isinstance(i, ast.FunctionDef) and i.name == "__init__"), None)
        args = ast.unparse(init.args) if init else ""
        args = args.split(",", 1)[1].strip() if "," in args else ""
        return "class", f"class {node.name}({args})", "", {}
    return None


def interface_contract(root: str, target: str) -> list[SymbolContract]:
    """Structured contract for `target`: every name siblings bind (directly or as module
    attributes), with ORIGINAL signature/lifecycle and real call-site examples."""
    sources = iter_py_files(root)
    bindings = imported_bindings(root, target)
    used: dict[str, list[str]] = {}  # symbol -> call sites
    for b in bindings:
        src = sources.get(b.importer, "")
        if b.symbol:
            used.setdefault(b.symbol, []).extend(binding_call_sites(src, b, limit=2))
        else:
            for attr in _module_attrs_used(src, b.local):
                used.setdefault(attr, []).extend(
                    s for s in binding_call_sites(src, b, limit=3) if f".{attr}" in s)
    if not used:
        return []

    kinds: dict[str, tuple[str, str, str, dict]] = {}
    try:
        for node in ast.parse(sources.get(target, "")).body:
            info = _def_contract(node)
            if info and getattr(node, "name", "") in used:
                kinds[node.name] = info
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in used:
                        kinds[t.id] = ("variable",
                                       f"{t.id} = {ast.unparse(node.value)[:80]}", "", {})
    except SyntaxError:
        pass

    return [
        SymbolContract(name=n, kind=k[0], signature=k[1], notes=k[2],
                       call_sites=tuple(dict.fromkeys(used[n]))[:3], shape=k[3])
        for n in sorted(used)
        for k in [kinds.get(n, ("variable", n, "", {}))]
    ]


def _planned_imports(files: dict[str, str], planned_paths: list[str]) -> dict[str, set[str]]:
    """path -> other planned paths it imports (edges for ordering). Handles ImportFrom
    (incl. `from . import mod`), plain Import, and aliases — resolution via _module_names.
    Takes an ordered LIST and builds insertion-ordered structures (determinism)."""
    by_module: dict[str, str] = {}
    for p in planned_paths:
        for mod in _module_names(p):
            by_module[mod] = p
    deps: dict[str, set[str]] = {p: set() for p in planned_paths}
    for path in planned_paths:
        try:
            tree = ast.parse(files.get(path, ""))
        except SyntaxError:
            continue
        mods: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = _resolve_module(node.module, node.level, path)
                if node.module is None:
                    mods.extend(f"{base}.{a.name}".lstrip(".") for a in node.names)
                else:
                    mods.append(base)
                    mods.extend(f"{base}.{a.name}" for a in node.names)  # from pkg import mod
            elif isinstance(node, ast.Import):
                mods.extend(a.name for a in node.names)
        for mod in mods:
            dep = by_module.get(mod) or by_module.get(mod.split(".")[-1])
            if dep and dep != path:
                deps[path].add(dep)
    return deps


def planned_artifact_topology_violations(
    content: str, manifest: dict[str, dict], path: str,
) -> list[str]:
    """Reject edges from a planned provider back to its consumers."""
    pins = [
        pin for pin in manifest.values()
        if pin.get("module") == path and pin.get("provenance") == "planned_create"
    ]
    consumers = {
        consumer["module"]
        for pin in pins for consumer in pin.get("consumers", [])
    }
    if not consumers:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    imports: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and (
            isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
            or isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
        ):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_module(node.module, node.level, path)
            if node.module is not None and base:
                imports.append((node.lineno, base))
            imports.extend(
                (node.lineno, f"{base}.{alias.name}".lstrip("."))
                for alias in node.names
            )
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    out = []
    for consumer in sorted(consumers):
        names = _module_names(consumer)
        line = next((line for line, module in imports if module in names), None)
        if line is not None:
            out.append(
                f"{path}:{line}: provider-first topology forbids importing declared "
                f"consumer {consumer}; keep shared state in the provider or a declared "
                "lower-level dependency"
            )
    return out


def _tarjan_sccs(deps: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCCs, iterative. Returned in REVERSE topological order of the
    condensation (dependencies' SCCs appear before dependents' — Tarjan's property)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0

    for root in deps:
        if root in index:
            continue
        work = [(root, iter(sorted(deps[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in deps:
                    continue
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(deps[nxt]))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == node:
                        break
                sccs.append(scc)
    return sccs


def dependency_order(files: dict[str, str], planned: list) -> list:
    """Re-sort PlannedFiles so imports migrate FIRST (FINDINGS §7). Tarjan identifies SCC
    MEMBERSHIP only; the condensation DAG is then Kahn-sorted with a heap keyed by each
    ready SCC's best `(role order, path)` member — fully deterministic, no dependence on
    set/dict/traversal order (v2 review finding #2). Role order breaks ties inside an
    SCC and between simultaneously-ready SCCs."""
    import heapq

    by_path = {pf.path: pf for pf in planned}
    ordered_paths = [pf.path for pf in planned]  # NEVER a set — determinism
    deps = _planned_imports(files, ordered_paths)
    # Original source cannot import a file that does not exist yet. Created artifacts
    # therefore contribute explicit frozen dependency edges: their dependencies run first,
    # and the artifact itself runs before every declared consumer.
    for pf in planned:
        if getattr(pf, "action", "rewrite") != "create":
            continue
        contract = getattr(pf, "artifact_contract", {}) or {}
        deps[pf.path].update(
            path for path in contract.get("depends_on", []) if path in deps
        )
        for consumer in contract.get("consumers", []):
            if consumer in deps:
                deps[consumer].add(pf.path)
    sccs = _tarjan_sccs(deps)

    scc_of = {p: i for i, scc in enumerate(sccs) for p in scc}
    indegree = [0] * len(sccs)
    dependents: list[set[int]] = [set() for _ in sccs]
    for path, targets in deps.items():
        for dep in targets:
            a, b = scc_of[dep], scc_of[path]  # edge: dep's SCC -> dependent's SCC
            if a != b and b not in dependents[a]:
                dependents[a].add(b)
                indegree[b] += 1

    def scc_key(i: int) -> tuple:
        return min((by_path[p].order, p) for p in sccs[i])

    ready = [(scc_key(i), i) for i in range(len(sccs)) if indegree[i] == 0]
    heapq.heapify(ready)
    ordered: list = []
    while ready:
        _, i = heapq.heappop(ready)
        ordered.extend(sorted((by_path[p] for p in sccs[i]),
                              key=lambda pf: (pf.order, pf.path)))
        for j in dependents[i]:
            indegree[j] -= 1
            if indegree[j] == 0:
                heapq.heappush(ready, (scc_key(j), j))
    return ordered


def build_migration_units(
    files: dict[str, str], planned: list, manifest: dict[str, dict], *, max_files: int = 4
) -> list[dict]:
    """Select small, tightly-coupled initial generation units for framework seams.

    This is deliberately semantic and recipe-agnostic: a seed is a preserved callable
    with a required companion export (a resource-helper pin); it may pull in one importing
    application factory and one importing test harness. Routers and unrelated tests are
    excluded, even when they import the resource. The result is deterministic, bounded,
    JSON-safe, and contains no repository-name/path exceptions.
    """
    by_path = {pf.path: pf for pf in planned}
    order = {pf.path: i for i, pf in enumerate(planned)}
    deps = _planned_imports(files, list(by_path))
    resource_owners = {
        p["module"] for p in manifest.values()
        if p.get("preserve_shape") and p.get("additional_exports")
        and p.get("module") in by_path
    }

    def depends_on(path: str, target: str) -> bool:
        pending = list(deps.get(path, set()))
        seen: set[str] = set()
        while pending:
            dep = pending.pop()
            if dep == target:
                return True
            if dep not in seen:
                seen.add(dep)
                pending.extend(deps.get(dep, set()))
        return False

    claimed: set[str] = set()
    units: list[dict] = []
    for owner in sorted(resource_owners, key=lambda p: (order[p], p)):
        if owner in claimed:
            continue
        members = [owner]
        factories = [
            p for p, pf in by_path.items()
            if pf.role == "app_factory" and depends_on(p, owner)
        ]
        factories.sort(key=lambda p: (order[p], p))
        if factories and len(members) < max_files:
            members.append(factories[0])

        harnesses = [
            p for p, pf in by_path.items()
            if pf.role == "test_harness"
            and any(depends_on(p, member) for member in members)
        ]
        # conftest is the standard pytest wiring seam; prefer it over behavioural test
        # modules without naming any repository or individual test.
        harnesses.sort(key=lambda p: (
            Path(p).name != "conftest.py", order[p], p,
        ))
        if harnesses and len(members) < max_files:
            members.append(harnesses[0])
        # Pytest fixture injection creates no import edge from a test module to
        # conftest. A CLI invocation nevertheless shares the runner adapter's call shape;
        # include at most one such harness while the unit remains bounded.
        invocation_harnesses = [
            p for p, pf in by_path.items()
            if pf.role == "test_harness" and p not in members
            and ".invoke(" in files.get(p, "")
        ]
        invocation_harnesses.sort(key=lambda p: (order[p], p))
        if invocation_harnesses and len(members) < max_files:
            members.append(invocation_harnesses[0])

        roles = {by_path[p].role for p in members}
        if len(members) < 2 or not ({"support", "app_factory"} & roles):
            continue
        members.sort(key=lambda p: (order[p], p))
        if {"support", "app_factory", "test_harness"} <= roles:
            reason = "shared resource/factory/test-harness seam"
        elif "app_factory" in roles:
            reason = "shared resource/application-factory seam"
        else:
            reason = "shared resource/test-harness seam"
        units.append({
            "id": f"framework-seam-{len(units) + 1}",
            "paths": members,
            "reason": reason,
        })
        claimed.update(members)
    return units


def build_manifest(root: str, planned: list, rules: list) -> dict[str, dict]:
    """Target-interface manifest: one frozen decision per cross-file symbol. Default
    target = original shape; exactly ONE matching PinRule may override it — two rules
    claiming the same symbol is a recipe bug and fails Plan loudly (never silently
    first-match). Plain JSON-safe dicts: this artifact lives in checkpointed state."""
    manifest: dict[str, dict] = {}
    roles = {item.path: item.role for item in planned}
    sources = iter_py_files(root)
    for pf in planned:
        if getattr(pf, "action", "rewrite") == "create":
            contract = getattr(pf, "artifact_contract", {}) or {}
            for export in contract.get("exports", []):
                key = f"{pf.path}::{export['name']}"
                if key in manifest:
                    raise ValueError(f"duplicate planned artifact export: {key}")
                manifest[key] = {
                    "module": pf.path,
                    "symbol": export["name"],
                    "kind": export["kind"],
                    "original": "planned target artifact",
                    "target_note": (
                        export.get("signature") or getattr(pf, "purpose", "")
                        or "implement the frozen planned export"
                    ),
                    "notes": getattr(pf, "purpose", ""),
                    "call_sites": [],
                    "consumers": [
                        {"module": path, "local": export["name"], "binding": "planned"}
                        for path in contract.get("consumers", [])
                    ],
                    "shape": {},
                    "preserve_shape": False,
                    "target_kind": export["kind"],
                    "additional_exports": [],
                    "members": list(export.get("members", [])),
                    "member_shapes": _planned_member_shapes(
                        sources, export.get("members", []),
                    ),
                    "capabilities": list(contract.get("capabilities", [])),
                    "factory_consumers": [
                        path for path in contract.get("consumers", [])
                        if roles.get(path) == "app_factory"
                    ],
                    "depends_on": list(contract.get("depends_on", [])),
                    "provenance": "planned_create",
                }
            continue
        subtask_types = {s.type for s in pf.subtasks}
        for c in interface_contract(root, pf.path):
            matches = [r for r in rules
                       if r.subtask in subtask_types and r.applies(c)]
            if len(matches) > 1:
                raise ValueError(
                    f"interface pin conflict for {pf.path}::{c.name}: rules "
                    f"{[r.subtask for r in matches]} all claim it — make applies() "
                    f"predicates disjoint")
            rule = matches[0] if matches else None
            note = rule.note.format(name=c.name) if rule else "keep the original shape"
            consumers: list[dict] = []
            for binding in imported_bindings(root, pf.path):
                uses_symbol = binding.symbol == c.name
                uses_module_attr = (
                    binding.symbol is None
                    and c.name in _module_attrs_used(
                        sources.get(binding.importer, ""), binding.local,
                    )
                )
                if uses_symbol or uses_module_attr:
                    consumers.append({
                        "module": binding.importer,
                        "local": binding.local,
                        "binding": "symbol" if uses_symbol else "module",
                    })
            manifest[f"{pf.path}::{c.name}"] = {
                "module": pf.path,
                "symbol": c.name,
                "kind": c.kind,
                "original": c.signature,
                "target_note": note,
                "notes": c.notes,
                "call_sites": list(c.call_sites),
                "consumers": sorted(
                    consumers,
                    key=lambda item: (item["module"], item["local"], item["binding"]),
                ),
                "shape": c.shape,
                "preserve_shape": rule.preserve_shape if rule else True,
                "target_kind": (rule.target_kind if rule and rule.target_kind else c.kind),
                "additional_exports": [
                    name.format(name=c.name) for name in (rule.additional_exports if rule else ())
                ],
            }
    return manifest


def _planned_member_shapes(
    sources: dict[str, str], members: list[str],
) -> dict[str, str]:
    """Infer planned class-member shape from existing consumer expressions."""
    wanted = set(members)
    shapes: dict[str, str] = {}
    if not wanted:
        return shapes
    for source in sources.values():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in wanted:
                continue
            parent = parents.get(node)
            # ponytail: direct member calls are enough for current evidence; add local
            # alias-flow tracking only when a measured consumer calls through an alias.
            called = isinstance(parent, ast.Call) and parent.func is node
            call_parent = parents.get(parent) if called else None
            shape = (
                "context_manager"
                if called and isinstance(call_parent, ast.withitem)
                and call_parent.context_expr is parent
                else "method" if called else "attribute"
            )
            rank = {"attribute": 0, "method": 1, "context_manager": 2}
            if rank[shape] >= rank.get(shapes.get(node.attr, "attribute"), 0):
                shapes[node.attr] = shape
    return dict(sorted(shapes.items()))
