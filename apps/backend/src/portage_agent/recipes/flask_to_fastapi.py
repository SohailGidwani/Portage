"""The Flask → FastAPI recipe (v1).

Detects Flask source, classifies each file's transformations, and builds behaviour-preserving
rewrite prompts. The migration deliberately spans the things deterministic tools can't do:
routing decorators, path/query/body parsing, blueprints→routers, error handlers→exception
handlers, the app factory, and the test-client seam.

Framework-agnostic modules (no `flask` import, not a test harness) are left alone — they're
the stable core the routes call, so the migration stays focused on the framework seam.
"""

from __future__ import annotations

import ast
import re
import textwrap
from copy import deepcopy
from pathlib import Path, PurePosixPath

from portage_agent.agent.nodes.common import (
    _module_names,
    _resolve_module,
    imported_bindings_from_sources,
)

from .base import MAX_CREATED_ARTIFACTS, PinRule, PlannedFile, Subtask, register
from .flask_test_compat import render_flask_test_compat

# --- marker → subtask detection -------------------------------------------------------
_FLASK_IMPORT = re.compile(r"^\s*(from\s+flask\b|import\s+flask\b)", re.MULTILINE)
_FLASK_FAMILY_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+"
    r"(?:flask(?:\b|_)|flask_login\b|flask_sqlalchemy\b|flask_restx\b)",
    re.MULTILINE,
)
_ROUTE = re.compile(r"\.route\s*\(|methods\s*=")
_BLUEPRINT = re.compile(r"\bBlueprint\s*\(")
_REQUEST_PARSE = re.compile(r"request\.(args|get_json|json|form|values|data)\b")
_ERRORHANDLER = re.compile(r"\berrorhandler\s*\(")
_APP_FACTORY = re.compile(r"\bFlask\s*\(|def\s+create_app\b")
_TEST_CLIENT = re.compile(r"\.test_client\s*\(|get_json\s*\(")
_TEMPLATES = re.compile(r"\brender_template\s*\(|\bget_flashed_messages\b")
_SESSION_FLASH = re.compile(
    r"\bflash\s*\(|(?<!\.)\bsession\[|(?<!\.)\bsession\.get\b|"
    r"(?<!\.)\bsession\.clear\b"
)
_G_CONTEXT = re.compile(r"\bg\.[a-zA-Z_]|before_app_request|before_request|\bcurrent_app\b")
_FLASK_LOGIN = re.compile(
    r"\bflask_login\b|\blogin_required\b|\bcurrent_user\b|\blogin_user\b|\blogout_user\b"
)
_AUTH_RUNTIME = re.compile(
    r"\blogin_required\b|\bcurrent_user\b|\blogin_user\b|\blogout_user\b"
)
_FLASK_SQLALCHEMY = re.compile(r"\bflask_sqlalchemy\b|\bSQLAlchemy\s*\(")
_CLI_SEAM = re.compile(r"\bapp\.cli\b|\btest_cli_runner\s*\(|\bclick\.|\.invoke\s*\(")
_CLICK_COMMAND = re.compile(
    r"@click\.command\(\s*['\"]([^'\"]+)['\"]\s*\)\s*"
    r"(?:@[^\n]+\s*)*def\s+(\w+)\s*\(",
    re.MULTILINE,
)
_TEMPLATE_URL_FOR = re.compile(
    r"\burl_for\(\s*['\"]([^'\"]+)['\"]"
)
_DIRECT_TEST_MEMBERS = ("app_context", "test_client", "test_cli_runner", "testing")
_DIRECT_TEST_CONTEXT_GLOBALS = frozenset({"g", "session", "current_app", "request"})
_ARTIFACT_BASENAME_BY_CAPABILITY = {
    "authentication": "authentication.py",
    "database_extension": "database.py",
    "direct_test_surface": "testing.py",
    "request_context": "context.py",
    "session_and_flash": "session.py",
    "template_rendering": "templating.py",
    "test_context_surface": "runtime_context.py",
}
_GENERAL_ARTIFACT_BASENAMES = ("runtime.py", "compat.py", "support.py", "adapters.py")
_ALLOWED_IMPORT_ROOTS = {
    "_portage_fastapi_test_compat",
    "click", "fastapi", "httpx", "itsdangerous", "jinja2", "multipart", "pydantic",
    "pytest", "sqlalchemy", "starlette", "uvicorn", "werkzeug",
}

# The flask_login API surface — single source for the auth_login pin rule's match set
# AND the request_context pin rule's carve-out (see pin_rules below), so the two
# function-kind rules stay disjoint by construction.
_FLASK_LOGIN_NAMES = {"login_user", "logout_user", "current_user", "login_required"}
_RESOURCE_FUNCTION = re.compile(
    r"^(get|open|connect)_(db|database|session|connection)$"
)


def _parsed(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _config_keys(tree: ast.AST) -> list[str]:
    """Literal keys read from a Flask-style ``*.config`` mapping."""
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "config"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "config"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "from_mapping"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "config"
        ):
            keys.update(keyword.arg for keyword in node.keywords if keyword.arg)
    return sorted(keys)


def _factory_config_facts(source: str) -> dict:
    tree = _parsed(source)
    if tree is None:
        return {"keys": [], "override_parameters": [], "optional_parameters": []}
    overrides = {
        node.args[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "update"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "config"
        and node.args
        and isinstance(node.args[0], ast.Name)
    }
    optional = set()
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        positional = [*function.args.posonlyargs, *function.args.args]
        for argument, default in zip(
            positional[-len(function.args.defaults):] if function.args.defaults else [],
            function.args.defaults,
            strict=True,
        ):
            if isinstance(default, ast.Constant) and default.value is None:
                optional.add(argument.arg)
        optional.update(
            argument.arg for argument, default in zip(
                function.args.kwonlyargs, function.args.kw_defaults, strict=True,
            )
            if isinstance(default, ast.Constant) and default.value is None
        )
    return {
        "keys": _config_keys(tree),
        "override_parameters": sorted(overrides),
        "optional_parameters": sorted(optional & overrides),
    }


def _resource_facts(source: str, helper_name: str) -> dict:
    tree = _parsed(source)
    if tree is None:
        return {
            "config_keys": [], "context_cache_members": [], "resource_files": [],
            "cleanup_functions": [], "initializer": "", "sqlite_cross_thread": False,
        }
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    helper = functions.get(helper_name)
    cache_members = sorted({
        node.attr for node in ast.walk(helper) if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name) and node.value.id == "g"
    }) if helper is not None else []
    resource_files = sorted({
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "open_resource"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    })
    cleanup_functions = sorted(
        name for name, function in functions.items()
        if name != helper_name and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "close"
            for node in ast.walk(function)
        )
    )
    return {
        "config_keys": _config_keys(tree),
        "context_cache_members": cache_members,
        "resource_files": resource_files,
        "cleanup_functions": cleanup_functions,
        "initializer": "init_app" if "init_app" in functions else "",
        "sqlite_cross_thread": any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sqlite3" and node.func.attr == "connect"
            for node in ast.walk(tree)
        ),
    }


def _click_command_contracts(files: dict[str, str]) -> list[dict]:
    contracts: list[dict] = []
    for path, source in files.items():
        tree = _parsed(source)
        if tree is None:
            continue
        functions = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        names = set(functions)
        for command_name, function_name in _CLICK_COMMAND.findall(source):
            function = functions.get(function_name)
            if function is None:
                continue
            contracts.append({
                "name": command_name,
                "function": function_name,
                "module": path,
                # Module-global lookup is intentional: tests may monkeypatch this handler.
                "handlers": sorted({
                    node.func.id for node in ast.walk(function)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in names and node.func.id != function_name
                }),
            })
    return sorted(contracts, key=lambda item: (item["module"], item["function"]))


def _initializer_contracts(
    files: dict[str, str], planned: list[PlannedFile], owner: str, symbol: str,
) -> list[dict]:
    roles = {item.path: item.role for item in planned}
    contracts = []
    for binding in imported_bindings_from_sources(files, owner):
        if roles.get(binding.importer) != "app_factory":
            continue
        tree = _parsed(files.get(binding.importer, ""))
        if tree is None:
            continue
        target = binding.local if binding.symbol == symbol else (
            f"{binding.local}.{symbol}" if binding.symbol is None else ""
        )
        if target and any(
            isinstance(node, ast.Call) and ast.unparse(node.func) == target
            for node in ast.walk(tree)
        ):
            contracts.append({
                "factory": binding.importer,
                "provider": owner,
                "symbol": symbol,
                "original_call": f"{target}(app)",
            })
    return contracts


def _plain_string_routes(source: str) -> list[str]:
    tree = _parsed(source)
    if tree is None:
        return []
    return sorted({
        function.name
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "route"
            for decorator in function.decorator_list
        )
        and any(
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            for node in ast.walk(function)
        )
    })


def _factory_endpoint_aliases(source: str) -> list[dict]:
    tree = _parsed(source)
    if tree is None:
        return []
    aliases = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_url_rule"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        endpoint = next(
            (keyword.value.value for keyword in node.keywords
             if keyword.arg == "endpoint"
             and isinstance(keyword.value, ast.Constant)
             and isinstance(keyword.value.value, str)),
            "",
        )
        if endpoint:
            aliases.append({"path": node.args[0].value, "name": endpoint})
    return aliases


def _route_name_contracts(source: str) -> list[dict]:
    """Freeze Flask's blueprint-qualified reverse-URL names."""
    tree = _parsed(source)
    if tree is None:
        return []
    blueprints = {
        target.id: statement.value.args[0].value
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and isinstance(statement.value, ast.Call)
        and (
            isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "Blueprint"
            or isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "Blueprint"
        )
        and statement.value.args
        and isinstance(statement.value.args[0], ast.Constant)
        and isinstance(statement.value.args[0].value, str)
        for target in (
            statement.targets if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if isinstance(target, ast.Name)
    }
    contracts = []
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        for decorator in function.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "route"
                and isinstance(decorator.func.value, ast.Name)
            ):
                continue
            endpoint = next(
                (keyword.value.value for keyword in decorator.keywords
                 if keyword.arg == "endpoint"
                 and isinstance(keyword.value, ast.Constant)
                 and isinstance(keyword.value.value, str)),
                function.name,
            )
            receiver = decorator.func.value.id
            prefix = blueprints.get(receiver)
            contracts.append({
                "function": function.name,
                "receiver": receiver,
                "name": f"{prefix}.{endpoint}" if prefix else endpoint,
            })
    return contracts


