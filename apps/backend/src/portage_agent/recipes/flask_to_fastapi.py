"""The Flask → FastAPI recipe (v1).

Detects Flask source, classifies each file's transformations, and builds behaviour-preserving
rewrite prompts. The migration deliberately spans the things deterministic tools can't do:
routing decorators, path/query/body parsing, blueprints→routers, error handlers→exception
handlers, the app factory, and the test-client seam.

Framework-agnostic modules (no `flask` import, not a test harness) are left alone — they're
the stable core the routes call, so the migration stays focused on the framework seam.
"""

from __future__ import annotations

import re

from .base import PlannedFile, Subtask, register

# --- marker → subtask detection -------------------------------------------------------
_FLASK_IMPORT = re.compile(r"^\s*(from\s+flask\b|import\s+flask\b)", re.MULTILINE)
_ROUTE = re.compile(r"\.route\s*\(|methods\s*=")
_BLUEPRINT = re.compile(r"\bBlueprint\s*\(")
_REQUEST_PARSE = re.compile(r"request\.(args|get_json|json|form|values|data)\b")
_ERRORHANDLER = re.compile(r"\berrorhandler\s*\(")
_APP_FACTORY = re.compile(r"\bFlask\s*\(|def\s+create_app\b")
_TEST_CLIENT = re.compile(r"\.test_client\s*\(|get_json\s*\(")
_TEMPLATES = re.compile(r"\brender_template\s*\(|\bget_flashed_messages\b")
_SESSION_FLASH = re.compile(r"\bflash\s*\(|\bsession\[|\bsession\.get\b|\bsession\.clear\b")
_G_CONTEXT = re.compile(r"\bg\.[a-zA-Z_]|before_app_request|before_request|\bcurrent_app\b")
_FLASK_LOGIN = re.compile(
    r"\bflask_login\b|\blogin_required\b|\bcurrent_user\b|\blogin_user\b|\blogout_user\b"
)
_FLASK_SQLALCHEMY = re.compile(r"\bflask_sqlalchemy\b|\bSQLAlchemy\s*\(")


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
        "`starlette.datastructures.State` itself (it has no update/get). Keep "
        "`app.instance_path` semantics with plain `os.path` code.",
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
        "Replace `flask.Blueprint(...)` with `fastapi.APIRouter()`. Keep the module-level "
        "variable name the importer expects (e.g. expose `router`).",
    ),
    "route_to_endpoint": Subtask(
        "route_to_endpoint",
        "Routes → typed endpoints",
        "Convert each `@bp.route('/p', methods=[M])` to the matching `@router.<method>('/p')`. "
        "Turn Flask path converters like `<int:item_id>` into FastAPI path params "
        "`/{item_id}` with a typed arg `item_id: int`. Preserve EVERY status code "
        "(e.g. 201 via `status_code=201`; a 204 by returning a 204 `Response`).",
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
        "fixture or setUp).",
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
        "to the app. Flask's `session[...]` maps to `request.session[...]`. Implement "
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
        "redirects (302) to the login page exactly like flask_login did.",
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
        "(`SQLALCHEMY_DATABASE_URI`), resolved at create_app/init time.",
    ),
    "request_context": Subtask(
        "request_context",
        "g / before_request → dependencies",
        "Replace the `g` object and `before_app_request`/`before_request` hooks with "
        "explicit per-request wiring: a dependency (or helper called at the top of each "
        "endpoint) that computes what the hook stored on `g` (e.g. `g.user` from the "
        "session, `g.db` connection) and passes it to the endpoint and into the template "
        "context under the SAME attribute names the templates use. `current_app.config` "
        "moves to module-level config or the app instance. Per-request resources with "
        "teardown (`g.db` + `teardown_appcontext(close_db)`) become ONE yield dependency: "
        "`def get_db(): db = connect(); try: yield db; finally: db.close()` — do NOT "
        "register teardown as middleware (wrong signature) and do NOT use `app.state` as "
        "a context manager (it isn't one); `app.state` holds only config/constants.",
    ),
}


