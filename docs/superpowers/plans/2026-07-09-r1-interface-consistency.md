# R1 — Cross-File Interface Consistency Implementation Plan (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**v3 (2026-07-09), after second review round.** Changes from v2: (a) pins are now
symbol-aware `PinRule`s with `applies()` predicates — one rule text is shared between the
Subtask instruction and the pin (single source, not a "MUST mirror" comment), and multiple
matching rules for one symbol **fail Plan loudly**; (b) `dependency_order` uses Tarjan for
SCC *membership only*, then deterministically Kahn-sorts the condensation DAG with a heap
keyed `(min member role order, path)` — no hash-order dependence; (c) the manifest carries
machine-readable `shape` facts (`required_positional`, `required_keyword_only`, `is_async`,
`is_generator`) — the checker never re-parses prose/signature strings, and generator flips
+ kwonly args are actually checked; (d) repair accounting updated end-to-end: `report.py`
counts `contract_repair` in `llm_calls`, and Execute tracks a separate `call_cost` so the
per-job cost ceiling sees both calls; (e) the `request_context` pin no longer recommends
`next(gen())` — the decided target is keep-the-original-shape + a companion yield
dependency for endpoints (generator closing stays owned by the DI layer, and callers don't
change at all).

**v2 (2026-07-09), after external review of v1.** Changes: (a) a persisted, SCC-aware
**target-interface manifest** is now the spine — ordering, prompts, mechanical validation,
and recovery all consume the same frozen artifact (v1 pinned ORIGINAL Flask shapes, which
contradicted subtask rules that legitimately change interfaces, e.g. `request_context`'s
yield-dependency mandate); (b) true Tarjan SCC ordering (v1's Kahn fallback could emit a
dependent before its cyclic dependency); (c) import extraction handles `from . import db`,
aliases, and `import x.y as z` + attribute call sites (v1 missed all three; flaskr's
factory uses the first); (d) the AST gate is named what it is (export-presence + pinned-
shape check); (e) repair calls get first-class accounting and see their rejected draft;
persistent violations keep the *better* draft; (f) full-suite test steps state their live-
Postgres precondition.

**Goal:** Eliminate cross-file call-shape drift (FINDINGS §7, 19/24 probe failures): decide every cross-file interface ONCE at plan time, migrate files in true dependency order against that decision, and mechanically check generated files before any sandbox run.

**Execution status (2026-07-11):** Tasks 1–6 are implemented and locally green; Task 7's
smoke, accounting audit, Qwen iteration grid, and GPT-4o gate were run. The gate did not
close because flaskr remained 0/3 green. Commit checkboxes remain deliberately open for
Sohail; see `corpus/FINDINGS.md` §7 and `notes/2026-07-11-r1-interface-consistency.md`.

**Architecture:** All logic in the agent layer (`agent/nodes/common.py`, `plan.py`, `execute.py`); the `Recipe` Protocol is unchanged — recipes may *optionally* expose an `interface_pins: dict[str, str]` attribute (subtask type → target-shape note) read via `getattr`, so recipe #2 inherits everything. Flow: Plan builds `interface_manifest` (state-checkpointed, frozen; replan appends, never mutates) from AST facts + recipe pin notes → Plan orders tasks by SCC-condensation topological order → Execute prompts state DEFINES/CALLS from the manifest → an AST gate checks presence + pinned shapes, one accounted repair call on violation.

**Tech Stack:** Python 3.12, stdlib `ast` only (no new deps), pytest units on host, eval harness for gates.

## Global Constraints

- Ruff clean (`uv run ruff check src`; line length 100; E,F,I,UP,B). Async nodes. No new pip deps.
- `recipes/base.py` Protocol unchanged (optional attribute via `getattr` only).
- Unit-test commands are self-contained per file. **Full-suite runs require live Postgres:** `docker compose up -d db api` first (test_auth_service.py hits it by design), then `cd apps/backend && POSTGRES_HOST=localhost uv run pytest tests -q`.
- Sohail drives commits: at each commit step, stage + show the diff, commit on his go-ahead (or standing instruction).
- Gates: iterate with qwen driver (free); the published `r1-gate` suite runs GPT-4o driver, K=3, in-container settings assertion first.

---

### Task 1: Binding-aware import extraction (`imported_bindings`)

Foundation everything else queries. For a target module, find every importer and HOW it binds the target's names — handling `from m import x`, `from m import x as y`, `from . import m` (module binding), `import a.b`, `import a.b as c`. Call sites are then found via the *local* binding (alias- and attribute-aware).

**Files:**
- Modify: `apps/backend/src/portage_agent/agent/nodes/common.py` (replace `export_contract`'s scan loop with shared machinery; keep `export_contract`'s public signature working on top of it)
- Test: `apps/backend/tests/test_imported_bindings.py` (new)

**Interfaces:**
- Consumes: existing `_module_names(rel)`, `iter_py_files(root)`.
- Produces:
  - `ModuleBinding` dataclass: `importer: str` (rel path), `symbol: str | None` (None ⇒ whole module bound), `local: str` (name used in importer source).
  - `imported_bindings(root: str, target: str) -> list[ModuleBinding]`
  - `binding_call_sites(src: str, b: ModuleBinding, *, limit: int = 3) -> list[str]` — one-line usage snippets: for symbol bindings matches `local(` and `local` in `with`/decorator lines; for module bindings matches `local.<attr>(` and reports which attrs are used.
  - `export_contract(root, target)` reimplemented as: sorted set of `b.symbol` for symbol bindings **plus** attributes used on module bindings — behavior superset of today's.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/test_imported_bindings.py
"""imported_bindings(): alias/module/relative-import-aware extraction (R1 Task 1)."""
from pathlib import Path

from portage_agent.agent.nodes.common import (
    ModuleBinding,
    binding_call_sites,
    export_contract,
    imported_bindings,
)


def _repo(tmp_path: Path, files: dict[str, str]) -> str:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return str(tmp_path)


def test_plain_from_import():
    b = ModuleBinding(importer="a.py", symbol="get_db", local="get_db")
    src = "from db import get_db\nconn = get_db()\n"
    assert binding_call_sites(src, b) == ["conn = get_db()"]


