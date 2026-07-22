"""The source-derived Flask to FastAPI migration recipe."""

from __future__ import annotations

import ast
import textwrap
from copy import deepcopy
from pathlib import Path

from portage_agent.agent.nodes.common import imported_bindings_from_sources, iter_py_files

from ._flask_analysis import (
    _ALLOWED_IMPORT_ROOTS,
    _APP_FACTORY,
    _BLUEPRINT,
    _CLI_SEAM,
    _ERRORHANDLER,
    _FLASK_FAMILY_IMPORT,
    _FLASK_LOGIN,
    _FLASK_LOGIN_NAMES,
    _FLASK_SQLALCHEMY,
    _G_CONTEXT,
    _NOTE_DB_SURFACE,
    _NOTE_LOGIN_SURFACE,
    _NOTE_RESOURCE_FN,
    _REQUEST_PARSE,
    _RESOURCE_FUNCTION,
    _ROUTE,
    _SESSION_FLASH,
    _SUBTASKS,
    _TEMPLATE_FUNCTIONS,
    _TEMPLATES,
    _TEST_CLIENT,
    _artifact_capability_requirements,
    _artifact_placement_contract,
    _blueprint_error_handler_facts,
    _blueprint_factories,
    _click_command_contracts,
    _click_registrar_contracts,
    _decorated_provider_protocols,
    _detected_artifact_surfaces,
    _direct_json_return_functions,
    _direct_test_context_imports,
    _exception_handler_contracts,
    _exercised_flask_template_functions,
    _factory_config_facts,
    _factory_endpoint_aliases,
    _factory_local_imports,
    _factory_static_mount,
    _flask_login_consumer_contracts,
    _initializer_contracts,
    _instance_export_contracts,
    _is_test_file,
    _mixed_form_routes,
    _parsed,
    _plain_string_routes,
    _render_alias,
    _request_hook_facts,
    _resource_facts,
    _returned_lifecycle_contracts,
    _route_functions_without_local_handlers,
    _route_name_contracts,
    _sqlalchemy_provider_contracts,
    _template_context_processor_contracts,
    _template_framework_globals,
    _test_path_reason,
    _view_decorator_contracts,
)
from ._flask_runtime import (
    _realize_ambient_request_binding,
    _realize_cli_factory,
    _realize_decorated_provider_protocols,
    _realize_dynamic_instance_exports,
    _realize_extension_provider_facade,
    _realize_extension_provider_order,
    _realize_factory_contracts,
    _realize_implicit_sqlalchemy_tables,
    _realize_resource_consumers,
    _realize_resource_contracts,
)
from ._flask_web import (
    _normalize_exception_handler_status,
    _normalize_mutated_fetchone_rows,
    _normalize_redirect_urls,
    _normalize_session_middleware_import,
    _normalize_template_response,
    _normalize_werkzeug_abort,
    _realize_authentication_consumers,
    _realize_blueprint_error_handlers,
    _realize_error_handler_ownership,
    _realize_request_hook_names,
    _realize_route_contracts,
    _realize_template_consumers,
    _realize_template_context_processors,
    _realize_template_provider_globals,
    _realize_view_decorator_contracts,
)
from .base import MAX_CREATED_ARTIFACTS, PinRule, PlannedFile, Subtask, register
from .flask_test_compat import render_flask_test_compat


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
        content = _normalize_session_middleware_import(content)
        content = _normalize_template_response(content)
        content = _normalize_exception_handler_status(content)
        content = _normalize_redirect_urls(content)
        content = _normalize_werkzeug_abort(content)
        content = _normalize_mutated_fetchone_rows(content)
        content = _realize_ambient_request_binding(path, content, seam_plan)
        content = _realize_template_consumers(path, content, seam_plan)
        content = _realize_authentication_consumers(path, content, seam_plan)
        content = _realize_template_provider_globals(path, content, seam_plan)
        content = _realize_error_handler_ownership(path, content, seam_plan)
        content = _realize_blueprint_error_handlers(path, content, seam_plan)
        content = _realize_view_decorator_contracts(path, content, seam_plan)
        content = _realize_route_contracts(path, content, seam_plan)
        content = _realize_request_hook_names(path, content, seam_plan)
        content = _realize_resource_consumers(path, content, seam_plan)
        content = _realize_resource_contracts(path, content, seam_plan)
        content = _realize_factory_contracts(path, content, seam_plan)
        content = _realize_template_context_processors(path, content, seam_plan)
        content = _realize_implicit_sqlalchemy_tables(path, content, seam_plan)
        content = _realize_extension_provider_facade(path, content, seam_plan)
        content = _realize_dynamic_instance_exports(path, content, seam_plan)
        content = _realize_decorated_provider_protocols(path, content, seam_plan)
        content = _realize_extension_provider_order(path, content, seam_plan)
        return _realize_cli_factory(path, content, seam_plan)

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

        if capabilities == {"direct_test_surface"} and len(classes) == 1:
            members = set(classes[0].get("members", []))
            supported = {"app_context", "test_client", "test_cli_runner", "testing"}
            dependencies = contract.get("depends_on", [])
            if members and members <= supported and len(dependencies) == 1:
                class_name = classes[0]["name"]
                if not class_name.isidentifier():
                    return None
                runtime_module = dependencies[0].removesuffix(".py").replace("/", ".")
                registrars = _click_registrar_contracts(iter_py_files(worktree))
                registrar_imports = "\n".join(
                    f"from {item['module'].removesuffix('.py').replace('/', '.')} "
                    f"import {item['function']} as _register_cli_{index}"
                    for index, item in enumerate(registrars)
                )
                registrar_calls = "\n".join(
                    f"        _register_cli_{index}(self._cli)"
                    for index in range(len(registrars))
                )
                return textwrap.dedent(f'''\
                    from __future__ import annotations

                    from contextlib import contextmanager

                    import click
                    from click.testing import CliRunner
                    from fastapi import FastAPI
                    from fastapi.testclient import TestClient

                    from {runtime_module} import _pop_context, _push_context
                    {registrar_imports}


                    class _Response:
                        def __init__(self, response):
                            self._response = response

                        def __getattr__(self, name):
                            return getattr(self._response, name)

                        @property
                        def data(self):
                            return self._response.content

                        def get_json(self):
                            return self._response.json()

                        def get_data(self, as_text=False):
                            return self._response.text if as_text else self._response.content


                    class _Client:
                        def __init__(self, app):
                            self._client = TestClient(app, follow_redirects=False)

                        def __enter__(self):
                            self._client.__enter__()
                            return self

                        def __exit__(self, *args):
                            return self._client.__exit__(*args)

                        def __getattr__(self, name):
                            return getattr(self._client, name)

                        def request(self, *args, **kwargs):
                            return _Response(self._client.request(*args, **kwargs))

                        def get(self, *args, **kwargs):
                            return _Response(self._client.get(*args, **kwargs))

                        def post(self, *args, **kwargs):
                            return _Response(self._client.post(*args, **kwargs))

                        def put(self, *args, **kwargs):
                            return _Response(self._client.put(*args, **kwargs))

                        def patch(self, *args, **kwargs):
                            return _Response(self._client.patch(*args, **kwargs))

                        def delete(self, *args, **kwargs):
                            return _Response(self._client.delete(*args, **kwargs))


                    class _CliRegistry:
                        def __init__(self):
                            self.cli = self
                            self.commands = {{}}

                        def command(self, name=None, *args, **kwargs):
                            def decorate(function):
                                command = click.command(name, *args, **kwargs)(function)
                                self.commands[command.name] = command
                                return command
                            return decorate

                        def add_command(self, command, name=None):
                            self.commands[name or command.name] = command


                    class _CliRunner:
                        def __init__(self, commands):
                            self._commands = commands

                        def invoke(self, cli=None, args=None, **kwargs):
                            values = list(args or [])
                            command = cli
                            if command is None:
                                command = self._commands[values.pop(0)]
                            return CliRunner().invoke(command, args=values, **kwargs)


                    class _AppContext:
                        def __init__(self, app):
                            self._app = app
                            self._token = None

                        def push(self):
                            self._token = _push_context({{"app": self._app, "session": {{}}}})
                            return self._app

                        def pop(self):
                            if self._token is not None:
                                for callback in self._app._cleanup_callbacks:
                                    callback()
                                _pop_context(self._token)
                                self._token = None

                        def __enter__(self):
                            return self.push()

                        def __exit__(self, exc_type, exc, traceback):
                            self.pop()
                            return False


                    class {class_name}(FastAPI):
                        def __init__(
                            self, *args, testing=False, config=None,
                            cleanup_callbacks=(), **kwargs
                        ):
                            super().__init__(*args, **kwargs)
                            self.state.config = {{}}
                            if config is not None:
                                self.state.config.update({{
                                    name: getattr(config, name) for name in dir(config)
                                    if name.isupper()
                                }})
                            if testing:
                                self.state.config["TESTING"] = True
                            self._cleanup_callbacks = tuple(cleanup_callbacks)
                            self._cli = _CliRegistry()
                    {registrar_calls}

                        @property
                        def config(self):
                            return self.state.config

                        @property
                        def testing(self):
                            return bool(self.config.get("TESTING", False))

                        def app_context(self):
                            return _AppContext(self)

                        def test_client(self):
                            return _Client(self)

                        def test_cli_runner(self):
                            return _CliRunner(self._cli.commands)
                    ''')

        if (
            capabilities & {"request_context", "session_and_flash"}
            and capabilities <= {
                "request_context", "session_and_flash", "authentication",
            }
        ):
            if len(classes) != 1 or set(classes[0].get("members", [])) - {
                "dispatch", "get_request_context", "manage_session",
            }:
                return None
            class_name = classes[0]["name"]
            runtime = textwrap.dedent(f'''\
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


                def get_request_context() -> dict[str, Any]:
                    return _current()


                def manage_session(request: Request) -> dict:
                    return request.session


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
                        return get_request_context()

                    @staticmethod
                    def manage_session(request: Request) -> dict:
                        return manage_session(request)
                ''')
            if "authentication" not in capabilities:
                return runtime
            auth_contract = {
                **contract,
                "capabilities": ["authentication"],
                "depends_on": [file.path],
                "exports": [
                    item for item in exports if item.get("name") in _FLASK_LOGIN_NAMES
                ],
            }
            auth = FlaskToFastAPIRecipe.render_created_artifact(
                PlannedFile(
                    path=file.path, role=file.role, action="create",
                    artifact_contract=auth_contract,
                ),
                worktree,
            )
            if auth is None:
                return None
            own_module = file.path.removesuffix(".py").replace("/", ".")
            omitted = {
                "from __future__ import annotations",
                f"from {own_module} import g, session",
            }
            auth = "\n".join(
                line for line in auth.splitlines() if line not in omitted
            ).strip()
            return runtime + "\n" + auth + "\n"

        if capabilities == {"authentication"}:
            names = {item["name"] for item in exports}
            if not names or names - _FLASK_LOGIN_NAMES:
                return None
            dependencies = contract.get("depends_on", [])
            protocols = [
                protocol for protocol in _decorated_provider_protocols(
                    iter_py_files(worktree)
                )
                if "user_loader" in protocol.get("decorator_members", [])
            ]
            if len(dependencies) != 1 or len(protocols) != 1:
                return None
            runtime_module = dependencies[0].removesuffix(".py").replace("/", ".")
            manager_module = protocols[0]["provider"].removesuffix(".py").replace(
                "/", "."
            )
            manager_symbol = protocols[0]["symbol"]
            return textwrap.dedent(f'''\
                from __future__ import annotations

                import inspect
                from functools import wraps

                from fastapi.responses import RedirectResponse

                from {runtime_module} import g, session
                from {manager_module} import {manager_symbol} as _login_manager


                class _AnonymousUser:
                    is_authenticated = False
                    is_active = False
                    is_anonymous = True

                    @staticmethod
                    def get_id():
                        return None

                    def __bool__(self):
                        return False


                class _AuthenticatedUser:
                    is_authenticated = True
                    is_active = True
                    is_anonymous = False

                    def __init__(self, user):
                        self._user = user

                    def __getattr__(self, name):
                        return getattr(self._user, name)

                    def get_id(self):
                        getter = getattr(self._user, "get_id", None)
                        return getter() if callable(getter) else getattr(self._user, "id", None)

                    def __bool__(self):
                        return True


                _anonymous_user = _AnonymousUser()


                def _load_current_user():
                    user_id = session.get("_user_id")
                    loader = getattr(_login_manager, "load_user", None)
                    if user_id is None or not callable(loader):
                        return _anonymous_user
                    user = loader(user_id)
                    return _AuthenticatedUser(user) if user is not None else _anonymous_user


                class _CurrentUserProxy:
                    def __getattr__(self, name):
                        return getattr(_load_current_user(), name)

                    def __bool__(self):
                        return bool(_load_current_user())


                current_user = _CurrentUserProxy()


                def login_user(user, remember=False, duration=None, force=False, fresh=True):
                    getter = getattr(user, "get_id", None)
                    user_id = getter() if callable(getter) else getattr(user, "id", None)
                    if user_id is None:
                        return False
                    session["_user_id"] = str(user_id)
                    session["_fresh"] = bool(fresh)
                    if remember:
                        session["_remember"] = "set"
                    return True


                def logout_user():
                    for key in ("_user_id", "_fresh", "_remember"):
                        session.pop(key, None)
                    return True


                def _login_location():
                    request = g.request
                    endpoint = getattr(_login_manager, "login_view", None)
                    if endpoint:
                        names = [
                            route.name for route in request.app.router.routes
                            if route.name == endpoint or route.name.endswith(f".{{endpoint}}")
                        ]
                        if len(names) == 1:
                            endpoint = names[0]
                        try:
                            return request.url_for(endpoint).path
                        except Exception:
                            pass
                    return "/"


                def login_required(view):
                    @wraps(view)
                    async def wrapped_view(**kwargs):
                        if not current_user.is_authenticated:
                            return RedirectResponse(_login_location(), status_code=302)
                        result = view(**kwargs)
                        return await result if inspect.isawaitable(result) else result
                    return wrapped_view
                ''')

        if capabilities == {"direct_test_surface"} and len(classes) == 1:
            class_members = set(classes[0].get("members", []))
            lifecycle_contracts = [
                item for item in _returned_lifecycle_contracts(
                    iter_py_files(worktree), contract.get("consumers", []),
                )
                if item["factory_member"] in class_members
            ]
            if len(lifecycle_contracts) == 1 and class_members <= {
                lifecycle_contracts[0]["factory_member"], "testing",
            }:
                dependencies = contract.get("depends_on", [])
                if len(dependencies) != 1:
                    return None
                lifecycle = lifecycle_contracts[0]
                module = dependencies[0].removesuffix(".py").replace("/", ".")
                class_name = classes[0]["name"]
                factory_member = lifecycle["factory_member"]
                entry_member = lifecycle["entry_member"]
                exit_member = lifecycle["exit_member"]
                testing_parameter = ", testing=False" if "testing" in class_members else ""
                testing_assignment = (
                    "        self.testing = bool(testing)\n"
                    if "testing" in class_members else ""
                )
                return textwrap.dedent(f'''\
                    from __future__ import annotations

                    from fastapi import FastAPI

                    from {module} import _pop_context, _push_context


                    class _PortageReturnedLifecycle:
                        def __init__(self, app, cleanup_callbacks=()):
                            self._app = app
                            self._cleanup_callbacks = tuple(cleanup_callbacks)
                            self._tokens = []

                        def {entry_member}(self):
                            self._tokens.append(_push_context())
                            return self._app

                        def {exit_member}(self):
                            if not self._tokens:
                                raise RuntimeError("lifecycle exit without matching entry")
                            for callback in self._cleanup_callbacks:
                                callback()
                            _pop_context(self._tokens.pop())

                        def __enter__(self):
                            return self.{entry_member}()

                        def __exit__(self, exc_type, exc, traceback):
                            self.{exit_member}()
                            return False


                    class {class_name}(FastAPI):
                        def __init__(
                            self, *args{testing_parameter}, cleanup_callbacks=(), **kwargs
                        ):
                            super().__init__(*args, **kwargs)
                    {testing_assignment}        self._cleanup_callbacks = tuple(cleanup_callbacks)

                        def {factory_member}(self):
                            return _PortageReturnedLifecycle(
                                self, self._cleanup_callbacks
                            )
                    ''')

        if capabilities == {"direct_test_surface"}:
            if (
                len(classes) != 1
                or set(classes[0].get("members", [])) != {"test_client"}
            ):
                return None
            class_name = classes[0]["name"]
            return textwrap.dedent(f'''\
                from fastapi import FastAPI
                from fastapi.testclient import TestClient as _FastAPITestClient


                class {class_name}(FastAPI):
                    def test_client(self):
                        return _FastAPITestClient(self)
                ''')

        if capabilities == {"direct_test_surface", "test_context_surface"}:
            if len(classes) != 1 or set(classes[0].get("members", [])) - {
                "app_context", "testing",
            }:
                return None
            variables = {
                item["name"] for item in exports if item.get("kind") == "variable"
            }
            if variables - {"g", "session"}:
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

        function_names = {item["name"] for item in functions}
        if (
            capabilities == {"template_rendering"}
            and "render_template" in function_names
            and function_names <= _TEMPLATE_FUNCTIONS
        ):
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
            if "url_for" in function_names and not runtime_import:
                return None
            url_for_function = (
                "\n\n                def url_for(name: str, **path_params):\n"
                "                    return _TemplateRequest(g.request).url_for("
                "name, **path_params)\n"
                if "url_for" in function_names else ""
            )
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


                def render_template(request: Request, template_name: str, **context):
                    values = {{
                        **vars(request.state).get("_state", {{}}),
                        {runtime_context}"request": _TemplateRequest(request), **context,
                    }}
                    return templates.TemplateResponse(request, template_name, values)
                {url_for_function}
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
        complex_surface = any(
            pattern.search(source)
            for source in files.values()
            for pattern in (_TEMPLATES, _SESSION_FLASH, _FLASK_LOGIN, _FLASK_SQLALCHEMY)
        )
        complex_surface = complex_surface or bool(
            _exercised_flask_template_functions(files, set(files))
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
            "When unchanged templates consume an authentication global, keep the template "
            "provider standalone so its dependency on authentication cannot create a cycle "
            "with the request/session runtime.\n\n"
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
                if not classes and capability == "direct_test_surface":
                    target = {
                        "name": "FastAPIApp", "kind": "class", "signature": "",
                        "members": sorted(required_members),
                    }
                    owner["exports"].append(target)
                    classes = [target]
                    exports[target["name"]] = target
                    added_exports.append({"name": target["name"], "kind": "class"})
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
        direct_owners = [
            item for item in completed
            if "direct_test_surface" in item.get("capabilities", [])
        ]
        auth_owners = [
            item for item in completed
            if "authentication" in item.get("capabilities", [])
        ]
        template_globals = _template_framework_globals(files)
        returned_lifecycles = _returned_lifecycle_contracts(files, factory_paths)
        template_owners = [
            item for item in completed
            if "template_rendering" in item.get("capabilities", [])
        ]
        if len(auth_owners) == 1:
            auth_owner = auth_owners[0]
            bindings = _flask_login_consumer_contracts(files)
            consumers = sorted(bindings)
            previous_consumers = set(auth_owner.get("consumers", []))
            auth_owner["consumers"] = sorted({*previous_consumers, *consumers})
            exports = {item["name"] for item in auth_owner.get("exports", [])}
            additions = [
                {
                    "name": name,
                    "kind": "variable" if name == "current_user" else "function",
                    "signature": "",
                    "members": [],
                }
                for name in sorted({
                    item["symbol"] for entries in bindings.values() for item in entries
                } - exports)
            ]
            if additions or previous_consumers != set(auth_owner["consumers"]):
                auth_owner["exports"].extend(additions)
                audit = audit_by_path.setdefault(auth_owner["path"], {
                    "path": auth_owner["path"],
                    "capabilities": [],
                    "added_consumers": [],
                    "added_exports": [],
                    "added_class_members": [],
                })
                audit["added_consumers"] = sorted(
                    set(auth_owner["consumers"]) - previous_consumers
                )
                audit["added_exports"].extend(
                    {"name": item["name"], "kind": item["kind"]}
                    for item in additions
                )
        if len(template_owners) == 1:
            template_owner = template_owners[0]
            template_names = sorted({
                "render_template",
                *_exercised_flask_template_functions(
                    files, set(template_owner.get("consumers", [])),
                ),
            })
            template_exports = [
                {
                    "name": name, "kind": "function", "signature": "",
                    "members": [],
                }
                for name in template_names
            ]
            previous = template_owner.get("exports", [])
            standalone_template = set(template_owner.get("capabilities", [])) == {
                "template_rendering",
            }
            existing = {export.get("name") for export in previous}
            completed_exports = template_exports if standalone_template else [
                *previous,
                *(export for export in template_exports if export["name"] not in existing),
            ]
            if previous != completed_exports:
                template_owner["exports"] = completed_exports
                audit = audit_by_path.setdefault(template_owner["path"], {
                    "path": template_owner["path"],
                    "capabilities": [],
                    "added_consumers": [],
                    "added_exports": [],
                    "added_class_members": [],
                })
                previous_functions = {
                    export.get("name") for export in previous
                    if export.get("kind") == "function"
                }
                audit["added_exports"].extend(
                    {"name": name, "kind": "function"}
                    for name in template_names if name not in previous_functions
                )
                removed = [
                    export for export in previous
                    if standalone_template and (
                        export.get("name") not in template_names
                        or export.get("kind") != "function"
                    )
                ]
                if removed:
                    audit["removed_exports"] = [
                        {"name": export["name"], "kind": export["kind"]}
                        for export in removed
                    ]
                if standalone_template and any(
                    export.get("signature") for export in previous
                ):
                    audit["normalized_signatures"] = template_names
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
            if len(runtime_classes) <= 1:
                if runtime_classes:
                    runtime_class = runtime_classes[0]
                    if not combined_test_owner:
                        previous_members = set(runtime_class.get("members", []))
                        runtime_class["members"] = sorted(runtime_members)
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
                if added_runtime_export:
                    audit = audit_by_path.setdefault(runtime_owner["path"], {
                        "path": runtime_owner["path"],
                        "capabilities": [],
                        "added_consumers": [],
                        "added_exports": [],
                        "added_class_members": [],
                    })
                    audit["added_exports"].extend(added_runtime_export)
                elif not combined_test_owner:
                    removed_members = sorted(previous_members - runtime_members)
                    if removed_members:
                        audit = audit_by_path.setdefault(runtime_owner["path"], {
                            "path": runtime_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        audit["removed_class_members"] = [{
                            "export": runtime_class["name"],
                            "members": removed_members,
                        }]

            runtime_support = {
                "get_request_context": "function", "manage_session": "function",
                **(
                    {"g": "variable"}
                    if "request_context" in runtime_owner.get("capabilities", []) else {}
                ),
                **(
                    {
                        "session": "variable", "flash": "function",
                        "get_flashed_messages": "function",
                    }
                    if "session_and_flash" in runtime_owner.get("capabilities", []) else {}
                ),
            }
            if len(template_owners) == 1 and (
                runtime_owner["path"] != template_owners[0]["path"]
            ):
                runtime_support.update({
                    "g": "variable", "get_flashed_messages": "function",
                })
            if len(auth_owners) == 1 and _flask_login_consumer_contracts(files):
                runtime_support.update({"g": "variable", "session": "variable"})
            if len(direct_owners) == 1 and runtime_owner["path"] != direct_owners[0]["path"]:
                runtime_support.update({
                    "_push_context": "function", "_pop_context": "function",
                })
            runtime_signatures = {
                "_push_context": "def _push_context(state=None)",
                "_pop_context": "def _pop_context(token)",
                "flash": "def flash(message)",
                "get_flashed_messages": "def get_flashed_messages()",
                "get_request_context": "def get_request_context()",
                "manage_session": "def manage_session(request)",
            }
            runtime_names = {
                export.get("name") for export in runtime_owner.get("exports", [])
            }
            added_runtime_support = []
            normalized_runtime_signatures = []
            for name, kind in sorted(runtime_support.items()):
                if name in runtime_names:
                    existing = next(
                        export for export in runtime_owner["exports"]
                        if export.get("name") == name
                    )
                    if (
                        name in runtime_signatures
                        and existing.get("signature") != runtime_signatures[name]
                    ):
                        existing["signature"] = runtime_signatures[name]
                        normalized_runtime_signatures.append(name)
                    continue
                runtime_owner["exports"].append({
                    "name": name, "kind": kind,
                    "signature": runtime_signatures.get(name, ""), "members": [],
                })
                added_runtime_support.append({"name": name, "kind": kind})
            if added_runtime_support or normalized_runtime_signatures:
                audit = audit_by_path.setdefault(runtime_owner["path"], {
                    "path": runtime_owner["path"],
                    "capabilities": [],
                    "added_consumers": [],
                    "added_exports": [],
                    "added_class_members": [],
                })
                audit["added_exports"].extend(added_runtime_support)
                if normalized_runtime_signatures:
                    audit["normalized_signatures"] = sorted({
                        *audit.get("normalized_signatures", []),
                        *normalized_runtime_signatures,
                    })
            runtime_capabilities = set(runtime_owner.get("capabilities", []))
            if (
                runtime_capabilities <= {"request_context", "session_and_flash"}
                and len(runtime_classes) <= 1
            ):
                allowed = {*runtime_support, runtime_class["name"]}
                removed = [
                    export for export in runtime_owner["exports"]
                    if export.get("name") not in allowed
                ]
                if removed:
                    runtime_owner["exports"] = [
                        export for export in runtime_owner["exports"]
                        if export.get("name") in allowed
                    ]
                    audit = audit_by_path.setdefault(runtime_owner["path"], {
                        "path": runtime_owner["path"],
                        "capabilities": [],
                        "added_consumers": [],
                        "added_exports": [],
                        "added_class_members": [],
                    })
                    audit["removed_exports"] = [
                        {"name": export["name"], "kind": export["kind"]}
                        for export in removed
                    ]

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

            if len(direct_owners) == 1 and returned_lifecycles:
                direct_owner = direct_owners[0]
                if runtime_owner["path"] != direct_owner["path"]:
                    dependencies = set(direct_owner.get("depends_on", []))
                    if runtime_owner["path"] not in dependencies:
                        direct_owner["depends_on"] = sorted({
                            *dependencies, runtime_owner["path"],
                        })
                        audit = audit_by_path.setdefault(direct_owner["path"], {
                            "path": direct_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        audit["added_dependencies"] = [runtime_owner["path"]]
                lifecycle_note = (
                    " Implement these source-observed returned-object lifecycle "
                    f"contracts on the app facade: {returned_lifecycles}. Each factory "
                    "member returns a real object exposing the listed entry and exit "
                    "members as well as context-manager entry/exit; both paths bind and "
                    "reset the shared runtime context exactly once."
                )
                if lifecycle_note.strip() not in direct_owner["instructions"]:
                    direct_owner["instructions"] += lifecycle_note
                    audit = audit_by_path.setdefault(direct_owner["path"], {
                        "path": direct_owner["path"],
                        "capabilities": [],
                        "added_consumers": [],
                        "added_exports": [],
                        "added_class_members": [],
                    })
                    audit["instruction_completed"] = True

            if len(test_owners) == 1:
                test_owner = test_owners[0]
                test_capabilities = set(test_owner.get("capabilities", []))
                previous_exports = test_owner.get("exports", [])
                if (
                    test_capabilities
                    and test_capabilities <= {
                        "direct_test_surface", "test_context_surface",
                    }
                    and any(
                        export.get("kind") == "class"
                        for export in previous_exports
                    )
                ):
                    removed = [
                        export for export in previous_exports
                        if export.get("kind") == "function"
                    ]
                    if removed:
                        test_owner["exports"] = [
                            export for export in previous_exports
                            if export.get("kind") != "function"
                        ]
                        audit = audit_by_path.setdefault(test_owner["path"], {
                            "path": test_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        audit["removed_exports"] = [
                            {"name": export["name"], "kind": export["kind"]}
                            for export in removed
                        ]
                required_test_members = set(
                    requirements.get("direct_test_surface", {}).get(
                        "required_class_members", []
                    )
                )
                test_classes = [
                    export for export in test_owner.get("exports", [])
                    if export.get("kind") == "class"
                ]
                if {
                    "direct_test_surface", "test_context_surface",
                } <= test_capabilities and test_capabilities <= {
                    "direct_test_surface", "test_context_surface",
                } and len(test_classes) == 1:
                    proxy_exports = requirements.get("test_context_surface", {}).get(
                        "required_export_kinds", {},
                    )
                    previous_exports = test_owner.get("exports", [])
                    existing = {
                        export.get("name"): export.get("kind")
                        for export in previous_exports
                    }
                    completed_exports = [
                        *test_classes,
                        *({
                            "name": name, "kind": kind, "signature": "", "members": [],
                        } for name, kind in sorted(proxy_exports.items())),
                    ]
                    if previous_exports != completed_exports:
                        test_owner["exports"] = completed_exports
                        audit = audit_by_path.setdefault(test_owner["path"], {
                            "path": test_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        audit["added_exports"].extend(
                            {"name": name, "kind": kind}
                            for name, kind in sorted(proxy_exports.items())
                            if existing.get(name) != kind
                        )
                        removed = [
                            export for export in previous_exports
                            if export.get("kind") != "class"
                            and proxy_exports.get(export.get("name"))
                            != export.get("kind")
                        ]
                        audit.setdefault("removed_exports", []).extend(
                            {"name": export["name"], "kind": export["kind"]}
                            for export in removed
                        )
                if (
                    "direct_test_surface" in test_owner.get("capabilities", [])
                    and len(test_classes) == 1 and required_test_members
                ):
                    previous_members = set(test_classes[0].get("members", []))
                    missing_members = sorted(required_test_members - previous_members)
                    removed_members = sorted(previous_members - required_test_members)
                    test_classes[0]["members"] = sorted(required_test_members)
                    if missing_members or removed_members:
                        audit = audit_by_path.setdefault(test_owner["path"], {
                            "path": test_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        if missing_members:
                            audit["added_class_members"].append({
                                "export": test_classes[0]["name"],
                                "members": missing_members,
                            })
                        if removed_members:
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

            if len(auth_owners) == 1:
                auth_owner = auth_owners[0]
                if runtime_owner["path"] != auth_owner["path"]:
                    dependencies = set(auth_owner.get("depends_on", []))
                    if runtime_owner["path"] not in dependencies:
                        auth_owner["depends_on"] = sorted({
                            *dependencies, runtime_owner["path"],
                        })
                        audit = audit_by_path.setdefault(auth_owner["path"], {
                            "path": auth_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        audit.setdefault("added_dependencies", []).append(
                            runtime_owner["path"]
                        )
                if len(template_owners) == 1 and "current_user" in template_globals:
                    template_owner = template_owners[0]
                    dependencies = set(template_owner.get("depends_on", []))
                    if auth_owner["path"] not in dependencies:
                        template_owner["depends_on"] = sorted({
                            *dependencies, auth_owner["path"],
                        })
                        audit = audit_by_path.setdefault(template_owner["path"], {
                            "path": template_owner["path"],
                            "capabilities": [],
                            "added_consumers": [],
                            "added_exports": [],
                            "added_class_members": [],
                        })
                        audit.setdefault("added_dependencies", []).append(
                            auth_owner["path"]
                        )

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
            capabilities = set(item.get("capabilities", []))
            if (
                "template_rendering" in capabilities
                and len(capabilities) > 1
                and (
                    _template_framework_globals(files)
                    or _template_context_processor_contracts(files)
                )
            ):
                out.append(
                    f"artifact {path} combines template rendering with "
                    f"{sorted(capabilities - {'template_rendering'})}, but source "
                    "templates require shared globals/context processors; keep the "
                    "template provider standalone so its request-first contract has "
                    "one deterministic realization"
                )
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
            required = {
                name: "function" for name in sorted({
                    "render_template",
                    *_exercised_flask_template_functions(
                        files, set(owner.get("consumers", [])),
                    ),
                })
            }
            actual = {
                export.get("name"): export.get("kind") for export in exports
            }
            if any(actual.get(name) != kind for name, kind in required.items()):
                out.append(
                    f"standalone template owner {owner['path']} requires the "
                    f"template function exports {sorted(required)}"
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
        by_path = {item.path: item for item in planned}
        for path in sorted({
            item["handler_path"] for item in _blueprint_error_handler_facts(files)
        }):
            if path in by_path:
                if not any(task.type == "error_handler" for task in by_path[path].subtasks):
                    by_path[path].subtasks.append(_SUBTASKS["error_handler"])
                continue
            item = PlannedFile(
                path=path, role="support", order=15,
                subtasks=[_SUBTASKS["error_handler"]],
            )
            planned.append(item)
            by_path[path] = item
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

        provider_protocols = _decorated_provider_protocols(files)
        for protocol in provider_protocols:
            provider = protocol["provider"]
            symbol = protocol["symbol"]
            members = protocol["decorator_members"]
            decisions[f"provider_protocol:{provider}:{symbol}"] = {
                "kind": "provider_protocol",
                **protocol,
                "files": sorted({provider, *protocol["consumers"]}),
                "instruction": (
                    f"Source constructs `{symbol}` in `{provider}` and consumes its "
                    f"direct protocol {protocol}. Preserve that module-level "
                    "provider instance and make every listed member a callable decorator "
                    "or ordinary callable/attribute with the same source shape; do not replace the "
                    "provider with a class object or an unrelated framework primitive "
                    "that lacks those members."
                ),
            }

        auth_owners = [
            pf for pf in planned
            if pf.action == "create" and "authentication" in (
                pf.artifact_contract or {}
            ).get("capabilities", [])
        ]
        auth_consumers = _flask_login_consumer_contracts(files)
        login_managers = [
            protocol for protocol in provider_protocols
            if "user_loader" in protocol.get("decorator_members", [])
        ]
        if len(auth_owners) == 1 and auth_consumers:
            owner = auth_owners[0]
            manager = login_managers[0] if len(login_managers) == 1 else {}
            template_globals = _template_framework_globals(files)
            template_providers = sorted(
                pf.path for pf in planned
                if pf.action == "create" and "template_rendering" in (
                    pf.artifact_contract or {}
                ).get("capabilities", [])
            )
            decisions["authentication_runtime"] = {
                "kind": "authentication_runtime",
                "provider": owner.path,
                "exports": sorted(
                    export["name"]
                    for export in (owner.artifact_contract or {}).get("exports", [])
                ),
                "runtime_providers": list((owner.artifact_contract or {}).get(
                    "depends_on", []
                )),
                "manager_provider": manager.get("provider", ""),
                "manager_symbol": manager.get("symbol", ""),
                "consumer_bindings": auth_consumers,
                "template_globals": template_globals,
                "template_providers": template_providers,
                "files": sorted({
                    owner.path,
                    *auth_consumers,
                    *template_providers,
                    *(
                        [manager["provider"]]
                        if manager.get("provider") else []
                    ),
                    *(owner.artifact_contract or {}).get("depends_on", []),
                }),
                "instruction": (
                    "Preserve the source-imported Flask-Login surface on one session-backed "
                    f"provider: {auth_consumers}. Consumers import those exact names from "
                    f"`{owner.path}` instead of defining local substitutes. Preserve each "
                    "source call shape. `current_user` is an attribute proxy loaded through "
                    "the source user-loader provider; `login_required` wraps the original "
                    "view with a kwargs-only async wrapper. Template globals "
                    f"{template_globals} consume that same proxy."
                ),
            }

        returned_lifecycles = _returned_lifecycle_contracts(files, factory_paths)
        for pf in planned:
            contract = pf.artifact_contract or {}
            if pf.action != "create" or "direct_test_surface" not in contract.get(
                "capabilities", []
            ):
                continue
            classes = [
                export for export in contract.get("exports", [])
                if export.get("kind") == "class"
            ]
            if len(classes) != 1:
                continue
            members = set(classes[0].get("members", []))
            protocols = [
                item for item in returned_lifecycles
                if item["factory_member"] in members
            ]
            if protocols:
                decisions[f"returned_lifecycle:{pf.path}"] = {
                    "kind": "returned_lifecycle",
                    "provider": pf.path,
                    "class": classes[0]["name"],
                    "contracts": protocols,
                    "files": sorted({
                        pf.path, *contract.get("consumers", []),
                        *(item["consumer"] for item in protocols),
                    }),
                    "instruction": (
                        "The app facade has these source-observed returned-object "
                        f"protocols: {protocols}. Each factory member returns an object "
                        "with its exact entry/exit methods; that same object also supports "
                        "`with`. Entry binds the shared runtime context, exit invokes "
                        "cleanup callbacks and resets it exactly once."
                    ),
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
                    f"provider initializers: {initializers}. "
                    + (
                        "Flask config.from_object loads inherited uppercase attributes. "
                        f"Preserve that behavior for {config['from_objects']} by reading "
                        "uppercase names through dir()/getattr() or an equivalent MRO-aware "
                        "copy; vars() and __dict__ are forbidden because they drop inherited "
                        "settings. "
                        if config["from_objects"] else ""
                    )
                    + "A planned app-facade class "
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
                "config_from_objects": config["from_objects"],
                "instance_exports": _instance_export_contracts(manifest, pf.path),
                "local_imports": _factory_local_imports(files.get(pf.path, ""), pf.path),
                "initializers": initializers,
                "cleanup_callbacks": cleanup_callbacks,
                "endpoint_aliases": endpoint_aliases,
                "static_mount": static_mount,
            }

            handlers = _exception_handler_contracts(files.get(pf.path, ""))
            if handlers:
                names = {item["exception_name"] for item in handlers}
                imported_routes = [
                    candidate.path for candidate in planned
                    if candidate.role == "router" and any(
                        binding.importer == pf.path
                        for binding in imported_bindings_from_sources(
                            files, candidate.path,
                        )
                    )
                ]
                route_functions = {
                    path: functions
                    for path in [pf.path, *imported_routes]
                    if (functions := _route_functions_without_local_handlers(
                        files.get(path, ""), names,
                    ))
                }
                # Ambiguous terminal exception names cannot be safely matched across
                # differently imported provider modules, so keep those model-owned.
                if len(names) != len(handlers):
                    route_functions = {}
                decisions[f"error_handler_ownership:{pf.path}"] = {
                    "kind": "error_handler_ownership",
                    "owner": pf.path,
                    "files": sorted({pf.path, *route_functions}),
                    "handlers": handlers,
                    "route_functions": route_functions,
                    "instruction": (
                        "These source exception handlers own the HTTP status and top-level "
                        f"JSON envelope: {handlers}. Registered route functions "
                        f"{route_functions} must let those exceptions propagate; do not "
                        "catch them and replace the response with FastAPI's detail envelope."
                    ),
                }

        blueprint_handlers = _blueprint_error_handler_facts(files)
        for handler_path in sorted({item["handler_path"] for item in blueprint_handlers}):
            handlers = [
                item for item in blueprint_handlers if item["handler_path"] == handler_path
            ]
            factories = sorted({
                factory
                for item in handlers
                for factory in _blueprint_factories(
                    files, item["blueprint_provider"], item["blueprint_symbol"],
                    set(factory_paths),
                )
            })
            decisions[f"blueprint_error_handlers:{handler_path}"] = {
                "kind": "blueprint_error_handlers",
                "handler_path": handler_path,
                "factory_files": factories,
                "files": sorted({handler_path, *factories}),
                "handlers": handlers,
                "instruction": (
                    "Flask Blueprint error handlers have no APIRouter equivalent. Keep "
                    "each named handler as an undecorated exported function, preserving "
                    "its frozen exception/status registration, response helper calls, "
                    f"status constants, and payload keys: {handlers}. Register it on "
                    f"the application factories {factories} with add_exception_handler. "
                    "Do not turn handlers into routes or attach errorhandler/"
                    "exception_handler attributes to an APIRouter."
                ),
            }

        for contract in _sqlalchemy_provider_contracts(files, planned):
            provider = contract["provider"]
            symbol = contract["symbol"]
            decisions[f"extension_provider:{provider}:{symbol}"] = {
                "kind": "extension_provider",
                "files": sorted({provider, *contract["consumers"]}),
                **contract,
                "instruction": (
                    f"`{provider}` owns the module-level SQLAlchemy provider `{symbol}`. "
                    f"Its source-exercised public members are {contract['members']}; the "
                    "plain-SQLAlchemy replacement must realize each one on that exact "
                    "export. Assign that export at module scope before every consumer "
                    "import; never create or rebind it inside the application factory. "
                    "The factory may configure the already-existing provider. The "
                    f"provider may import declared consumers {contract['consumers']} only "
                    "after the module-level provider exists. Configure it from "
                    "SQLALCHEMY_DATABASE_URI only "
                    "after the factory has loaded the original configuration."
                ),
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
                "classes": classes,
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
            if pf.action == "create" and (
                "test_context_surface" in (pf.artifact_contract or {}).get(
                    "capabilities", []
                )
                or "direct_test_surface" in (pf.artifact_contract or {}).get(
                    "capabilities", []
                ) and any(
                    export.get("kind") == "class"
                    and "app_context" in export.get("members", [])
                    for export in (pf.artifact_contract or {}).get("exports", [])
                )
            )
        )
        if runtime_context_providers and test_context_providers:
            runtime_classes = {
                pf.path: sorted(
                    export["name"]
                    for export in (pf.artifact_contract or {}).get("exports", [])
                    if export.get("kind") == "class"
                )
                for pf in planned if pf.path in runtime_context_providers
            }
            decisions["ambient_context_runtime"] = {
                "kind": "ambient_context_runtime",
                "files": sorted({
                    *runtime_context_providers, *test_context_providers, *factory_paths,
                    *session_paths,
                }),
                "runtime_providers": runtime_context_providers,
                "runtime_classes": runtime_classes,
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
                "context_globals": _template_framework_globals(files),
                "authentication_provider": (
                    auth_owners[0].path if len(auth_owners) == 1 else ""
                ),
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

        context_processors = _template_context_processor_contracts(files)
        if context_processors and template_provider_paths:
            factory_files = sorted({item["provider"] for item in context_processors})
            context_keys = sorted({
                key for item in context_processors for key in item["keys"]
            })
            decisions["template_context_processors"] = {
                "kind": "template_context_processors",
                "processors": context_processors,
                "factory_files": factory_files,
                "template_provider_files": template_provider_paths,
                "files": sorted({*factory_files, *template_provider_paths}),
                "instruction": (
                    "Preserve every source @app.context_processor as a request-time "
                    "template context provider. Register the source callbacks on the "
                    "target app and merge their returned mappings into every render; "
                    "do not expose processors as HTTP routes. The source-observed "
                    f"returned keys are {context_keys}."
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
                    "configuration used by the direct helper. Context-cached resources may "
                    "instead use the exact `g` proxy imported from the frozen ambient-runtime "
                    "provider; never use Flask's `g`, a free global app/request, "
                    "`current_app`, or invented app attributes. "
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
                "path": pf.path,
                "files": list(members),
                "direct_json_return_functions": _direct_json_return_functions(
                    files.get(pf.path, ""),
                ),
                "instruction": (
                    "Use `fastapi.testclient.TestClient(app)` for HTTP plumbing. Remove "
                    "Flask application-context blocks and call real exported setup helpers "
                    "directly with their pinned signatures. Do not invent methods or state "
                    "attributes on FastAPI to imitate Flask (`app.container`, resource "
                    "openers, `test_client`, `test_cli_runner`, or similar). Preserve every "
                    "assertion and its meaning. A source helper that directly returns "
                    "response JSON may change only get_json() to json(); never unwrap or "
                    "reshape an application response envelope in the test harness."
                ),
            }

        cli_paths = [p for p, src in files.items() if p in by_path and _CLI_SEAM.search(src)]
        if cli_paths:
            command_bindings = _click_command_contracts(files)
            registrars = _click_registrar_contracts(files)
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
              | {item["module"] for item in registrars}
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
                "registrars": registrars,
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