def _view_decorator_contracts(source: str) -> list[dict]:
    """Record decorators whose returned wrapper preserves the wrapped signature."""
    tree = _parsed(source)
    if tree is None:
        return []
    contracts = []
    for function in (
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.args.args
    ):
        nested = {
            node.name: node for node in function.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        returned = {
            node.value.id for node in function.body
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
        }
        for name in sorted(returned & nested.keys()):
            wrapper = nested[name]
            if any(
                isinstance(decorator, ast.Call)
                and ast.unparse(decorator.func).split(".")[-1] == "wraps"
                for decorator in wrapper.decorator_list
            ):
                contracts.append({
                    "function": function.name,
                    "parameter": function.args.args[0].arg,
                    "wrapper": name,
                })
    return contracts


def _factory_static_mount(files: dict[str, str], factory_path: str) -> dict:
    package = PurePosixPath(factory_path).parent
    static_root = package / "static" if str(package) != "." else PurePosixPath("static")
    has_static = any(
        static_root == PurePosixPath(path)
        or static_root in PurePosixPath(path).parents
        for path in files
    )
    template_uses_static = any(
        PurePosixPath(path).suffix.lower() in {".html", ".jinja", ".jinja2"}
        and "static" in _TEMPLATE_URL_FOR.findall(source)
        for path, source in files.items()
    )
    return (
        {"path": "/static", "name": "static", "directory": static_root.as_posix()}
        if has_static and template_uses_static else {}
    )


def _mixed_form_routes(source: str) -> list[dict]:
    """GET+POST Flask routes whose form fields must stay optional on GET."""
    tree = _parsed(source)
    if tree is None:
        return []
    routes = []
    for function in (
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        methods: set[str] = set()
        for decorator in function.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "route"
            ):
                continue
            methods = {"GET"}
            value = next(
                (keyword.value for keyword in decorator.keywords
                 if keyword.arg == "methods"),
                None,
            )
            if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                methods = {
                    item.value.upper() for item in value.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
        if not {"GET", "POST"} <= methods:
            continue
        fields = sorted({
            node.slice.value
            for node in ast.walk(function)
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "request" and node.value.attr == "form"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        })
        if fields:
            routes.append({"function": function.name, "fields": fields})
    return routes


def _request_hook_facts(source: str) -> list[dict]:
    tree = _parsed(source)
    if tree is None:
        return []
    hooks = []
    for function in (
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        decorators = {
            decorator.func.attr
            for decorator in function.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in {"before_app_request", "before_request"}
        } | {
            decorator.attr
            for decorator in function.decorator_list
            if isinstance(decorator, ast.Attribute)
            and decorator.attr in {"before_app_request", "before_request"}
        }
        if not decorators:
            continue
        hooks.append({
            "function": function.name,
            "scope": sorted(decorators)[0],
            "session_keys": sorted({
                node.args[0].value
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "session" and node.func.attr == "get"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            }),
            "context_members": sorted({
                node.attr for node in ast.walk(function)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name) and node.value.id == "g"
            }),
        })
    return hooks


def _normalize_template_response(content: str) -> str:
    """Mechanically upgrade the one deprecated Starlette template call shape.

    GPT-4o repeatedly reproduces the old API after exact feedback. This transform is
    framework-level and semantics-preserving: it only runs when a real Jinja2Templates
    instance and an enclosing request argument make the rewrite unambiguous.
    """
    tree = _parsed(content)
    if tree is None:
        return content
    instances = {
        target.id
        for statement in tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and isinstance(statement.value, ast.Call)
        and (
            isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "Jinja2Templates"
            or isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "Jinja2Templates"
        )
        for target in (
            statement.targets if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if isinstance(target, ast.Name)
    }
    if not instances:
        return content
    direct_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module in {"starlette.responses", "fastapi.responses"}
        for alias in statement.names if alias.name == "TemplateResponse"
    }

    class Upgrade(ast.NodeTransformer):
        def __init__(self):
            self.requests: list[str] = []
            self.changed = False

        def _function(self, node):
            positional = [*node.args.posonlyargs, *node.args.args]
            self.requests.append(positional[0].arg if positional else "")
            node = self.generic_visit(node)
            self.requests.pop()
            return node

        visit_FunctionDef = _function
        visit_AsyncFunctionDef = _function

        def visit_Call(self, node):  # noqa: N802
            node = self.generic_visit(node)
            request_name = self.requests[-1] if self.requests else ""
            if not request_name or not node.args:
                return node
            direct = isinstance(node.func, ast.Name) and node.func.id in direct_names
            bound = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "TemplateResponse"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in instances
            )
            if not (direct or bound):
                return node
            if (
                isinstance(node.args[0], ast.Name)
                and node.args[0].id == request_name
            ):
                return node
            context = node.args[1] if len(node.args) > 1 else ast.Dict(keys=[], values=[])
            if isinstance(context, ast.Dict):
                kept = [
                    (key, value) for key, value in zip(
                        context.keys, context.values, strict=True,
                    )
                    if not (
                        isinstance(key, ast.Constant) and key.value == "request"
                    )
                ]
                context = ast.Dict(
                    keys=[key for key, _ in kept],
                    values=[value for _, value in kept],
                )
            node.func = ast.Attribute(
                value=ast.Name(id=sorted(instances)[0], ctx=ast.Load()),
                attr="TemplateResponse", ctx=ast.Load(),
            )
            node.args = [
                ast.Name(id=request_name, ctx=ast.Load()), node.args[0], context,
            ]
            node.keywords = [kw for kw in node.keywords if kw.arg != "request"]
            self.changed = True
            return node

    upgrade = Upgrade()
    tree = upgrade.visit(tree)
    if not upgrade.changed:
        return content
    for statement in list(tree.body):
        if not (
            isinstance(statement, ast.ImportFrom)
            and statement.module in {"starlette.responses", "fastapi.responses"}
        ):
            continue
        statement.names = [
            alias for alias in statement.names if alias.name != "TemplateResponse"
        ]
        if not statement.names:
            tree.body.remove(statement)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_redirect_urls(content: str) -> str:
    """Keep Flask's relative ``url_for`` default in RedirectResponse calls."""
    tree = _parsed(content)
    if tree is None:
        return content

    class RelativeRedirect(ast.NodeTransformer):
        changed = False

        def visit_Call(self, node):  # noqa: N802
            self.generic_visit(node)
            if ast.unparse(node.func).split(".")[-1] != "RedirectResponse":
                return node
            targets = [keyword for keyword in node.keywords if keyword.arg == "url"]
            values = [target.value for target in targets] or node.args[:1]
            for value in values:
                if not (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "url_for"
                ):
                    continue
                relative = ast.Attribute(value=value, attr="path", ctx=ast.Load())
                if targets:
                    targets[0].value = relative
                else:
                    node.args[0] = relative
                self.changed = True
            return node

    normalizer = RelativeRedirect()
    tree = normalizer.visit(tree)
    if not normalizer.changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _normalize_werkzeug_abort(content: str) -> str:
    """Replace Flask/Werkzeug's implicit abort handling with FastAPI HTTPException."""
    tree = _parsed(content)
    if tree is None:
        return content
    abort_names = {
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.module == "werkzeug.exceptions"
        for alias in statement.names if alias.name == "abort"
    }
    uses_http_exception = any(
        isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        and node.id == "HTTPException"
        for node in ast.walk(tree)
    )
    imported_http_exception = any(
        isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
        and any(alias.name == "HTTPException" for alias in statement.names)
        for statement in tree.body
    )
    if not abort_names and (not uses_http_exception or imported_http_exception):
        return content
    for statement in list(tree.body):
        if not (
            isinstance(statement, ast.ImportFrom)
            and statement.module == "werkzeug.exceptions"
        ):
            continue
        statement.names = [alias for alias in statement.names if alias.name != "abort"]
        if not statement.names:
            tree.body.remove(statement)
    http_exception = next((
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
        for alias in statement.names if alias.name == "HTTPException"
    ), None)
    if http_exception is None:
        fastapi_import = next((
            statement for statement in tree.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "fastapi"
        ), None)
        if fastapi_import is None:
            fastapi_import = ast.ImportFrom(
                module="fastapi", names=[ast.alias(name="HTTPException")], level=0,
            )
            import_at = 1 if (
                tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)
                and isinstance(tree.body[0].value.value, str)
            ) else 0
            while (
                import_at < len(tree.body)
                and isinstance(tree.body[import_at], ast.ImportFrom)
                and tree.body[import_at].module == "__future__"
            ):
                import_at += 1
            tree.body.insert(import_at, fastapi_import)
        else:
            fastapi_import.names.append(ast.alias(name="HTTPException"))
        http_exception = "HTTPException"
    defined = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    helpers = [
        ast.parse(
            f"def {name}(status_code, description=None):\n"
            f"    raise {http_exception}(status_code=status_code, detail=description)"
        ).body[0]
        for name in sorted(abort_names - defined)
    ]
    insert_at = 1 if (
        tree.body and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ) else 0
    while insert_at < len(tree.body) and isinstance(
        tree.body[insert_at], (ast.Import, ast.ImportFrom)
    ):
        insert_at += 1
    tree.body[insert_at:insert_at] = helpers
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_factory_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Materialize frozen factory wiring that has one mechanical realization."""
    decisions = (seam_plan or {}).get("decisions", {}).values()
    decision = next((
        item for item in decisions
        if item.get("kind") == "application_factory" and item.get("factory") == path
    ), None)
    tree = _parsed(content)
    if tree is None:
        return content
    changed = False
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
        context_names = {
            alias.asname or alias.name
            for statement in tree.body if isinstance(statement, ast.ImportFrom)
            and (statement.module or "").split(".")[-1] in runtime_modules
            for alias in statement.names
        }
        for function in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
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
        for call in (
            node for node in ast.walk(tree) if isinstance(node, ast.Call)
        ):
            invalid = [
                keyword for keyword in call.keywords
                if keyword.arg == "lifespan"
                and isinstance(keyword.value, ast.Attribute)
                and isinstance(keyword.value.value, ast.Name)
                and keyword.value.value.id in context_names
            ]
            if invalid:
                call.keywords = [
                    keyword for keyword in call.keywords if keyword not in invalid
                ]
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
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
    }
    constructors = [
        statement.value
        for statement in ast.walk(tree)
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
            if local_name is None:
                if provider_import is None:
                    module = provider.removesuffix(".py").replace("/", ".")
                    provider_import = ast.ImportFrom(
                        module=module, names=[], level=0,
                    )
                    insert_at = 1 if (
                        tree.body and isinstance(tree.body[0], ast.Expr)
                        and isinstance(tree.body[0].value, ast.Constant)
                        and isinstance(tree.body[0].value.value, str)
                    ) else 0
                    while insert_at < len(tree.body) and isinstance(
                        tree.body[insert_at], (ast.Import, ast.ImportFrom)
                    ):
                        insert_at += 1
                    tree.body.insert(insert_at, provider_import)
                provider_import.names.append(ast.alias(name=name))
                local_name = name
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


def _realize_route_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Set mechanically-known FastAPI route names used by reverse lookup."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "route_names" and item.get("path") == path
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    expected: dict[str, set[str]] = {}
    for route in decision.get("routes", []):
        expected.setdefault(route["function"], set()).add(route["name"])
    changed = False
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and len(expected.get(node.name, ())) == 1
    ):
        name = next(iter(expected[function.name]))
        for decorator in function.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in {
                    "api_route", "get", "post", "put", "patch", "delete",
                    "options", "head",
                }
            ):
                continue
            keyword = next(
                (item for item in decorator.keywords if item.arg == "name"), None,
            )
            value = ast.Constant(name)
            if keyword is None:
                decorator.keywords.append(ast.keyword(arg="name", value=value))
            elif not (
                isinstance(keyword.value, ast.Constant)
                and keyword.value.value == name
            ):
                keyword.value = value
            else:
                continue
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_view_decorator_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Restore ``functools.wraps`` when the original view decorator used it."""
    decision = next((
        item for item in (seam_plan or {}).get("decisions", {}).values()
        if item.get("kind") == "view_decorators" and item.get("path") == path
    ), None)
    tree = _parsed(content)
    if decision is None or tree is None:
        return content
    functools_name = next((
        alias.asname or alias.name
        for statement in tree.body if isinstance(statement, ast.Import)
        for alias in statement.names if alias.name == "functools"
    ), None)
    wraps_name = next((
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "functools"
        for alias in statement.names if alias.name == "wraps"
    ), None)
    changed = False
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for contract in decision.get("decorators", []):
        function = functions.get(contract["function"])
        wrapper = next((
            node for node in (function.body if function else [])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == contract["wrapper"]
        ), None)
        if wrapper is None:
            continue
        wrapped = any(
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).split(".")[-1] == "wraps"
            for decorator in wrapper.decorator_list
        )
        if not wrapped:
            if functools_name is None and wraps_name is None:
                functools_name = "functools"
                insert_at = 1 if (
                    tree.body and isinstance(tree.body[0], ast.Expr)
                    and isinstance(tree.body[0].value, ast.Constant)
                    and isinstance(tree.body[0].value.value, str)
                ) else 0
                while insert_at < len(tree.body) and isinstance(
                    tree.body[insert_at], (ast.Import, ast.ImportFrom)
                ):
                    insert_at += 1
                tree.body.insert(
                    insert_at, ast.Import(names=[ast.alias(name="functools")]),
                )
            decorator = (
                ast.Name(id=wraps_name, ctx=ast.Load()) if wraps_name else
                ast.Attribute(
                    value=ast.Name(id=functools_name, ctx=ast.Load()),
                    attr="wraps", ctx=ast.Load(),
                )
            )
            wrapper.decorator_list.insert(0, ast.Call(
                func=decorator,
                args=[ast.Name(id=contract["parameter"], ctx=ast.Load())],
                keywords=[],
            ))
            changed = True
        wrapper_parameters = {
            argument.arg for argument in [
                *wrapper.args.posonlyargs, *wrapper.args.args, *wrapper.args.kwonlyargs,
            ]
        }
        for call in (
            node for node in ast.walk(wrapper)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == contract["parameter"]
        ):
            existing = {keyword.arg for keyword in call.keywords if keyword.arg}
            forwarded = [
                argument for argument in call.args
                if isinstance(argument, ast.Name)
                and argument.id in wrapper_parameters - existing
            ]
            if not forwarded:
                continue
            call.args = [argument for argument in call.args if argument not in forwarded]
            named = [
                ast.keyword(
                    arg=argument.id,
                    value=ast.Name(id=argument.id, ctx=ast.Load()),
                )
                for argument in forwarded
            ]
            splat = next(
                (index for index, keyword in enumerate(call.keywords)
                 if keyword.arg is None),
                len(call.keywords),
            )
            call.keywords[splat:splat] = named
            changed = True
    if not changed:
        return content
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _realize_resource_contracts(
    path: str, content: str, seam_plan: dict | None,
) -> str:
    """Remove duplicate cleanup wiring already owned by the frozen app facade."""
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
        initializer = next((
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == initializer_name
        ), None)
        if initializer is None or not initializer.args.args:
            continue
        app_name = initializer.args.args[0].arg

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


