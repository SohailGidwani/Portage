"""R1.1: deterministic framework-seam decisions and selective coupled units."""

import json
from pathlib import Path

from portage_agent.agent.nodes.common import (
    build_manifest,
    build_migration_units,
    dependency_order,
)
from portage_agent.agent.nodes.execute import (
    _cluster_violations,
    all_generation_violations,
    framework_seam_violations,
    seam_sections,
)
from portage_agent.agent.nodes.plan import complete_unit_dependencies, drop_first_recipe_task
from portage_agent.recipes.base import PlannedFile, Subtask
from portage_agent.recipes.flask_to_fastapi import recipe


def _pf(path: str, role: str, *subtasks: str) -> PlannedFile:
    return PlannedFile(
        path=path,
        role=role,
        subtasks=[Subtask(s, s, "instruction") for s in subtasks],
    )


def test_drop_task_skips_deterministic_execution_infrastructure():
    adapter = PlannedFile(
        path="_portage_fastapi_test_compat.py", role="test_compat", subtasks=[],
        origin="infrastructure",
    )
    router = _pf("pkg/views.py", "router", "blueprint_to_router")
    factory = _pf("pkg/__init__.py", "app_factory", "app_factory")
    planned = [adapter, router, factory]

    assert drop_first_recipe_task(planned) is router
    assert planned == [adapter, factory]


def test_drop_task_fails_closed_when_only_infrastructure_exists():
    adapter = PlannedFile(
        path="_portage_fastapi_test_compat.py", role="test_compat", subtasks=[],
        origin="infrastructure",
    )
    planned = [adapter]

    assert drop_first_recipe_task(planned) is None
    assert planned == [adapter]


FILES = {
    "pkg/db.py": (
        "from flask import current_app, g\n"
        "def get_db():\n    return g.db\n"
    ),
    "pkg/__init__.py": (
        "from flask import Flask\n"
        "from . import db\n"
        "def create_app(config=None):\n"
        "    app = Flask(__name__)\n"
        "    db.init_app(app)\n"
        "    return app\n"
    ),
    "tests/conftest.py": (
        "from pkg import create_app\n"
        "from pkg.db import get_db\n"
        "def app():\n"
        "    app = create_app()\n"
        "    with app.app_context():\n"
        "        get_db()\n"
    ),
    "pkg/views.py": "from flask import Blueprint\nbp = Blueprint('v', __name__)\n",
}

PLANNED = [
    _pf("pkg/db.py", "support", "request_context"),
    _pf("pkg/views.py", "router", "blueprint_to_router"),
    _pf("pkg/__init__.py", "app_factory", "app_factory"),
    _pf("tests/conftest.py", "test_harness", "test_harness"),
]

MANIFEST = {
    "pkg/db.py::get_db": {
        "module": "pkg/db.py",
        "symbol": "get_db",
        "kind": "function",
        "original": "def get_db()",
        "target_note": "keep helper; add dependency",
        "preserve_shape": True,
        "additional_exports": ["get_db_dep"],
        "shape": {"required_positional": 0},
    }
}


def test_units_cluster_only_the_resource_factory_harness_seam():
    units = build_migration_units(FILES, PLANNED, MANIFEST)
    assert units == [{
        "id": "framework-seam-1",
        "paths": ["pkg/db.py", "pkg/__init__.py", "tests/conftest.py"],
        "reason": "shared resource/factory/test-harness seam",
    }]
    assert "pkg/views.py" not in units[0]["paths"]


def test_recipe_seam_plan_is_json_safe_and_generic():
    units = build_migration_units(FILES, PLANNED, MANIFEST)
    plan = recipe.build_seam_plan(FILES, PLANNED, MANIFEST, units)
    json.dumps(plan)
    kinds = {d["kind"] for d in plan["decisions"].values()}
    assert {"application_factory", "resource_lifecycle", "test_harness"} <= kinds
    assert plan["units"] == units


def test_recipe_freezes_application_owned_test_surface_realization():
    artifact = PlannedFile(
        path="pkg/testing.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["direct_test_surface"],
            "exports": [{
                "name": "AppFacade", "kind": "class",
                "members": ["testing", "test_client"],
            }],
            "consumers": ["pkg/__init__.py"],
            "depends_on": [],
        },
    )
    planned = [artifact, _pf("pkg/__init__.py", "app_factory", "app_factory")]

    plan = recipe.build_seam_plan(FILES, planned, {}, [])
    provider = seam_sections(plan, "pkg/testing.py")
    consumer = seam_sections(plan, "pkg/__init__.py")

    assert "target application facade/wrapper" in provider
    assert "not an HTTP TestClient" in provider
    assert "must import and construct the owner class" in consumer
    assert "_portage_fastapi_test_compat" in consumer

    provider_source = """\
class AppFacade:
    def __init__(self):
        self.add_middleware(object)
    def test_client(self):
        pass
"""
    assert any(
        "factory owns middleware configuration" in item
        for item in framework_seam_violations(
            provider_source, plan, "pkg/testing.py",
        )
    )


def test_seam_prompt_for_cluster_member_rejects_invented_app_apis():
    units = build_migration_units(FILES, PLANNED, MANIFEST)
    plan = recipe.build_seam_plan(FILES, PLANNED, MANIFEST, units)
    section = seam_sections(plan, "tests/conftest.py")
    assert "FRAMEWORK SEAM DECISIONS" in section
    assert "Do not invent" in section
    assert "app.state" in section
    assert "COUPLED MIGRATION UNIT" in section


def test_seam_prompt_explains_executable_verification_cut():
    plan = {
        "decisions": {},
        "units": [],
        "execution_cuts": [{
            "id": "executable-cut-1",
            "paths": ["pkg/views.py", "pkg/__init__.py"],
            "reason": "executable framework contracts: router_registration",
            "mode": "coordinated",
        }],
    }

    section = seam_sections(plan, "pkg/views.py")

    assert "EXECUTABLE VERIFICATION CUT executable-cut-1" in section
    assert "mixed Flask/FastAPI intermediate state" in section


def test_mechanical_seam_gate_rejects_invented_fastapi_capabilities():
    units = build_migration_units(FILES, PLANNED, MANIFEST)
    plan = recipe.build_seam_plan(FILES, PLANNED, MANIFEST, units)
    harness = (
        "def client(app):\n"
        "    with app.container():\n"
        "        return app.state.open_resource('db')\n"
    )
    violations = framework_seam_violations(harness, plan, "tests/conftest.py")
    assert any("container" in v for v in violations)
    assert any("open_resource" in v for v in violations)


def test_resource_helper_cannot_read_free_app_or_flask_context_globals():
    units = build_migration_units(FILES, PLANNED, MANIFEST)
    plan = recipe.build_seam_plan(FILES, PLANNED, MANIFEST, units)
    bad = "def get_db():\n    return app.state.config['DATABASE']\n"
    assert framework_seam_violations(bad, plan, "pkg/db.py")

    good = (
        "_config = {'DATABASE': 'x'}\n"
        "def get_db():\n"
        "    return _config['DATABASE']\n"
    )
    assert framework_seam_violations(good, plan, "pkg/db.py") == []


