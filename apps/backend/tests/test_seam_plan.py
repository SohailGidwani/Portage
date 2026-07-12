"""R1.1: deterministic framework-seam decisions and selective coupled units."""

import json
from pathlib import Path

from portage_agent.agent.nodes.common import (
    build_manifest,
    build_migration_units,
    dependency_order,
)
from portage_agent.agent.nodes.execute import framework_seam_violations, seam_sections
from portage_agent.agent.nodes.plan import complete_unit_dependencies
from portage_agent.recipes.base import PlannedFile, Subtask
from portage_agent.recipes.flask_to_fastapi import recipe


def _pf(path: str, role: str, *subtasks: str) -> PlannedFile:
    return PlannedFile(
        path=path,
        role=role,
        subtasks=[Subtask(s, s, "instruction") for s in subtasks],
    )


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


def test_seam_prompt_for_cluster_member_rejects_invented_app_apis():
    units = build_migration_units(FILES, PLANNED, MANIFEST)
    plan = recipe.build_seam_plan(FILES, PLANNED, MANIFEST, units)
    section = seam_sections(plan, "tests/conftest.py")
    assert "FRAMEWORK SEAM DECISIONS" in section
    assert "Do not invent" in section
    assert "app.state" in section
    assert "COUPLED MIGRATION UNIT" in section


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
