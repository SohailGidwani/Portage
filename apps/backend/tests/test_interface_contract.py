"""interface_contract(): structured original-shape extraction (R1 Task 2)."""
from pathlib import Path

from portage_agent.agent.nodes.common import interface_contract


def _repo(tmp_path: Path, files: dict[str, str]) -> str:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return str(tmp_path)


def test_function_signature_and_call_sites(tmp_path):
    root = _repo(tmp_path, {
        "db.py": "def get_db(path=None):\n    return path\n",
        "blog.py": "from db import get_db\nconn = get_db()\n",
    })
    c = interface_contract(root, "db.py")[0]
    assert (c.name, c.kind, c.signature) == ("get_db", "function", "def get_db(path=None)")
    assert "conn = get_db()" in c.call_sites


def test_generator_decorator_notes_and_with_usage(tmp_path):
    root = _repo(tmp_path, {
        "db.py": ("from contextlib import contextmanager\n"
                  "@contextmanager\n"
                  "def get_db():\n    yield 1\n"),
        "views.py": "from db import get_db\nwith get_db() as db:\n    pass\n",
    })
    c = interface_contract(root, "db.py")[0]
    assert "@contextmanager" in c.notes and "generator" in c.notes
    assert any("with get_db()" in s for s in c.call_sites)
    assert c.shape == {
        "required_positional": 0,
        "required_positional_names": [],
        "required_keyword_only": [],
        "positional_capacity": 0,
        "keyword_names": [],
        "accepts_varargs": False,
        "accepts_varkw": False,
        "is_async": False,
        "is_generator": True,
        "returns_nested_function": False,
    }


def test_shape_facts_async_and_kwonly(tmp_path):
    root = _repo(tmp_path, {
        "svc.py": "async def fetch(url, *, timeout, retries=2):\n    return url\n",
        "app.py": "from svc import fetch\n",
    })
    c = interface_contract(root, "svc.py")[0]
    assert c.signature.startswith("async def fetch(")
    assert c.shape == {
        "required_positional": 1,
        "required_positional_names": ["url"],
        "required_keyword_only": ["timeout"],
        "positional_capacity": 1,
        "keyword_names": ["url", "timeout", "retries"],
        "accepts_varargs": False,
        "accepts_varkw": False,
        "is_async": True,
        "is_generator": False,
        "returns_nested_function": False,
    }


def test_module_binding_attrs_become_contracts(tmp_path):
    root = _repo(tmp_path, {
        "flaskr/__init__.py": "from . import db\ndb.init_app(1)\n",
        "flaskr/db.py": "def init_app(app):\n    pass\ndef unused():\n    pass\n",
    })
    names = [c.name for c in interface_contract(root, "flaskr/db.py")]
    assert names == ["init_app"]  # only what's actually used, not `unused`


def test_class_and_variable_kinds(tmp_path):
    root = _repo(tmp_path, {
        "models.py": (
            "ANON = object()\nclass User:\n"
            "    def __init__(self, name):\n        self.name = name\n"
        ),
        "auth.py": "from models import User, ANON\nu = User('x')\n",
    })
    by = {c.name: c for c in interface_contract(root, "models.py")}
    assert by["User"].kind == "class" and by["User"].signature == "class User(name)"
    assert by["ANON"].kind == "variable"


def test_nested_generator_does_not_mark_outer_as_generator(tmp_path):
    # A `yield` inside a nested `def` belongs to the INNER function's scope, not the
    # outer one — ast.walk(node) over-recurses into nested defs (reviewed bug, F1).
    root = _repo(tmp_path, {
        "db.py": "def outer():\n    def inner():\n        yield 1\n    return inner\n",
        "app.py": "from db import outer\nx = outer()\n",
    })
    c = interface_contract(root, "db.py")[0]
    assert c.shape["is_generator"] is False
    assert c.shape["returns_nested_function"] is True
    assert "generator" not in c.notes