def test_resource_contract_freezes_config_cache_file_and_cleanup_topology():
    files = {
        **FILES,
        "pkg/db.py": """\
import click
import sqlite3
from flask import current_app, g
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'])
    return g.db
def close_db(error=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()
def init_db():
    with current_app.open_resource('schema.sql') as handle:
        get_db().executescript(handle.read().decode('utf8'))
@click.command('init-db')
def init_db_command():
    init_db()
def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
""",
        "pkg/__init__.py": """\
from flask import Flask
from . import db
def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(DATABASE='default.sqlite')
    if test_config:
        app.config.update(test_config)
    db.init_app(app)
    return app
""",
        "tests/conftest.py": FILES["tests/conftest.py"] + "\napp.test_cli_runner()\n",
    }
    units = build_migration_units(files, PLANNED, MANIFEST)
    plan = recipe.build_seam_plan(files, PLANNED, MANIFEST, units)
    resource = plan["decisions"]["resource_lifecycle:pkg/db.py::get_db"]

    assert resource["config_keys"] == ["DATABASE"]
    assert resource["context_cache_members"] == ["db"]
    assert resource["resource_files"] == ["schema.sql"]
    assert resource["cleanup_functions"] == ["close_db"]
    assert resource["dependency"] == "get_db_dep"
    assert resource["sqlite_cross_thread"] is True
    assert resource["factory_initializers"] == [{
        "factory": "pkg/__init__.py",
        "provider": "pkg/db.py",
        "symbol": "init_app",
        "original_call": "db.init_app(app)",
    }]

    bad = """\
import click
import sqlite3
DATABASE = None
def get_db():
    return sqlite3.connect(DATABASE)
def get_db_dep():
    yield get_db()
def close_db(error=None):
    get_db().close()
def init_db():
    with open('pkg/schema.sql') as handle:
        get_db().executescript(handle.read().decode('utf8'))
@click.command('init-db')
def init_db_command():
    init_db()
def init_app(app):
    global DATABASE
    DATABASE = app.state.config['DATABASE']
    app.cli.add_command(init_db_command)
"""
    violations = framework_seam_violations(bad, plan, "pkg/db.py")
    assert any("breaks resource identity" in item for item in violations)
    assert any("process cwd" in item for item in violations)
    assert any("binary mode" in item for item in violations)
    assert any("check_same_thread=False" in item for item in violations)
    assert any("FastAPI has no `app.cli`" in item for item in violations)
    assert any("clear the module-owned cached resource" in item for item in violations)

    direct_contextvar = """\
import click
import sqlite3
from contextvars import ContextVar
from pathlib import Path
DATABASE = None
_db = ContextVar('db', default=None)
def get_db():
    db = _db.get()
    if db is None:
        db = sqlite3.connect(DATABASE, check_same_thread=False)
        _db.set(db)
    return db
def get_db_dep():
    db = get_db()
    try:
        yield db
    finally:
        close_db()
def close_db(error=None):
    db = _db.get()
    if db is not None:
        db.close()
        _db.set(None)
def init_db():
    with Path(__file__).with_name('schema.sql').open('rb') as handle:
        get_db().executescript(handle.read().decode('utf8'))
@click.command('init-db')
def init_db_command():
    init_db()
def init_app(app):
    global DATABASE
    DATABASE = app.state.config['DATABASE']
"""
    assert any(
        "do not store a live sqlite3 connection directly in ContextVar" in item
        for item in framework_seam_violations(
            direct_contextvar, plan, "pkg/db.py",
        )
    )

    good = """\
import click
import sqlite3
from contextvars import ContextVar
from pathlib import Path
DATABASE = None
_context = ContextVar('db_context', default=None)
def _state():
    state = _context.get()
    if state is None:
        state = {}
        _context.set(state)
    return state
def get_db():
    state = _state()
    if 'db' not in state:
        state['db'] = sqlite3.connect(DATABASE, check_same_thread=False)
    return state['db']
def get_db_dep():
    db = get_db()
    try:
        yield db
    finally:
        close_db()
def close_db(error=None):
    db = _state().pop('db', None)
    if db is not None:
        db.close()
def init_db():
    with Path(__file__).with_name('schema.sql').open('rb') as handle:
        get_db().executescript(handle.read().decode('utf8'))
@click.command('init-db')
def init_db_command():
    init_db()
def init_app(app):
    global DATABASE
    DATABASE = app.state.config['DATABASE']
"""
    assert framework_seam_violations(good, plan, "pkg/db.py") == []

    duplicate_cleanup = good.replace(
        "    DATABASE = app.state.config['DATABASE']",
        "    DATABASE = app.state.config['DATABASE']\n"
        "    app.state.cleanup_callbacks.append(close_db)",
    )
    assert any(
        "must not invent app.state cleanup storage" in item
        for item in framework_seam_violations(
            duplicate_cleanup, plan, "pkg/db.py",
        )
    )
    realized = recipe.normalize_generated("pkg/db.py", duplicate_cleanup, plan)
    assert "cleanup_callbacks" not in realized
    assert framework_seam_violations(realized, plan, "pkg/db.py") == []

    whole_mapping = good.replace(
        "global DATABASE\n    DATABASE = app.state.config['DATABASE']",
        "global DATABASE\n    DATABASE = app.state.config",
    )
    assert not any(
        "must copy configuration keys" in item
        for item in framework_seam_violations(whole_mapping, plan, "pkg/db.py")
    )

    whole_mapping_update = good.replace(
        "DATABASE = None",
        "DATABASE = None\nCONFIG = {}",
    ).replace(
        "global DATABASE\n    DATABASE = app.state.config['DATABASE']",
        "CONFIG.update(app.state.config)",
    )
    assert not any(
        "must copy configuration keys" in item
        for item in framework_seam_violations(
            whole_mapping_update, plan, "pkg/db.py",
        )
    )

    manual_dependency = """\
from .db import get_db_dep
def create_app():
    db = get_db_dep()
    return db
"""
    assert any(
        "must be passed to Depends without calling it" in item
        for item in framework_seam_violations(
            manual_dependency, plan, "pkg/__init__.py",
        )
    )


def test_factory_contract_preserves_alias_and_implicit_static_endpoint():
    files = {
        "pkg/__init__.py": """\
from flask import Flask
def create_app():
    app = Flask(__name__)
    app.add_url_rule('/', endpoint='index')
    return app
""",
        "pkg/templates/base.html": "{{ url_for('static', filename='style.css') }}",
        "pkg/static/style.css": "",
    }
    planned = [PlannedFile(path="pkg/__init__.py", role="app_factory")]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    decision = plan["decisions"]["application_factory:pkg/__init__.py"]

    assert decision["endpoint_aliases"] == [{"path": "/", "name": "index"}]
    assert decision["static_mount"] == {
        "path": "/static", "name": "static", "directory": "pkg/static",
    }

    bad = "from fastapi import FastAPI\ndef create_app(): return FastAPI()\n"
    violations = framework_seam_violations(bad, plan, "pkg/__init__.py")
    assert any("reverse-URL alias 'index'" in item for item in violations)
    assert any("mounting StaticFiles" in item for item in violations)

    good = """\
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
async def index():
    return None
def create_app():
    app = FastAPI()
    app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')
    app.add_api_route('/', index, name='index')
    return app
"""
    assert framework_seam_violations(good, plan, "pkg/__init__.py") == []

    missing_alias = good.replace(
        "    app.add_api_route('/', index, name='index')\n", "",
    )
    realized = recipe.normalize_generated(
        "pkg/__init__.py", missing_alias, plan,
    )
    assert "name='index'" in realized
    assert framework_seam_violations(realized, plan, "pkg/__init__.py") == []

    invalid_alias = missing_alias.replace(
        "    return app\n",
        "    app.router.routes.append(app.router.url_path_for('blog.index').route('/'))\n"
        "    return app\n",
    )
    realized = recipe.normalize_generated("pkg/__init__.py", invalid_alias, plan)
    assert "router.routes.append" not in realized
    assert "add_api_route('/', lambda: None, name='index'" in realized

    bare_lifespan = good.replace(
        "app = FastAPI()",
        "app = FastAPI(lifespan=lifespan)",
    ).replace(
        "async def index():",
        "async def lifespan(app):\n    yield\nasync def index():",
    )
    assert any(
        "bare async generator" in item
        for item in framework_seam_violations(
            bare_lifespan, plan, "pkg/__init__.py",
        )
    )


