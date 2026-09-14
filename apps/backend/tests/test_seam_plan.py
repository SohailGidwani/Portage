"""R1.1: deterministic framework-seam decisions and selective coupled units."""

import ast
import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from portage_agent.agent.nodes.artifact_plan import artifact_planned_files
from portage_agent.agent.nodes.common import (
    build_manifest,
    build_migration_units,
    dependency_order,
)
from portage_agent.agent.nodes.execute import (
    _cluster_violations,
    all_generation_violations,
    framework_seam_violations,
    planned_capability_consumer_violations,
    planned_provider_import_violations,
    seam_sections,
)
from portage_agent.agent.nodes.plan import complete_unit_dependencies, drop_recipe_task
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

    assert drop_recipe_task(planned) is factory
    assert planned == [adapter, router]


def test_drop_task_skips_created_artifacts_and_drops_original_source():
    provider = PlannedFile(
        path="pkg/runtime.py", role="support", subtasks=[],
        origin="recipe", action="create",
    )
    router = _pf("pkg/views.py", "router", "blueprint_to_router")
    planned = [provider, router]

    assert drop_recipe_task(planned) is router
    assert planned == [provider]


def test_drop_task_never_removes_the_behavioral_oracle():
    router = _pf("pkg/views.py", "router", "blueprint_to_router")
    test = _pf("tests/test_views.py", "test_harness", "test_adapter_wiring")
    planned = [router, test]

    assert drop_recipe_task(planned, {"tests/test_views.py"}) is router
    assert planned == [test]


def test_drop_task_fails_closed_when_only_infrastructure_exists():
    adapter = PlannedFile(
        path="_portage_fastapi_test_compat.py", role="test_compat", subtasks=[],
        origin="infrastructure",
    )
    planned = [adapter]

    assert drop_recipe_task(planned) is None
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


