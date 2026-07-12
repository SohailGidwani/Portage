"""R1.1: binding-aware, statically-obvious caller compatibility checks."""

from portage_agent.agent.nodes.execute import caller_contract_violations


def _manifest(*, preserve_shape: bool = True) -> dict[str, dict]:
    return {
        "pkg/db.py::get_db": {
            "module": "pkg/db.py",
            "symbol": "get_db",
            "kind": "function",
            "original": "def get_db()",
            "target_note": "keep direct helper shape",
            "notes": "",
            "call_sites": ["get_db()"],
            "preserve_shape": preserve_shape,
            "shape": {
                "required_positional": 0,
                "required_positional_names": [],
                "required_keyword_only": [],
                "positional_capacity": 0,
                "keyword_names": [],
                "accepts_varargs": False,
                "accepts_varkw": False,
                "is_async": False,
                "is_generator": False,
            },
        }
    }


def test_direct_import_extra_argument_is_rejected():
    src = "from pkg.db import get_db\nconn = get_db(app)\n"
    violations = caller_contract_violations(src, _manifest(), "tests/conftest.py")
    assert len(violations) == 1
    assert "get_db" in violations[0] and "too many positional" in violations[0]


def test_alias_and_relative_imports_are_binding_aware():
    alias = "from pkg.db import get_db as acquire\nconn = acquire(app)\n"
    relative = "from .db import get_db\nconn = get_db(app)\n"
    assert caller_contract_violations(alias, _manifest(), "tests/conftest.py")
    assert caller_contract_violations(relative, _manifest(), "pkg/setup.py")


def test_module_alias_call_is_rejected():
    src = "import pkg.db as database\nconn = database.get_db(app)\n"
    violations = caller_contract_violations(src, _manifest(), "tests/conftest.py")
    assert violations and "database.get_db" in violations[0]


def test_valid_call_and_unknown_star_args_are_not_false_positives():
    valid = "from pkg.db import get_db\nconn = get_db()\n"
    dynamic = "from pkg.db import get_db\nconn = get_db(*args, **kwargs)\n"
    assert caller_contract_violations(valid, _manifest(), "tests/conftest.py") == []
    assert caller_contract_violations(dynamic, _manifest(), "tests/conftest.py") == []


def test_redefined_target_shape_is_not_caller_checked():
    src = "from pkg.db import get_db\nconn = get_db(app)\n"
    assert caller_contract_violations(
        src, _manifest(preserve_shape=False), "tests/conftest.py"
    ) == []


def test_unexpected_keyword_is_rejected():
    src = "from pkg.db import get_db\nconn = get_db(request=app)\n"
    violations = caller_contract_violations(src, _manifest(), "tests/conftest.py")
    assert violations and "unexpected keyword" in violations[0]


def test_removed_original_import_is_caught_when_local_call_remains():
    manifest = _manifest()
    manifest["pkg/db.py::get_db"]["consumers"] = [{
        "module": "tests/conftest.py",
        "local": "get_db",
        "binding": "symbol",
    }]
    violations = caller_contract_violations(
        "def setup():\n    return get_db()\n", manifest, "tests/conftest.py"
    )
    assert violations and "binding" in violations[0] and "removed" in violations[0]