def test_blueprint_route_names_are_frozen_realized_and_checked():
    files = {
        "pkg/auth.py": """\
from flask import Blueprint
bp = Blueprint('auth', __name__, url_prefix='/auth')
@bp.route('/register', methods=('GET', 'POST'))
def register(): pass
@bp.route('/logout', endpoint='sign_out')
def logout(): pass
""",
    }
    planned = [_pf("pkg/auth.py", "router", "route_to_endpoint")]
    plan = recipe.build_seam_plan(files, planned, {}, [])

    assert plan["decisions"]["route_names:pkg/auth.py"]["routes"] == [
        {"function": "register", "receiver": "bp", "name": "auth.register"},
        {"function": "logout", "receiver": "bp", "name": "auth.sign_out"},
    ]
    generated = """\
from fastapi import APIRouter
bp = APIRouter(prefix='/auth')
@bp.get('/register')
@bp.post('/register', name='wrong')
async def register(): pass
@bp.get('/logout')
async def logout(): pass
"""
    assert any(
        "reverse-URL name 'auth.register'" in item
        for item in framework_seam_violations(generated, plan, "pkg/auth.py")
    )

    realized = recipe.normalize_generated("pkg/auth.py", generated, plan)

    assert realized.count("name='auth.register'") == 2
    assert "name='auth.sign_out'" in realized
    assert framework_seam_violations(realized, plan, "pkg/auth.py") == []


def test_redirect_url_for_keeps_flask_relative_location_semantics():
    generated = """\
from starlette.responses import RedirectResponse
async def register(request):
    return RedirectResponse(url=request.url_for('auth.login'), status_code=302)
async def logout(request):
    return RedirectResponse(request.url_for('index'), status_code=302)
"""

    realized = recipe.normalize_generated("pkg/auth.py", generated, {})

    assert "request.url_for('auth.login').path" in realized
    assert "request.url_for('index').path" in realized


def test_werkzeug_abort_is_realized_as_raised_fastapi_http_exception():
    generated = """\
from fastapi import APIRouter
from werkzeug.exceptions import abort
def get_item(item_id):
    if item_id == 0:
        abort(404, 'missing')
"""

    realized = recipe.normalize_generated("pkg/views.py", generated, {})

    assert "werkzeug.exceptions" not in realized
    assert "from fastapi import APIRouter, HTTPException" in realized
    assert "def abort(status_code, description=None):" in realized
    assert "raise HTTPException(status_code=status_code, detail=description)" in realized

    missing_import = "def forbidden():\n    raise HTTPException(status_code=403)\n"
    normalized = recipe.normalize_generated("pkg/blog.py", missing_import)
    assert "from fastapi import HTTPException" in normalized


def test_original_view_decorator_wraps_contract_is_frozen_and_realized():
    files = {"pkg/auth.py": """\
import functools
def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        return view(**kwargs)
    return wrapped_view
"""}
    planned = [_pf("pkg/auth.py", "support", "request_context")]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = """\
def login_required(view):
    async def wrapped_view(request, **kwargs):
        return await view(request, **kwargs)
    return wrapped_view
"""
    assert any(
        "must preserve the wrapped endpoint signature" in item
        for item in framework_seam_violations(generated, plan, "pkg/auth.py")
    )

    realized = recipe.normalize_generated("pkg/auth.py", generated, plan)

    assert "import functools" in realized
    assert "@functools.wraps(view)" in realized
    assert "view(request=request, **kwargs)" in realized
    assert framework_seam_violations(realized, plan, "pkg/auth.py") == []


def test_factory_contract_requires_defaults_overrides_initializer_order_and_one_app():
    files = {
        **FILES,
        "pkg/db.py": (
            "from flask import current_app, g\n"
            "def get_db():\n    return current_app.config['DATABASE']\n"
            "def init_app(app):\n    pass\n"
        ),
        "pkg/__init__.py": """\
from flask import Flask
from . import db
def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(SECRET_KEY='dev', DATABASE='default.sqlite')
    if test_config:
        app.config.update(test_config)
    db.init_app(app)
    return app
""",
    }
    units = build_migration_units(files, PLANNED, MANIFEST)
    plan = recipe.build_seam_plan(files, PLANNED, MANIFEST, units)
    factory_contract = plan["decisions"]["application_factory:pkg/__init__.py"]
    assert factory_contract["optional_parameters"] == ["test_config"]
    plan["project_modules"] = ["pkg", "pkg.db", "pkg.testing"]
    plan["project_roots"] = ["pkg"]
    manifest = {
        "pkg/testing.py::AppFacade": {
            "module": "pkg/testing.py", "symbol": "AppFacade",
            "kind": "class", "target_kind": "class",
            "provenance": "planned_create", "members": ["testing"],
            "consumers": [{"module": "pkg/__init__.py"}],
        },
    }
    bad = """\
from fastapi import FastAPI
from . import db
from .testing import AppFacade
def create_app(test_config=None):
    app = FastAPI()
    db.init_app(app)
    app.state.config = {'DATABASE': 'default.sqlite'}
    return AppFacade(app)
"""
    violations = framework_seam_violations(
        bad, plan, "pkg/__init__.py", manifest,
    )
    assert any("omits original default keys ['SECRET_KEY']" in item for item in violations)
    assert any("must apply `test_config`" in item for item in violations)
    assert any("must run after defaults" in item for item in violations)
    assert any("not wrapped around" in item for item in violations)
    assert any("must receive `testing=`" in item for item in violations)

    good = """\
from . import db
from .testing import AppFacade
def create_app(test_config=None):
    app = AppFacade(testing=bool(test_config and test_config.get('TESTING')))
    app.state.config = {'SECRET_KEY': 'dev', 'DATABASE': 'default.sqlite'}
    if test_config:
        app.state.config.update(test_config)
    db.init_app(app)
    return app
"""
    assert framework_seam_violations(
        good, plan, "pkg/__init__.py", manifest,
    ) == []
    local_config = """\
from . import db
from .testing import AppFacade
def create_app(test_config=None):
    config = {'SECRET_KEY': 'dev', 'DATABASE': 'default.sqlite'}
    if test_config is not None:
        config.update(test_config)
    app = AppFacade(testing=config.get('TESTING', False))
    app.state.config = config
    db.init_app(app)
    return app
"""
    assert framework_seam_violations(
        local_config, plan, "pkg/__init__.py", manifest,
    ) == []
    unsafe_optional = good.replace(
        "bool(test_config and test_config.get('TESTING'))",
        "test_config.get('TESTING', False)",
    )
    assert any(
        "optional config `test_config` may be None" in item
        for item in framework_seam_violations(
            unsafe_optional, plan, "pkg/__init__.py", manifest,
        )
    )


