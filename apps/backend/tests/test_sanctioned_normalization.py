"""Recipe-owned, audited test-plumbing normalization invariants."""

from portage_agent.agent.nodes.oracle import (
    apply_sanctioned_normalizations,
    build_oracle_manifest,
    oracle_violations,
)
from portage_agent.agent.nodes.plan import attach_sanctioned_normalizations
from portage_agent.recipes.base import PlannedFile
from portage_agent.recipes.flask_to_fastapi import (
    FlaskToFastAPIRecipe,
    _artifact_capability_requirements,
    _artifact_placement_contract,
)

TEST_SOURCE = """\
from flask import g
from flask import session as flask_session

def test_logged_in():
    assert g.user["username"] == "test"
    assert flask_session["user_id"] == 1
"""

ARCHITECTURE_FILES = {
    "pkg/app.py": """\
from flask import Flask
def create_app():
    return Flask(__name__)
""",
    "pkg/auth.py": """\
from flask import Blueprint, g, render_template, session
def login_required(fn): return fn
def view():
    session["user_id"] = g.user["id"]
    return render_template("auth.html")
""",
    "pkg/blog.py": """\
from flask import Blueprint, g, render_template, session
def login_required(fn): return fn
def view():
    session["post_id"] = g.user["id"]
    return render_template("blog.html")
""",
    "tests/test_runtime.py": """\
from flask import g, session
def test_runtime(app):
    with app.app_context():
        assert g is not None
    assert app.test_client()
    assert app.test_cli_runner()
""",
    "tests/test_factory.py": """\
from pkg.app import create_app
def test_config():
    assert create_app().testing
""",
}


def _owner() -> dict:
    return {
        "path": "pkg/testing.py",
        "role": "support",
        "purpose": "Expose real migrated request context to preserved tests",
        "instructions": "Implement runtime-backed proxies.",
        "capabilities": ["test_context_surface"],
        "exports": [
            {"name": "g", "kind": "variable", "signature": "", "members": []},
            {"name": "session", "kind": "variable", "signature": "", "members": []},
        ],
        "consumers": ["tests/test_auth.py"],
        "depends_on": [],
    }


def test_recipe_requires_a_frozen_owner_for_direct_context_imports():
    files = {
        "tests/test_auth.py": TEST_SOURCE,
        "pkg/app.py": "from flask import Flask\napp = Flask(__name__)\n",
    }
    planned = FlaskToFastAPIRecipe().plan_files(files)

    missing = FlaskToFastAPIRecipe.artifact_plan_violations([], files, planned)
    assert "test_context_surface requires exactly one owner artifact, got 0" in missing

    assert FlaskToFastAPIRecipe.artifact_plan_violations(
        [_owner()], files, planned,
    ) == []

    test_owned = {**_owner(), "path": "tests/context_test.py"}
    violations = FlaskToFastAPIRecipe.artifact_plan_violations(
        [test_owned], files, planned,
    )
    assert any(
        'basename "context_test.py" ends with forbidden suffix "_test.py"' in item
        for item in violations
    )