# R1 target-interface notes: module constants used BOTH inside the matching _SUBTASKS
# instruction and in pin_rules below — checklist and contract share the literal string,
# so they cannot drift (v3: single source, not a mirror-by-convention comment).
_NOTE_RESOURCE_FN = (
    "{name}: KEEP the original callable shape — same args, same return, callers do not "
    "change. For FastAPI DI, ADD a companion yield dependency that wraps it "
    "(`def {name}_dep(): resource = {name}(); try: yield resource; finally: close`) and "
    "use `Depends({name}_dep)` in endpoints; cleanup lives in the dependency, NEVER in "
    "callers (no bare `next(gen())` anywhere — it leaks the generator). If the original "
    "helper read Flask `current_app` configuration, keep its zero-argument public shape by "
    "having the existing factory/init seam copy configuration into module-owned state; "
    "NEVER make the helper read an undefined global app/request or FastAPI instance path."
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


_SUBTASKS: dict[str, Subtask] = {
    "app_factory": Subtask(
        "app_factory",
        "Migrate the application factory",
        "Replace the Flask app factory with a FastAPI one: `create_app()` must build a "
        "`FastAPI()` instance and `include_router(...)` the migrated router, keeping the "
        "function name `create_app` and its factory shape (other modules import it). "
        "Flask's `app.config` is a PLAIN DICT — migrate it as one: `app.state.config = {}` "
        "(a real dict), `config.from_mapping(...)`/`.update(...)` → dict update, "
        "`app.config['X']` → `app.state.config['X']`. Never call methods on "
        "`starlette.datastructures.State` itself (it has no update/get). FastAPI has no "
        "`instance_path`: compute the equivalent directory as a plain local `os.path` value "
        "and store only resulting config values, never `app.instance_path`.",
    ),
    "error_handler": Subtask(
        "error_handler",
        "Convert error handlers",
        "Convert every `@app.errorhandler(Exc)` into `@app.exception_handler(Exc)` that "
        "returns `fastapi.responses.JSONResponse(status_code=..., content=...)` with the "
        "SAME status code and JSON body as before.",
    ),
    "blueprint_to_router": Subtask(
        "blueprint_to_router",
        "Blueprint → APIRouter",
        "Replace `flask.Blueprint(...)` with `fastapi.APIRouter()`. Keep the EXACT "
        "module-level variable name importers expect (`bp`, `router`, or another original "
        "name); do not rename it merely to `router`.",
    ),
    "route_to_endpoint": Subtask(
        "route_to_endpoint",
        "Routes → typed endpoints",
        "Convert each `@bp.route('/p', methods=[M])` to the matching `@router.<method>('/p')`. "
        "Turn Flask path converters like `<int:item_id>` into FastAPI path params "
        "`/{item_id}` with a typed arg `item_id: int`. Preserve EVERY status code "
        "(e.g. 201 via `status_code=201`; a 204 by returning a 204 `Response`). "
        "Replace Werkzeug/Flask `abort(code, message)` with a raised FastAPI "
        "`HTTPException(status_code=code, detail=message)`.",
    ),
    "request_parsing": Subtask(
        "request_parsing",
        "Request parsing",
        "Replace `request.args.get(...)` with typed query parameters and `request.get_json()` "
        "with a JSON body parameter (a `dict` or a Pydantic model). Preserve optionality and "
        "defaults exactly (e.g. `?done=true` is an optional bool; a missing body is allowed).",
    ),
    "test_harness": Subtask(
        "test_harness",
        "Migrate the test client seam",
        "Rewrite framework PLUMBING only; every assertion must keep its exact meaning. "
        "Flask's `app.test_client()` → `fastapi.testclient.TestClient(app)`; "
        "`resp.get_json()` → `resp.json()`; `resp.get_data(as_text=True)` → `resp.text`; "
        "`client.get(..., follow_redirects=True)` keeps the same kwarg. A test that "
        "inspects `flask.session`/`g` directly (e.g. inside `with client:`) must assert "
        "the SAME fact through observable behaviour instead (response content, cookies, a "
        "follow-up request) — never delete or weaken the assertion. PRESERVE THE FILE'S "
        "STRUCTURE: do not add module-level statements the original didn't have (e.g. "
        "never call `create_app()` at import time if the original built the app inside a "
        "fixture or setUp). If Flask's bound CLI runner becomes `click.testing.CliRunner`, "
        "preserve the existing fixture/caller interface: either return a tiny adapter whose "
        "`invoke(args=[command, ...])` dispatches to the real exported Click command, or "
        "migrate only that invocation plumbing to `invoke(command, args=[...])`; never "
        "attach a fake runner to FastAPI/app.state.",
    ),
    "templates_render": Subtask(
        "templates_render",
        "render_template → Jinja2Templates",
        "Replace `render_template(name, **ctx)` with `fastapi.templating.Jinja2Templates`. "
        "Create ONE module-level `templates = Jinja2Templates(directory=...)` pointing at "
        "the EXISTING templates directory and return "
        "`templates.TemplateResponse(request, name, ctx)`. The .html files must NOT be "
        "edited, so every Jinja global they use must keep working: give every route a "
        "`name=` equal to its old Flask endpoint name (e.g. `name=\"blog.index\"`) so the "
        "templates' `url_for(...)` resolves via Starlette; inject anything else the "
        "templates reference (`g`, `get_flashed_messages`) into the context dict on every "
        "render (a small shared `render(request, name, **ctx)` helper is the clean way). "
        "`redirect(url)` becomes `RedirectResponse(url, status_code=302)` — NEVER the "
        "default 307, which re-sends POST bodies and breaks form flows.",
    ),
    "sessions_flash": Subtask(
        "sessions_flash",
        "session / flash → SessionMiddleware",
        "Add `starlette.middleware.sessions.SessionMiddleware` (a `secret_key` is required) "
        "to the app with `app.add_middleware(SessionMiddleware, ...)`. The middleware "
        "class signs cookie-backed ASGI sessions; it is NOT itself a session value or "
        "proxy and must never be instantiated into a module-level `session` variable. "
        "Flask's `session[...]` maps to the active `request.session[...]`. Implement "
        "`flash(msg)` as appending to `request.session.setdefault('_flashes', [])`, and "
        "provide `get_flashed_messages()` to templates as a per-render callable that POPS "
        "'_flashes' from the session (Flask semantics: read-once).",
    ),
    "auth_login": Subtask(
        "auth_login",
        "flask_login → session-based auth",
        "`flask_login` is Flask-only and there is NO drop-in FastAPI package in the "
        "allowed set (do NOT import `fastapi_login`/`fastapi_users` — they don't exist "
        "here). Reimplement the small surface actually used: `login_user(u)` → store the "
        "user id in `request.session`; `logout_user()` → remove it/clear the session; "
        "`current_user` → a dependency/helper that loads the user from the session and "
        "returns an anonymous stand-in with `is_authenticated=False` when absent (keep "
        "the attribute names templates/tests read); `@login_required` → a check that "
        "redirects (302) to the login page exactly like flask_login did."
        + "\nInterface decision: " + _NOTE_LOGIN_SURFACE,
    ),
    "sqlalchemy_plain": Subtask(
        "sqlalchemy_plain",
        "flask_sqlalchemy → plain SQLAlchemy",
        "`flask_sqlalchemy` needs a Flask app — replace it with PLAIN SQLAlchemy while "
        "keeping the module-level `db`-like surface everything imports: an `engine` + "
        "`SessionLocal = sessionmaker(...)` + a `Base(DeclarativeBase)`. `db.Model` "
        "subclasses become `Base` subclasses with the same `__tablename__`/columns "
        "(`db.Column(db.String(20))` → `Column(String(20))` — same types, same "
        "constraints). `db.session` uses become an explicit session (module-level scoped "
        "session is acceptable to keep call sites unchanged: `db_session.add/commit/...`). "
        "`db.create_all()`/`drop_all()` → `Base.metadata.create_all(engine)`/`drop_all`. "
        "Configure the engine from the SAME config value the app used "
        "(`SQLALCHEMY_DATABASE_URI`), resolved at create_app/init time."
        + "\nInterface decision: " + _NOTE_DB_SURFACE,
    ),
    "request_context": Subtask(
        "request_context",
        "g / before_request → dependencies",
        "Replace the `g` object and `before_app_request`/`before_request` hooks with "
        "explicit per-request wiring: a dependency (or helper called at the top of each "
        "endpoint) that computes what the hook stored on `g` (e.g. `g.user` from the "
        "session, `g.db` connection) and passes it to the endpoint and into the template "
        "context under the SAME attribute names the templates use. `current_app.config` "
        "moves to module-level config or the app instance. Preserve existing direct-call "
        "resource helpers such as `get_db()` for non-endpoint callers; FastAPI endpoints "
        "must acquire those resources through a companion yield dependency so teardown "
        "runs reliably. Do NOT register teardown as middleware (wrong signature), call a "
        "generator with bare `next(...)`, or use `app.state` as a context manager; "
        "`app.state` holds only config/constants."
        + "\nInterface decision for cross-file resource functions: " + _NOTE_RESOURCE_FN,
    ),
}


def _test_path_reason(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if base == "conftest.py":
        return 'basename "conftest.py" is reserved for pytest configuration'
    if base == "tests.py":
        return 'basename "tests.py" is a test module'
    if base.startswith("test_"):
        return f'basename "{base}" starts with forbidden prefix "test_"'
    if base.endswith("_test.py"):
        return f'basename "{base}" ends with forbidden suffix "_test.py"'
    if "tests" in PurePosixPath(path).parts:
        return 'path component "tests" marks a test tree'
    return ""


def _is_test_file(path: str) -> bool:
    return bool(_test_path_reason(path))


def _artifact_placement_contract(
    files: dict[str, str], planned: list[PlannedFile],
) -> dict[str, list[str]]:
    """Derive the prompt and validator's path rules from the repository layout."""
    roots: set[str] = set()
    for item in planned:
        if _is_test_file(item.path):
            continue
        parent = PurePosixPath(item.path).parent
        if str(parent) == ".":
            roots.add(".")
            continue
        parts = parent.parts
        package_root = next((
            "/".join(parts[:end])
            for end in range(1, len(parts) + 1)
            if f"{'/'.join(parts[:end])}/__init__.py" in files
        ), str(parent))
        roots.add(package_root)

    parents = {str(PurePosixPath(path).parent) for path in files}
    allowed = set(roots)
    for root in roots - {"."}:
        allowed.update(
            parent for parent in parents
            if parent == root or parent.startswith(f"{root}/")
        )

    test_roots = set()
    for path in files:
        if not _is_test_file(path):
            continue
        parts = PurePosixPath(path).parts
        if "tests" in parts:
            test_roots.add("/".join(parts[:parts.index("tests") + 1]))
        else:
            test_roots.add(str(PurePosixPath(path).parent))

    basenames = set(_GENERAL_ARTIFACT_BASENAMES)
    basenames.update(_ARTIFACT_BASENAME_BY_CAPABILITY.values())
    allowed_paths = sorted(
        candidate
        for parent in allowed
        for basename in basenames
        for candidate in [basename if parent == "." else f"{parent}/{basename}"]
        if candidate not in files and not _is_test_file(candidate)
    )

    return {
        "application_roots": sorted(roots),
        "test_roots": sorted(test_roots),
        "allowed_new_artifact_parent_directories": sorted(allowed),
        "allowed_new_artifact_paths": allowed_paths,
        "forbidden_test_path_rules": [
            'basename equals "conftest.py" or "tests.py"',
            'basename starts with "test_"',
            'basename ends with "_test.py"',
            'a path component equals "tests"',
        ],
    }


def _direct_test_context_imports(files: dict[str, str]) -> dict[str, list[dict]]:
    """Find exact, single-line Flask context imports eligible for deterministic plumbing."""
    found: dict[str, list[dict]] = {}
    for path, source in files.items():
        if not _is_test_file(path):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ImportFrom)
                and node.module == "flask"
                and node.level == 0
                and node.lineno == node.end_lineno
            ):
                continue
            selected = [
                alias for alias in node.names
                if alias.name in _DIRECT_TEST_CONTEXT_GLOBALS
            ]
            if not selected:
                continue
            before = lines[node.lineno - 1]
            # Exact-line replacement intentionally declines comments and compound
            # statements; those remain visible as unsupported rather than being reformatted.
            if "#" in before or ";" in before:
                continue
            found.setdefault(path, []).append({
                "line": node.lineno,
                "before": before,
                "selected": [
                    {"name": alias.name, "asname": alias.asname} for alias in selected
                ],
                "remaining": [
                    {"name": alias.name, "asname": alias.asname}
                    for alias in node.names if alias not in selected
                ],
            })
    return found