def test_plain_flask_string_route_requires_explicit_fastapi_text_response():
    files = {
        "pkg/app.py": """\
from flask import Flask
def create_app():
    app = Flask(__name__)
    @app.route('/hello')
    def hello():
        return 'Hello, World!'
    return app
""",
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    bad = """\
from fastapi import FastAPI
def create_app():
    app = FastAPI()
    @app.get('/hello')
    async def hello():
        return 'Hello, World!'
    return app
"""
    assert any(
        "bare string changes the response bytes" in item
        for item in framework_seam_violations(bad, plan, "pkg/app.py")
    )
    good = bad.replace(
        "from fastapi import FastAPI", "from fastapi import FastAPI\n"
        "from fastapi.responses import PlainTextResponse",
    ).replace("@app.get('/hello')", "@app.get('/hello', name='hello')").replace(
        "return 'Hello, World!'", "return PlainTextResponse('Hello, World!')",
    )
    assert framework_seam_violations(good, plan, "pkg/app.py") == []

    decorator_response = good.replace(
        "@app.get('/hello', name='hello')",
        "@app.get('/hello', name='hello', response_class=PlainTextResponse)",
    ).replace(
        "return PlainTextResponse('Hello, World!')", "return 'Hello, World!'",
    )
    assert framework_seam_violations(decorator_response, plan, "pkg/app.py") == []


def test_session_provider_uses_request_mapping_not_raw_cookies():
    plan = {
        "decisions": {
            "session_runtime": {
                "kind": "session_runtime", "files": ["pkg/context.py"],
                "factory_files": [], "provider_files": ["pkg/context.py"],
                "original_cookie_writer_files": [], "instruction": "request session",
            },
        },
    }
    bad = "def manage(response):\n    response.set_cookie('session', 'value')\n"
    violations = framework_seam_violations(bad, plan, "pkg/context.py")
    assert any("active request.session" in item for item in violations)
    assert any("not synthesize raw response cookies" in item for item in violations)

    good = "def manage(request):\n    request.session['user_id'] = 1\n"
    assert framework_seam_violations(good, plan, "pkg/context.py") == []


def test_template_provider_requires_current_request_first_api():
    plan = {
        "decisions": {
            "template_runtime": {
                "kind": "template_runtime", "files": ["pkg/templating.py"],
                "provider_files": ["pkg/templating.py"], "instruction": "request first",
                "provider_functions": {"pkg/templating.py": ["render"]},
            },
        },
    }
    bad = (
        "def render(request, name, context):\n"
        "    return templates.TemplateResponse(name, context)\n"
    )
    assert any(
        "request-first" in item
        for item in framework_seam_violations(bad, plan, "pkg/templating.py")
    )
    good = (
        "def render(request, name, context=None):\n"
        "    return templates.TemplateResponse(request, name, context)\n"
    )
    assert framework_seam_violations(good, plan, "pkg/templating.py") == []


def test_template_provider_rejects_nonexistent_direct_response_import():
    plan = {
        "decisions": {
            "template_runtime": {
                "kind": "template_runtime", "files": ["pkg/templating.py"],
                "provider_files": ["pkg/templating.py"], "instruction": "request first",
                "provider_functions": {"pkg/templating.py": ["render"]},
            },
        },
    }
    bad = """\
from starlette.responses import TemplateResponse
def render(request, name, **context):
    return TemplateResponse(name, {"request": request, **context})
"""
    violations = framework_seam_violations(bad, plan, "pkg/templating.py")
    assert any("not importable from response modules" in item for item in violations)
    assert any("configured Jinja2Templates instance" in item for item in violations)

    normalized = recipe.normalize_generated("pkg/templating.py", (
        "from fastapi.templating import Jinja2Templates\n"
        "from starlette.responses import TemplateResponse\n"
        "templates = Jinja2Templates(directory='templates')\n"
        "def render(request, name, **context):\n"
        "    return TemplateResponse(name, {'request': request, **context})\n"
    ))
    assert "from starlette.responses import TemplateResponse" not in normalized
    assert "templates.TemplateResponse(request, name" in normalized
    assert framework_seam_violations(
        normalized, plan, "pkg/templating.py",
    ) == []


def test_mixed_form_routes_and_request_hooks_are_frozen_and_checked():
    files = {
        "pkg/auth.py": """\
from flask import Blueprint, g, request, session
bp = Blueprint('auth', __name__)
@bp.before_app_request
def load_logged_in_user():
    g.user = session.get('user_id')
@bp.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        return request.form['username']
""",
    }
    planned = [_pf("pkg/auth.py", "router", "route_to_endpoint", "request_context")]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    assert plan["decisions"]["mixed_form_routes:pkg/auth.py"]["routes"] == [{
        "function": "login", "fields": ["username"],
    }]
    assert plan["decisions"]["request_hooks:pkg/auth.py"]["hooks"] == [{
        "function": "load_logged_in_user", "scope": "before_app_request",
        "session_keys": ["user_id"], "context_members": ["user"],
    }]

    bad = """\
from fastapi import APIRouter, Form
bp = APIRouter()
async def login(username: str = Form(...)):
    return username
"""
    violations = framework_seam_violations(bad, plan, "pkg/auth.py")
    assert any("must not require FastAPI Form" in item for item in violations)
    assert any("pre-request hook `load_logged_in_user`" in item for item in violations)

    good = """\
from fastapi import APIRouter, Depends, Request
async def load_logged_in_user(request: Request):
    request.state.user = request.session.get('user_id')
bp = APIRouter(dependencies=[Depends(load_logged_in_user)])
@bp.get('/login', name='auth.login')
@bp.post('/login', name='auth.login')
async def login(request: Request):
    if request.method == 'POST':
        form = await request.form()
        return form['username']
"""
    assert framework_seam_violations(good, plan, "pkg/auth.py") == []


def test_request_hook_wiring_is_checked_across_its_provider_consumer_cut():
    plan = {
        "project_modules": ["pkg", "pkg.auth"], "project_roots": ["pkg"],
        "decisions": {"request_hooks:pkg/auth.py": {
            "kind": "request_hooks", "path": "pkg/auth.py",
            "files": ["pkg/auth.py", "pkg/__init__.py"],
            "hooks": [{"function": "load_logged_in_user"}],
        }},
    }
    provider = "def load_logged_in_user():\n    pass\n"
    bad = {"pkg/auth.py": provider, "pkg/__init__.py": "app = object()\n"}
    assert any(
        "frozen provider/consumer cut" in item
        for item in _cluster_violations(bad, {}, plan, {}).get("pkg/auth.py", [])
    )

    good = {
        **bad,
        "pkg/__init__.py": (
            "from fastapi import Depends\n"
            "from .auth import load_logged_in_user\n"
            "dependency = Depends(load_logged_in_user)\n"
        ),
    }
    assert _cluster_violations(good, {}, plan, {}) == {}


def test_before_app_request_is_promoted_from_router_to_global_dependency():
    files = {
        "pkg/auth.py": """\
from flask import Blueprint, g, session
bp = Blueprint('auth', __name__)
@bp.before_app_request
def load_user(): g.user = session.get('user_id')
@bp.route('/login')
def login(): pass
""",
        "pkg/__init__.py": """\
from flask import Flask
def create_app(): return Flask(__name__)
""",
    }
    planned = [
        _pf("pkg/auth.py", "router", "route_to_endpoint", "request_context"),
        _pf("pkg/__init__.py", "app_factory", "app_factory"),
    ]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    plan.update({"project_modules": ["pkg", "pkg.auth"], "project_roots": ["pkg"]})
    auth = """\
from fastapi import APIRouter, Depends, Request
bp = APIRouter()
async def load_user(request: Request): pass
@bp.get('/login', name='auth.login', dependencies=[Depends(load_user)])
async def login(): pass
"""
    factory = """\
from fastapi import FastAPI
from .auth import bp
from starlette.responses import PlainTextResponse
def create_app():
    app = FastAPI()
    app.include_router(bp)
    @app.get('/hello')
    async def hello():
        return PlainTextResponse('hello')
    return app
"""
    broken = _cluster_violations(
        {"pkg/auth.py": auth, "pkg/__init__.py": factory}, {}, plan, {},
    )
    assert any(
        "must be a global application dependency" in item
        for items in broken.values() for item in items
    )

    realized = recipe.normalize_generated("pkg/__init__.py", factory, plan)

    assert "from fastapi import FastAPI, Depends" in realized
    assert "from .auth import bp, load_user" in realized
    assert "FastAPI(dependencies=[Depends(load_user)])" in realized
    assert _cluster_violations(
        {"pkg/auth.py": auth, "pkg/__init__.py": realized}, {}, plan, {},
    ) == {}

    no_provider_import = recipe.normalize_generated(
        "pkg/__init__.py",
        "from __future__ import annotations\nfrom fastapi import FastAPI\n"
        "def create_app():\n    app = FastAPI()\n    return app\n",
        plan,
    )
    assert no_provider_import.startswith("from __future__ import annotations")
    assert "from pkg.auth import load_user" in no_provider_import
    assert "FastAPI(dependencies=[Depends(load_user)])" in no_provider_import


def test_new_artifact_rejects_dependency_unavailable_offline():
    plan = {
        "allowed_import_roots": ["fastapi", "werkzeug"],
        "original_import_roots": {"pkg/authentication.py": []},
        "project_roots": ["pkg"],
    }
    bad = "from passlib.context import CryptContext\n"
    assert any(
        "import `passlib` is unavailable" in item
        for item in framework_seam_violations(
            bad, plan, "pkg/authentication.py",
        )
    )
    good = "from werkzeug.security import check_password_hash\n"
    assert framework_seam_violations(good, plan, "pkg/authentication.py") == []


def test_cli_dispatcher_must_strip_old_command_token():
    files = {
        **FILES,
        "pkg/db.py": FILES["pkg/db.py"] + (
            "\nimport click\n@click.command('init-db')\ndef init_db_command():\n    pass\n"
        ),
        "tests/conftest.py": FILES["tests/conftest.py"] + "\napp.test_cli_runner()\n",
    }
    units = build_migration_units(files, PLANNED, MANIFEST)
    plan = recipe.build_seam_plan(files, PLANNED, MANIFEST, units)
    bad = (
        "from click.testing import CliRunner\n"
        "from pkg.db import init_db_command\n"
        "def invoke(args):\n"
        "    return CliRunner().invoke(init_db_command, args=args)\n"
    )
    violations = framework_seam_violations(bad, plan, "tests/conftest.py")
    assert any("old command token" in v for v in violations)

    good = bad.replace("args=args", "args=args[1:]")
    assert framework_seam_violations(good, plan, "tests/conftest.py") == []

    low_level = "def invoke(args):\n    return init_db_command.main(args=args[1:])\n"
    assert any(
        "low-level Click" in v
        for v in framework_seam_violations(low_level, plan, "tests/conftest.py")
    )

    attached = "def wire(app):\n    app.state.init_db_command = init_db_command\n"
    assert any(
        "must not be stored" in v
        for v in framework_seam_violations(attached, plan, "tests/conftest.py")
    )


def test_cli_contract_preserves_owner_late_binding_and_exact_harness_mapping():
    files = {
        **FILES,
        "pkg/db.py": FILES["pkg/db.py"] + """\
import click
def init_db():
    pass
@click.command('init-db')
def init_db_command():
    init_db()
""",
        "tests/conftest.py": FILES["tests/conftest.py"] + "\napp.test_cli_runner()\n",
    }
    units = build_migration_units(files, PLANNED, MANIFEST)
    plan = recipe.build_seam_plan(files, PLANNED, MANIFEST, units)
    plan["decisions"]["test_compatibility"] = {
        "kind": "test_compatibility", "files": ["tests/conftest.py"],
        "module": "_portage_fastapi_test_compat", "instruction": "adapt",
    }
    binding = plan["decisions"]["standalone_cli"]["command_bindings"]
    assert binding == [{
        "name": "init-db", "function": "init_db_command", "module": "pkg/db.py",
        "handlers": ["init_db"],
    }]

    bad_owner = """\
import click
def init_db():
    pass
@click.command('reset-db')
def init_db_command():
    pass
"""
    owner_violations = framework_seam_violations(bad_owner, plan, "pkg/db.py")
    assert any("real Click command named `init-db`" in item for item in owner_violations)
    assert any("module-level handlers ['init_db']" in item for item in owner_violations)

    bad_harness = """\
from _portage_fastapi_test_compat import adapt_app
from pkg.db import init_db_command
def app():
    raw = create_app()
    return adapt_app(raw, commands={'wrong-name': init_db_command})
"""
    harness_violations = framework_seam_violations(
        bad_harness, plan, "tests/conftest.py",
    )
    assert any(
        "commands must map `init-db`" in item for item in harness_violations
    )

    good_harness = bad_harness.replace("'wrong-name'", "'init-db'")
    assert framework_seam_violations(
        good_harness, plan, "tests/conftest.py",
    ) == []


def test_session_middleware_installation_is_separate_from_session_access():
    plan = {
        "version": 1,
        "decisions": {
            "session_runtime": {
                "kind": "session_runtime",
                "files": ["pkg/context.py", "pkg/app.py"],
                "factory_files": ["pkg/app.py"],
                "instruction": "Install middleware; expose request-backed access.",
            },
        },
        "project_modules": [],
        "project_roots": [],
    }
    bad_owner = (
        "from starlette.middleware.sessions import SessionMiddleware\n"
        "session = SessionMiddleware(secret_key='secret')\n"
    )
    violations = framework_seam_violations(
        bad_owner, plan, "pkg/context.py",
    )
    assert any("ASGI middleware, not a session/proxy" in item for item in violations)

    missing_install = (
        "from fastapi import FastAPI\n"
        "from starlette.middleware.sessions import SessionMiddleware\n"
        "def create_app():\n"
        "    return FastAPI()\n"
    )
    assert any(
        "requires app.add_middleware" in item
        for item in framework_seam_violations(missing_install, plan, "pkg/app.py")
    )

    installed = (
        "from fastapi import FastAPI\n"
        "from starlette.middleware.sessions import SessionMiddleware\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    app.add_middleware(SessionMiddleware, secret_key='secret')\n"
        "    return app\n"
    )
    assert framework_seam_violations(installed, plan, "pkg/app.py") == []


def test_apirouter_cannot_own_middleware():
    source = (
        "from fastapi import APIRouter as Router\n"
        "errors = Router()\n"
        "@errors.middleware('http')\n"
        "async def catch(request, call_next):\n"
        "    return await call_next(request)\n"
    )
    assert any(
        "APIRouter `errors` has no `middleware` API" in item
        for item in framework_seam_violations(
            source, {"project_modules": ["pkg.errors"]}, "pkg/errors.py",
        )
    )
    assert any(
        "APIRouter `errors` has no `exception_handler` API" in item
        for item in framework_seam_violations(
            source.replace("middleware('http')", "exception_handler(Exception)"),
            {"project_modules": ["pkg.errors"]}, "pkg/errors.py",
        )
    )


def test_planned_test_surfaces_share_concrete_runtime_semantics():
    provider = PlannedFile(
        path="pkg/testing.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["direct_test_surface", "test_context_surface"],
            "exports": [
                {"name": "AppFacade", "kind": "class", "members": [
                    "app_context", "testing",
                ]},
                {"name": "g", "kind": "variable", "members": []},
                {"name": "session", "kind": "variable", "members": []},
            ],
            "consumers": ["pkg/app.py", "tests/test_auth.py"],
            "depends_on": [],
        },
    )
    factory = PlannedFile(path="pkg/app.py", role="app_factory")

    decisions = recipe.build_seam_plan(
        {}, [provider, factory], {}, [],
    )["decisions"]
    facade = decisions["planned_test_surface:pkg/testing.py"]["instruction"]
    context = decisions["planned_test_context:pkg/testing.py"]["instruction"]

    assert "return an instance of this exact class" in facade
    assert "ASGI-callable" in facade
    assert "never source it from invented `app.state.testing`" in facade
    assert "proxy OBJECT" in context
    assert "contextvars.ContextVar" in context
    assert "`g` must support attribute access" in context
    assert "`session` must support mapping operations" in context


