"""Source-derived Flask contracts used by the FastAPI migration recipe."""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from portage_agent.agent.nodes.common import (
    _module_names,
    _resolve_module,
    imported_bindings_from_sources,
)

from .base import PlannedFile, Subtask

_FLASK_IMPORT = re.compile(r"^\s*(from\s+flask\b|import\s+flask\b)", re.MULTILINE)

_FLASK_FAMILY_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+"
    r"(?:flask(?:\b|_)|flask_login\b|flask_sqlalchemy\b|flask_restx\b)",
    re.MULTILINE,
)

_ROUTE = re.compile(r"\.route\s*\(|methods\s*=")

_BLUEPRINT = re.compile(r"\bBlueprint\s*\(")

_REQUEST_PARSE = re.compile(r"request\.(args|get_json|json|form|values|data)\b")

_ERRORHANDLER = re.compile(r"\b(?:app_)?errorhandler\s*\(")

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

_DIRECT_TEST_MEMBERS = ("test_client", "test_cli_runner", "testing")

_DIRECT_TEST_CONTEXT_GLOBALS = frozenset({"g", "session", "current_app", "request"})

_TEMPLATE_FUNCTIONS = frozenset({"render_template", "url_for"})

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

_FLASK_LOGIN_NAMES = {"login_user", "logout_user", "current_user", "login_required"}

_RESOURCE_FUNCTION = re.compile(
    r"^(get|open|connect)_(db|database|session|connection)$"
)

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
        "A helper that may raise `HTTPException` must be a normal `def`; Python forbids "
        "`raise` inside lambda expressions. "
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


def _parsed(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _exception_handler_contracts(source: str) -> list[dict]:
    """Source-owned exception envelopes that are simple enough to freeze mechanically."""
    tree = _parsed(source)
    if tree is None:
        return []
    contracts = []
    for function in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        decorator = next((
            item for item in function.decorator_list
            if isinstance(item, ast.Call) and item.args
            and ast.unparse(item.func).split(".")[-1] == "errorhandler"
        ), None)
        if decorator is None:
            continue
        returned = next((
            node.value for node in ast.walk(function)
            if isinstance(node, ast.Return) and node.value is not None
        ), None)
        if not (
            isinstance(returned, ast.Tuple) and len(returned.elts) == 2
            and isinstance(returned.elts[1], ast.Constant)
            and isinstance(returned.elts[1].value, int)
            and isinstance(returned.elts[0], ast.Call)
            and ast.unparse(returned.elts[0].func).split(".")[-1] == "jsonify"
        ):
            continue
        jsonify_call = returned.elts[0]
        payload = jsonify_call.args[0] if len(jsonify_call.args) == 1 else None
        keys = (
            sorted(
                key.value for key in payload.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            )
            if isinstance(payload, ast.Dict)
            else sorted(keyword.arg for keyword in jsonify_call.keywords if keyword.arg)
        )
        if not keys:
            continue
        exception = ast.unparse(decorator.args[0])
        contracts.append({
            "exception": exception,
            "exception_name": exception.split(".")[-1],
            "function": function.name,
            "status_code": returned.elts[1].value,
            "json_keys": keys,
        })
    return contracts


def _route_functions_without_local_handlers(
    source: str, handled_exceptions: set[str],
) -> list[str]:
    tree = _parsed(source)
    if tree is None:
        return []
    functions = []
    for function in (
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).split(".")[-1] == "route"
            for decorator in node.decorator_list
        )
    ):
        caught = {
            ast.unparse(handler.type).split(".")[-1]
            for handler in ast.walk(function)
            if isinstance(handler, ast.ExceptHandler) and handler.type is not None
        }
        if not caught & handled_exceptions:
            functions.append(function.name)
    return functions


def _direct_json_return_functions(source: str) -> list[str]:
    tree = _parsed(source)
    if tree is None:
        return []
    return sorted({
        function.name
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr in {"get_json", "json"}
            for node in ast.walk(function)
        )
    })


def _blueprint_symbols(source: str) -> set[str]:
    tree = _parsed(source)
    if tree is None:
        return set()
    return {
        target.id
        for statement in tree.body if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and isinstance(statement.value, ast.Call)
        and ast.unparse(statement.value.func).split(".")[-1] == "Blueprint"
        for target in (
            statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        )
        if isinstance(target, ast.Name)
    }