def _render_alias(alias: dict) -> str:
    return alias["name"] + (f" as {alias['asname']}" if alias.get("asname") else "")


def _attribute_names(source: str) -> set[str]:
    """Return statically visible attribute names without making survey parsing fatal."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


def _detected_artifact_surfaces(files: dict[str, str]) -> dict[str, list[str]]:
    patterns = {
        "template_rendering": _TEMPLATES,
        "session_and_flash": _SESSION_FLASH,
        "authentication": _AUTH_RUNTIME,
        "request_context": _G_CONTEXT,
        "database_extension": _FLASK_SQLALCHEMY,
        "direct_test_surface": _TEST_CLIENT,
    }
    surfaces = {
        name: sorted(path for path, source in files.items() if pattern.search(source))
        for name, pattern in patterns.items()
    }
    return {name: paths for name, paths in surfaces.items() if paths}


def _artifact_capability_requirements(
    files: dict[str, str], planned: list[PlannedFile],
) -> dict[str, dict]:
    """Derive the validator's exhaustive ownership checklist from source consumers."""
    targets = {item.path for item in planned}
    surfaces = _detected_artifact_surfaces(files)
    requirements = {
        capability: {
            "owner_rule": "exactly_one",
            "consumers": sorted(path for path in consumers if path in targets),
            "required_exports": [],
            "required_class_members": [],
        }
        for capability, consumers in surfaces.items()
        if len({path for path in consumers if path in targets}) >= 2
    }
    test_attributes = {
        path: _attribute_names(source)
        for path, source in files.items()
        if _is_test_file(path) and path.rsplit("/", 1)[-1] != "conftest.py"
    }
    direct_members = sorted({
        member
        for attributes in test_attributes.values()
        for member in _DIRECT_TEST_MEMBERS
        if member in attributes
    })
    if direct_members:
        requirements["direct_test_surface"] = {
            "owner_rule": "exactly_one",
            "consumers": sorted(
                item.path for item in planned if item.role == "app_factory"
            ),
            "required_exports": [],
            "required_class_members": direct_members,
        }
    direct_context_imports = _direct_test_context_imports(files)
    direct_context_imports = {
        path: imports for path, imports in direct_context_imports.items()
        if path in targets
    }
    if direct_context_imports:
        requirements["test_context_surface"] = {
            "owner_rule": "exactly_one",
            "consumers": sorted(direct_context_imports),
            "required_exports": sorted({
                alias["name"]
                for imports in direct_context_imports.values()
                for entry in imports for alias in entry["selected"]
            }),
            "required_export_kinds": {
                name: "variable"
                for name in sorted({
                    alias["name"]
                    for imports in direct_context_imports.values()
                    for entry in imports for alias in entry["selected"]
                })
            },
            "required_class_members": [],
        }
    return dict(sorted(requirements.items()))