def test_application_context_owner_must_run_resource_cleanup_callbacks():
    plan = {
        "decisions": {
            "surface": {
                "kind": "planned_test_surface", "provider": "pkg/testing.py",
                "files": ["pkg/testing.py"], "instruction": "owned context",
            },
            "resource": {
                "kind": "resource_lifecycle", "module": "pkg/db.py",
                "files": ["pkg/testing.py"], "cleanup_functions": ["close_db"],
                "instruction": "close on exit",
            },
        },
    }
    bad = """\
class App:
    def app_context(self):
        yield self
"""
    assert any(
        "invoke each callback" in item
        for item in framework_seam_violations(bad, plan, "pkg/testing.py")
    )
    good = """\
from contextlib import contextmanager
class App:
    def __init__(self, cleanup_callbacks=()):
        self._cleanup_callbacks = cleanup_callbacks
    @contextmanager
    def app_context(self):
        try:
            yield self
        finally:
            for callback in self._cleanup_callbacks:
                callback()
"""
    assert framework_seam_violations(good, plan, "pkg/testing.py") == []

    wrong_receiver = """\
class App:
    def __init__(self, cleanup_callbacks=()):
        self._cleanup_callbacks = cleanup_callbacks
    def app_context(self):
        class Context:
            def __exit__(self, *args):
                for callback in self._cleanup_callbacks:
                    callback()
        return Context()
"""
    assert any(
        "invoke each callback" in item
        for item in framework_seam_violations(wrong_receiver, plan, "pkg/testing.py")
    )


