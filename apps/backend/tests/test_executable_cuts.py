"""Executable-cut analysis from generic framework-contract edges."""

import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from portage_agent.agent.nodes.common import dependency_order
from portage_agent.agent.nodes.executable_cut import (
    build_executable_cut_analysis,
    merge_small_cuts_into_units,
)
from portage_agent.recipes.base import PlannedFile, Subtask

plan_module = importlib.import_module("portage_agent.agent.nodes.plan")


def _pf(path: str, role: str, *subtasks: str, order: int = 100) -> PlannedFile:
    return PlannedFile(
        path=path,
        role=role,
        subtasks=[Subtask(name, name, "instruction") for name in subtasks],
        order=order,
    )


def _ordered(files: dict[str, str], planned: list[PlannedFile]) -> list[PlannedFile]:
    return dependency_order(files, planned)


def _write_factory_fixture(root) -> None:
    package = root / "pkg"
    tests = root / "tests"
    package.mkdir()
    tests.mkdir()
    (package / "views.py").write_text(
        "from flask import Blueprint\n"
        "bp = Blueprint('items', __name__)\n"
        "@bp.get('/items')\n"
        "def items():\n    return []\n"
    )
    (package / "__init__.py").write_text(
        "from flask import Flask\n"
        "from .views import bp\n"
        "def create_app():\n"
        "    app = Flask(__name__)\n"
        "    app.register_blueprint(bp)\n"
        "    return app\n"
    )
    (tests / "conftest.py").write_text(
        "import pytest\n"
        "from pkg import create_app\n"
        "@pytest.fixture\n"
        "def client():\n    return create_app().test_client()\n"
    )
    (tests / "test_items.py").write_text(
        "def test_items(client):\n"
        "    response = client.get('/items')\n"
        "    assert response.status_code == 200\n"
    )


def test_router_registrar_and_factory_harness_form_one_executable_cut():
    files = {
        "pkg/views.py": (
            "from flask import Blueprint\n"
            "bp = Blueprint('items', __name__)\n"
        ),
        "pkg/__init__.py": (
            "from flask import Flask\n"
            "from .views import bp as api_bp\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    app.register_blueprint(api_bp)\n"
            "    return app\n"
        ),
        "tests/conftest.py": (
            "from pkg import create_app\n"
            "def client():\n"
            "    return create_app().test_client()\n"
        ),
    }
    planned = _ordered(files, [
        _pf("pkg/views.py", "router", "blueprint_to_router", order=10),
        _pf("pkg/__init__.py", "app_factory", "app_factory", order=20),
        _pf("tests/conftest.py", "test_harness", "test_harness", order=30),
    ])

    analysis = build_executable_cut_analysis(
        files, planned, {}, {"tests/conftest.py": "adapter_wiring"},
    )

    json.dumps(analysis)
    assert analysis["cuts"] == [{
        "id": "executable-cut-1",
        "paths": ["pkg/views.py", "pkg/__init__.py", "tests/conftest.py"],
        "reason": "executable framework contracts: factory_harness, router_registration",
        "edge_kinds": ["factory_harness", "router_registration"],
        "mode": "coordinated",
    }]
    assert {edge["kind"] for edge in analysis["edges"]} == {
        "factory_harness", "router_registration",
    }


def test_module_alias_attribute_registration_is_binding_aware():
    files = {
        "pkg/views.py": "from flask import Blueprint\nbp = Blueprint('v', __name__)\n",
        "pkg/app.py": (
            "from flask import Flask\n"
            "from . import views as api_views\n"
            "app = Flask(__name__)\n"
            "app.register_blueprint(api_views.bp)\n"
        ),
    }
    planned = _ordered(files, [
        _pf("pkg/views.py", "router", "blueprint_to_router", order=10),
        _pf("pkg/app.py", "app_factory", "app_factory", order=20),
    ])

    analysis = build_executable_cut_analysis(files, planned, {}, {})

    assert analysis["cuts"][0]["paths"] == ["pkg/views.py", "pkg/app.py"]
    edge = analysis["edges"][0]
    assert edge["kind"] == "router_registration"
    assert edge["operation"] == "register_blueprint"
    assert "api_views.bp" in edge["evidence"]


def test_blueprint_collection_and_loop_registration_form_an_indirect_edge():
    files = {
        "pkg/views.py": "from flask import Blueprint\nbp = Blueprint('v', __name__)\n",
        "pkg/app.py": (
            "from flask import Flask\n"
            "from .views import bp\n"
            "blueprints = [bp]\n"
            "app = Flask(__name__)\n"
            "for blueprint in blueprints:\n"
            "    app.register_blueprint(blueprint)\n"
        ),
    }
    planned = _ordered(files, [
        _pf("pkg/views.py", "router", "blueprint_to_router", order=10),
        _pf("pkg/app.py", "app_factory", "app_factory", order=20),
    ])

    analysis = build_executable_cut_analysis(files, planned, {}, {})

    assert analysis["cuts"][0]["paths"] == ["pkg/views.py", "pkg/app.py"]
    assert analysis["edges"][0]["operation"] == "register_blueprint_indirect"


