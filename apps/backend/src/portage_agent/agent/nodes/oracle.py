"""Test-oracle extraction and integrity checks for compatibility-first migrations."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from .common import iter_py_files

# These oracle strategies never produce a framework rewrite. ``adapter`` and
# ``unchanged`` files are preserved deterministically; ``unsupported_test_seam`` files
# are reported and skipped. They therefore cannot be members of an executable migration
# cut or coordinated generation unit.
NON_REWRITE_TEST_STRATEGIES = frozenset({
    "adapter", "unchanged", "sanctioned_normalization", "unsupported_test_seam",
})


def _is_test_file(path: str) -> bool:
    name = Path(path).name
    return (
        name == "conftest.py" or name == "tests.py" or name.startswith("test_")
        or name.endswith("_test.py") or "/tests/" in f"/{path}"
    )


class _AssertionNormalizer(ast.NodeTransformer):
    """Canonicalize only approved Flask-response/FastAPI-response equivalents."""

    @staticmethod
    def _sentinel(name: str, receiver: ast.expr) -> ast.Call:
        return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=[receiver], keywords=[])

    def visit_Call(self, node: ast.Call) -> ast.AST:  # noqa: N802 - ast visitor API
        node = self.generic_visit(node)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"get_json", "json"}:
            normalized = self._sentinel("_PORTAGE_RESPONSE_JSON", node.func.value)
            return ast.copy_location(normalized, node)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get_data":
            as_text = any(
                kw.arg == "as_text" and isinstance(kw.value, ast.Constant)
                and kw.value.value is True for kw in node.keywords
            )
            sentinel = "_PORTAGE_RESPONSE_TEXT" if as_text else "_PORTAGE_RESPONSE_BYTES"
            return ast.copy_location(self._sentinel(sentinel, node.func.value), node)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802
        node = self.generic_visit(node)
        if node.attr == "text":
            return ast.copy_location(self._sentinel("_PORTAGE_RESPONSE_TEXT", node.value), node)
        if node.attr in {"data", "content"}:
            return ast.copy_location(self._sentinel("_PORTAGE_RESPONSE_BYTES", node.value), node)
        return node


def _fingerprint(node: ast.AST) -> str:
    normalized = _AssertionNormalizer().visit(ast.fix_missing_locations(node))
    return ast.dump(normalized, annotate_fields=True, include_attributes=False)


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    return ast.unparse(target)


def _test_records(content: str) -> dict[str, dict]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {}
    records: dict[str, dict] = {}

    def walk(stmts: list[ast.stmt], prefix: tuple[str, ...] = ()) -> None:
        for node in stmts:
            if isinstance(node, ast.ClassDef):
                walk(node.body, (*prefix, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name.startswith("test")
            ):
                key = "::".join((*prefix, node.name))
                assertions = sorted(
                    _fingerprint(child.test) for child in ast.walk(node)
                    if isinstance(child, ast.Assert)
                )
                raises = sorted(
                    _fingerprint(child.items[0].context_expr.args[0])
                    for child in ast.walk(node)
                    if isinstance(child, ast.With) and child.items
                    and isinstance(child.items[0].context_expr, ast.Call)
                    and _decorator_name(child.items[0].context_expr) == "pytest.raises"
                    and child.items[0].context_expr.args
                )
                decorators = [_decorator_name(d) for d in node.decorator_list]
                skip_calls = [
                    ast.unparse(child.func) for child in ast.walk(node)
                    if isinstance(child, ast.Call)
                    and ast.unparse(child.func) in {"pytest.skip", "pytest.xfail"}
                ]
                records[key] = {
                    "assertions": assertions,
                    "raises": raises,
                    "parametrize": sorted(
                        _fingerprint(d) for d in node.decorator_list
                        if "parametrize" in _decorator_name(d)
                    ),
                    "skip_xfail": sorted(
                        [
                            name for name in decorators
                            if "skip" in name or "xfail" in name
                        ] + skip_calls
                    ),
                }

    walk(tree.body)
    return records


def _fixture_records(content: str) -> dict[str, dict]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {}
    records: dict[str, dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = [
            decorator for decorator in node.decorator_list
            if "fixture" in _decorator_name(decorator)
        ]
        if not decorators:
            continue
        records[node.name] = {
            "dependencies": [
                argument.arg for argument in [*node.args.posonlyargs, *node.args.args]
            ],
            "keyword_dependencies": [argument.arg for argument in node.args.kwonlyargs],
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "is_generator": any(
                isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(node)
            ),
            "decorators": sorted(_fingerprint(decorator) for decorator in decorators),
        }
    return records


def _fixtures(content: str) -> list[str]:
    return sorted(_fixture_records(content))


def _global_skip_xfail(content: str) -> list[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.extend(
                _decorator_name(decorator) for decorator in node.decorator_list
                if "skip" in _decorator_name(decorator)
                or "xfail" in _decorator_name(decorator)
            )
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            rendered = ast.unparse(node)
            if "pytestmark" in rendered and ("skip" in rendered or "xfail" in rendered):
                out.append(rendered)
    return sorted(out)


def classify_test_strategy(path: str, content: str) -> str:
    """Compatibility first; unsupported only for direct Flask context-global inspection."""
    if not _is_test_file(path):
        return "unchanged"
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return "guarded_rewrite"
    context_globals = {"g", "session", "current_app", "request"}
    direct_context = any(
        isinstance(node, ast.ImportFrom) and node.module == "flask"
        and any(alias.name in context_globals for alias in node.names)
        for node in ast.walk(tree)
    ) or any(
        isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "flask" and node.attr in context_globals
        for node in ast.walk(tree)
    )
    unsupported_context_builders = (
        ".test_request_context(", ".request_context(",
    )
    if direct_context or any(marker in content for marker in unsupported_context_builders):
        return "unsupported_test_seam"
    supported = (
        ".test_client(", ".app_context(", ".test_cli_runner(", ".session_transaction(",
        ".get_json(", ".get_data(", ".data", ".config", ".testing", ".instance_path",
    )
    if Path(path).name == "conftest.py" and any(marker in content for marker in supported):
        return "adapter_wiring"
    if any(marker in content for marker in supported):
        return "adapter"
    if "from flask" in content or "import flask" in content:
        return "guarded_rewrite"
    return "unchanged"


def build_oracle_manifest(root: str) -> dict[str, dict]:
    manifest: dict[str, dict] = {}
    for path, content in iter_py_files(root).items():
        if not _is_test_file(path):
            continue
        manifest[path] = {
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "tests": _test_records(content),
            "fixtures": _fixtures(content),
            "fixture_shapes": _fixture_records(content),
            "global_skip_xfail": _global_skip_xfail(content),
            "strategy": classify_test_strategy(path, content),
        }
    return manifest


def apply_sanctioned_normalizations(content: str, replacements: list[dict]) -> str:
    """Apply recipe-frozen, exact-line plumbing replacements and nothing else.

    Recipes decide which transformations are sanctioned. The engine only enforces that
    each frozen original line still matches and that no replacement range overlaps.
    """
    lines = content.splitlines(keepends=True)
    occupied: set[int] = set()
    for replacement in sorted(replacements, key=lambda item: item["line"]):
        line = int(replacement["line"])
        index = line - 1
        if index < 0 or index >= len(lines) or index in occupied:
            raise ValueError(f"invalid or overlapping sanctioned normalization line {line}")
        original = lines[index].removesuffix("\n").removesuffix("\r")
        if original != replacement["before"]:
            raise ValueError(
                f"sanctioned normalization source drift at line {line}: "
                f"expected {replacement['before']!r}, got {original!r}"
            )
        ending = "\r\n" if lines[index].endswith("\r\n") else (
            "\n" if lines[index].endswith("\n") else ""
        )
        lines[index] = replacement["after"] + ending
        occupied.add(index)
    return "".join(lines)


def oracle_violations(original: dict, generated: str) -> list[str]:
    current = _test_records(generated)
    expected = original.get("tests", {})
    out: list[str] = []
    current_fixtures = _fixtures(generated)
    if current_fixtures != original.get("fixtures", []):
        out.append(
            f"fixture set changed: expected {original.get('fixtures', [])}, "
            f"got {current_fixtures}"
        )
    current_fixture_shapes = _fixture_records(generated)
    for name in sorted(set(current_fixture_shapes) & set(original.get("fixture_shapes", {}))):
        if current_fixture_shapes[name] != original["fixture_shapes"][name]:
            out.append(f"{name}: fixture dependencies/lifecycle/decorator changed")
    introduced_global = (
        set(_global_skip_xfail(generated)) - set(original.get("global_skip_xfail", []))
    )
    if introduced_global:
        out.append(f"introduced module/class skip/xfail markers {sorted(introduced_global)}")
    if set(current) != set(expected):
        out.append(
            f"test set changed: expected {sorted(expected)}, got {sorted(current)}"
        )
    for name in sorted(set(current) & set(expected)):
        before, after = expected[name], current[name]
        if before["assertions"] != after["assertions"]:
            out.append(f"{name}: assertion set changed or weakened")
        if before["raises"] != after["raises"]:
            out.append(f"{name}: pytest.raises expectation changed")
        if before["parametrize"] != after["parametrize"]:
            out.append(f"{name}: parametrize contract changed")
        introduced = set(after["skip_xfail"]) - set(before["skip_xfail"])
        if introduced:
            out.append(f"{name}: introduced skip/xfail decorators {sorted(introduced)}")
    return out