def test_split_runtime_and_test_contexts_share_one_store_and_middleware_order():
    runtime = PlannedFile(
        path="pkg/context.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["request_context", "session_and_flash"],
            "exports": [{
                "name": "RuntimeContext", "kind": "class", "members": [],
            }],
            "consumers": ["pkg/auth.py", "pkg/app.py"], "depends_on": [],
        },
    )
    testing = PlannedFile(
        path="pkg/testing.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["test_context_surface"],
            "exports": [
                {"name": "g", "kind": "variable", "members": []},
                {"name": "session", "kind": "variable", "members": []},
            ],
            "consumers": ["tests/test_auth.py"], "depends_on": ["pkg/context.py"],
        },
    )
    planned = [
        runtime, testing, _pf("pkg/auth.py", "router", "sessions_flash"),
        _pf("pkg/app.py", "app_factory", "app_factory"),
    ]
    manifest = {
        "pkg/context.py::RuntimeContext": {
            "module": "pkg/context.py", "symbol": "RuntimeContext",
            "target_kind": "class", "provenance": "planned_create",
            "members": [], "consumers": [],
        },
    }
    plan = recipe.build_seam_plan({}, planned, manifest, [])

    bad_runtime = """\
from contextvars import ContextVar
state = ContextVar('state', default={})
class RuntimeContext:
    pass
"""
    assert any(
        "implement request middleware" in item
        for item in framework_seam_violations(
            bad_runtime, plan, "pkg/context.py", manifest,
        )
    )
    provider_installs = bad_runtime.replace(
        "    pass", "    async def dispatch(self, request, call_next):\n"
        "        return await call_next(request)\n"
        "def init_app(app):\n"
        "    app.add_middleware(RuntimeContext)",
    )
    assert any(
        "factory owns ordered installation" in item
        for item in framework_seam_violations(
            provider_installs, plan, "pkg/context.py", manifest,
        )
    )
    bad_testing = """\
from contextvars import ContextVar
g = ContextVar('g')
session = ContextVar('session')
"""
    assert any(
        "must import/re-export" in item
        for item in framework_seam_violations(
            bad_testing, plan, "pkg/testing.py", manifest,
        )
    )
    bad_factory = """\
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from .context import RuntimeContext
def create_app():
    app = FastAPI(lifespan=RuntimeContext.manage_session)
    app.add_middleware(SessionMiddleware, secret_key='secret')
    app.add_middleware(RuntimeContext)
    return app
"""
    assert any(
        "SessionMiddleware is outermost" in item
        for item in framework_seam_violations(
            bad_factory, plan, "pkg/app.py", manifest,
        )
    )
    assert any(
        "is middleware, not an application lifespan" in item
        for item in framework_seam_violations(
            bad_factory, plan, "pkg/app.py", manifest,
        )
    )
    normalized_factory = recipe.normalize_generated("pkg/app.py", bad_factory, plan)
    assert "lifespan=" not in normalized_factory
    assert normalized_factory.index("add_middleware(RuntimeContext)") < (
        normalized_factory.index("add_middleware(SessionMiddleware")
    )
    assert not any(
        "SessionMiddleware is outermost" in item
        for item in framework_seam_violations(
            normalized_factory, plan, "pkg/app.py", manifest,
        )
    )

    constructor_factory = """\
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from .context import RuntimeContext
def create_app():
    return FastAPI(middleware=[
        Middleware(RuntimeContext),
        Middleware(SessionMiddleware, secret_key='secret'),
    ])
"""
    assert any(
        "SessionMiddleware is outermost" in item
        for item in framework_seam_violations(
            constructor_factory, plan, "pkg/app.py", manifest,
        )
    )
    normalized_constructor = recipe.normalize_generated(
        "pkg/app.py", constructor_factory, plan,
    )
    assert normalized_constructor.index("Middleware(SessionMiddleware") < (
        normalized_constructor.index("Middleware(RuntimeContext")
    )
    assert not any(
        "SessionMiddleware is outermost" in item
        or "session runtime requires" in item
        or "factory must install" in item
        for item in framework_seam_violations(
            normalized_constructor, plan, "pkg/app.py", manifest,
        )
    )


def test_contract_compiler_links_split_test_context_to_runtime_owner():
    files = {
        "pkg/auth.py": "from flask import g, session\nprint(g.user, session.get('id'))\n",
        "pkg/blog.py": "from flask import g, session\nprint(g.user, session.get('id'))\n",
        "pkg/app.py": "from flask import Flask\ndef create_app(): return Flask(__name__)\n",
        "tests/test_auth.py": "from flask import g, session\n",
    }
    planned = [
        _pf("pkg/auth.py", "router", "request_context", "sessions_flash"),
        _pf("pkg/blog.py", "router", "request_context", "sessions_flash"),
        _pf("pkg/app.py", "app_factory", "app_factory"),
        _pf("tests/test_auth.py", "test_harness", "test_harness"),
    ]
    proposal = [
        {
            "path": "pkg/context.py", "role": "support", "purpose": "runtime",
            "instructions": "own runtime context",
            "capabilities": ["request_context", "session_and_flash"],
            "exports": [{
                "name": "RuntimeContext", "kind": "class", "signature": "",
                "members": [],
            }],
            "consumers": ["pkg/auth.py", "pkg/blog.py"], "depends_on": [],
        },
        {
            "path": "pkg/testing.py", "role": "support", "purpose": "test context",
            "instructions": "re-export runtime state",
            "capabilities": ["test_context_surface"],
            "exports": [
                {"name": "g", "kind": "variable", "signature": "", "members": []},
                {"name": "session", "kind": "variable", "signature": "", "members": []},
            ],
            "consumers": ["tests/test_auth.py"], "depends_on": [],
        },
        {
            "path": "pkg/templating.py", "role": "support", "purpose": "templates",
            "instructions": "render templates",
            "capabilities": ["template_rendering"],
            "exports": [{
                "name": "render_template", "kind": "function", "signature": "",
                "members": [],
            }],
            "consumers": ["pkg/auth.py", "pkg/blog.py"], "depends_on": [],
        },
    ]

    completed, audit = recipe.materialize_artifact_contracts(proposal, files, planned)

    test_owner = next(item for item in completed if item["path"] == "pkg/testing.py")
    runtime_owner = next(item for item in completed if item["path"] == "pkg/context.py")
    template_owner = next(
        item for item in completed if item["path"] == "pkg/templating.py"
    )
    assert "pkg/app.py" in runtime_owner["consumers"]
    assert "BaseHTTPMiddleware" in runtime_owner["instructions"]
    assert test_owner["depends_on"] == ["pkg/context.py"]
    assert template_owner["depends_on"] == ["pkg/context.py"]
    assert "cleanup_callbacks=()" in test_owner["instructions"]
    assert next(
        item for item in audit if item["path"] == "pkg/testing.py"
    )["added_dependencies"] == ["pkg/context.py"]


