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


_SUBTASKS: dict[str, Subtask] = {
    "app_factory": Subtask(
        "app_factory",
        "Migrate the application factory",
        "Replace the Flask app factory with a FastAPI one: `create_app()` must build a "
        "`FastAPI()` instance and `include_router(...)` the migrated router, keeping the "
        "function name `create_app` and its factory shape (other modules import it).",
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
        "Rewrite the client fixture from Flask's `app.test_client()` to "
        "`fastapi.testclient.TestClient(app)`, and the body helper from `resp.get_json()` to "
        "`resp.json()`. Do NOT change any test assertions or other fixtures' behaviour.",
    ),
}


def _is_conftest(path: str) -> bool:
    return path.endswith("conftest.py") or "/tests/" in f"/{path}"


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
        if is_flask and (_BLUEPRINT.search(src) or _ROUTE.search(src)):
            role = "router"
            order = 10
            if _BLUEPRINT.search(src):
                subtasks.append(_SUBTASKS["blueprint_to_router"])
            if _ROUTE.search(src):
                subtasks.append(_SUBTASKS["route_to_endpoint"])
            if _REQUEST_PARSE.search(src):
                subtasks.append(_SUBTASKS["request_parsing"])
        elif is_flask and _APP_FACTORY.search(src):
            role = "app_factory"
            order = 20
            subtasks.append(_SUBTASKS["app_factory"])
            if _ERRORHANDLER.search(src):
                subtasks.append(_SUBTASKS["error_handler"])
        elif _is_conftest(path) and _TEST_CLIENT.search(src):
            role = "test_harness"
            order = 30
            subtasks.append(_SUBTASKS["test_harness"])

        if not subtasks:
            return None
        # A file may legitimately be both factory + routes; fold those in if present.
        if role == "app_factory" and _ROUTE.search(src):
            subtasks.append(_SUBTASKS["route_to_endpoint"])
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
            "fastapi, starlette, uvicorn, httpx, pydantic, pytest.\n"
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