def test_resource_helper_can_use_only_the_frozen_runtime_g_proxy():
    plan = {"project_modules": ["pkg", "pkg.db", "pkg.runtime_context"],
            "project_roots": ["pkg"], "decisions": {
        "resource": {
            "kind": "resource_lifecycle", "module": "pkg/db.py",
            "symbol": "get_db", "files": ["pkg/db.py"],
            "context_cache_members": ["db"],
        },
        "ambient": {
            "kind": "ambient_context_runtime", "files": ["pkg/db.py"],
            "runtime_providers": ["pkg/runtime_context.py"],
        },
    }}
    safe = "from .runtime_context import g\ndef get_db():\n    return g.db\n"
    unsafe = "from flask import g\ndef get_db():\n    return g.db\n"

    assert framework_seam_violations(safe, plan, "pkg/db.py") == []
    assert any(
        "direct resource helper reads `g`" in item
        for item in framework_seam_violations(unsafe, plan, "pkg/db.py")
    )
    wrong_member = (
        "from .runtime_context import g\n"
        "def get_db():\n    return g.config['DATABASE']\n"
    )
    assert any(
        "move ['config'] to module-owned configuration" in item
        for item in framework_seam_violations(wrong_member, plan, "pkg/db.py")
    )


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
    normalized_local = recipe.normalize_generated(
        "pkg/db.py", direct_contextvar, plan,
    )
    assert "current_app" not in normalized_local
    assert " g." not in normalized_local
    namespace = {}
    exec(normalized_local, namespace)
    namespace["init_app"](SimpleNamespace(
        state=SimpleNamespace(config={"DATABASE": ":memory:"}),
    ))
    first = namespace["get_db"]()
    assert namespace["get_db"]() is first
    namespace["close_db"]()
    assert namespace["get_db"]() is not first
    namespace["close_db"]()
    assert framework_seam_violations(
        normalized_local, plan, "pkg/db.py",
    ) == []

    plan["decisions"]["ambient"] = {
        "kind": "ambient_context_runtime",
        "files": ["pkg/db.py", "pkg/runtime.py"],
        "runtime_providers": ["pkg/runtime.py"],
        "current_app_consumers": ["pkg/db.py"],
    }
    normalized_context = recipe.normalize_generated(
        "pkg/db.py",
        direct_contextvar.replace(
            "sqlite3.connect(DATABASE,",
            "sqlite3.connect(g.config['DATABASE'],",
        ),
        plan,
    )
    assert "from pkg.runtime import current_app, g" in normalized_context
    assert "if 'db' not in g:" in normalized_context
    assert "g.db = sqlite3.connect(" in normalized_context
    assert "sqlite3.connect(current_app.config['DATABASE']" in normalized_context
    assert "check_same_thread=False" in normalized_context
    assert not any(
        "live sqlite3 connection directly in ContextVar" in item
        or "frozen `g` may cache" in item
        for item in framework_seam_violations(
            normalized_context, plan, "pkg/db.py",
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


def test_resource_row_factory_reset_keeps_source_call_frequency():
    files = {
        "pkg/db.py": (
            "import sqlite3\n"
            "from flask import current_app, g\n"
            "def get_db():\n"
            "    if 'db' not in g:\n"
            "        g.db = sqlite3.connect(current_app.config['DATABASE'])\n"
            "    g.db.row_factory = sqlite3.Row\n"
            "    return g.db\n"
            "def close_db(error=None):\n"
            "    db = g.pop('db', None)\n"
            "    if db is not None: db.close()\n"
        ),
    }
    planned = [_pf("pkg/db.py", "support", "request_context")]
    manifest = {"pkg/db.py::get_db": {
        "module": "pkg/db.py", "symbol": "get_db", "preserve_shape": True,
        "additional_exports": ["get_db_dep"],
    }}
    plan = recipe.build_seam_plan(files, planned, manifest, [])
    generated = (
        "import sqlite3\n"
        "from pkg.context import current_app, g\n"
        "def get_db():\n"
        "    if 'db' not in g:\n"
        "        g.db = sqlite3.connect(current_app.config['DATABASE'])\n"
        "        g.db.row_factory = sqlite3.Row\n"
        "    return g.db\n"
        "def close_db(error=None): g.pop('db', None)\n"
    )

    normalized = recipe.normalize_generated("pkg/db.py", generated, plan)
    helper = next(
        node for node in ast.parse(normalized).body
        if isinstance(node, ast.FunctionDef) and node.name == "get_db"
    )

    assert isinstance(helper.body[-3], ast.If)
    assert ast.unparse(helper.body[-2]).endswith(".row_factory = sqlite3.Row")
    assert isinstance(helper.body[-1], ast.Return)


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

    indirect = good.replace(
        "def create_app():",
        "def create_app():\n    base_dir = Path(__file__).resolve().parent",
    ).replace(
        "Path(__file__).parent / 'static'", "str(base_dir / 'static')",
    )
    normalized = recipe.normalize_generated("pkg/__init__.py", indirect, plan)
    assert "directory=Path(__file__).resolve().parent / 'static'" in normalized
    assert "check_dir=False" in normalized
    assert framework_seam_violations(
        normalized, plan, "pkg/__init__.py",
    ) == []

    false_package_path = good.replace(
        "StaticFiles(directory=Path(__file__).parent / 'static')",
        "StaticFiles(directory='static')",
    ).replace(
        "from fastapi.staticfiles import StaticFiles",
        "from fastapi.staticfiles import StaticFiles\n"
        "from fastapi.templating import Jinja2Templates\n"
        "templates = Jinja2Templates(directory='templates')",
    ).replace(
        "async def index():", "openapi = Path(__file__).parent / 'static'\nasync def index():",
    )
    assert any(
        "mounting StaticFiles" in item
        for item in framework_seam_violations(
            false_package_path, plan, "pkg/__init__.py",
        )
    )
    realized_static = recipe.normalize_generated(
        "pkg/__init__.py", false_package_path, plan,
    )
    assert "directory=Path(__file__).resolve().parent / 'static'" in realized_static
    assert "check_dir=False" in realized_static
    assert "def _portage_url_for(context, name, /, **path_params):" in realized_static
    assert "path_params.pop('filename')" in realized_static
    assert framework_seam_violations(
        realized_static, plan, "pkg/__init__.py",
    ) == []

    already_relative = good.replace(
        "from fastapi.staticfiles import StaticFiles",
        "from fastapi.staticfiles import StaticFiles\n"
        "from fastapi.templating import Jinja2Templates\n"
        "templates = Jinja2Templates(directory=Path(__file__).parent / 'templates')",
    )
    realized_relative = recipe.normalize_generated(
        "pkg/__init__.py", already_relative, plan,
    )
    assert "def _portage_url_for(context, name, /, **path_params):" in realized_relative

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
    realized = recipe.normalize_generated(
        "pkg/__init__.py", bare_lifespan, plan,
    )
    assert "@asynccontextmanager\nasync def lifespan(app):" in realized
    assert framework_seam_violations(realized, plan, "pkg/__init__.py") == []


def test_default_flask_static_endpoint_is_realized_from_python_sources_only():
    files = {
        "pkg/__init__.py": (
            "from flask import Flask\n"
            "def create_app(): return Flask(__name__)\n"
        ),
        "pkg/views.py": (
            "from flask import Blueprint, render_template\n"
            "bp = Blueprint('page', __name__)\n"
            "@bp.route('/')\n"
            "def index(): return render_template('index.html')\n"
        ),
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = (
        "from fastapi import FastAPI\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    return app\n"
    )

    normalized = recipe.normalize_generated("pkg/__init__.py", generated, plan)

    assert "app.mount('/static'" in normalized
    assert "check_dir=False" in normalized
    assert "Path(__file__).resolve().parent / 'static'" in normalized


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
        {
            "function": "register", "receiver": "bp", "path": "/register",
            "name": "auth.register", "prefix": "/auth",
        },
        {
            "function": "logout", "receiver": "bp", "path": "/logout",
            "name": "auth.sign_out", "prefix": "/auth",
        },
    ]
    generated = """\
from fastapi import APIRouter
bp = APIRouter()
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
    assert any(
        "preserve source URL prefix '/auth'" in item
        for item in framework_seam_violations(generated, plan, "pkg/auth.py")
    )

    realized = recipe.normalize_generated("pkg/auth.py", generated, plan)

    assert realized.count("name='auth.register'") == 2
    assert "name='auth.sign_out'" in realized
    assert "APIRouter(prefix='/auth')" in realized
    assert framework_seam_violations(realized, plan, "pkg/auth.py") == []

    renamed = recipe.normalize_generated("pkg/auth.py", """\
from fastapi import APIRouter
bp = APIRouter()
@bp.get('/register')
async def show_register(): pass
@bp.get('/logout')
async def sign_out(): pass
""", plan)
    assert "name='auth.register'" in renamed
    assert "name='auth.sign_out'" in renamed
    assert "APIRouter(prefix='/auth')" in renamed
    assert framework_seam_violations(renamed, plan, "pkg/auth.py") == []

    reshaped = recipe.normalize_generated("pkg/auth.py", """\
from fastapi import APIRouter
router = APIRouter()
@router.post('/register/')
async def create_user(): pass
@router.get('/logout/')
async def sign_out_user(): pass
""", plan)
    assert "name='auth.register'" in reshaped
    assert "name='auth.sign_out'" in reshaped
    assert "APIRouter(prefix='/auth')" in reshaped
    assert framework_seam_violations(reshaped, plan, "pkg/auth.py") == []

    imperative = """\
from fastapi import APIRouter
bp = APIRouter()
async def register(): pass
async def logout(): pass
bp.add_api_route('/register', register, methods=['GET', 'POST'])
bp.add_api_route('/logout', logout, name='wrong')
"""
    imperative = recipe.normalize_generated("pkg/auth.py", imperative, plan)
    assert "name='auth.register'" in imperative
    assert "name='auth.sign_out'" in imperative
    assert "APIRouter(prefix='/auth')" in imperative
    assert framework_seam_violations(imperative, plan, "pkg/auth.py") == []


def test_blueprint_method_shortcut_preserves_reverse_url_name():
    files = {"pkg/auth.py": (
        "from flask import Blueprint\n"
        "bp = Blueprint('auth', __name__)\n"
        "@bp.post('/logout')\n"
        "def logout(): pass\n"
    )}
    plan = recipe.build_seam_plan(
        files, [_pf("pkg/auth.py", "router", "route_to_endpoint")], {}, [],
    )
    generated = (
        "from fastapi import APIRouter\n"
        "bp = APIRouter()\n"
        "@bp.post('/logout')\n"
        "async def logout(): pass\n"
    )

    normalized = recipe.normalize_generated("pkg/auth.py", generated, plan)

    assert "name='auth.logout'" in normalized
    assert framework_seam_violations(normalized, plan, "pkg/auth.py") == []


def test_route_contract_matches_renamed_path_parameters():
    plan = {"decisions": {"routes": {
        "kind": "route_names", "path": "pkg/blog.py", "files": ["pkg/blog.py"],
        "routes": [{
            "function": "update", "receiver": "bp", "path": "/{id}/update",
            "name": "blog.update", "prefix": "",
        }],
    }}}
    generated = """\
from fastapi import APIRouter
router = APIRouter()
@router.post('/{post_id}/update/')
async def save_post(post_id: int): pass
"""

    realized = recipe.normalize_generated("pkg/blog.py", generated, plan)

    assert "name='blog.update'" in realized
    assert framework_seam_violations(realized, plan, "pkg/blog.py") == []


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


def test_url_for_is_coerced_before_python_string_trimming():
    generated = """\
async def openapi(request):
    return request.url_for('home').rstrip('/')
"""

    realized = recipe.normalize_generated("pkg/views.py", generated, {})

    assert "str(request.url_for('home')).rstrip('/')" in realized


def test_template_response_is_not_wrapped_in_another_response():
    generated = """\
from fastapi import Request, Response
from fastapi.templating import Jinja2Templates
templates = Jinja2Templates(directory='templates')
def render_page(request: Request, values):
    return templates.TemplateResponse(request, 'page.html', values)
def route(request: Request):
    return Response(render_page(request, {}), media_type='text/html')
"""

    realized = recipe.normalize_generated("pkg/views.py", generated, {})

    assert "return render_page(request, {})" in realized
    assert "Response(render_page" not in realized


def test_template_helper_receives_request_from_its_route_caller():
    generated = """\
from fastapi import Request
from fastapi.templating import Jinja2Templates
templates = Jinja2Templates(directory='templates')
def render_page(values):
    return templates.TemplateResponse('page.html', {'values': values})
async def route(request: Request):
    return render_page({})
"""

    realized = recipe.normalize_generated("pkg/views.py", generated, {})

    assert "def render_page(values, request: Request)" in realized
    assert "templates.TemplateResponse(request, 'page.html'" in realized
    assert "return render_page({}, request)" in realized


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


def test_error_handler_envelopes_own_route_exceptions_and_tests_do_not_unwrap():
    files = {
        "pkg/api.py": (
            "from flask import Blueprint, jsonify\n"
            "from . import store\n"
            "bp = Blueprint('api', __name__)\n"
            "@bp.route('/items/<int:item_id>')\n"
            "def get_item(item_id):\n"
            "    return jsonify(store.get_item(item_id))\n"
        ),
        "pkg/app.py": (
            "from flask import Flask, jsonify\n"
            "from .api import bp\n"
            "from .store import ItemNotFound\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    app.register_blueprint(bp)\n"
            "    @app.errorhandler(ItemNotFound)\n"
            "    def missing(exc):\n"
            "        return jsonify({'error': str(exc)}), 404\n"
            "    return app\n"
        ),
        "tests/conftest.py": "def body(resp):\n    return resp.get_json()\n",
    }
    planned = [
        _pf("pkg/api.py", "router", "route_to_endpoint"),
        _pf("pkg/app.py", "app_factory", "app_factory", "error_handler"),
        _pf("tests/conftest.py", "test_harness", "test_harness"),
    ]
    units = [{
        "id": "fixture-seam", "paths": list(files), "reason": "factory/router/test seam",
    }]
    plan = recipe.build_seam_plan(files, planned, {}, units)
    decision = plan["decisions"]["error_handler_ownership:pkg/app.py"]
    assert decision["route_functions"] == {"pkg/api.py": ["get_item"]}
    assert decision["handlers"] == [{
        "exception": "ItemNotFound",
        "exception_name": "ItemNotFound",
        "function": "missing",
        "status_code": 404,
        "json_keys": ["error"],
    }]

    generated_router = (
        "from fastapi import APIRouter, HTTPException\n"
        "from . import store\n"
        "bp = APIRouter()\n"
        "@bp.get('/items/{item_id}')\n"
        "def get_item(item_id: int):\n"
        "    try:\n"
        "        return store.get_item(item_id)\n"
        "    except store.ItemNotFound as exc:\n"
        "        raise HTTPException(status_code=404, detail=str(exc))\n"
    )
    realized = recipe.normalize_generated("pkg/api.py", generated_router, plan)
    assert "except store.ItemNotFound" not in realized
    assert "_portage_flask_response(store.get_item(item_id))" in realized
    assert not any(
        "app-owned exceptions" in item
        for item in framework_seam_violations(realized, plan, "pkg/api.py")
    )

    good_factory = (
        "from fastapi import FastAPI\n"
        "from fastapi.responses import JSONResponse\n"
        "from .store import ItemNotFound\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    @app.exception_handler(ItemNotFound)\n"
        "    async def missing(request, exc):\n"
        "        return JSONResponse(status_code=404, content={'error': str(exc)})\n"
        "    return app\n"
    )
    assert not any(
        "top-level JSON keys" in item
        for item in framework_seam_violations(good_factory, plan, "pkg/app.py")
    )
    bad_factory = good_factory.replace("{'error': str(exc)}", "{'detail': str(exc)}")
    assert any(
        "top-level JSON keys ['error']" in item
        for item in framework_seam_violations(bad_factory, plan, "pkg/app.py")
    )

    direct = "def body(resp):\n    return resp.json()\n"
    assert framework_seam_violations(direct, plan, "tests/conftest.py") == []
    unwrapped = (
        "def body(resp):\n"
        "    data = resp.json()\n"
        "    return data.get('detail', data)\n"
    )
    assert any(
        "application error envelopes cannot be unwrapped" in item
        for item in framework_seam_violations(unwrapped, plan, "tests/conftest.py")
    )


def test_exception_handler_response_tuple_keeps_its_source_status():
    source = (
        "def register(app):\n"
        "    @app.exception_handler(404)\n"
        "    async def missing(request, exc):\n"
        "        return render_template(request, '404.html'), 404\n"
    )
    normalized = recipe.normalize_generated("pkg/errors.py", source, {})

    assert "_portage_response.status_code = 404" in normalized
    assert "return _portage_response" in normalized
    assert "return _portage_response" in normalized
    assert framework_seam_violations(
        normalized,
        {"decisions": {}, "project_modules": ["pkg.errors"]},
        "pkg/errors.py",
    ) == []


def test_exception_handler_render_keyword_becomes_a_real_response_status():
    source = (
        "def register(app):\n"
        "    @app.exception_handler(404)\n"
        "    async def missing(request, exc):\n"
        "        return render_template(request, '404.html', status_code=404)\n"
    )

    normalized = recipe.normalize_generated("pkg/errors.py", source, {})

    assert "_portage_response.status_code = 404" in normalized


def test_route_template_status_normalization_is_idempotent():
    plan = {"decisions": {"statuses": {
        "kind": "route_response_statuses", "path": "pkg/views.py",
        "files": ["pkg/views.py"],
        "routes": [{
            "function": "view", "handlers": [],
            "returns": [{"literals": ["missing.html"], "status_code": 404}],
        }],
    }}}
    generated = (
        "from fastapi import APIRouter, Request\n"
        "from pkg.templating import render_template\n"
        "bp = APIRouter()\n"
        "@bp.get('/missing')\n"
        "def view(request: Request):\n"
        "    return render_template(request, 'missing.html')\n"
    )

    normalized = recipe.normalize_generated("pkg/views.py", generated, plan)
    normalized = recipe.normalize_generated("pkg/views.py", normalized, plan)

    assert "JSONResponse(content=" not in normalized
    assert "render_template(request, 'missing.html', status_code=404)" in normalized


def test_exception_template_helper_status_normalization_is_idempotent():
    plan = {"decisions": {"statuses": {
        "kind": "route_response_statuses", "path": "pkg/views.py",
        "files": ["pkg/views.py"],
        "routes": [{
            "function": "search",
            "handlers": [{"exceptions": ["SearchError"], "status_code": 400}],
            "returns": [],
        }],
    }}}
    generated = (
        "from pkg.templating import render_template\n"
        "def search(request):\n"
        "    def render_error(error):\n"
        "        return render_template(request, 'search.html', error=error)\n"
        "    try:\n"
        "        return find()\n"
        "    except SearchError as error:\n"
        "        return render_error(error)\n"
    )

    normalized = recipe.normalize_generated("pkg/views.py", generated, plan)
    normalized = recipe.normalize_generated("pkg/views.py", normalized, plan)

    assert "JSONResponse(content=_portage_response" not in normalized
    assert "status_code=400" in normalized

    already_realized = generated.replace(
        "        return render_error(error)\n",
        "        response = render_error(error)\n"
        "        response.status_code = 400\n"
        "        return _portage_flask_response(response)\n",
    )
    normalized = recipe.normalize_generated("pkg/views.py", already_realized, plan)
    assert "JSONResponse(content=response" not in normalized


def test_mapping_update_cannot_read_a_key_written_earlier_in_the_same_call():
    plan = {"decisions": {}, "project_modules": ["pkg.views"]}
    collapsed = (
        "def view(state):\n"
        "    state.update({'items': load(), 'count': len(state['items'])})\n"
    )
    sequential = (
        "def view(state):\n"
        "    state['items'] = load()\n"
        "    state['count'] = len(state['items'])\n"
    )

    assert any(
        "preserve the source's sequential assignments" in item
        for item in framework_seam_violations(collapsed, plan, "pkg/views.py")
    )
    assert framework_seam_violations(sequential, plan, "pkg/views.py") == []


def test_route_response_statuses_survive_dict_translation():
    files = {"pkg/views.py": """\
from http import HTTPStatus
from flask import Blueprint, jsonify
bp = Blueprint('items', __name__)
@bp.post('/items')
def create():
    try:
        save()
    except Duplicate:
        return jsonify({'error': 'already exists'}), HTTPStatus.CONFLICT
    if invalid():
        return jsonify({'error': 'invalid item'}), HTTPStatus.BAD_REQUEST
    return jsonify({'id': 1}), HTTPStatus.CREATED
"""}
    planned = [_pf("pkg/views.py", "router", "route_to_endpoint")]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = """\
from fastapi import APIRouter
bp = APIRouter()
@bp.post('/items', status_code=201)
async def create():
    try:
        save()
    except Duplicate:
        return {'error': 'already exists'}
    if invalid():
        return {'error': 'invalid item'}
    return {'id': 1}
"""

    normalized = recipe.normalize_generated("pkg/views.py", generated, plan)

    assert "JSONResponse(content={'error': 'already exists'}, status_code=409)" in normalized
    assert "JSONResponse(content={'error': 'invalid item'}, status_code=400)" in normalized


def test_http_exception_dependency_preserves_source_json_error_envelope():
    files = {"pkg/app.py": """\
from flask import Flask, jsonify
from http import HTTPStatus
def create_app():
    app = Flask(__name__)
    @app.get('/items')
    def items():
        try:
            return load_items()
        except ValueError as exc:
            return jsonify({'error': str(exc)}), HTTPStatus.BAD_REQUEST
    return app
"""}
    planned = [_pf("pkg/app.py", "app_factory", "app_factory")]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = """\
from http import HTTPStatus
from fastapi import Depends, FastAPI, Request
def read_page(request: Request):
    if request.query_params.get('page') == '0':
        raise ValueError('invalid page')
    return 1
def create_app():
    app = FastAPI()
    @app.get('/items')
    async def items(page: int = Depends(read_page)):
        return []
    return app
"""

    realized = recipe.normalize_generated("pkg/app.py", generated, plan)

    assert "raise _PortageEnvelopeHTTPException(" in realized
    assert "status_code=400" in realized
    assert "@app.exception_handler(_PortageEnvelopeHTTPException)" in realized
    assert "content={key: exc.detail}" in realized


def test_ambient_context_provider_retains_the_active_request():
    plan = {"decisions": {"ambient_context_runtime": {
        "kind": "ambient_context_runtime",
        "runtime_providers": ["pkg/context.py"],
        "runtime_classes": {"pkg/context.py": ["RequestContextMiddleware"]},
    }}}
    source = (
        "from contextvars import ContextVar\n"
        "from starlette.middleware.base import BaseHTTPMiddleware\n"
        "_context = ContextVar('context', default={})\n"
        "class RequestContextMiddleware(BaseHTTPMiddleware):\n"
        "    async def dispatch(self, request, call_next):\n"
        "        token = _context.set({'session': request.session})\n"
        "        return await call_next(request)\n"
    )

    normalized = recipe.normalize_generated("pkg/context.py", source, plan)

    assert "'request': request" in normalized
    manifest = {"pkg/context.py::RequestContextMiddleware": {
        "module": "pkg/context.py", "symbol": "RequestContextMiddleware",
        "target_kind": "class",
    }}
    assert not any(
        "retain the active request" in violation
        for violation in framework_seam_violations(
            normalized, plan, "pkg/context.py", manifest,
        )
    )


def test_split_blueprint_handlers_are_planned_and_registered_on_the_app():
    files = {
        "pkg/api/__init__.py": (
            "from flask import Blueprint\n"
            "bp = Blueprint('api', __name__)\n"
            "from pkg.api import errors\n"
        ),
        "pkg/api/errors.py": (
            "from werkzeug.exceptions import HTTPException\n"
            "from pkg.api import bp\n"
            "def error_response(status_code):\n"
            "    payload = {'error': 'failed'}\n"
            "    return payload, status_code\n"
            "@bp.errorhandler(HTTPException)\n"
            "def handle_exception(exc):\n"
            "    return error_response(exc.code)\n"
            "@bp.app_errorhandler(404)\n"
            "def not_found(exc):\n"
            "    return error_response(404)\n"
        ),
        "pkg/app.py": (
            "from flask import Flask\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    from pkg.api import bp as api_bp\n"
            "    app.register_blueprint(api_bp)\n"
            "    return app\n"
        ),
    }
    planned = recipe.plan_files(files)
    handler = next(item for item in planned if item.path == "pkg/api/errors.py")
    assert handler.role == "support"
    assert [task.type for task in handler.subtasks] == ["error_handler"]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    decision = plan["decisions"]["blueprint_error_handlers:pkg/api/errors.py"]
    assert decision["factory_files"] == ["pkg/app.py"]
    assert [item["scope"] for item in decision["handlers"]] == [
        "errorhandler", "app_errorhandler",
    ]
    assert decision["handlers"][0]["registration"] == {
        "kind": "exception",
        "module": "werkzeug.exceptions",
        "symbol": "HTTPException",
        "source": "HTTPException",
    }
    assert decision["handlers"][1]["registration"] == {
        "kind": "status", "value": 404,
    }

    generated_handler = files["pkg/api/errors.py"].replace(
        "@bp.app_errorhandler(404)", "@bp.get('/fake-404')",
    ).replace("def handle_exception(exc):", "def handle_exception(exc, request):")
    realized_handler = recipe.normalize_generated(
        "pkg/api/errors.py", generated_handler, plan,
    )
    assert ".errorhandler(" not in realized_handler
    assert "@bp.get" not in realized_handler
    assert "def handle_exception(request, exc):" in realized_handler
    assert "def not_found(request, exc):" in realized_handler
    assert not any(
        "blueprint handler" in item or "response helper" in item
        for item in framework_seam_violations(
            realized_handler, plan, "pkg/api/errors.py",
        )
    )

    generated_factory = (
        "from fastapi import FastAPI\n"
        "from pkg.api.errors import handle_exception, not_found\n"
        "from pkg.api import bp as api_bp\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    app.add_exception_handler(Exception, handle_exception)\n"
        "    app.add_exception_handler(404, not_found)\n"
        "    app.include_router(api_bp)\n"
        "    return app\n"
    )
    realized_factory = recipe.normalize_generated("pkg/app.py", generated_factory, plan)
    assert realized_factory.count("app.add_exception_handler(") == 2
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module in {"pkg.api", "pkg.api.errors"}
        for node in ast.parse(realized_factory).body
    )
    assert "from werkzeug.exceptions import HTTPException as _portage_exception_0" in (
        realized_factory
    )
    assert not any(
        "application factory must register source handler" in item
        for item in framework_seam_violations(realized_factory, plan, "pkg/app.py")
    )

    changed_body = realized_handler.replace("return error_response(404)", "return {}")
    assert any(
        "omits source response helpers" in item
        for item in framework_seam_violations(changed_body, plan, "pkg/api/errors.py")
    )


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
    async def inner(request, **kwargs):
        return await view(request, **kwargs)
    return inner
"""
    assert any(
        "must preserve the wrapped endpoint signature" in item
        for item in framework_seam_violations(generated, plan, "pkg/auth.py")
    )

    realized = recipe.normalize_generated("pkg/auth.py", generated, plan)

    assert "import functools" in realized
    assert "@functools.wraps(view)" in realized
    assert "async def inner(**kwargs)" in realized
    assert "view(**kwargs)" in realized
    assert framework_seam_violations(realized, plan, "pkg/auth.py") == []


def test_decorator_factory_hides_only_its_injected_view_parameter():
    files = {"pkg/auth.py": """\
from functools import wraps
def require_role(admin=False):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            return view(object(), *args, **kwargs)
        return wrapped
    return decorator
"""}
    planned = [_pf("pkg/auth.py", "router", "route_to_endpoint")]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    assert plan["decisions"]["view_decorators:pkg/auth.py"]["decorators"] == [{
        "function": "require_role", "container": "decorator",
        "parameter": "view", "wrapper": "wrapped",
    }]
    generated = """\
from functools import wraps
def require_role(admin=False):
    def decorator(view):
        @wraps(view)
        async def wrapped(request, *args, **kwargs):
            return await view(object(), request, *args, **kwargs)
        return wrapped
    return decorator
@require_role()
async def endpoint(user, request, item_id):
    return item_id
"""

    realized = recipe.normalize_generated("pkg/auth.py", generated, plan)

    assert "wrapped.__signature__ = inspect.signature(view).replace" in realized
    namespace = {}
    exec(realized, namespace)
    assert list(__import__("inspect").signature(namespace["endpoint"]).parameters) == [
        "request", "item_id",
    ]


def test_factory_nested_decorator_exposes_request_before_body_dependency():
    files = {"pkg/app.py": """\
from functools import wraps
def create_app():
    def require_auth():
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                return view(object(), *args, **kwargs)
            return wrapped
        return decorator
"""}
    planned = [_pf("pkg/app.py", "app_factory", "app_factory")]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = """\
from functools import wraps
from fastapi import Depends, Request
def create_app():
    def require_auth():
        def decorator(view):
            @wraps(view)
            async def wrapped(request: Request, *args, **kwargs):
                return await view(object(), *args, **kwargs)
            return wrapped
        return decorator
    @require_auth()
    async def endpoint(user, payload: dict = Depends(read_payload)):
        return payload
"""

    realized = recipe.normalize_generated("pkg/app.py", generated, plan)

    assert "inspect.Parameter('request'" in realized
    assert "annotation=Request" in realized
    assert framework_seam_violations(realized, plan, "pkg/app.py") == []


def test_request_body_dependency_is_async_and_awaited():
    generated = """\
from fastapi import Depends, Request
def read_payload(request: Request) -> dict:
    return request.json() or {}
async def endpoint(payload: dict = Depends(read_payload)):
    return payload
"""

    realized = recipe.normalize_generated("pkg/views.py", generated, {})

    assert "async def read_payload(request: Request)" in realized
    assert "return await request.json() or {}" in realized


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
    app = Flask(__name__, static_folder=None)
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
        good + "\ndef render(request):\n    return vars(request.state)\n",
        plan, "pkg/__init__.py", manifest,
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
    branch_config = """\
from .testing import AppFacade
def create_app(test_config=None):
    if test_config:
        config = {'SECRET_KEY': 'test', 'DATABASE': 'test.sqlite'}
    else:
        config = {'SECRET_KEY': 'dev', 'DATABASE': 'default.sqlite'}
    app = AppFacade(testing=bool(test_config))
    return app
"""
    normalized_branch = recipe.normalize_generated(
        "pkg/__init__.py", branch_config, plan,
    )
    assert "dict(config) if hasattr(config, 'items')" in normalized_branch
    assert "app.state.config.update(test_config)" in normalized_branch
    assert framework_seam_violations(
        normalized_branch, plan, "pkg/__init__.py", manifest,
    ) == []

    override_only_files = {
        "pkg/__init__.py": (
            "from flask import Flask\n"
            "def create_app(test_config=None):\n"
            "    app = Flask(__name__)\n"
            "    if test_config: app.config.update(test_config)\n"
            "    return app\n"
        ),
    }
    override_plan = recipe.build_seam_plan(
        override_only_files, recipe.plan_files(override_only_files), {}, [],
    )
    override_only = recipe.normalize_generated(
        "pkg/__init__.py",
        "from fastapi import FastAPI\n"
        "def create_app(test_config=None):\n"
        "    app = FastAPI()\n"
        "    app.state.config = {}\n"
        "    return app\n",
        override_plan,
    )
    assert "app.state.config.update(test_config)" in override_only
    assert framework_seam_violations(
        override_only, override_plan, "pkg/__init__.py",
    ) == []
    missing_initializer = local_config.replace("    db.init_app(app)\n", "")
    normalized = recipe.normalize_generated(
        "pkg/__init__.py", missing_initializer, plan,
    )
    assert normalized.index("app.state.config = config") < normalized.index(
        "db.init_app(app)"
    ) < normalized.index("return app")
    assert framework_seam_violations(
        normalized, plan, "pkg/__init__.py", manifest,
    ) == []
    wrong_owner = missing_initializer.replace(
        "    return app\n", "    app.init_app()\n    return app\n",
    )
    normalized_wrong_owner = recipe.normalize_generated(
        "pkg/__init__.py", wrong_owner, plan,
    )
    assert "db.init_app(app)" in normalized_wrong_owner
    direct_import = missing_initializer.replace(
        "from . import db", "from .db import init_app",
    )
    normalized_direct_import = recipe.normalize_generated(
        "pkg/__init__.py", direct_import, plan,
    )
    assert "init_app(app)" in normalized_direct_import
    wrapped_return = missing_initializer.replace(
        "def create_app(test_config=None):\n",
        'def create_app(test_config=None):\n    """factory"""\n',
    ).replace("return app", "return AppFacade(app)")
    normalized_wrapped = recipe.normalize_generated(
        "pkg/__init__.py", wrapped_return, plan,
    )
    assert "db.init_app(app)" in normalized_wrapped
    wrapped_tree = ast.parse(normalized_wrapped)
    wrapped_factory = next(
        node for node in wrapped_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )
    assert ast.get_docstring(wrapped_factory) == "factory"
    wiring_only = """\
from fastapi import FastAPI
from . import db
from .testing import AppFacade
def create_app(test_config=None):
    app = FastAPI()
    shadow = FastAPI()
    app.add_middleware(object)
    shadow.add_middleware(object)
    return AppFacade(app)
"""
    normalized_wiring_only = recipe.normalize_generated(
        "pkg/__init__.py", wiring_only, plan,
    )
    assert normalized_wiring_only.index("app = FastAPI()") < (
        normalized_wiring_only.index("db.init_app(app)")
    )
    assert "db.init_app(app)" in normalized_wiring_only
    assert normalized_wiring_only.index("db.init_app(app)") < (
        normalized_wiring_only.index("return AppFacade(app)")
    )
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
    app = Flask(__name__, static_folder=None)
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


def test_imported_template_provider_uses_current_response_api_without_json_wrapper():
    plan = {"decisions": {
        "provider": {
            "kind": "planned_provider_exports", "provider": "pkg/templating.py",
            "exports": {
                "render_template": {"kind": "function", "members": []},
                "templates": {"kind": "variable", "members": []},
            },
            "files": ["pkg/templating.py", "pkg/views.py"],
            "instruction": "frozen template provider",
        },
        "runtime": {
            "kind": "template_runtime", "provider_files": ["pkg/templating.py"],
            "provider_functions": {"pkg/templating.py": ["render_template"]},
            "consumer_functions": {"pkg/views.py": ["render_template"]},
            "files": ["pkg/templating.py", "pkg/views.py"],
            "instruction": "current request-first API",
        },
    }}
    generated = (
        "from fastapi import APIRouter, Request\n"
        "from fastapi.responses import JSONResponse\n"
        "from pkg.templating import render_template, templates\n"
        "bp = APIRouter()\n"
        "@bp.get('/')\n"
        "async def view(request: Request):\n"
        "    return JSONResponse(content=templates.TemplateResponse(\n"
        "        'missing.html', {'request': request}, status_code=404\n"
        "    ), status_code=404)\n"
    )

    normalized = recipe.normalize_generated("pkg/views.py", generated, plan)

    assert "render_template(request, 'missing.html', status_code=404)" in normalized
    assert "JSONResponse" not in normalized

    nested_helper = generated.replace(
        "    return JSONResponse(content=templates.TemplateResponse(\n"
        "        'missing.html', {'request': request}, status_code=404\n"
        "    ), status_code=404)\n",
        "    def render_error():\n"
        "        return render_template(request, 'missing.html', status_code=404)\n"
        "    return JSONResponse(content=render_error(), status_code=404)\n",
    )
    normalized = recipe.normalize_generated("pkg/views.py", nested_helper, plan)
    assert "_portage_flask_response(render_error())" in normalized
    assert "JSONResponse" not in normalized


def test_computed_string_route_keeps_flask_html_response_bytes():
    generated = """\
from __future__ import annotations
from fastapi import APIRouter
bp = APIRouter()
def render_value(): return '<p>value</p>'
@bp.post('/render')
async def render():
    return render_value()
"""

    normalized = recipe.normalize_generated("pkg/views.py", generated, {})
    namespace = {}
    exec(normalized, namespace)
    response = asyncio.run(namespace["render"]())

    assert normalized.splitlines()[0] == "from __future__ import annotations"
    assert response.body == b"<p>value</p>"
    assert response.media_type == "text/html"


def test_module_member_import_is_aliased_when_route_has_the_same_name():
    files = {
        "pkg/store.py": "def history(): return []\n",
        "pkg/views.py": (
            "from flask import Blueprint\n"
            "from . import store\n"
            "bp = Blueprint('page', __name__)\n"
            "@bp.route('/history')\n"
            "def history(): return store.history()\n"
        ),
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = (
        "from fastapi import APIRouter\n"
        "from pkg.store import history\n"
        "bp = APIRouter()\n"
        "@bp.get('/history')\n"
        "def history(): return history()\n"
    )

    normalized = recipe.normalize_generated("pkg/views.py", generated, plan)

    assert "from pkg.store import history as _portage_history" in normalized
    assert "_portage_flask_response(_portage_history())" in normalized


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
    assert "str(Path(__file__).resolve().parent / 'templates')" in normalized


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
        "source": (
            "def load_logged_in_user():\n"
            "    g.user = session.get('user_id')"
        ),
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

    normalized = recipe.normalize_generated(
        "pkg/auth.py",
        good.replace("load_logged_in_user", "load_logged_in_user_dependency"),
        plan,
    )
    assert "def load_logged_in_user(" in normalized
    assert "Depends(load_logged_in_user)" in normalized
    assert "load_logged_in_user_dependency" not in normalized

    restored = recipe.normalize_generated(
        "pkg/auth.py",
        "from fastapi import APIRouter, Depends\n"
        "from pkg.runtime import g, session\n"
        "bp = APIRouter(dependencies=[Depends(load_logged_in_user)])\n",
        plan,
    )
    assert "def load_logged_in_user():" in restored
    assert "g.user = session.get('user_id')" in restored
    assert not any(
        "pre-request hook" in item
        for item in framework_seam_violations(restored, plan, "pkg/auth.py")
    )
    assert framework_seam_violations(normalized, plan, "pkg/auth.py") == []


def test_only_mutated_fetchone_rows_are_materialized():
    generated = (
        "def load(connection):\n"
        "    record = connection.execute('select 1 as id').fetchone()\n"
        "    record['ready'] = True\n"
        "    return record\n"
    )

    normalized = recipe.normalize_generated("pkg/store.py", generated)

    assert "record = _portage_mutable_row(" in normalized
    namespace = {}
    exec(normalized, namespace)
    import sqlite3
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    assert namespace["load"](connection) == {"id": 1, "ready": True}


def test_resource_consumers_keep_yield_dependencies_inside_depends():
    plan = {
        "decisions": {"resource": {
            "kind": "resource_lifecycle", "module": "pkg/store.py",
            "symbol": "open_store", "dependency": "open_store_dep",
            "files": ["pkg/store.py", "pkg/views.py"],
        }},
    }
    generated = """\
from fastapi import Depends
from .store import open_store_dep

async def first():
    return await open_store_dep()

def second():
    return next(open_store_dep())

def endpoint(store=Depends(open_store_dep())):
    return store
"""

    normalized = recipe.normalize_generated("pkg/views.py", generated, plan)

    assert "return open_store()" in normalized
    assert "next(open_store_dep())" not in normalized
    assert "Depends(open_store_dep)" in normalized
    assert not any(
        "must be passed to Depends without calling it" in item
        for item in framework_seam_violations(normalized, plan, "pkg/views.py")
    )


def test_template_consumers_pass_the_endpoint_request_to_the_provider():
    plan = {
        "decisions": {"templates": {
            "kind": "template_runtime",
            "files": ["pkg/rendering.py", "pkg/views.py"],
            "provider_files": ["pkg/rendering.py"],
            "provider_functions": {"pkg/rendering.py": ["render_template"]},
        }},
    }
    generated = """\
from starlette.requests import Request
from .rendering import render_template as render, templates

async def page(request: Request):
    return render('page.html', value=1)
"""

    normalized = recipe.normalize_generated("pkg/views.py", generated, plan)

    assert "render(request, 'page.html', value=1)" in normalized
    assert "templates" not in normalized

    frozen = {"decisions": {"templates": {
        **plan["decisions"]["templates"],
        "consumer_functions": {
            "pkg/views.py": ["render_template", "url_for"],
        },
        "provider_functions": {
            "pkg/rendering.py": ["render_template", "url_for"],
        },
    }}}
    miswired = """\
from fastapi.templating import Jinja2Templates
from pkg.runtime import url_for
templates = Jinja2Templates(directory='templates')
def render(request, name, **context):
    return templates.TemplateResponse(request, name, context)
def page(request):
    return render(request, 'page.html'), url_for('home')
"""
    normalized = recipe.normalize_generated("pkg/views.py", miswired, frozen)
    assert "from pkg.rendering import render_template, url_for" in normalized
    assert "from pkg.runtime import url_for" not in normalized
    assert "def render(" not in normalized
    assert "render_template(request, 'page.html')" in normalized

    missing_request = """\
from fastapi import APIRouter, Depends
from .rendering import render_template as render
router = APIRouter()
@router.get('/')
async def page(store=Depends(object)):
    return render('page.html', value=1)
"""
    normalized = recipe.normalize_generated("pkg/views.py", missing_request, plan)
    assert "async def page(request: Request, store=Depends(object))" in normalized
    assert "render(request, 'page.html', value=1)" in normalized

    ambient = {"decisions": {
        "templates": {
            **plan["decisions"]["templates"],
            "files": ["pkg/rendering.py", "pkg/email.py"],
            "consumer_functions": {"pkg/email.py": ["render_template"]},
        },
        "context": {
            "kind": "ambient_context_runtime",
            "files": ["pkg/email.py", "pkg/runtime.py"],
            "runtime_providers": ["pkg/runtime.py"],
        },
    }}
    helper = recipe.normalize_generated(
        "pkg/email.py",
        "from fastapi import Request\n"
        "from pkg.runtime import g\n"
        "from pkg.rendering import render_template\n"
        "def render_email(): return render_template(Request, 'message.html')\n"
        "def duplicate(): return render_template("
        "g.request, g.request, Request, Request(), 'dupe.html')\n"
        "def message(): return Message(body=render_template('message.txt'))\n",
        ambient,
    )
    assert "from pkg.runtime import g" in helper
    assert "render_template(g.request, 'message.html')" in helper
    assert "render_template(g.request, 'dupe.html')" in helper
    assert "render_template(g.request, 'message.txt').body.decode()" in helper


def test_template_auth_global_is_loaded_at_render_time_to_avoid_provider_cycles():
    plan = {"decisions": {"templates": {
        "kind": "template_runtime",
        "files": ["pkg/templating.py"],
        "provider_files": ["pkg/templating.py"],
        "context_globals": ["current_user"],
        "authentication_provider": "pkg/authentication.py",
    }}}
    generated = (
        "from pkg.authentication import current_user\n"
        "def render_template():\n"
        "    values = {}\n"
        "    return values\n"
    )

    normalized = recipe.normalize_generated("pkg/templating.py", generated, plan)

    assert "\nfrom pkg.authentication import current_user" not in normalized
    assert "    from pkg.authentication import current_user" in normalized
    assert "'current_user': current_user" in normalized
    assert not any(
        "must inject the frozen" in item
        for item in framework_seam_violations(normalized, plan, "pkg/templating.py")
    )


def test_source_translation_literals_and_invented_mail_dependency_are_realized():
    files = {"pkg/notifications.py": (
        "from flask_babel import gettext as translate\n"
        "def title(): return translate('Welcome')\n"
    )}
    planned = [_pf("pkg/notifications.py", "support", "auth_login")]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    assert plan["decisions"][
        "translation_literals:pkg/notifications.py"
    ]["bindings"] == ["translate"]
    translated = recipe.normalize_generated(
        "pkg/notifications.py",
        "def title():\n    return translate('Welcome')\n",
        plan,
    )
    namespace = {}
    exec(translated, namespace)
    assert namespace["title"]() == "Welcome"
    assert "translate(" not in translated

    underscore_files = {"pkg/views.py": (
        "from flask_babel import _\n"
        "def title(name): return _('Welcome %(name)s', name=name)\n"
    )}
    underscore_plan = recipe.build_seam_plan(
        underscore_files, [_pf("pkg/views.py", "router", "route_to_endpoint")], {}, [],
    )
    assert underscore_plan["decisions"][
        "translation_literals:pkg/views.py"
    ]["bindings"] == ["_"]
    translated = recipe.normalize_generated(
        "pkg/views.py", underscore_files["pkg/views.py"], underscore_plan,
    )
    assert "flask_babel" not in translated
    namespace = {}
    exec(translated, namespace)
    assert namespace["title"]("Ada") == "Welcome Ada"

    generated_mail = """\
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
config = ConnectionConfig(MAIL_FROM='sender@example.com', MAIL_SERVER='smtp.example.com')
message = MessageSchema(
    subject='Subject', recipients=['reader@example.com'], body='<b>Hello</b>',
    subtype=MessageType.html,
)
"""
    normalized_mail = recipe.normalize_generated(
        "pkg/email.py", generated_mail, {},
    )
    assert "fastapi_mail" not in normalized_mail
    namespace = {}
    exec(normalized_mail, namespace)

    class Mailbox:
        sent = []

        def __init__(self, server, port):
            self.server = server
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send_message(self, message):
            self.sent.append(message)

    namespace["_portage_smtplib"].SMTP = Mailbox
    namespace["_portage_smtplib"].SMTP_SSL = Mailbox
    __import__("asyncio").run(
        namespace["FastMail"](namespace["config"]).send_message(
            namespace["message"],
        )
    )
    sent = Mailbox.sent[-1]
    assert sent["Subject"] == "Subject"
    assert sent["From"] == "sender@example.com"
    assert sent["To"] == "reader@example.com"


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


def test_misleveled_relative_import_uses_nearest_existing_project_module():
    plan = {
        "project_modules": [
            "pkg", "pkg.api", "pkg.models", "pkg.runtime", "models", "runtime",
        ],
        "decisions": {},
    }
    generated = (
        "from .models import User\n"
        "from .runtime import session\n"
    )

    normalized = recipe.normalize_generated("pkg/api/authentication.py", generated, plan)

    assert "from pkg.models import User" in normalized
    assert "from pkg.runtime import session" in normalized
    assert not any(
        "neither exists" in item
        for item in framework_seam_violations(
            normalized, plan, "pkg/api/authentication.py",
        )
    )


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
    assert "from .auth import bp" in realized
    assert "    from pkg.auth import load_user" in realized
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

    router_cli = (
        "from fastapi import APIRouter\n"
        "gateway = APIRouter()\n"
        "@gateway.cli.group()\n"
        "def tools(): pass\n"
    )
    assert any(
        "FastAPI has no `gateway.cli`" in item
        for item in framework_seam_violations(router_cli, plan, "pkg/db.py")
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


def test_session_middleware_cannot_masquerade_as_an_unrelated_provider():
    plan = {
        "version": 1,
        "decisions": {},
        "project_modules": ["pkg.extensions"],
        "project_roots": ["pkg"],
    }
    bad = (
        "from starlette.middleware.sessions import SessionMiddleware\n"
        "csrf = SessionMiddleware(secret_key='secret')\n"
    )
    assert any(
        "ASGI middleware, not a session/proxy" in item
        for item in framework_seam_violations(bad, plan, "pkg/extensions.py")
    )

    wrapped = bad.replace(
        "csrf = SessionMiddleware(secret_key='secret')",
        "wrapped = SessionMiddleware(app, secret_key='secret')",
    )
    assert framework_seam_violations(wrapped, plan, "pkg/extensions.py") == []


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
                {"name": "testing", "kind": "variable", "members": []},
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


def test_direct_test_surface_is_rendered_and_constructed_by_its_consumer(tmp_path):
    provider = PlannedFile(
        path="pkg/testing.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["direct_test_surface"],
            "exports": [{
                "name": "AppFacade", "kind": "class", "members": ["test_client"],
            }],
            "consumers": ["pkg/app.py"], "depends_on": [],
        },
    )
    factory = PlannedFile(path="pkg/app.py", role="app_factory")
    plan = recipe.build_seam_plan({}, [provider, factory], {}, [])
    rendered = recipe.render_created_artifact(provider, str(tmp_path))
    assert "class AppFacade(FastAPI):" in rendered
    assert "def test_client(self):" in rendered
    assert "return FlaskClientAdapter(self)" in rendered

    raw = (
        "from fastapi import FastAPI\n"
        "from pkg.testing import AppFacade\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    return app\n"
        "app = create_app()\n"
    )
    assert any(
        "must construct planned facade" in item
        for item in framework_seam_violations(raw, plan, "pkg/app.py")
    )
    realized = recipe.normalize_generated("pkg/app.py", raw, plan)
    assert "app = AppFacade()" in realized
    assert not any(
        "must construct planned facade" in item
        for item in framework_seam_violations(realized, plan, "pkg/app.py")
    )

    wrapped = raw.replace("return app", "return AppFacade(app)")
    realized_wrapped = recipe.normalize_generated("pkg/app.py", wrapped, plan)
    assert "app = AppFacade()" in realized_wrapped
    assert "return app" in realized_wrapped
    assert "AppFacade(app)" not in realized_wrapped

    module_plan = recipe.build_seam_plan({
        "pkg/app.py": (
            "from flask import Flask as WebApp\n"
            "app = WebApp(__name__)\n"
            "@app.get('/')\n"
            "def index(): return 'ok'\n"
        ),
    }, [provider, factory], {}, [])
    factory_only = (
        "from pkg.testing import AppFacade\n"
        "def create_app():\n"
        "    application = AppFacade()\n"
        "    return application\n"
    )
    realized_export = recipe.normalize_generated(
        "pkg/app.py", factory_only, module_plan,
    )
    assert realized_export.endswith("app = create_app()\n")

    testing_provider = PlannedFile(
        path="pkg/testing.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["direct_test_surface"],
            "exports": [{
                "name": "AppFacade", "kind": "class", "members": ["testing"],
            }],
            "consumers": ["pkg/app.py"], "depends_on": [],
        },
    )
    testing_plan = recipe.build_seam_plan(
        {}, [testing_provider, factory], {}, [],
    )
    duplicate_app = """\
from fastapi import FastAPI
from pkg.testing import AppFacade
def create_app():
    raw_app = FastAPI()
    application = AppFacade(testing=False)
    if not raw_app.testing:
        application.state.ready = True
    return application
"""
    realized = recipe.normalize_generated("pkg/app.py", duplicate_app, testing_plan)
    assert "if not application.testing:" in realized
    assert "raw_app.testing" not in realized


def test_public_factory_uses_frozen_facade_in_every_branch():
    provider = PlannedFile(
        path="pkg/testing.py",
        role="support",
        action="create",
        artifact_contract={
            "capabilities": ["direct_test_surface"],
            "exports": [{
                "name": "AppFacade",
                "kind": "class",
                "members": ["app_context", "testing"],
            }],
            "consumers": ["pkg/app.py"],
            "depends_on": [],
        },
    )
    factory = PlannedFile(path="pkg/app.py", role="app_factory")
    plan = recipe.build_seam_plan({}, [provider, factory], {}, [])
    generated = """\
from __future__ import annotations
from fastapi import FastAPI
from pkg.testing import AppFacade
def create_app(testing=False):
    if testing:
        app = FastAPI(title='test')
    else:
        app = FastAPI(title='live')
    app.testing = testing
    return app
"""

    realized = recipe.normalize_generated("pkg/app.py", generated, plan)

    assert realized.startswith("from __future__ import annotations")
    assert realized.count("app = AppFacade(") == 2
    assert "app = FastAPI(" not in realized
    assert "app.testing = testing" in realized
    assert not any(
        "must construct planned facade" in item
        for item in framework_seam_violations(realized, plan, "pkg/app.py")
    )


def test_planned_facade_gate_applies_only_to_factory_consumers():
    plan = {"decisions": {"surface": {
        "kind": "planned_test_surface", "provider": "pkg/testing.py",
        "classes": [{"name": "AppFacade", "members": ["testing"]}],
        "files": ["pkg/testing.py", "pkg/app.py", "pkg/helper.py"],
        "factory_consumers": ["pkg/app.py"],
    }}}

    assert not any(
        "must construct planned facade" in item
        for item in framework_seam_violations(
            "from fastapi import FastAPI\nvalue = FastAPI()\n",
            plan, "pkg/helper.py",
        )
    )


def test_factory_moves_source_local_submodule_import_back_inside_factory():
    files = {
        "pkg/__init__.py": (
            "from flask import Flask\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    from . import views\n"
            "    app.register_blueprint(views.bp)\n"
            "    return app\n"
        ),
        "pkg/views.py": "from flask import Blueprint\nbp = Blueprint('v', __name__)\n",
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = """\
from fastapi import FastAPI
from pkg.views import bp
def create_app():
    app = FastAPI()
    app.include_router(bp)
    return app
"""

    normalized = recipe.normalize_generated("pkg/__init__.py", generated, plan)

    assert normalized.index("def create_app") < normalized.index(
        "    from pkg.views import bp"
    )
    assert not any(
        "source-local import" in item
        for item in framework_seam_violations(
            normalized, plan, "pkg/__init__.py",
        )
    )


def test_injected_default_parameter_moves_after_required_source_arguments():
    generated = """\
from fastapi import APIRouter, Depends, Request
router = APIRouter()
def active_request(): return None
@router.post('/items')
async def update(request: Request = Depends(active_request), title: str, body: str):
    return title, body, request
"""

    normalized = recipe.normalize_generated("pkg/views.py", generated, {})

    tree = ast.parse(normalized)
    update = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "update"
    )
    assert [argument.arg for argument in update.args.args] == [
        "title", "body", "request",
    ]
    assert len(update.args.defaults) == 1


def test_source_safe_imports_and_private_helpers_close_generated_bindings():
    files = {
        "pkg/auth.py": (
            "from __future__ import annotations\n"
            "import json\n"
            "from .models import User\n"
            "def _decode_token(value):\n"
            "    return json.loads(value)\n"
            "def lookup(value):\n"
            "    return User.query.get(_decode_token(value)['id'])\n"
        ),
        "pkg/models.py": "class User: pass\n",
    }
    planned = [
        PlannedFile(path="pkg/auth.py", role="support"),
        PlannedFile(path="pkg/models.py", role="support"),
    ]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    plan["project_modules"] = ["pkg.auth", "pkg.models"]
    plan["project_roots"] = ["pkg"]
    generated = (
        "from __future__ import annotations\n"
        "def lookup(value):\n"
        "    return User.query.get(_decode_token(value)['id'])\n"
    )

    normalized = recipe.normalize_generated("pkg/auth.py", generated, plan)

    assert "import json" in normalized
    assert normalized.startswith("from __future__ import annotations")
    assert "from .models import User" in normalized
    assert "def _decode_token(value):" in normalized
    assert all_generation_violations(normalized, {}, "pkg/auth.py", plan) == []


def test_source_initializer_callback_closes_generated_factory_binding():
    files = {
        "pkg/app.py": (
            "from flask import Flask, current_app, request\n"
            "from .extensions import babel\n"
            "def get_locale():\n"
            "    return request.accept_languages.best_match("
            "current_app.config['LANGUAGES'])\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    babel.init_app(app, locale_selector=get_locale)\n"
            "    return app\n"
        ),
        "pkg/extensions.py": "babel = object()\n",
    }
    planned = [
        PlannedFile(
            path="pkg/runtime_context.py", role="support", action="create",
            artifact_contract={
                "capabilities": ["request_context"],
                "exports": [
                    {"name": "current_app", "kind": "variable", "members": []},
                    {"name": "get_request_context", "kind": "function", "members": []},
                ],
                "consumers": ["pkg/app.py"], "depends_on": [],
            },
        ),
        PlannedFile(path="pkg/app.py", role="app_factory"),
        PlannedFile(path="pkg/extensions.py", role="support"),
    ]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    plan["project_modules"] = ["pkg.app", "pkg.extensions", "pkg.runtime_context"]
    plan["project_roots"] = ["pkg"]
    generated = (
        "from fastapi import FastAPI\n"
        "from pkg.runtime_context import current_app\n"
        "from .extensions import babel\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    app.state.config = {'LANGUAGES': ['en']}\n"
        "    babel.init_app(app, locale_selector=get_locale)\n"
        "    return app\n"
    )

    normalized = recipe.normalize_generated("pkg/app.py", generated, plan)

    assert "def get_locale():" in normalized
    assert "locale_selector=get_locale" in normalized
    assert "_portage_get_request_context()['request']" in normalized
    compile(normalized, "pkg/app.py", "exec")
    assert all_generation_violations(normalized, {}, "pkg/app.py", plan) == []


def test_source_instance_path_is_owned_as_application_state(tmp_path):
    files = {
        "pkg/app.py": (
            "from flask import Flask\n"
            "def create_app():\n"
            "    app = Flask(__name__, instance_path='/srv/custom')\n"
            "    return app\n"
        ),
        "pkg/consumer.py": (
            "from flask import current_app\n"
            "def data_path(): return current_app.instance_path\n"
        ),
    }
    runtime = PlannedFile(
        path="pkg/context.py",
        role="support",
        action="create",
        artifact_contract={
            "capabilities": ["request_context"],
            "exports": [
                {"name": "Runtime", "kind": "class", "members": [
                    "get_request_context", "manage_session",
                ]},
                {"name": "current_app", "kind": "variable", "members": []},
            ],
            "consumers": ["pkg/app.py", "pkg/consumer.py"],
            "depends_on": [],
        },
    )
    testing = PlannedFile(
        path="pkg/testing.py",
        role="support",
        action="create",
        artifact_contract={
            "capabilities": ["direct_test_surface", "test_context_surface"],
            "exports": [{
                "name": "AppFacade", "kind": "class", "members": ["app_context"],
            }],
            "consumers": ["pkg/app.py"],
            "depends_on": ["pkg/context.py"],
        },
    )
    planned = [
        runtime,
        testing,
        PlannedFile(path="pkg/consumer.py", role="support"),
        PlannedFile(path="pkg/app.py", role="app_factory"),
    ]
    plan = recipe.build_seam_plan(files, planned, {}, [])

    factory = recipe.normalize_generated(
        "pkg/app.py",
        "from fastapi import FastAPI\n"
        "def create_app():\n"
        "    app = FastAPI(instance_path='/srv/custom')\n"
        "    return app\n",
        plan,
    )
    consumer = recipe.normalize_generated(
        "pkg/consumer.py",
        "from pkg.context import current_app\n"
        "def data_path(): return current_app.instance_path\n",
        plan,
    )
    rendered_runtime = recipe.render_created_artifact(runtime, str(tmp_path))

    assert plan["decisions"]["application_factory:pkg/app.py"][
        "app_state_members"
    ] == ["instance_path"]
    assert "app = AppFacade()" in factory
    assert "app.state.instance_path = '/srv/custom'" in factory
    assert "instance_path=" not in factory
    assert "return current_app.instance_path" in consumer
    assert "return getattr(app.state, name)" in rendered_runtime
    assert not any(
        "instance_path" in item
        for item in framework_seam_violations(
            consumer, plan, "pkg/consumer.py",
        )
    )


def test_public_export_may_call_local_factory_that_returns_owned_facade():
    manifest = {
        "pkg/testing.py::AppFacade": {
            "module": "pkg/testing.py", "symbol": "AppFacade",
            "provenance": "planned_create", "members": ["test_client"],
            "capabilities": ["direct_test_surface"],
            "factory_consumers": ["pkg/app.py"],
        },
        "pkg/app.py::app": {
            "module": "pkg/app.py", "symbol": "app", "target_kind": "variable",
        },
    }
    content = """\
from pkg.testing import AppFacade
def create_app():
    application = AppFacade()
    return application
app = create_app()
"""

    assert planned_capability_consumer_violations(
        content, manifest, "pkg/app.py",
    ) == []


def test_public_factory_may_construct_owned_facade_inside_branches():
    manifest = {
        "pkg/testing.py::AppFacade": {
            "module": "pkg/testing.py", "symbol": "AppFacade",
            "provenance": "planned_create", "members": ["testing"],
            "capabilities": ["direct_test_surface"],
            "factory_consumers": ["pkg/app.py"],
        },
        "pkg/app.py::create_app": {
            "module": "pkg/app.py", "symbol": "create_app",
            "target_kind": "function",
        },
    }
    content = """\
from pkg.testing import AppFacade
def create_app(testing=False):
    if testing:
        app = AppFacade(testing=True)
    else:
        app = AppFacade(testing=False)
    return app
"""

    assert planned_capability_consumer_violations(
        content, manifest, "pkg/app.py",
    ) == []


def test_factory_without_planned_facade_does_not_require_facade_cleanup_input():
    plan = {
        "decisions": {
            "factory": {
                "kind": "application_factory", "factory": "pkg/app.py",
                "files": ["pkg/app.py"],
                "cleanup_callbacks": [{
                    "provider": "pkg/db.py", "functions": ["close_db"],
                }],
            },
        },
    }
    content = """\
from fastapi import FastAPI
def create_app():
    return FastAPI()
"""

    assert framework_seam_violations(content, plan, "pkg/app.py") == []


def test_factory_initializer_is_realized_without_a_planned_facade():
    files = {
        "pkg/db.py": """\
from flask import current_app, g
def get_db():
    return g.db
def close_db(error=None):
    g.pop('db', None)
def init_app(app):
    app.teardown_appcontext(close_db)
""",
        "pkg/app.py": """\
from flask import Flask
def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(DATABASE='default.sqlite')
    if test_config is not None:
        app.config.update(test_config)
    from . import db
    db.init_app(app)
    return app
""",
    }
    planned = [
        _pf("pkg/db.py", "support", "request_context"),
        _pf("pkg/app.py", "app_factory", "app_factory"),
    ]
    manifest = {
        "pkg/db.py::get_db": {
            "module": "pkg/db.py", "symbol": "get_db",
            "preserve_shape": True, "additional_exports": ["get_db_dep"],
        },
    }
    plan = recipe.build_seam_plan(files, planned, manifest, [])
    generated = """\
from fastapi import FastAPI
from . import db
def create_app(test_config=None):
    config = {'DATABASE': 'default.sqlite'}
    if test_config is not None:
        config.update(test_config)
    app = FastAPI()
    app.state.config = config
    return app
"""

    realized = recipe.normalize_generated("pkg/app.py", generated, plan)

    assert "db.init_app(app)" in realized
    assert not any(
        "application factory must call" in item
        or "planned application facade must receive cleanup" in item
        for item in framework_seam_violations(realized, plan, "pkg/app.py")
    )


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

    delegated_good = """\
class Context:
    def __init__(self, app):
        self._app = app
    def pop(self):
        for callback in self._app._cleanup_callbacks:
            callback()
class App:
    def __init__(self, cleanup_callbacks=()):
        self._cleanup_callbacks = tuple(cleanup_callbacks)
    def app_context(self):
        return Context(self)
"""
    assert framework_seam_violations(
        delegated_good, plan, "pkg/testing.py",
    ) == []

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

    plan["decisions"]["provider_protocol:pkg/extensions.py:csrf"] = {
        "kind": "provider_protocol",
        "provider": "pkg/extensions.py",
        "symbol": "csrf",
        "constructor": {"module": "flask_wtf.csrf", "name": "CSRFProtect"},
        "files": ["pkg/app.py", "pkg/extensions.py"],
    }
    csrf_factory = normalized_factory.replace(
        "from .context import RuntimeContext",
        "from .context import RuntimeContext\nfrom .extensions import csrf",
    ).replace(
        "    app.add_middleware(RuntimeContext)",
        "    app.add_middleware(RuntimeContext)\n    csrf.init_app(app)",
    )
    normalized_csrf = recipe.normalize_generated("pkg/app.py", csrf_factory, plan)
    assert normalized_csrf.index("csrf.init_app(app)") < normalized_csrf.index(
        "add_middleware(RuntimeContext)"
    ) < normalized_csrf.index("add_middleware(SessionMiddleware")

    missing_install = bad_factory.replace(
        "lifespan=RuntimeContext.manage_session", "lifespan=RuntimeContext()",
    ).replace("    app.add_middleware(RuntimeContext)\n", "").replace(
        "def create_app():",
        "def helper():\n    value = object()\n    return value\ndef create_app():",
    )
    normalized_missing = recipe.normalize_generated(
        "pkg/app.py", missing_install, plan,
    )
    assert "lifespan=" not in normalized_missing
    assert "app.add_middleware(RuntimeContext)" in normalized_missing
    assert "value.add_middleware" not in normalized_missing
    assert normalized_missing.index("add_middleware(RuntimeContext)") < (
        normalized_missing.index("add_middleware(SessionMiddleware")
    )
    assert not any(
        "factory must install" in item or "is middleware, not an application lifespan" in item
        for item in framework_seam_violations(
            normalized_missing, plan, "pkg/app.py", manifest,
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
        "pkg/auth.py": (
            "from flask import g, session, url_for\n"
            "print(g.user, session.get('id'), url_for('auth.login'))\n"
        ),
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
    assert {
        export["name"] for export in template_owner["exports"]
        if export["kind"] == "function"
    } == {"render_template", "url_for"}
    assert "cleanup_callbacks=()" in test_owner["instructions"]
    assert next(
        item for item in audit if item["path"] == "pkg/testing.py"
    )["added_dependencies"] == ["pkg/context.py"]


def test_runtime_current_app_proxy_preserves_flask_logger(tmp_path):
    artifact = PlannedFile(
        path="pkg/context.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["request_context"],
            "exports": [
                {"name": "Runtime", "kind": "class", "members": [
                    "get_request_context", "manage_session",
                ]},
                {"name": "current_app", "kind": "variable", "members": []},
                {"name": "g", "kind": "variable", "members": []},
            ],
            "consumers": ["pkg/views.py"], "depends_on": [],
        },
    )

    rendered = recipe.render_created_artifact(artifact, str(tmp_path))

    assert rendered is not None
    assert "import logging" in rendered
    assert 'if name == "logger":' in rendered
    assert "return logging.getLogger" in rendered


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
                {"name": "_push_context", "kind": "function", "members": []},
                {"name": "_pop_context", "kind": "function", "members": []},
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
            }, {
                "name": "url_for", "kind": "function", "members": [],
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
    assert "def get_request_context()" in rendered["pkg/context.py"]
    assert "def manage_session(request: Request)" in rendered["pkg/context.py"]
    assert "from pkg.context import _pop_context, _push_context, g, session" in (
        rendered["pkg/testing.py"]
    )
    assert "request, template_name, values, status_code=status_code" in (
        rendered["pkg/templating.py"]
    )
    assert 'vars(request.state).get("_state", {})' in rendered["pkg/templating.py"]
    assert 'path_params.pop("filename")' in rendered["pkg/templating.py"]
    assert 'path_params.pop("_external", False)' in rendered["pkg/templating.py"]
    assert "return str(url) if external else url.path" in rendered["pkg/templating.py"]
    assert "def url_for(name: str, **path_params)" in rendered["pkg/templating.py"]
    assert "templates.env.globals['url_for'] = url_for" in (
        rendered["pkg/templating.py"]
    )
    assert '"config": getattr(request.app.state, "config", {})' in (
        rendered["pkg/templating.py"]
    )
    assert "autoescape=select_autoescape" in rendered["pkg/templating.py"]
    assert "Jinja2Templates(env=Environment(" in rendered["pkg/templating.py"]
    namespace = {}
    exec(rendered["pkg/context.py"], namespace)
    token = namespace["_push_context"]({"session": {}})
    namespace["flash"]("saved", "success")
    assert namespace["get_flashed_messages"](with_categories=True) == [
        ("success", "saved"),
    ]
    namespace["_pop_context"](token)
    violations = {
        path: all_generation_violations(
            content, manifest, path, seam_plan, None,
        )
        for path, content in rendered.items()
    }
    assert not any(violations.values()), violations


def test_contract_compiler_canonicalizes_context_and_test_facade(
    tmp_path,
):
    files = {
        "pkg/app.py": "from flask import Flask\ndef create_app(): return Flask(__name__)\n",
        "pkg/auth.py": "from flask import g, session\nprint(g.user, session.get('id'))\n",
        "pkg/search.py": (
            "from flask import current_app\n"
            "def enabled(): return bool(current_app.state.search)\n"
        ),
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
        _pf("pkg/search.py", "support", "request_context"),
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
    assert {"get_request_context", "manage_session"} <= {
        export["name"] for export in runtime["exports"]
        if export["kind"] == "function"
    }
    assert {"g", "session", "current_app", "flash", "get_flashed_messages"} <= {
        export["name"] for export in runtime["exports"]
    }
    assert next(
        export for export in runtime["exports"]
        if export["name"] == "_push_context"
    )["signature"] == "def _push_context(state=None)"
    assert next(
        export for export in testing["exports"] if export["kind"] == "class"
    )["members"] == ["app_context", "testing"]
    assert {
        export["name"] for export in testing["exports"]
        if export["kind"] == "variable"
    } == {"g", "session"}
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
    assert "current_app = _CurrentAppProxy()" in rendered_runtime
    assert '"app": request.app' in rendered_runtime
    assert next(
        export for export in testing["exports"] if export["kind"] == "class"
    )["name"] == "FastAPIApp"
    assert "class FastAPIApp(FastAPI)" in rendered_testing
    rendered_mapping_facade = recipe.render_created_artifact(
        PlannedFile(
            path="pkg/facade.py", role="support", action="create",
            artifact_contract={
                "capabilities": ["direct_test_surface"],
                "depends_on": ["pkg/context.py"],
                "exports": [{
                    "name": "App", "kind": "class",
                    "members": ["app_context", "testing"],
                }],
            },
        ),
        str(tmp_path),
    )
    assert 'dict(config) if hasattr(config, "items")' in rendered_mapping_facade
    assert any(
        item.get("renamed_exports") == [{
            "from": "app_context", "to": "FastAPIApp",
        }]
        for item in audit
    )


def test_source_current_app_consumer_uses_frozen_runtime_proxy_not_factory_reentry():
    plan = {"decisions": {"ambient": {
        "kind": "ambient_context_runtime",
        "runtime_providers": ["pkg/runtime.py"],
        "runtime_classes": {"pkg/runtime.py": ["RuntimeContext"]},
        "current_app_consumers": ["pkg/search.py"],
    }}}
    generated = (
        "from pkg import create_app\n"
        "app = create_app()\n"
        "def enabled():\n"
        "    return bool(app.state.config.get('SEARCH_URL'))\n"
    )

    normalized = recipe.normalize_generated("pkg/search.py", generated, plan)

    assert "create_app" not in normalized
    assert "app =" not in normalized
    assert "from pkg.runtime import current_app" in normalized
    assert "current_app.state.config.get('SEARCH_URL')" in normalized


def test_combined_template_owner_gets_canonical_export_and_renderer_declines(tmp_path):
    files = {
        "pkg/app.py": "from flask import Flask\ndef create_app(): return Flask(__name__)\n",
        "pkg/views.py": (
            "from flask import g, render_template\n"
            "def index(): return render_template('index.html', user=g.user)\n"
        ),
    }
    planned = recipe.plan_files(files)
    proposal = [{
        "path": "pkg/runtime.py", "role": "support", "purpose": "runtime",
        "instructions": "own context and templates",
        "capabilities": ["request_context", "template_rendering"],
        "exports": [
            {"name": "RuntimeContext", "kind": "class", "members": []},
            {"name": "render", "kind": "function", "members": []},
        ],
        "consumers": ["pkg/views.py"], "depends_on": [],
    }]

    completed, _audit = recipe.materialize_artifact_contracts(
        proposal, files, planned,
    )
    owner = completed[0]

    assert {
        export["name"] for export in owner["exports"]
    } >= {"RuntimeContext", "get_request_context", "render_template"}
    assert recipe.render_created_artifact(
        PlannedFile(
            path=owner["path"], role="support", action="create",
            artifact_contract=owner,
        ),
        str(tmp_path),
    ) is None

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


def test_source_used_extensions_are_realized_as_material_lifecycle_providers():
    files = {
        "pkg/extensions.py": (
            "from flask_example import Extension\n"
            "from flask_mail import Mail\n"
            "from flask_migrate import Migrate\n"
            "from flask_wtf.csrf import CSRFProtect\n"
            "mail = Mail()\n"
            "migrate = Migrate()\n"
            "csrf = CSRFProtect()\n"
            "extension = Extension()\n"
            "extension.mode = 'strict'\n"
            "@extension.handler\n"
            "def handle(value): return value\n"
        ),
        "pkg/app.py": (
            "from flask import Flask\n"
            "from flask_example import ExtensionError\n"
            "from .extensions import csrf, extension, mail, migrate\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    csrf.init_app(app)\n"
            "    mail.init_app(app)\n"
            "    migrate.init_app(app, object())\n"
            "    extension.init_app(app)\n"
            "    @app.errorhandler(ExtensionError)\n"
            "    def rejected(error): return str(error), 400\n"
            "    return app\n"
        ),
        "pkg/email.py": (
            "from flask_mail import Message\n"
            "from .extensions import extension, mail\n"
            "def send(message):\n"
            "    Message(subject='notice')\n"
            "    extension.send(message)\n"
            "    return mail.send(message)\n"
        ),
        "tests/test_mail.py": (
            "from pkg.extensions import extension, mail\n"
            "def test_mail():\n"
            "    with mail.record_messages() as outbox:\n"
            "        mail.send(object())\n"
            "        assert len(outbox) == 1\n"
            "    with extension.record_messages() as captured:\n"
            "        extension.send(object())\n"
            "        assert len(captured) == 1\n"
        ),
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    protocols = {
        item["symbol"]: item
        for item in plan["decisions"].values()
        if item.get("kind") == "provider_protocol"
    }

    assert set(protocols) >= {"csrf", "extension", "mail", "migrate"}
    assert protocols["mail"]["context_manager_members"] == ["record_messages"]
    assert "Message" in protocols["mail"]["callable_members"]
    assert protocols["extension"]["attribute_values"] == {"mode": "strict"}
    assert protocols["extension"]["decorator_members"] == ["handler"]
    assert protocols["extension"]["exception_members"] == ["ExtensionError"]
    factory_contract = plan["decisions"]["application_factory:pkg/app.py"]
    assert [item["symbol"] for item in factory_contract["initializers"]] == [
        "csrf", "extension", "mail", "migrate",
    ]
    assert [item["original_call"] for item in factory_contract["initializers"]] == [
        "csrf.init_app(app)",
        "extension.init_app(app)",
        "mail.init_app(app)",
        "migrate.init_app(app, object())",
    ]
    placeholder_errors = framework_seam_violations(
        "mail = None\n", plan, "pkg/extensions.py",
    )
    assert any("cannot be a None placeholder" in item for item in placeholder_errors)

    translated = "from fastapi import APIRouter\nrouter = APIRouter()\n"
    assert framework_seam_violations(
        translated, plan, "pkg/extensions.py",
    ) == []

    normalized = recipe.normalize_generated(
        "pkg/extensions.py",
        "mail = None\nmigrate = None\ncsrf = object()\nextension = None\n",
        plan,
    )
    assert "mail = None" not in normalized
    assert "migrate = None" not in normalized
    assert "csrf = object()" not in normalized
    assert "extension = None" not in normalized
    assert framework_seam_violations(
        normalized, plan, "pkg/extensions.py",
    ) == []

    namespace = {}
    exec(normalized, namespace)
    app = SimpleNamespace(
        state=SimpleNamespace(config={}), exception_handlers={}, middlewares=[],
    )
    app.middleware = lambda _kind: lambda function: (
        app.middlewares.append(function) or function
    )
    db = object()
    assert namespace["csrf"].init_app(app) is app
    assert namespace["migrate"].init_app(app, db) is app
    assert namespace["extension"].init_app(app) is app
    assert namespace["extension"].mode == "strict"
    assert namespace["extension"].handler(lambda: 1)() == 1
    assert namespace["ExtensionError"]().description == "Extension"
    invalid = SimpleNamespace(
        app=app, method="POST", session={},
        body=lambda: asyncio.sleep(0, result=b""),
        form=lambda: asyncio.sleep(0, result={}),
    )
    response = asyncio.run(app.middlewares[0](
        invalid, lambda _request: asyncio.sleep(0, result="ok"),
    ))
    assert response.status_code == 400

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    live_app = FastAPI()
    live_app.state.config = {"WTF_CSRF_ENABLED": True}
    namespace["csrf"].init_app(live_app)
    live_app.add_middleware(SessionMiddleware, secret_key="test-secret")

    @live_app.get("/form")
    async def form(request: Request):
        request.session["_csrf_token"] = "known-token"
        return {"ready": True}

    @live_app.post("/form")
    async def submit(request: Request):
        return dict(await request.form())

    client = TestClient(live_app)
    assert client.get("/form").status_code == 200
    valid = client.post("/form", data={"csrf_token": "known-token", "value": "kept"})
    assert valid.json()["value"] == "kept"
    assert client.post("/form", data={"value": "rejected"}).status_code == 400
    with namespace["mail"].record_messages() as outbox:
        assert namespace["mail"].Message(subject="notice").subject == "notice"
        message = object()
        assert namespace["mail"].send(message) is message
        assert outbox == [message]
    with namespace["extension"].record_messages() as captured:
        message = object()
        assert namespace["extension"].send(message) is message
        assert captured == [message]

    normalized_factory = recipe.normalize_generated(
        "pkg/app.py",
        "from fastapi import FastAPI\n"
        "from pkg.runtime import ExtensionError\n"
        "from .extensions import csrf, extension, mail, migrate\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    @app.exception_handler(ExtensionError)\n"
        "    def rejected(request, error): return str(error)\n"
        "    return app\n",
        plan,
    )
    assert "from pkg.extensions import ExtensionError" in normalized_factory
    assert "from pkg.runtime import ExtensionError" not in normalized_factory
    assert normalized_factory.index("csrf.init_app(app)") < normalized_factory.index(
        "return app"
    )
    assert "migrate.init_app(app, object())" in normalized_factory


def test_source_flask_forms_keep_bound_fields_hidden_tag_and_submit_shape(monkeypatch):
    files = {
        "pkg/forms.py": (
            "from flask_wtf import FlaskForm\n"
            "from wtforms import StringField\n"
            "class ContactForm(FlaskForm): name = StringField('Name')\n"
        ),
        "pkg/views.py": (
            "from flask import Blueprint\n"
            "from .forms import ContactForm\n"
            "bp = Blueprint('views', __name__)\n"
            "@bp.route('/contact', methods=['GET', 'POST'])\n"
            "def contact():\n"
            "    form = ContactForm()\n"
            "    return str(form.validate_on_submit())\n"
        ),
    }
    planned = recipe.plan_files(files)
    planned.append(PlannedFile(
        path="pkg/runtime.py", role="support", action="create",
        artifact_contract={"capabilities": ["request_context", "session_and_flash"]},
    ))
    plan = recipe.build_seam_plan(files, planned, {}, [])
    decision = plan["decisions"]["form_provider:pkg/forms.py"]
    assert decision["classes"] == ["ContactForm"]
    assert decision["consumers"] == ["pkg/views.py"]

    normalized = recipe.normalize_generated(
        "pkg/forms.py",
        "from wtforms import StringField\n"
        "class ContactForm:\n"
        "    name = StringField('Name')\n",
        plan,
    )
    assert "class ContactForm(_PortageForm)" in normalized
    assert "def hidden_tag(self)" in normalized
    assert "def validate_on_submit(self)" in normalized
    assert all_generation_violations(normalized, {}, "pkg/forms.py", plan) == []

    package = ModuleType("pkg")
    package.__path__ = []
    runtime = ModuleType("pkg.runtime")
    runtime.session = {}
    runtime.get_request_context = lambda: {
        "request": SimpleNamespace(method="POST"),
    }
    wtforms = ModuleType("wtforms")

    class FakeForm:
        def __init__(self, formdata=None):
            self.formdata = formdata

        def validate(self):
            return True

    class FakeField:
        def __init__(self, *_args, **_kwargs):
            self.data = None

    wtforms.Form = FakeForm
    wtforms.HiddenField = wtforms.StringField = FakeField
    monkeypatch.setitem(sys.modules, "pkg", package)
    monkeypatch.setitem(sys.modules, "pkg.runtime", runtime)
    monkeypatch.setitem(sys.modules, "wtforms", wtforms)
    namespace = {"__name__": "pkg.forms", "__package__": "pkg"}
    exec(normalized, namespace)
    form = namespace["ContactForm"]()
    assert 'name="csrf_token"' in str(form.hidden_tag())
    assert runtime.session["_csrf_token"]
    assert form.validate_on_submit() is True

    consumer = recipe.normalize_generated(
        "pkg/views.py",
        "from fastapi import Request\n"
        "from pkg.forms import ContactForm\n"
        "async def contact(request: Request):\n"
        "    form = ContactForm()\n"
        "    return form.validate_on_submit()\n",
        plan,
    )
    assert "ContactForm(await request.form() if request.method == 'POST' else None)" in consumer


def test_basic_authorization_decoder_is_realized_from_source_semantics():
    files = {
        "pkg/auth.py": (
            "from flask import request\n"
            "def authenticate():\n    return request.authorization\n"
        ),
    }
    planned = [_pf("pkg/auth.py", "router", "route_to_endpoint")]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = (
        "def authenticate(authorization):\n"
        "    return _decode_basic_auth(authorization)\n"
    )

    normalized = recipe.normalize_generated("pkg/auth.py", generated, plan)

    assert "import base64 as _portage_base64" in normalized
    assert "def _decode_basic_auth(value):" in normalized
    assert all_generation_violations(normalized, {}, "pkg/auth.py", plan) == []
    namespace = {}
    exec(normalized, namespace)
    assert namespace["_decode_basic_auth"]("Basic dXNlcjpwYXNz") == ["user", "pass"]


def test_app_context_facade_depends_on_and_uses_the_runtime_owner(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "commands.py").write_text(
        "def init_app(app):\n"
        "    @app.cli.command('seed')\n"
        "    def seed(): pass\n"
    )
    files = {
        "pkg/app.py": "from flask import Flask\ndef create_app(): return Flask(__name__)\n",
        "pkg/model.py": "from flask import current_app\nprint(current_app.config)\n",
        "tests/conftest.py": (
            "from pkg.app import create_app\n"
            "def app():\n"
            "    app = create_app()\n"
            "    with app.app_context():\n        yield app\n"
        ),
    }
    planned = recipe.plan_files(files)
    proposal = [{
        "path": "pkg/runtime.py", "role": "support", "purpose": "runtime",
        "instructions": "runtime", "capabilities": ["request_context"],
        "exports": [{
            "name": "RuntimeContext", "kind": "class", "signature": "",
            "members": ["dispatch"],
        }],
        "consumers": ["pkg/app.py", "pkg/model.py"], "depends_on": [],
    }, {
        "path": "pkg/testing.py", "role": "support", "purpose": "testing",
        "instructions": "testing", "capabilities": ["direct_test_surface"],
        "exports": [{
            "name": "AppFacade", "kind": "class", "signature": "",
            "members": ["app_context"],
        }],
        "consumers": ["pkg/app.py"], "depends_on": [],
    }]

    completed, _audit = recipe.materialize_artifact_contracts(
        proposal, files, planned,
    )
    facade = next(item for item in completed if item["path"] == "pkg/testing.py")
    assert facade["depends_on"] == ["pkg/runtime.py"]
    created = PlannedFile(
        path=facade["path"], role=facade["role"], action="create",
        artifact_contract=facade,
    )
    rendered = recipe.render_created_artifact(created, str(tmp_path))
    assert "return _AppContext(self)" in rendered
    assert "_register_cli_0" not in rendered


def test_template_filter_consumers_require_the_template_environment_export(tmp_path):
    (tmp_path / "pkg" / "templates").mkdir(parents=True)
    files = {
        "pkg/one.py": (
            "from flask import Blueprint, render_template\n"
            "from pkg.filters import shout\n"
            "bp = Blueprint('one', __name__)\n"
            "bp.add_app_template_filter(shout, 'loud')\n"
            "def page(): return render_template('one.html')\n"
        ),
        "pkg/filters.py": "def shout(value): return value.upper()\n",
        "pkg/two.py": (
            "from flask import render_template\n"
            "def page(): return render_template('two.html')\n"
        ),
    }
    planned = recipe.plan_files(files)
    proposal = [{
        "path": "pkg/templating.py", "role": "support",
        "purpose": "Render templates.", "instructions": "Render templates.",
        "capabilities": ["template_rendering"],
        "consumers": ["pkg/one.py", "pkg/two.py"], "depends_on": [],
        "exports": [{
            "name": "render_template", "kind": "function",
            "signature": "", "members": [],
        }],
    }]

    completed, _audit = recipe.materialize_artifact_contracts(
        proposal, files, planned,
    )
    artifact = completed[0]
    assert {item["name"]: item["kind"] for item in artifact["exports"]} == {
        "render_template": "function", "templates": "variable",
    }

    created = PlannedFile(
        path=artifact["path"], role=artifact["role"], action="create",
        artifact_contract=artifact,
    )
    rendered = recipe.render_created_artifact(created, str(tmp_path))
    assert "templates = Jinja2Templates" in rendered
    plan = recipe.build_seam_plan(files, [*planned, created], {}, [])
    normalized = recipe.normalize_generated("pkg/templating.py", rendered, plan)
    assert "from pkg.filters import shout as _portage_template_filter_0" in normalized
    assert "templates.env.filters['loud'] = _portage_template_filter_0" in normalized


def test_extension_provider_contract_freezes_surface_and_import_direction():
    files = {
        "app/__init__.py": (
            "from flask import Flask\n"
            "from flask_sqlalchemy import SQLAlchemy\n"
            "db = SQLAlchemy()\n"
            "def create_app():\n    return Flask(__name__)\n"
            "from app import models\n"
        ),
        "app/models.py": (
            "from app import db\n"
            "class User(db.Model):\n    pass\n"
            "class Movie(db.Model):\n    __tablename__ = 'films'\n"
        ),
        "tests/test_db.py": (
            "from app import db\n"
            "from app.models import Movie, User\n"
            "def test_db():\n"
            "    db.create_all()\n"
            "    db.session.commit()\n"
            "    db.drop_all()\n"
            "    Movie.query.first()\n"
        ),
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    decision = plan["decisions"]["extension_provider:app/__init__.py:db"]

    assert decision["consumers"] == ["app/models.py", "tests/test_db.py"]
    assert decision["members"] == ["Model", "create_all", "drop_all", "session"]
    assert decision["implicit_tables"] == {"app/models.py": {"User": "user"}}
    assert decision["query_models"] == ["Movie"]
    assert "Assign that export at module scope" in decision["instruction"]

    normalized_model = recipe.normalize_generated(
        "app/models.py", "from app import db\nclass User(db.Model):\n    pass\n", plan,
    )
    assert "__tablename__ = 'user'" in normalized_model

    bad = (
        "from app import models\n"
        "from sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "class DBFacade:\n"
        "    def __init__(self):\n"
        "        self.Base = Base\n"
        "        self.session = object()\n"
        "db = DBFacade()\n"
    )
    violations = framework_seam_violations(bad, plan, "app/__init__.py")
    assert any("missing source-exercised members" in item for item in violations)
    assert any("imports consumer app/models.py before provider initialization" in item
               for item in violations)

    normalized = recipe.normalize_generated("app/__init__.py", bad, plan)
    assert normalized.index("db = DBFacade()") < normalized.index(
        "from app import models"
    )
    assert not any(
        "imports consumer" in item
        for item in framework_seam_violations(normalized, plan, "app/__init__.py")
    )
    assert "class _PortageModelQuery" in normalized
    assert "query = _PortageModelQuery()" in normalized

    good = (
        "class DBFacade:\n"
        "    class Model: pass\n"
        "    def __init__(self): self.session = object()\n"
        "    def create_all(self): pass\n"
        "    def drop_all(self): pass\n"
        "db = DBFacade()\n"
        "def create_app():\n"
        "    from app import models\n"
        "    return models\n"
    )
    extension_errors = [
        item for item in framework_seam_violations(good, plan, "app/__init__.py")
        if "extension provider" in item or "source-exercised" in item
    ]
    assert extension_errors == []


def test_extension_provider_preserves_source_observed_lazy_consumer_imports():
    plan = {"decisions": {"extension_provider:pkg/extensions.py:db": {
        "kind": "extension_provider", "provider": "pkg/extensions.py",
        "symbol": "db", "consumers": ["pkg/models.py"],
        "lazy_consumers": ["pkg/models.py"], "members": [],
    }}}
    generated = (
        "class DBFacade: pass\n"
        "db = DBFacade()\n"
        "from pkg.models import User\n"
        "def load_user(key):\n    return User(key)\n"
    )

    normalized = recipe.normalize_generated(
        "pkg/extensions.py", generated, plan,
    )

    assert normalized.index("def load_user") < normalized.index(
        "    from pkg.models import User"
    )
    assert "\nfrom pkg.models import User\n" not in normalized


def test_direct_test_facade_captures_source_cli_registrar(tmp_path):
    files = {
        "pkg/runtime.py": "def _push_context(state=None): return state\n"
        "def _pop_context(token): pass\n",
        "pkg/commands.py": (
            "import click\n"
            "def register_commands(app):\n"
            "    @app.cli.command('init-db')\n"
            "    def initdb(): click.echo('ready')\n"
        ),
        "pkg/__init__.py": (
            "from flask import Flask\n"
            "from pkg.commands import register_commands\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    register_commands(app)\n"
            "    return app\n"
        ),
        "test_app.py": (
            "def test_cli(app):\n"
            "    app.app_context().push()\n"
            "    app.test_client()\n"
            "    app.test_cli_runner().invoke(args=['init-db'])\n"
        ),
    }
    for path, source in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    planned = recipe.plan_files(files)
    contract = {
        "path": "pkg/testing.py", "role": "support",
        "capabilities": ["direct_test_surface"],
        "exports": [{
            "name": "TargetApp", "kind": "class", "signature": "",
            "members": ["app_context", "test_client", "test_cli_runner"],
        }],
        "consumers": ["pkg/__init__.py"], "depends_on": ["pkg/context.py"],
    }
    runtime = PlannedFile(
        path="pkg/context.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["request_context", "session_and_flash"],
            "exports": [{
                "name": "RuntimeContext", "kind": "class", "members": ["dispatch"],
            }],
            "consumers": ["pkg/__init__.py", "pkg/testing.py"], "depends_on": [],
        },
    )
    artifact = PlannedFile(
        path="pkg/testing.py", role="support", action="create",
        artifact_contract=contract,
    )

    rendered = recipe.render_created_artifact(artifact, str(tmp_path))
    assert rendered is not None
    tree = ast.parse(rendered)
    target = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                  and node.name == "TargetApp")
    assert {node.name for node in target.body if isinstance(node, ast.FunctionDef)} >= {
        "app_context", "test_client", "test_cli_runner",
    }
    assert "from pkg.commands import register_commands as _register_cli_0" in rendered

    seam_plan = recipe.build_seam_plan(
        files, [*planned, runtime, artifact], {}, [],
    )
    assert seam_plan["decisions"]["ambient_context_runtime"]["test_providers"] == [
        "pkg/testing.py"
    ]
    normalized = recipe.normalize_generated(
        "pkg/__init__.py",
        "from starlette.middleware.sessions import SessionMiddleware\n"
        "from pkg.commands import register_commands\n"
        "from pkg.context import RuntimeContext\n"
        "def create_app():\n"
        "    app = TargetApp()\n"
        "    register_commands(app)\n"
        "    app.add_middleware(SessionMiddleware, secret_key='dev')\n"
        "    app.add_middleware(RuntimeContext)\n"
        "    @app.middleware('http')\n"
        "    async def inject(request, call_next):\n"
        "        return await call_next(request)\n"
        "    return app\n",
        seam_plan,
    )
    assert "register_commands(app)" not in normalized
    assert normalized.index("add_middleware(RuntimeContext)") < normalized.index(
        "add_middleware(SessionMiddleware"
    )
    assert normalized.index("async def inject") < normalized.index(
        "add_middleware(SessionMiddleware"
    )


def test_module_bound_cli_registrar_is_removed_and_preserves_teardown(tmp_path):
    files = {
        "pkg/db.py": (
            "from flask import current_app, g\n"
            "def get_db(): return g.db\n"
            "def get_db_dep(): yield get_db()\n"
            "def close_db(error=None): g.pop('db', None)\n"
        ),
        "pkg/commands.py": (
            "import click\n"
            "from .db import close_db\n"
            "@click.command('init-db')\n"
            "def init_db_command(): pass\n"
            "def init_app(app):\n"
            "    app.teardown_appcontext(close_db)\n"
            "    app.cli.add_command(init_db_command)\n"
        ),
        "pkg/__init__.py": (
            "from flask import Flask\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    from . import commands\n"
            "    commands.init_app(app)\n"
            "    return app\n"
        ),
        "tests/conftest.py": "def runner(app): return app.test_cli_runner()\n",
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])

    factory = plan["decisions"]["application_factory:pkg/__init__.py"]
    assert factory["cleanup_callbacks"] == [{
        "provider": "pkg/db.py", "functions": ["close_db"],
    }]
    generated = (
        "from pkg.testing import TargetApp\n"
        "def create_app():\n"
        "    app = TargetApp()\n"
        "    from . import commands\n"
        "    commands.init_app(app)\n"
        "    return app\n"
    )
    plan["decisions"]["surface"] = {
        "kind": "planned_test_surface", "provider": "pkg/testing.py",
        "factory_consumers": ["pkg/__init__.py"],
        "classes": [{"name": "TargetApp", "members": ["app_context"]}],
        "files": ["pkg/__init__.py"], "instruction": "owned facade",
    }

    normalized = recipe.normalize_generated("pkg/__init__.py", generated, plan)

    assert "commands.init_app(app)" not in normalized
    assert "cleanup_callbacks=(_portage_cleanup_0,)" in normalized
    assert "from pkg.db import close_db as _portage_cleanup_0" in normalized


def test_initialized_sqlalchemy_facade_cleans_scoped_session_on_app_context_exit():
    plan = {"decisions": {
        "factory": {
            "kind": "application_factory", "factory": "pkg/app.py",
            "files": ["pkg/app.py", "pkg/extensions.py"],
            "initializers": [{
                "provider": "pkg/extensions.py", "symbol": "db",
                "original_call": "db.init_app(app)",
            }],
            "cleanup_callbacks": [],
        },
        "database": {
            "kind": "extension_provider", "provider": "pkg/extensions.py",
            "symbol": "db", "members": ["init_app", "session"],
            "files": ["pkg/app.py", "pkg/extensions.py"],
        },
        "surface": {
            "kind": "planned_test_surface", "provider": "pkg/testing.py",
            "factory_consumers": ["pkg/app.py"],
            "classes": [{"name": "TargetApp", "members": ["app_context"]}],
            "files": ["pkg/app.py"],
        },
    }}
    generated = (
        "from pkg.extensions import db\n"
        "from pkg.testing import TargetApp\n"
        "def create_app():\n"
        "    app = TargetApp()\n"
        "    db.init_app(app)\n"
        "    return app\n"
    )

    normalized = recipe.normalize_generated("pkg/app.py", generated, plan)

    assert "cleanup_callbacks=(db.session.remove,)" in normalized


def test_module_owned_extension_exception_is_not_laundered_through_provider():
    plan = {"decisions": {"csrf": {
        "kind": "provider_protocol", "provider": "pkg/extensions.py",
        "symbol": "csrf", "exception_members": ["CSRFError"],
        "files": ["pkg/extensions.py", "pkg/app.py"],
    }}}
    generated = (
        "from fastapi import FastAPI\n"
        "from pkg.extensions import csrf\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    @app.exception_handler(csrf.CSRFError)\n"
        "    async def handle(request, error): return None\n"
        "    return app\n"
    )

    normalized = recipe.normalize_generated("pkg/app.py", generated, plan)

    assert "from pkg.extensions import CSRFError" in normalized
    assert "app.exception_handler(CSRFError)" in normalized
    assert "csrf.CSRFError" not in normalized


def test_factory_instance_path_join_accepts_string_directory():
    plan = {"decisions": {"factory": {
        "kind": "application_factory", "factory": "pkg/__init__.py",
        "app_state_members": ["instance_path"], "files": ["pkg/__init__.py"],
        "instruction": "preserve instance path",
    }}}
    generated = (
        "from fastapi import FastAPI\n"
        "import tempfile\n"
        "def create_app():\n"
        "    instance_path = tempfile.mkdtemp()\n"
        "    app = FastAPI()\n"
        "    app.state.instance_path = instance_path\n"
        "    app.state.config = {'DATABASE': str(instance_path / 'app.sqlite')}\n"
        "    return app\n"
    )

    normalized = recipe.normalize_generated("pkg/__init__.py", generated, plan)

    assert "str(_PortagePath(instance_path) / 'app.sqlite')" in normalized
    assert "from pathlib import Path as _PortagePath" in normalized
    harness = recipe.normalize_generated(
        "tests/conftest.py",
        "from _portage_fastapi_test_compat import adapt_app\n"
        "def fixture():\n"
        "    app = create_app()\n"
        "    app = adapt_app(app, instance_path=app.instance_path)\n"
        "    return app.instance_path\n",
        plan,
    )
    assert "adapt_app(app, instance_path=app.state.instance_path)" in harness
    assert "return app.instance_path" in harness


def test_planned_provider_export_is_not_read_as_function_member():
    plan = {"decisions": {"provider": {
        "kind": "planned_provider_exports", "provider": "pkg/runtime.py",
        "exports": {
            "render": {"kind": "function", "members": []},
            "engine": {"kind": "variable", "members": []},
        },
        "files": ["pkg/runtime.py", "pkg/views.py"],
        "instruction": "consume frozen exports directly",
    }}}
    generated = (
        "from pkg.runtime import render\n"
        "engine = render.engine\n"
        "def view(): return engine\n"
    )

    normalized = recipe.normalize_generated("pkg/views.py", generated, plan)

    assert "engine as _portage_engine" in normalized
    assert "engine = _portage_engine" in normalized
    manifest = {
        "runtime::render": {
            "module": "pkg/runtime.py", "symbol": "render", "target_kind": "function",
            "members": [], "provenance": "planned_create",
        },
        "runtime::engine": {
            "module": "pkg/runtime.py", "symbol": "engine", "target_kind": "variable",
            "members": [], "provenance": "planned_create",
        },
    }
    assert planned_provider_import_violations(
        normalized, manifest, "pkg/views.py",
    ) == []
    assert any(
        "reads provider export `engine` as a member" in item
        for item in planned_provider_import_violations(
            generated, manifest, "pkg/views.py",
        )
    )


def test_factory_normalization_preserves_inherited_object_config_and_session_import():
    files = {
        "pkg/__init__.py": (
            "from flask import Flask\n"
            "from .settings import config\n"
            "def create_app(name='testing'):\n"
            "    app = Flask(__name__)\n"
            "    app.config.from_object(config[name])\n"
            "    return app\n"
        ),
        "pkg/settings.py": (
            "class Base: SECRET_KEY = 'dev'\n"
            "class Testing(Base): TESTING = True\n"
            "config = {'testing': Testing}\n"
        ),
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = (
        "from fastapi import FastAPI\n"
        "from fastapi.middleware.sessions import SessionMiddleware\n"
        "from .settings import config\n"
        "def create_app(name='testing'):\n"
        "    app = FastAPI()\n"
        "    app.state.config = dict(config[name].__dict__)\n"
        "    app.add_middleware(SessionMiddleware, secret_key='dev')\n"
        "    return app\n"
    )

    normalized = recipe.normalize_generated("pkg/__init__.py", generated, plan)

    assert "from starlette.middleware.sessions import SessionMiddleware" in normalized
    assert "__dict__" not in normalized
    assert "dir(config[name])" in normalized
    assert not any(
        "config.from_object settings" in item
        for item in framework_seam_violations(normalized, plan, "pkg/__init__.py")
    )


def test_factory_normalization_completes_partial_object_config_from_source_contract():
    files = {
        "pkg/app.py": (
            "from flask import Flask\n"
            "from .settings import Config\n"
            "def create_app(config_class=Config):\n"
            "    app = Flask(__name__)\n"
            "    app.config.from_object(config_class)\n"
            "    if app.config['MAIL_SERVER']:\n"
            "        pass\n"
            "    return app\n"
        ),
        "pkg/settings.py": "class Config:\n    MAIL_SERVER = None\n",
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = (
        "from fastapi import FastAPI\n"
        "from .settings import Config\n"
        "def create_app(config_class=Config):\n"
        "    app = FastAPI()\n"
        "    app.state.config = {'TESTING': False}\n"
        "    if app.testing:\n"
        "        pass\n"
        "    return app\n"
    )

    normalized = recipe.normalize_generated("pkg/app.py", generated, plan)

    assert "dir(config_class)" in normalized
    assert "'TESTING': False" in normalized
    assert "app.state.config.get('TESTING', False)" in normalized
    assert "app.testing" not in normalized
    assert not any(
        "omits original default keys" in item
        for item in framework_seam_violations(normalized, plan, "pkg/app.py")
    )

    update_only = generated.replace(
        "app.state.config = {'TESTING': False}",
        "app.state.config.update({'TESTING': False})",
    )
    normalized = recipe.normalize_generated("pkg/app.py", update_only, plan)
    assert "app.state.config = {_portage_config_key: getattr(config_class" in normalized
    assert normalized.index("app.state.config =") < normalized.index(
        "app.state.config.update"
    )


def test_extension_provider_uses_source_factory_database_config():
    files = {
        "pkg/__init__.py": (
            "from flask import Flask\n"
            "from .settings import config\n"
            "from .extensions import db\n"
            "def create_app(name='development'):\n"
            "    app = Flask(__name__)\n"
            "    app.config.from_object(config[name])\n"
            "    db.init_app(app)\n"
            "    return app\n"
        ),
        "pkg/settings.py": (
            "class Base: SECRET_KEY = 'dev'\n"
            "class Development(Base): SQLALCHEMY_DATABASE_URI = 'sqlite:///dev.db'\n"
            "class Testing(Base): SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'\n"
            "config = {'development': Development, 'testing': Testing}\n"
        ),
        "pkg/extensions.py": (
            "from flask_sqlalchemy import SQLAlchemy\n"
            "db = SQLAlchemy()\n"
        ),
        "tests/test_app.py": (
            "from pkg.extensions import db\n"
            "def test_database_lifecycle():\n"
            "    db.create_all()\n"
            "    db.drop_all()\n"
        ),
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    decision = plan["decisions"]["extension_provider:pkg/extensions.py:db"]
    assert decision["database_config"] == {
        "module": "pkg.settings", "symbol": "config", "default_key": "development",
        "sqlite": True,
    }
    generated = (
        "from sqlalchemy import create_engine\n"
        "from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker\n"
        "from pkg.settings import Base\n"
        "class Model(DeclarativeBase): pass\n"
        "def _create_engine(uri):\n"
        "    return create_engine(uri, connect_args={'check_same_thread': False})\n"
        "engine = _create_engine(Base.SQLALCHEMY_DATABASE_URI)\n"
        "SessionLocal = sessionmaker(bind=engine)\n"
        "class DB:\n"
        "    engine = engine\n"
        "    Model = Model\n"
        "    session = scoped_session(SessionLocal)\n"
        "    def create_all(self): Model.metadata.create_all(bind=engine)\n"
        "    def drop_all(self): Model.metadata.drop_all(bind=engine)\n"
        "    def init_app(self, app): pass\n"
        "db = DB()\n"
    )

    normalized = recipe.normalize_generated("pkg/extensions.py", generated, plan)

    assert "from pkg.settings import Base, config" in normalized
    assert "create_engine(config['development'].SQLALCHEMY_DATABASE_URI)" in normalized
    assert "from sqlalchemy.pool import StaticPool" in normalized
    assert "'check_same_thread': False" in normalized
    assert "'sqlite:///:memory:'" in normalized
    assert "_portage_create_engine(config['development'].SQLALCHEMY_DATABASE_URI)" in normalized
    assert (
        "_portage_create_engine(config['development'].SQLALCHEMY_DATABASE_URI, **"
        not in normalized
    )
    assert "uri = app.state.config.get('SQLALCHEMY_DATABASE_URI')" in normalized
    assert "provider.session.configure(bind=provider.engine)" in normalized
    assert "kwargs.setdefault('bind', self.engine)" in normalized
    assert "return self.metadata.create_all(*args, **kwargs)" in normalized
    assert "return self.metadata.drop_all(*args, **kwargs)" in normalized


def test_separate_source_database_provider_rebinds_to_runtime_config():
    files = {
        "pkg/extensions.py": (
            "from flask_sqlalchemy import SQLAlchemy\n"
            "db = SQLAlchemy()\n"
        ),
        "pkg/app.py": (
            "from flask import Flask\n"
            "from .extensions import db\n"
            "def create_app(config=None):\n"
            "    app = Flask(__name__)\n"
            "    app.config.update(config or {})\n"
            "    db.init_app(app)\n"
            "    return app\n"
        ),
        "tests/test_db.py": (
            "from pkg.extensions import db\n"
            "def test_db():\n"
            "    db.create_all()\n"
            "    db.session.commit()\n"
            "    db.drop_all()\n"
        ),
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    decision = plan["decisions"]["extension_provider:pkg/extensions.py:db"]
    assert decision["members"] == [
        "create_all", "drop_all", "init_app", "session",
    ]
    generated = (
        "from sqlalchemy import create_engine\n"
        "from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker\n"
        "class Base(DeclarativeBase): pass\n"
        "engine = create_engine('sqlite:///initial.db')\n"
        "SessionLocal = sessionmaker(bind=engine)\n"
        "class DBFacade:\n"
        "    Model = Base\n"
        "    engine = engine\n"
        "    session = scoped_session(SessionLocal)\n"
        "    def init_app(self): pass\n"
        "    def create_all(self): pass\n"
        "    def drop_all(self): pass\n"
        "db = DBFacade()\n"
    )

    normalized = recipe.normalize_generated("pkg/extensions.py", generated, plan)
    namespace = {}
    exec(normalized, namespace)
    db = namespace["db"]
    initial_engine = db.engine
    app = SimpleNamespace(state=SimpleNamespace(config={
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
    }))

    db.init_app(app)

    assert db.engine is not initial_engine
    assert str(db.engine.url) == "sqlite://"
    db.create_all()
    db.session.commit()
    db.drop_all()

    namespace_generated = (
        "from types import SimpleNamespace\n"
        "from sqlalchemy import create_engine\n"
        "from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker\n"
        "class Base(DeclarativeBase): pass\n"
        "engine = create_engine('sqlite:///initial.db')\n"
        "SessionLocal = sessionmaker(bind=engine)\n"
        "def init_app(app): session.configure(bind=create_engine('sqlite:///wrong.db'))\n"
        "db = SimpleNamespace(session=SessionLocal(), Model=Base, init_app=init_app, "
        "create_all=lambda: Base.metadata.create_all(bind=engine), "
        "drop_all=lambda: Base.metadata.drop_all(bind=engine))\n"
    )
    namespace_normalized = recipe.normalize_generated(
        "pkg/extensions.py", namespace_generated, plan,
    )
    assert "init_app=_portage_db_init_app" in namespace_normalized
    namespace = {}
    exec(namespace_normalized, namespace)
    namespace["db"].init_app(app)
    assert str(namespace["db"].engine.url) == "sqlite://"
    namespace["db"].create_all()
    namespace["db"].drop_all()


def test_runtime_rebound_database_provider_has_import_safe_bootstrap_engine():
    files = {
        "pkg/extensions.py": (
            "from flask_sqlalchemy import SQLAlchemy\n"
            "db = SQLAlchemy()\n"
        ),
        "pkg/app.py": (
            "from flask import Flask\n"
            "from .extensions import db\n"
            "def create_app(config=None):\n"
            "    app = Flask(__name__)\n"
            "    app.config.update(config or {})\n"
            "    db.init_app(app)\n"
            "    return app\n"
        ),
        "tests/test_db.py": (
            "from pkg.extensions import db\n"
            "def test_db(): db.create_all()\n"
        ),
    }
    planned = recipe.plan_files(files)
    plan = recipe.build_seam_plan(files, planned, {}, [])
    generated = (
        "from sqlalchemy import create_engine\n"
        "from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker\n"
        "def build_runtime_config(instance_path):\n"
        "    return {'SQLALCHEMY_DATABASE_URI': 'sqlite:///configured.db'}\n"
        "class Base(DeclarativeBase): pass\n"
        "engine = create_engine(build_runtime_config()['SQLALCHEMY_DATABASE_URI'])\n"
        "SessionLocal = sessionmaker(bind=engine)\n"
        "class DBFacade:\n"
        "    Model = Base\n"
        "    engine = engine\n"
        "    session = scoped_session(SessionLocal)\n"
        "    def init_app(self, app): pass\n"
        "    def create_all(self): pass\n"
        "db = DBFacade()\n"
    )

    normalized = recipe.normalize_generated("pkg/extensions.py", generated, plan)
    namespace = {}
    exec(normalized, namespace)

    assert "build_runtime_config()" not in normalized
    assert str(namespace["db"].engine.url) == "sqlite://"


def test_dynamic_class_lambda_methods_receive_the_bound_instance():
    files = {"pkg/__init__.py": (
        "from flask import Flask\n"
        "from flask_login import LoginManager\n"
        "login = LoginManager()\n"
        "def create_app(): return Flask(__name__)\n"
    )}
    manifest = {"pkg/__init__.py::login": {
        "module": "pkg/__init__.py", "symbol": "login",
        "target_kind": "variable", "original": "login = LoginManager()",
        "call_sites": ["@login.user_loader"],
    }}
    plan = recipe.build_seam_plan(
        files, [PlannedFile(path="pkg/__init__.py", role="app_factory")],
        manifest, [],
    )
    assert plan["decisions"][
        "application_factory:pkg/__init__.py"
    ]["instance_exports"] == [{
        "symbol": "login", "decorator_members": ["user_loader"],
    }]

    for facade in (
        "type('LoginManager', (), {'user_loader': lambda callback: callback})()",
        "type('LoginManager', (), {'user_loader': None})",
    ):
        generated = (
            f"login = {facade}\n"
            "@login.user_loader\n"
            "def load_user(user_id):\n"
            "    return user_id\n"
        )
        normalized = recipe.normalize_generated(
            "pkg/__init__.py", generated, plan,
        )
        namespace = {}
        exec(normalized, namespace)

        assert not isinstance(namespace["login"], type)
        assert namespace["load_user"]("7") == "7"


def test_direct_decorator_provider_protocol_is_binding_aware_and_realized():
    files = {
        "pkg/security.py": (
            "from flask_httpauth import HTTPTokenAuth\n"
            "from flask_babel import lazy_gettext as translate\n"
            "sentinel = HTTPTokenAuth()\n"
            "sentinel.label = translate('Access')\n"
            "@sentinel.verify\n"
            "def remember(value): return value\n"
        ),
        "pkg/views.py": (
            "from pkg.security import sentinel as shield\n"
            "@shield.guard\n"
            "def endpoint(): return 'ok'\n"
            "shield.init_app(object())\n"
        ),
    }
    planned = [
        _pf("pkg/security.py", "support", "auth_login"),
        _pf("pkg/views.py", "router", "route_to_endpoint"),
    ]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    protocol = plan["decisions"]["provider_protocol:pkg/security.py:sentinel"]
    assert protocol["decorator_members"] == ["guard", "verify"]
    assert protocol["callable_members"] == ["init_app"]
    assert protocol["attribute_values"] == {"label": "Access"}
    assert protocol["consumers"] == ["pkg/views.py"]

    generated = (
        "class Carrier:\n"
        "    label = translate('wrong')\n"
        "    def verify(self, callback): return None\n"
        "    def init_app(self, app): pass\n"
        "sentinel = Carrier()\n"
        "@sentinel.verify\n"
        "def remember(value): return value\n"
    )
    assert any(
        "missing callable" in item
        for item in framework_seam_violations(
            generated, plan, "pkg/security.py",
        )
    )

    normalized = recipe.normalize_generated("pkg/security.py", generated, plan)
    namespace = {}
    exec(normalized, namespace)
    assert type(namespace["sentinel"]).__name__ == "Carrier"
    assert namespace["sentinel"].label == "Access"
    assert namespace["remember"]("kept") == "kept"
    app = object()
    assert namespace["sentinel"].init_app(app) is app
    assert namespace["sentinel"]._apps == [(app, (), {})]
    assert framework_seam_violations(
        normalized, plan, "pkg/security.py",
    ) == []


def test_flask_login_surface_is_compiled_rendered_and_wired_from_source(tmp_path):
    files = {
        "pkg/extensions.py": (
            "from flask_login import LoginManager\n"
            "login_manager = LoginManager()\n"
            "@login_manager.user_loader\n"
            "def load_user(user_id): return object()\n"
            "login_manager.login_view = 'signin'\n"
        ),
        "pkg/views.py": (
            "from flask import Blueprint, render_template\n"
            "from flask_login import current_user, login_required, login_user, logout_user\n"
            "bp = Blueprint('main', __name__)\n"
            "def signin(user): login_user(user)\n"
            "def signout(): logout_user()\n"
            "@login_required\n"
            "def index(): return render_template('index.html', user=current_user)\n"
        ),
        "pkg/app.py": (
            "from flask import Flask\n"
            "def create_app(): return Flask(__name__)\n"
        ),
        "pkg/templates/index.html": "{% if current_user.is_authenticated %}yes{% endif %}",
    }
    for path, source in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    planned = recipe.plan_files(files)
    proposal = [
        {
            "path": "pkg/runtime.py", "role": "support", "purpose": "request state",
            "instructions": "own session state", "capabilities": ["session_and_flash"],
            "exports": [{
                "name": "Runtime", "kind": "class", "signature": "", "members": [],
            }],
            "consumers": ["pkg/app.py", "pkg/views.py"], "depends_on": [],
        },
        {
            "path": "pkg/authentication.py", "role": "support", "purpose": "auth",
            "instructions": "own authentication", "capabilities": ["authentication"],
            "exports": [{
                "name": "login_user", "kind": "function", "signature": "", "members": [],
            }],
            "consumers": ["pkg/views.py"], "depends_on": [],
        },
        {
            "path": "pkg/templating.py", "role": "support", "purpose": "templates",
            "instructions": "render templates", "capabilities": ["template_rendering"],
            "exports": [{
                "name": "render_template", "kind": "function", "signature": "", "members": [],
            }],
            "consumers": ["pkg/views.py"], "depends_on": [],
        },
    ]
    mixed = json.loads(json.dumps(proposal))
    next(
        item for item in mixed if "template_rendering" in item["capabilities"]
    )["capabilities"].append("session_and_flash")
    assert any(
        "keep the template provider standalone" in violation
        for violation in recipe.artifact_plan_violations(mixed, files, planned)
    )
    completed, _ = recipe.materialize_artifact_contracts(proposal, files, planned)
    artifacts = artifact_planned_files(completed)
    auth = next(item for item in artifacts if item.path == "pkg/authentication.py")
    template = next(item for item in artifacts if item.path == "pkg/templating.py")
    assert {item["name"] for item in auth.artifact_contract["exports"]} == {
        "current_user", "login_required", "login_user", "logout_user",
    }
    assert auth.artifact_contract["depends_on"] == ["pkg/runtime.py"]
    assert template.artifact_contract["depends_on"] == [
        "pkg/authentication.py", "pkg/runtime.py",
    ]

    seam_plan = recipe.build_seam_plan(files, [*planned, *artifacts], {}, [])
    rendered_auth = recipe.render_created_artifact(auth, str(tmp_path))
    assert rendered_auth is not None
    assert "current_user = _CurrentUserProxy()" in rendered_auth
    assert "async def wrapped_view(**kwargs)" in rendered_auth
    assert "inspect.signature(view, eval_str=True)" in rendered_auth
    assert "from pkg.extensions import load_user as _load_user_callback" in rendered_auth
    assert "loader = _load_user_callback" in rendered_auth

    combined = PlannedFile(
        path="pkg/combined_auth.py", role="support", action="create",
        artifact_contract={
            "capabilities": ["authentication", "session_and_flash"],
            "exports": [
                {
                    "name": "RequestContextMiddleware", "kind": "class",
                    "members": ["dispatch", "get_request_context", "manage_session"],
                },
                *[
                    {"name": name, "kind": kind, "members": []}
                    for name, kind in (
                        ("current_user", "variable"),
                        ("login_required", "function"),
                        ("login_user", "function"),
                        ("logout_user", "function"),
                        ("g", "variable"), ("session", "variable"),
                        ("flash", "function"),
                        ("get_flashed_messages", "function"),
                    )
                ],
            ],
            "consumers": ["pkg/views.py"], "depends_on": [],
        },
    )
    rendered_combined = recipe.render_created_artifact(combined, str(tmp_path))
    assert rendered_combined is not None
    assert "g = _GProxy()" in rendered_combined
    assert "session = _SessionProxy()" in rendered_combined
    assert "current_user = _CurrentUserProxy()" in rendered_combined
    assert "from pkg.combined_auth import g, session" not in rendered_combined

    generated_view = (
        "def current_user(): return None\n"
        "def login_required(view): return view\n"
        "def signin(user): login_user(user)\n"
        "def signout(): logout_user()\n"
    )
    normalized_view = recipe.normalize_generated(
        "pkg/views.py", generated_view, seam_plan,
    )
    assert "from pkg.authentication import" in normalized_view
    assert "def current_user" not in normalized_view
    assert "def login_required" not in normalized_view
    assert framework_seam_violations(
        normalized_view, seam_plan, "pkg/views.py",
    ) == []

    rendered_template = recipe.render_created_artifact(template, str(tmp_path))
    normalized_template = recipe.normalize_generated(
        "pkg/templating.py", rendered_template or "", seam_plan,
    )
    assert "from pkg.authentication import current_user" in normalized_template
    assert "'current_user': current_user" in normalized_template

    provider = recipe.normalize_generated(
        "pkg/extensions.py",
        "class Manager:\n"
        "    def user_loader(self, callback): return callback\n"
        "login_manager = Manager()\n"
        "@login_manager.user_loader\n"
        "def load_user(user_id): return 'wrong'\n",
        seam_plan,
    )
    assert "login_manager.login_view = 'signin'" in provider
    assert "def load_user(user_id):\n    return object()" in provider
    assert framework_seam_violations(
        provider, seam_plan, "pkg/extensions.py",
    ) == []

    class_object_provider = recipe.normalize_generated(
        "pkg/extensions.py", "login_manager = object\n", seam_plan,
    )
    assert framework_seam_violations(
        class_object_provider, seam_plan, "pkg/extensions.py",
    ) == []


def test_template_context_processors_are_preserved_as_app_owned_mappings():
    files = {
        "pkg/app.py": (
            "from flask import Flask\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    @app.context_processor\n"
            "    def provide_label():\n"
            "        return {'label': 'source'}\n"
            "    return app\n"
        ),
        "pkg/templates/index.html": "{{ label }}",
    }
    planned = [
        _pf("pkg/app.py", "app_factory", "app_factory"),
        PlannedFile(
            path="pkg/templating.py", role="support", action="create",
            artifact_contract={
                "path": "pkg/templating.py",
                "capabilities": ["template_rendering"],
                "exports": [{
                    "name": "render_template", "kind": "function",
                    "signature": "", "members": [],
                }],
                "consumers": ["pkg/app.py"], "depends_on": [],
            },
        ),
    ]
    plan = recipe.build_seam_plan(files, planned, {}, [])
    decision = plan["decisions"]["template_context_processors"]
    assert decision["processors"][0]["keys"] == ["label"]

    factory = recipe.normalize_generated(
        "pkg/app.py",
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    @app.get('/provide_label')\n"
        "    def provide_label(): return {'label': 'wrong'}\n"
        "    return app\n",
        plan,
    )
    assert "@app.get" not in factory
    assert "return {'label': 'source'}" in factory
    assert "app.state._portage_context_processors = (provide_label,)" in factory
    assert framework_seam_violations(factory, plan, "pkg/app.py") == []

    template = recipe.normalize_generated(
        "pkg/templating.py",
        "def render_template(request, template_name, **context):\n"
        "    values = {**context}\n"
        "    return templates.TemplateResponse(request, template_name, values)\n",
        plan,
    )
    assert "getattr(request.app.state, '_portage_context_processors', ())" in template
    assert "values.update(_portage_context_processor())" in template
    assert framework_seam_violations(template, plan, "pkg/templating.py") == []


def test_returned_lifecycle_contract_uses_source_names_and_real_object(tmp_path):
    files = {
        "pkg/app.py": (
            "from flask import Flask\n"
            "def build_site(): return Flask(__name__)\n"
        ),
        "checks/test_scope.py": (
            "from pkg.app import build_site\n"
            "class Case:\n"
            "    def setup(self):\n"
            "        self.site = build_site()\n"
            "        self.scope = self.site.session_scope()\n"
            "        self.scope.activate()\n"
            "    def teardown(self):\n"
            "        self.scope.deactivate()\n"
        ),
    }
    for path, source in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)

    planned = [_pf("pkg/app.py", "app_factory", "app_factory")]
    proposal = [
        {
            "path": "pkg/runtime.py", "role": "support",
            "purpose": "Own runtime state.", "instructions": "Own runtime state.",
            "capabilities": ["request_context"],
            "exports": [{
                "name": "RuntimeMiddleware", "kind": "class", "members": [],
            }],
            "consumers": ["pkg/app.py"], "depends_on": [],
        },
        {
            "path": "pkg/testing.py", "role": "support",
            "purpose": "Own the app test surface.",
            "instructions": "Own the app test surface.",
            "capabilities": ["direct_test_surface"],
            "exports": [{
                "name": "SiteFacade", "kind": "class", "members": [],
            }],
            "consumers": ["pkg/app.py"], "depends_on": [],
        },
    ]
    completed, _audit = recipe.materialize_artifact_contracts(
        proposal, files, planned,
    )
    testing = next(item for item in completed if item["path"] == "pkg/testing.py")
    assert testing["exports"][0]["members"] == ["session_scope"]
    assert testing["depends_on"] == ["pkg/runtime.py"]

    testing_file = PlannedFile(
        path=testing["path"], role=testing["role"], action="create",
        artifact_contract=testing,
    )
    rendered = recipe.render_created_artifact(testing_file, str(tmp_path))
    assert rendered is not None
    assert "def session_scope(self):" in rendered
    assert "def activate(self):" in rendered
    assert "def deactivate(self):" in rendered

    artifact_files = [
        PlannedFile(
            path=item["path"], role=item["role"], action="create",
            artifact_contract=item,
        )
        for item in completed
    ]
    seam_plan = recipe.build_seam_plan(
        files, [*planned, *artifact_files], {}, [],
    )
    assert framework_seam_violations(
        rendered, seam_plan, "pkg/testing.py",
    ) == []

    events = []
    executable = rendered.replace(
        "from pkg.runtime import _pop_context, _push_context",
        "def _push_context(state=None):\n"
        "    events.append('entered')\n"
        "    return len(events)\n"
        "def _pop_context(token):\n"
        "    events.append(('exited', token))",
    )
    namespace = {"events": events}
    exec(executable, namespace)
    app = namespace["SiteFacade"]()
    scope = app.session_scope()
    assert scope.activate() is app
    scope.deactivate()
    with app.session_scope() as entered:
        assert entered is app
    assert events == ["entered", ("exited", 1), "entered", ("exited", 3)]


def test_request_hook_materialization_cannot_reintroduce_provider_cycle():
    plan = {
        "decisions": {
            "application_factory:app/__init__.py": {
                "kind": "application_factory", "factory": "app/__init__.py",
                "files": ["app/__init__.py"], "config_from_objects": [],
            },
            "request_hooks:app/main/routes.py": {
                "kind": "request_hooks", "path": "app/main/routes.py",
                "files": ["app/main/routes.py", "app/__init__.py"],
                "hooks": [{
                    "function": "before_request", "scope": "before_app_request",
                }],
            },
            "extension_provider:app/__init__.py:db": {
                "kind": "extension_provider", "provider": "app/__init__.py",
                "symbol": "db", "files": ["app/__init__.py", "app/main/routes.py"],
                "consumers": ["app/main/routes.py"], "members": [],
            },
        },
    }
    generated = (
        "from fastapi import FastAPI\n"
        "class DBFacade: pass\n"
        "db = DBFacade()\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    return app\n"
    )

    normalized = recipe.normalize_generated("app/__init__.py", generated, plan)

    assert "    from app.main.routes import before_request" in normalized
    assert "\nfrom app.main.routes import before_request" not in normalized
    assert not any(
        "imports consumer" in item
        for item in framework_seam_violations(normalized, plan, "app/__init__.py")
    )


def test_factory_local_import_keeps_relative_src_layout_identity():
    path = "src/pkg/__init__.py"
    files = {path: (
        "from flask import Flask\n"
        "def create_app():\n"
        "    app = Flask(__name__)\n"
        "    from . import db\n"
        "    db.init_app(app)\n"
        "    return app\n"
    )}
    plan = recipe.build_seam_plan(
        files, [PlannedFile(path=path, role="app_factory")], {}, [],
    )
    generated = (
        "from fastapi import FastAPI\n"
        "from src.pkg import db\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    db.init_app(app)\n"
        "    return app\n"
    )

    normalized = recipe.normalize_generated(path, generated, plan)

    assert "    from . import db\n" in normalized
    assert "from src.pkg import db" not in normalized


def test_extension_mapping_is_realized_as_source_exercised_object_surface():
    plan = {"decisions": {"extension_provider:pkg/__init__.py:db": {
        "kind": "extension_provider", "provider": "pkg/__init__.py",
        "symbol": "db", "files": ["pkg/__init__.py", "pkg/models.py"],
        "consumers": ["pkg/models.py"],
        "members": ["Model", "event", "metadata", "session"],
    }}}
    generated = (
        "from sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "def SessionLocal(): return object()\n"
        "db = {'Base': Base, 'event': None, 'SessionLocal': SessionLocal}\n"
    )

    normalized = recipe.normalize_generated("pkg/__init__.py", generated, plan)

    assert "from types import SimpleNamespace" in normalized
    assert "Model=Base" in normalized
    assert "event=_portage_sqlalchemy_event" in normalized
    assert "metadata=Base.metadata" in normalized
    assert "session=scoped_session(SessionLocal)" in normalized
    assert not any(
        "missing source-exercised members" in item
        for item in framework_seam_violations(normalized, plan, "pkg/__init__.py")
    )

    class_normalized = recipe.normalize_generated(
        "pkg/__init__.py",
        "from sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "def SessionLocal(): return object()\n"
        "class DBFacade:\n"
        "    Base = Base\n"
        "    SessionLocal = SessionLocal\n"
        "db = DBFacade()\n",
        plan,
    )
    assert "Model = Base" in class_normalized
    assert "event = event" in class_normalized
    assert "metadata = Base.metadata" in class_normalized
    assert "session = scoped_session(SessionLocal)" in class_normalized
    assert not any(
        "missing source-exercised members" in item
        for item in framework_seam_violations(
            class_normalized, plan, "pkg/__init__.py",
        )
    )

    constructor_normalized = recipe.normalize_generated(
        "pkg/__init__.py",
        "from sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "def SessionLocal(): return object()\n"
        "class DBFacade:\n"
        "    def __init__(self, session_factory, model):\n"
        "        self.SessionLocal = session_factory\n"
        "        self.Base = model\n"
        "    def session(self): return self.SessionLocal()\n"
        "db = DBFacade(SessionLocal, Base)\n",
        plan,
    )
    assert "Model = Base" in constructor_normalized
    assert "session = scoped_session(SessionLocal)" in constructor_normalized
    assert not any(
        "missing source-exercised members" in item
        for item in framework_seam_violations(
            constructor_normalized, plan, "pkg/__init__.py",
        )
    )

    module_base_normalized = recipe.normalize_generated(
        "pkg/__init__.py",
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n"
        "class DBFacade:\n"
        "    def __init__(self): self.session = object()\n"
        "db = DBFacade()\n",
        plan,
    )
    assert "Model = Base" in module_base_normalized

    dynamic_normalized = recipe.normalize_generated(
        "pkg/__init__.py",
        "from sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "def SessionLocal(): return object()\n"
        "db = type('DBFacade', (), {'Base': Base, "
        "'SessionLocal': SessionLocal, 'event': None})\n",
        plan,
    )
    assert "db = SimpleNamespace(" in dynamic_normalized
    assert "Model=Base" in dynamic_normalized
    assert "metadata=Base.metadata" in dynamic_normalized

    plan["decisions"]["extension_provider:pkg/__init__.py:db"]["members"].extend(
        ["create_all", "drop_all"]
    )
    direct_namespace = recipe.normalize_generated(
        "pkg/__init__.py",
        "from sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "engine = object()\n"
        "def SessionLocal(): return object()\n"
        "db = SimpleNamespace(engine=engine, Base=Base, "
        "SessionLocal=SessionLocal, event=None, "
        "create_all=Base.metadata.create_all)\n",
        plan,
    )
    assert "from types import SimpleNamespace" in direct_namespace
    assert "Model=Base" in direct_namespace
    assert "event=_portage_sqlalchemy_event" in direct_namespace
    assert "metadata=Base.metadata" in direct_namespace
    assert "session=scoped_session(SessionLocal)" in direct_namespace
    assert "def _portage_db_create_all" in direct_namespace
    assert "def _portage_db_drop_all" in direct_namespace
    assert "create_all=Base.metadata.create_all" not in direct_namespace
    assert framework_seam_violations(
        direct_namespace, plan, "pkg/__init__.py",
    ) == []


def test_source_exercised_sqlalchemy_methods_work_on_both_facade_shapes():
    members = [
        "Model", "create_all", "drop_all", "first_or_404", "get_or_404",
        "init_app", "metadata", "paginate", "select", "session",
    ]
    plan = {"decisions": {"extension_provider:pkg/__init__.py:db": {
        "kind": "extension_provider", "provider": "pkg/__init__.py",
        "symbol": "db", "files": ["pkg/__init__.py", "pkg/models.py"],
        "consumers": ["pkg/models.py"], "members": members,
    }}}
    prefix = (
        "from sqlalchemy import create_engine, select\n"
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, "
        "sessionmaker\n"
        "class Base(DeclarativeBase): pass\n"
        "engine = create_engine('sqlite://')\n"
        "SessionLocal = sessionmaker(bind=engine)\n"
    )
    suffix = (
        "class Record(Base):\n"
        "    __tablename__ = 'record'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
    )
    generated = [
        prefix
        + "class DBFacade:\n"
        "    engine = engine\n"
        "    Base = Base\n"
        "    SessionLocal = SessionLocal\n"
        "db = DBFacade()\n"
        + suffix,
        prefix
        + "db = {'engine': engine, 'Base': Base, "
        "'SessionLocal': SessionLocal}\n"
        + suffix,
    ]

    for content in generated:
        normalized = recipe.normalize_generated("pkg/__init__.py", content, plan)
        normalized = recipe.normalize_generated(
            "pkg/__init__.py",
            normalized.replace("from sqlalchemy.pool import StaticPool\n", ""),
            plan,
        )
        assert "from sqlalchemy.pool import StaticPool" in normalized
        assert framework_seam_violations(
            normalized, plan, "pkg/__init__.py",
        ) == []
        namespace = {}
        exec(normalized, namespace)
        db = namespace["db"]
        record = namespace["Record"]
        db.create_all()
        db.session.add_all([record(id=1), record(id=2)])
        db.session.commit()

        assert db.get_or_404(record, 1).id == 1
        assert db.first_or_404(db.select(record).where(record.id == 2)).id == 2
        first = db.paginate(db.select(record).order_by(record.id), page=1, per_page=1)
        second = db.paginate(db.select(record).order_by(record.id), page=2, per_page=1)
        assert [item.id for item in first.items] == [1]
        assert (first.total, first.pages, first.has_next, first.next_num) == (
            2, 2, True, 2,
        )
        assert (second.has_prev, second.prev_num) == (True, 1)
        try:
            db.get_or_404(record, 99)
        except namespace["HTTPException"] as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("get_or_404 returned instead of raising")

        original_engine = db.engine
        state = type("State", (), {"config": {
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        }})()
        app = type("App", (), {"state": state})()
        db.init_app(app)
        assert db.engine is not original_engine
        assert str(db.engine.url) == "sqlite:///:memory:"
        db.session.remove()


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
        }, {
            "name": "render_template", "kind": "function",
            "signature": "def render_template(template_name)", "members": [],
        }],
    }
    completed, audit = recipe.materialize_artifact_contracts(
        [artifact], files, planned,
    )
    assert completed[0]["exports"] == [{
        "name": "render_template", "kind": "function", "signature": "",
        "members": [],
    }]
    assert audit[0]["removed_exports"] == [
        {"name": "TemplateRenderer", "kind": "class"},
    ]
    assert audit[0]["normalized_signatures"] == ["render_template"]
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