def test_repository_placement_contract_is_derived_and_shared_with_policy():
    files = {
        "src/shop/__init__.py": "from flask import Flask\n",
        "src/shop/views.py": "from flask import Blueprint\n",
        "src/shop/admin/routes.py": "from flask import Blueprint\n",
        "src/shop/compat.py": "",
        "tests/test_app.py": "def test_app(): pass\n",
    }
    planned = [
        PlannedFile("src/shop/__init__.py", "app_factory"),
        PlannedFile("src/shop/views.py", "router"),
        PlannedFile("tests/test_app.py", "test_harness"),
    ]
    contract = _artifact_placement_contract(files, planned)

    assert contract["application_roots"] == ["src/shop"]
    assert contract["test_roots"] == ["tests"]
    assert contract["allowed_new_artifact_parent_directories"] == [
        "src/shop", "src/shop/admin",
    ]
    assert "src/shop/runtime.py" in contract["allowed_new_artifact_paths"]
    assert "src/shop/admin/runtime.py" in contract["allowed_new_artifact_paths"]
    assert "src/shop/compat.py" not in contract["allowed_new_artifact_paths"]

    valid = {"path": "src/shop/runtime.py"}
    assert FlaskToFastAPIRecipe.artifact_plan_violations(
        [valid], files, planned,
    ) == []

    test_named = {"path": "src/shop/test_context.py"}
    violations = FlaskToFastAPIRecipe.artifact_plan_violations(
        [test_named], files, planned,
    )
    assert violations == [
        'artifact src/shop/test_context.py is invalid: basename "test_context.py" '
        'starts with forbidden prefix "test_"; choose a non-test application-module '
        "name and preserve all other decisions"
    ]

    outside = {"path": "helpers/runtime.py"}
    violations = FlaskToFastAPIRecipe.artifact_plan_violations(
        [outside], files, planned,
    )
    assert "path must be selected exactly from allowed_new_artifact_paths" in violations[0]

    collision = {"path": "src/shop/compat.py"}
    violations = FlaskToFastAPIRecipe.artifact_plan_violations(
        [collision], files, planned,
    )
    assert "path must be selected exactly from allowed_new_artifact_paths" in violations[0]


def test_repository_placement_contract_supports_flat_module_layouts():
    files = {
        "app.py": "from flask import Flask\n",
        "test_app.py": "def test_app(): pass\n",
    }
    planned = [
        PlannedFile("app.py", "app_factory"),
        PlannedFile("test_app.py", "test_harness"),
    ]

    contract = _artifact_placement_contract(files, planned)

    assert contract["application_roots"] == ["."]
    assert contract["allowed_new_artifact_parent_directories"] == ["."]
    assert "runtime.py" in contract["allowed_new_artifact_paths"]
    assert all("/" not in path for path in contract["allowed_new_artifact_paths"])


def test_architect_checklist_is_exhaustive_packable_and_shared_with_policy():
    recipe = FlaskToFastAPIRecipe()
    planned = recipe.plan_files(ARCHITECTURE_FILES)
    requirements = _artifact_capability_requirements(ARCHITECTURE_FILES, planned)

    assert set(requirements) == {
        "authentication",
        "direct_test_surface",
        "request_context",
        "session_and_flash",
        "template_rendering",
        "test_context_surface",
    }
    assert requirements["direct_test_surface"]["required_class_members"] == [
        "app_context", "test_cli_runner", "test_client", "testing",
    ]
    assert requirements["test_context_surface"]["required_exports"] == ["g", "session"]

    consumers = sorted({
        path for requirement in requirements.values()
        for path in requirement["consumers"]
    })
    combined_owner = {
        "path": "pkg/runtime.py",
        "role": "support",
        "purpose": "Own the shared target runtime.",
        "instructions": "Implement every frozen capability over one runtime state.",
        "capabilities": list(requirements),
        "exports": [
            {"name": "g", "kind": "variable", "signature": "", "members": []},
            {"name": "session", "kind": "variable", "signature": "", "members": []},
            {
                "name": "RuntimeSurface", "kind": "class", "signature": "",
                "members": ["app_context", "test_cli_runner", "test_client", "testing"],
            },
        ],
        "consumers": consumers,
        "depends_on": [],
    }
    assert recipe.artifact_plan_violations(
        [combined_owner], ARCHITECTURE_FILES, planned,
    ) == []

    incomplete = {
        **combined_owner,
        "capabilities": [
            capability for capability in combined_owner["capabilities"]
            if capability != "authentication"
        ],
    }
    assert "authentication requires exactly one owner artifact, got 0" in (
        recipe.artifact_plan_violations([incomplete], ARCHITECTURE_FILES, planned)
    )

    prompt = recipe.build_artifact_plan_prompt(
        files={"pkg/app.py": ARCHITECTURE_FILES["pkg/app.py"]},
        analysis_files=ARCHITECTURE_FILES,
        planned=planned,
        non_python_files="templates/auth.html\ntemplates/blog.html",
        existing_python_paths=sorted(ARCHITECTURE_FILES),
    )
    assert "REQUIRED CAPABILITY CHECKLIST" in prompt
    assert str(requirements) in prompt
    assert "at most 4 artifacts" in prompt
    assert "combine compatible capabilities" in prompt
    assert "same artifact must own both" in prompt
    assert "REPOSITORY PLACEMENT CONTRACT" in prompt
    assert "'application_roots': ['pkg']" in prompt
    assert "'test_roots': ['tests']" in prompt
    assert "'allowed_new_artifact_parent_directories': ['pkg']" in prompt
    assert "'allowed_new_artifact_paths':" in prompt
    assert "pkg/runtime.py" in prompt
    assert "copied EXACTLY" in prompt
    assert 'basename starts with "test_"' in prompt