def test_recipe_deterministically_renders_fixed_context_test_and_template_plumbing(
    tmp_path,
):
    (tmp_path / "pkg" / "templates").mkdir(parents=True)
    files = {
        "pkg/app.py": "from flask import Flask\ndef create_app(): return Flask(__name__)\n",
        "pkg/auth.py": "from flask import g, render_template, session\n",
    }
    runtime = PlannedFile(
        path="pkg/context.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["request_context", "session_and_flash"],
            "exports": [
                {"name": "RuntimeContext", "kind": "class", "members": [
                    "dispatch", "get_request_context", "manage_session",
                ]},
                {"name": "g", "kind": "variable", "members": []},
                {"name": "session", "kind": "variable", "members": []},
                {"name": "flash", "kind": "function", "members": []},
                {"name": "get_flashed_messages", "kind": "function", "members": []},
            ],
            "consumers": ["pkg/app.py", "pkg/auth.py"], "depends_on": [],
        },
    )
    testing = PlannedFile(
        path="pkg/testing.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["direct_test_surface", "test_context_surface"],
            "exports": [
                {"name": "AppFacade", "kind": "class", "members": [
                    "app_context", "testing",
                ]},
                {"name": "g", "kind": "variable", "members": []},
                {"name": "session", "kind": "variable", "members": []},
            ],
            "consumers": ["pkg/app.py"], "depends_on": ["pkg/context.py"],
        },
    )
    templating = PlannedFile(
        path="pkg/templating.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["template_rendering"],
            "exports": [{
                "name": "render_template", "kind": "function", "members": [],
            }],
            "consumers": ["pkg/auth.py"], "depends_on": ["pkg/context.py"],
        },
    )
    planned = [
        runtime, testing, templating,
        PlannedFile(path="pkg/app.py", role="app_factory"),
        PlannedFile(path="pkg/auth.py", role="router"),
    ]
    manifest = build_manifest(str(tmp_path), planned, recipe.pin_rules)
    seam_plan = recipe.build_seam_plan(files, planned, manifest, [])

    rendered = {
        item.path: recipe.render_created_artifact(item, str(tmp_path))
        for item in (runtime, testing, templating)
    }

    assert all(rendered.values())
    assert "def __contains__" in rendered["pkg/context.py"]
    assert "from pkg.context import _pop_context, _push_context, g, session" in (
        rendered["pkg/testing.py"]
    )
    assert "TemplateResponse(request, template_name, values)" in (
        rendered["pkg/templating.py"]
    )
    assert 'path_params.pop("filename")' in rendered["pkg/templating.py"]
    assert "url_for(name, **path_params).path" in rendered["pkg/templating.py"]
    violations = {
        path: all_generation_violations(
            content, manifest, path, seam_plan, None,
        )
        for path, content in rendered.items()
    }
    assert not any(violations.values()), violations


def test_contract_compiler_canonicalizes_context_and_test_facade_for_rendering(
    tmp_path,
):
    files = {
        "pkg/app.py": "from flask import Flask\ndef create_app(): return Flask(__name__)\n",
        "pkg/auth.py": "from flask import g, session\nprint(g.user, session.get('id'))\n",
        "tests/test_factory.py": (
            "def test_factory(app):\n"
            "    assert app.testing\n"
            "    with app.app_context():\n"
            "        pass\n"
        ),
        "tests/test_auth.py": "from flask import g, session\n",
    }
    planned = [
        _pf("pkg/app.py", "app_factory", "app_factory"),
        _pf("pkg/auth.py", "router", "request_context", "sessions_flash"),
        _pf("tests/test_factory.py", "test_harness", "test_harness"),
        _pf("tests/test_auth.py", "test_harness", "test_harness"),
    ]
    proposal = [
        {
            "path": "pkg/context.py", "role": "support", "purpose": "runtime",
            "instructions": "provide request context",
            "capabilities": ["request_context", "session_and_flash"],
            "exports": [
                {"name": "get_request_context", "kind": "function", "members": []},
                {"name": "manage_session", "kind": "function", "members": []},
            ],
            "consumers": ["pkg/auth.py"], "depends_on": [],
        },
        {
            "path": "pkg/testing.py", "role": "support", "purpose": "testing",
            "instructions": "provide the app test surface",
            "capabilities": ["direct_test_surface", "test_context_surface"],
            "exports": [
                {"name": "app_context", "kind": "class", "members": [
                    "app", "app_context", "client", "runner", "testing",
                ]},
                {"name": "g", "kind": "variable", "members": []},
                {"name": "session", "kind": "variable", "members": []},
            ],
            "consumers": ["pkg/app.py", "tests/test_auth.py"], "depends_on": [],
        },
    ]

    completed, audit = recipe.materialize_artifact_contracts(
        proposal, files, planned,
    )
    runtime = next(item for item in completed if item["path"] == "pkg/context.py")
    testing = next(item for item in completed if item["path"] == "pkg/testing.py")
    runtime_classes = [
        export for export in runtime["exports"] if export["kind"] == "class"
    ]
    assert runtime_classes == [{
        "name": "RequestContextMiddleware", "kind": "class", "signature": "",
        "members": ["get_request_context", "manage_session"],
    }]
    assert not any(
        export["kind"] == "function" for export in runtime["exports"]
    )
    assert next(
        export for export in testing["exports"] if export["kind"] == "class"
    )["members"] == ["app_context", "testing"]
    assert next(
        item for item in audit if item["path"] == "pkg/testing.py"
    )["removed_class_members"][0]["members"] == ["app", "client", "runner"]
    assert recipe.artifact_plan_violations(completed, files, planned) == []

    rendered_runtime = recipe.render_created_artifact(
        PlannedFile(
            path=runtime["path"], role="support", action="create",
            artifact_contract=runtime,
        ),
        str(tmp_path),
    )
    rendered_testing = recipe.render_created_artifact(
        PlannedFile(
            path=testing["path"], role="support", action="create",
            artifact_contract=testing,
        ),
        str(tmp_path),
    )
    assert "class RequestContextMiddleware(BaseHTTPMiddleware)" in rendered_runtime
    assert "class app_context(FastAPI)" in rendered_testing