def test_resource_router_factory_and_harness_are_one_cut():
    files = {
        "pkg/db.py": (
            "from flask import current_app, g\n"
            "def get_db():\n    return g.db\n"
            "def init_app(app):\n    app.teardown_appcontext(lambda: None)\n"
        ),
        "pkg/views.py": (
            "from flask import Blueprint\n"
            "from .db import get_db\n"
            "bp = Blueprint('v', __name__)\n"
            "@bp.get('/items')\n"
            "def items():\n    return list(get_db().execute('select 1'))\n"
        ),
        "pkg/__init__.py": (
            "from flask import Flask\n"
            "from . import db\n"
            "from .views import bp\n"
            "def create_app():\n"
            "    app = Flask(__name__)\n"
            "    db.init_app(app)\n"
            "    app.register_blueprint(bp)\n"
            "    return app\n"
        ),
        "tests/conftest.py": (
            "from pkg import create_app\n"
            "def app():\n    return create_app()\n"
        ),
    }
    planned = _ordered(files, [
        _pf("pkg/db.py", "support", "request_context", order=15),
        _pf("pkg/views.py", "router", "blueprint_to_router", order=10),
        _pf("pkg/__init__.py", "app_factory", "app_factory", order=20),
        _pf("tests/conftest.py", "test_harness", "test_harness", order=30),
    ])
    manifest = {
        "pkg/db.py::get_db": {
            "module": "pkg/db.py", "symbol": "get_db",
            "additional_exports": ["get_db_dep"],
        },
    }

    analysis = build_executable_cut_analysis(
        files, planned, manifest, {"tests/conftest.py": "adapter_wiring"},
    )

    assert analysis["cuts"][0]["paths"] == [
        "pkg/db.py", "pkg/views.py", "pkg/__init__.py", "tests/conftest.py",
    ]
    assert set(analysis["cuts"][0]["edge_kinds"]) == {
        "extension_initialization", "factory_harness", "factory_provider_call",
        "framework_state", "resource_lifecycle", "router_registration",
    }


def test_cli_provider_and_harness_form_a_contract_edge():
    files = {
        "pkg/commands.py": (
            "import click\n"
            "@click.command('init-db')\n"
            "def init_db_command():\n    pass\n"
        ),
        "tests/conftest.py": (
            "from pkg.commands import init_db_command\n"
            "def commands():\n    return {'init-db': init_db_command}\n"
        ),
    }
    planned = [
        _pf("pkg/commands.py", "support", order=10),
        _pf("tests/conftest.py", "test_harness", "test_harness", order=20),
    ]

    analysis = build_executable_cut_analysis(files, planned, {}, {})

    assert analysis["cuts"][0]["edge_kinds"] == ["cli_harness"]


def test_stateful_decorator_reference_creates_edge_without_a_call():
    files = {
        "pkg/auth.py": (
            "from flask import session\n"
            "def login_required(function):\n    return function\n"
        ),
        "pkg/views.py": (
            "from flask import Blueprint\n"
            "from .auth import login_required\n"
            "bp = Blueprint('v', __name__)\n"
            "@login_required\n"
            "def private():\n    return 'ok'\n"
        ),
    }
    planned = _ordered(files, [
        _pf("pkg/auth.py", "support", "auth_login", order=10),
        _pf("pkg/views.py", "router", "blueprint_to_router", order=20),
    ])

    analysis = build_executable_cut_analysis(files, planned, {}, {})

    assert analysis["cuts"][0]["paths"] == ["pkg/auth.py", "pkg/views.py"]
    assert analysis["cuts"][0]["edge_kinds"] == ["framework_state"]


def test_large_component_is_an_explicit_batch_only_cut():
    files = {
        f"pkg/view{i}.py": (
            "from flask import Blueprint\n"
            f"bp{i} = Blueprint('v{i}', __name__)\n"
        )
        for i in range(5)
    }
    imports = "".join(
        f"from .view{i} import bp{i}\n" for i in range(5)
    )
    registrations = "".join(
        f"    app.register_blueprint(bp{i})\n" for i in range(5)
    )
    files["pkg/__init__.py"] = (
        "from flask import Flask\n" + imports
        + "def create_app():\n    app = Flask(__name__)\n"
        + registrations + "    return app\n"
    )
    planned = _ordered(files, [
        *[_pf(f"pkg/view{i}.py", "router", "blueprint_to_router", order=10)
          for i in range(5)],
        _pf("pkg/__init__.py", "app_factory", "app_factory", order=20),
    ])

    analysis = build_executable_cut_analysis(files, planned, {}, {})

    assert analysis["cuts"][0]["mode"] == "batch_only"
    assert len(analysis["cuts"][0]["paths"]) == 6
    assert analysis["diagnostics"][0]["kind"] == "large_executable_cut"
    assert "bounded component recovery" in analysis["diagnostics"][0]["next_need"]