def test_materializer_completes_all_deterministic_contract_facts_once():
    recipe = FlaskToFastAPIRecipe()
    planned = recipe.plan_files(ARCHITECTURE_FILES)
    requirements = _artifact_capability_requirements(ARCHITECTURE_FILES, planned)
    draft = {
        "path": "pkg/runtime.py",
        "role": "support",
        "purpose": "Own the selected shared runtime.",
        "instructions": "Implement real target-runtime behavior.",
        "capabilities": list(requirements),
        "exports": [{
            "name": "RuntimeSurface", "kind": "class", "signature": "",
            "members": ["test_client"],
        }],
        "consumers": [],
        "depends_on": [],
    }

    completed, audit = recipe.materialize_artifact_contracts(
        [draft], ARCHITECTURE_FILES, planned,
    )
    owner = completed[0]
    exports = {item["name"]: item for item in owner["exports"]}

    assert owner["consumers"] == sorted({
        path for requirement in requirements.values()
        for path in requirement["consumers"]
    })
    assert exports["g"]["kind"] == "variable"
    assert exports["session"]["kind"] == "variable"
    assert exports["RuntimeSurface"]["members"] == [
        "app_context", "test_cli_runner", "test_client", "testing",
    ]
    assert recipe.artifact_plan_violations(completed, ARCHITECTURE_FILES, planned) == []
    assert audit[0]["path"] == "pkg/runtime.py"
    assert audit[0]["added_exports"] == [
        {"name": "g", "kind": "variable"},
        {"name": "session", "kind": "variable"},
    ]
    assert audit[0]["added_class_members"] == [{
        "export": "RuntimeSurface",
        "members": ["app_context", "test_cli_runner", "testing"],
    }]

    repeated, repeated_audit = recipe.materialize_artifact_contracts(
        completed, ARCHITECTURE_FILES, planned,
    )
    assert repeated == completed
    assert repeated_audit == []
    normalizations = recipe.build_test_normalizations(ARCHITECTURE_FILES, completed)
    assert set(normalizations) == {"tests/test_runtime.py"}


def test_materializer_preserves_wrong_kinds_and_refuses_ambiguous_classes():
    recipe = FlaskToFastAPIRecipe()
    planned = recipe.plan_files(ARCHITECTURE_FILES)
    requirements = _artifact_capability_requirements(ARCHITECTURE_FILES, planned)
    draft = {
        "path": "pkg/runtime.py",
        "role": "support",
        "purpose": "Own the selected shared runtime.",
        "instructions": "Implement real target-runtime behavior.",
        "capabilities": list(requirements),
        "exports": [
            {"name": "g", "kind": "function", "signature": "", "members": []},
            {"name": "First", "kind": "class", "signature": "",
             "members": ["test_client"]},
            {"name": "Second", "kind": "class", "signature": "",
             "members": ["app_context"]},
        ],
        "consumers": [],
        "depends_on": [],
    }

    completed, _ = recipe.materialize_artifact_contracts(
        [draft], ARCHITECTURE_FILES, planned,
    )
    exports = {item["name"]: item for item in completed[0]["exports"]}
    violations = recipe.artifact_plan_violations(
        completed, ARCHITECTURE_FILES, planned,
    )

    assert exports["g"]["kind"] == "function"
    assert exports["First"]["members"] == ["test_client"]
    assert exports["Second"]["members"] == ["app_context"]
    assert any("wrong export kinds" in item for item in violations)
    assert any("requires exactly one class export" in item for item in violations)