def _registration_ref(tree: ast.Module, path: str, value: ast.AST) -> dict | None:
    if isinstance(value, ast.Constant) and isinstance(value.value, int):
        return {"kind": "status", "value": value.value}
    expression = ast.unparse(value)
    if isinstance(value, ast.Name):
        for statement in tree.body:
            if not isinstance(statement, ast.ImportFrom):
                continue
            for alias in statement.names:
                if (alias.asname or alias.name) == value.id:
                    return {
                        "kind": "exception",
                        "module": _resolve_module(statement.module, statement.level, path),
                        "symbol": alias.name,
                        "source": expression,
                    }
        if any(isinstance(node, ast.ClassDef) and node.name == value.id for node in tree.body):
            return {
                "kind": "exception",
                "module": path.removesuffix(".py").replace("/", "."),
                "symbol": value.id,
                "source": expression,
            }
    if isinstance(value, ast.Attribute):
        root = expression.split(".")[0]
        imported = next((
            alias.name for statement in tree.body if isinstance(statement, ast.Import)
            for alias in statement.names if (alias.asname or alias.name) == root
        ), None)
        if imported:
            suffix = expression.split(".")[1:]
            return {
                "kind": "exception",
                "module": ".".join([imported, *suffix[:-1]]),
                "symbol": suffix[-1],
                "source": expression,
            }
    if isinstance(value, ast.Name) and value.id in {"Exception", "RuntimeError", "ValueError"}:
        return {
            "kind": "builtin", "symbol": value.id, "source": expression,
        }
    return None