def test_small_cut_absorbs_overlapping_legacy_unit_in_dependency_order():
    units = [{
        "id": "framework-seam-1",
        "paths": ["db.py", "app.py", "conftest.py"],
        "reason": "resource seam",
    }]
    cuts = [{
        "id": "executable-cut-1",
        "paths": ["db.py", "views.py", "app.py", "conftest.py"],
        "reason": "executable framework contracts: resource_lifecycle",
        "mode": "coordinated",
    }]

    assert merge_small_cuts_into_units(units, cuts) == [{
        "id": "coordinated-unit-1",
        "paths": ["db.py", "views.py", "app.py", "conftest.py"],
        "reason": "executable framework contracts: resource_lifecycle",
    }]


def test_framework_neutral_import_does_not_force_a_cut():
    files = {
        "pkg/store.py": "def list_items():\n    return []\n",
        "pkg/views.py": "from .store import list_items\ndef items():\n    return list_items()\n",
    }
    planned = [
        _pf("pkg/store.py", "support", order=10),
        _pf("pkg/views.py", "router", "blueprint_to_router", order=20),
    ]

    analysis = build_executable_cut_analysis(files, planned, {}, {})

    assert analysis["edges"] == []
    assert analysis["cuts"] == []
    assert analysis["diagnostics"] == []


def test_unsupported_oracle_file_cannot_join_an_executable_cut():
    files = {
        "pkg/auth.py": (
            "from flask import session\n"
            "def current_user():\n    return session.get('user_id')\n"
        ),
        "tests/test_auth.py": (
            "from flask import session\n"
            "from pkg.auth import current_user\n"
            "def test_user():\n    assert current_user() == session.get('user_id')\n"
        ),
    }
    planned = _ordered(files, [
        _pf("pkg/auth.py", "support", "auth_login", order=10),
        _pf("tests/test_auth.py", "test_harness", "test_harness", order=20),
    ])

    analysis = build_executable_cut_analysis(
        files, planned, {}, {"tests/test_auth.py": "unsupported_test_seam"},
    )

    assert analysis["edges"] == []
    assert analysis["cuts"] == []
    assert analysis["diagnostics"] == []


@pytest.mark.asyncio
async def test_plan_checkpoints_cut_and_uses_it_as_coordinated_unit(tmp_path, monkeypatch):
    _write_factory_fixture(tmp_path)

    captured: list[dict] = []

    async def save_plan(_job_id, specs):
        captured.extend(specs)
        return [
            SimpleNamespace(to_state_dict=lambda spec=spec: spec)
            for spec in specs
        ]

    monkeypatch.setattr(plan_module.task_store, "save_plan", save_plan)
    monkeypatch.setattr(plan_module, "ensure_worktree", AsyncMock())

    result = await plan_module.plan_node({
        "job_id": "00000000-0000-0000-0000-000000000042",
        "workspace": str(tmp_path),
        "migration_recipe": "flask_to_fastapi",
        "graph_summary": {"total_nodes": 0},
        "config": {},
    })

    assert result["seam_plan"]["version"] == 2
    assert result["seam_plan"]["execution_cuts"] == [{
        "id": "executable-cut-1",
        "paths": ["pkg/views.py", "pkg/__init__.py", "tests/conftest.py"],
        "reason": "executable framework contracts: factory_harness, router_registration",
        "edge_kinds": ["factory_harness", "router_registration"],
        "mode": "coordinated",
    }]
    assert result["seam_plan"]["units"][0]["paths"] == [
        "pkg/views.py", "pkg/__init__.py", "tests/conftest.py",
    ]
    assert result["test_compat_path"] == "_portage_fastapi_test_compat.py"
    assert captured[0]["type"] == "test_compat"
    assert captured[0]["verify_spec"]["origin"] == "infrastructure"


@pytest.mark.asyncio
async def test_replan_grows_cut_without_retaining_overlapping_stale_unit(
    tmp_path, monkeypatch,
):
    _write_factory_fixture(tmp_path)

    async def snapshots(_job_id, specs):
        return [
            SimpleNamespace(to_state_dict=lambda spec=spec: spec)
            for spec in specs
        ]

    monkeypatch.setattr(plan_module.task_store, "save_plan", snapshots)
    monkeypatch.setattr(plan_module.task_store, "append_tasks", snapshots)
    monkeypatch.setattr(plan_module, "ensure_worktree", AsyncMock())
    base_state = {
        "job_id": "00000000-0000-0000-0000-000000000043",
        "workspace": str(tmp_path),
        "migration_recipe": "flask_to_fastapi",
        "graph_summary": {"total_nodes": 0},
        "config": {"inject_fault": "drop_task"},
    }

    initial = await plan_module.plan_node(base_state)
    assert initial["seam_plan"]["execution_cuts"][0]["paths"] == [
        "pkg/__init__.py", "tests/conftest.py",
    ]

    replanned = await plan_module.plan_node({
        **base_state,
        **initial,
        "replan_requested": True,
    })

    assert replanned["seam_plan"]["execution_cuts"][0]["paths"] == [
        "pkg/views.py", "pkg/__init__.py", "tests/conftest.py",
    ]
    units = replanned["seam_plan"]["units"]
    assert len(units) == 1
    assert units[0]["paths"] == [
        "pkg/views.py", "pkg/__init__.py", "tests/conftest.py",
    ]