def test_from_dot_import_module_binding(tmp_path):
    # THE flaskr factory pattern v1 missed: `from . import db` binds the MODULE.
    root = _repo(tmp_path, {
        "flaskr/__init__.py": "from . import db\ndef create_app():\n    db.init_app(app)\n",
        "flaskr/db.py": "def init_app(app):\n    pass\n",
    })
    bindings = imported_bindings(root, "flaskr/db.py")
    assert any(b.symbol is None and b.local == "db" and b.importer == "flaskr/__init__.py"
               for b in bindings)
    assert "init_app" in export_contract(root, "flaskr/db.py")


def test_aliased_from_import(tmp_path):
    root = _repo(tmp_path, {
        "db.py": "def get_db():\n    return 1\n",
        "views.py": "from db import get_db as acquire\nconn = acquire()\n",
    })
    b = next(x for x in imported_bindings(root, "db.py") if x.symbol == "get_db")
    assert b.local == "acquire"
    src = (Path(root) / "views.py").read_text()
    assert binding_call_sites(src, b) == ["conn = acquire()"]


def test_module_import_with_alias_and_attribute_calls(tmp_path):
    root = _repo(tmp_path, {
        "app/db.py": "def get_db():\n    return 1\n",
        "cli.py": "import app.db as db\nrows = db.get_db().all()\n",
    })
    bindings = imported_bindings(root, "app/db.py")
    mod = next(b for b in bindings if b.symbol is None)
    assert mod.local == "db"
    src = (Path(root) / "cli.py").read_text()
    assert any("db.get_db()" in s for s in binding_call_sites(src, mod))
    assert "get_db" in export_contract(root, "app/db.py")  # attr usage counts as export


def test_star_and_unparseable_skipped(tmp_path):
    root = _repo(tmp_path, {
        "db.py": "def get_db():\n    return 1\n",
        "a.py": "from db import *\n",
        "broken.py": "def broken(:\n",
    })
    assert imported_bindings(root, "db.py") == []
```

- [ ] **Step 2: Run tests → expect FAIL (ImportError)**

Run: `cd apps/backend && uv run pytest tests/test_imported_bindings.py -q`

- [ ] **Step 3: Implement in common.py**

Replace the body machinery of `export_contract` with (keep `_module_names` as-is; add
`from dataclasses import dataclass` to imports):

```python
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