def _handler_return_facts(function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    helpers: set[str] = set()
    status_codes: set[int] = set()
    literals: set[str] = set()
    for returned in (
        node.value for node in ast.walk(function)
        if isinstance(node, ast.Return) and node.value is not None
    ):
        response = (
            returned.elts[0]
            if isinstance(returned, ast.Tuple) and returned.elts else returned
        )
        if isinstance(response, ast.Call):
            helpers.add(ast.unparse(response.func).split(".")[-1])
        status_codes.update(
            node.value for node in ast.walk(returned)
            if isinstance(node, ast.Constant) and isinstance(node.value, int)
            and not isinstance(node.value, bool)
        )
        literals.update(
            node.value for node in ast.walk(returned)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    return {
        "response_helpers": sorted(helpers),
        "response_literals": sorted(literals),
        "status_codes": sorted(status_codes),
    }


def _payload_helper_contract(tree: ast.Module, name: str) -> dict | None:
    function = next((
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ), None)
    if function is None:
        return None
    keys = {
        key.value
        for node in ast.walk(function)
        for key in (
            node.keys if isinstance(node, ast.Dict)
            else [node.slice] if isinstance(node, ast.Subscript) else []
        )
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    parameters = {
        argument.arg for argument in [
            *function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs,
        ]
    }
    returned_status_parameters = sorted({
        node.elts[1].id
        for node in ast.walk(function)
        if isinstance(node, ast.Tuple) and len(node.elts) >= 2
        and isinstance(node.elts[1], ast.Name) and node.elts[1].id in parameters
    })
    if not keys and not returned_status_parameters:
        return None
    return {
        "function": name,
        "json_keys": sorted(keys),
        "string_literals": sorted({
            node.value for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }),
        "returned_status_parameters": returned_status_parameters,
    }


def _blueprint_error_handler_facts(files: dict[str, str]) -> list[dict]:
    """Binding-aware handlers owned by Flask Blueprint objects, including split modules."""
    facts = []
    for provider, source in files.items():
        for symbol in sorted(_blueprint_symbols(source)):
            bindings = [(provider, symbol)]
            bindings.extend(
                (
                    binding.importer,
                    binding.local if binding.symbol == symbol
                    else f"{binding.local}.{symbol}" if binding.symbol is None else "",
                )
                for binding in imported_bindings_from_sources(files, provider)
            )
            for path, receiver in bindings:
                if not receiver or (tree := _parsed(files.get(path, ""))) is None:
                    continue
                for function in (
                    node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                ):
                    decorator = next((
                        item for item in function.decorator_list
                        if isinstance(item, ast.Call) and item.args
                        and isinstance(item.func, ast.Attribute)
                        and item.func.attr in {"errorhandler", "app_errorhandler"}
                        and ast.unparse(item.func.value) == receiver
                    ), None)
                    if decorator is None:
                        continue
                    registration = _registration_ref(tree, path, decorator.args[0])
                    if registration is None:
                        continue
                    returns = _handler_return_facts(function)
                    helpers = [
                        contract for name in returns["response_helpers"]
                        if (contract := _payload_helper_contract(tree, name)) is not None
                    ]
                    parameters = [
                        argument.arg
                        for argument in [*function.args.posonlyargs, *function.args.args]
                    ]
                    facts.append({
                        "handler_path": path,
                        "blueprint_provider": provider,
                        "blueprint_symbol": symbol,
                        "receiver": receiver,
                        "scope": decorator.func.attr,
                        "function": function.name,
                        "error_parameter": parameters[0] if parameters else "exc",
                        "registration": registration,
                        **returns,
                        "payload_helpers": helpers,
                    })
    return sorted(facts, key=lambda item: (item["handler_path"], item["function"]))


def _blueprint_factories(
    files: dict[str, str], provider: str, symbol: str, factory_paths: set[str],
) -> list[str]:
    factories = set()
    for binding in imported_bindings_from_sources(files, provider):
        if binding.importer not in factory_paths:
            continue
        receiver = binding.local if binding.symbol == symbol else (
            f"{binding.local}.{symbol}" if binding.symbol is None else ""
        )
        tree = _parsed(files.get(binding.importer, ""))
        if receiver and tree is not None and any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_blueprint" and node.args
            and ast.unparse(node.args[0]) == receiver
            for node in ast.walk(tree)
        ):
            factories.add(binding.importer)
    return sorted(factories)


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
        return {
            "keys": [], "override_parameters": [], "optional_parameters": [],
            "from_objects": [],
        }
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
        "from_objects": sorted({
            ast.unparse(node.args[0])
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "from_object"
            and node.args
        }),
    }


def _factory_database_config(files: dict[str, str], path: str) -> dict:
    """Resolve a simple config.from_object(mapping[factory_default]) DB URI source."""
    tree = _parsed(files.get(path, ""))
    factory = next((
        node for node in (tree.body if tree else [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "create_app"
    ), None)
    if factory is None:
        return {}
    positional = [*factory.args.posonlyargs, *factory.args.args]
    defaults = {
        argument.arg: default.value
        for argument, default in zip(
            positional[-len(factory.args.defaults):], factory.args.defaults, strict=True,
        )
        if isinstance(default, ast.Constant) and isinstance(default.value, str)
    } if factory.args.defaults else {}
    configured = next((
        node.args[0] for node in ast.walk(factory)
        if isinstance(node, ast.Call) and node.args
        and isinstance(node.func, ast.Attribute) and node.func.attr == "from_object"
        and isinstance(node.args[0], ast.Subscript)
        and isinstance(node.args[0].value, ast.Name)
        and isinstance(node.args[0].slice, ast.Name)
        and node.args[0].slice.id in defaults
    ), None)
    if configured is None:
        return {}
    root = configured.value.id
    binding = next((
        (statement, alias)
        for statement in tree.body if isinstance(statement, ast.ImportFrom)
        for alias in statement.names if (alias.asname or alias.name) == root
    ), None)
    if binding is None:
        return {}
    statement, alias = binding
    module = _resolve_module(statement.module, statement.level, path)
    config_source = next((
        source for candidate, source in files.items()
        if module in _module_names(candidate)
    ), "")
    if "SQLALCHEMY_DATABASE_URI" not in config_source:
        return {}
    return {
        "module": module, "symbol": alias.name,
        "default_key": defaults[configured.slice.id],
        "sqlite": "sqlite:" in config_source,
    }


def _factory_local_imports(source: str, path: str) -> list[dict]:
    tree = _parsed(source)
    factory = next((
        node for node in (tree.body if tree else [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "create_app"
    ), None)
    return [
        {
            "module": _resolve_module(statement.module, statement.level, path),
            "source_module": statement.module,
            "level": statement.level,
            "symbol": alias.name,
            "asname": alias.asname,
        }
        for statement in (factory.body if factory else [])
        if isinstance(statement, ast.ImportFrom)
        for alias in statement.names
    ]


def _sqlalchemy_provider_contracts(
    files: dict[str, str], planned: list[PlannedFile],
) -> list[dict]:
    """Freeze the exercised surface and import direction of SQLAlchemy providers."""
    roles = {item.path: item.role for item in planned}
    contracts: list[dict] = []
    for provider, source in files.items():
        tree = _parsed(source)
        if tree is None:
            continue
        constructors = tuple({
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "flask_sqlalchemy"
            for alias in node.names if alias.name == "SQLAlchemy"
        })
        modules = tuple({
            alias.asname or alias.name
            for node in tree.body if isinstance(node, ast.Import)
            for alias in node.names if alias.name == "flask_sqlalchemy"
        })

        def constructs_sqlalchemy(
            value: ast.AST | None,
            _constructors: tuple[str, ...] = constructors,
            _modules: tuple[str, ...] = modules,
        ) -> bool:
            return bool(
                isinstance(value, ast.Call) and (
                    isinstance(value.func, ast.Name) and value.func.id in _constructors
                    or isinstance(value.func, ast.Attribute)
                    and value.func.attr == "SQLAlchemy"
                    and ast.unparse(value.func.value) in _modules
                )
            )

        symbols = {
            target.id
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and constructs_sqlalchemy(statement.value)
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }
        for symbol in sorted(symbols):
            consumers: set[str] = set()
            model_roots: dict[str, set[str]] = {provider: {symbol}}
            members = _attribute_names_for_root(source, symbol)
            for binding in imported_bindings_from_sources(files, provider):
                if binding.symbol == symbol:
                    root = binding.local
                elif binding.symbol is None:
                    root = f"{binding.local}.{symbol}"
                else:
                    continue
                used = _attribute_names_for_root(
                    files.get(binding.importer, ""), root,
                )
                if used or binding.symbol == symbol:
                    consumers.add(binding.importer)
                    model_roots.setdefault(binding.importer, set()).add(root)
                    members.update(used)
            factories = sorted({
                path for path in {provider, *consumers}
                if roles.get(path) == "app_factory"
            })
            database_configs = [
                fact for path in factories
                if (fact := _factory_database_config(files, path))
            ]
            implicit_tables = {
                path: tables
                for path in sorted({provider, *consumers})
                if (tables := _implicit_sqlalchemy_tables(files.get(path, ""), symbol))
            }
            model_names = {
                node.name
                for model_path, roots in model_roots.items()
                if (model_tree := _parsed(files.get(model_path, ""))) is not None
                for node in model_tree.body if isinstance(node, ast.ClassDef)
                if any(
                    ast.unparse(base) in {f"{root}.Model" for root in roots}
                    for base in node.bases
                )
            }
            query_models = sorted({
                node.value.id
                for candidate in files.values()
                if (candidate_tree := _parsed(candidate)) is not None
                for node in ast.walk(candidate_tree)
                if isinstance(node, ast.Attribute) and node.attr == "query"
                and isinstance(node.value, ast.Name) and node.value.id in model_names
            })
            lazy_consumers = sorted({
                consumer
                for consumer in consumers
                for function in tree.body
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                for statement in ast.walk(function)
                if isinstance(statement, ast.ImportFrom)
                and _resolve_module(statement.module, statement.level, provider)
                in _module_names(consumer)
            })
            contracts.append({
                "provider": provider,
                "symbol": symbol,
                "consumers": sorted(consumers),
                "members": sorted(members),
                "factory_files": factories,
                "database_config": database_configs[0] if len(database_configs) == 1 else {},
                "implicit_tables": implicit_tables,
                "lazy_consumers": lazy_consumers,
                "query_models": query_models,
            })
    return contracts


def _implicit_sqlalchemy_tables(source: str, provider: str) -> dict[str, str]:
    """Freeze table names Flask-SQLAlchemy would infer for db.Model subclasses."""
    tree = _parsed(source)
    if tree is None:
        return {}

    def table_name(class_name: str) -> str:
        split_acronyms = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", class_name)
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", split_acronyms).lower()

    return {
        node.name: table_name(node.name)
        for node in tree.body if isinstance(node, ast.ClassDef)
        and any(
            isinstance(base, ast.Attribute)
            and isinstance(base.value, ast.Name)
            and base.value.id == provider and base.attr == "Model"
            for base in node.bases
        )
        and not any(
            isinstance(statement, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id in {"__table__", "__tablename__"}
                for target in (
                    statement.targets if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
            for statement in node.body
        )
    }


def _attribute_names_for_root(source: str, root: str) -> set[str]:
    tree = _parsed(source)
    if tree is None:
        return set()
    return {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and ast.unparse(node.value) == root
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


def _click_registrar_contracts(files: dict[str, str]) -> list[dict]:
    """Top-level functions that register nested Click commands on an app-like object."""
    contracts = []
    for path, source in files.items():
        tree = _parsed(source)
        if tree is None:
            continue
        for function in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.args.args
        ):
            receiver = function.args.args[0].arg
            if any(
                isinstance(node, ast.Attribute)
                and node.attr in {"command", "add_command"}
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "cli"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == receiver
                for node in ast.walk(function)
            ):
                contracts.append({
                    "module": path,
                    "function": function.name,
                    "receiver": receiver,
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
    blueprints = {}
    for statement in tree.body:
        if not (
            isinstance(statement, (ast.Assign, ast.AnnAssign))
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
        ):
            continue
        url_prefix = next((
            keyword.value.value for keyword in statement.value.keywords
            if keyword.arg == "url_prefix"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ), "")
        for target in (
            statement.targets if isinstance(statement, ast.Assign)
            else [statement.target]
        ):
            if isinstance(target, ast.Name):
                blueprints[target.id] = {
                    "name": statement.value.args[0].value,
                    "prefix": url_prefix,
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
            blueprint = blueprints.get(receiver, {})
            contracts.append({
                "function": function.name,
                "receiver": receiver,
                "path": re.sub(
                    r"<(?:[^:>]+:)?([^>]+)>", r"{\1}",
                    decorator.args[0].value,
                ) if (
                    decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                ) else "",
                "name": (
                    f"{blueprint['name']}.{endpoint}"
                    if blueprint else endpoint
                ),
                "prefix": blueprint.get("prefix", ""),
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


def _instance_export_contracts(
    manifest: dict[str, dict], path: str,
) -> list[dict]:
    """Freeze source variables constructed as instances and used as decorators."""
    contracts = []
    for pin in manifest.values():
        if pin.get("module") != path or pin.get("target_kind") != "variable":
            continue
        original = _parsed(pin.get("original", ""))
        if not (
            original and len(original.body) == 1
            and isinstance(original.body[0], (ast.Assign, ast.AnnAssign))
            and isinstance(original.body[0].value, ast.Call)
        ):
            continue
        symbol = pin.get("symbol", "")
        pattern = re.compile(rf"^@{re.escape(symbol)}\.([A-Za-z_]\w*)")
        contracts.append({
            "symbol": symbol,
            "decorator_members": sorted({
                match.group(1)
                for call_site in pin.get("call_sites", [])
                if (match := pattern.match(call_site))
            }),
        })
    return sorted(contracts, key=lambda item: item["symbol"])


def _decorated_provider_protocols(files: dict[str, str]) -> list[dict]:
    """Source instances used only through direct ``@provider.member`` decorators.

    Parameterized decorator registries (routes, exception handlers, and similar
    framework-owned surfaces) are deliberately excluded: those are migrated into target
    framework registrations rather than preserved as provider methods.
    """
    protocols = []
    for provider, source in sorted(files.items()):
        tree = _parsed(source)
        if tree is None:
            continue
        symbols = {
            target.id
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and isinstance(statement.value, ast.Call)
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }
        if not symbols:
            continue
        bindings = imported_bindings_from_sources(files, provider)
        for symbol in sorted(symbols):
            roots: dict[str, set[str]] = {provider: {symbol}}
            for binding in bindings:
                if binding.symbol == symbol:
                    roots.setdefault(binding.importer, set()).add(binding.local)
                elif binding.symbol is None:
                    roots.setdefault(binding.importer, set()).add(
                        f"{binding.local}.{symbol}"
                    )
            members: set[str] = set()
            callable_members: set[str] = set()
            attribute_members: set[str] = set()
            attribute_values: dict[str, str | int | float | bool | None] = {}
            callbacks: list[dict[str, str]] = []
            consumers: set[str] = set()
            has_parameterized_decorator = False
            for consumer, local_roots in roots.items():
                consumer_tree = _parsed(files.get(consumer, ""))
                if consumer_tree is None:
                    continue
                for node in ast.walk(consumer_tree):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and ast.unparse(node.func.value) in local_roots
                    ):
                        callable_members.add(node.func.attr)
                        consumers.add(consumer)
                    if isinstance(node, (ast.Assign, ast.AnnAssign)):
                        targets = (
                            node.targets if isinstance(node, ast.Assign)
                            else [node.target]
                        )
                        for target in targets:
                            if (
                                isinstance(target, ast.Attribute)
                                and ast.unparse(target.value) in local_roots
                            ):
                                attribute_members.add(target.attr)
                                consumers.add(consumer)
                                try:
                                    value = ast.literal_eval(node.value)
                                except (TypeError, ValueError):
                                    continue
                                if isinstance(value, (str, int, float, bool, type(None))):
                                    attribute_values[target.attr] = value
                for function in (
                    node for node in ast.walk(consumer_tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                ):
                    for decorator in function.decorator_list:
                        candidate = decorator.func if isinstance(decorator, ast.Call) else decorator
                        if not (
                            isinstance(candidate, ast.Attribute)
                            and ast.unparse(candidate.value) in local_roots
                        ):
                            continue
                        if isinstance(decorator, ast.Call):
                            has_parameterized_decorator = True
                        else:
                            members.add(candidate.attr)
                            consumers.add(consumer)
                            flask_names = {
                                alias.asname or alias.name
                                for statement in consumer_tree.body
                                if isinstance(statement, ast.ImportFrom)
                                and (statement.module or "").startswith("flask")
                                for alias in statement.names
                            }
                            if consumer == provider and not any(
                                isinstance(node, ast.Name)
                                and isinstance(node.ctx, ast.Load)
                                and node.id in flask_names
                                for statement in function.body
                                for node in ast.walk(statement)
                            ):
                                callbacks.append({
                                    "member": candidate.attr,
                                    "function": function.name,
                                    "source": ast.unparse(function),
                                })
            if members and not has_parameterized_decorator:
                protocols.append({
                    "provider": provider,
                    "symbol": symbol,
                    "decorator_members": sorted(members),
                    "callable_members": sorted(callable_members),
                    "attribute_members": sorted(attribute_members),
                    "attribute_values": dict(sorted(attribute_values.items())),
                    "callbacks": sorted(callbacks, key=lambda item: item["function"]),
                    "consumers": sorted(consumers - {provider}),
                })
    return protocols


def _factory_bindings_by_consumer(
    files: dict[str, str], factory_paths: list[str],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    factory_bindings: dict[str, set[str]] = {}
    module_bindings: dict[str, set[str]] = {}
    for factory_path in factory_paths:
        for binding in imported_bindings_from_sources(files, factory_path):
            if binding.symbol:
                factory_bindings.setdefault(binding.importer, set()).add(binding.local)
            else:
                module_bindings.setdefault(binding.importer, set()).add(binding.local)
    return factory_bindings, module_bindings


def _factory_app_receivers(
    tree: ast.Module, direct_factories: set[str], factory_modules: set[str],
) -> set[str]:
    receivers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(
            node.value, ast.Call,
        ):
            continue
        called = ast.unparse(node.value.func)
        if called not in direct_factories and not any(
            called.startswith(f"{module}.") for module in factory_modules
        ):
            continue
        receivers.update(
            ast.unparse(target)
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
    return receivers


def _direct_factory_context_members(
    files: dict[str, str], factory_paths: list[str],
) -> set[str]:
    """App members whose return values tests enter directly with ``with``."""
    direct, modules = _factory_bindings_by_consumer(files, factory_paths)
    members: set[str] = set()
    for path, source in files.items():
        if not _is_test_file(path) or (tree := _parsed(source)) is None:
            continue
        receivers = _factory_app_receivers(
            tree, direct.get(path, set()), modules.get(path, set()),
        )
        receivers.update(
            ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in _DIRECT_TEST_MEMBERS
        )
        members.update(
            item.context_expr.func.attr
            for node in ast.walk(tree)
            if isinstance(node, (ast.With, ast.AsyncWith))
            for item in node.items
            if isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and ast.unparse(item.context_expr.func.value) in receivers
        )
    return members


def _returned_lifecycle_contracts(
    files: dict[str, str], factory_paths: list[str],
) -> list[dict]:
    """Find test-owned ``value = app.member(); value.enter()/exit()`` protocols.

    The receiver must come from an imported application factory. This small binding check
    prevents ordinary returned objects in tests from becoming application-surface members.
    The first two distinct no-argument calls define entry then exit; richer protocols stay
    model-owned until evidence justifies widening the deterministic rule.
    """
    factory_bindings, module_bindings = _factory_bindings_by_consumer(
        files, factory_paths,
    )

    contracts = []
    for path, source in sorted(files.items()):
        if not _is_test_file(path):
            continue
        tree = _parsed(source)
        if tree is None:
            continue
        direct_factories = factory_bindings.get(path, set())
        factory_modules = module_bindings.get(path, set())
        app_receivers = _factory_app_receivers(
            tree, direct_factories, factory_modules,
        )

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not (
                isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and ast.unparse(node.value.func.value) in app_receivers
            ):
                continue
            targets = [
                ast.unparse(target)
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            ]
            calls = sorted(
                (
                    candidate.lineno,
                    candidate.func.attr,
                )
                for candidate in ast.walk(tree)
                if isinstance(candidate, ast.Call)
                and not candidate.args and not candidate.keywords
                and isinstance(candidate.func, ast.Attribute)
                and ast.unparse(candidate.func.value) in targets
            )
            ordered_members = list(dict.fromkeys(member for _, member in calls))
            if len(ordered_members) != 2:
                continue
            contracts.append({
                "consumer": path,
                "factory_member": node.value.func.attr,
                "entry_member": ordered_members[0],
                "exit_member": ordered_members[1],
            })
    return contracts


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


def _exercised_flask_template_functions(
    files: dict[str, str], paths: set[str],
) -> set[str]:
    """Return Flask template functions that source consumers actually call."""
    found: set[str] = set()
    for path in paths:
        tree = _parsed(files.get(path, ""))
        if tree is None:
            continue
        direct = {
            alias.asname or alias.name: alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "flask"
            for alias in node.names if alias.name in _TEMPLATE_FUNCTIONS
        }
        flask_modules = {
            alias.asname or alias.name
            for node in tree.body if isinstance(node, ast.Import)
            for alias in node.names if alias.name == "flask"
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in direct:
                    found.add(direct[node.func.id])
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _TEMPLATE_FUNCTIONS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in flask_modules
            ):
                found.add(node.func.attr)
    return found


def _flask_login_consumer_contracts(files: dict[str, str]) -> dict[str, list[dict]]:
    """Freeze the Flask-Login names and call shapes each source consumer uses."""
    contracts: dict[str, list[dict]] = {}
    for path, source in files.items():
        tree = _parsed(source)
        if tree is None:
            continue
        bound = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        } | {
            target.id
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            for target in (
                statement.targets if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }
        references: list[tuple[str, str, str]] = []
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module == "flask_login":
                references.extend(
                    (alias.name, alias.asname or alias.name, alias.asname or alias.name)
                    for alias in statement.names if alias.name in _FLASK_LOGIN_NAMES
                )
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.name != "flask_login":
                        continue
                    module_name = alias.asname or alias.name
                    used = {
                        node.attr for node in ast.walk(tree)
                        if isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == module_name
                        and node.attr in _FLASK_LOGIN_NAMES
                    }
                    references.extend(
                        (
                            name,
                            name if name not in bound else f"_portage_{name}",
                            f"{module_name}.{name}",
                        )
                        for name in sorted(used)
                    )
        entries = []
        for symbol, local, source_ref in references:
            calls = sorted({
                (
                    len(node.args),
                    tuple(sorted(keyword.arg for keyword in node.keywords if keyword.arg)),
                )
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and ast.unparse(node.func) == source_ref
                and not any(isinstance(argument, ast.Starred) for argument in node.args)
                and all(keyword.arg is not None for keyword in node.keywords)
            })
            entries.append({
                "symbol": symbol,
                "local": local,
                "source_ref": source_ref,
                "call_shapes": [
                    {"positional": positional, "keywords": list(keywords)}
                    for positional, keywords in calls
                ],
            })
        if entries:
            contracts[path] = sorted(entries, key=lambda item: item["symbol"])
    return contracts


def _template_framework_globals(files: dict[str, str]) -> list[str]:
    templates = "\n".join(
        source for path, source in files.items()
        if PurePosixPath(path).suffix in {".html", ".htm", ".jinja", ".jinja2"}
    )
    has_login_manager = any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and (
            isinstance(node, ast.ImportFrom) and node.module == "flask_login"
            or isinstance(node, ast.Import)
            and any(alias.name == "flask_login" for alias in node.names)
        )
        for source in files.values() if (tree := _parsed(source)) is not None
        for node in tree.body
    )
    return [
        "current_user"
        for name in ["current_user"]
        if has_login_manager and re.search(rf"\b{re.escape(name)}\b", templates)
    ]


def _template_context_processor_contracts(files: dict[str, str]) -> list[dict]:
    """Extract Flask context processors without assigning meaning to their keys."""
    contracts = []
    for path, source in sorted(files.items()):
        tree = _parsed(source)
        if tree is None:
            continue
        flask_names = {
            alias.asname or alias.name
            for statement in tree.body
            if isinstance(statement, ast.ImportFrom)
            and (statement.module or "").startswith("flask")
            for alias in statement.names
        }
        for factory in (
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for function in (
                node for node in factory.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                decorator = next((
                    item for item in function.decorator_list
                    if isinstance(item, ast.Attribute)
                    and item.attr == "context_processor"
                ), None)
                if decorator is None:
                    continue
                keys = set()
                for returned in (
                    node.value for node in ast.walk(function)
                    if isinstance(node, ast.Return) and node.value is not None
                ):
                    if isinstance(returned, ast.Dict):
                        keys.update(
                            key.value for key in returned.keys
                            if isinstance(key, ast.Constant)
                            and isinstance(key.value, str)
                        )
                    elif (
                        isinstance(returned, ast.Call)
                        and isinstance(returned.func, ast.Name)
                        and returned.func.id == "dict"
                    ):
                        keys.update(
                            keyword.arg for keyword in returned.keywords
                            if keyword.arg is not None
                        )
                neutral = not any(
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in flask_names
                    for statement in function.body for node in ast.walk(statement)
                )
                contracts.append({
                    "provider": path,
                    "factory": factory.name,
                    "receiver": ast.unparse(decorator.value),
                    "function": function.name,
                    "keys": sorted(keys),
                    "source": ast.unparse(function) if neutral else "",
                })
    return contracts


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
    surfaces["template_rendering"] = sorted({
        *surfaces.get("template_rendering", []),
        *(
            path for path in files
            if _exercised_flask_template_functions(files, {path})
        ),
    })
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
    if "template_rendering" in requirements:
        consumers = set(requirements["template_rendering"]["consumers"])
        functions = {
            "render_template",
            *_exercised_flask_template_functions(files, consumers),
        }
        requirements["template_rendering"]["required_exports"] = sorted(functions)
        requirements["template_rendering"]["required_export_kinds"] = {
            name: "function" for name in sorted(functions)
        }
    if "authentication" in requirements:
        bindings = _flask_login_consumer_contracts(files)
        symbols = sorted({
            item["symbol"]
            for path, entries in bindings.items() if path in targets
            for item in entries
        })
        requirements["authentication"]["required_exports"] = symbols
        requirements["authentication"]["required_export_kinds"] = {
            name: "variable" if name == "current_user" else "function"
            for name in symbols
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
    } | _direct_factory_context_members(
        files,
        [item.path for item in planned if item.role == "app_factory"],
    ) | {
        contract["factory_member"]
        for contract in _returned_lifecycle_contracts(
            files,
            [item.path for item in planned if item.role == "app_factory"],
        )
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
