"""Deterministic runtime/provider normalizers for Flask to FastAPI."""

from __future__ import annotations

import ast
import io
import symtable
import tokenize
from copy import deepcopy
from pathlib import PurePosixPath

from portage_agent.agent.nodes.common import _module_names, _resolve_module

from ._flask_analysis import (
    _parsed,
)


def _module_import_index(tree: ast.Module) -> int:
    """Return the first legal insertion point after a docstring/future imports."""
    index = int(bool(
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ))
    while (
        index < len(tree.body)
        and isinstance(tree.body[index], ast.ImportFrom)
        and tree.body[index].module == "__future__"
    ):
        index += 1
    return index


def _realize_flask_form_provider(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Back source FlaskForm subclasses with framework-free WTForms behavior."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "form_provider" and item.get("provider") == path
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None or not decision.get("runtime_provider"):
        return content

    changed = False
    for statement in list(tree.body):
        if not isinstance(statement, ast.ImportFrom) or statement.module != "flask_wtf":
            continue
        statement.names = [alias for alias in statement.names if alias.name != "FlaskForm"]
        if not statement.names:
            tree.body.remove(statement)
        changed = True

    imports = [
        ast.ImportFrom(
            module="wtforms",
            names=[
                ast.alias(name="Form", asname="_PortageFormBase"),
                ast.alias(name="HiddenField"),
            ],
            level=0,
        ),
        ast.ImportFrom(
            module="markupsafe",
            names=[ast.alias(name="Markup"), ast.alias(name="escape")],
            level=0,
        ),
        ast.ImportFrom(
            module="secrets", names=[ast.alias(name="token_urlsafe")], level=0,
        ),
        ast.ImportFrom(
            module=decision["runtime_provider"].removesuffix(".py").replace("/", "."),
            names=[
                ast.alias(name="get_request_context", asname="_portage_form_context"),
                ast.alias(name="session", asname="_portage_form_session"),
            ],
            level=0,
        ),
    ]
    existing_imports = {
        (statement.module, alias.name, alias.asname)
        for statement in tree.body if isinstance(statement, ast.ImportFrom)
        for alias in statement.names
    }
    insert_at = _module_import_index(tree)
    for statement in imports:
        missing = [
            alias for alias in statement.names
            if (statement.module, alias.name, alias.asname) not in existing_imports
        ]
        if not missing:
            continue
        tree.body.insert(insert_at, ast.ImportFrom(
            module=statement.module, names=missing, level=0,
        ))
        insert_at += 1
        changed = True

    base_name = "_PortageForm"
    if not any(
        isinstance(node, ast.ClassDef) and node.name == base_name for node in tree.body
    ):
        base = ast.parse(
            "class _PortageForm(_PortageFormBase):\n"
            "    csrf_token = HiddenField()\n"
            "    def hidden_tag(self):\n"
            "        token = _portage_form_session.get('_csrf_token')\n"
            "        if not token:\n"
            "            token = token_urlsafe(32)\n"
            "            _portage_form_session['_csrf_token'] = token\n"
            "        self.csrf_token.data = token\n"
            "        return Markup(f'<input id=\"csrf_token\" name=\"csrf_token\" "
            "type=\"hidden\" value=\"{escape(token)}\">')\n"
            "    def validate_on_submit(self):\n"
            "        request = _portage_form_context().get('request')\n"
            "        return bool(\n"
            "            request is not None and request.method == 'POST' and self.validate()\n"
            "        )\n"
        ).body[0]
        first_form = next((
            index for index, node in enumerate(tree.body)
            if isinstance(node, ast.ClassDef) and node.name in decision["classes"]
        ), len(tree.body))
        tree.body.insert(first_form, base)
        changed = True
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in decision["classes"]:
            continue
        kept = [
            base for base in node.bases
            if ast.unparse(base).split(".")[-1] not in {"FlaskForm", "Form"}
        ]
        wanted = ast.Name(id=base_name, ctx=ast.Load())
        if not any(ast.dump(base) == ast.dump(wanted) for base in kept):
            kept.insert(0, wanted)
        if [ast.dump(base) for base in node.bases] != [ast.dump(base) for base in kept]:
            node.bases = kept
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_flask_form_consumers(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Bind source form objects to request form data inside async target routes."""
    classes = {
        name
        for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "form_provider" and path in item.get("consumers", [])
        for name in item.get("classes", [])
    }
    tree = _parsed(content)
    if not classes or tree is None:
        return content
    changed = False
    for function in (
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    ):
        request_name = next((
            argument.arg
            for argument in [*function.args.posonlyargs, *function.args.args]
            if argument.arg == "request"
            or argument.annotation is not None
            and ast.unparse(argument.annotation).split(".")[-1] == "Request"
        ), "")
        if not request_name:
            continue
        for call in (
            node for statement in function.body for node in ast.walk(statement)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in classes and not node.args and not node.keywords
        ):
            request = ast.Name(id=request_name, ctx=ast.Load())
            call.args.append(ast.IfExp(
                test=ast.Compare(
                    left=ast.Attribute(
                        value=deepcopy(request), attr="method", ctx=ast.Load(),
                    ),
                    ops=[ast.Eq()], comparators=[ast.Constant(value="POST")],
                ),
                body=ast.Await(value=ast.Call(
                    func=ast.Attribute(
                        value=deepcopy(request), attr="form", ctx=ast.Load(),
                    ),
                    args=[], keywords=[],
                )),
                orelse=ast.Constant(value=None),
            ))
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_factory_static_mount(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Resolve frozen static directories relative to their application module."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "application_factory"
        and item.get("factory") == path and item.get("static_mount")
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    static_mount = decision["static_mount"]
    package = PurePosixPath(path).parent
    directory = PurePosixPath(static_mount["directory"])
    try:
        relative = directory.relative_to(package)
    except ValueError:
        return content
    changed = False
    found_mount = False
    for call in (
        node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and ast.unparse(node.func).split(".")[-1] == "StaticFiles"
    ):
        keyword = next((item for item in call.keywords if item.arg == "directory"), None)
        if keyword is None:
            continue
        found_mount = True
        value: ast.expr = ast.Call(
            func=ast.Attribute(
                value=ast.Call(
                    func=ast.Name(id="Path", ctx=ast.Load()),
                    args=[ast.Name(id="__file__", ctx=ast.Load())], keywords=[],
                ),
                attr="resolve", ctx=ast.Load(),
            ),
            args=[], keywords=[],
        )
        value = ast.Attribute(value=value, attr="parent", ctx=ast.Load())
        for part in relative.parts:
            value = ast.BinOp(left=value, op=ast.Div(), right=ast.Constant(part))
        if ast.dump(keyword.value) != ast.dump(value):
            keyword.value = value
            changed = True
        check_dir = next((item for item in call.keywords if item.arg == "check_dir"), None)
        if check_dir is None:
            call.keywords.append(ast.keyword(
                arg="check_dir", value=ast.Constant(value=False),
            ))
            changed = True
        elif not (
            isinstance(check_dir.value, ast.Constant)
            and check_dir.value.value is False
        ):
            check_dir.value = ast.Constant(value=False)
            changed = True
    if not found_mount:
        factory = next((
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "create_app"
        ), None)
        returns = [
            (index, statement.value.id)
            for index, statement in enumerate(factory.body if factory else [])
            if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name)
        ]
        if len(returns) != 1:
            return content
        index, app_name = returns[0]
        directory_code = "Path(__file__).resolve().parent" + "".join(
            f" / {part!r}" for part in relative.parts
        )
        factory.body.insert(index, ast.parse(
            f"{app_name}.mount({static_mount['path']!r}, "
            f"StaticFiles(directory={directory_code}, check_dir=False), "
            f"name={static_mount['name']!r})"
        ).body[0])
        tree.body.insert(_module_import_index(tree), ast.ImportFrom(
            module="fastapi.staticfiles", names=[ast.alias(name="StaticFiles")],
            level=0,
        ))
        found_mount = True
        changed = True
    if changed:
        path_import = next((
            statement for statement in tree.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "pathlib"
        ), None)
        if path_import is None:
            tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                module="pathlib", names=[ast.alias(name="Path")], level=0,
            ))
        elif not any(alias.name == "Path" for alias in path_import.names):
            path_import.names.append(ast.alias(name="Path"))
    template_names = {
        target.id
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and isinstance(statement.value, ast.Call)
        and ast.unparse(statement.value.func).split(".")[-1] == "Jinja2Templates"
        for target in (
            statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        )
        if isinstance(target, ast.Name)
    }
    if template_names and not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_portage_url_for"
        for node in tree.body
    ):
        jinja_import = next((
            statement for statement in tree.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "jinja2"
        ), None)
        if jinja_import is None:
            tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                module="jinja2", names=[ast.alias(name="pass_context")], level=0,
            ))
        elif not any(alias.name == "pass_context" for alias in jinja_import.names):
            jinja_import.names.append(ast.alias(name="pass_context"))
        adapter = ast.parse(
            "@pass_context\n"
            "def _portage_url_for(context, name, /, **path_params):\n"
            f"    if name == {static_mount['name']!r} and 'filename' in path_params:\n"
            "        path_params.setdefault('path', path_params.pop('filename'))\n"
            "    external = bool(path_params.pop('_external', False))\n"
            "    url = context['request'].url_for(name, **path_params)\n"
            "    return str(url) if external else url.path\n"
        ).body[0]
        tree.body.append(adapter)
        tree.body.extend(
            ast.parse(
                f"{name}.env.globals['url_for'] = _portage_url_for"
            ).body[0]
            for name in sorted(template_names)
        )
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_invalid_signature_order(content: str) -> str:
    """Move defaulted injected positional parameters behind required ones."""
    try:
        ast.parse(content)
        return content
    except SyntaxError as exc:
        if not (
            "non-default argument follows default argument" in exc.msg
            or "parameter without a default follows parameter with a default" in exc.msg
        ):
            return content
    tokens = list(tokenize.generate_tokens(io.StringIO(content).readline))
    pairs = [(token.type, token.string) for token in tokens]
    changed = False
    index = 0
    while index < len(pairs):
        if pairs[index] != (tokenize.NAME, "def"):
            index += 1
            continue
        opening = next((
            cursor for cursor in range(index + 1, len(pairs))
            if pairs[cursor] == (tokenize.OP, "(")
        ), None)
        if opening is None:
            break
        depth = 0
        closing = None
        for cursor in range(opening, len(pairs)):
            if pairs[cursor] == (tokenize.OP, "("):
                depth += 1
            elif pairs[cursor] == (tokenize.OP, ")"):
                depth -= 1
                if depth == 0:
                    closing = cursor
                    break
        if closing is None:
            break
        segments: list[list[tuple[int, str]]] = [[]]
        nested = 0
        for token in pairs[opening + 1:closing]:
            if token[0] == tokenize.OP and token[1] in "([{":
                nested += 1
            elif token[0] == tokenize.OP and token[1] in ")]}":
                nested -= 1
            if token == (tokenize.OP, ",") and nested == 0:
                segments.append([])
            else:
                segments[-1].append(token)
        trailing = bool(segments and not segments[-1])
        values = segments[:-1] if trailing else segments
        boundary = next((
            position for position, segment in enumerate(values)
            if any(token == (tokenize.OP, "/") for token in segment)
            or next((token for token in segment if token[0] not in {
                tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
            }), None) in {(tokenize.OP, "*"), (tokenize.OP, "**")}
        ), len(values))
        positional = values[:boundary]
        seen_default = False
        invalid = False
        for segment in positional:
            has_default = any(token == (tokenize.OP, "=") for token in segment)
            invalid = invalid or seen_default and not has_default
            seen_default = seen_default or has_default
        if invalid:
            required = [
                segment for segment in positional
                if not any(token == (tokenize.OP, "=") for token in segment)
            ]
            defaulted = [
                segment for segment in positional
                if any(token == (tokenize.OP, "=") for token in segment)
            ]
            values = [*required, *defaulted, *values[boundary:]]
            rebuilt: list[tuple[int, str]] = []
            for position, segment in enumerate(values):
                if position:
                    rebuilt.append((tokenize.OP, ","))
                rebuilt.extend(segment)
            if trailing:
                rebuilt.append((tokenize.OP, ","))
            pairs[opening + 1:closing] = rebuilt
            changed = True
            index = opening + len(rebuilt) + 2
        else:
            index = closing + 1
    if not changed:
        return content
    candidate = tokenize.untokenize(pairs)
    try:
        ast.parse(candidate)
    except SyntaxError:
        return content
    return candidate


def _realize_binding_closure(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "binding_closure" and item.get("path") == path
    ), None)
    if decision is None or _parsed(content) is None:
        return content

    def undefined(source: str) -> set[str]:
        root = symtable.symtable(source, path, "exec")
        tables = []
        stack = [root]
        while stack:
            table = stack.pop()
            tables.append(table)
            stack.extend(table.get_children())
        bound = {
            symbol.get_name() for table in tables for symbol in table.get_symbols()
            if symbol.is_global() and (
                symbol.is_assigned() or symbol.is_imported() or symbol.is_parameter()
            )
        }
        return {
            symbol.get_name() for table in tables for symbol in table.get_symbols()
            if symbol.is_referenced() and symbol.is_global()
            and symbol.get_name() not in bound
        }

    tree = _parsed(content)
    assert tree is not None
    changed = False
    import_at = _module_import_index(tree)
    for _ in range(2):
        missing = undefined(ast.unparse(tree))
        if not missing:
            break
        for name, source in decision.get("helpers", {}).items():
            if name not in missing:
                continue
            helper = _parsed(source)
            if helper is not None:
                tree.body.extend(helper.body)
                changed = True
        missing = undefined(ast.unparse(tree))
        for item in decision.get("imports", []):
            wanted = missing & set(item.get("bindings", []))
            if not wanted or (parsed := _parsed(item["source"])) is None:
                continue
            statement = parsed.body[0]
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                statement.names = [
                    alias for alias in statement.names
                    if (alias.asname or (
                        alias.name.split(".")[0]
                        if isinstance(statement, ast.Import) else alias.name
                    )) in wanted
                ]
                tree.body.insert(import_at, statement)
                import_at += 1
                changed = True
    request_helpers = decision.get("request_helpers", {})
    runtime_provider = decision.get("runtime_provider", "")
    if request_helpers and runtime_provider:
        imported = next((
            statement for statement in tree.body
            if isinstance(statement, ast.ImportFrom)
            and _resolve_module(statement.module, statement.level, path)
            in _module_names(runtime_provider)
        ), None)
        if imported is None:
            imported = ast.ImportFrom(
                module=runtime_provider.removesuffix(".py").replace("/", "."),
                names=[], level=0,
            )
            tree.body.insert(import_at, imported)
            import_at += 1
        accessor = next((
            alias.asname or alias.name for alias in imported.names
            if alias.name == "get_request_context"
        ), "")
        if not accessor:
            accessor = "_portage_get_request_context"
            imported.names.append(ast.alias(
                name="get_request_context", asname=accessor,
            ))

        class ReplaceRequest(ast.NodeTransformer):
            def __init__(self, names: set[str]):
                self.names = names

            def visit_Name(self, node):  # noqa: N802
                if isinstance(node.ctx, ast.Load) and node.id in self.names:
                    return ast.Subscript(
                        value=ast.Call(
                            func=ast.Name(id=accessor, ctx=ast.Load()),
                            args=[], keywords=[],
                        ),
                        slice=ast.Constant("request"), ctx=node.ctx,
                    )
                return node

        for function in tree.body:
            if (
                isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                and function.name in request_helpers
            ):
                ReplaceRequest(set(request_helpers[function.name])).visit(function)
                changed = True
    definitions = {
        statement.name for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    bound = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    for fact in decision.get("module_member_calls", []):
        member = fact["member"]
        imported = next((
            alias
            for statement in tree.body if isinstance(statement, ast.ImportFrom)
            and _resolve_module(statement.module, statement.level, path)
            in _module_names(fact["target"])
            for alias in statement.names
            if alias.name == member and (alias.asname or alias.name) in definitions
        ), None)
        if imported is None:
            continue
        local = imported.asname or imported.name
        alias_name = f"_portage_{member}"
        suffix = 2
        while alias_name in bound:
            alias_name = f"_portage_{member}_{suffix}"
            suffix += 1
        imported.asname = alias_name
        bound.add(alias_name)

        class ReplaceCalls(ast.NodeTransformer):
            def __init__(self, old: str, new: str):
                self.old = old
                self.new = new

            def visit_Call(self, node):  # noqa: N802
                node = self.generic_visit(node)
                if isinstance(node.func, ast.Name) and node.func.id == self.old:
                    node.func.id = self.new
                return node

        for function in tree.body:
            if (
                isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                and function.name in fact["functions"]
            ):
                replace = ReplaceCalls(local, alias_name)
                function.body = [replace.visit(item) for item in function.body]
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_provider_export_topology(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Replace ``direct_export.sibling`` with the sibling's frozen module export."""
    decisions = [
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "planned_provider_exports"
        and item.get("provider") != path and path in item.get("files", [])
    ]
    tree = _parsed(content)
    if not decisions or tree is None:
        return content
    providers = {item["provider"]: item["exports"] for item in decisions}

    def provider_for(module: str) -> str | None:
        matches = [owner for owner in providers if module in _module_names(owner)]
        return matches[0] if len(matches) == 1 else None

    direct: dict[str, tuple[str, str]] = {}
    imports: dict[str, ast.ImportFrom] = {}
    imported_exports: dict[tuple[str, str], str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom):
            continue
        owner = provider_for(_resolve_module(statement.module, statement.level, path))
        if owner is None:
            continue
        imports[owner] = statement
        for alias in statement.names:
            local = alias.asname or alias.name
            if alias.name in providers[owner]:
                direct[local] = (owner, alias.name)
                imported_exports[(owner, alias.name)] = local
    if not direct:
        return content
    bound = {
        name
        for statement in tree.body
        for name in (
            [statement.name]
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else [alias.asname or alias.name for alias in statement.names]
            if isinstance(statement, ast.ImportFrom)
            else [alias.asname or alias.name.split(".")[0] for alias in statement.names]
            if isinstance(statement, ast.Import)
            else [target.id for target in statement.targets if isinstance(target, ast.Name)]
            if isinstance(statement, ast.Assign)
            else [statement.target.id]
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            else []
        )
    }
    replacements: dict[tuple[str, str], str] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id in direct
        ):
            continue
        owner, source_export = direct[node.value.id]
        if (
            node.attr not in providers[owner]
            or node.attr in providers[owner][source_export].get("members", [])
        ):
            continue
        key = (owner, node.attr)
        if key in replacements:
            continue
        local = imported_exports.get(key)
        if local is None:
            local = node.attr
            if local in bound:
                local = f"_portage_{node.attr}"
                suffix = 2
                while local in bound:
                    local = f"_portage_{node.attr}_{suffix}"
                    suffix += 1
            imports[owner].names.append(ast.alias(
                name=node.attr,
                asname=local if local != node.attr else None,
            ))
            bound.add(local)
            imported_exports[key] = local
        replacements[key] = local
    if not replacements:
        return content

    class Realize(ast.NodeTransformer):
        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802
            node = self.generic_visit(node)
            if not isinstance(node.value, ast.Name) or node.value.id not in direct:
                return node
            owner, source_export = direct[node.value.id]
            if node.attr in providers[owner][source_export].get("members", []):
                return node
            local = replacements.get((owner, node.attr))
            return ast.Name(id=local, ctx=node.ctx) if local else node

    tree = Realize().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_project_import_levels(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Correct a relative import that points below the current package by one level."""
    modules = set((seam_plan or {}).get("project_modules", []))
    tree = _parsed(content)
    if not modules or tree is None:
        return content
    package = path.removesuffix(".py").split("/")[:-1]
    changed = False
    for statement in tree.body:
        if not (
            isinstance(statement, ast.ImportFrom)
            and statement.level and statement.module
            and _resolve_module(statement.module, statement.level, path) not in modules
        ):
            continue
        suffix = statement.module.split(".")
        candidate = next((
            ".".join([*package[:depth], *suffix])
            for depth in range(len(package) - 1, -1, -1)
            if ".".join([*package[:depth], *suffix]) in modules
        ), "")
        if candidate:
            statement.module = candidate
            statement.level = 0
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_test_adapter_instance_path(
    content: str, seam_plan: dict | None,
) -> str:
    if not any(
        item.get("kind") == "application_factory"
        and "instance_path" in item.get("app_state_members", [])
        for item in (seam_plan or {}).get("decisions", {}).values()
    ):
        return content
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
    for call in (
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).split(".")[-1] == "adapt_app"
    ):
        keyword = next((item for item in call.keywords if item.arg == "instance_path"), None)
        if not (
            keyword and isinstance(keyword.value, ast.Attribute)
            and keyword.value.attr == "instance_path"
            and not (
                isinstance(keyword.value.value, ast.Attribute)
                and keyword.value.value.attr == "state"
            )
        ):
            continue
        keyword.value.value = ast.Attribute(
            value=keyword.value.value, attr="state", ctx=ast.Load(),
        )
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_ambient_request_binding(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Keep the active Request in the shared ambient-context mapping."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "ambient_context_runtime"
        and (
            path in item.get("runtime_providers", [])
            or path in item.get("current_app_consumers", [])
        )
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content

    changed = False
    if path in decision.get("current_app_consumers", []):
        app_bindings = {
            target.id
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1] == "create_app"
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }

        class CurrentAppRewriter(ast.NodeTransformer):
            def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802
                if isinstance(node.ctx, ast.Load) and node.id in app_bindings:
                    return ast.Name(id="current_app", ctx=node.ctx)
                return node

            def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802
                node = self.generic_visit(node)
                if (
                    node.attr == "config"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "g"
                ):
                    node.value = ast.Name(id="current_app", ctx=ast.Load())
                return node

        tree = CurrentAppRewriter().visit(tree)

        class InstancePathRewriter(ast.NodeTransformer):
            def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802
                node = self.generic_visit(node)
                if (
                    node.attr == "instance_path"
                    and not (
                        isinstance(node.value, ast.Name)
                        and node.value.id == "current_app"
                    )
                    and not (
                        isinstance(node.value, ast.Attribute)
                        and node.value.attr == "state"
                    )
                ):
                    node.value = ast.Attribute(
                        value=node.value, attr="state", ctx=ast.Load(),
                    )
                return node

        tree = InstancePathRewriter().visit(tree)
        tree.body = [
            statement for statement in tree.body
            if not (
                isinstance(statement, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id in app_bindings
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                )
            )
        ]
        needs_proxy = bool(app_bindings) or any(
            isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            and node.id == "current_app" for node in ast.walk(tree)
        )
        for statement in list(tree.body):
            if isinstance(statement, ast.ImportFrom):
                statement.names = [
                    alias for alias in statement.names
                    if alias.name != "current_app"
                    and not (
                        alias.name == "create_app"
                        and not any(
                            isinstance(node, ast.Name)
                            and isinstance(node.ctx, ast.Load)
                            and node.id == (alias.asname or alias.name)
                            for node in ast.walk(tree)
                        )
                    )
                ]
                if not statement.names:
                    tree.body.remove(statement)
        providers = decision.get("runtime_providers", [])
        if needs_proxy and len(providers) == 1:
            provider_path = providers[0]
            imported = next((
                statement for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and _resolve_module(statement.module, statement.level, path)
                in _module_names(provider_path)
            ), None)
            if imported is None:
                imported = ast.ImportFrom(
                    module=provider_path.removesuffix(".py").replace("/", "."),
                    names=[], level=0,
                )
                tree.body.insert(_module_import_index(tree), imported)
            if not any(alias.name == "current_app" for alias in imported.names):
                imported.names.append(ast.alias(name="current_app"))
        changed = needs_proxy
    runtime_classes = set(decision.get("runtime_classes", {}).get(path, []))
    for cls in (
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in runtime_classes
    ):
        for function in (
            node for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"dispatch", "__call__"}
        ):
            positional = [*function.args.posonlyargs, *function.args.args]
            request_name = next(
                (argument.arg for argument in positional if argument.arg != "self"), "",
            )
            if not request_name:
                continue
            for mapping in (
                node for node in ast.walk(function) if isinstance(node, ast.Dict)
            ):
                pairs = list(zip(mapping.keys, mapping.values, strict=True))
                if not any(
                    isinstance(key, ast.Constant) and key.value == "session"
                    and isinstance(value, ast.Attribute) and value.attr == "session"
                    and isinstance(value.value, ast.Name)
                    and value.value.id == request_name
                    for key, value in pairs
                ) or any(
                    isinstance(key, ast.Constant) and key.value == "request"
                    for key in mapping.keys
                ):
                    continue
                mapping.keys.append(ast.Constant("request"))
                mapping.values.append(ast.Name(id=request_name, ctx=ast.Load()))
                changed = True

    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_decorated_provider_protocols(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Realize missing or stubbed direct-decorator provider members."""
    protocols = [
        decision
        for decision in (seam_plan or {}).get("decisions", {}).values()
        if decision.get("kind") == "provider_protocol"
        and decision.get("provider") == path
    ]
    tree = _parsed(content)
    if tree is None or not protocols:
        return content

    changed = False
    for protocol in protocols:
        symbol = protocol["symbol"]
        assignment = next((
            statement for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == symbol
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ), None)
        if assignment is None:
            continue
        existing = {
            target.attr
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name) and target.value.id == symbol
        }
        value = assignment.value
        class_name = (
            value.func.id
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            else ""
        )
        provider_class = next((
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ), None)
        decorator_members = set(protocol.get("decorator_members", []))
        callable_members = set(protocol.get("callable_members", []))
        context_members = set(protocol.get("context_manager_members", []))
        exception_members = set(protocol.get("exception_members", []))
        for exception_name in sorted(exception_members):
            if any(
                isinstance(node, ast.ClassDef) and node.name == exception_name
                for node in tree.body
            ):
                continue
            description = exception_name.removesuffix("Error").replace("_", " ")
            tree.body.insert(tree.body.index(assignment), ast.parse(
                f"class {exception_name}(Exception):\n"
                "    def __init__(self, description=None):\n"
                f"        self.description = description or {description!r}\n"
                "        super().__init__(self.description)\n"
            ).body[0])
            changed = True
        if provider_class is None and protocol.get("constructor"):
            helper_class = (
                f"_Portage{''.join(part.title() for part in symbol.split('_'))}Provider"
            )
            attributes = protocol.get("attribute_values", {})
            lines = [
                f"class {helper_class}:",
                "    def __init__(self):",
                "        self._apps = []",
                "        self._outboxes = []",
                "        self._calls = []",
                *(
                    f"        self.{name} = {value!r}"
                    for name, value in sorted(attributes.items())
                ),
            ]
            for member in sorted(decorator_members):
                lines.extend([
                    f"    def {member}(self, callback):",
                    f"        self._{member} = callback",
                    "        return callback",
                ])
            for member in sorted(callable_members - decorator_members):
                if member == "init_app":
                    lines.extend([
                        "    def init_app(self, app, *args, **kwargs):",
                        "        self._apps.append((app, args, kwargs))",
                        "        return app",
                    ])
                elif member == "send":
                    lines.extend([
                        "    def send(self, message):",
                        "        for outbox in tuple(self._outboxes):",
                        "            outbox.append(message)",
                        "        return message",
                    ])
                elif member == "Message":
                    lines.extend([
                        "    def Message(self, **values):",
                        "        from types import SimpleNamespace",
                        "        return SimpleNamespace(**values)",
                    ])
                elif member in context_members:
                    lines.extend([
                        "    @contextmanager",
                        f"    def {member}(self):",
                        "        captured = []",
                        "        self._outboxes.append(captured)",
                        "        try:",
                        "            yield captured",
                        "        finally:",
                        "            self._outboxes.remove(captured)",
                    ])
                else:
                    lines.extend([
                        f"    def {member}(self, *args, **kwargs):",
                        f"        self._calls.append(({member!r}, args, kwargs))",
                        "        return args[0] if args else None",
                    ])
            provider_class = ast.parse("\n".join(lines) + "\n").body[0]
            insertion = tree.body.index(assignment)
            tree.body.insert(insertion, provider_class)
            assignment.value = ast.Call(
                func=ast.Name(id=helper_class, ctx=ast.Load()), args=[], keywords=[],
            )
            if context_members and not any(
                isinstance(node, ast.ImportFrom) and node.module == "contextlib"
                and any(alias.name == "contextmanager" for alias in node.names)
                for node in tree.body
            ):
                tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                    module="contextlib", names=[ast.alias(name="contextmanager")],
                    level=0,
                ))
            value = assignment.value
            class_name = helper_class
            changed = True
        if provider_class is not None:
            csrf_protocol = (
                protocol.get("constructor", {}).get("module") == "flask_wtf.csrf"
                and protocol.get("constructor", {}).get("name") == "CSRFProtect"
            )
            if csrf_protocol:
                error_name = next(iter(sorted(exception_members)), "")
                rejection = (
                    f"                error = {error_name}()\n"
                    f"                handler = app.exception_handlers.get({error_name})\n"
                    "                if handler is not None:\n"
                    "                    response = handler(request, error)\n"
                    "                    if isawaitable(response):\n"
                    "                        return await response\n"
                    "                    return response\n"
                    if error_name else ""
                ) + (
                    "                return PlainTextResponse(\n"
                    "                    'The CSRF token is missing or invalid.', status_code=400\n"
                    "                )\n"
                )
                source = (
                    "def init_app(self, app, *args, **kwargs):\n"
                    "    from hmac import compare_digest\n"
                    "    from inspect import isawaitable\n"
                    "    from starlette.responses import PlainTextResponse\n"
                    "    apps = getattr(self, '_apps', [])\n"
                    "    apps.append((app, args, kwargs))\n"
                    "    self._apps = apps\n"
                    "    @app.middleware('http')\n"
                    "    async def _portage_csrf_protect(request, call_next):\n"
                    "        config = getattr(request.app.state, 'config', {})\n"
                    "        enabled = config.get('WTF_CSRF_ENABLED', True)\n"
                    "        if enabled and request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:\n"
                    "            await request.body()\n"
                    "            form = await request.form()\n"
                    "            supplied = str(form.get('csrf_token') or '')\n"
                    "            expected = str(request.session.get('_csrf_token') or '')\n"
                    "            if not expected or not compare_digest(supplied, expected):\n"
                    f"{rejection}"
                    "        return await call_next(request)\n"
                    "    return app\n"
                )
                provider_class.body = [
                    node for node in provider_class.body
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    or node.name != "init_app"
                ]
                provider_class.body.append(ast.parse(source).body[0])
                changed = True
            for index, method in enumerate(provider_class.body):
                if not (
                    isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and method.name in decorator_members
                ):
                    continue
                callback_alias = (
                    "    self._user_callback = callback\n"
                    if method.name == "user_loader" else ""
                )
                provider_class.body[index] = ast.parse(
                    f"def {method.name}(self, callback):\n"
                    f"    self._{method.name} = callback\n"
                    f"{callback_alias}"
                    "    return callback\n"
                ).body[0]
                changed = True
            needs_contextmanager = False
            for index, method in enumerate(provider_class.body):
                if not (
                    isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and method.name in decorator_members | callable_members
                    and all(
                        isinstance(statement, ast.Pass)
                        or isinstance(statement, ast.Return)
                        and (
                            statement.value is None
                            or isinstance(statement.value, ast.Constant)
                            and statement.value.value is None
                        )
                        for statement in method.body
                    )
                ):
                    continue
                member = method.name
                if member in decorator_members:
                    source = (
                        f"def {member}(self, callback):\n"
                        f"    self._{member} = callback\n"
                        "    return callback\n"
                    )
                elif member == "init_app":
                    source = (
                        "def init_app(self, app, *args, **kwargs):\n"
                        "    apps = getattr(self, '_apps', [])\n"
                        "    apps.append((app, args, kwargs))\n"
                        "    self._apps = apps\n"
                        "    return app\n"
                    )
                elif member == "Message":
                    source = (
                        "def Message(self, **values):\n"
                        "    from types import SimpleNamespace\n"
                        "    return SimpleNamespace(**values)\n"
                    )
                elif member in context_members:
                    source = (
                        "@contextmanager\n"
                        f"def {member}(self):\n"
                        "    captured = []\n"
                        "    outboxes = getattr(self, '_outboxes', [])\n"
                        "    outboxes.append(captured)\n"
                        "    self._outboxes = outboxes\n"
                        "    try:\n"
                        "        yield captured\n"
                        "    finally:\n"
                        "        outboxes.remove(captured)\n"
                    )
                    needs_contextmanager = True
                else:
                    source = (
                        f"def {member}(self, *args, **kwargs):\n"
                        "    calls = getattr(self, '_calls', [])\n"
                        f"    calls.append(({member!r}, args, kwargs))\n"
                        "    self._calls = calls\n"
                        "    return args[0] if args else None\n"
                    )
                provider_class.body[index] = ast.parse(source).body[0]
                changed = True
            if needs_contextmanager and not any(
                isinstance(node, ast.ImportFrom) and node.module == "contextlib"
                and any(alias.name == "contextmanager" for alias in node.names)
                for node in tree.body
            ):
                tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                    module="contextlib", names=[ast.alias(name="contextmanager")],
                    level=0,
                ))
            existing.update(
                node.name for node in provider_class.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            existing.update(
                target.id
                for statement in provider_class.body
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            )
        attribute_members = set(protocol.get("attribute_members", []))
        attribute_values = protocol.get("attribute_values", {})
        provider_statements = [
            (statement, False) for statement in tree.body
        ] + [
            (statement, True) for statement in (
                provider_class.body if provider_class is not None else []
            )
        ]
        for statement, class_level in provider_statements:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                member = (
                    target.id if class_level and isinstance(target, ast.Name)
                    else target.attr if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == symbol
                    ) else ""
                )
                if member in attribute_values:
                    statement.value = ast.Constant(value=attribute_values[member])
                    changed = True
        for callback in protocol.get("callbacks", []):
            original = _parsed(callback["source"])
            if original is None or len(original.body) != 1:
                continue
            replacement = original.body[0]
            existing_callback = next((
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == callback["function"]
            ), None)
            if existing_callback is None:
                tree.body.insert(tree.body.index(assignment) + 1, replacement)
            else:
                tree.body[tree.body.index(existing_callback)] = replacement
            changed = True
        missing = sorted((decorator_members | callable_members) - existing)
        missing_attributes = sorted(attribute_members - existing)
        if not missing and not missing_attributes:
            continue

        if isinstance(value, ast.Call):
            insertion = tree.body.index(assignment) + 1
            additions: list[ast.stmt] = []
            for member in sorted(decorator_members & set(missing)):
                helper = f"_portage_{symbol}_{member}"
                additions.extend([
                    ast.FunctionDef(
                        name=helper,
                        args=ast.arguments(
                            posonlyargs=[], args=[ast.arg(arg="callback")],
                            kwonlyargs=[], kw_defaults=[], defaults=[],
                        ),
                        body=[ast.Return(value=ast.Name(id="callback", ctx=ast.Load()))],
                        decorator_list=[], returns=None, type_comment=None,
                    ),
                    ast.Assign(
                        targets=[ast.Attribute(
                            value=ast.Name(id=symbol, ctx=ast.Load()),
                            attr=member, ctx=ast.Store(),
                        )],
                        value=ast.Name(id=helper, ctx=ast.Load()),
                    ),
                ])
            for member in sorted(callable_members - decorator_members - existing):
                additions.append(ast.FunctionDef(
                    name=f"_portage_{symbol}_{member}",
                    args=ast.arguments(
                        posonlyargs=[], args=[], vararg=ast.arg(arg="args"),
                        kwonlyargs=[], kw_defaults=[], kwarg=ast.arg(arg="kwargs"),
                        defaults=[],
                    ),
                    body=[ast.Return(value=ast.Constant(value=None))],
                    decorator_list=[], returns=None, type_comment=None,
                ))
                additions.append(ast.Assign(
                    targets=[ast.Attribute(
                        value=ast.Name(id=symbol, ctx=ast.Load()),
                        attr=member, ctx=ast.Store(),
                    )],
                    value=ast.Name(
                        id=f"_portage_{symbol}_{member}", ctx=ast.Load(),
                    ),
                ))
            additions.extend(
                ast.Assign(
                    targets=[ast.Attribute(
                        value=ast.Name(id=symbol, ctx=ast.Load()),
                        attr=member, ctx=ast.Store(),
                    )],
                    value=ast.Constant(value=attribute_values.get(member)),
                )
                for member in missing_attributes
            )
            tree.body[insertion:insertion] = additions
        else:
            helper_class = f"_Portage{''.join(part.title() for part in symbol.split('_'))}Protocol"
            class_node = ast.ClassDef(
                name=helper_class, bases=[], keywords=[], decorator_list=[],
                body=[
                    ast.FunctionDef(
                        name=member,
                        args=ast.arguments(
                            posonlyargs=[],
                            args=[ast.arg(arg="self"), ast.arg(arg="callback")],
                            kwonlyargs=[], kw_defaults=[], defaults=[],
                        ),
                        body=[ast.Return(value=ast.Name(id="callback", ctx=ast.Load()))],
                        decorator_list=[], returns=None, type_comment=None,
                    )
                    for member in sorted(decorator_members & set(missing))
                ] + [
                    ast.FunctionDef(
                        name=member,
                        args=ast.arguments(
                            posonlyargs=[], args=[ast.arg(arg="self")],
                            vararg=ast.arg(arg="args"), kwonlyargs=[],
                            kw_defaults=[], kwarg=ast.arg(arg="kwargs"), defaults=[],
                        ),
                        body=[ast.Return(value=ast.Constant(value=None))],
                        decorator_list=[], returns=None, type_comment=None,
                    )
                    for member in sorted(callable_members - decorator_members - existing)
                ] + [
                    ast.Assign(
                        targets=[ast.Name(id=member, ctx=ast.Store())],
                        value=ast.Constant(value=attribute_values.get(member)),
                    )
                    for member in missing_attributes
                ],
            )
            insertion = tree.body.index(assignment)
            tree.body.insert(insertion, class_node)
            assignment.value = ast.Call(
                func=ast.Name(id=helper_class, ctx=ast.Load()), args=[], keywords=[],
            )
        changed = True

    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_dynamic_instance_exports(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Make model-generated ``type`` facades honor frozen instance/decorator shape."""
    contracts = {
        item["symbol"]: item
        for decision in (seam_plan or {}).get("decisions", {}).values()
        if decision.get("kind") == "application_factory"
        and decision.get("factory") == path
        for item in decision.get("instance_exports", [])
    }
    if not contracts:
        return content
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
    receiver_names = {"self", "cls", "_self", "_", "instance"}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        symbol = next((
            target.id for target in targets
            if isinstance(target, ast.Name) and target.id in contracts
        ), "")
        if not symbol:
            continue
        value = statement.value
        constructor = (
            value if isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name) and value.func.id == "type"
            else value.func if isinstance(value, ast.Call)
            and isinstance(value.func, ast.Call)
            and isinstance(value.func.func, ast.Name) and value.func.func.id == "type"
            else None
        )
        if not (
            isinstance(constructor, ast.Call) and len(constructor.args) == 3
            and isinstance(constructor.args[2], ast.Dict)
        ):
            continue
        if constructor is value:
            statement.value = ast.Call(func=constructor, args=[], keywords=[])
            changed = True
        mapping = constructor.args[2]
        pairs = {
            key.value: index
            for index, key in enumerate(mapping.keys)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        for member in contracts[symbol].get("decorator_members", []):
            index = pairs.get(member)
            if index is None:
                mapping.keys.append(ast.Constant(value=member))
                mapping.values.append(ast.Lambda(
                    args=ast.arguments(
                        posonlyargs=[], args=[ast.arg(arg="self"), ast.arg(arg="callback")],
                        kwonlyargs=[], kw_defaults=[], defaults=[],
                    ),
                    body=ast.Name(id="callback", ctx=ast.Load()),
                ))
                changed = True
                continue
            member_value = mapping.values[index]
            if isinstance(member_value, ast.Constant) and member_value.value is None or (
                isinstance(member_value, ast.Lambda)
                and isinstance(member_value.body, ast.Constant)
                and member_value.body.value is None
            ):
                mapping.values[index] = ast.Lambda(
                    args=ast.arguments(
                        posonlyargs=[], args=[ast.arg(arg="self"), ast.arg(arg="callback")],
                        kwonlyargs=[], kw_defaults=[], defaults=[],
                    ),
                    body=ast.Name(id="callback", ctx=ast.Load()),
                )
                changed = True
        for member_value in mapping.values:
            if not isinstance(member_value, ast.Lambda) or member_value.args.vararg:
                continue
            positional = [*member_value.args.posonlyargs, *member_value.args.args]
            if positional and positional[0].arg in receiver_names:
                continue
            target = (
                member_value.args.posonlyargs
                if member_value.args.posonlyargs else member_value.args.args
            )
            target.insert(0, ast.arg(arg="self"))
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_extension_provider_order(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Place consumer imports after the provider object they import back from."""
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
    for decision in (seam_plan or {}).get("decisions", {}).values():
        if decision.get("kind") != "extension_provider" or decision.get(
            "provider"
        ) != path:
            continue
        symbol = decision["symbol"]
        provider = next((
            statement for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == symbol
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ), None)
        if provider is None:
            continue
        provider_index = tree.body.index(provider)
        consumers = decision.get("consumers", [])
        lazy_consumers = decision.get("lazy_consumers", [])
        for statement in list(tree.body):
            modules = []
            local_names: set[str] = set()
            if isinstance(statement, ast.ImportFrom):
                base = _resolve_module(statement.module, statement.level, path)
                modules = [base, *(
                    f"{base}.{alias.name}".lstrip(".") for alias in statement.names
                )]
                local_names = {alias.asname or alias.name for alias in statement.names}
            elif isinstance(statement, ast.Import):
                modules = [alias.name for alias in statement.names]
                local_names = {
                    alias.asname or alias.name.split(".")[0]
                    for alias in statement.names
                }
            consumer = next((
                consumer for consumer in lazy_consumers
                if any(module in _module_names(consumer) for module in modules)
            ), None)
            if consumer is None or not local_names:
                continue
            users = [
                function for function in tree.body
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(
                    isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in local_names for node in ast.walk(function)
                )
            ]
            outside_use = any(
                isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id in local_names
                for top in tree.body
                if top is not statement and top not in users
                for node in ast.walk(top)
            )
            if not users or outside_use:
                continue
            tree.body.remove(statement)
            for function in users:
                function.body.insert(0, deepcopy(statement))
            changed = True
        provider_index = tree.body.index(provider)
        early = []
        for statement in tree.body[:provider_index]:
            modules = []
            if isinstance(statement, ast.ImportFrom):
                base = _resolve_module(statement.module, statement.level, path)
                modules = [base, *(
                    f"{base}.{alias.name}".lstrip(".") for alias in statement.names
                )]
            elif isinstance(statement, ast.Import):
                modules = [alias.name for alias in statement.names]
            if any(
                module in _module_names(consumer)
                for consumer in consumers for module in modules
            ):
                early.append(statement)
        if not early:
            continue
        for statement in early:
            tree.body.remove(statement)
        provider_index = tree.body.index(provider)
        tree.body[provider_index + 1:provider_index + 1] = early
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_cli_factory(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Remove Flask-only command registration when the app-owned facade captures it."""
    decision = (seam_plan or {}).get("decisions", {}).get("standalone_cli", {})
    registrars = decision.get("registrars", [])
    if path not in decision.get("factory_files", []) or not registrars:
        return content
    tree = _parsed(content)
    if tree is None:
        return content
    required_initializers = {
        (initializer["provider"], initializer["symbol"])
        for factory in (seam_plan or {}).get("decisions", {}).values()
        if factory.get("kind") == "application_factory"
        and factory.get("factory") == path
        for initializer in factory.get("initializers", [])
    }
    registrar_names = set()
    for statement in ast.walk(tree):
        if not isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        for registrar in registrars:
            if (registrar["module"], registrar["function"]) in required_initializers:
                continue
            wanted = _module_names(registrar["module"])
            function = registrar["function"]
            if isinstance(statement, ast.ImportFrom):
                module = _resolve_module(statement.module, statement.level, path)
                if module in wanted:
                    registrar_names.update(
                        alias.asname or alias.name
                        for alias in statement.names if alias.name == function
                    )
                for alias in statement.names:
                    if f"{module}.{alias.name}".lstrip(".") in wanted:
                        registrar_names.add(f"{alias.asname or alias.name}.{function}")
            else:
                for alias in statement.names:
                    if alias.name in wanted:
                        registrar_names.add(f"{alias.asname or alias.name}.{function}")
    changed = False
    for function in (
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        kept = []
        for statement in function.body:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and ast.unparse(statement.value.func) in registrar_names
            ):
                changed = True
                continue
            kept.append(statement)
        function.body = kept
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_extension_provider_facade(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Complete mapping/class facades with the frozen SQLAlchemy object surface."""
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False

    def imported_name(module: str, name: str, asname: str | None = None) -> str:
        nonlocal changed
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module == module:
                for alias in statement.names:
                    if alias.name == name:
                        return alias.asname or alias.name
                statement.names.append(ast.alias(name=name, asname=asname))
                changed = True
                return asname or name
        insert_at = 1 if (
            tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ) else 0
        while (
            insert_at < len(tree.body)
            and isinstance(tree.body[insert_at], ast.ImportFrom)
            and tree.body[insert_at].module == "__future__"
        ):
            insert_at += 1
        tree.body.insert(insert_at, ast.ImportFrom(
            module=module, names=[ast.alias(name=name, asname=asname)], level=0,
        ))
        changed = True
        return asname or name

    for decision in (seam_plan or {}).get("decisions", {}).values():
        if decision.get("kind") != "extension_provider" or decision.get(
            "provider"
        ) != path:
            continue
        symbol = decision["symbol"]
        assignment = next((
            statement for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == symbol
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ), None)
        if assignment is None:
            continue
        required = set(decision.get("members", []))
        fallback_model = next((
            target.id
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1]
            in {"declarative_base", "DeclarativeBase"}
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        ), "")
        fallback_model = fallback_model or next((
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
            and any(
                ast.unparse(base).split(".")[-1] == "DeclarativeBase"
                for base in node.bases
            )
        ), "")
        database_config = decision.get("database_config", {})
        engine_factory = ""
        engine_helper = None
        if "init_app" in required or database_config.get("sqlite"):
            engine_factory = "_portage_create_engine"
            raw_create_engine = imported_name("sqlalchemy", "create_engine")
            static_pool = imported_name("sqlalchemy.pool", "StaticPool")
            engine_helper = next((
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == engine_factory
            ), None)
            if engine_helper is None:
                engine_helper = ast.parse(
                    f"def {engine_factory}(uri):\n"
                    "    value = str(uri)\n"
                    "    kwargs = {}\n"
                    "    if value.startswith('sqlite'):\n"
                    "        kwargs['connect_args'] = {'check_same_thread': False}\n"
                    "        if value in {'sqlite://', 'sqlite:///:memory:'}:\n"
                    f"            kwargs['poolclass'] = {static_pool}\n"
                    f"    return {raw_create_engine}(uri, **kwargs)\n"
                ).body[0]
                first_definition = next((
                    index for index, node in enumerate(tree.body)
                    if isinstance(
                        node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                ), len(tree.body))
                tree.body.insert(first_definition, engine_helper)
                changed = True
        elif database_config:
            engine_factory = imported_name("sqlalchemy", "create_engine")
        if database_config:
            config_name = imported_name(
                database_config["module"], database_config["symbol"],
            )
            uri = ast.Attribute(
                value=ast.Subscript(
                    value=ast.Name(id=config_name, ctx=ast.Load()),
                    slice=ast.Constant(database_config["default_key"]),
                    ctx=ast.Load(),
                ),
                attr="SQLALCHEMY_DATABASE_URI", ctx=ast.Load(),
            )
            for statement in tree.body:
                if not (
                    isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and isinstance(statement.value, ast.Call)
                    and statement.value.args
                    and "engine" in ast.unparse(statement.value.func).lower()
                ):
                    continue
                statement.value.args[0] = deepcopy(uri)
                changed = True
            for call in (
                node for statement in tree.body if statement is not engine_helper
                for node in ast.walk(statement)
                if isinstance(node, ast.Call)
                and ast.unparse(node.func).split(".")[-1] == "create_engine"
                and node.args
            ):
                call.func = ast.Name(id=engine_factory, ctx=ast.Load())
                call.args[0] = deepcopy(uri)
                call.keywords = []
                changed = True
        module_engine_assignment = next((
            statement for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and "engine" in ast.unparse(statement.value.func).lower()
            and any(
                isinstance(target, ast.Name)
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ), None)
        module_engine = next((
            target.id
            for target in (
                module_engine_assignment.targets
                if isinstance(module_engine_assignment, ast.Assign)
                else [module_engine_assignment.target]
                if isinstance(module_engine_assignment, ast.AnnAssign)
                else []
            )
            if isinstance(target, ast.Name)
        ), "")
        module_session_factory = next((
            target.id
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1] == "sessionmaker"
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        ), "")
        if (
            "init_app" in required and not database_config
            and module_engine_assignment is not None
        ):
            engine_call = module_engine_assignment.value
            engine_call.func = ast.Name(id=engine_factory, ctx=ast.Load())
            if (
                not engine_call.args
                or not isinstance(engine_call.args[0], ast.Constant)
                or not isinstance(engine_call.args[0].value, str)
            ):
                engine_call.args = [ast.Constant("sqlite://")]
                engine_call.keywords = []
            changed = True
        existing_definitions = {
            node.name for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        runtime_helpers: list[ast.stmt] = []
        if "paginate" in required and "_PortagePagination" not in existing_definitions:
            runtime_helpers.extend(ast.parse(
                "class _PortagePagination:\n"
                "    def __init__(self, items, page, per_page, total):\n"
                "        self.items = items\n"
                "        self.page = page\n"
                "        self.per_page = per_page\n"
                "        self.total = total\n"
                "    @property\n"
                "    def pages(self):\n"
                "        if self.total is None:\n"
                "            return 0\n"
                "        return (self.total + self.per_page - 1) // self.per_page\n"
                "    @property\n"
                "    def has_prev(self):\n"
                "        return self.page > 1\n"
                "    @property\n"
                "    def prev_num(self):\n"
                "        return self.page - 1\n"
                "    @property\n"
                "    def has_next(self):\n"
                "        return self.page < self.pages\n"
                "    @property\n"
                "    def next_num(self):\n"
                "        return self.page + 1\n"
            ).body)
        missing_404_helpers = required & {"first_or_404", "get_or_404"}
        if missing_404_helpers:
            http_exception = imported_name("fastapi", "HTTPException")
            if (
                "get_or_404" in required
                and "_portage_sqlalchemy_get_or_404" not in existing_definitions
            ):
                runtime_helpers.extend(ast.parse(
                    "def _portage_sqlalchemy_get_or_404(\n"
                    "    provider, entity, ident, *, description=None, **kwargs\n"
                    "):\n"
                    "    value = provider.session.get(entity, ident, **kwargs)\n"
                    "    if value is None:\n"
                    f"        raise {http_exception}(status_code=404, "
                    "detail=description or 'Not Found')\n"
                    "    return value\n"
                ).body)
            if (
                "first_or_404" in required
                and "_portage_sqlalchemy_first_or_404" not in existing_definitions
            ):
                runtime_helpers.extend(ast.parse(
                    "def _portage_sqlalchemy_first_or_404(\n"
                    "    provider, statement, *, description=None\n"
                    "):\n"
                    "    value = provider.session.scalars(statement).first()\n"
                    "    if value is None:\n"
                    f"        raise {http_exception}(status_code=404, "
                    "detail=description or 'Not Found')\n"
                    "    return value\n"
                ).body)
        if (
            "paginate" in required
            and "_portage_sqlalchemy_paginate" not in existing_definitions
        ):
            select = imported_name("sqlalchemy", "select")
            func = imported_name("sqlalchemy", "func")
            http_exception = imported_name("fastapi", "HTTPException")
            runtime_helpers.extend(ast.parse(
                "def _portage_sqlalchemy_paginate(\n"
                "    provider, statement, *, page=None, per_page=None,\n"
                "    max_per_page=None, error_out=True, count=True\n"
                "):\n"
                "    try:\n"
                "        page = 1 if page is None else int(page)\n"
                "        per_page = 20 if per_page is None else int(per_page)\n"
                "        if max_per_page is not None:\n"
                "            per_page = min(per_page, int(max_per_page))\n"
                "    except (TypeError, ValueError):\n"
                "        page = per_page = 0\n"
                "    if page < 1 or per_page < 1:\n"
                "        if error_out:\n"
                f"            raise {http_exception}(status_code=404, detail='Not Found')\n"
                "        page = max(page, 1)\n"
                "        per_page = max(per_page, 1)\n"
                "    offset = (page - 1) * per_page\n"
                "    if hasattr(statement, 'all'):\n"
                "        items = list(statement.limit(per_page).offset(offset).all())\n"
                "        total = statement.order_by(None).count() if count else None\n"
                "    else:\n"
                "        items = list(provider.session.scalars(\n"
                "            statement.limit(per_page).offset(offset)\n"
                "        ).all())\n"
                "        if count:\n"
                "            unordered = statement.order_by(None)\n"
                f"            count_statement = {select}({func}.count()).select_from(\n"
                "                unordered.subquery()\n"
                "            )\n"
                "            total = int(provider.session.scalar(count_statement) or 0)\n"
                "        else:\n"
                "            total = None\n"
                "    if not items and page != 1 and error_out:\n"
                f"        raise {http_exception}(status_code=404, detail='Not Found')\n"
                "    return _PortagePagination(items, page, per_page, total)\n"
            ).body)
        if (
            "init_app" in required
            and "_portage_sqlalchemy_init_app" not in existing_definitions
        ):
            runtime_helpers.extend(ast.parse(
                "def _portage_sqlalchemy_init_app(provider, app):\n"
                "    uri = app.state.config.get('SQLALCHEMY_DATABASE_URI')\n"
                "    if not uri:\n"
                "        return None\n"
                "    current = getattr(provider, 'engine', None)\n"
                "    if current is not None and str(getattr(current, 'url', '')) == str(uri):\n"
                "        return None\n"
                "    provider.session.remove()\n"
                f"    provider.engine = {engine_factory}(uri)\n"
                "    provider.session.configure(bind=provider.engine)\n"
            ).body)
        if runtime_helpers:
            insertion = tree.body.index(assignment)
            for helper_node in runtime_helpers:
                tree.body.insert(insertion, helper_node)
                insertion += 1
            changed = True
        if decision.get("query_models"):
            base = next((
                node for node in tree.body if isinstance(node, ast.ClassDef)
                and any(
                    isinstance(parent, ast.Name) and parent.id == "DeclarativeBase"
                    or isinstance(parent, ast.Attribute)
                    and parent.attr == "DeclarativeBase"
                    for parent in node.bases
                )
            ), None)
            if base is not None and not any(
                isinstance(statement, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id == "query"
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                )
                for statement in base.body
            ):
                descriptor_name = "_PortageModelQuery"
                descriptor = ast.parse(
                    f"class {descriptor_name}:\n"
                    "    def __get__(self, instance, model):\n"
                    f"        return {symbol}.session.query(model)\n"
                ).body[0]
                tree.body.insert(tree.body.index(base), descriptor)
                base.body.append(ast.Assign(
                    targets=[ast.Name(id="query", ctx=ast.Store())],
                    value=ast.Call(
                        func=ast.Name(id=descriptor_name, ctx=ast.Load()),
                        args=[], keywords=[],
                    ),
                ))
                changed = True
        dynamic_type = (
            assignment.value
            if isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Name)
            and assignment.value.func.id == "type"
            and len(assignment.value.args) == 3
            and isinstance(assignment.value.args[2], ast.Dict)
            else assignment.value.func
            if isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Call)
            and isinstance(assignment.value.func.func, ast.Name)
            and assignment.value.func.func.id == "type"
            and len(assignment.value.func.args) == 3
            and isinstance(assignment.value.func.args[2], ast.Dict)
            else None
        )
        if dynamic_type is not None:
            assignment.value = deepcopy(dynamic_type.args[2])
            changed = True
        namespace_names = {"SimpleNamespace"}
        namespace_names.update(
            alias.asname or alias.name
            for statement in tree.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "types"
            for alias in statement.names if alias.name == "SimpleNamespace"
        )
        if (
            isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Name)
            and assignment.value.func.id in namespace_names
            and not assignment.value.args
            and all(keyword.arg for keyword in assignment.value.keywords)
        ):
            assignment.value = ast.Dict(
                keys=[ast.Constant(keyword.arg) for keyword in assignment.value.keywords],
                values=[keyword.value for keyword in assignment.value.keywords],
            )
            changed = True
        if (
            isinstance(assignment.value, ast.Call)
            and isinstance(assignment.value.func, ast.Name)
        ):
            facade = next((
                node for node in tree.body if isinstance(node, ast.ClassDef)
                and node.name == assignment.value.func.id
            ), None)
            if facade is None:
                continue
            original_body_size = len(facade.body)
            values = {
                target.id: statement.value
                for statement in facade.body
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            }
            initializer = next((
                node for node in facade.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "__init__"
            ), None)
            if initializer is not None:
                parameters = [
                    *initializer.args.posonlyargs, *initializer.args.args,
                ][1:]
                arguments = {
                    parameter.arg: argument
                    for parameter, argument in zip(
                        parameters, assignment.value.args, strict=False,
                    )
                }
                arguments.update({
                    keyword.arg: keyword.value for keyword in assignment.value.keywords
                    if keyword.arg
                })
                for statement in ast.walk(initializer):
                    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    for target in targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                            and isinstance(statement.value, ast.Name)
                            and statement.value.id in arguments
                        ):
                            values[target.attr] = arguments[statement.value.id]

            def add_class_member(
                name: str, value: ast.expr, *, owner=facade, members=values,
            ) -> None:
                owner.body.append(ast.Assign(
                    targets=[ast.Name(id=name, ctx=ast.Store())], value=value,
                ))
                members[name] = value

            if database_config and "engine" not in values and module_engine:
                add_class_member(
                    "engine", ast.Name(id=module_engine, ctx=ast.Load()),
                )
            if "Model" in required and "Model" not in values:
                model = values.get("Base") or (
                    ast.Name(id=fallback_model, ctx=ast.Load()) if fallback_model else None
                )
                if model is not None:
                    add_class_member("Model", deepcopy(model))
            for member in required & {"case", "event"}:
                if member not in values:
                    add_class_member(
                        member,
                        ast.Name(id=imported_name("sqlalchemy", member), ctx=ast.Load()),
                    )
            if (
                required & {"metadata", "create_all", "drop_all"}
                and "metadata" not in values
                and values.get("Model")
            ):
                add_class_member("metadata", ast.Attribute(
                    value=deepcopy(values["Model"]), attr="metadata", ctx=ast.Load(),
                ))
            session_factory = values.get("SessionLocal") or (
                ast.Name(id=module_session_factory, ctx=ast.Load())
                if module_session_factory else None
            )
            if "session" in required and session_factory is not None:
                existing_session = values.get("session")
                raw_session = (
                    existing_session is None
                    or isinstance(existing_session, ast.Call)
                    and ast.dump(existing_session.func) == ast.dump(session_factory)
                )
                if raw_session:
                    session = ast.Call(
                        func=ast.Name(
                            id=imported_name("sqlalchemy.orm", "scoped_session"),
                            ctx=ast.Load(),
                        ),
                        args=[deepcopy(session_factory)], keywords=[],
                    )
                    if existing_session is None:
                        add_class_member("session", session)
                    else:
                        for statement in facade.body:
                            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and any(
                                isinstance(target, ast.Name) and target.id == "session"
                                for target in (
                                    statement.targets if isinstance(statement, ast.Assign)
                                    else [statement.target]
                                )
                            ):
                                statement.value = session
                                values["session"] = session
                                break
            defined = {
                node.name for node in facade.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            sqlalchemy_select = (
                imported_name("sqlalchemy", "select") if "select" in required else ""
            )
            facade_methods = {
                "get_or_404": (
                    "def get_or_404(self, entity, ident, *, description=None, **kwargs):\n"
                    "    return _portage_sqlalchemy_get_or_404(\n"
                    "        self, entity, ident, description=description, **kwargs\n"
                    "    )\n"
                ),
                "first_or_404": (
                    "def first_or_404(self, statement, *, description=None):\n"
                    "    return _portage_sqlalchemy_first_or_404(\n"
                    "        self, statement, description=description\n"
                    "    )\n"
                ),
                "paginate": (
                    "def paginate(\n"
                    "    self, statement, *, page=None, per_page=None,\n"
                    "    max_per_page=None, error_out=True, count=True\n"
                    "):\n"
                    "    return _portage_sqlalchemy_paginate(\n"
                    "        self, statement, page=page, per_page=per_page,\n"
                    "        max_per_page=max_per_page, error_out=error_out, count=count\n"
                    "    )\n"
                ),
                "init_app": (
                    "def init_app(self, app):\n"
                    "    return _portage_sqlalchemy_init_app(self, app)\n"
                ),
                "select": (
                    "def select(self, *entities):\n"
                    f"    return {sqlalchemy_select}(*entities)\n"
                ),
            }
            for member in sorted(required & facade_methods.keys()):
                replace = member == "init_app"
                if member in defined and not replace:
                    continue
                if replace:
                    facade.body = [
                        node for node in facade.body
                        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        or node.name != member
                    ]
                facade.body.append(ast.parse(facade_methods[member]).body[0])
                defined.add(member)
                changed = True
            for member in sorted(required & {"create_all", "drop_all"}):
                if not (values.get("metadata") and values.get("engine")):
                    continue
                if member in defined:
                    facade.body = [
                        node for node in facade.body
                        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        or node.name != member
                    ]
                method = ast.parse(
                    f"def {member}(self, *args, **kwargs):\n"
                    "    kwargs.pop('bind_key', None)\n"
                    "    kwargs.setdefault('bind', self.engine)\n"
                    f"    return self.metadata.{member}(*args, **kwargs)\n"
                ).body[0]
                facade.body.append(method)
            changed = changed or len(facade.body) != original_body_size
            continue
        if not isinstance(assignment.value, ast.Dict):
            continue
        pairs = [
            (key.value, value)
            for key, value in zip(
                assignment.value.keys, assignment.value.values, strict=True,
            )
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
            and key.value.isidentifier()
        ]
        if len(pairs) != len(assignment.value.keys):
            continue
        values = dict(pairs)
        if (
            module_engine and "engine" not in values
            and required & {"create_all", "drop_all", "init_app"}
        ):
            engine = ast.Name(id=module_engine, ctx=ast.Load())
            pairs.append(("engine", engine))
            values["engine"] = engine
        if "Model" in required and "Model" not in values:
            model = values.get("Base") or next((
                ast.Name(id=node.name, ctx=ast.Load())
                for node in tree.body if isinstance(node, ast.ClassDef)
                and any(
                    isinstance(base, ast.Name) and base.id == "DeclarativeBase"
                    or isinstance(base, ast.Attribute)
                    and base.attr == "DeclarativeBase"
                    for base in node.bases
                )
            ), ast.Name(id=fallback_model, ctx=ast.Load()) if fallback_model else None)
            if model is not None:
                pairs.append(("Model", model))
                values["Model"] = model
        for member in required & {"case", "event"}:
            if member not in values or (
                isinstance(values[member], ast.Constant)
                and values[member].value is None
            ):
                local = imported_name(
                    "sqlalchemy", member, f"_portage_sqlalchemy_{member}",
                )
                replacement = ast.Name(id=local, ctx=ast.Load())
                if member in values:
                    pairs = [
                        (key, replacement if key == member else value)
                        for key, value in pairs
                    ]
                else:
                    pairs.append((member, replacement))
                values[member] = replacement
        if (
            required & {"metadata", "create_all", "drop_all"}
            and "metadata" not in values and values.get("Model")
        ):
            metadata = ast.Attribute(
                value=deepcopy(values["Model"]), attr="metadata", ctx=ast.Load(),
            )
            pairs.append(("metadata", metadata))
            values["metadata"] = metadata
        session_factory = values.get("SessionLocal") or (
            ast.Name(id=module_session_factory, ctx=ast.Load())
            if module_session_factory else None
        )
        if "session" in required and session_factory is not None:
            existing_session = values.get("session")
            raw_session = (
                existing_session is None
                or isinstance(existing_session, ast.Call)
                and ast.dump(existing_session.func) == ast.dump(session_factory)
            )
            if raw_session:
                session = ast.Call(
                    func=ast.Name(
                        id=imported_name("sqlalchemy.orm", "scoped_session"),
                        ctx=ast.Load(),
                    ),
                    args=[deepcopy(session_factory)], keywords=[],
                )
                if existing_session is None:
                    pairs.append(("session", session))
                else:
                    pairs = [
                        (key, session if key == "session" else value)
                        for key, value in pairs
                    ]
                values["session"] = session
        insertion = tree.body.index(assignment)
        sqlalchemy_select = (
            imported_name("sqlalchemy", "select") if "select" in required else ""
        )
        namespace_methods = {
            "get_or_404": (
                f"def _portage_{symbol}_get_or_404(\n"
                "    entity, ident, *, description=None, **kwargs\n"
                "):\n"
                f"    return _portage_sqlalchemy_get_or_404(\n"
                f"        {symbol}, entity, ident, description=description, **kwargs\n"
                "    )\n"
            ),
            "first_or_404": (
                f"def _portage_{symbol}_first_or_404(statement, *, description=None):\n"
                f"    return _portage_sqlalchemy_first_or_404(\n"
                f"        {symbol}, statement, description=description\n"
                "    )\n"
            ),
            "paginate": (
                f"def _portage_{symbol}_paginate(\n"
                "    statement, *, page=None, per_page=None, max_per_page=None,\n"
                "    error_out=True, count=True\n"
                "):\n"
                f"    return _portage_sqlalchemy_paginate(\n"
                f"        {symbol}, statement, page=page, per_page=per_page,\n"
                "        max_per_page=max_per_page, error_out=error_out, count=count\n"
                "    )\n"
            ),
            "init_app": (
                f"def _portage_{symbol}_init_app(app):\n"
                f"    return _portage_sqlalchemy_init_app({symbol}, app)\n"
            ),
            "select": (
                f"def _portage_{symbol}_select(*entities):\n"
                f"    return {sqlalchemy_select}(*entities)\n"
            ),
        }
        for member in sorted(required & namespace_methods.keys()):
            replace = (
                member not in values
                or member in {"init_app", "select"}
                or isinstance(values.get(member), ast.Constant)
                and values[member].value is None
            )
            if not replace:
                continue
            helper_name = f"_portage_{symbol}_{member}"
            if not any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == helper_name for node in tree.body
            ):
                tree.body.insert(
                    insertion, ast.parse(namespace_methods[member]).body[0],
                )
                insertion += 1
            value = ast.Name(id=helper_name, ctx=ast.Load())
            if member in values:
                pairs = [
                    (key, value if key == member else existing)
                    for key, existing in pairs
                ]
            else:
                pairs.append((member, value))
            values[member] = value
        for member in sorted(required & {"create_all", "drop_all"}):
            if not (values.get("metadata") and values.get("engine")):
                continue
            helper_name = f"_portage_{symbol}_{member}"
            if not any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == helper_name for node in tree.body
            ):
                helper = ast.parse(
                    f"def {helper_name}(*args, **kwargs):\n"
                    "    kwargs.pop('bind_key', None)\n"
                    f"    kwargs.setdefault('bind', {symbol}.engine)\n"
                    f"    return {symbol}.metadata.{member}(*args, **kwargs)\n"
                ).body[0]
                tree.body.insert(insertion, helper)
                insertion += 1
            value = ast.Name(id=helper_name, ctx=ast.Load())
            if member in values:
                pairs = [
                    (key, value if key == member else existing)
                    for key, existing in pairs
                ]
            else:
                pairs.append((member, value))
            values[member] = value
        namespace = imported_name("types", "SimpleNamespace")
        assignment.value = ast.Call(
            func=ast.Name(id=namespace, ctx=ast.Load()), args=[],
            keywords=[
                ast.keyword(arg=key, value=value) for key, value in pairs
            ],
        )
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_implicit_sqlalchemy_tables(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Materialize table names that Flask-SQLAlchemy supplied implicitly."""
    expected = {
        class_name: table_name
        for decision in (seam_plan or {}).get("decisions", {}).values()
        if decision.get("kind") == "extension_provider"
        for class_name, table_name in decision.get("implicit_tables", {}).get(
            path, {},
        ).items()
    }
    tree = _parsed(content)
    if tree is None or not expected:
        return content
    changed = False
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in expected:
            continue
        if any(
            isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id in {"__table__", "__tablename__"}
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
            for statement in node.body
        ):
            continue
        insert_at = int(bool(
            node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ))
        node.body.insert(insert_at, ast.Assign(
            targets=[ast.Name(id="__tablename__", ctx=ast.Store())],
            value=ast.Constant(value=expected[node.name]),
        ))
        changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _incomplete_object_config(value: ast.AST) -> ast.AST | None:
    """Return the object copied by vars()/__dict__, which drops inherited settings."""
    if (
        isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
        and value.func.id == "vars" and len(value.args) == 1
    ):
        return value.args[0]
    if isinstance(value, ast.Attribute) and value.attr == "__dict__":
        return value.value
    if (
        isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
        and value.func.id == "dict" and len(value.args) == 1
    ):
        return _incomplete_object_config(value.args[0])
    return None


def _uppercase_object_config(value: ast.AST) -> ast.DictComp:
    name = "_portage_config_key"
    return ast.DictComp(
        key=ast.Name(id=name, ctx=ast.Load()),
        value=ast.Call(
            func=ast.Name(id="getattr", ctx=ast.Load()),
            args=[deepcopy(value), ast.Name(id=name, ctx=ast.Load())],
            keywords=[],
        ),
        generators=[ast.comprehension(
            target=ast.Name(id=name, ctx=ast.Store()),
            iter=ast.Call(
                func=ast.Name(id="dir", ctx=ast.Load()),
                args=[deepcopy(value)], keywords=[],
            ),
            ifs=[ast.Call(
                func=ast.Attribute(
                    value=ast.Name(id=name, ctx=ast.Load()),
                    attr="isupper", ctx=ast.Load(),
                ),
                args=[], keywords=[],
            )],
            is_async=0,
        )],
    )


def _copies_object_config(value: ast.AST, source: ast.AST) -> bool:
    wanted = ast.dump(source, include_attributes=False)
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "dir"
        and len(node.args) == 1
        and ast.dump(node.args[0], include_attributes=False) == wanted
        for node in ast.walk(value)
    )


def _realize_factory_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Materialize frozen factory wiring that has one mechanical realization."""
    decisions = list((seam_plan or {}).get("decisions", {}).values())
    decision = next((
        item for item in decisions
        if item.get("kind") == "application_factory" and item.get("factory") == path
    ), None)
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
    factory = next((
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "create_app"
    ), None)
    exception_owners = {
        name: (item["provider"], item["symbol"])
        for item in decisions if item.get("kind") == "provider_protocol"
        and item.get("provider") != path and path in item.get("files", [])
        for name in item.get("exception_members", [])
    }
    if exception_owners:
        defined = {
            node.name for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        loaded = {
            node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        for name, (provider, symbol) in exception_owners.items():
            provider_refs = set()
            for statement in tree.body:
                if isinstance(statement, ast.ImportFrom) and _resolve_module(
                    statement.module, statement.level, path,
                ) in _module_names(provider):
                    provider_refs.update(
                        alias.asname or alias.name
                        for alias in statement.names if alias.name == symbol
                    )
                elif isinstance(statement, ast.Import):
                    provider_refs.update(
                        f"{alias.asname or alias.name}.{symbol}"
                        for alias in statement.names
                        if alias.name in _module_names(provider)
                    )
            if any(
                isinstance(node, ast.Attribute) and node.attr == name
                and ast.unparse(node.value) in provider_refs
                for node in ast.walk(tree)
            ):
                class ExceptionOwnerRewriter(ast.NodeTransformer):
                    def __init__(self, exception_name, refs):
                        self.exception_name = exception_name
                        self.refs = refs

                    def visit_Attribute(self, node):
                        node = self.generic_visit(node)
                        if (
                            node.attr == self.exception_name
                            and ast.unparse(node.value) in self.refs
                        ):
                            return ast.copy_location(
                                ast.Name(id=self.exception_name, ctx=ast.Load()), node,
                            )
                        return node

                tree = ExceptionOwnerRewriter(name, provider_refs).visit(tree)
                loaded.add(name)
                changed = True
            if name in defined or name not in loaded:
                continue
            found = False
            for statement in list(tree.body):
                if not isinstance(statement, ast.ImportFrom):
                    continue
                aliases = [alias for alias in statement.names if alias.name == name]
                if not aliases:
                    continue
                if _resolve_module(statement.module, statement.level, path) in _module_names(
                    provider,
                ):
                    found = True
                    continue
                statement.names = [alias for alias in statement.names if alias not in aliases]
                if not statement.names:
                    tree.body.remove(statement)
                changed = True
            if not found:
                tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                    module=provider.removesuffix(".py").replace("/", "."),
                    names=[ast.alias(name=name)], level=0,
                ))
                changed = True
    test_surface = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "planned_test_surface"
        and path in item.get("factory_consumers", [])
        and item.get("provider") != path
    ), None)
    classes = (test_surface or {}).get("classes", [])
    facade_names: set[str] = set()
    if len(classes) == 1:
        provider = test_surface["provider"]
        class_name = classes[0]["name"]
        facade_names = {
            alias.asname or alias.name
            for statement in tree.body if isinstance(statement, ast.ImportFrom)
            and _resolve_module(statement.module, statement.level, path)
            in _module_names(provider)
            for alias in statement.names if alias.name == class_name
        }
        if not facade_names:
            tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                module=provider.removesuffix(".py").replace("/", "."),
                names=[ast.alias(name=class_name)], level=0,
            ))
            facade_names = {class_name}
            changed = True
        facade_name = next(iter(facade_names))
        if factory is not None:
            for returned_node in (
                node for node in ast.walk(factory)
                if isinstance(node, ast.Return)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id in facade_names
                and node.value.args and isinstance(node.value.args[0], ast.Name)
            ):
                raw_name = returned_node.value.args[0].id
                raw_assignment = next((
                    statement for statement in ast.walk(factory)
                    if isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and isinstance(statement.value, ast.Call)
                    and ast.unparse(statement.value.func).split(".")[-1] == "FastAPI"
                    and any(
                        isinstance(target, ast.Name) and target.id == raw_name
                        for target in (
                            statement.targets if isinstance(statement, ast.Assign)
                            else [statement.target]
                        )
                    )
                ), None)
                if raw_assignment is None:
                    continue
                raw_assignment.value.func = ast.Name(id=facade_name, ctx=ast.Load())
                returned_node.value = ast.Name(id=raw_name, ctx=ast.Load())
                changed = True
        returned = {
            node.value.id for node in ast.walk(factory)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        } if factory else set()
        assignments = [
            statement for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1] == "FastAPI"
        ]
        if factory is not None:
            assignments.extend(
                statement for statement in ast.walk(factory)
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and isinstance(statement.value, ast.Call)
                and ast.unparse(statement.value.func).split(".")[-1] == "FastAPI"
                and any(
                    isinstance(target, ast.Name) and target.id in returned
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                )
            )
        for statement in assignments:
            statement.value.func = ast.Name(id=facade_name, ctx=ast.Load())
            changed = True
        module_exports = test_surface.get("module_app_exports", {}).get(path, [])
        required_args = (
            len(factory.args.posonlyargs) + len(factory.args.args)
            - len(factory.args.defaults)
            + sum(default is None for default in factory.args.kw_defaults)
        ) if factory is not None else 1
        if (
            factory is not None and len(module_exports) == 1 and required_args == 0
            and not any(
                isinstance(statement, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id == module_exports[0]
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                )
                for statement in tree.body
            )
        ):
            tree.body.append(ast.Assign(
                targets=[ast.Name(id=module_exports[0], ctx=ast.Store())],
                value=ast.Call(
                    func=ast.Name(id=factory.name, ctx=ast.Load()), args=[], keywords=[],
                ),
            ))
            changed = True
        if factory is not None and "testing" in classes[0].get("members", []):
            facade_instances = {
                target.id
                for statement in ast.walk(factory)
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and statement.value.func.id in facade_names
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            }
            raw_apps = {
                target.id
                for statement in ast.walk(factory)
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and isinstance(statement.value, ast.Call)
                and ast.unparse(statement.value.func).split(".")[-1] == "FastAPI"
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                if isinstance(target, ast.Name)
            }
            if len(facade_instances) == 1:
                owner = next(iter(facade_instances))
                for node in ast.walk(factory):
                    if (
                        isinstance(node, ast.Attribute) and node.attr == "testing"
                        and isinstance(node.value, ast.Name)
                        and node.value.id in raw_apps
                    ):
                        node.value = ast.Name(id=owner, ctx=ast.Load())
                        changed = True
        callback_specs = [
            (callback["provider"], function)
            for callback in (decision or {}).get("cleanup_callbacks", [])
            for function in callback.get("functions", [])
        ]
        if factory is not None:
            callback_values: list[ast.expr] = []
            for index, (provider, function) in enumerate(callback_specs):
                alias_name = f"_portage_cleanup_{index}"
                module = provider.removesuffix(".py").replace("/", ".")
                imported = any(
                    isinstance(statement, ast.ImportFrom)
                    and statement.module == module
                    and any(
                        alias.name == function and alias.asname == alias_name
                        for alias in statement.names
                    )
                    for statement in tree.body
                )
                if not imported:
                    tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                        module=module,
                        names=[ast.alias(name=function, asname=alias_name)],
                        level=0,
                    ))
                    changed = True
                callback_values.append(ast.Name(id=alias_name, ctx=ast.Load()))
            initialized = {
                (item.get("provider"), item.get("symbol"))
                for item in (decision or {}).get("initializers", [])
            }
            session_providers = {
                (item.get("provider"), item.get("symbol"))
                for item in (seam_plan or {}).get("decisions", {}).values()
                if item.get("kind") == "extension_provider"
                and "session" in item.get("members", [])
                and (item.get("provider"), item.get("symbol")) in initialized
            }
            for _provider, symbol in sorted(session_providers):
                receiver = next((
                    deepcopy(node.func.value)
                    for node in ast.walk(factory)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "init_app"
                    and ast.unparse(node.func.value).split(".")[-1] == symbol
                ), None)
                if receiver is not None:
                    callback_values.append(ast.Attribute(
                        value=ast.Attribute(
                            value=receiver, attr="session", ctx=ast.Load(),
                        ),
                        attr="remove", ctx=ast.Load(),
                    ))
            if callback_values:
                for call in (
                    node for node in ast.walk(factory)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in facade_names
                ):
                    keyword = next((
                        item for item in call.keywords
                        if item.arg == "cleanup_callbacks"
                    ), None)
                    value = ast.Tuple(elts=deepcopy(callback_values), ctx=ast.Load())
                    if keyword is None:
                        call.keywords.append(ast.keyword(
                            arg="cleanup_callbacks", value=value,
                        ))
                    else:
                        keyword.value = value
                    changed = True
    if decision and factory and not any(
        "testing" in item.get("members", []) for item in classes
    ):
        app_names = {
            target.id
            for statement in ast.walk(factory)
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1]
            in {"FastAPI", *facade_names}
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }

        class TestingReadRewriter(ast.NodeTransformer):
            replacements = 0

            def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
                if (
                    isinstance(node.ctx, ast.Load) and node.attr == "testing"
                    and isinstance(node.value, ast.Name)
                    and node.value.id in app_names
                ):
                    self.replacements += 1
                    return ast.Call(
                        func=ast.Attribute(
                            value=ast.Attribute(
                                value=ast.Attribute(
                                    value=ast.Name(id=node.value.id, ctx=ast.Load()),
                                    attr="state", ctx=ast.Load(),
                                ),
                                attr="config", ctx=ast.Load(),
                            ),
                            attr="get", ctx=ast.Load(),
                        ),
                        args=[ast.Constant(value="TESTING"), ast.Constant(value=False)],
                        keywords=[],
                    )
                return self.generic_visit(node)

        rewriter = TestingReadRewriter()
        rewriter.visit(factory)
        changed = changed or bool(rewriter.replacements)
    if decision and factory:
        lifespan_names = {
            keyword.value.id
            for call in ast.walk(factory)
            if isinstance(call, ast.Call)
            for keyword in call.keywords
            if keyword.arg == "lifespan" and isinstance(keyword.value, ast.Name)
        }
        lifespan_functions = {
            node.name: node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name in lifespan_names
            and any(
                isinstance(part, (ast.Yield, ast.YieldFrom)) for part in ast.walk(node)
            )
        }
        undecorated = [
            function for function in lifespan_functions.values()
            if not any(
                ast.unparse(decorator).split(".")[-1] == "asynccontextmanager"
                for decorator in function.decorator_list
            )
        ]
        if undecorated:
            decorator_name = next((
                alias.asname or alias.name
                for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and statement.module == "contextlib"
                for alias in statement.names if alias.name == "asynccontextmanager"
            ), "")
            if not decorator_name:
                tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                    module="contextlib",
                    names=[ast.alias(name="asynccontextmanager")], level=0,
                ))
                decorator_name = "asynccontextmanager"
            for function in undecorated:
                function.decorator_list.insert(
                    0, ast.Name(id=decorator_name, ctx=ast.Load()),
                )
            changed = True
    if decision and factory:
        if "instance_path" in decision.get("app_state_members", []):
            returned_apps = {
                node.value.id for node in ast.walk(factory)
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
            }
            if len(returned_apps) == 1:
                app_name = next(iter(returned_apps))
                constructors = [
                    node for node in ast.walk(factory)
                    if isinstance(node, ast.Call)
                    and ast.unparse(node.func).split(".")[-1]
                    in {"FastAPI", *facade_names}
                ]
                expression = None
                for call in constructors:
                    explicit = next((
                        keyword for keyword in call.keywords
                        if keyword.arg == "instance_path"
                    ), None)
                    if explicit is not None:
                        expression = explicit.value
                        call.keywords.remove(explicit)
                        changed = True
                already_set = any(
                    isinstance(node, (ast.Assign, ast.AnnAssign))
                    and any(
                        ast.unparse(target) == f"{app_name}.state.instance_path"
                        for target in (
                            node.targets if isinstance(node, ast.Assign)
                            else [node.target]
                        )
                    )
                    for node in ast.walk(factory)
                )
                if not already_set:
                    if expression is None and decision.get("instance_path_expression"):
                        try:
                            expression = ast.parse(
                                decision["instance_path_expression"], mode="eval",
                            ).body
                        except SyntaxError:
                            expression = None
                    if expression is None:
                        path_name = "_PortagePath"
                        if not any(
                            isinstance(node, ast.ImportFrom)
                            and node.module == "pathlib"
                            and any(
                                alias.name == "Path"
                                and (alias.asname or alias.name) == path_name
                                for alias in node.names
                            )
                            for node in tree.body
                        ):
                            insert_at = int(bool(
                                tree.body and isinstance(tree.body[0], ast.ImportFrom)
                                and tree.body[0].module == "__future__"
                            ))
                            tree.body.insert(insert_at, ast.ImportFrom(
                                module="pathlib",
                                names=[ast.alias(name="Path", asname=path_name)],
                                level=0,
                            ))
                        expression = ast.parse(
                            "str(_PortagePath(__file__).resolve().parent / 'instance')",
                            mode="eval",
                        ).body
                    assignment = ast.Assign(
                        targets=[ast.Attribute(
                            value=ast.Attribute(
                                value=ast.Name(id=app_name, ctx=ast.Load()),
                                attr="state", ctx=ast.Load(),
                            ),
                            attr="instance_path", ctx=ast.Store(),
                        )],
                        value=expression,
                    )
                    cursor = next((
                        index + 1 for index, statement in enumerate(factory.body)
                        if any(
                            isinstance(node, ast.Name)
                            and isinstance(node.ctx, ast.Store)
                            and node.id == app_name
                            for node in ast.walk(statement)
                        )
                    ), 0)
                    factory.body.insert(cursor, assignment)
                    changed = True

        class FactoryInstancePathRewriter(ast.NodeTransformer):
            def visit_Attribute(self, node: ast.Attribute) -> ast.AST:  # noqa: N802
                node = self.generic_visit(node)
                if (
                    node.attr == "instance_path"
                    and not (
                        isinstance(node.value, ast.Attribute)
                        and node.value.attr == "state"
                    )
                ):
                    node.value = ast.Attribute(
                        value=node.value, attr="state", ctx=ast.Load(),
                    )
                return node

        if "instance_path" in decision.get("app_state_members", []):
            factory = FactoryInstancePathRewriter().visit(factory)
            changed = True
            instance_bindings = {
                node.value.id
                for node in ast.walk(factory)
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(node.value, ast.Name)
                and any(
                    ast.unparse(target).endswith(".state.instance_path")
                    for target in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                )
            }
            path_constructor = next((
                alias.asname or alias.name
                for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and statement.module == "pathlib"
                for alias in statement.names if alias.name == "Path"
            ), "_PortagePath")

            class InstancePathJoinRewriter(ast.NodeTransformer):
                replacements = 0

                def visit_BinOp(self, node: ast.BinOp) -> ast.AST:  # noqa: N802
                    node = self.generic_visit(node)
                    if (
                        isinstance(node.op, ast.Div)
                        and isinstance(node.left, ast.Name)
                        and node.left.id in instance_bindings
                    ):
                        node.left = ast.Call(
                            func=ast.Name(id=path_constructor, ctx=ast.Load()),
                            args=[node.left], keywords=[],
                        )
                        self.replacements += 1
                    return node

            join_rewriter = InstancePathJoinRewriter()
            join_rewriter.visit(factory)
            if join_rewriter.replacements:
                if path_constructor == "_PortagePath":
                    tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                        module="pathlib",
                        names=[ast.alias(name="Path", asname=path_constructor)],
                        level=0,
                    ))
                changed = True

        required_config = set(decision.get("config_keys", []))
        returned_apps = {
            node.value.id for node in ast.walk(factory)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        }
        if (
            required_config or decision.get("override_parameters")
        ) and len(returned_apps) == 1:
            app_name = next(iter(returned_apps))
            config_target = f"{app_name}.state.config"
            config_assignment = next((
                (index, statement)
                for index, statement in enumerate(factory.body)
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and any(
                    ast.unparse(target) == config_target
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                )
            ), None)
            if config_assignment is None:
                local_config = next((
                    target.id
                    for statement in ast.walk(factory)
                    if isinstance(statement, (ast.Assign, ast.AnnAssign))
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    if isinstance(target, ast.Name) and target.id == "config"
                ), "")
                if local_config:
                    cursor = next((
                        index + 1 for index, statement in enumerate(factory.body)
                        if any(
                            isinstance(node, ast.Name)
                            and isinstance(node.ctx, ast.Store)
                            and node.id == app_name
                            for node in ast.walk(statement)
                        )
                    ), 0)
                    assignment = ast.Assign(
                        targets=[ast.parse(config_target, mode="eval").body],
                        value=ast.IfExp(
                            test=ast.Call(
                                func=ast.Name(id="hasattr", ctx=ast.Load()),
                                args=[
                                    ast.Name(id=local_config, ctx=ast.Load()),
                                    ast.Constant(value="items"),
                                ],
                                keywords=[],
                            ),
                            body=ast.Call(
                                func=ast.Name(id="dict", ctx=ast.Load()),
                                args=[ast.Name(id=local_config, ctx=ast.Load())],
                                keywords=[],
                            ),
                            orelse=_uppercase_object_config(
                                ast.Name(id=local_config, ctx=ast.Load()),
                            ),
                        ),
                    )
                    assignment.targets[0].ctx = ast.Store()
                    factory.body.insert(cursor, assignment)
                    config_assignment = (cursor, assignment)
                    changed = True
            if config_assignment is not None:
                cursor = factory.body.index(config_assignment[1]) + 1
                for parameter in decision.get("override_parameters", []):
                    has_update = any(
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "update"
                        and ast.unparse(node.func.value) == config_target
                        and node.args
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == parameter
                        for node in ast.walk(factory)
                    )
                    if has_update:
                        continue
                    factory.body.insert(cursor, ast.If(
                        test=ast.Name(id=parameter, ctx=ast.Load()),
                        body=[ast.Expr(value=ast.Call(
                            func=ast.Attribute(
                                value=ast.parse(config_target, mode="eval").body,
                                attr="update", ctx=ast.Load(),
                            ),
                            args=[ast.Name(id=parameter, ctx=ast.Load())], keywords=[],
                        ))],
                        orelse=[],
                    ))
                    cursor += 1
                    changed = True

        for contract in decision.get("local_imports", []):
            moved = []
            moved_statements = []
            submodule = f"{contract['module']}.{contract['symbol']}".lstrip(".")
            for statement in list(tree.body):
                if not isinstance(statement, ast.ImportFrom):
                    continue
                resolved = _resolve_module(statement.module, statement.level, path)
                if resolved == submodule:
                    moved_statements.append(statement)
                    tree.body.remove(statement)
                    continue
                if resolved != contract["module"]:
                    continue
                selected = [
                    alias for alias in statement.names
                    if alias.name == contract["symbol"]
                ]
                if not selected:
                    continue
                moved.extend(selected)
                statement.names = [
                    alias for alias in statement.names if alias not in selected
                ]
                if not statement.names:
                    tree.body.remove(statement)
            if not moved and not moved_statements:
                continue
            local_names = {
                alias.asname or alias.name
                for alias in [
                    *moved,
                    *(alias for statement in moved_statements for alias in statement.names),
                ]
            }
            insert_at = next((
                index for index, statement in enumerate(factory.body)
                if any(
                    isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id in local_names
                    for node in ast.walk(statement)
                )
            ), next((
                index for index, statement in enumerate(factory.body)
                if isinstance(statement, ast.Return)
            ), len(factory.body)))
            additions = [*moved_statements]
            if moved:
                additions.append(ast.ImportFrom(
                    module=contract.get("source_module", contract["module"]),
                    names=moved, level=contract.get("level", 0),
                ))
            factory.body[insert_at:insert_at] = additions
            changed = True
    if decision and decision.get("config_from_objects"):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and ast.unparse(node.func.value).endswith(".config")
                and node.args
                and (source := _incomplete_object_config(node.args[0])) is not None
            ):
                node.args[0] = _uppercase_object_config(source)
                changed = True
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not any(ast.unparse(target).endswith(".config") for target in targets):
                    continue
                source = _incomplete_object_config(node.value)
                if source is not None:
                    node.value = _uppercase_object_config(source)
                    changed = True
        sources = []
        for expression in decision["config_from_objects"]:
            try:
                sources.append(ast.parse(expression, mode="eval").body)
            except SyntaxError:
                continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(
                ast.unparse(target).endswith(".state.config") for target in targets
            ):
                continue
            missing = [
                source for source in sources
                if not _copies_object_config(node.value, source)
            ]
            defaults = None
            for source in missing:
                mapping = _uppercase_object_config(source)
                defaults = mapping if defaults is None else ast.BinOp(
                    left=defaults, op=ast.BitOr(), right=mapping,
                )
            if defaults is not None:
                node.value = ast.BinOp(
                    left=defaults, op=ast.BitOr(), right=node.value,
                )
                changed = True
        has_config_assignment = any(
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                ast.unparse(target).endswith(".state.config")
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
            for node in ast.walk(factory or tree)
        )
        if factory is not None and sources and not has_config_assignment:
            config_ref = next((
                node for statement in factory.body for node in ast.walk(statement)
                if isinstance(node, ast.Attribute) and node.attr == "config"
                and isinstance(node.value, ast.Attribute) and node.value.attr == "state"
            ), None)
            insert_at = next((
                index for index, statement in enumerate(factory.body)
                if config_ref is not None and any(
                    node is config_ref for node in ast.walk(statement)
                )
            ), -1)
            if config_ref is None:
                returned = {
                    node.value.id for node in ast.walk(factory)
                    if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
                }
                app_assignment = next((
                    (index, target.id)
                    for index, statement in enumerate(factory.body)
                    if isinstance(statement, (ast.Assign, ast.AnnAssign))
                    and isinstance(statement.value, ast.Call)
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    if isinstance(target, ast.Name) and target.id in returned
                ), None)
                if app_assignment is not None:
                    insert_at, app_name = app_assignment
                    insert_at += 1
                    config_ref = ast.Attribute(
                        value=ast.Attribute(
                            value=ast.Name(id=app_name, ctx=ast.Load()),
                            attr="state", ctx=ast.Load(),
                        ),
                        attr="config", ctx=ast.Load(),
                    )
            if config_ref is not None:
                defaults = None
                for source in sources:
                    mapping = _uppercase_object_config(source)
                    defaults = mapping if defaults is None else ast.BinOp(
                        left=defaults, op=ast.BitOr(), right=mapping,
                    )
                target = deepcopy(config_ref)
                target.ctx = ast.Store()
                factory.body.insert(insert_at, ast.Assign(
                    targets=[target], value=defaults,
                ))
                changed = True
    if decision and factory and decision.get("initializers"):
        returned_apps = {
            node.value.id for node in ast.walk(factory)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        }
        returned_apps.update(
            node.value.args[0].id for node in ast.walk(factory)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Call) and node.value.args
            and isinstance(node.value.args[0], ast.Name)
        )
        configured_apps = {
            node.value.value.id
            for node in ast.walk(factory)
            if isinstance(node, ast.Attribute) and node.attr == "config"
            and isinstance(node.value, ast.Attribute) and node.value.attr == "state"
            and isinstance(node.value.value, ast.Name)
        }
        constructed_apps = {
            target.id
            for statement in ast.walk(factory)
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func).split(".")[-1]
            in {"FastAPI", *facade_names}
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }
        wired_apps = {
            node.func.value.id
            for node in ast.walk(factory)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in {
                "add_exception_handler", "add_middleware", "api_route", "delete",
                "get", "head", "include_router", "mount", "options", "patch",
                "post", "put",
            }
        }
        source_apps = {
            argument.id
            for initializer in decision["initializers"]
            for argument in ast.parse(
                initializer["original_call"], mode="eval",
            ).body.args
            if isinstance(argument, ast.Name)
            and any(
                isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                and node.id == argument.id
                for node in ast.walk(factory)
            )
        }
        candidate_sets = (
            returned_apps, configured_apps, constructed_apps, wired_apps, source_apps,
        )
        nonempty_candidates = [item for item in candidate_sets if item]
        overlap = (
            set.intersection(*nonempty_candidates) if nonempty_candidates else set()
        )
        scores = {
            name: sum(name in candidates for candidates in candidate_sets)
            for candidates in candidate_sets for name in candidates
        }
        best_score = max(scores.values(), default=0)
        best = {name for name, score in scores.items() if score == best_score}
        app_names = next(
            (item for item in candidate_sets if len(item) == 1),
            overlap if len(overlap) == 1 else best,
        )
        pending: list[ast.Call] = []
        for initializer in decision["initializers"]:
            wanted = _module_names(initializer["provider"])
            tails = {name.split(".")[-1] for name in wanted}
            refs: set[str] = set()
            for statement in [*tree.body, *factory.body]:
                if isinstance(statement, ast.ImportFrom):
                    module = _resolve_module(statement.module, statement.level, path)
                    if module in wanted or module.split(".")[-1] in tails:
                        refs.update(
                            alias.asname or alias.name
                            for alias in statement.names
                            if alias.name == initializer["symbol"]
                        )
                    for alias in statement.names:
                        if f"{module}.{alias.name}".lstrip(".") in wanted:
                            refs.add(
                                f"{alias.asname or alias.name}."
                                f"{initializer['symbol']}"
                            )
                elif isinstance(statement, ast.Import):
                    refs.update(
                        f"{alias.asname or alias.name}.{initializer['symbol']}"
                        for alias in statement.names
                        if alias.name in wanted or alias.name.split(".")[-1] in tails
                    )
            original_call = ast.parse(
                initializer["original_call"], mode="eval",
            ).body
            refs.add(ast.unparse(original_call.func))
            if any(
                isinstance(node, ast.Call)
                and ast.unparse(node.func) in refs
                for node in ast.walk(factory)
            ):
                continue
            call = ast.parse(initializer["original_call"], mode="eval").body
            root = next(
                node.id for node in ast.walk(call.func) if isinstance(node, ast.Name)
            )
            bound = any(
                isinstance(statement, ast.ImportFrom)
                and any(
                    (alias.asname or alias.name) == root
                    for alias in statement.names
                )
                for statement in [*tree.body, *factory.body]
            )
            if not bound:
                local = next((
                    item for item in decision.get("local_imports", [])
                    if item["symbol"] == root
                ), None)
                if local:
                    import_at = int(bool(
                        factory.body and isinstance(factory.body[0], ast.Expr)
                        and isinstance(factory.body[0].value, ast.Constant)
                        and isinstance(factory.body[0].value.value, str)
                    ))
                    factory.body.insert(import_at, ast.ImportFrom(
                        module=local.get("source_module", local["module"]),
                        names=[ast.alias(name=root)], level=local.get("level", 0),
                    ))
                else:
                    factory.body.insert(0, ast.ImportFrom(
                        module=initializer["provider"].removesuffix(".py").replace(
                            "/", "."
                        ),
                        names=[ast.alias(name=initializer["symbol"])], level=0,
                    ))
                    call.func = ast.Name(id=initializer["symbol"], ctx=ast.Load())
            pending.append(call)

        if pending and len(app_names) == 1:
            app_name = next(iter(app_names))
            for call in pending:
                call.args = [
                    ast.Name(id=app_name, ctx=ast.Load()), *call.args[1:],
                ]
            config_indices = [
                index for index, statement in enumerate(factory.body)
                if any(
                    ast.unparse(node).endswith(".state.config")
                    or ast.unparse(node).endswith(".state.config.update")
                    for node in ast.walk(statement)
                    if isinstance(node, (ast.Attribute, ast.Name))
                )
            ]
            binding_indices = [
                index for index, statement in enumerate(factory.body)
                if any(
                    isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                    and node.id == app_name
                    for node in ast.walk(statement)
                )
            ]
            cursor = max([*config_indices, *binding_indices], default=-1) + 1
            for call in pending:
                factory.body.insert(cursor, ast.Expr(value=call))
                cursor += 1
            changed = True
    context_names: set[str] = set()
    ambient = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "ambient_context_runtime"
        and path in item.get("factory_files", [])
    ), None)
    if ambient:
        runtime_modules = {
            PurePosixPath(provider).stem
            for provider in ambient.get("runtime_providers", [])
        }
        runtime_class_exports = {
            name for names in ambient.get("runtime_classes", {}).values()
            for name in names
        }
        context_names = {
            alias.asname or alias.name
            for statement in tree.body if isinstance(statement, ast.ImportFrom)
            and (statement.module or "").split(".")[-1] in runtime_modules
            for alias in statement.names if alias.name in runtime_class_exports
        }
        for function in [factory] if factory is not None else []:
            middleware = [
                (index, statement, statement.value.args[0].id)
                for index, statement in enumerate(function.body)
                if isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Attribute)
                and statement.value.func.attr == "add_middleware"
                and statement.value.args
                and isinstance(statement.value.args[0], ast.Name)
            ]
            sessions = [item for item in middleware if item[2] == "SessionMiddleware"]
            contexts = [item for item in middleware if item[2] in context_names]
            if len(sessions) == len(contexts) == 1 and sessions[0][0] < contexts[0][0]:
                session_statement = sessions[0][1]
                function.body.remove(session_statement)
                context_index = function.body.index(contexts[0][1])
                function.body.insert(context_index + 1, session_statement)
                changed = True
            if len(sessions) == 1:
                session_statement = sessions[0][1]
                inline_middleware = [
                    statement for statement in function.body
                    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and any(
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == "middleware"
                        for decorator in statement.decorator_list
                    )
                ]
                if inline_middleware and function.body.index(session_statement) < max(
                    function.body.index(statement) for statement in inline_middleware
                ):
                    function.body.remove(session_statement)
                    last_middleware = max(
                        inline_middleware, key=lambda statement: function.body.index(statement)
                    )
                    function.body.insert(
                        function.body.index(last_middleware) + 1, session_statement,
                    )
                    changed = True
            returned_apps = {
                node.value.id for node in ast.walk(function)
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
            }
            installed = any(
                isinstance(call, ast.Call) and call.args
                and (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "add_middleware"
                    or ast.unparse(call.func).split(".")[-1] == "Middleware"
                )
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id in context_names
                for call in ast.walk(function)
            )
            if len(returned_apps) == len(context_names) == 1 and not installed:
                app_name = next(iter(returned_apps))
                context_name = next(iter(context_names))
                install = ast.Expr(value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id=app_name, ctx=ast.Load()),
                        attr="add_middleware", ctx=ast.Load(),
                    ),
                    args=[ast.Name(id=context_name, ctx=ast.Load())], keywords=[],
                ))
                insert_at = next((
                    index for index, statement in enumerate(function.body)
                    if isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Attribute)
                    and statement.value.func.attr == "add_middleware"
                    and statement.value.args
                    and isinstance(statement.value.args[0], ast.Name)
                    and statement.value.args[0].id == "SessionMiddleware"
                ), next((
                    index for index, statement in enumerate(function.body)
                    if isinstance(statement, ast.Return)
                ), len(function.body)))
                function.body.insert(insert_at, install)
                changed = True
        for keyword in (
            keyword
            for call in ast.walk(tree) if isinstance(call, ast.Call)
            for keyword in call.keywords
            if keyword.arg == "middleware"
            and isinstance(keyword.value, (ast.List, ast.Tuple))
        ):
            specs = keyword.value.elts
            sessions = [
                index for index, item in enumerate(specs)
                if isinstance(item, ast.Call) and item.args
                and isinstance(item.args[0], ast.Name)
                and item.args[0].id == "SessionMiddleware"
            ]
            contexts = [
                index for index, item in enumerate(specs)
                if isinstance(item, ast.Call) and item.args
                and isinstance(item.args[0], ast.Name)
                and item.args[0].id in context_names
            ]
            if len(sessions) == len(contexts) == 1 and sessions[0] > contexts[0]:
                session = specs.pop(sessions[0])
                specs.insert(contexts[0], session)
                changed = True
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            invalid = [
                keyword for keyword in call.keywords
                if keyword.arg == "lifespan"
                and any(
                    isinstance(node, ast.Name) and node.id in context_names
                    for node in ast.walk(keyword.value)
                )
            ]
            if invalid:
                call.keywords = [
                    keyword for keyword in call.keywords if keyword not in invalid
                ]
                changed = True

    csrf_protocols = [
        item for item in decisions if item.get("kind") == "provider_protocol"
        and item.get("constructor", {}).get("module") == "flask_wtf.csrf"
        and path in item.get("files", [])
    ]
    if factory is not None and csrf_protocols:
        csrf_names = {item["symbol"] for item in csrf_protocols}
        for item in csrf_protocols:
            modules = _module_names(item["provider"])
            csrf_names.update(
                alias.asname or alias.name
                for statement in tree.body if isinstance(statement, ast.ImportFrom)
                and _resolve_module(statement.module, statement.level, path) in modules
                for alias in statement.names if alias.name == item["symbol"]
            )
        session_statement = next((
            statement for statement in factory.body
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "add_middleware"
            and statement.value.args
            and ast.unparse(statement.value.args[0]).split(".")[-1]
            == "SessionMiddleware"
        ), None)
        csrf_initializers = [
            statement for statement in factory.body
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "init_app"
            and isinstance(statement.value.func.value, ast.Name)
            and statement.value.func.value.id in csrf_names
        ]
        if (
            session_statement is not None and csrf_initializers
            and factory.body.index(session_statement) < max(
                factory.body.index(statement) for statement in csrf_initializers
            )
        ):
            factory.body.remove(session_statement)
            insert_at = max(
                factory.body.index(statement) for statement in csrf_initializers
            ) + 1
            factory.body.insert(insert_at, session_statement)
            changed = True
        context_statements = [
            statement for statement in factory.body
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "add_middleware"
            and statement.value.args
            and isinstance(statement.value.args[0], ast.Name)
            and statement.value.args[0].id in context_names
        ]
        if (
            len(context_statements) == 1 and csrf_initializers
            and factory.body.index(context_statements[0]) < max(
                factory.body.index(statement) for statement in csrf_initializers
            )
        ):
            context_statement = context_statements[0]
            factory.body.remove(context_statement)
            insert_at = max(
                factory.body.index(statement) for statement in csrf_initializers
            ) + 1
            factory.body.insert(insert_at, context_statement)
            changed = True

    global_hooks = [
        (item["path"], hook["function"])
        for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "request_hooks" and path in item.get("files", [])
        for hook in item.get("hooks", [])
        if hook.get("scope") == "before_app_request"
    ]
    returned_apps = {
        node.value.id
        for node in (ast.walk(factory) if factory is not None else ())
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
    }
    constructors = [
        statement.value
        for statement in (ast.walk(factory) if factory is not None else ())
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and isinstance(statement.value, ast.Call)
        and any(
            isinstance(target, ast.Name) and target.id in returned_apps
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
        )
    ]
    if len(constructors) == 1:
        depends_name = next((
            alias.asname or alias.name
            for statement in tree.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
            for alias in statement.names if alias.name == "Depends"
        ), None)
        if global_hooks and depends_name is None:
            fastapi_import = next((
                statement for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and statement.module == "fastapi" and statement.level == 0
            ), None)
            if fastapi_import is None:
                fastapi_import = ast.ImportFrom(
                    module="fastapi", names=[], level=0,
                )
                insert_at = 1 if (
                    tree.body and isinstance(tree.body[0], ast.Expr)
                    and isinstance(tree.body[0].value, ast.Constant)
                    and isinstance(tree.body[0].value.value, str)
                ) else 0
                while (
                    insert_at < len(tree.body)
                    and isinstance(tree.body[insert_at], ast.ImportFrom)
                    and tree.body[insert_at].module == "__future__"
                ):
                    insert_at += 1
                tree.body.insert(insert_at, fastapi_import)
            fastapi_import.names.append(ast.alias(name="Depends"))
            depends_name = "Depends"
            changed = True

        dependencies = next(
            (keyword for keyword in constructors[0].keywords
             if keyword.arg == "dependencies"),
            None,
        )
        for provider, name in sorted(global_hooks):
            provider_import = next((
                statement for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and _resolve_module(statement.module, statement.level, path)
                in _module_names(provider)
            ), None)
            local_name = next((
                alias.asname or alias.name
                for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and _resolve_module(statement.module, statement.level, path)
                in _module_names(provider)
                for alias in statement.names if alias.name == name
            ), None)
            local_name = local_name or name
            if provider_import is not None:
                selected = [
                    alias for alias in provider_import.names if alias.name == name
                ] or [ast.alias(name=name)]
                provider_import.names = [
                    alias for alias in provider_import.names if alias.name != name
                ]
                if not provider_import.names:
                    tree.body.remove(provider_import)
            else:
                selected = [ast.alias(
                    name=name, asname=local_name if local_name != name else None,
                )]
            if not any(
                isinstance(statement, ast.ImportFrom)
                and _resolve_module(statement.module, statement.level, path)
                in _module_names(provider)
                and any(alias.name == name for alias in statement.names)
                for statement in factory.body
            ):
                module = provider.removesuffix(".py").replace("/", ".")
                factory.body.insert(0, ast.ImportFrom(
                    module=module, names=selected, level=0,
                ))
                changed = True
            call = ast.Call(
                func=ast.Name(id=depends_name or "Depends", ctx=ast.Load()),
                args=[ast.Name(id=local_name, ctx=ast.Load())], keywords=[],
            )
            values = dependencies.value.elts if dependencies and isinstance(
                dependencies.value, (ast.List, ast.Tuple)
            ) else []
            if any(ast.dump(value) == ast.dump(call) for value in values):
                continue
            if dependencies is None:
                dependencies = ast.keyword(
                    arg="dependencies", value=ast.List(elts=[], ctx=ast.Load()),
                )
                constructors[0].keywords.append(dependencies)
                values = dependencies.value.elts
            if isinstance(dependencies.value, (ast.List, ast.Tuple)):
                dependencies.value.elts.append(deepcopy(call))
                changed = True

    aliases = decision.get("endpoint_aliases", []) if decision else []
    if aliases:
        for function in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            kept = [
                statement for statement in function.body
                if not (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Attribute)
                    and statement.value.func.attr in {"append", "extend", "insert"}
                    and ast.unparse(statement.value.func.value).endswith(
                        ".router.routes"
                    )
                )
            ]
            changed |= len(kept) != len(function.body)
            function.body = kept
    existing = {
        (
            node.args[0].value,
            next((
                keyword.value.value for keyword in node.keywords
                if keyword.arg == "name"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ), ""),
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
    missing = [
        alias for alias in aliases
        if (alias["path"], alias["name"]) not in existing
    ]
    if missing:
        returns = [
            (function, index, statement.value.id)
            for function in tree.body
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            for index, statement in enumerate(function.body)
            if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name)
        ]
        if len(returns) == 1:
            function, index, app_name = returns[0]
            function.body[index:index] = [
                ast.parse(
                    f"{app_name}.add_api_route({alias['path']!r}, lambda: None, "
                    f"name={alias['name']!r}, include_in_schema=False)"
                ).body[0]
                for alias in missing
            ]
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_resource_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Realize resource caching and remove duplicate facade-owned cleanup wiring."""
    decisions = [
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "resource_lifecycle" and item.get("module") == path
    ]
    tree = _parsed(content)
    if not decisions or tree is None:
        return content
    changed = False
    for decision in decisions:
        initializer_name = decision.get("initializer")
        cleanup_names = set(decision.get("cleanup_functions", []))
        cache_members = decision.get("context_cache_members", [])
        helper = next((
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == decision.get("symbol")
        ), None)
        connect = next((
            node for node in ast.walk(helper)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sqlite3" and node.func.attr == "connect"
        ), None) if helper is not None else None
        if (
            helper is not None and connect is not None
            and decision.get("sqlite_cross_thread") and len(cache_members) == 1
        ):
            cache = cache_members[0]
            ambient = next((
                item for item in (seam_plan or {}).get("decisions", {}).values()
                if item.get("kind") == "ambient_context_runtime"
                and path in item.get("files", [])
            ), None)
            providers = (ambient or {}).get("runtime_providers", [])
            if not any(keyword.arg == "check_same_thread" for keyword in connect.keywords):
                connect.keywords.append(ast.keyword(
                    arg="check_same_thread", value=ast.Constant(value=False),
                ))
            config_keys = decision.get("config_keys", [])
            local_prefix = f"_portage_{decision['symbol']}"
            state_name = f"{local_prefix}_state"
            config_name = f"{local_prefix}_config"
            if len(config_keys) == 1 and connect.args and len(providers) != 1:
                connect.args[0] = ast.Subscript(
                    value=ast.Name(id=config_name, ctx=ast.Load()),
                    slice=ast.Constant(config_keys[0]), ctx=ast.Load(),
                )
            elif len(config_keys) == 1 and connect.args:
                connect.args[0] = ast.Subscript(
                    value=ast.Attribute(
                        value=ast.Name(id="current_app", ctx=ast.Load()),
                        attr="config", ctx=ast.Load(),
                    ),
                    slice=ast.Constant(config_keys[0]), ctx=ast.Load(),
                )
            row_factory = next((
                deepcopy(statement.value)
                for statement in ast.walk(helper)
                if isinstance(statement, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Attribute) and target.attr == "row_factory"
                    for target in (
                        statement.targets if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                )
            ), None)
            if len(providers) == 1:
                initialize = [ast.Assign(
                    targets=[ast.Attribute(
                        value=ast.Name(id="g", ctx=ast.Load()),
                        attr=cache, ctx=ast.Store(),
                    )],
                    value=deepcopy(connect),
                )]
                if row_factory is not None and not decision.get("row_factory_each_call"):
                    initialize.append(ast.Assign(
                        targets=[ast.Attribute(
                            value=ast.Attribute(
                                value=ast.Name(id="g", ctx=ast.Load()),
                                attr=cache, ctx=ast.Load(),
                            ),
                            attr="row_factory", ctx=ast.Store(),
                        )],
                        value=row_factory,
                    ))
                helper.body = [
                    ast.If(
                        test=ast.Compare(
                            left=ast.Constant(cache), ops=[ast.NotIn()],
                            comparators=[ast.Name(id="g", ctx=ast.Load())],
                        ),
                        body=initialize, orelse=[],
                    ),
                    *(
                        [ast.Assign(
                            targets=[ast.Attribute(
                                value=ast.Attribute(
                                    value=ast.Name(id="g", ctx=ast.Load()),
                                    attr=cache, ctx=ast.Load(),
                                ),
                                attr="row_factory", ctx=ast.Store(),
                            )],
                            value=deepcopy(row_factory),
                        )]
                        if row_factory is not None
                        and decision.get("row_factory_each_call")
                        else []
                    ),
                    ast.Return(value=ast.Attribute(
                        value=ast.Name(id="g", ctx=ast.Load()),
                        attr=cache, ctx=ast.Load(),
                    )),
                ]
                cleanup_source = (
                    f"_portage_{cache} = g.pop({cache!r}, None)\n"
                    f"if _portage_{cache} is not None:\n"
                    f"    _portage_{cache}.close()\n"
                )
                imported = next((
                    statement for statement in tree.body
                    if isinstance(statement, ast.ImportFrom)
                    and _resolve_module(statement.module, statement.level, path)
                    in _module_names(providers[0])
                ), None)
                if imported is None:
                    imported = ast.ImportFrom(
                        module=providers[0].removesuffix(".py").replace("/", "."),
                        names=[], level=0,
                    )
                    tree.body.insert(_module_import_index(tree), imported)
                required = {"g", *(("current_app",) if config_keys else ())}
                present = {alias.name for alias in imported.names}
                imported.names.extend(
                    ast.alias(name=name) for name in sorted(required - present)
                )
            else:
                context_name = f"{local_prefix}_context"
                if not any(
                    isinstance(statement, ast.ImportFrom)
                    and statement.module == "contextvars"
                    and any(
                        alias.name == "ContextVar"
                        and alias.asname == "_PortageContextVar"
                        for alias in statement.names
                    )
                    for statement in tree.body
                ):
                    tree.body.insert(_module_import_index(tree), ast.ImportFrom(
                        module="contextvars",
                        names=[ast.alias(
                            name="ContextVar", asname="_PortageContextVar",
                        )],
                        level=0,
                    ))
                if not any(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == state_name for node in tree.body
                ):
                    insert_at = tree.body.index(helper)
                    tree.body[insert_at:insert_at] = ast.parse(
                        f"{context_name} = _PortageContextVar("
                        f"{context_name!r}, default=None)\n"
                        f"{config_name} = {{}}\n"
                        f"def {state_name}():\n"
                        f"    state = {context_name}.get()\n"
                        "    if state is None:\n"
                        "        state = {}\n"
                        f"        {context_name}.set(state)\n"
                        "    return state\n"
                    ).body
                state = "_portage_state"
                cache_value = ast.Subscript(
                    value=ast.Name(id=state, ctx=ast.Load()),
                    slice=ast.Constant(cache), ctx=ast.Load(),
                )
                initialize = [ast.Assign(
                    targets=[deepcopy(cache_value)], value=deepcopy(connect),
                )]
                if row_factory is not None and not decision.get("row_factory_each_call"):
                    initialize.append(ast.Assign(
                        targets=[ast.Attribute(
                            value=deepcopy(cache_value), attr="row_factory", ctx=ast.Store(),
                        )],
                        value=row_factory,
                    ))
                helper.body = [
                    ast.Assign(
                        targets=[ast.Name(id=state, ctx=ast.Store())],
                        value=ast.Call(
                            func=ast.Name(id=state_name, ctx=ast.Load()),
                            args=[], keywords=[],
                        ),
                    ),
                    ast.If(
                        test=ast.Compare(
                            left=ast.Constant(cache), ops=[ast.NotIn()],
                            comparators=[ast.Name(id=state, ctx=ast.Load())],
                        ),
                        body=initialize, orelse=[],
                    ),
                    *(
                        [ast.Assign(
                            targets=[ast.Attribute(
                                value=deepcopy(cache_value),
                                attr="row_factory", ctx=ast.Store(),
                            )],
                            value=deepcopy(row_factory),
                        )]
                        if row_factory is not None
                        and decision.get("row_factory_each_call")
                        else []
                    ),
                    ast.Return(value=deepcopy(cache_value)),
                ]
                cleanup_source = (
                    f"_portage_state = {state_name}()\n"
                    f"_portage_{cache} = _portage_state.pop({cache!r}, None)\n"
                    f"if _portage_{cache} is not None:\n"
                    f"    _portage_{cache}.close()\n"
                )
            for cleanup in (
                node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in cleanup_names
            ):
                cleanup.body = ast.parse(cleanup_source).body
            changed = True
        initializer = next((
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == initializer_name
        ), None)
        if initializer is None or not initializer.args.args:
            continue
        app_name = initializer.args.args[0].arg
        local_config = f"_portage_{decision['symbol']}_config"
        if len(decision.get("config_keys", [])) == 1 and any(
            isinstance(target, ast.Name) and target.id == local_config
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
        ):
            config_update = ast.parse(
                f"{local_config}.update(getattr({app_name}.state, 'config', {{}}))"
            ).body[0]
            if not any(ast.dump(item) == ast.dump(config_update) for item in initializer.body):
                initializer.body.insert(0, config_update)
                changed = True

        def duplicate_cleanup(
            statement: ast.stmt,
            prefix: str = f"{app_name}.state.",
            cleanups: frozenset[str] = frozenset(cleanup_names),
        ) -> bool:
            return bool(
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Attribute)
                and statement.value.func.attr == "append"
                and ast.unparse(statement.value.func.value).startswith(prefix)
                and statement.value.args
                and isinstance(statement.value.args[0], ast.Name)
                and statement.value.args[0].id in cleanups
            )

        kept = [statement for statement in initializer.body if not duplicate_cleanup(statement)]
        changed |= len(kept) != len(initializer.body)
        initializer.body = kept
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_resource_consumers(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Keep yield dependencies in DI and ordinary callers on the direct helper."""
    decisions = [
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "resource_lifecycle"
        and item.get("module") != path and path in item.get("files", [])
        and item.get("symbol") and item.get("dependency")
    ]
    tree = _parsed(content)
    if not decisions or tree is None:
        return content

    bound = {
        name
        for statement in tree.body
        for name in (
            [statement.name]
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else [alias.asname or alias.name.split(".")[0] for alias in statement.names]
            if isinstance(statement, (ast.Import, ast.ImportFrom))
            else [target.id for target in statement.targets if isinstance(target, ast.Name)]
            if isinstance(statement, ast.Assign)
            else []
        )
    }
    imports: dict[str, tuple[ast.ImportFrom | None, str | None, str]] = {}
    for decision in decisions:
        owner = decision["module"]
        dependency = decision["dependency"]
        symbol = decision["symbol"]
        wanted = _module_names(owner)
        tails = {name.split(".")[-1] for name in wanted}
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom):
                module = _resolve_module(statement.module, statement.level, path)
                if module in wanted or module.split(".")[-1] in tails:
                    direct = next((
                        alias.asname or alias.name
                        for alias in statement.names if alias.name == symbol
                    ), None)
                    for alias in statement.names:
                        if alias.name == dependency:
                            imports[alias.asname or alias.name] = (
                                statement, direct, symbol,
                            )
                for alias in statement.names:
                    candidate = f"{module}.{alias.name}".lstrip(".")
                    if candidate in wanted:
                        local = alias.asname or alias.name
                        imports[f"{local}.{dependency}"] = (
                            None, f"{local}.{symbol}", symbol,
                        )
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.name in wanted or alias.name.split(".")[-1] in tails:
                        local = alias.asname or alias.name
                        imports[f"{local}.{dependency}"] = (
                            None, f"{local}.{symbol}", symbol,
                        )
    if not imports:
        return content

    parents = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    ordinary = {
        ref for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (ref := ast.unparse(node.func)) in imports
        and not (
            isinstance(parents.get(node), ast.Call)
            and ast.unparse(parents[node].func).split(".")[-1] == "Depends"
        )
    }
    direct_refs: dict[str, str] = {}
    for ref, (statement, direct, symbol) in imports.items():
        if ref not in ordinary:
            direct_refs[ref] = direct or symbol
            continue
        if direct is None and statement is not None:
            direct = symbol
            if direct in bound:
                stem = f"_portage_direct_{symbol}"
                direct = stem
                suffix = 2
                while direct in bound:
                    direct = f"{stem}_{suffix}"
                    suffix += 1
            statement.names.append(ast.alias(
                name=symbol, asname=None if direct == symbol else direct,
            ))
            bound.add(direct)
        direct_refs[ref] = direct or symbol

    class Realize(ast.NodeTransformer):
        @staticmethod
        def _ref(node: ast.AST) -> str:
            return ast.unparse(node)

        @staticmethod
        def _direct(call: ast.Call, ref: str) -> ast.Call:
            return ast.copy_location(ast.Call(
                func=ast.parse(direct_refs[ref], mode="eval").body,
                args=call.args, keywords=call.keywords,
            ), call)

        def visit_Await(self, node: ast.Await) -> ast.AST:
            if (
                isinstance(node.value, ast.Call)
                and (ref := self._ref(node.value.func)) in ordinary
            ):
                return self.visit(self._direct(node.value, ref))
            return self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> ast.AST:
            if self._ref(node.func).split(".")[-1] == "Depends":
                node.args = [
                    argument.func
                    if isinstance(argument, ast.Call)
                    and self._ref(argument.func) in imports
                    else self.visit(argument)
                    for argument in node.args
                ]
                node.keywords = [self.visit(keyword) for keyword in node.keywords]
                return node
            if (
                self._ref(node.func) == "next" and len(node.args) == 1
                and isinstance(node.args[0], ast.Call)
                and (ref := self._ref(node.args[0].func)) in ordinary
            ):
                return self.visit(self._direct(node.args[0], ref))
            node = self.generic_visit(node)
            ref = self._ref(node.func)
            return self._direct(node, ref) if ref in ordinary else node

    tree = Realize().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"
