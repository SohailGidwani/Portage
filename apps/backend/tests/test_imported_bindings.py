"""imported_bindings(): alias/module/relative-import-aware extraction (R1 Task 1)."""
from pathlib import Path

from portage_agent.agent.nodes.common import (
    ModuleBinding,
    binding_call_sites,
    export_contract,
    imported_bindings,
)


def _repo(tmp_path: Path, files: dict[str, str]) -> str:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return str(tmp_path)


def test_plain_from_import():
    b = ModuleBinding(importer="a.py", symbol="get_db", local="get_db")
    src = "from db import get_db\nconn = get_db()\n"
    assert binding_call_sites(src, b) == ["conn = get_db()"]


def test_from_dot_import_module_binding(tmp_path):
    # THE flaskr factory pattern v1 missed: `from . import db` binds the MODULE.
    root = _repo(tmp_path, {
        "flaskr/__init__.py": "from . import db\ndef create_app():\n    db.init_app(app)\n",
        "flaskr/db.py": "def init_app(app):\n    pass\n",
    })
    bindings = imported_bindings(root, "flaskr/db.py")
    assert any(b.symbol is None and b.local == "db" and b.importer == "flaskr/__init__.py"
               for b in bindings)
    assert "init_app" in export_contract(root, "flaskr/db.py")


def test_aliased_from_import(tmp_path):
    root = _repo(tmp_path, {
        "db.py": "def get_db():\n    return 1\n",
        "views.py": "from db import get_db as acquire\nconn = acquire()\n",
    })
    b = next(x for x in imported_bindings(root, "db.py") if x.symbol == "get_db")
    assert b.local == "acquire"
    src = (Path(root) / "views.py").read_text()
    assert binding_call_sites(src, b) == ["conn = acquire()"]


def test_module_import_with_alias_and_attribute_calls(tmp_path):
    root = _repo(tmp_path, {
        "app/db.py": "def get_db():\n    return 1\n",
        "cli.py": "import app.db as db\nrows = db.get_db().all()\n",
    })
    bindings = imported_bindings(root, "app/db.py")
    mod = next(b for b in bindings if b.symbol is None)
    assert mod.local == "db"
    src = (Path(root) / "cli.py").read_text()
    assert any("db.get_db()" in s for s in binding_call_sites(src, mod))
    assert "get_db" in export_contract(root, "app/db.py")  # attr usage counts as export


def test_star_and_unparseable_skipped(tmp_path):
    root = _repo(tmp_path, {
        "db.py": "def get_db():\n    return 1\n",
        "a.py": "from db import *\n",
        "broken.py": "def broken(:\n",
    })
    assert imported_bindings(root, "db.py") == []


def test_unaliased_dotted_import_resolves_the_real_symbol(tmp_path):
    # `import app.db` (no alias) binds the name `app` in Python, but the module accessed
    # via attribute chain is `app.db` — resolution must use the full dotted path, not just
    # the submodule tail (reviewed bug, F2: was reporting export "db" instead of "get_db").
    root = _repo(tmp_path, {
        "app/db.py": "def get_db():\n    return 1\n",
        "cli.py": "import app.db\nrows = app.db.get_db()\n",
    })
    assert export_contract(root, "app/db.py") == ["get_db"]
    mod = next(b for b in imported_bindings(root, "app/db.py") if b.symbol is None)
    src = (Path(root) / "cli.py").read_text()
    assert any("app.db.get_db()" in s for s in binding_call_sites(src, mod))


def test_from_parent_package_imports_target_module(tmp_path):
    root = _repo(tmp_path, {
        "pkg/db.py": "def get_db():\n    return 1\n",
        "consumer.py": "from pkg import db\nrows = db.get_db()\n",
    })
    bindings = imported_bindings(root, "pkg/db.py")
    assert any(b.symbol is None and b.local == "db" for b in bindings)
    assert export_contract(root, "pkg/db.py") == ["get_db"]


def test_from_relative_parent_package_imports_target_module(tmp_path):
    root = _repo(tmp_path, {
        "app/pkg/db.py": "def get_db():\n    return 1\n",
        "app/consumer.py": "from .pkg import db\nrows = db.get_db()\n",
    })
    bindings = imported_bindings(root, "app/pkg/db.py")
    assert any(b.symbol is None and b.local == "db" for b in bindings)
    assert export_contract(root, "app/pkg/db.py") == ["get_db"]


def test_parent_package_symbol_reexport_is_not_mistaken_for_submodule(tmp_path):
    root = _repo(tmp_path, {
        "api/__init__.py": "from .app import app\n",
        "api/app.py": "app = object()\n",
        "wsgi.py": "from api import app\napp.run()\n",
    })
    bindings = imported_bindings(root, "api/app.py")
    assert ModuleBinding("api/__init__.py", "app", "app") in bindings
    assert not any(b.importer == "wsgi.py" and b.symbol is None for b in bindings)
    assert export_contract(root, "api/app.py") == ["app"]


def test_same_basename_in_different_packages_does_not_cross_bind(tmp_path):
    root = _repo(tmp_path, {
        "app/email.py": "def send_email():\n    pass\n",
        "app/auth/email.py": "def send_reset():\n    pass\n",
        "app/auth/routes.py": (
            "from app.auth.email import send_reset\n"
            "send_reset()\n"
        ),
    })

    assert imported_bindings(root, "app/email.py") == []
    assert imported_bindings(root, "app/auth/email.py") == [
        ModuleBinding("app/auth/routes.py", "send_reset", "send_reset"),
    ]
