"""Foundation pass: test-oracle manifests and guarded plumbing rewrites."""

from pathlib import Path

from portage_agent.agent.nodes.oracle import (
    build_oracle_manifest,
    classify_test_strategy,
    oracle_violations,
)


def _repo(tmp_path: Path, files: dict[str, str]) -> str:
    for rel, src in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src)
    return str(tmp_path)


SOURCE = """\
import pytest

@pytest.mark.parametrize("value", [1, 2])
def test_body(client, value):
    response = client.get(f"/{value}")
    assert response.status_code == 200
    assert response.get_json() == {"value": value}

def test_error():
    with pytest.raises(ValueError):
        raise ValueError("x")
"""


def _entry(tmp_path: Path) -> dict:
    root = _repo(tmp_path, {"tests/test_api.py": SOURCE})
    return build_oracle_manifest(root)["tests/test_api.py"]


def test_allowed_response_plumbing_normalizes_without_weakening_assertion(tmp_path):
    migrated = SOURCE.replace("response.get_json()", "response.json()")
    assert oracle_violations(_entry(tmp_path), migrated) == []


def test_deleted_assert_and_renamed_test_are_rejected(tmp_path):
    deleted = SOURCE.replace("    assert response.status_code == 200\n", "")
    assert any("assertion" in v for v in oracle_violations(_entry(tmp_path), deleted))
    renamed = SOURCE.replace("def test_body", "def test_items")
    assert any("test set changed" in v for v in oracle_violations(_entry(tmp_path), renamed))


def test_added_skip_changed_raises_and_removed_parametrize_are_rejected(tmp_path):
    skipped = SOURCE.replace("def test_error", "@pytest.mark.skip\ndef test_error")
    assert any("skip/xfail" in v for v in oracle_violations(_entry(tmp_path), skipped))
    changed = SOURCE.replace("pytest.raises(ValueError)", "pytest.raises(Exception)")
    assert any("raises" in v for v in oracle_violations(_entry(tmp_path), changed))
    unparametrized = SOURCE.replace(
        '@pytest.mark.parametrize("value", [1, 2])\n', ""
    )
    assert any("parametrize" in v for v in oracle_violations(_entry(tmp_path), unparametrized))


def test_runtime_and_module_level_skips_are_rejected(tmp_path):
    runtime_skip = SOURCE.replace(
        "def test_error():", 'def test_error():\n    pytest.skip("not today")'
    )
    assert any("skip/xfail" in v for v in oracle_violations(_entry(tmp_path), runtime_skip))
    module_skip = "pytestmark = pytest.mark.skip\n" + SOURCE
    assert any("module/class" in v for v in oracle_violations(_entry(tmp_path), module_skip))


def test_fixture_names_are_part_of_the_oracle(tmp_path):
    source = """\
import pytest
@pytest.fixture
def client():
    return object()
def test_ok(client):
    assert client is not None
"""
    root = _repo(tmp_path, {"tests/conftest.py": source})
    entry = build_oracle_manifest(root)["tests/conftest.py"]
    renamed = source.replace("client", "api_client")
    assert any("fixture set" in v for v in oracle_violations(entry, renamed))


def test_fixture_dependencies_and_yield_lifecycle_are_preserved(tmp_path):
    source = """\
import pytest
@pytest.fixture
def app(tmp_path):
    yield tmp_path
@pytest.fixture
def runner(app):
    return app
"""
    root = _repo(tmp_path, {"tests/conftest.py": source})
    entry = build_oracle_manifest(root)["tests/conftest.py"]
    no_dependency = source.replace("def runner(app):", "def runner():")
    no_yield = source.replace("yield tmp_path", "return tmp_path")
    assert any("dependencies/lifecycle" in v for v in oracle_violations(entry, no_dependency))
    assert any("dependencies/lifecycle" in v for v in oracle_violations(entry, no_yield))


def test_strategy_prefers_adapter_and_marks_direct_flask_globals_unsupported():
    assert classify_test_strategy("tests/test_api.py", "response.get_json()") == "adapter"
    assert classify_test_strategy("tests/conftest.py", "app.test_client()") == "adapter_wiring"
    assert classify_test_strategy("tests/test_auth.py", "from flask import g\nassert g.user") == (
        "unsupported_test_seam"
    )
    assert classify_test_strategy(
        "tests/test_auth.py", "from flask import jsonify, session"
    ) == "unsupported_test_seam"
    assert classify_test_strategy(
        "tests/test_auth.py", "with app.test_request_context('/'):\n    pass"
    ) == "unsupported_test_seam"
    assert classify_test_strategy("tests/test_math.py", "def test_x(): assert 1") == "unchanged"