def _is_test_file(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return (
        base == "conftest.py"
        or base.startswith("test_")
        or base.endswith("_test.py")
        or base == "tests.py"
        or "/tests/" in f"/{path}"
    )


class FlaskToFastAPIRecipe:
    name = "flask_to_fastapi"
    source_framework = "flask"
    target_framework = "fastapi"
    # Exactly what the network-off sandbox image ships (see sandbox/Dockerfile.sandbox).
    sandbox_packages = ["fastapi", "starlette", "uvicorn", "httpx", "pydantic", "pytest"]

    def matches(self, files: dict[str, str]) -> bool:
        return any(_FLASK_IMPORT.search(src) for src in files.values())

    def _classify(self, path: str, src: str) -> PlannedFile | None:
        subtasks: list[Subtask] = []
        role = ""
        order = 100

        is_flask = bool(_FLASK_IMPORT.search(src))

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

    def system_prompt(self) -> str:
        return (
            "You are Portage, an expert code-migration agent. You migrate ONE Python source "
            "file from the Flask web framework to FastAPI, preserving behaviour exactly.\n\n"
            "Hard rules:\n"
            "1. Output ONLY the complete migrated file inside a single ```python fenced block. "
            "No prose before or after.\n"
            "2. Preserve all public names other modules rely on (module path, the "
            "`create_app` factory, the `router` variable, function names imported elsewhere).\n"
            "3. Preserve exact HTTP behaviour: same paths, methods, status codes (incl. 201/204), "
            "and identical response JSON shapes.\n"
            "4. Keep importing the project's own modules unchanged (e.g. `from . import store`); "
            "never reimplement or modify framework-agnostic logic.\n"
            "5. The test suite runs OFFLINE (no network). Import ONLY the Python standard "
            "library, this project's own modules, and these packages: "
            "fastapi, starlette, uvicorn, httpx, pydantic, pytest, jinja2, itsdangerous, "
            "sqlalchemy, python-multipart (needed for fastapi `Form(...)`; imported "
            "implicitly).\n"
            "6. Keep `from __future__ import annotations` if the original had it.\n"
            "7. Return plain Python data (dict/list) from endpoints so the route's declared "
            "`status_code` is applied — do NOT wrap a normal return in `JSONResponse`/`Response` "
            "(that overrides the status, e.g. silently turning a 201 into a 200). For an empty "
            "204 response return `fastapi.Response(status_code=204)`.\n"
            "9. `APIRouter` has NO `exception_handler` or `errorhandler` — exception handlers "
            "exist only on the app. A Flask blueprint-level `errorhandler` moves to the file "
            "that creates the app (`@app.exception_handler`), or becomes an explicit "
            "try/except returning the same status/body if the app file is not being edited.\n"
            "10. A module that other files import a router from MUST expose it as a "
            "module-level name `router` (e.g. `router = APIRouter()`), and every name the "
            "context files import from this module must still be defined.\n"
            "11. NEVER use `@app.on_event(...)` (deprecated; the test runner promotes the "
            "deprecation warning to an error) — use a `lifespan` async context manager "
            "passed to `FastAPI(lifespan=...)` for startup/shutdown work.\n"
            "12. NEVER invent or import packages that don't exist (there is no "
            "`fastapi_flash`, `fastapi_login`, etc.). When a Flask feature has no FastAPI "
            "equivalent in the allowed package set (e.g. `flash()` messages, `session`), "
            "implement a minimal inline equivalent with Starlette's SessionMiddleware or "
            "plain request/response state — behaviour-preserving and self-contained.\n"
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
        return (
            f"Migrate this file from Flask to FastAPI.\n\n"
            f"File: {file.path}  (role: {file.role})\n\n"
            f"Transformations to apply:\n{checklist}\n"
            f"{ctx_blocks}\n"
            f"--- file to migrate: {file.path} ---\n{source}\n\n"
            f"Return ONLY the full migrated contents of {file.path} in one ```python block."
        )


recipe = register(FlaskToFastAPIRecipe())