def test_materializer_does_not_invent_missing_capability_owners():
    recipe = FlaskToFastAPIRecipe()
    planned = recipe.plan_files(ARCHITECTURE_FILES)
    draft = {
        "path": "pkg/runtime.py",
        "role": "support",
        "purpose": "Own part of the selected runtime.",
        "instructions": "Implement real target-runtime behavior.",
        "capabilities": ["test_context_surface"],
        "exports": [{
            "name": "RuntimeSurface", "kind": "class", "signature": "", "members": [],
        }],
        "consumers": [],
        "depends_on": [],
    }

    completed, _ = recipe.materialize_artifact_contracts(
        [draft], ARCHITECTURE_FILES, planned,
    )

    assert len(completed) == 1
    assert "authentication requires exactly one owner artifact, got 0" in (
        recipe.artifact_plan_violations(completed, ARCHITECTURE_FILES, planned)
    )


def test_normalization_changes_only_frozen_import_lines_and_preserves_oracle(tmp_path):
    test_path = tmp_path / "tests" / "test_auth.py"
    test_path.parent.mkdir()
    test_path.write_text(TEST_SOURCE)
    original = build_oracle_manifest(str(tmp_path))["tests/test_auth.py"]
    specs = FlaskToFastAPIRecipe.build_test_normalizations(
        {"tests/test_auth.py": TEST_SOURCE}, [_owner()],
    )

    assert set(specs) == {"tests/test_auth.py"}
    normalized = apply_sanctioned_normalizations(
        TEST_SOURCE, specs["tests/test_auth.py"]["replacements"],
    )

    assert normalized.splitlines()[:2] == [
        "from pkg.testing import g",
        "from pkg.testing import session as flask_session",
    ]
    assert normalized.splitlines()[2:] == TEST_SOURCE.splitlines()[2:]
    assert oracle_violations(original, normalized) == []


def test_normalization_refuses_source_drift():
    specs = FlaskToFastAPIRecipe.build_test_normalizations(
        {"tests/test_auth.py": TEST_SOURCE}, [_owner()],
    )
    drifted = TEST_SOURCE.replace("from flask import g", "from flask import g as global_g")

    try:
        apply_sanctioned_normalizations(
            drifted, specs["tests/test_auth.py"]["replacements"],
        )
    except ValueError as exc:
        assert "source drift" in str(exc)
    else:  # pragma: no cover - assertion spelling is clearer than pytest.raises here
        raise AssertionError("source drift must reject the normalization")


def test_normalized_test_joins_owner_cut_without_joining_generation_unit():
    planned = [
        PlannedFile("pkg/testing.py", "support", [], 10, action="create"),
        PlannedFile("pkg/app.py", "app_factory", [], 20),
        PlannedFile("tests/test_auth.py", "test_harness", [], 30),
    ]
    analysis = {
        "cuts": [{
            "id": "executable-cut-1",
            "paths": ["pkg/testing.py", "pkg/app.py"],
            "reason": "executable framework contracts: planned_artifact",
            "edge_kinds": ["planned_artifact"],
            "mode": "coordinated",
        }],
        "edges": [],
    }
    normalization = FlaskToFastAPIRecipe.build_test_normalizations(
        {"tests/test_auth.py": TEST_SOURCE}, [_owner()],
    )

    attach_sanctioned_normalizations(analysis, normalization, planned)

    assert analysis["cuts"][0]["paths"] == [
        "pkg/testing.py", "pkg/app.py", "tests/test_auth.py",
    ]
    assert analysis["edges"][0]["kind"] == "sanctioned_test_normalization"