class FlaskToFastAPIRecipe:
    name = "flask_to_fastapi"
    source_framework = "flask"
    target_framework = "fastapi"
    # Exactly what the network-off sandbox image ships (see sandbox/Dockerfile.sandbox).
    sandbox_packages = [
        "fastapi", "starlette", "uvicorn", "httpx", "pydantic", "pytest", "click",
        "itsdangerous",
    ]
    test_compat_path = "_portage_fastapi_test_compat.py"

    @staticmethod
    def render_test_compat() -> str:
        return render_flask_test_compat()

    @staticmethod
    def normalize_generated(
        path: str, content: str, seam_plan: dict | None = None,
    ) -> str:
        content = _normalize_template_response(content)
        content = _normalize_redirect_urls(content)
        content = _normalize_werkzeug_abort(content)
        content = _realize_view_decorator_contracts(path, content, seam_plan)
        content = _realize_route_contracts(path, content, seam_plan)
        content = _realize_resource_contracts(path, content, seam_plan)
        return _realize_factory_contracts(path, content, seam_plan)

    @staticmethod
    def render_created_artifact(file: PlannedFile, worktree: str) -> str | None:
        """Render only fixed framework plumbing selected by the frozen architecture.

        Business/service artifacts remain model-generated. These three shapes contain no
        repository business logic; asking a model to restate them caused measured retry
        loops and interface drift.
        """
        contract = file.artifact_contract or {}
        capabilities = set(contract.get("capabilities", []))
        exports = contract.get("exports", [])
        classes = [item for item in exports if item.get("kind") == "class"]
        functions = [item for item in exports if item.get("kind") == "function"]

        if capabilities & {"request_context", "session_and_flash"}:
            if len(classes) != 1 or set(classes[0].get("members", [])) - {
                "dispatch", "get_request_context", "manage_session",
            }:
                return None
            class_name = classes[0]["name"]
            return textwrap.dedent(f'''\
                from __future__ import annotations

                from collections.abc import Iterator, MutableMapping
                from contextvars import ContextVar, Token
                from typing import Any

                from starlette.middleware.base import BaseHTTPMiddleware
                from starlette.requests import Request


                _context: ContextVar[dict[str, Any] | None] = ContextVar(
                    "portage_request_context", default=None
                )
                # ponytail: this fallback exists only for synchronous post-request test
                # inspection; use per-client capture if concurrent inspection is needed.
                _last_context: dict[str, Any] = {{}}


                def _current() -> dict[str, Any]:
                    current = _context.get()
                    return _last_context if current is None else current


                def _push_context(state: dict[str, Any] | None = None) -> Token:
                    return _context.set(state if state is not None else {{"session": {{}}}})


                def _pop_context(token: Token) -> None:
                    _context.reset(token)


                class _GProxy:
                    def __contains__(self, name: str) -> bool:
                        return name in _current()

                    def __getattr__(self, name: str):
                        try:
                            return _current()[name]
                        except KeyError as exc:
                            raise AttributeError(name) from exc

                    def __setattr__(self, name: str, value) -> None:
                        _current()[name] = value

                    def pop(self, name: str, default=None):
                        return _current().pop(name, default)


                class _SessionProxy(MutableMapping):
                    @staticmethod
                    def _values() -> dict:
                        return _current().setdefault("session", {{}})

                    def __getitem__(self, key):
                        return self._values()[key]

                    def __setitem__(self, key, value) -> None:
                        self._values()[key] = value

                    def __delitem__(self, key) -> None:
                        del self._values()[key]

                    def __iter__(self) -> Iterator:
                        return iter(self._values())

                    def __len__(self) -> int:
                        return len(self._values())


                g = _GProxy()
                session = _SessionProxy()


                def flash(message) -> None:
                    session.setdefault("_flashes", []).append(message)


                def get_flashed_messages() -> list:
                    return session.pop("_flashes", [])


                class {class_name}(BaseHTTPMiddleware):
                    async def dispatch(self, request: Request, call_next):
                        state = {{"request": request, "session": request.session}}
                        token = _push_context(state)
                        try:
                            return await call_next(request)
                        finally:
                            _last_context.clear()
                            _last_context.update(state)
                            _pop_context(token)

                    @staticmethod
                    def get_request_context() -> dict[str, Any]:
                        return _current()

                    @staticmethod
                    def manage_session(request: Request) -> dict:
                        return request.session
                ''')

        if {"direct_test_surface", "test_context_surface"} <= capabilities:
            if len(classes) != 1 or set(classes[0].get("members", [])) - {
                "app_context", "testing",
            }:
                return None
            dependencies = contract.get("depends_on", [])
            if len(dependencies) != 1:
                return None
            module = dependencies[0].removesuffix(".py").replace("/", ".")
            class_name = classes[0]["name"]
            return textwrap.dedent(f'''\
                from __future__ import annotations

                from contextlib import contextmanager

                from fastapi import FastAPI

                from {module} import _pop_context, _push_context, g, session


                class {class_name}(FastAPI):
                    def __init__(
                        self, *args, testing=False, cleanup_callbacks=(), **kwargs
                    ):
                        super().__init__(*args, **kwargs)
                        self.testing = bool(testing)
                        self._cleanup_callbacks = tuple(cleanup_callbacks)

                    @contextmanager
                    def app_context(self):
                        token = _push_context()
                        try:
                            yield self
                        finally:
                            for callback in self._cleanup_callbacks:
                                callback()
                            _pop_context(token)
                ''')

        if capabilities == {"template_rendering"} and len(functions) == 1:
            template_dir = Path(worktree, file.path).parent / "templates"
            if not template_dir.is_dir():
                return None
            dependencies = contract.get("depends_on", [])
            runtime_import = ""
            runtime_context = ""
            if len(dependencies) == 1:
                module = dependencies[0].removesuffix(".py").replace("/", ".")
                runtime_import = (
                    f"from {module} import g, get_flashed_messages\n"
                )
                runtime_context = (
                    '"g": g, "get_flashed_messages": get_flashed_messages, '
                )
            function_name = functions[0]["name"]
            return textwrap.dedent(f'''\
                from __future__ import annotations

                from pathlib import Path

                from fastapi.templating import Jinja2Templates
                from starlette.requests import Request

                {runtime_import}
                templates = Jinja2Templates(
                    directory=str(Path(__file__).resolve().parent / "templates")
                )


                class _TemplateRequest:
                    def __init__(self, request: Request):
                        self._request = request

                    @property
                    def form(self):
                        return getattr(self._request, "_form", None) or {{}}

                    def url_for(self, name: str, **path_params):
                        if name.rsplit(".", 1)[-1] == "static" and "filename" in path_params:
                            path_params.setdefault("path", path_params.pop("filename"))
                        return self._request.url_for(name, **path_params).path

                    def __getattr__(self, name):
                        return getattr(self._request, name)


                def {function_name}(request: Request, template_name: str, **context):
                    values = {{{runtime_context}"request": _TemplateRequest(request), **context}}
                    return templates.TemplateResponse(request, template_name, values)
                ''')
        return None

    # R1: symbol-aware pin rules. applies() runs on the SymbolContract, so a file
    # carrying several idiom subtasks pins each symbol by what it IS. Predicates must
    # stay disjoint — build_manifest fails Plan loudly if two rules claim one symbol.
    # Disjointness argument: sqlalchemy_plain checks `c.kind == "variable"` while
    # request_context/auth_login only ever match `c.kind == "function"`, so the
    # variable-kind rule never overlaps the two function-kind rules. Between those two,
    # auth_login claims exactly the flask_login API names (_FLASK_LOGIN_NAMES);
    # request_context claims every OTHER function via an explicit name carve-out — the
    # same set feeds both predicates, so the two are disjoint by construction rather than
    # by luck (a custom `login_required` function resolves to auth_login only).
    pin_rules = [
        PinRule(subtask="request_context",
                applies=lambda c: c.kind == "function"
                and bool(_RESOURCE_FUNCTION.match(c.name)),
                note=_NOTE_RESOURCE_FN, preserve_shape=True, target_kind="function",
                additional_exports=("{name}_dep",)),
        PinRule(subtask="auth_login",
                applies=lambda c: c.name in _FLASK_LOGIN_NAMES,
                note=_NOTE_LOGIN_SURFACE, preserve_shape=True,
                target_kind="function"),
        PinRule(subtask="sqlalchemy_plain",
                applies=lambda c: c.kind == "variable" and "SQLAlchemy(" in c.signature,
                note=_NOTE_DB_SURFACE, target_kind="variable"),
    ]

    def matches(self, files: dict[str, str]) -> bool:
        return any(_FLASK_FAMILY_IMPORT.search(src) for src in files.values())

    @staticmethod
    def should_plan_artifacts(
        files: dict[str, str], planned: list[PlannedFile],
    ) -> bool:
        """Architect only when the source exposes a shared compatibility surface.

        Small JSON/factory fixtures remain on the deterministic facade path. Templates,
        sessions/auth/extensions, or direct test-client use without conftest are the
        measured cases where in-place rewriting has proved insufficient.
        """
        del planned
        complex_surface = any(
            pattern.search(source)
            for source in files.values()
            for pattern in (_TEMPLATES, _SESSION_FLASH, _FLASK_LOGIN, _FLASK_SQLALCHEMY)
        )
        direct_test_client = any(
            _is_test_file(path) and path.rsplit("/", 1)[-1] != "conftest.py"
            and ".test_client(" in source
            for path, source in files.items()
        )
        return complex_surface or direct_test_client or bool(
            _direct_test_context_imports(files)
        )

    def build_artifact_plan_prompt(
        self, *, files: dict[str, str], planned: list[PlannedFile],
        non_python_files: str, existing_python_paths: list[str],
        analysis_files: dict[str, str] | None = None,
    ) -> str:
        analysis = analysis_files or files
        targets = {item.path for item in planned}
        blocks = "\n\n".join(
            f"--- {path} ---\n{source}"
            for path, source in files.items() if path in targets
        )
        schema = (
            '[{"path":"package/helper.py","role":"support",'
            '"purpose":"...","instructions":"...",'
            '"capabilities":["template_rendering","session_and_flash"],'
            '"exports":[{"name":"symbol","kind":"function|class|variable",'
            '"signature":"optional","members":["class_member"]}],'
            '"consumers":["existing/planned.py"],'
            '"depends_on":["existing/or/proposed.py"]}]'
        )
        surfaces = _detected_artifact_surfaces(analysis)
        requirements = _artifact_capability_requirements(analysis, planned)
        placement = _artifact_placement_contract(analysis, planned)
        return (
            "Decide whether this Flask→FastAPI migration needs purposeful NEW Python "
            "modules to own shared context, session/auth, template, extension, or test "
            "compatibility behavior. Prefer zero artifacts when existing files plus the "
            "provided deterministic test facade suffice. Never propose a copy of business "
            "logic, a test file, or an existing path. New modules must be inside the "
            "application package, use only the recipe's allowed packages, and expose the "
            "smallest coherent surface. Every consumer must be one of the listed migration "
            "targets. Consumers are downstream: never list a consumer in `depends_on`, "
            "and keep proposed artifact dependencies acyclic. A capability family used "
            "by two or more migration targets MUST have "
            "one shared owner artifact unless an existing target-framework module already "
            "owns that complete surface; do not duplicate auth/session/template/context "
            "helpers independently in multiple routers. A proposed artifact must declare "
            "every target file that should import it. Return strict JSON matching this "
            "schema (or [] only when no shared/new ownership is needed):\n"
            f"{schema}\n\nNon-Python repository paths:\n{non_python_files}\n\n"
            f"Detected capability families (context only):\n{surfaces}\n\n"
            "REQUIRED CAPABILITY CHECKLIST — exhaustive and mechanically validated. "
            "Every capability key below must appear in exactly one artifact's "
            "`capabilities`. After ownership is selected, Plan deterministically adds the "
            "listed consumers, typed required exports, and uniquely attributable class "
            "members before validation. When `required_class_members` is nonempty, your "
            "owner must still declare one unambiguous CLASS export for Plan to complete.\n"
            f"{requirements}\n\n"
            f"You may create at most {MAX_CREATED_ARTIFACTS} artifacts. The checklist "
            "does NOT mean one file per capability: combine compatible capabilities in "
            "one coherent owner whenever needed to stay within the budget. When both "
            "`request_context` and `session_and_flash` are required, the same artifact "
            "must own both because they expose one request-scoped runtime state. "
            "Test-context exports and direct test surfaces may "
            "share an owner when their runtime state is the same. A `direct_test_surface` "
            "class is the target app wrapper/subclass returned by an application factory; "
            "do not place its app-facing members on an authentication/service helper, and "
            "combine it only with capabilities that the same app runtime can coherently "
            "own. Direct context exports "
            "must be real runtime-backed proxies, never constants or test-only fakes.\n\n"
            "REPOSITORY PLACEMENT CONTRACT — authoritative and mechanically validated. "
            "Every proposed `path` must be copied EXACTLY from "
            "`allowed_new_artifact_paths`; do not invent, prefix, suffix, or alter a path. "
            "Those choices are collision-free, use existing application directories, and "
            "satisfy every forbidden-test-path rule. Check each path against this contract "
            "before returning JSON:\n"
            f"{placement}\n\n"
            "Every proposed path must be new application code. Never create anything "
            "under a test directory, never name a test module, and never reuse any "
            "existing Python path from this explicit forbidden list:\n"
            f"{existing_python_paths}\n\n"
            f"Migration targets:\n{blocks}"
        )

    @staticmethod
    def materialize_artifact_contracts(
        plan: list[dict], files: dict[str, str], planned: list[PlannedFile],
    ) -> tuple[list[dict], list[dict]]:
        """Compile recipe-derived contract facts after the architect chooses ownership."""
        completed = deepcopy(plan)
        audit_by_path: dict[str, dict] = {}
        requirements = _artifact_capability_requirements(files, planned)
        factory_paths = sorted(
            item.path for item in planned if item.role == "app_factory"
        )

        for capability, requirement in requirements.items():
            owners = [
                item for item in completed
                if capability in item.get("capabilities", [])
            ]
            if len(owners) != 1:
                continue
            owner = owners[0]
            added_consumers = sorted(
                set(requirement["consumers"]) - set(owner.get("consumers", []))
            )
            if added_consumers:
                owner["consumers"] = sorted({
                    *owner.get("consumers", []), *added_consumers,
                })

            exports = {item["name"]: item for item in owner.get("exports", [])}
            added_exports = []
            for name, kind in sorted(
                requirement.get("required_export_kinds", {}).items()
            ):
                if name in exports:
                    continue
                export = {
                    "name": name, "kind": kind, "signature": "", "members": [],
                }
                owner["exports"].append(export)
                exports[name] = export
                added_exports.append({"name": name, "kind": kind})

            required_members = set(requirement["required_class_members"])
            added_class_members = []
            if required_members:
                classes = [
                    item for item in owner.get("exports", [])
                    if item.get("kind") == "class"
                ]
                touching = [
                    item for item in classes
                    if required_members & set(item.get("members", []))
                ]
                target = (
                    classes[0] if len(classes) == 1
                    else touching[0] if len(touching) == 1
                    else None
                )
                if target is not None:
                    missing = sorted(
                        required_members - set(target.get("members", []))
                    )
                    if missing:
                        target["members"] = sorted({*target.get("members", []), *missing})
                        added_class_members.append({
                            "export": target["name"], "members": missing,
                        })

            if not (added_consumers or added_exports or added_class_members):
                continue
            audit = audit_by_path.setdefault(owner["path"], {
                "path": owner["path"],
                "capabilities": [],
                "added_consumers": [],
                "added_exports": [],
                "added_class_members": [],
            })
            audit["capabilities"].append(capability)
            audit["added_consumers"] = sorted({
                *audit["added_consumers"], *added_consumers,
            })
            audit["added_exports"].extend(added_exports)
            audit["added_class_members"].extend(added_class_members)

        runtime_owners = [
            item for item in completed
            if set(item.get("capabilities", [])) & {"request_context", "session_and_flash"}
        ]
        test_owners = [
            item for item in completed
            if "test_context_surface" in item.get("capabilities", [])
        ]
        template_owners = [
            item for item in completed
            if "template_rendering" in item.get("capabilities", [])
        ]
        if len(template_owners) == 1:
            template_owner = template_owners[0]
            template_export = {
                "name": "render_template", "kind": "function", "signature": "",
                "members": [],
            }
            if (
                set(template_owner.get("capabilities", [])) == {"template_rendering"}
                and template_owner.get("exports") != [template_export]
            ):
                previous_exports = [
                    export.get("name") for export in template_owner.get("exports", [])
                ]
                template_owner["exports"] = [template_export]
                audit = audit_by_path.setdefault(template_owner["path"], {
                    "path": template_owner["path"],
                    "capabilities": [],
                    "added_consumers": [],
                    "added_exports": [],
                    "added_class_members": [],
                })
                audit["added_exports"].append({
                    "name": "render_template", "kind": "function",
                })
                audit["removed_exports"] = previous_exports
        if len(runtime_owners) == 1:
            runtime_owner = runtime_owners[0]
            combined_test_owner = (
                len(test_owners) == 1
                and test_owners[0]["path"] == runtime_owner["path"]
            )
            runtime_members = {"get_request_context", "manage_session"}
            runtime_classes = [
                export for export in runtime_owner.get("exports", [])
                if export.get("kind") == "class"
            ]
            removed_exports = []
            if len(runtime_classes) <= 1:
                removed_exports = [
                    export["name"] for export in runtime_owner.get("exports", [])
                    if export.get("kind") == "function"
                    and export.get("name") in runtime_members
                ]
                runtime_owner["exports"] = [
                    export for export in runtime_owner.get("exports", [])
                    if export.get("name") not in removed_exports
                ]
                if runtime_classes:
                    runtime_class = runtime_classes[0]
                    if not combined_test_owner:
                        runtime_class["members"] = sorted(
                            runtime_members
                            | (set(runtime_class.get("members", [])) & {"dispatch"})
                        )
                    added_runtime_export = []
                else:
                    names = {
                        export.get("name") for export in runtime_owner["exports"]
                    }
                    class_name = next(
                        name for name in (
                            "RequestContextMiddleware",
                            "FastAPIRequestContextMiddleware",
                        ) if name not in names
                    )
                    runtime_class = {
                        "name": class_name, "kind": "class", "signature": "",
                        "members": sorted(runtime_members),
                    }
                    runtime_owner["exports"].append(runtime_class)
                    added_runtime_export = [{"name": class_name, "kind": "class"}]
                if removed_exports or added_runtime_export:
                    audit = audit_by_path.setdefault(runtime_owner["path"], {
                        "path": runtime_owner["path"],
                        "capabilities": [],
                        "added_consumers": [],
                        "added_exports": [],
                        "added_class_members": [],
                    })
                    audit["added_exports"].extend(added_runtime_export)
                    audit["removed_exports"] = removed_exports
            added_factory_consumers = sorted(
                set(factory_paths) - set(runtime_owner.get("consumers", []))
            )
            if added_factory_consumers:
                runtime_owner["consumers"] = sorted({
                    *runtime_owner.get("consumers", []), *added_factory_consumers,
                })
                audit = audit_by_path.setdefault(runtime_owner["path"], {
                    "path": runtime_owner["path"],
                    "capabilities": [],
                    "added_consumers": [],
                    "added_exports": [],
                    "added_class_members": [],
                })
                audit["added_consumers"] = sorted({
                    *audit["added_consumers"], *added_factory_consumers,
                })
            runtime_note = (
                " Implement the sole context class as BaseHTTPMiddleware with async "
                "dispatch(request, call_next). It owns the shared ContextVar-backed "
                "request state but never calls app.add_middleware itself; the declared "
                "application-factory consumer imports and installs it."
            )
            if runtime_note.strip() not in runtime_owner["instructions"]:
                runtime_owner["instructions"] += runtime_note
                audit = audit_by_path.setdefault(runtime_owner["path"], {
                    "path": runtime_owner["path"],
                    "capabilities": [],
                    "added_consumers": [],
                    "added_exports": [],
                    "added_class_members": [],
                })
                audit["instruction_completed"] = True

            if len(test_owners) == 1:
                test_owner = test_owners[0]
                required_test_members = set(
                    requirements.get("direct_test_surface", {}).get(
                        "required_class_members", []
                    )
                )
                test_classes = [
                    export for export in test_owner.get("exports", [])
                    if export.get("kind") == "class"
                ]
                if (
                    "direct_test_surface" in test_owner.get("capabilities", [])
                    and len(test_classes) == 1 and required_test_members
                ):
                    previous_members = set(test_classes[0].get("members", []))
                    test_classes[0]["members"] = sorted(required_test_members)
                    removed_members = sorted(previous_members - required_test_members)
                    if removed_members:
                        audit = audit_by_path.setdefault(test_owner["path"], {
                            "path": test_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        audit["removed_class_members"] = [{
                            "export": test_classes[0]["name"],
                            "members": removed_members,
                        }]
                if runtime_owner["path"] != test_owner["path"]:
                    dependencies = set(test_owner.get("depends_on", []))
                    if runtime_owner["path"] not in dependencies:
                        test_owner["depends_on"] = sorted({
                            *dependencies, runtime_owner["path"],
                        })
                        audit = audit_by_path.setdefault(test_owner["path"], {
                            "path": test_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        audit["added_dependencies"] = [runtime_owner["path"]]
                test_note = (
                    f" Import and re-export the exact g/session proxies from "
                    f"{runtime_owner['path']}. The app facade constructor explicitly "
                    "accepts testing=False and cleanup_callbacks=(), stores the callbacks "
                    "on self, and its @contextmanager app_context(self) invokes them in "
                    "a finally block. Do not create another ContextVar or put cleanup "
                    "callbacks on app_context itself."
                )
                if test_note.strip() not in test_owner["instructions"]:
                    test_owner["instructions"] += test_note
                    audit = audit_by_path.setdefault(test_owner["path"], {
                        "path": test_owner["path"],
                        "capabilities": [],
                        "added_consumers": [],
                        "added_exports": [],
                        "added_class_members": [],
                    })
                    audit["instruction_completed"] = True

            if len(template_owners) == 1:
                template_owner = template_owners[0]
                if runtime_owner["path"] != template_owner["path"]:
                    dependencies = set(template_owner.get("depends_on", []))
                    if runtime_owner["path"] not in dependencies:
                        template_owner["depends_on"] = sorted({
                            *dependencies, runtime_owner["path"],
                        })
                        audit = audit_by_path.setdefault(template_owner["path"], {
                            "path": template_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        audit["added_dependencies"] = [runtime_owner["path"]]

        return completed, [audit_by_path[path] for path in sorted(audit_by_path)]

    @staticmethod
    def artifact_plan_violations(
        plan: list[dict], files: dict[str, str], planned: list[PlannedFile],
    ) -> list[str]:
        """Validate the exact deterministic checklist embedded in the architect prompt."""
        placement = _artifact_placement_contract(files, planned)
        allowed_paths = set(placement["allowed_new_artifact_paths"])
        out = []
        for item in plan:
            path = item["path"]
            reason = _test_path_reason(path)
            if reason:
                out.append(
                    f"artifact {path} is invalid: {reason}; choose a non-test "
                    "application-module name and preserve all other decisions"
                )
                continue
            if path not in allowed_paths:
                out.append(
                    f"artifact {path} is invalid: path must be selected exactly from "
                    f"allowed_new_artifact_paths {sorted(allowed_paths)!r}; preserve all "
                    "other decisions"
                )
        requirements = _artifact_capability_requirements(files, planned)
        request_owners = [
            item for item in plan
            if "request_context" in item.get("capabilities", [])
        ]
        session_owners = [
            item for item in plan
            if "session_and_flash" in item.get("capabilities", [])
        ]
        if (
            {"request_context", "session_and_flash"} <= set(requirements)
            and len(request_owners) == len(session_owners) == 1
            and request_owners[0]["path"] != session_owners[0]["path"]
        ):
            out.append(
                "request_context and session_and_flash require one shared "
                "request-scoped runtime owner"
            )
        for capability, requirement in requirements.items():
            owners = [
                item for item in plan if capability in item.get("capabilities", [])
            ]
            if len(owners) != 1:
                out.append(
                    f"{capability} requires exactly one owner artifact, got {len(owners)}"
                )
                continue
            owner = owners[0]
            missing = set(requirement["consumers"]) - set(owner.get("consumers", []))
            if missing:
                out.append(
                    f"{capability} owner {owner['path']} omits consumers {sorted(missing)}"
                )
            required_members = set(requirement["required_class_members"])
            if required_members:
                owning_classes = [
                    export.get("name")
                    for export in owner.get("exports", [])
                    if export.get("kind") == "class"
                    and required_members <= set(export.get("members", []))
                ]
                if len(owning_classes) != 1:
                    out.append(
                        f"{capability} owner {owner['path']} requires exactly one class "
                        f"export containing members {sorted(required_members)}, got "
                        f"{sorted(owning_classes)}"
                    )
            export_contracts = requirement.get("required_export_kinds", {})
            if export_contracts:
                exports = {
                    export.get("name"): export.get("kind")
                    for export in owner.get("exports", [])
                }
                missing_symbols = set(export_contracts) - set(exports)
                if missing_symbols:  # materialization should make this unreachable
                    out.append(
                        f"{capability} owner {owner['path']} omits exports "
                        f"{sorted(missing_symbols)}"
                    )
                wrong_kinds = {
                    name: {"actual": exports[name], "required": kind}
                    for name, kind in export_contracts.items()
                    if name in exports and exports[name] != kind
                }
                if wrong_kinds:
                    out.append(
                        f"{capability} owner {owner['path']} has wrong export kinds "
                        f"{wrong_kinds}"
                    )
        runtime_owners = [
            item for item in plan
            if set(item.get("capabilities", []))
            & {"request_context", "session_and_flash"}
        ]
        if len(runtime_owners) == 1:
            classes = [
                export for export in runtime_owners[0].get("exports", [])
                if export.get("kind") == "class"
            ]
            if len(classes) != 1:
                out.append(
                    f"runtime context owner {runtime_owners[0]['path']} requires exactly "
                    f"one middleware class export, got {len(classes)}"
                )
        for owner in (
            item for item in plan
            if {"direct_test_surface", "test_context_surface"}
            <= set(item.get("capabilities", []))
        ):
            classes = [
                export for export in owner.get("exports", [])
                if export.get("kind") == "class"
            ]
            if len(classes) != 1:
                out.append(
                    f"application test-surface owner {owner['path']} requires exactly "
                    f"one FastAPI facade class export, got {len(classes)}"
                )
        for owner in (
            item for item in plan
            if set(item.get("capabilities", [])) == {"template_rendering"}
        ):
            exports = owner.get("exports", [])
            if not (
                len(exports) == 1
                and exports[0].get("name") == "render_template"
                and exports[0].get("kind") == "function"
            ):
                out.append(
                    f"standalone template owner {owner['path']} requires exactly the "
                    "render_template function export"
                )
        return out

    @staticmethod
    def build_test_normalizations(
        files: dict[str, str], artifact_plan: list[dict],
    ) -> dict[str, dict]:
        """Freeze only import substitutions backed by an accepted artifact contract."""
        requirements = _direct_test_context_imports(files)
        owners = [
            item for item in artifact_plan
            if "test_context_surface" in item.get("capabilities", [])
        ]
        if len(owners) != 1:
            return {}
        owner = owners[0]
        exports = {export.get("name") for export in owner.get("exports", [])}
        consumers = set(owner.get("consumers", []))
        target_module = owner["path"].removesuffix(".py").replace("/", ".")
        normalizations: dict[str, dict] = {}
        for path, imports in requirements.items():
            required = {
                alias["name"] for entry in imports for alias in entry["selected"]
            }
            if path not in consumers or not required <= exports:
                continue
            replacements = []
            for entry in imports:
                indent = entry["before"][:len(entry["before"]) - len(entry["before"].lstrip())]
                rendered: list[str] = []
                if entry["remaining"]:
                    rendered.append(
                        indent + "from flask import "
                        + ", ".join(_render_alias(alias) for alias in entry["remaining"])
                    )
                rendered.append(
                    indent + f"from {target_module} import "
                    + ", ".join(_render_alias(alias) for alias in entry["selected"])
                )
                replacements.append({
                    "line": entry["line"],
                    "before": entry["before"],
                    "after": "\n".join(rendered),
                    "symbols": [alias["name"] for alias in entry["selected"]],
                })
            normalizations[path] = {
                "kind": "flask_context_import",
                "owner_path": owner["path"],
                "target_module": target_module,
                "replacements": replacements,
            }
        return normalizations

    def _classify(self, path: str, src: str) -> PlannedFile | None:
        subtasks: list[Subtask] = []
        role = ""
        order = 100

        is_flask = bool(_FLASK_FAMILY_IMPORT.search(src))

        # Test files first: a flask-importing test module is harness to adapt (plumbing
        # only, assertions preserved), never app code to redesign.
        if _is_test_file(path):
            if _TEST_CLIENT.search(src) or is_flask:
                role = "test_harness"
                order = 30
                subtasks.append(_SUBTASKS["test_harness"])
        # App factory before router: a file defining create_app()/Flask() migrates AFTER
        # the routers it includes (order 20 > 10), so it sees their migrated form; its own
        # routes are folded in below.
        elif is_flask and _APP_FACTORY.search(src):
            role = "app_factory"
            order = 20
            subtasks.append(_SUBTASKS["app_factory"])
            if _ERRORHANDLER.search(src):
                subtasks.append(_SUBTASKS["error_handler"])
            if _ROUTE.search(src):
                subtasks.append(_SUBTASKS["route_to_endpoint"])
            if _REQUEST_PARSE.search(src):
                subtasks.append(_SUBTASKS["request_parsing"])
        elif is_flask and (_BLUEPRINT.search(src) or _ROUTE.search(src)):
            role = "router"
            order = 10
            if _BLUEPRINT.search(src):
                subtasks.append(_SUBTASKS["blueprint_to_router"])
            if _ROUTE.search(src):
                subtasks.append(_SUBTASKS["route_to_endpoint"])
            if _REQUEST_PARSE.search(src):
                subtasks.append(_SUBTASKS["request_parsing"])
        elif is_flask:
            # Flask-importing support module (e.g. a db.py using g/current_app, an auth
            # helper using session) — no routes of its own, but it must still be migrated.
            role = "support"
            order = 15

        # Cross-cutting idioms: templates, sessions/flash, g/context (app code only —
        # test files keep their single strict harness subtask).
        if is_flask and role and role != "test_harness":
            if _TEMPLATES.search(src):
                subtasks.append(_SUBTASKS["templates_render"])
            if _SESSION_FLASH.search(src):
                subtasks.append(_SUBTASKS["sessions_flash"])
            if _FLASK_LOGIN.search(src):
                subtasks.append(_SUBTASKS["auth_login"])
            if _FLASK_SQLALCHEMY.search(src):
                subtasks.append(_SUBTASKS["sqlalchemy_plain"])
            if _G_CONTEXT.search(src):
                subtasks.append(_SUBTASKS["request_context"])
        if role == "support" and not subtasks:
            # Imports flask but uses none of the idioms we know — still needs the imports
            # swapped; the generic system rules cover it.
            subtasks.append(_SUBTASKS["request_context"])

        if not subtasks:
            return None
        return PlannedFile(path=path, role=role, subtasks=subtasks, order=order)

    def plan_files(self, files: dict[str, str]) -> list[PlannedFile]:
        planned = [pf for path, src in files.items() if (pf := self._classify(path, src))]
        planned.sort(key=lambda pf: (pf.order, pf.path))
        return planned

    def build_seam_plan(
        self, files: dict[str, str], planned: list[PlannedFile],
        manifest: dict[str, dict], units: list[dict],
    ) -> dict:
        """Deterministic Flask→FastAPI framework-owned interface decisions.

        Symbol pins decide Python call shapes. This companion artifact decides the
        *framework capability* seams that models otherwise hallucinate independently
        (`app_context`, fake `app.state` helpers, attached CLI runners). Rules are based
        only on recipe roles/subtasks and source idioms — never corpus names or tests.
        """
        by_path = {pf.path: pf for pf in planned}
        unit_for = {
            path: unit for unit in units for path in unit.get("paths", [])
        }
        decisions: dict[str, dict] = {}
        factory_paths = [pf.path for pf in planned if pf.role == "app_factory"]
        resource_contracts = {
            key: {
                **_resource_facts(files.get(pin["module"], ""), pin["symbol"]),
                "owner": pin["module"],
                "factory_initializers": _initializer_contracts(
                    files, planned, pin["module"], "init_app",
                ),
            }
            for key, pin in manifest.items()
            if pin.get("preserve_shape") and pin.get("additional_exports")
        }
        context_owner_paths = {
            pf.path for pf in planned
            if pf.action == "create" and set(
                (pf.artifact_contract or {}).get("capabilities", [])
            ) & {"direct_test_surface", "request_context", "test_context_surface"}
        }

        for pf in planned:
            if pf.role != "app_factory":
                continue
            members = unit_for.get(pf.path, {}).get("paths", [pf.path])
            config = _factory_config_facts(files.get(pf.path, ""))
            endpoint_aliases = _factory_endpoint_aliases(files.get(pf.path, ""))
            static_mount = _factory_static_mount(files, pf.path)
            initializers = [
                initializer
                for contract in resource_contracts.values()
                for initializer in contract["factory_initializers"]
                if initializer["factory"] == pf.path
            ]
            cleanup_callbacks = [
                {"provider": contract["owner"], "functions": contract["cleanup_functions"]}
                for contract in resource_contracts.values()
                if contract["cleanup_functions"] and any(
                    initializer["factory"] == pf.path
                    for initializer in contract["factory_initializers"]
                )
            ]
            decisions[f"application_factory:{pf.path}"] = {
                "kind": "application_factory",
                "factory": pf.path,
                "files": list(members),
                "instruction": (
                    "The target app is a real FastAPI instance. `app.state.config` may "
                    "hold a plain configuration dict, but app/app.state expose no invented "
                    "context managers, resource openers, database containers, test clients, "
                    "or CLI runners. Initialize framework-independent resources through "
                    "real exported project helpers and real FastAPI lifespan/dependencies. "
                    "FastAPI has no `instance_path`: compute any instance directory as a "
                    "plain local filesystem path in the factory and store only resulting "
                    f"configuration values. Original literal configuration keys are "
                    f"{config['keys']}; populate their defaults and apply overrides from "
                    f"{config['override_parameters']} before calling these original "
                    f"provider initializers: {initializers}. A planned app-facade class "
                    "that subclasses FastAPI must be constructed as the application "
                    "itself; do not build a separate FastAPI and pass it as an arbitrary "
                    "positional constructor argument. Pass these original resource cleanup "
                    f"functions into the facade for app-context exit: {cleanup_callbacks}. "
                    f"Override parameters {config['optional_parameters']} default to None; "
                    "guard them before `.get(...)` or other mapping access. "
                    f"Preserve original reverse-URL aliases {endpoint_aliases} with a "
                    "real named target route; never copy or mutate APIRoute objects. "
                    + (
                        f"Templates require the implicit Flask static endpoint: mount "
                        f"StaticFiles from package directory {static_mount['directory']} "
                        f"at {static_mount['path']} with name={static_mount['name']!r}, "
                        "resolving the directory from __file__."
                        if static_mount else ""
                    )
                ),
                "config_keys": config["keys"],
                "override_parameters": config["override_parameters"],
                "optional_parameters": config["optional_parameters"],
                "initializers": initializers,
                "cleanup_callbacks": cleanup_callbacks,
                "endpoint_aliases": endpoint_aliases,
                "static_mount": static_mount,
            }

        for pf in planned:
            decorators = _view_decorator_contracts(files.get(pf.path, ""))
            if decorators:
                decisions[f"view_decorators:{pf.path}"] = {
                    "kind": "view_decorators",
                    "files": [pf.path],
                    "path": pf.path,
                    "decorators": decorators,
                    "instruction": (
                        "Preserve these original view-decorator wrapper signatures with "
                        f"functools.wraps: {decorators}."
                    ),
                }

        for pf in planned:
            routes = _route_name_contracts(files.get(pf.path, ""))
            if routes:
                decisions[f"route_names:{pf.path}"] = {
                    "kind": "route_names",
                    "files": [pf.path],
                    "path": pf.path,
                    "routes": routes,
                    "instruction": (
                        "Preserve these exact Flask reverse-URL endpoint names on the "
                        f"corresponding FastAPI route decorators: {routes}."
                    ),
                }

        for pf in planned:
            plain_string_functions = _plain_string_routes(files.get(pf.path, ""))
            if not plain_string_functions:
                continue
            decisions[f"response_shape:{pf.path}"] = {
                "kind": "response_shape",
                "files": [pf.path],
                "path": pf.path,
                "plain_string_functions": plain_string_functions,
                "instruction": (
                    f"Original Flask routes {plain_string_functions} return a plain string "
                    "body. A bare string returned by FastAPI becomes a JSON string with "
                    "extra quote bytes; return an explicit HTMLResponse/PlainTextResponse "
                    "with the original text and status instead."
                ),
            }

        for pf in planned:
            routes = _mixed_form_routes(files.get(pf.path, ""))
            if routes:
                decisions[f"mixed_form_routes:{pf.path}"] = {
                    "kind": "mixed_form_routes",
                    "path": pf.path,
                    "files": [pf.path],
                    "routes": routes,
                    "instruction": (
                        f"Original mixed GET+POST form routes are {routes}. GET must render "
                        "without form input, so do not declare these fields as FastAPI "
                        "`Form(...)` parameters. Accept `Request`, and only for POST parse "
                        "`form = await request.form()` inside the method branch; preserve "
                        "the original missing/empty-field validation in application code."
                    ),
                }

        for pf in planned:
            hooks = _request_hook_facts(files.get(pf.path, ""))
            if hooks:
                decisions[f"request_hooks:{pf.path}"] = {
                    "kind": "request_hooks",
                    "path": pf.path,
                    "files": sorted({pf.path, *factory_paths}),
                    "hooks": hooks,
                    "instruction": (
                        f"Original pre-request hooks are {hooks}. Preserve each function "
                        "as real per-request FastAPI wiring (router/factory `Depends` or "
                        "equivalent middleware), not an unused helper. It must read the "
                        "same session keys and populate the same shared context members "
                        "before decorated/ordinary handlers run."
                    ),
                }

        for pf in planned:
            contract = pf.artifact_contract or {}
            if "direct_test_surface" not in contract.get("capabilities", []):
                continue
            classes = [
                export for export in contract.get("exports", [])
                if export.get("kind") == "class" and export.get("members")
            ]
            if not classes:
                continue
            owned = "; ".join(
                f"{export['name']} owns {', '.join(export['members'])}"
                for export in classes
            )
            consumers = contract.get("consumers", [])
            decisions[f"planned_test_surface:{pf.path}"] = {
                "kind": "planned_test_surface",
                "provider": pf.path,
                "files": sorted({pf.path, *consumers}),
                "instruction": (
                    f"The application-owned test surface is `{pf.path}`: {owned}. Its "
                    "class is the target application facade/wrapper returned or publicly "
                    "exported by the declared consumer—not an HTTP TestClient. The public "
                    "factory/export must return an instance of this exact class, not the "
                    "raw FastAPI object or an unused side instance. The facade must remain "
                    "ASGI-callable (subclass FastAPI or forward ASGI calls) so TestClient "
                    "can run it. Store `testing` from the factory's original test config on "
                    "the facade itself; never source it from invented `app.state.testing`. "
                    "`app_context()` must enter and reset the same runtime context used by "
                    "the exported context proxies. The "
                    "provider must accept configuration, callbacks, and other runtime "
                    "inputs through constructor arguments and must not import or construct "
                    "its consumer. The consumer "
                    "must import and construct the owner class. Implement the frozen "
                    "members directly in application code; never delegate them to "
                    "Portage's deterministic `_portage_fastapi_test_compat` infrastructure. "
                    "The application factory alone installs middleware. When an app context "
                    "owns context-cached resources, accept cleanup callbacks from the "
                    "factory and invoke them as app_context exits; do not reverse-import "
                    "resource consumers from this provider."
                ),
            }

        for pf in planned:
            contract = pf.artifact_contract or {}
            if "test_context_surface" not in contract.get("capabilities", []):
                continue
            exports = sorted(
                export["name"] for export in contract.get("exports", [])
                if export.get("kind") == "variable"
            )
            decisions[f"planned_test_context:{pf.path}"] = {
                "kind": "planned_test_context",
                "files": sorted({pf.path, *contract.get("consumers", [])}),
                "instruction": (
                    f"The runtime-backed context exports are {exports} from `{pf.path}`. "
                    "Each exported name is a proxy OBJECT, never a dict, function, or "
                    "accessor alias. Back the proxies with request-local state such as "
                    "stdlib `contextvars.ContextVar`; `g` must support attribute access and "
                    "`session` must support mapping operations. Request middleware and the "
                    "owned app-context manager must set/reset the same state so consumers "
                    "can inspect it safely during and immediately after a test-client "
                    "request. Keep accessor functions private behind the proxy objects."
                ),
            }

        session_paths = [
            pf.path for pf in planned
            if "session_and_flash" in (pf.artifact_contract or {}).get(
                "capabilities", []
            )
            or any(subtask.type == "sessions_flash" for subtask in pf.subtasks)
        ]
        if session_paths:
            session_provider_paths = sorted(
                pf.path for pf in planned
                if pf.action == "create" and "session_and_flash" in (
                    pf.artifact_contract or {}
                ).get("capabilities", [])
            )
            decisions["session_runtime"] = {
                "kind": "session_runtime",
                "files": sorted({*session_paths, *factory_paths}),
                "factory_files": sorted(factory_paths),
                "provider_files": session_provider_paths,
                "original_cookie_writer_files": sorted(
                    path for path in session_paths if ".set_cookie(" in files.get(path, "")
                ),
                "instruction": (
                    "Treat cookie signing and session access as two distinct surfaces. "
                    "Install the SessionMiddleware CLASS only in an application factory "
                    "with `app.add_middleware(SessionMiddleware, secret_key=...)`. Never "
                    "call/assign SessionMiddleware as a `session`, `g`, context, or proxy "
                    "object. Runtime session helpers/proxies must instead read the active "
                    "request's `request.session`; they do not own or instantiate ASGI "
                    "middleware."
                ),
            }

        runtime_context_providers = sorted(
            pf.path for pf in planned
            if pf.action == "create" and set(
                (pf.artifact_contract or {}).get("capabilities", [])
            ) & {"request_context", "session_and_flash"}
        )
        test_context_providers = sorted(
            pf.path for pf in planned
            if pf.action == "create" and "test_context_surface" in (
                pf.artifact_contract or {}
            ).get("capabilities", [])
        )
        if runtime_context_providers and test_context_providers:
            decisions["ambient_context_runtime"] = {
                "kind": "ambient_context_runtime",
                "files": sorted({
                    *runtime_context_providers, *test_context_providers, *factory_paths,
                    *session_paths,
                }),
                "runtime_providers": runtime_context_providers,
                "test_providers": test_context_providers,
                "factory_files": sorted(factory_paths),
                "instruction": (
                    "Use one ambient request-state implementation, never parallel "
                    "ContextVars. The runtime provider owns the live ContextVar-backed "
                    "g/session/flash state and request middleware; a distinct test-context "
                    "provider imports and re-exports those exact proxy objects. The "
                    "middleware binds `request.session`, initializes g before handlers, "
                    "and retains a post-request snapshot for sanctioned test inspection. "
                    "The factory installs context middleware before SessionMiddleware so "
                    "SessionMiddleware is outermost and session exists when context is "
                    "bound. The owned app_context enters the same store and invokes its "
                    "factory-supplied cleanup callbacks on exit."
                ),
            }

        template_provider_paths = sorted(
            pf.path for pf in planned
            if pf.action == "create" and "template_rendering" in (
                pf.artifact_contract or {}
            ).get("capabilities", [])
        )
        if template_provider_paths:
            template_functions = {
                pf.path: sorted(
                    export["name"]
                    for export in (pf.artifact_contract or {}).get("exports", [])
                    if export.get("kind") == "function"
                )
                for pf in planned if pf.path in template_provider_paths
            }
            template_files = sorted({
                *template_provider_paths,
                *(
                    consumer
                    for pf in planned if pf.path in template_provider_paths
                    for consumer in (pf.artifact_contract or {}).get("consumers", [])
                ),
            })
            decisions["template_runtime"] = {
                "kind": "template_runtime",
                "files": template_files,
                "provider_files": template_provider_paths,
                "provider_functions": template_functions,
                "instruction": (
                    "Use the current request-first Starlette/FastAPI template API: "
                    "`templates.TemplateResponse(request, name, context)`. A shared "
                    "render helper must accept only request and template name as required "
                    "positional arguments; template context is optional or `**kwargs`, so "
                    "a render with no extra context remains valid. The deprecated "
                    "two-positional-argument form emits a warning and fails suites that "
                    "correctly treat framework deprecations as errors."
                ),
            }

        for key, pin in manifest.items():
            if not (pin.get("preserve_shape") and pin.get("additional_exports")):
                continue
            owner = pin["module"]
            contract = resource_contracts[key]
            members = sorted({
                *unit_for.get(owner, {}).get("paths", [owner]), *context_owner_paths,
            })
            decisions[f"resource_lifecycle:{key}"] = {
                "kind": "resource_lifecycle",
                "files": list(members),
                "instruction": (
                    f"Keep direct helper `{pin['symbol']}` callable exactly as pinned by "
                    "the interface manifest for setup, CLI, and other non-endpoint code. "
                    f"Endpoints acquire it through `{pin['additional_exports'][0]}`. "
                    "Never add an app/request argument to the direct helper and never make "
                    "callers drive a generator manually. The factory must first build "
                    "`app.state.config` (including test overrides); the existing same-shape "
                    "`init_app(app)` may then copy required values into private module-owned "
                    "configuration used by the direct helper. The helper must not read a free "
                    "global app/request, `current_app`, or invented app attributes. "
                    f"Original resource facts are frozen: config keys "
                    f"{contract['config_keys']}, context-cache members "
                    f"{contract['context_cache_members']}, package-relative resources "
                    f"{contract['resource_files']}, cleanup functions "
                    f"{contract['cleanup_functions']}, and factory initializer wiring "
                    f"{contract['factory_initializers']}. Preserve one resource per active "
                    "runtime/app context, close and clear it when that context exits, and "
                    "resolve package resources relative to the owning module rather than "
                    "the process working directory. If bytes are decoded, open/read bytes. "
                    + (
                        "This provider uses sqlite3: FastAPI may create a dependency and "
                        "execute an async endpoint on different threads, so every migrated "
                        "sqlite3.connect call must set check_same_thread=False."
                        if contract["sqlite_cross_thread"] else ""
                    )
                    + (
                        " Implement the cache as module-owned request/app-context state "
                        "(for example ContextVar): the direct helper checks it before "
                        "opening, and each cleanup function closes and resets it. Never "
                        "store a live closable resource directly in a ContextVar; put it "
                        "inside a mutable context-state mapping so cleanup in a copied "
                        "request context clears the parent-visible entry. Never "
                        "delete the original cleanup function."
                        if contract["context_cache_members"] else ""
                    )
                ),
                "module": owner,
                "symbol": pin["symbol"],
                "dependency": pin["additional_exports"][0],
                **contract,
            }

        for pf in planned:
            if pf.role != "test_harness":
                continue
            members = unit_for.get(pf.path, {}).get("paths", [pf.path])
            decisions[f"test_harness:{pf.path}"] = {
                "kind": "test_harness",
                "files": list(members),
                "instruction": (
                    "Use `fastapi.testclient.TestClient(app)` for HTTP plumbing. Remove "
                    "Flask application-context blocks and call real exported setup helpers "
                    "directly with their pinned signatures. Do not invent methods or state "
                    "attributes on FastAPI to imitate Flask (`app.container`, resource "
                    "openers, `test_client`, `test_cli_runner`, or similar). Preserve every "
                    "assertion and its meaning."
                ),
            }

        cli_paths = [p for p, src in files.items() if p in by_path and _CLI_SEAM.search(src)]
        if cli_paths:
            command_bindings = _click_command_contracts(files)
            commands = {
                item["function"]: item["name"] for item in command_bindings
            }
            harness_files = sorted(
                pf.path for pf in planned
                if pf.role == "test_harness" and _CLI_SEAM.search(files.get(pf.path, ""))
            )
            factory_files = sorted(pf.path for pf in planned if pf.role == "app_factory")
            affected = sorted({
                member
                for path in cli_paths
                for member in unit_for.get(path, {}).get("paths", [path])
            } | {item["module"] for item in command_bindings}
              | set(harness_files) | set(factory_files))
            decisions["standalone_cli"] = {
                "kind": "standalone_cli",
                "files": affected,
                "instruction": (
                    "FastAPI has no Flask-style `app.cli` or `test_cli_runner`. Preserve a "
                    "real existing Click command as a standalone exported command and test "
                    "it with `click.testing.CliRunner`; otherwise validate the same setup "
                    "through public helpers/observable app behavior. Preserve an existing "
                    "runner fixture's `invoke(args=[command, ...])` interface with a small "
                    "dispatcher adapter when behavioural tests consume it; never attach a "
                    "fake CLI runner to app.state. When dispatching a command selected by "
                    "the first old-style args token, remove that command token before "
                    "passing the remaining args to the Click command. The adapter must "
                    "return `CliRunner().invoke(command, args=args[1:])` (a Click `Result`); "
                    "never call low-level `Command.main()` and never store commands/runners "
                    "on app.state. The original command ownership and late-bound handler "
                    f"calls are {command_bindings}. Command-owner modules keep those real "
                    "exports; only compatibility/test-harness wiring passes the exact "
                    "name→command mapping to `adapt_app`. Application factories must not "
                    "register, execute, or store Click commands."
                ),
                "commands": commands,
                "command_bindings": command_bindings,
                "harness_files": harness_files,
                "factory_files": factory_files,
            }

        original_import_roots = {}
        for pf in planned:
            tree = _parsed(files.get(pf.path, ""))
            original_import_roots[pf.path] = sorted({
                name.split(".")[0]
                for node in (ast.walk(tree) if tree is not None else [])
                for name in (
                    [node.module] if isinstance(node, ast.ImportFrom)
                    and node.level == 0 and node.module
                    else [alias.name for alias in node.names]
                    if isinstance(node, ast.Import) else []
                )
            })
        return {
            "version": 1, "decisions": decisions, "units": units,
            "allowed_import_roots": sorted(_ALLOWED_IMPORT_ROOTS),
            "original_import_roots": original_import_roots,
        }

    def system_prompt(self) -> str:
        return (
            "You are Portage, an expert code-migration agent. You migrate ONE Python source "
            "file from the Flask web framework to FastAPI, preserving behaviour exactly.\n\n"
            "Hard rules:\n"
            "1. Output ONLY the complete migrated file inside a single ```python fenced block. "
            "No prose before or after.\n"
            "2. Preserve all public names other modules rely on (module path, the "
            "`create_app` factory, exact router variable names, functions imported elsewhere).\n"
            "3. Preserve exact HTTP behaviour: same paths, methods, status codes (incl. 201/204), "
            "and identical response JSON shapes.\n"
            "4. Keep importing the project's own modules unchanged (e.g. `from . import store`); "
            "never reimplement or modify framework-agnostic logic.\n"
            "5. The test suite runs OFFLINE (no network). Import ONLY the Python standard "
            "library, this project's own modules, and these packages: "
            "fastapi, starlette, uvicorn, httpx, pydantic, pytest, jinja2, itsdangerous, "
            "sqlalchemy, click, werkzeug, python-multipart (needed for fastapi `Form(...)`; "
            "imported implicitly).\n"
            "6. Keep `from __future__ import annotations` if the original had it.\n"
            "7. Return plain Python data (dict/list) from endpoints so the route's declared "
            "`status_code` is applied — do NOT wrap a normal return in `JSONResponse`/`Response` "
            "(that overrides the status, e.g. silently turning a 201 into a 200). For an empty "
            "204 response return `fastapi.Response(status_code=204)`.\n"
            "9. `APIRouter` has NO `exception_handler` or `errorhandler` — exception handlers "
            "exist only on the app. A Flask blueprint-level `errorhandler` moves to the file "
            "that creates the app (`@app.exception_handler`), or becomes an explicit "
            "try/except returning the same status/body if the app file is not being edited.\n"
            "10. A module that other files import a router from MUST expose the EXACT "
            "module-level name importers use (`bp`, `router`, or another original name). Do "
            "not rename it merely to `router`; every interface decision must be defined.\n"
            "11. NEVER use `@app.on_event(...)` (deprecated; the test runner promotes the "
            "deprecation warning to an error) — use a `lifespan` async context manager "
            "passed to `FastAPI(lifespan=...)` for startup/shutdown work.\n"
            "12. NEVER invent or import packages that don't exist (there is no "
            "`fastapi_flash`, `fastapi_login`, etc.). When a Flask feature has no FastAPI "
            "equivalent in the allowed package set (e.g. `flash()` messages, `session`), "
            "implement a minimal inline equivalent with Starlette's SessionMiddleware or "
            "plain request/response state — behaviour-preserving and self-contained.\n"
            "13. `APIRouter` has no `middleware` decorator or `add_middleware` method. "
            "Middleware belongs to the FastAPI application and must be installed with "
            "`app.add_middleware(...)`.\n"
            "14. Preserve view decorators as decorators. If the original function accepts "
            "a view and returns a local wrapper, keep that shape with `functools.wraps`; "
            "the wrapper reads the planned ambient context and calls/awaits the view. Do "
            "not reinterpret the decorated view argument as a FastAPI Request or dependency.\n"
            "8. Do NOT add try/except around calls to the project's own modules and do NOT "
            "raise `HTTPException`. Let those exceptions propagate to the app's registered "
            "`@app.exception_handler(...)`s, and keep each handler's EXACT status code and JSON "
            "body (e.g. `{\"error\": ...}`, not FastAPI's default `{\"detail\": ...}`)."
        )

    def build_user_prompt(
        self, *, file: PlannedFile, source: str, context: dict[str, str]
    ) -> str:
        checklist = "\n".join(f"  - {s.title}: {s.instruction}" for s in file.subtasks)
        ctx_blocks = "".join(
            f"\n--- context file: {name} ---\n{body}\n" for name, body in context.items()
        )
        action = "Create" if file.action == "create" else "Migrate"
        artifact = ""
        if file.action == "create":
            artifact = (
                f"\nPurpose: {file.purpose}\nFrozen artifact contract: "
                f"{file.artifact_contract}\n"
            )
        return (
            f"{action} this file for the Flask to FastAPI target architecture.\n\n"
            f"File: {file.path}  (role: {file.role})\n\n"
            f"{artifact}"
            f"Transformations to apply:\n{checklist}\n"
            f"{ctx_blocks}\n"
            f"--- file to migrate: {file.path} ---\n{source}\n\n"
            f"Return ONLY the full migrated contents of {file.path} in one ```python block."
        )

    def build_cluster_prompt(
        self, *, files: list[PlannedFile], sources: dict[str, str],
        context: dict[str, str],
    ) -> str:
        """One coordinated prompt for a small Plan-selected framework seam."""
        ctx_blocks = "".join(
            f"\n--- context file: {name} ---\n{body}\n" for name, body in context.items()
        )
        targets: list[str] = []
        for file in files:
            checklist = "\n".join(
                f"  - {s.title}: {s.instruction}" for s in file.subtasks
            )
            targets.append(
                f"\n--- file to migrate: {file.path} (role: {file.role}) ---\n"
                f"Action: {file.action}. Purpose: {file.purpose or 'rewrite in place'}.\n"
                f"Frozen artifact contract: {file.artifact_contract}\n"
                f"Transformations:\n{checklist}\n\n{sources[file.path]}\n"
            )
        paths = [file.path for file in files]
        output = "\n".join(
            f"<<<PORTAGE_FILE:{path}>>>\n```python\n<complete {path}>\n```\n"
            "<<<PORTAGE_END_FILE>>>"
            for path in paths
        )
        return (
            "Migrate this tightly-coupled Flask framework seam to FastAPI as ONE coherent "
            "unit. Resolve configuration, resource lifecycle, app construction, and test "
            "setup once across all files; do not invent compatibility APIs.\n"
            f"Files: {', '.join(paths)}\n"
            f"{ctx_blocks}{''.join(targets)}\n"
            "Return exactly one complete Python block for every requested path using this "
            f"exact marker format (replace placeholders):\n{output}"
        )


recipe = register(FlaskToFastAPIRecipe())
