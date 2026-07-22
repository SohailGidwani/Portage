"""Strict schema and reconstruction for artifact-producing migration plans."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import portage_agent.agent.nodes.plan as plan_module
import portage_agent.agent.nodes.report as report_module
from portage_agent.agent.nodes.artifact_plan import (
    artifact_planned_files,
    parse_artifact_plan,
)
from portage_agent.agent.nodes.common import (
    build_manifest,
    create_cut_checkpoint,
    dependency_order,
    new_import_cycle_violations,
)
from portage_agent.agent.nodes.executable_cut import build_executable_cut_analysis
from portage_agent.agent.nodes.execute import (
    _validated_deterministic_artifacts,
    contract_sections,
    contract_violations,
    planned_provider_import_violations,
)
from portage_agent.recipes.base import PlannedFile, Subtask
from portage_agent.recipes.flask_to_fastapi import FlaskToFastAPIRecipe


def _valid(**overrides):
    item = {
        "path": "pkg/compat.py",
        "role": "support",
        "purpose": "Own the target framework compatibility surface.",
        "instructions": "Implement the declared exports without importing Flask.",
        "capabilities": ["direct_test_surface"],
        "exports": [{
            "name": "CompatApp", "kind": "class",
            "members": ["test_client"],
        }],
        "consumers": ["pkg/app.py"],
        "depends_on": [],
    }
    item.update(overrides)
    return json.dumps([item])


def _parse(text):
    return parse_artifact_plan(
        text,
        existing_files={"pkg/app.py", "tests/test_app.py"},
        rewrite_paths={"pkg/app.py"},
    )


def test_invalid_deterministic_artifacts_fall_back_with_reportable_errors(tmp_path):
    planned = {
        "pkg/error.py": PlannedFile(path="pkg/error.py", role="support", action="create"),
        "pkg/invalid.py": PlannedFile(
            path="pkg/invalid.py", role="support", action="create",
        ),
    }

    def render(file, _worktree):
        if file.path == "pkg/error.py":
            raise RuntimeError("renderer failed")
        return "def incomplete(\n"

    rendered, rejected = _validated_deterministic_artifacts(
        planned, render, str(tmp_path), {}, {}, {},
    )

    assert rendered == {}
    assert rejected["pkg/error.py"] == ["RuntimeError: renderer failed"]
    assert rejected["pkg/invalid.py"]


def test_deterministic_artifacts_use_the_same_recipe_normalizer(tmp_path):
    planned = {
        "pkg/runtime.py": PlannedFile(
            path="pkg/runtime.py", role="support", action="create",
        ),
    }

    rendered, rejected = _validated_deterministic_artifacts(
        planned,
        lambda _file, _worktree: "value = 1\n",
        str(tmp_path),
        {},
        {},
        {},
        lambda _path, content, _seams: content.replace("1", "2"),
    )

    assert rejected == {}
    assert rendered == {"pkg/runtime.py": "value = 2\n"}


def test_consumer_cannot_import_an_undeclared_planned_provider_symbol():
    manifest = {
        "pkg/context.py::g": {
            "module": "pkg/context.py", "symbol": "g",
            "provenance": "planned_create",
        },
    }

    assert planned_provider_import_violations(
        "from pkg.context import session\n", manifest, "pkg/auth.py",
    ) == [
        "pkg/auth.py:1: imports undeclared `session` from planned provider "
        "pkg/context.py; declare it in that provider's frozen exports or consume "
        "an existing export"
    ]
    assert planned_provider_import_violations(
        "import pkg.context as context\nprint(context.session)\n",
        manifest,
        "pkg/auth.py",
    )
    assert planned_provider_import_violations(
        "from pkg.context import g\nprint(g)\n", manifest, "pkg/auth.py",
    ) == []


def test_new_import_cycles_are_rejected_but_source_cycles_are_preserved():
    acyclic = {
        "pkg/a.py": "VALUE = 1\n",
        "pkg/b.py": "from pkg.a import VALUE\n",
    }
    cyclic = {
        "pkg/a.py": "from pkg.b import VALUE\n",
        "pkg/b.py": "from pkg.a import VALUE\n",
    }

    assert new_import_cycle_violations(acyclic, cyclic) == [
        "new runtime import cycle introduced: pkg/a.py -> pkg/b.py -> pkg/a.py"
    ]
    assert new_import_cycle_violations(cyclic, cyclic) == []

    lazy_source = {
        "pkg/a.py": "def load():\n    from pkg.b import VALUE\n",
        "pkg/b.py": "from pkg.a import load\nVALUE = 1\n",
    }
    assert new_import_cycle_violations(lazy_source, cyclic) == [
        "new runtime import cycle introduced: pkg/a.py -> pkg/b.py -> pkg/a.py"
    ]


def test_planned_function_signature_freezes_provider_call_shape(tmp_path):
    provider = PlannedFile(
        path="pkg/context.py", role="support", action="create",
        artifact_contract={
            "purpose": "shared context",
            "capabilities": ["request_context"],
            "exports": [{
                "name": "push_context", "kind": "function",
                "signature": "def push_context(state=None)", "members": [],
            }],
            "consumers": ["pkg/app.py"], "depends_on": [],
        },
    )
    manifest = build_manifest(str(tmp_path), [provider], [])

    assert any(
        "required arg count grew" in item
        for item in contract_violations(
            "def push_context(state):\n    return state\n",
            manifest,
            "pkg/context.py",
        )
    )
    assert contract_violations(
        "def push_context(state=None):\n    return state\n",
        manifest,
        "pkg/context.py",
    ) == []


FLASK_ARCHITECT_FILES = {
    "pkg/app.py": "from flask import Flask\ndef create_app(): return Flask(__name__)\n",
    "pkg/auth.py": (
        "from flask import Blueprint, g, render_template, session\n"
        "def login_required(fn): return fn\n"
        "def view(): return render_template('auth.html', user=g.user, sid=session.get('id'))\n"
    ),
    "pkg/blog.py": (
        "from flask import Blueprint, g, render_template, session\n"
        "def login_required(fn): return fn\n"
        "def view(): return render_template('blog.html', user=g.user, sid=session.get('id'))\n"
    ),
    "tests/test_runtime.py": (
        "from flask import g, session\n"
        "def test_runtime(app):\n"
        "    with app.app_context(): assert g is not None\n"
        "    assert app.test_client()\n"
    ),
}


def _flask_architect_proposal(*, include_authentication: bool) -> str:
    artifacts = [
        {
            "path": "pkg/runtime_context.py", "role": "support",
            "purpose": "Own request and session state.",
            "instructions": "Implement one shared request runtime.",
            "capabilities": ["request_context", "session_and_flash"],
            "exports": [{
                "name": "RuntimeMiddleware", "kind": "class", "members": [],
            }],
            "consumers": [], "depends_on": [],
        },
        {
            "path": "pkg/templating.py", "role": "support",
            "purpose": "Own template rendering.",
            "instructions": "Implement the frozen template functions.",
            "capabilities": ["template_rendering"],
            "exports": [{
                "name": "render_template", "kind": "function", "members": [],
            }],
            "consumers": [], "depends_on": ["pkg/runtime_context.py"],
        },
        {
            "path": "pkg/testing.py", "role": "support",
            "purpose": "Own the application test surface.",
            "instructions": "Implement the frozen app facade.",
            "capabilities": ["direct_test_surface", "test_context_surface"],
            "exports": [{
                "name": "CompatApp", "kind": "class", "members": ["test_client"],
            }],
            "consumers": [], "depends_on": ["pkg/runtime_context.py"],
        },
    ]
    if include_authentication:
        artifacts.append({
            "path": "pkg/authentication.py", "role": "support",
            "purpose": "Own shared authentication behavior.",
            "instructions": "Implement the frozen authentication surface.",
            "capabilities": ["authentication"],
            "exports": [{
                "name": "login_required", "kind": "function", "members": [],
            }],
            "consumers": [], "depends_on": ["pkg/runtime_context.py"],
        })
    return json.dumps(artifacts)


def test_test_facade_contract_prunes_unconsumed_function_exports():
    recipe = FlaskToFastAPIRecipe()
    planned = recipe.plan_files(FLASK_ARCHITECT_FILES)
    proposal = json.loads(_flask_architect_proposal(include_authentication=True))
    testing = next(
        item for item in proposal if "direct_test_surface" in item["capabilities"]
    )
    testing["exports"].append({
        "name": "invented_factory", "kind": "function", "members": [],
    })
    testing["exports"][0]["members"].append("invented_member")

    completed, audit = recipe.materialize_artifact_contracts(
        proposal, FLASK_ARCHITECT_FILES, planned,
    )

    testing = next(
        item for item in completed if "direct_test_surface" in item["capabilities"]
    )
    assert all(export["kind"] != "function" for export in testing["exports"])
    assert any(
        entry.get("removed_exports") == [{
            "name": "invented_factory", "kind": "function",
        }]
        for entry in audit
    )
    assert any(
        entry.get("removed_class_members") == [{
            "export": "CompatApp", "members": ["invented_member"],
        }]
        for entry in audit
    )
    assert recipe.artifact_plan_violations(
        completed, FLASK_ARCHITECT_FILES, planned,
    ) == []


def test_contract_compiler_completes_a_capability_owner_with_empty_exports():
    recipe = FlaskToFastAPIRecipe()
    planned = recipe.plan_files(FLASK_ARCHITECT_FILES)
    proposal = json.loads(_flask_architect_proposal(include_authentication=True))
    runtime = next(
        item for item in proposal if "session_and_flash" in item["capabilities"]
    )
    runtime["exports"] = []
    testing = next(
        item for item in proposal if "direct_test_surface" in item["capabilities"]
    )
    testing["exports"] = []

    completed, audit = plan_module._validate_artifact_plan(
        recipe, proposal, FLASK_ARCHITECT_FILES, planned,
    )

    runtime = next(
        item for item in completed if "session_and_flash" in item["capabilities"]
    )
    assert {item["name"] for item in runtime["exports"]} >= {
        "RequestContextMiddleware", "flash", "get_flashed_messages",
        "get_request_context", "manage_session", "session",
    }
    testing = next(
        item for item in completed if "direct_test_surface" in item["capabilities"]
    )
    facade = next(item for item in testing["exports"] if item["kind"] == "class")
    assert facade["name"] == "FastAPIApp"
    assert {"app_context", "test_client"} <= set(facade["members"])
    assert any(item["path"] == runtime["path"] for item in audit)


def test_contract_compiler_canonicalizes_recipe_owned_runtime_surface():
    recipe = FlaskToFastAPIRecipe()
    planned = recipe.plan_files(FLASK_ARCHITECT_FILES)
    proposal = json.loads(_flask_architect_proposal(include_authentication=True))
    runtime = next(
        item for item in proposal if "session_and_flash" in item["capabilities"]
    )
    runtime["exports"][0]["members"] = ["session", "user"]
    runtime["exports"].append({
        "name": "manage_session", "kind": "function",
        "signature": "def manage_session()", "members": [],
    })
    runtime["exports"].append({
        "name": "get_session", "kind": "function",
        "signature": "def get_session(request)", "members": [],
    })

    completed, audit = recipe.materialize_artifact_contracts(
        proposal, FLASK_ARCHITECT_FILES, planned,
    )

    runtime = next(
        item for item in completed if "session_and_flash" in item["capabilities"]
    )
    assert "get_session" not in {item["name"] for item in runtime["exports"]}
    runtime_class = next(item for item in runtime["exports"] if item["kind"] == "class")
    assert runtime_class["members"] == ["get_request_context", "manage_session"]
    manage_session = next(
        item for item in runtime["exports"] if item["name"] == "manage_session"
    )
    assert manage_session["signature"] == "def manage_session(request)"
    runtime_audit = next(item for item in audit if item["path"] == runtime["path"])
    assert runtime_audit["removed_exports"] == [{
        "name": "get_session", "kind": "function",
    }]
    assert runtime_audit["removed_class_members"] == [{
        "export": "RuntimeMiddleware", "members": ["session", "user"],
    }]
    assert "manage_session" in runtime_audit["normalized_signatures"]


def test_contract_compiler_prunes_unconsumed_test_context_exports():
    recipe = FlaskToFastAPIRecipe()
    planned = recipe.plan_files(FLASK_ARCHITECT_FILES)
    proposal = json.loads(_flask_architect_proposal(include_authentication=True))
    testing = next(
        item for item in proposal if "test_context_surface" in item["capabilities"]
    )
    testing["exports"].extend([
        {"name": "app_context", "kind": "variable", "members": []},
        {"name": "testing", "kind": "variable", "members": []},
    ])

    completed, audit = recipe.materialize_artifact_contracts(
        proposal, FLASK_ARCHITECT_FILES, planned,
    )

    testing = next(
        item for item in completed if "test_context_surface" in item["capabilities"]
    )
    assert {
        item["name"] for item in testing["exports"] if item["kind"] != "class"
    } == {"g", "session"}
    testing_audit = next(item for item in audit if item["path"] == testing["path"])
    assert {item["name"] for item in testing_audit["removed_exports"]} >= {
        "app_context", "testing",
    }


def test_valid_plan_is_normalized_and_rehydrates_create_task():
    plan = _parse(_valid())
    files = artifact_planned_files(plan)

    assert plan[0]["exports"][0] == {
        "name": "CompatApp", "kind": "class", "signature": "",
        "members": ["test_client"],
    }
    assert plan[0]["capabilities"] == ["direct_test_surface"]
    assert files[0].action == "create"
    assert files[0].artifact_contract["consumers"] == ["pkg/app.py"]
    assert files[0].verify_spec()["action"] == "create"


def test_empty_plan_is_valid():
    assert _parse("[]") == []


@pytest.mark.parametrize("text, match", [
    ("```json\n[]\n```", "strict JSON"),
    (json.dumps({}), "JSON list"),
    (_valid(path="../escape.py"), "repository-relative"),
    (_valid(path="pkg/app.py"), "collides"),
    (_valid(path="pkg/compat.txt"), "Python file"),
    (_valid(exports=[]), "at least one export"),
    (_valid(exports=[{"name": "not-valid", "kind": "function"}]), "identifier"),
    (_valid(exports=[{"name": "helper", "kind": "module"}]), "kind"),
    (_valid(exports=[{
        "name": "app_context", "kind": "class", "signature": "class AppContext",
    }]), "signature declares AppContext"),
    (_valid(consumers=["tests/test_app.py"]), "non-rewrite consumers"),
    (_valid(depends_on=["pkg/missing.py"]), "unknown dependencies"),
    (_valid(depends_on=["pkg/app.py"]), "declared consumers"),
])
def test_invalid_plan_is_rejected_atomically(text, match):
    with pytest.raises(ValueError, match=match):
        _parse(text)


def test_duplicate_paths_and_exports_are_rejected():
    item = json.loads(_valid())[0]
    with pytest.raises(ValueError, match="duplicate artifact paths"):
        _parse(json.dumps([item, item]))
    item["exports"].append(dict(item["exports"][0]))
    with pytest.raises(ValueError, match="duplicate export"):
        _parse(json.dumps([item]))


def test_plan_size_is_bounded():
    item = json.loads(_valid())[0]
    plan = [dict(item, path=f"pkg/compat_{index}.py") for index in range(5)]
    with pytest.raises(ValueError, match="maximum is 4"):
        _parse(json.dumps(plan))


def test_created_artifact_dependency_cycle_is_rejected():
    first = json.loads(_valid())[0]
    second = dict(first, path="pkg/runtime.py", consumers=[])
    first["depends_on"] = [second["path"]]
    second["depends_on"] = [first["path"]]

    with pytest.raises(ValueError, match="dependencies contain a cycle"):
        _parse(json.dumps([first, second]))


def test_created_artifact_contract_orders_and_cuts_before_consumer(tmp_path):
    source = "from flask import Flask\ndef create_app():\n    return Flask(__name__)\n"
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text(source)
    created = artifact_planned_files(_parse(_valid()))[0]
    consumer = PlannedFile(
        path="pkg/app.py", role="app_factory",
        subtasks=[Subtask("app_factory", "factory", "rewrite")], order=20,
    )

    ordered = dependency_order({"pkg/app.py": source}, [consumer, created])
    manifest = build_manifest(str(tmp_path), ordered, [])
    analysis = build_executable_cut_analysis(
        {"pkg/app.py": source}, ordered, manifest, {},
    )

    assert [item.path for item in ordered] == ["pkg/compat.py", "pkg/app.py"]
    pin = manifest["pkg/compat.py::CompatApp"]
    assert pin["provenance"] == "planned_create"
    assert pin["members"] == ["test_client"]
    assert pin["capabilities"] == ["direct_test_surface"]
    assert pin["factory_consumers"] == ["pkg/app.py"]
    assert pin["consumers"][0]["module"] == "pkg/app.py"
    assert analysis["cuts"][0]["paths"] == ["pkg/compat.py", "pkg/app.py"]
    assert analysis["cuts"][0]["edge_kinds"] == ["planned_artifact"]


def test_owned_capability_requires_manifest_owner_member_and_consumer():
    manifest = {
        "pkg/compat.py::CompatApp": {
            "module": "pkg/compat.py", "symbol": "CompatApp", "kind": "class",
            "target_kind": "class", "target_note": "owned compatibility app",
            "original": "planned target artifact", "shape": {},
            "preserve_shape": False, "additional_exports": [],
            "members": ["test_client"], "provenance": "planned_create",
            "capabilities": ["direct_test_surface"],
            "factory_consumers": ["pkg/app.py"],
            "consumers": [{
                "module": "pkg/app.py", "local": "CompatApp", "binding": "planned",
            }],
        },
        "pkg/app.py::app": {
            "module": "pkg/app.py", "symbol": "app", "kind": "variable",
            "target_kind": "variable", "target_note": "keep app export",
            "original": "app", "shape": {}, "preserve_shape": True,
            "additional_exports": [], "members": [], "consumers": [],
        },
    }
    seam_plan = {
        "project_modules": ["pkg", "pkg.app", "pkg.compat"],
        "project_roots": ["pkg"],
    }

    owner = "class CompatApp:\n    def test_client(self):\n        return object()\n"
    consumer = "from .compat import CompatApp\napp = CompatApp()\napp.test_client()\n"
    assert contract_violations(owner, manifest, "pkg/compat.py", seam_plan) == []
    assert contract_violations(consumer, manifest, "pkg/app.py", seam_plan) == []

    imported_only = (
        "from .compat import CompatApp\n"
        "unused = CompatApp()\napp = object()\n"
    )
    assert any(
        "never returns" in item
        for item in contract_violations(imported_only, manifest, "pkg/app.py", seam_plan)
    )

    missing = "class CompatApp:\n    pass\n"
    assert any(
        "missing planned capability members" in item
        for item in contract_violations(missing, manifest, "pkg/compat.py", seam_plan)
    )
    outsider = "app.test_client()\n"
    assert any(
        "forbidden" in item
        for item in contract_violations(outsider, manifest, "pkg/other.py", seam_plan)
    )
    delegated = (
        "class CompatApp:\n"
        "    def test_client(self):\n"
        "        return self._adapter.test_client()\n"
    )
    assert any(
        "unverified receiver" in item
        for item in contract_violations(delegated, manifest, "pkg/compat.py", seam_plan)
    )


def test_planned_class_members_include_instance_attributes_initialized_by_constructor():
    manifest = {
        "pkg/compat.py::CompatApp": {
            "module": "pkg/compat.py", "symbol": "CompatApp", "kind": "class",
            "target_kind": "class", "target_note": "owned compatibility app",
            "original": "planned target artifact", "shape": {},
            "preserve_shape": False, "additional_exports": [],
            "members": ["app", "client", "testing"], "provenance": "planned_create",
            "consumers": [],
        },
    }
    content = (
        "class CompatApp:\n"
        "    def __init__(self, app):\n"
        "        self.app = app\n"
        "        self.client: object = object()\n"
        "        self.testing = False\n"
    )

    assert contract_violations(content, manifest, "pkg/compat.py") == []


def test_planned_class_member_shapes_are_derived_from_consumer_use(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "app.py").write_text("app = object()\n")
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_app(app):\n"
        "    with app.test_client() as client:\n"
        "        assert client\n"
        "    assert app.testing\n"
    )
    owner = PlannedFile(
        path="pkg/compat.py", role="support", action="create",
        artifact_contract={
            "exports": [{
                "name": "CompatApp", "kind": "class",
                "members": ["test_client", "testing"],
            }],
            "capabilities": ["direct_test_surface"],
            "consumers": ["pkg/app.py"],
            "depends_on": [],
        },
    )
    planned = [owner, PlannedFile("pkg/app.py", "app_factory")]

    manifest = build_manifest(str(tmp_path), planned, [])
    pin = manifest["pkg/compat.py::CompatApp"]

    assert pin["member_shapes"] == {
        "test_client": "context_manager", "testing": "attribute",
    }
    prompt = contract_sections(manifest, "pkg/compat.py")
    assert "test_client=context_manager" in prompt
    assert "testing=attribute" in prompt


def test_context_manager_member_rejects_an_undecorated_generator():
    manifest = {
        "pkg/compat.py::CompatApp": {
            "module": "pkg/compat.py", "symbol": "CompatApp", "kind": "class",
            "target_kind": "class", "target_note": "owned compatibility app",
            "original": "planned target artifact", "shape": {},
            "preserve_shape": False, "additional_exports": [],
            "members": ["app_context"],
            "member_shapes": {"app_context": "context_manager"},
            "provenance": "planned_create", "consumers": [],
        },
    }
    wrong = "class CompatApp:\n    def app_context(self):\n        yield self\n"
    assert any(
        "generator implementation must use @contextmanager" in item
        for item in contract_violations(wrong, manifest, "pkg/compat.py")
    )
    correct = (
        "from contextlib import contextmanager\n"
        "class CompatApp:\n"
        "    @contextmanager\n"
        "    def app_context(self):\n"
        "        yield self\n"
    )
    assert contract_violations(correct, manifest, "pkg/compat.py") == []


def test_planned_class_member_shapes_reject_wrong_provider_realization():
    manifest = {
        "pkg/compat.py::CompatApp": {
            "module": "pkg/compat.py", "symbol": "CompatApp", "kind": "class",
            "target_kind": "class", "target_note": "owned compatibility app",
            "original": "planned target artifact", "shape": {},
            "preserve_shape": False, "additional_exports": [],
            "members": ["test_client", "testing"],
            "member_shapes": {"test_client": "method", "testing": "attribute"},
            "provenance": "planned_create", "consumers": [],
        },
    }
    wrong = (
        "class CompatApp:\n"
        "    def __init__(self, app):\n"
        "        self.test_client = app\n"
        "    def testing(self):\n"
        "        return False\n"
    )

    violations = contract_violations(wrong, manifest, "pkg/compat.py")

    assert any("CompatApp.test_client: consumer calls" in item for item in violations)
    assert any("CompatApp.testing: consumer reads" in item for item in violations)
    correct = (
        "class CompatApp:\n"
        "    def test_client(self):\n"
        "        return object()\n"
        "    @property\n"
        "    def testing(self):\n"
        "        return False\n"
    )
    assert contract_violations(correct, manifest, "pkg/compat.py") == []


def test_helper_class_consumer_does_not_require_public_factory_wiring():
    manifest = {
        "pkg/context.py::RequestContext": {
            "module": "pkg/context.py", "symbol": "RequestContext", "kind": "class",
            "target_kind": "class", "target_note": "own request context",
            "original": "planned target artifact", "shape": {},
            "preserve_shape": False, "additional_exports": [],
            "members": ["current"], "capabilities": ["request_context"],
            "factory_consumers": [], "provenance": "planned_create",
            "consumers": [{"module": "pkg/auth.py"}],
        },
    }
    consumer = "from .context import RequestContext\ncontext = RequestContext()\n"

    seam_plan = {
        "project_modules": ["pkg.auth", "pkg.context"], "project_roots": ["pkg"],
    }
    assert contract_violations(consumer, manifest, "pkg/auth.py", seam_plan) == []


def test_test_context_exports_must_be_runtime_backed_proxies():
    manifest = {
        "pkg/context.py::g": {
            "module": "pkg/context.py", "symbol": "g", "kind": "variable",
            "target_kind": "variable", "target_note": "runtime-backed g proxy",
            "original": "planned target artifact", "shape": {},
            "preserve_shape": False, "additional_exports": [], "members": [],
            "capabilities": ["test_context_surface"], "provenance": "planned_create",
            "consumers": [],
        },
    }

    assert any(
        "runtime-backed proxy" in item
        for item in contract_violations("g = {}\n", manifest, "pkg/context.py")
    )
    assert any(
        "function alias" in item
        for item in contract_violations(
            "def get_g(): pass\ng = get_g\n", manifest, "pkg/context.py",
        )
    )
    assert any(
        "one-time ContextVar snapshot" in item
        for item in contract_violations(
            "from contextvars import ContextVar\n"
            "_context = ContextVar('context', default={})\n"
            "g = _context.get()\n",
            manifest,
            "pkg/context.py",
        )
    )
    assert contract_violations(
        "class Proxy: pass\ng = Proxy()\n", manifest, "pkg/context.py",
    ) == []


def test_planned_provider_cannot_import_its_declared_consumer(tmp_path):
    source = "from flask import Flask\ndef create_app(): return Flask(__name__)\n"
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text(source)
    planned = artifact_planned_files(_parse(_valid()))
    manifest = build_manifest(str(tmp_path), planned, [])

    violations = contract_violations(
        "from .app import create_app\nclass CompatApp:\n"
        "    def test_client(self): return object()\n",
        manifest,
        "pkg/compat.py",
    )

    assert any("provider-first topology" in item for item in violations)
    assert any(
        "provider-first topology" in item
        for item in contract_violations(
            "class CompatApp:\n"
            "    def test_client(self):\n"
            "        from .app import create_app\n"
            "        return create_app()\n",
            manifest,
            "pkg/compat.py",
        )
    )


def test_unowned_same_project_import_is_rejected_before_sandbox():
    seam_plan = {
        "project_modules": ["watchlist", "watchlist.app"],
        "project_roots": ["watchlist"],
    }
    content = "from watchlist.utils import render\n"
    violations = contract_violations(content, {}, "watchlist/app.py", seam_plan)
    assert any("neither exists" in item for item in violations)

    relative = "from . import utils\nvalue = utils.render\n"
    violations = contract_violations(relative, {}, "watchlist/app.py", seam_plan)
    assert any("watchlist.utils" in item and "neither exists" in item for item in violations)

    seam_plan = {
        "project_modules": ["pkg", "pkg.email"], "project_roots": ["pkg"],
    }
    assert contract_violations(
        "from email.message import EmailMessage\n", {}, "pkg/email.py", seam_plan,
    ) == []


@pytest.mark.asyncio
async def test_architect_call_is_validated_accounted_and_frozen(tmp_path, monkeypatch):
    proposal = _valid()
    architect = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000099",
        status="pending", attempts=0,
        verify_spec={"kind": "architecture", "action": "architect"},
    )
    update = AsyncMock()
    monkeypatch.setattr(
        plan_module.task_store, "ensure_architect_task", AsyncMock(return_value=architect),
    )
    monkeypatch.setattr(plan_module.task_store, "update_task", update)
    monkeypatch.setattr(
        plan_module, "get_llm",
        lambda: SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(
            text=proposal, prompt_tokens=100, completion_tokens=50, cost_usd=0.0123456,
        ))),
    )

    class Recipe:
        @staticmethod
        def should_plan_artifacts(files, planned):
            return True

        @staticmethod
        def build_artifact_plan_prompt(**kwargs):
            return "plan artifacts"

        @staticmethod
        def artifact_plan_violations(plan, files, planned):
            return ["one owner is required"] if not plan else []

    planned = [PlannedFile(path="pkg/app.py", role="app_factory")]
    frozen, exists = await plan_module._plan_created_artifacts(
        job_id="00000000-0000-0000-0000-000000000098",
        recipe=Recipe(), files={"pkg/app.py": ""}, planned=planned,
        existing_plan=None, workspace=str(tmp_path),
    )

    assert exists is True
    assert frozen[0]["path"] == "pkg/compat.py"
    kwargs = update.await_args.kwargs
    model_label = (
        plan_module.settings.llm_driver_model_label
        or plan_module.settings.llm_driver_model
    )
    assert kwargs["status"] == "done"
    assert kwargs["verify_spec"]["artifact_plan"] == frozen
    assert kwargs["append_attempt"] == {
        "attempt": 1, "tier": "driver", "model": model_label,
        "action": "architect", "prompt_tokens": 100,
        "completion_tokens": 50, "cost_usd": 0.012346,
    }


@pytest.mark.asyncio
async def test_real_flask_policy_rejection_triggers_repair_after_materialization(
    tmp_path, monkeypatch,
):
    architect = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000089",
        status="pending", attempts=0,
        verify_spec={"kind": "architecture", "action": "architect"},
    )
    update = AsyncMock()
    complete = AsyncMock(side_effect=[
        SimpleNamespace(
            text=_flask_architect_proposal(include_authentication=False),
            prompt_tokens=100, completion_tokens=50, cost_usd=0.01,
        ),
        SimpleNamespace(
            text=_flask_architect_proposal(include_authentication=True),
            prompt_tokens=120, completion_tokens=60, cost_usd=0.02,
        ),
    ])
    monkeypatch.setattr(
        plan_module.task_store, "ensure_architect_task", AsyncMock(return_value=architect),
    )
    monkeypatch.setattr(plan_module.task_store, "update_task", update)
    monkeypatch.setattr(
        plan_module, "get_llm", lambda: SimpleNamespace(complete=complete),
    )
    recipe = FlaskToFastAPIRecipe()
    planned = recipe.plan_files(FLASK_ARCHITECT_FILES)

    frozen, _ = await plan_module._plan_created_artifacts(
        job_id="00000000-0000-0000-0000-000000000088",
        recipe=recipe, files=FLASK_ARCHITECT_FILES, planned=planned,
        existing_plan=None, workspace=str(tmp_path),
    )

    assert complete.await_count == 2
    runtime = next(
        item for item in frozen if "request_context" in item["capabilities"]
    )
    testing = next(
        item for item in frozen if "direct_test_surface" in item["capabilities"]
    )
    assert any("authentication" in item["capabilities"] for item in frozen)
    runtime_exports = {item["name"]: item for item in runtime["exports"]}
    testing_exports = {item["name"]: item for item in testing["exports"]}
    assert runtime_exports["g"]["kind"] == "variable"
    assert runtime_exports["session"]["kind"] == "variable"
    assert testing_exports["CompatApp"]["members"] == [
        "app_context", "test_client",
    ]
    entries = [call.kwargs["append_attempt"] for call in update.await_args_list]
    assert [entry["action"] for entry in entries] == [
        "architect", "architect_repair",
    ]
    assert "authentication requires exactly one owner" in entries[0]["error"]
    final = update.await_args.kwargs
    assert final["status"] == "done"
    assert final["attempts"] == 1
    assert final["verify_spec"]["contract_completion"]


@pytest.mark.asyncio
async def test_invalid_materializer_output_fails_loudly_without_repair(
    tmp_path, monkeypatch,
):
    architect = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000087",
        status="pending", attempts=0,
        verify_spec={"kind": "architecture", "action": "architect"},
    )
    update = AsyncMock()
    complete = AsyncMock(return_value=SimpleNamespace(
        text=_valid(), prompt_tokens=10, completion_tokens=5, cost_usd=0.01,
    ))
    monkeypatch.setattr(
        plan_module.task_store, "ensure_architect_task", AsyncMock(return_value=architect),
    )
    monkeypatch.setattr(plan_module.task_store, "update_task", update)
    monkeypatch.setattr(
        plan_module, "get_llm", lambda: SimpleNamespace(complete=complete),
    )

    class BrokenRecipe:
        @staticmethod
        def should_plan_artifacts(files, planned):
            return True

        @staticmethod
        def build_artifact_plan_prompt(**kwargs):
            return "plan artifacts"

        @staticmethod
        def materialize_artifact_contracts(plan, files, planned):
            plan[0]["consumers"] = ["pkg/not-planned.py"]
            return plan, []

    with pytest.raises(
        plan_module.ArtifactContractMaterializationError,
        match="materialization failed",
    ):
        await plan_module._plan_created_artifacts(
            job_id="00000000-0000-0000-0000-000000000086",
            recipe=BrokenRecipe(), files={"pkg/app.py": ""},
            planned=[PlannedFile(path="pkg/app.py", role="app_factory")],
            existing_plan=None, workspace=str(tmp_path),
        )

    assert complete.await_count == 1
    assert update.await_count == 1
    failed = update.await_args.kwargs
    assert failed["status"] == "failed"
    assert failed["verify_spec"]["artifact_plan"] == []
    assert failed["append_attempt"]["action"] == "architect"


@pytest.mark.asyncio
async def test_report_distinguishes_architect_calls_repairs_and_completion(
    tmp_path, monkeypatch,
):
    completion = [{
        "path": "pkg/testing.py",
        "capabilities": ["test_context_surface"],
        "added_consumers": ["tests/test_runtime.py"],
        "added_exports": [{"name": "g", "kind": "variable"}],
        "added_class_members": [],
    }]
    architect_state = {
        "type": "artifact_architect", "status": "done", "attempts": 1,
        "verify_spec": {"contract_completion": completion},
        "attempts_log": [
            {"action": "architect", "model": "GPT-4o", "cost_usd": 0.01},
            {"action": "architect_repair", "model": "GPT-4o", "cost_usd": 0.02},
        ],
    }
    artifact_state = {
        "type": "support", "target_path": "pkg/testing.py", "status": "done",
        "attempts": 1, "verify_spec": {}, "attempts_log": [],
    }
    snapshots = [
        SimpleNamespace(to_state_dict=lambda: architect_state),
        SimpleNamespace(to_state_dict=lambda: artifact_state),
    ]
    monkeypatch.setattr(
        report_module.task_store, "load_tasks", AsyncMock(return_value=snapshots),
    )
    put = AsyncMock(return_value=str(tmp_path / "report.json"))
    monkeypatch.setattr(
        report_module, "LocalStorage", lambda: SimpleNamespace(put=put),
    )
    artifact = json.loads(_valid(path="pkg/testing.py"))[0]

    await report_module.report_node({
        "job_id": "00000000-0000-0000-0000-000000000085",
        "migrate": True,
        "workspace": str(tmp_path),
        "worktree": str(tmp_path),
        "artifact_plan": [artifact],
        "integrate_summary": {"total": 1, "passed": 1, "failed": 0, "errors": 0},
    })

    report = json.loads(put.await_args.args[1])
    architect = report["artifact_plan"]["architect"]
    assert architect == {
        "status": "done", "attempts": 1, "calls": 2,
        "repairs": 1, "model": "GPT-4o",
    }
    assert report["artifact_plan"]["contract_completion"] == completion
    assert report["llm_usage"]["calls"] == 2
    assert report["tree_state"] == "migrated"
    assert report["test_summary"]["tree_state"] == "migrated"


def test_report_tree_classifier_detects_mixed_cut_lineage(tmp_path):
    source = tmp_path / "pkg.py"
    created = tmp_path / "provider.py"
    source.write_text("VALUE = 1\n")
    checkpoint = create_cut_checkpoint(
        str(tmp_path), ["pkg.py", "provider.py"], {
            "pkg.py": {"status": "pending", "action": "rewrite"},
            "provider.py": {"status": "pending", "action": "create"},
        },
    )
    created.write_text("PROVIDER = 1\n")

    assert report_module._checkpoint_tree_state(
        str(tmp_path), checkpoint,
    ) == "hybrid"


@pytest.mark.asyncio
async def test_replan_reuses_frozen_artifacts_without_model_call(tmp_path, monkeypatch):
    ensure = AsyncMock()
    monkeypatch.setattr(plan_module.task_store, "ensure_architect_task", ensure)

    class Recipe:
        @staticmethod
        def should_plan_artifacts(files, planned):
            return True

        @staticmethod
        def build_artifact_plan_prompt(**kwargs):
            raise AssertionError("frozen replan must not call the architect")

    frozen = json.loads(_valid())
    result, exists = await plan_module._plan_created_artifacts(
        job_id="00000000-0000-0000-0000-000000000097",
        recipe=Recipe(), files={"pkg/app.py": ""},
        planned=[PlannedFile(path="pkg/app.py", role="app_factory")],
        existing_plan=frozen, workspace=str(tmp_path),
    )

    assert result == frozen
    assert exists is True
    ensure.assert_awaited_once()


@pytest.mark.asyncio
async def test_replay_validates_frozen_artifacts_without_architect_call(
    tmp_path, monkeypatch,
):
    ensure = AsyncMock()
    monkeypatch.setattr(plan_module.task_store, "ensure_architect_task", ensure)

    class Recipe:
        @staticmethod
        def should_plan_artifacts(files, planned):
            return True

        @staticmethod
        def build_artifact_plan_prompt(**kwargs):
            raise AssertionError("replay must not call the architect")

        @staticmethod
        def artifact_plan_violations(plan, files, planned):
            return []

    frozen = json.loads(_valid())
    result, exists = await plan_module._plan_created_artifacts(
        job_id="00000000-0000-0000-0000-000000000096",
        recipe=Recipe(), files={"pkg/app.py": ""},
        planned=[PlannedFile(path="pkg/app.py", role="app_factory")],
        existing_plan=None, workspace=str(tmp_path), replay_plan=frozen,
    )

    assert result[0]["path"] == frozen[0]["path"]
    assert result[0]["exports"][0]["name"] == "CompatApp"
    assert exists is False
    ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_architect_format_repair_is_a_separate_accounted_call(tmp_path, monkeypatch):
    architect = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000096",
        status="pending", attempts=0,
        verify_spec={"kind": "architecture", "action": "architect"},
    )
    update = AsyncMock()
    complete = AsyncMock(side_effect=[
        SimpleNamespace(
            text="```json\n[]\n```", prompt_tokens=10, completion_tokens=4,
            cost_usd=0.001,
        ),
        SimpleNamespace(
            text=_valid(), prompt_tokens=20, completion_tokens=8, cost_usd=0.002,
        ),
    ])
    monkeypatch.setattr(
        plan_module.task_store, "ensure_architect_task", AsyncMock(return_value=architect),
    )
    monkeypatch.setattr(plan_module.task_store, "update_task", update)
    monkeypatch.setattr(
        plan_module, "get_llm", lambda: SimpleNamespace(complete=complete),
    )

    class Recipe:
        @staticmethod
        def should_plan_artifacts(files, planned):
            return True

        @staticmethod
        def build_artifact_plan_prompt(**kwargs):
            return "plan artifacts"

        @staticmethod
        def artifact_plan_violations(plan, files, planned):
            return ["one owner is required"] if not plan else []

    frozen, _ = await plan_module._plan_created_artifacts(
        job_id="00000000-0000-0000-0000-000000000095",
        recipe=Recipe(), files={"pkg/app.py": ""},
        planned=[PlannedFile(path="pkg/app.py", role="app_factory")],
        existing_plan=None, workspace=str(tmp_path),
    )

    assert frozen[0]["path"] == "pkg/compat.py"
    assert update.await_count == 2
    repair_prompt = complete.await_args_list[1].args[0][-1].content
    assert "maximum of 4 artifacts" in repair_prompt
    assert "Combine compatible capabilities" in repair_prompt
    first, second = [call.kwargs["append_attempt"] for call in update.await_args_list]
    assert first["action"] == "architect" and first["cost_usd"] == 0.001
    assert "one owner is required" in first["error"]
    assert second["action"] == "architect_repair" and second["cost_usd"] == 0.002


@pytest.mark.asyncio
async def test_architect_gets_second_repair_only_after_strict_improvement(
    tmp_path, monkeypatch,
):
    architect = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000094",
        status="pending", attempts=0,
        verify_spec={"kind": "architecture", "action": "architect"},
    )
    update = AsyncMock()
    first = _valid(path="pkg/first.py")
    improved = _valid(path="pkg/improved.py")
    accepted = _valid(path="pkg/accepted.py")
    complete = AsyncMock(side_effect=[
        SimpleNamespace(text=first, prompt_tokens=10, completion_tokens=5, cost_usd=0.01),
        SimpleNamespace(
            text=improved, prompt_tokens=20, completion_tokens=6, cost_usd=0.02,
        ),
        SimpleNamespace(
            text=accepted, prompt_tokens=30, completion_tokens=7, cost_usd=0.03,
        ),
    ])
    monkeypatch.setattr(
        plan_module.task_store, "ensure_architect_task", AsyncMock(return_value=architect),
    )
    monkeypatch.setattr(plan_module.task_store, "update_task", update)
    monkeypatch.setattr(
        plan_module, "get_llm", lambda: SimpleNamespace(complete=complete),
    )

    class Recipe:
        @staticmethod
        def should_plan_artifacts(files, planned):
            return True

        @staticmethod
        def build_artifact_plan_prompt(**kwargs):
            return "plan artifacts"

        @staticmethod
        def artifact_plan_violations(plan, files, planned):
            path = plan[0]["path"]
            if path == "pkg/first.py":
                return ["invalid test-shaped path", "missing app_context"]
            if path == "pkg/improved.py":
                return ["invalid test-shaped path"]
            return []

    frozen, _ = await plan_module._plan_created_artifacts(
        job_id="00000000-0000-0000-0000-000000000093",
        recipe=Recipe(), files={"pkg/app.py": ""},
        planned=[PlannedFile(path="pkg/app.py", role="app_factory")],
        existing_plan=None, workspace=str(tmp_path),
    )

    assert frozen[0]["path"] == "pkg/accepted.py"
    assert complete.await_count == 3
    entries = [call.kwargs["append_attempt"] for call in update.await_args_list]
    assert [entry["action"] for entry in entries] == [
        "architect", "architect_repair", "architect_repair",
    ]
    assert [entry.get("repair_number") for entry in entries] == [None, 1, 2]
    assert [entry["cost_usd"] for entry in entries] == [0.01, 0.02, 0.03]
    second_repair_prompt = complete.await_args_list[2].args[0][-1].content
    assert "1 violation(s)" in second_repair_prompt
    assert "do not change already-valid decisions" in second_repair_prompt


@pytest.mark.asyncio
async def test_architect_resamples_once_when_repair_does_not_improve(
    tmp_path, monkeypatch,
):
    architect = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000092",
        status="pending", attempts=0,
        verify_spec={"kind": "architecture", "action": "architect"},
    )
    update = AsyncMock()
    complete = AsyncMock(side_effect=[
        SimpleNamespace(
            text=_valid(path="pkg/first.py"), prompt_tokens=10,
            completion_tokens=5, cost_usd=0.01,
        ),
        SimpleNamespace(
            text=_valid(path="pkg/lateral.py"), prompt_tokens=20,
            completion_tokens=6, cost_usd=0.02,
        ),
        SimpleNamespace(
            text=_valid(path="pkg/accepted.py"), prompt_tokens=30,
            completion_tokens=7, cost_usd=0.03,
        ),
    ])
    monkeypatch.setattr(
        plan_module.task_store, "ensure_architect_task", AsyncMock(return_value=architect),
    )
    monkeypatch.setattr(plan_module.task_store, "update_task", update)
    monkeypatch.setattr(
        plan_module, "get_llm", lambda: SimpleNamespace(complete=complete),
    )

    class Recipe:
        @staticmethod
        def should_plan_artifacts(files, planned):
            return True

        @staticmethod
        def build_artifact_plan_prompt(**kwargs):
            return "plan artifacts"

        @staticmethod
        def artifact_plan_violations(plan, files, planned):
            if plan[0]["path"] == "pkg/accepted.py":
                return []
            return ["violation one", "violation two"]

    frozen, _ = await plan_module._plan_created_artifacts(
        job_id="00000000-0000-0000-0000-000000000091",
        recipe=Recipe(), files={"pkg/app.py": ""},
        planned=[PlannedFile(path="pkg/app.py", role="app_factory")],
        existing_plan=None, workspace=str(tmp_path),
    )

    assert frozen[0]["path"] == "pkg/accepted.py"
    assert complete.await_count == 3
    entries = [call.kwargs["append_attempt"] for call in update.await_args_list]
    assert [entry["action"] for entry in entries] == [
        "architect", "architect_repair", "architect_resample",
    ]
    assert entries[1]["repair_number"] == 1
    assert "repair_number" not in entries[-1]
    assert update.await_args.kwargs["status"] == "done"


@pytest.mark.asyncio
async def test_invalid_resample_gets_the_remaining_bounded_repair(
    tmp_path, monkeypatch,
):
    architect = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000095",
        status="pending", attempts=0,
        verify_spec={"kind": "architecture", "action": "architect"},
    )
    update = AsyncMock()
    complete = AsyncMock(side_effect=[
        SimpleNamespace(
            text=_valid(path="pkg/first.py"), prompt_tokens=10,
            completion_tokens=5, cost_usd=0.01,
        ),
        SimpleNamespace(
            text=_valid(path="pkg/lateral.py"), prompt_tokens=20,
            completion_tokens=6, cost_usd=0.02,
        ),
        SimpleNamespace(
            text=_valid(path="pkg/still_invalid.py"), prompt_tokens=30,
            completion_tokens=7, cost_usd=0.03,
        ),
        SimpleNamespace(
            text=_valid(path="pkg/accepted.py"), prompt_tokens=40,
            completion_tokens=8, cost_usd=0.04,
        ),
    ])
    monkeypatch.setattr(
        plan_module.task_store, "ensure_architect_task", AsyncMock(return_value=architect),
    )
    monkeypatch.setattr(plan_module.task_store, "update_task", update)
    monkeypatch.setattr(
        plan_module, "get_llm", lambda: SimpleNamespace(complete=complete),
    )

    class Recipe:
        @staticmethod
        def should_plan_artifacts(files, planned):
            return True

        @staticmethod
        def build_artifact_plan_prompt(**kwargs):
            return "plan artifacts"

        @staticmethod
        def artifact_plan_violations(plan, files, planned):
            if plan[0]["path"] == "pkg/accepted.py":
                return []
            return ["same violation"]

    frozen, _ = await plan_module._plan_created_artifacts(
        job_id="00000000-0000-0000-0000-000000000094",
        recipe=Recipe(), files={"pkg/app.py": ""},
        planned=[PlannedFile(path="pkg/app.py", role="app_factory")],
        existing_plan=None, workspace=str(tmp_path),
    )

    assert frozen[0]["path"] == "pkg/accepted.py"
    assert complete.await_count == 4
    entries = [call.kwargs["append_attempt"] for call in update.await_args_list]
    assert [entry["action"] for entry in entries] == [
        "architect", "architect_repair", "architect_resample", "architect_repair",
    ]
    assert update.await_args.kwargs["status"] == "done"