def test_bundled_structural_fixture_forms_one_bounded_generic_unit():
    root = Path(__file__).parent / "fixtures" / "flask_structural"
    files = {
        str(path.relative_to(root)): path.read_text()
        for path in root.rglob("*.py")
    }
    planned = recipe.plan_files(files)
    manifest = build_manifest(str(root), planned, recipe.pin_rules)
    units = build_migration_units(files, planned, manifest)
    assert len(units) == 1
    assert units[0]["paths"] == [
        "src/structapp/db.py",
        "src/structapp/__init__.py",
        "tests/conftest.py",
        "tests/test_structural.py",
    ]


def test_recipe_plans_flask_family_extension_provider_without_base_flask_import():
    files = {
        "pkg/extensions.py": (
            "from flask_login import LoginManager\n"
            "from flask_sqlalchemy import SQLAlchemy\n"
            "db = SQLAlchemy()\nlogin_manager = LoginManager()\n"
        ),
    }

    planned = recipe.plan_files(files)

    assert recipe.matches(files)
    assert [item.path for item in planned] == ["pkg/extensions.py"]
    assert {subtask.type for subtask in planned[0].subtasks} == {
        "auth_login", "sqlalchemy_plain",
    }


def test_repeated_capability_requires_one_artifact_owner_covering_all_consumers():
    files = {
        "pkg/one.py": "from flask import render_template\nrender_template('one.html')\n",
        "pkg/two.py": "from flask import render_template\nrender_template('two.html')\n",
    }
    planned = recipe.plan_files(files)

    missing = recipe.artifact_plan_violations([], files, planned)
    assert missing == ["template_rendering requires exactly one owner artifact, got 0"]

    artifact = {
        "path": "pkg/templating.py", "role": "support",
        "purpose": "Render templates.", "instructions": "Render templates.",
        "capabilities": ["template_rendering"],
        "consumers": ["pkg/one.py", "pkg/two.py"],
        "depends_on": [],
        "exports": [{
            "name": "TemplateRenderer", "kind": "class", "signature": "",
            "members": ["render_template"],
        }],
    }
    completed, audit = recipe.materialize_artifact_contracts(
        [artifact], files, planned,
    )
    assert completed[0]["exports"] == [{
        "name": "render_template", "kind": "function", "signature": "",
        "members": [],
    }]
    assert audit[0]["removed_exports"] == ["TemplateRenderer"]
    assert recipe.artifact_plan_violations(completed, files, planned) == []


def test_database_session_attribute_is_not_flask_session_capability():
    files = {
        "pkg/extensions.py": (
            "from flask_sqlalchemy import SQLAlchemy\n"
            "db = SQLAlchemy()\n"
            "def load(model, key):\n    return db.session.get(model, key)\n"
        ),
        "pkg/auth.py": (
            "from flask import session\n"
            "def user_id():\n    return session.get('user_id')\n"
        ),
    }
    planned = recipe.plan_files(files)
    artifact = {
        "path": "pkg/session.py", "capabilities": ["session_and_flash"],
        "consumers": ["pkg/auth.py"],
        "exports": [{
            "name": "RequestContextMiddleware", "kind": "class",
            "members": ["get_request_context", "manage_session"],
        }],
    }

    assert recipe.artifact_plan_violations([artifact], files, planned) == []


def test_request_context_and_session_require_one_runtime_owner():
    files = {
        "pkg/auth.py": "from flask import g, session\nprint(g.user, session.get('id'))\n",
        "pkg/blog.py": "from flask import g, session\nprint(g.user, session.get('id'))\n",
    }
    planned = recipe.plan_files(files)
    consumers = sorted(files)
    split = [
        {
            "path": "pkg/context.py", "capabilities": ["request_context"],
            "consumers": consumers,
            "exports": [{"name": "Context", "kind": "class", "members": []}],
        },
        {
            "path": "pkg/session.py", "capabilities": ["session_and_flash"],
            "consumers": consumers,
            "exports": [{"name": "Session", "kind": "class", "members": []}],
        },
    ]

    assert recipe.artifact_plan_violations(split, files, planned) == [
        "request_context and session_and_flash require one shared request-scoped "
        "runtime owner"
    ]


def test_direct_test_app_surface_requires_owned_class_consumed_by_factory():
    files = {
        "pkg/app.py": (
            "from flask import Flask\n"
            "def create_app():\n    return Flask(__name__)\n"
        ),
        "tests/test_app.py": (
            "from pkg.app import create_app\n"
            "app = create_app()\n"
            "with app.app_context():\n    client = app.test_client()\n"
        ),
    }
    planned = recipe.plan_files(files)
    missing = recipe.artifact_plan_violations([], files, planned)
    assert missing == ["direct_test_surface requires exactly one owner artifact, got 0"]

    incomplete = {
        "path": "pkg/testing.py", "capabilities": ["direct_test_surface"],
        "consumers": ["pkg/app.py"],
        "exports": [{"name": "CompatApp", "kind": "class", "members": ["test_client"]}],
    }
    violations = recipe.artifact_plan_violations([incomplete], files, planned)
    assert violations == [
        "direct_test_surface owner pkg/testing.py requires exactly one class export "
        "containing members ['app_context', 'test_client'], got []"
    ]

    incomplete["exports"][0]["members"].append("app_context")
    assert recipe.artifact_plan_violations([incomplete], files, planned) == []


def test_adapter_unit_uses_spare_capacity_for_factory_dependencies():
    root = Path(__file__).parent / "fixtures" / "flask_structural"
    files = {
        str(path.relative_to(root)): path.read_text()
        for path in root.rglob("*.py")
    }
    planned = dependency_order(files, recipe.plan_files(files))
    manifest = build_manifest(str(root), planned, recipe.pin_rules)
    unit = build_migration_units(files, planned, manifest)[0]
    unit["paths"] = [
        path for path in unit["paths"] if path != "tests/test_structural.py"
    ]
    completed = complete_unit_dependencies(
        str(root), planned, [unit],
        {"tests/test_structural.py": "adapter", "tests/conftest.py": "adapter_wiring"},
    )
    assert "src/structapp/views.py" in completed[0]["paths"]
    assert len(completed[0]["paths"]) == 4


def test_test_compatibility_rejects_raw_factory_app_before_adaptation():
    plan = {
        "decisions": {
            "test_compatibility": {
                "kind": "test_compatibility",
                "files": ["tests/conftest.py"],
                "module": "_portage_fastapi_test_compat",
                "instruction": "wrap the app",
            },
        },
        "units": [],
    }
    raw = """\
from _portage_fastapi_test_compat import adapt_app
from pkg import create_app
def app():
    app = create_app()
    with app.app_context():
        pass
    yield app
"""
    violations = framework_seam_violations(raw, plan, "tests/conftest.py")
    assert any("raw FastAPI" in item for item in violations)
    assert any("escapes the fixture" in item for item in violations)

    adapted = raw.replace(
        "app = create_app()", "app = create_app()\n    app = adapt_app(app)"
    )
    assert framework_seam_violations(adapted, plan, "tests/conftest.py") == []


def test_test_compatibility_requires_facade_cli_dispatch():
    plan = {
        "decisions": {
            "test_compatibility": {
                "kind": "test_compatibility", "files": ["tests/conftest.py"],
                "instruction": "wrap", "module": "_portage_fastapi_test_compat",
            },
            "cli": {
                "kind": "standalone_cli", "files": ["tests/conftest.py"],
                "instruction": "dispatch", "commands": {"init_db_command": "init-db"},
            },
        },
        "units": [],
    }
    bad = """\
from click.testing import CliRunner
from _portage_fastapi_test_compat import adapt_app
def runner():
    return CliRunner()
"""
    violations = framework_seam_violations(bad, plan, "tests/conftest.py")
    assert any("raw Click CliRunner" in item for item in violations)
    assert any("real exported Click commands" in item for item in violations)
