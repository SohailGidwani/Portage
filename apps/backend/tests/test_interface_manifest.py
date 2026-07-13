"""build_manifest(): frozen, symbol-aware target-interface decisions (R1 Task 4)."""
from pathlib import Path

import pytest

from portage_agent.agent.nodes.common import build_manifest
from portage_agent.recipes.base import PinRule, PlannedFile, Subtask
from portage_agent.recipes.flask_to_fastapi import recipe as flask_to_fastapi_recipe


def _repo(tmp_path: Path, files: dict[str, str]) -> str:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return str(tmp_path)


# One file carrying TWO idiom subtasks — the v2 coarseness case: the sqlalchemy rule
# must claim only `db`, the request_context rule only functions like `get_db`.
FILES = {
    "ext.py": ("from flask_sqlalchemy import SQLAlchemy\n"
               "db = SQLAlchemy()\n"
               "def get_db():\n    return conn\n"),
    "blog.py": "from ext import db, get_db\nrows = get_db().all()\ndb.session.add(1)\n",
}
RULES = [
    PinRule(subtask="request_context",
            applies=lambda c: c.kind == "function",
            note="{name}: keep the original callable shape; endpoints get a companion "
                 "yield dependency"),
    PinRule(subtask="sqlalchemy_plain",
            applies=lambda c: c.kind == "variable" and "SQLAlchemy(" in c.signature,
            note="{name}: plain-SQLAlchemy surface, same module-level name"),
]
BOTH = [Subtask("request_context", "t", "i"), Subtask("sqlalchemy_plain", "t", "i")]


def _planned():
    return [PlannedFile(path="ext.py", role="support", subtasks=list(BOTH)),
            PlannedFile(path="blog.py", role="router", subtasks=[])]


def test_rules_claim_symbols_by_predicate_not_file(tmp_path):
    root = _repo(tmp_path, FILES)
    m = build_manifest(root, _planned(), RULES)
    assert "yield dependency" in m["ext.py::get_db"]["target_note"]
    assert "plain-SQLAlchemy" in m["ext.py::db"]["target_note"]


def test_no_matching_rule_keeps_original_shape(tmp_path):
    root = _repo(tmp_path, FILES)
    m = build_manifest(root, _planned(), rules=[])
    assert m["ext.py::get_db"]["target_note"] == "keep the original shape"


def test_conflicting_rules_fail_plan_loudly(tmp_path):
    root = _repo(tmp_path, FILES)
    greedy = RULES + [PinRule(subtask="sqlalchemy_plain",
                              applies=lambda c: True, note="claims everything")]
    with pytest.raises(ValueError, match="ext.py::db"):
        build_manifest(root, _planned(), greedy)


def test_rule_needs_its_subtask_on_the_file(tmp_path):
    root = _repo(tmp_path, FILES)
    planned = [PlannedFile(path="ext.py", role="support", subtasks=[]),  # no subtasks
               PlannedFile(path="blog.py", role="router", subtasks=[])]
    m = build_manifest(root, planned, RULES)
    assert m["ext.py::get_db"]["target_note"] == "keep the original shape"


def test_manifest_is_json_safe_and_carries_shape(tmp_path):
    import json
    root = _repo(tmp_path, FILES)
    m = build_manifest(root, _planned(), RULES)
    json.dumps(m)  # must not raise
    assert m["ext.py::get_db"]["shape"]["is_generator"] is False
    assert m["ext.py::get_db"]["consumers"] == [{
        "module": "blog.py", "local": "get_db", "binding": "symbol",
    }]


# R1 review CRITICAL 1 (task-5-review.md): the request_context (`c.kind == "function"`)
# and auth_login (name in the flask_login API set) predicates were NOT disjoint. flaskr's
# real auth.py seam — a custom `login_required` decorator using `g`/`before_app_request`,
# imported cross-file — carries BOTH subtasks, and `login_required` matched both rules,
# so build_manifest raised and plan_node hard-crashed before any migration. Regression
# test uses the REAL recipe.pin_rules (not a local reconstruction) so a future edit to
# the rules is caught here too.
FLASKR_AUTH_SEAM_FILES = {
    "auth.py": (
        "from flask import g\n\n"
        "def login_required(view):\n"
        "    def wrapped(**kw):\n"
        "        if g.user is None:\n"
        "            return None\n"
        "        return view(**kw)\n"
        "    return wrapped\n\n"
        "def get_db():\n"
        "    return g.db\n"
    ),
    "blog.py": (
        "from auth import login_required, get_db\n"
        "x = login_required\ny = get_db()\n"
    ),
}


def _flaskr_auth_seam_planned():
    return [
        PlannedFile(path="auth.py", role="support",
                    subtasks=[Subtask("request_context", "t", "i"),
                              Subtask("auth_login", "t", "i")]),
        PlannedFile(path="blog.py", role="router", subtasks=[]),
    ]


def test_flaskr_auth_seam_pin_rules_are_disjoint(tmp_path):
    root = _repo(tmp_path, FLASKR_AUTH_SEAM_FILES)
    m = build_manifest(root, _flaskr_auth_seam_planned(), flask_to_fastapi_recipe.pin_rules)
    login_note = m["auth.py::login_required"]["target_note"]
    db_note = m["auth.py::get_db"]["target_note"]
    assert "session" in login_note
    assert "yield dependency" in db_note
    assert m["auth.py::login_required"]["preserve_shape"] is True
    assert m["auth.py::login_required"]["shape"]["returns_nested_function"] is True


def test_request_context_only_pins_resource_functions(tmp_path):
    root = _repo(tmp_path, {
        "db.py": (
            "def get_db():\n    return 1\n"
            "def init_db():\n    return 2\n"
            "def close_db():\n    return 3\n"
        ),
        "app.py": "from db import get_db, init_db, close_db\n",
    })
    planned = [
        PlannedFile("db.py", "support", [Subtask("request_context", "t", "i")]),
        PlannedFile("app.py", "router", []),
    ]
    manifest = build_manifest(root, planned, flask_to_fastapi_recipe.pin_rules)
    resource = manifest["db.py::get_db"]
    assert resource["preserve_shape"] is True
    assert resource["target_kind"] == "function"
    assert resource["additional_exports"] == ["get_db_dep"]
    assert manifest["db.py::init_db"]["target_note"] == "keep the original shape"
    assert manifest["db.py::close_db"]["target_note"] == "keep the original shape"