def imported_bindings(root: str, target: str) -> list[ModuleBinding]:
    """Every binding of `target` across the repo's other .py files. AST-based,
    best-effort (unparseable importers skipped; `import *` ignored)."""
    wanted = _module_names(target)
    out: list[ModuleBinding] = []
    for rel, src in iter_py_files(root).items():
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
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in wanted or a.name.split(".")[-1] in {
                        w.split(".")[-1] for w in wanted
                    }:
                        out.append(ModuleBinding(
                            rel, None, a.asname or a.name.split(".")[0]))
    return out


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
    """Attribute names accessed on a module binding (`db.get_db` -> {'get_db'})."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    return {
        n.attr for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        and n.value.id == local
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
```

Delete the old `export_contract` loop body it replaces. (`_module_names` stays.)

- [ ] **Step 4: Run tests → expect 5 passed; then lint**

Run: `cd apps/backend && uv run pytest tests/test_imported_bindings.py -q && uv run ruff check src`

- [ ] **Step 5: Commit (with go-ahead)**

```bash
git add apps/backend/src/portage_agent/agent/nodes/common.py apps/backend/tests/test_imported_bindings.py
git commit -m "feat(r1): binding-aware import extraction (relative/module/alias imports)"
```

---

### Task 2: `interface_contract()` — original shapes + call sites on top of bindings

**Files:**
- Modify: `apps/backend/src/portage_agent/agent/nodes/common.py` (append)
- Test: `apps/backend/tests/test_interface_contract.py` (new)

**Interfaces:**
- Consumes: `imported_bindings`, `binding_call_sites`, `_module_attrs_used`, `iter_py_files`.
- Produces: `SymbolContract` dataclass (`name, kind, signature, notes, call_sites: tuple[str, ...]`) and `interface_contract(root: str, target: str) -> list[SymbolContract]`. Signatures/notes describe the ORIGINAL source — the manifest (Task 4) decides targets.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/test_interface_contract.py
"""interface_contract(): structured original-shape extraction (R1 Task 2)."""
from pathlib import Path

from portage_agent.agent.nodes.common import interface_contract


def _repo(tmp_path: Path, files: dict[str, str]) -> str:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return str(tmp_path)


def test_function_signature_and_call_sites(tmp_path):
    root = _repo(tmp_path, {
        "db.py": "def get_db(path=None):\n    return path\n",
        "blog.py": "from db import get_db\nconn = get_db()\n",
    })
    c = interface_contract(root, "db.py")[0]
    assert (c.name, c.kind, c.signature) == ("get_db", "function", "def get_db(path=None)")
    assert "conn = get_db()" in c.call_sites


def test_generator_decorator_notes_and_with_usage(tmp_path):
    root = _repo(tmp_path, {
        "db.py": ("from contextlib import contextmanager\n"
                  "@contextmanager\n"
                  "def get_db():\n    yield 1\n"),
        "views.py": "from db import get_db\nwith get_db() as db:\n    pass\n",
    })
    c = interface_contract(root, "db.py")[0]
    assert "@contextmanager" in c.notes and "generator" in c.notes
    assert any("with get_db()" in s for s in c.call_sites)
    assert c.shape == {"required_positional": 0, "required_keyword_only": [],
                       "is_async": False, "is_generator": True}


def test_shape_facts_async_and_kwonly(tmp_path):
    root = _repo(tmp_path, {
        "svc.py": "async def fetch(url, *, timeout, retries=2):\n    return url\n",
        "app.py": "from svc import fetch\n",
    })
    c = interface_contract(root, "svc.py")[0]
    assert c.signature.startswith("async def fetch(")
    assert c.shape == {"required_positional": 1, "required_keyword_only": ["timeout"],
                       "is_async": True, "is_generator": False}


def test_module_binding_attrs_become_contracts(tmp_path):
    root = _repo(tmp_path, {
        "flaskr/__init__.py": "from . import db\ndb.init_app(1)\n",
        "flaskr/db.py": "def init_app(app):\n    pass\ndef unused():\n    pass\n",
    })
    names = [c.name for c in interface_contract(root, "flaskr/db.py")]
    assert names == ["init_app"]  # only what's actually used, not `unused`


def test_class_and_variable_kinds(tmp_path):
    root = _repo(tmp_path, {
        "models.py": "ANON = object()\nclass User:\n    def __init__(self, name):\n        self.name = name\n",
        "auth.py": "from models import User, ANON\nu = User('x')\n",
    })
    by = {c.name: c for c in interface_contract(root, "models.py")}
    assert by["User"].kind == "class" and by["User"].signature == "class User(name)"
    assert by["ANON"].kind == "variable"
```

- [ ] **Step 2: Run → expect FAIL (ImportError)**

Run: `cd apps/backend && uv run pytest tests/test_interface_contract.py -q`

- [ ] **Step 3: Implement in common.py**

```python
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
    shape: dict     # {"required_positional": int, "required_keyword_only": [str],
                    #  "is_async": bool, "is_generator": bool} — functions only, else {}


def _shape_facts(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    a = node.args
    return {
        "required_positional": len(a.posonlyargs) + len(a.args) - len(a.defaults),
        "required_keyword_only": [
            kw.arg for kw, d in zip(a.kwonlyargs, a.kw_defaults) if d is None
        ],
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "is_generator": any(
            isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)
        ),
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
```

- [ ] **Step 4: Run both unit files → expect 10 passed; lint**

Run: `cd apps/backend && uv run pytest tests/test_imported_bindings.py tests/test_interface_contract.py -q && uv run ruff check src`

- [ ] **Step 5: Commit (with go-ahead)**

```bash
git add apps/backend/src/portage_agent/agent/nodes/common.py apps/backend/tests/test_interface_contract.py
git commit -m "feat(r1): structured interface contracts over binding extraction"
```

---

### Task 3: SCC-condensation dependency ordering

**Files:**
- Modify: `apps/backend/src/portage_agent/agent/nodes/common.py` (append)
- Test: `apps/backend/tests/test_dependency_order.py` (new)

**Interfaces:**
- Consumes: `_module_names`, `imported_bindings` is NOT used here (dependency edges need plain `import` too) — a local `_planned_imports` builds edges; `PlannedFile` (`.path`, `.order`).
- Produces: `dependency_order(files: dict[str, str], planned: list[PlannedFile]) -> list[PlannedFile]` — Tarjan SCCs → condensation DAG topo order → role-`order` tiebreak inside each SCC and between independent SCCs.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/test_dependency_order.py
"""dependency_order(): SCC-aware topological execution order (R1 Task 3)."""
from portage_agent.agent.nodes.common import dependency_order
from portage_agent.recipes.base import PlannedFile


def _pf(path, order):
    return PlannedFile(path=path, role="x", subtasks=[], order=order)


def _paths(files, planned):
    return [p.path for p in dependency_order(files, planned)]


def test_dependency_before_importer_despite_role_order():
    files = {
        "app/db.py": "def get_db():\n    return 1\n",
        "app/blog.py": "from app.db import get_db\n",
        "app/__init__.py": "from . import blog\n",
    }
    planned = [_pf("app/blog.py", 10), _pf("app/db.py", 15), _pf("app/__init__.py", 20)]
    out = _paths(files, planned)
    assert out.index("app/db.py") < out.index("app/blog.py") < out.index("app/__init__.py")


def test_dependent_of_cycle_runs_AFTER_the_cycle():
    # v1's Kahn fallback bug: c.py depends on the a<->b cycle and must NOT precede it,
    # even though its role order (5) is lowest.
    files = {
        "a.py": "from b import g\n",
        "b.py": "from a import f\n",
        "c.py": "from a import f\n",
    }
    planned = [_pf("c.py", 5), _pf("a.py", 10), _pf("b.py", 20)]
    out = _paths(files, planned)
    assert out.index("c.py") > max(out.index("a.py"), out.index("b.py"))
    assert out.index("a.py") < out.index("b.py")  # role order inside the SCC


def test_role_order_between_independent_files():
    files = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
    assert _paths(files, [_pf("b.py", 20), _pf("a.py", 10)]) == ["a.py", "b.py"]


def test_relative_and_plain_imports_create_edges():
    files = {
        "pkg/db.py": "def get_db():\n    return 1\n",
        "pkg/app.py": "from . import db\n",
        "cli.py": "import pkg.app\n",
    }
    planned = [_pf("cli.py", 1), _pf("pkg/app.py", 2), _pf("pkg/db.py", 3)]
    out = _paths(files, planned)
    assert out == ["pkg/db.py", "pkg/app.py", "cli.py"]


def test_deterministic_across_input_permutations():
    # v2 finding #2: independent-SCC order must come from (role order, path), never
    # from set/dict/traversal order. Same result for every input permutation.
    import itertools

    files = {f"m{i}.py": "x = 1\n" for i in range(5)}
    planned = [_pf(f"m{i}.py", 50) for i in range(5)]
    expected = [f"m{i}.py" for i in range(5)]  # tie on order 50 -> path ascending
    for perm in itertools.permutations(planned):
        assert _paths(files, list(perm)) == expected
```

- [ ] **Step 2: Run → expect FAIL (ImportError)**

Run: `cd apps/backend && uv run pytest tests/test_dependency_order.py -q`

- [ ] **Step 3: Implement in common.py**

```python
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
```

`_planned_imports` takes the ORDERED path list (change its signature to
`planned_paths: list[str]` and build `deps` by iterating that list) so every structure
downstream is insertion-ordered; `_tarjan_sccs` is used purely for membership — the heap
provides the user-visible order.

- [ ] **Step 4: Run → expect 5 passed; lint; full-suite check (db up)**

Run: `docker compose up -d db api && cd apps/backend && uv run pytest tests/test_dependency_order.py -q && uv run ruff check src && POSTGRES_HOST=localhost uv run pytest tests -q`

- [ ] **Step 5: Commit (with go-ahead)**

```bash
git add apps/backend/src/portage_agent/agent/nodes/common.py apps/backend/tests/test_dependency_order.py
git commit -m "feat(r1): SCC-condensation dependency ordering (Tarjan)"
```

---

### Task 4: Target-interface manifest — built at Plan, frozen, checkpointed

The spine. For every cross-file symbol: target shape = original shape UNLESS exactly one
symbol-aware `PinRule` claims it (v3: rules carry an `applies(SymbolContract)` predicate —
a file with several idiom subtasks pins each symbol by what it IS, not by whichever rule
matched the file first; **two rules claiming one symbol fail Plan loudly**). Single source
of truth: each rule's note text is one module constant used BOTH inside the corresponding
Subtask instruction and in the PinRule — the checklist and contract share the literal
string object. Persisted in graph state (`interface_manifest`); replan appends, never
mutates.

**Files:**
- Modify: `apps/backend/src/portage_agent/recipes/base.py` (add `PinRule` dataclass — a plain export, the `Recipe` Protocol is untouched)
- Modify: `apps/backend/src/portage_agent/agent/nodes/common.py` (manifest builder)
- Modify: `apps/backend/src/portage_agent/recipes/flask_to_fastapi.py` (note constants + `pin_rules` + wire constants into `_SUBTASKS` instructions)
- Modify: `apps/backend/src/portage_agent/agent/nodes/plan.py` (build + merge into state)
- Modify: `apps/backend/src/portage_agent/agent/state.py` (state key)
- Test: `apps/backend/tests/test_interface_manifest.py` (new)

**Interfaces:**
- Consumes: `interface_contract`, `SymbolContract` (Task 2); `PlannedFile.subtasks` (`.type`); optional `recipe.pin_rules: list[PinRule]`.
- Produces: `PinRule` dataclass (`subtask: str`, `applies: Callable[[SymbolContract], bool]`, `note: str` — note may use `{name}`); `build_manifest(root: str, planned: list, rules: list[PinRule]) -> dict[str, dict]` keyed `"<module_path>::<symbol>"`, values `{"module","symbol","kind","original","target_note","notes","call_sites","shape"}` (plain JSON-safe dicts — LangGraph checkpoint). Raises `ValueError` on pin conflict. Tasks 5–6 consume this exact shape from `state["interface_manifest"]`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/test_interface_manifest.py
"""build_manifest(): frozen, symbol-aware target-interface decisions (R1 Task 4)."""
from pathlib import Path

import pytest

from portage_agent.agent.nodes.common import build_manifest
from portage_agent.recipes.base import PinRule, PlannedFile, Subtask


def _repo(tmp_path: Path, files: dict[str, str]) -> str:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return str(tmp_path)


# One file carrying TWO idiom subtasks — the v2 coarseness case: the sqlalchemy rule
# must claim only `db`, the request_context rule only functions like `get_db`.
FILES = {
    "ext.py": ("from flask_sqlalchemy import SQLAlchemy\n"
               "db = SQLAlchemy()\n"
               "def get_db():\n    return conn\n"),
    "blog.py": "from ext import db, get_db\nrows = get_db().all()\ndb.session.add(1)\n",
}
RULES = [
    PinRule(subtask="request_context",
            applies=lambda c: c.kind == "function",
            note="{name}: keep the original callable shape; endpoints get a companion "
                 "yield dependency"),
    PinRule(subtask="sqlalchemy_plain",
            applies=lambda c: c.kind == "variable" and "SQLAlchemy(" in c.signature,
            note="{name}: plain-SQLAlchemy surface, same module-level name"),
]
BOTH = [Subtask("request_context", "t", "i"), Subtask("sqlalchemy_plain", "t", "i")]


def _planned():
    return [PlannedFile(path="ext.py", role="support", subtasks=list(BOTH)),
            PlannedFile(path="blog.py", role="router", subtasks=[])]


def test_rules_claim_symbols_by_predicate_not_file(tmp_path):
    root = _repo(tmp_path, FILES)
    m = build_manifest(root, _planned(), RULES)
    assert "yield dependency" in m["ext.py::get_db"]["target_note"]
    assert "plain-SQLAlchemy" in m["ext.py::db"]["target_note"]


def test_no_matching_rule_keeps_original_shape(tmp_path):
    root = _repo(tmp_path, FILES)
    m = build_manifest(root, _planned(), rules=[])
    assert m["ext.py::get_db"]["target_note"] == "keep the original shape"


def test_conflicting_rules_fail_plan_loudly(tmp_path):
    root = _repo(tmp_path, FILES)
    greedy = RULES + [PinRule(subtask="sqlalchemy_plain",
                              applies=lambda c: True, note="claims everything")]
    with pytest.raises(ValueError, match="ext.py::db"):
        build_manifest(root, _planned(), greedy)


def test_rule_needs_its_subtask_on_the_file(tmp_path):
    root = _repo(tmp_path, FILES)
    planned = [PlannedFile(path="ext.py", role="support", subtasks=[]),  # no subtasks
               PlannedFile(path="blog.py", role="router", subtasks=[])]
    m = build_manifest(root, planned, RULES)
    assert m["ext.py::get_db"]["target_note"] == "keep the original shape"


def test_manifest_is_json_safe_and_carries_shape(tmp_path):
    import json
    root = _repo(tmp_path, FILES)
    m = build_manifest(root, _planned(), RULES)
    json.dumps(m)  # must not raise
    assert m["ext.py::get_db"]["shape"]["is_generator"] is False
```

- [ ] **Step 2: Run → expect FAIL (ImportError)**

Run: `cd apps/backend && uv run pytest tests/test_interface_manifest.py -q`

- [ ] **Step 3: Implement `PinRule` (recipes/base.py) + builder (common.py)**

In `recipes/base.py` (plain export next to `Subtask`; Protocol untouched):

```python
@dataclass(frozen=True, slots=True)
class PinRule:
    """A symbol-aware target-interface rule. Applies to a symbol iff the DEFINING file's
    task carries `subtask` AND `applies(contract)` is true — a file with several idiom
    subtasks pins each symbol by what it IS. `note` (may use `{name}`) is the SAME string
    object the corresponding Subtask instruction embeds: single source of truth."""

    subtask: str
    applies: object  # Callable[[SymbolContract], bool] — kept loose to avoid import cycle
    note: str
```

In `common.py`:

```python
def build_manifest(root: str, planned: list, rules: list) -> dict[str, dict]:
    """Target-interface manifest: one frozen decision per cross-file symbol. Default
    target = original shape; exactly ONE matching PinRule may override it — two rules
    claiming the same symbol is a recipe bug and fails Plan loudly (never silently
    first-match). Plain JSON-safe dicts: this artifact lives in checkpointed state."""
    manifest: dict[str, dict] = {}
    for pf in planned:
        subtask_types = {s.type for s in pf.subtasks}
        for c in interface_contract(root, pf.path):
            matches = [r for r in rules
                       if r.subtask in subtask_types and r.applies(c)]
            if len(matches) > 1:
                raise ValueError(
                    f"interface pin conflict for {pf.path}::{c.name}: rules "
                    f"{[r.subtask for r in matches]} all claim it — make applies() "
                    f"predicates disjoint")
            note = matches[0].note.format(name=c.name) if matches else \
                "keep the original shape"
            manifest[f"{pf.path}::{c.name}"] = {
                "module": pf.path,
                "symbol": c.name,
                "kind": c.kind,
                "original": c.signature,
                "target_note": note,
                "notes": c.notes,
                "call_sites": list(c.call_sites),
                "shape": c.shape,
            }
    return manifest
```

- [ ] **Step 4: Recipe rules — one note constant feeds BOTH the Subtask instruction and the PinRule**

In `flask_to_fastapi.py`, above `_SUBTASKS` (import `PinRule` from `.base`):

```python
# R1 target-interface notes: module constants used BOTH inside the matching _SUBTASKS
# instruction and in pin_rules below — checklist and contract share the literal string,
# so they cannot drift (v3: single source, not a mirror-by-convention comment).
_NOTE_RESOURCE_FN = (
    "{name}: KEEP the original callable shape — same args, same return, callers do not "
    "change. For FastAPI DI, ADD a companion yield dependency that wraps it "
    "(`def {name}_dep(): resource = {name}(); try: yield resource; finally: close`) and "
    "use `Depends({name}_dep)` in endpoints; cleanup lives in the dependency, NEVER in "
    "callers (no bare `next(gen())` anywhere — it leaks the generator)."
)
_NOTE_LOGIN_SURFACE = (
    "{name}: reimplemented on the session but importable under this exact name with the "
    "attribute surface callers/templates read (is_authenticated, id, ...)."
)
_NOTE_DB_SURFACE = (
    "{name}: the flask_sqlalchemy object becomes a plain-SQLAlchemy surface KEEPING this "
    "module-level name and the attribute surface callers use (session/Model-equivalent: "
    "engine + SessionLocal + Base, or a db_session facade)."
)
```

Append each constant to the instruction of its subtask so the model's checklist carries
the identical decision text (edit the existing `_SUBTASKS` entries):

- `request_context` instruction: `+ "\nInterface decision for cross-file resource "
  "functions: " + _NOTE_RESOURCE_FN`
- `auth_login` instruction: `+ "\nInterface decision: " + _NOTE_LOGIN_SURFACE`
- `sqlalchemy_plain` instruction: `+ "\nInterface decision: " + _NOTE_DB_SURFACE`

Then the class attribute on `FlaskToFastAPIRecipe` (predicates are symbol-aware and
mutually disjoint by construction — kind checks don't overlap):

```python
    # R1: symbol-aware pin rules. applies() runs on the SymbolContract, so a file
    # carrying several idiom subtasks pins each symbol by what it IS. Predicates must
    # stay disjoint — build_manifest fails Plan loudly if two rules claim one symbol.
    pin_rules = [
        PinRule(subtask="request_context",
                applies=lambda c: c.kind == "function",
                note=_NOTE_RESOURCE_FN),
        PinRule(subtask="auth_login",
                applies=lambda c: c.name in {"login_user", "logout_user",
                                             "current_user", "login_required"},
                note=_NOTE_LOGIN_SURFACE),
        PinRule(subtask="sqlalchemy_plain",
                applies=lambda c: c.kind == "variable" and "SQLAlchemy(" in c.signature,
                note=_NOTE_DB_SURFACE),
    ]
```

(`auth_login` names are the stable flask_login API, not repo-specific. The overlap
`auth_login` × `request_context` on a function named `login_user` is possible if one file
carries both subtasks — acceptable loud failure; resolving it means tightening a
predicate, which is exactly the visibility the conflict error exists to force.)

- [ ] **Step 5: Wire into plan_node + state**

`state.py`: add `interface_manifest: dict` to `GraphState` (total=False style, matching
existing optional keys).

`plan.py`, in `plan_node` after `planned = recipe.plan_files(files)` (order of
operations: plan_files → **dependency_order + re-index** → drop_task fault → manifest →
_build_specs):

```python
    planned = dependency_order(files, planned)
    for i, pf in enumerate(planned):
        pf.order = i * 10
```

then after the fault block:

```python
    # R1: freeze the target-interface manifest. On replan only ADD new symbols — pins
    # already made keep binding every retry/escalation/reset to the same decision.
    # A pin conflict (ValueError) is a recipe bug: let it fail the job loudly.
    rules = getattr(recipe, "pin_rules", [])
    manifest = build_manifest(workspace, planned, rules)
    if replan:
        manifest = {**manifest, **(state.get("interface_manifest") or {})}
```

and include `"interface_manifest": manifest` in the returned state dict.
Import `build_manifest, dependency_order` from `.common`.

- [ ] **Step 6: Run → expect 3 passed + prior units green; lint**

Run: `cd apps/backend && uv run pytest tests/test_interface_manifest.py tests/test_imported_bindings.py tests/test_interface_contract.py tests/test_dependency_order.py -q && uv run ruff check src`

- [ ] **Step 7: Commit (with go-ahead)**

```bash
git add apps/backend/src/portage_agent/agent/nodes/common.py apps/backend/src/portage_agent/agent/nodes/plan.py apps/backend/src/portage_agent/agent/state.py apps/backend/src/portage_agent/recipes/base.py apps/backend/src/portage_agent/recipes/flask_to_fastapi.py apps/backend/tests/test_interface_manifest.py
git commit -m "feat(r1): symbol-aware frozen target-interface manifest at plan time"
```

---

### Task 5: Prompts consume the manifest (DEFINES + CALLS, target shapes)

**Files:**
- Modify: `apps/backend/src/portage_agent/agent/nodes/execute.py` (`contract_sections`, `_migrate_file`, call site)
- Test: `apps/backend/tests/test_contract_prompt.py` (new)

**Interfaces:**
- Consumes: `state["interface_manifest"]` (Task 4 shape).
- Produces: `contract_sections(manifest: dict[str, dict], path: str) -> str` — pure function of the manifest (no filesystem), so prompts are identical across retries. `_migrate_file` gains `manifest: dict` kwarg; the old `export_contract` prompt block is removed.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/test_contract_prompt.py
"""contract_sections(): DEFINES/CALLS prompt fragment from the manifest (R1 Task 5)."""
from portage_agent.agent.nodes.execute import contract_sections

MANIFEST = {
    "db.py::get_db": {
        "module": "db.py", "symbol": "get_db", "kind": "function",
        "original": "def get_db()",
        "target_note": "get_db: becomes ONE yield dependency",
        "notes": "", "call_sites": ["rows = get_db().all()"],
    },
}


def test_defines_side_states_target_not_just_original():
    frag = contract_sections(MANIFEST, "db.py")
    assert "DEFINES" in frag
    assert "yield dependency" in frag           # the TARGET decision
    assert "def get_db()" in frag               # original shown for reference
    assert "rows = get_db().all()" in frag      # current call sites to honor/adapt


def test_calls_side_for_a_consumer_file():
    frag = contract_sections(MANIFEST, "blog.py", consumed={"db.py::get_db"})
    assert "CALLS" in frag and "yield dependency" in frag
    assert "DEFINES" not in frag                # blog.py defines nothing contracted


def test_empty_manifest_is_silent():
    assert contract_sections({}, "db.py") == ""
```

- [ ] **Step 2: Run → expect FAIL (ImportError)**

Run: `cd apps/backend && uv run pytest tests/test_contract_prompt.py -q`

- [ ] **Step 3: Implement in execute.py**

Remove `export_contract` from the `common` import block (nothing else uses it in this
file after this step). Add:

```python
def _fmt_pin(p: dict) -> str:
    line = f"  - {p['symbol']}  (was: {p['original']}"
    if p.get("notes"):
        line += f"; {p['notes']}"
    line += f")\n      TARGET: {p['target_note']}"
    for s in p.get("call_sites", []):
        line += f"\n      current call site: {s}"
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
```

In `_migrate_file`: add kwargs `manifest: dict` and `consumed: set[str]`; replace the
`contract = export_contract(...)` block with `user += contract_sections(manifest, path, consumed)`.

In `execute_node`: add `imported_bindings` to the `.common` import block at module top;
before the loop, `manifest = state.get("interface_manifest") or {}`; inside the loop,
before calling `_migrate_file`, compute which pins this file consumes:

```python
            consumed: set[str] = set()
            for k, p in manifest.items():
                if p["module"] == path:
                    continue
                for b in imported_bindings(worktree, p["module"]):
                    if b.importer == path and (b.symbol == p["symbol"] or b.symbol is None):
                        consumed.add(k)
                        break
```

- [ ] **Step 4: Run → expect 3 passed + all units green; lint**

Run: `cd apps/backend && uv run pytest tests/test_contract_prompt.py tests/test_imported_bindings.py tests/test_interface_contract.py tests/test_dependency_order.py tests/test_interface_manifest.py -q && uv run ruff check src`

- [ ] **Step 5: Commit (with go-ahead)**

```bash
git add apps/backend/src/portage_agent/agent/nodes/execute.py apps/backend/tests/test_contract_prompt.py
git commit -m "feat(r1): prompts state frozen DEFINES/CALLS interface decisions"
```

---

### Task 6: Export-presence + pinned-shape check, with an accounted repair call

Named for what it checks: (1) presence — every DEFINES symbol exists at module level; (2) shape — where the manifest kept the original shape, required-arg count must not grow and async/generator flags must not flip (callers pass the original args). Violation → ONE repair call that sees its own rejected draft + the exact violations, logged as a first-class attempt entry with its own tier/model/usage. If the repair still violates, the draft with FEWER violations is written (tie → the repair draft) and Verify/Recover own it.

**Files:**
- Modify: `apps/backend/src/portage_agent/agent/nodes/execute.py`
- Modify: `apps/backend/src/portage_agent/agent/nodes/report.py:59` (llm_calls counts repair calls)
- Test: `apps/backend/tests/test_contract_check.py` (new)

**Interfaces:**
- Consumes: manifest dicts incl. `shape` (Task 4); `_shape_facts` from `.common` (Task 2).
- Produces: `contract_violations(content: str, manifest: dict[str, dict], path: str) -> list[str]` (human-readable violation strings).

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/test_contract_check.py
"""contract_violations(): export-presence + pinned-shape AST gate (R1 Task 6)."""
from portage_agent.agent.nodes.execute import contract_violations


def _pin(symbol, original, target_note="keep the original shape", kind="function"):
    return {"module": "db.py", "symbol": symbol, "kind": kind, "original": original,
            "target_note": target_note, "notes": "", "call_sites": [],
            "shape": ({"required_positional": 0, "required_keyword_only": [],
                       "is_async": False, "is_generator": False}
                      if kind == "function" else {})}


def test_missing_export_flagged():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    v = contract_violations("def other():\n    pass\n", m, "db.py")
    assert v and "get_db" in v[0] and "missing" in v[0]


def test_reexport_and_assignment_count_as_defined():
    m = {"db.py::get_db": _pin("get_db", "def get_db()"),
         "db.py::router": _pin("router", "router = APIRouter()", kind="variable")}
    src = "from impl import get_db\nrouter = object()\n"
    assert contract_violations(src, m, "db.py") == []


def test_grown_required_args_flagged_when_shape_kept():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    v = contract_violations("def get_db(request):\n    pass\n", m, "db.py")
    assert v and "required arg" in v[0]


def test_grown_args_ok_when_pin_redefines_shape():
    m = {"db.py::get_db": _pin("get_db", "def get_db()",
                               target_note="becomes a yield dependency")}
    assert contract_violations("def get_db(request):\n    yield 1\n", m, "db.py") == []


def test_async_original_not_falsely_flagged():
    # v2 bug: the checker re-parsed the signature STRING, which serialized async
    # originals as sync — shape facts come from the manifest now.
    m = {"svc.py::fetch": _pin("fetch", "async def fetch(url)",
                               kind="function")}
    m["svc.py::fetch"]["shape"] = {"required_positional": 1,
                                   "required_keyword_only": [],
                                   "is_async": True, "is_generator": False}
    m["svc.py::fetch"]["module"] = "svc.py"
    assert contract_violations("async def fetch(url):\n    return url\n", m, "svc.py") == []


def test_generator_flip_flagged():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    m["db.py::get_db"]["shape"] = {"required_positional": 0,
                                   "required_keyword_only": [],
                                   "is_async": False, "is_generator": True}
    v = contract_violations("def get_db():\n    return 1\n", m, "db.py")
    assert v and "generator" in v[0]


def test_new_required_kwonly_flagged():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    v = contract_violations("def get_db(*, timeout):\n    pass\n", m, "db.py")
    assert v and "keyword-only" in v[0]


def test_unparseable_flags_everything():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    v = contract_violations("def broken(:\n", m, "db.py")
    assert v and "unparseable" in v[0]


def test_other_modules_pins_ignored():
    m = {"auth.py::login": _pin("login", "def login()") | {"module": "auth.py"}}
    assert contract_violations("x = 1\n", m, "db.py") == []
```

- [ ] **Step 2: Run → expect FAIL (ImportError)**

Run: `cd apps/backend && uv run pytest tests/test_contract_check.py -q`

- [ ] **Step 3: Implement the checker in execute.py**

```python
def contract_violations(content: str, manifest: dict[str, dict], path: str) -> list[str]:
    """Export-presence + pinned-shape check for one generated file (R1). Presence: every
    DEFINES symbol exists at module level (def/class/assign/import re-export). Shape
    (only when the pin kept the original shape, using the manifest's machine-readable
    `shape` facts — NEVER re-parsed from prose/signature strings, v2 finding #3):
    required positional args must not grow, no new required keyword-only args, and
    async/generator-ness must not flip. Deliberately NOT full caller-compatibility
    analysis; Verify owns that."""
    pins = [p for p in manifest.values() if p["module"] == path]
    if not pins:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        return [f"{path} unparseable ({exc.msg} line {exc.lineno}) — every contracted "
                f"symbol is undefined"]
    defined: dict[str, ast.stmt] = {}
    for node in tree.body:
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
        orig = p.get("shape") or {}
        if (p["target_note"] == "keep the original shape" and orig
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
    return out
```

(`import ast` at module top of execute.py; add `_shape_facts` to the `.common` imports —
export it from common.py's public surface in Task 2.)

- [ ] **Step 4: Wire the accounted repair call into the execute loop**

Placement: right after `content, usage = await _migrate_file(...)`, BEFORE fault
injection (a repaired file must still be corruptible — fault semantics unchanged).
Persistence (attempts_log) and control flow (the `spent` ceiling) are accounted
SEPARATELY (v2 finding #4): each LLM call amends its own log entry, while `call_cost`
accumulates every call this iteration made for the in-memory ceiling.

```python
            call_cost = usage.get("cost_usd", 0.0)
            broken = contract_violations(content, manifest, path)
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
                    manifest=manifest, consumed=consumed,
                )
                await task_store.update_task(t.id, amend_last_attempt=usage2)
                call_cost += usage2.get("cost_usd", 0.0)
                # Keep the better draft: fewer violations wins; tie -> the repair draft
                # (it saw the feedback). v1 kept draft 1 on persistent violation — wrong.
                broken2 = contract_violations(content2, manifest, path)
                if len(broken2) <= len(broken):
                    content = content2
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
                # both calls' usage is already amended onto their own log entries; the
                # done-update below must not double-amend (zeroed usage = no-op amend)
```

Two consumer updates in the SAME task (v2 finding #4 — persistence fixed alone is not
enough):

1. **The ceiling flow**: the existing `spent += usage.get("cost_usd", 0.0)` line after
   the done-update becomes `spent += call_cost` — the in-memory per-job cost ceiling now
   sees BOTH calls even though `usage` was zeroed for the amend.
2. **`report.py:59`**: `"calls": sum(1 for a in attempts if a.get("action") == "migrate")`
   becomes `"calls": sum(1 for a in attempts if a.get("action") in ("migrate", "contract_repair"))`
   — repair calls are LLM calls; `llm_calls` in runs/metrics must count them. (Token/cost
   sums already iterate all attempts — no change there.)

And make the done-update amend tolerant: replace `amend_last_attempt=usage` in the
`status=done` update with `amend_last_attempt=usage if any(usage.values()) else None`
(check `db/task_store.py` tolerates `None` as a no-op; add an `if amend_last_attempt:`
guard if not).

- [ ] **Step 5: Run all unit files → green; lint; full suite (db up)**

Run: `docker compose up -d db api && cd apps/backend && uv run pytest tests/test_contract_check.py -q && uv run ruff check src && POSTGRES_HOST=localhost uv run pytest tests -q`

- [ ] **Step 6: Commit (with go-ahead)**

```bash
git add apps/backend/src/portage_agent/agent/nodes/execute.py apps/backend/src/portage_agent/agent/nodes/report.py apps/backend/tests/test_contract_check.py
git commit -m "feat(r1): shape-fact contract gate; repair calls fully accounted"
```

---

### Task 7: End-to-end regression + the R1 gate grid

**Files:** none (measurement). First: `docker compose build worker && docker compose up -d worker`.

- [x] **Step 1: Fixture + fault regression (qwen driver fine here)**

Run: `docker compose run --rm --no-deps worker python -m portage_agent.eval --corpus /corpus/corpus.toml --k 1 --scenarios baseline,bad_patch,drop_task --repos flask-items-fixture --suite r1-smoke`
Expected: all three GREEN (ordering/manifest changes must not break recovery or replan;
drop_task now drops the first file in TOPO order — confirm the replan still repairs it).

- [x] **Step 2: Attempts-log audit (the accounting fix, one query)**

Run a bad_patch job, then:
`docker compose exec db psql -U portage -d portage -tc "SELECT jsonb_array_elements(attempts_log)->>'action' AS action, jsonb_array_elements(attempts_log)->>'cost_usd' IS NOT NULL AS has_cost FROM tasks WHERE job_id='<job>' AND attempts_log::text LIKE '%contract_repair%';"`
Expected: every `migrate` AND every `contract_repair` row carries its own usage; none
merged. Cross-check the run's `llm_calls` in the runs table equals the total row count
(repairs now counted), and that report.json's `llm_usage.cost_usd` matches the sum.
(If no contract_repair fired organically, force one by temporarily tightening a pin.)

- [x] **Step 3: Inner-loop iteration on flaskr + watchlist (qwen, free)**

Run: `docker compose run --rm --no-deps worker python -m portage_agent.eval --corpus /corpus/corpus.toml --k 3 --scenarios baseline --repos flaskr,watchlist --suite r1-iter`
Fix what the failures show — in the extraction/manifest machinery or recipe PIN TEXTS,
never as repo-specific prompt hacks (R5's held-out set will expose those).

- [x] **Step 4: The gate (GPT-4o driver, published numbers)**

`.env`: `LLM_DRIVER_MODEL=azure/AskPandaAI4o`, `LLM_DRIVER_MODEL_LABEL=GPT-4o` →
`docker compose up -d --force-recreate worker` → in-container settings assertion → then:

Run: `docker compose run --rm --no-deps worker python -m portage_agent.eval --corpus /corpus/corpus.toml --k 3 --scenarios baseline --repos flask-items-fixture,minimal-flask-api,flaskr,watchlist --suite r1-gate`

Gate (vs `k3-baseline`): fixture 3/3 AND mfa ≥2/3 (no regression); flaskr ≥1/3 green with
call-shape drift no longer dominant in the failure probe; watchlist avg test-pass ≥0.75.

- [x] **Step 5: Record**

Update `corpus/FINDINGS.md` §7 (verdict + r1-gate table) and tick R1 in
`portage-recipe-excellence-plan.md`. Restore qwen driver if desired.

Recorded outcome (2026-07-11): findings and master plan updated. GPT-4o was restored as
the persistent driver after the Qwen experiment; R1 was intentionally left open because
flaskr failed the ≥1/3 green gate at 0/3.

### R1.1 follow-on — completed, held-out gate still open

Implemented generically after Task 7: frozen consumer bindings and narrow direct-caller
checks; deterministic framework-capability seam decisions; bounded resource/factory/test-
harness cluster generation with coordinated retries; package-symbol-reexport resolution;
mechanical FastAPI/Click seam checks; full recipe-instruction rehydration; and a bundled
structural fixture. `r1-1-structural-confirm` and minimal-flask-api reached 3/3, while
`r1-1-flaskr-confirm` remained 0/3 and watchlist remained 0/3 in `r1-1-final-gate`.
The implementation is complete; R1 remains open on its original held-out criterion.

---

## Self-Review Notes (v3)

- Second-round findings addressed: #1 symbol-aware `PinRule.applies()` predicates with
  loud conflict failure (`test_conflicting_rules_fail_plan_loudly`), note text single-
  sourced into both Subtask instruction and pin; #2 Tarjan = membership only, heap-Kahn
  condensation keyed `(role order, path)`, ordered list end-to-end, permutation test;
  #3 machine-readable `shape` in SymbolContract + manifest, checker compares AST facts
  via the shared `_shape_facts` (async-original false-flag test, generator-flip test,
  kwonly test); #4 `report.py` llm_calls counts `contract_repair`, `spent += call_cost`
  keeps the ceiling truthful; the `request_context` pin decision is now keep-original +
  companion yield dependency (no `next(gen())` anywhere — generator closing owned by DI).
- Verified during review intake: `report.py:59` filter confirmed migrate-only;
  `spent +=` line confirmed in execute_node; v2's async check provably re-parsed
  signature strings that dropped `async`.

## Self-Review Notes (v2)

- Review findings addressed: #1 manifest is the spine (Task 4; pins mirror subtask text —
  the v1 contradiction is structurally impossible); #2 Tarjan SCC + condensation order
  with the reviewer's exact case as a test (`test_dependent_of_cycle_runs_AFTER_the_cycle`);
  #3 `from . import db` / aliases / module-attr call sites each have a dedicated test
  (Task 1); #4 renamed export-presence + pinned-shape check, shape checks only where the
  pin keeps the original (Task 6); #5 per-call amend-then-append accounting with a DB
  audit step (Task 6 Step 4, Task 7 Step 2); #6 fewer-violations draft selection, tie →
  repair draft; #7 repair prompt embeds the rejected draft + exact violations; #8 full-
  suite steps start db/api and set POSTGRES_HOST, unit steps are per-file.
- Deliberate v3 scope cuts: broad whole-program caller analysis still belongs to Verify,
  but R1.1 later added narrow binding/arity enforcement for statically-obvious direct calls.
  LLM-decided
  target interfaces (v1 of the manifest is deterministic: original-or-pin-rule; an LLM
  planning call can upgrade `target_note` later without changing any consumer). R1.1 also
  replaced the cluster deferral with bounded semantic seam units—not whole-SCC migration.
- Type consistency check: manifest value dict keys (`module/symbol/kind/original/
  target_note/notes/call_sites`) identical across Tasks 4/5/6 tests and code;
  `contract_sections(manifest, path, consumed)` consistent between test and impl;
  `ModuleBinding` fields consistent across Tasks 1/2.
- Honest uncertainty: `task_store.update_task`'s `amend_last_attempt` None-tolerance must
  be verified at Task 6 Step 4 (the plan says check and guard if needed).
