"""dependency_order(): SCC-aware topological execution order (R1 Task 3)."""
from portage_agent.agent.nodes.common import dependency_order
from portage_agent.recipes.base import PlannedFile


def _pf(path, order):
    return PlannedFile(path=path, role="x", subtasks=[], order=order)


def _paths(files, planned):
    return [p.path for p in dependency_order(files, planned)]


def test_dependency_before_importer_despite_role_order():
    files = {
        "app/db.py": "def get_db():\n    return 1\n",
        "app/blog.py": "from app.db import get_db\n",
        "app/__init__.py": "from . import blog\n",
    }
    planned = [_pf("app/blog.py", 10), _pf("app/db.py", 15), _pf("app/__init__.py", 20)]
    out = _paths(files, planned)
    assert out.index("app/db.py") < out.index("app/blog.py") < out.index("app/__init__.py")


def test_dependent_of_cycle_runs_AFTER_the_cycle():
    # v1's Kahn fallback bug: c.py depends on the a<->b cycle and must NOT precede it,
    # even though its role order (5) is lowest.
    files = {
        "a.py": "from b import g\n",
        "b.py": "from a import f\n",
        "c.py": "from a import f\n",
    }
    planned = [_pf("c.py", 5), _pf("a.py", 10), _pf("b.py", 20)]
    out = _paths(files, planned)
    assert out.index("c.py") > max(out.index("a.py"), out.index("b.py"))
    assert out.index("a.py") < out.index("b.py")  # role order inside the SCC


def test_role_order_between_independent_files():
    files = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
    assert _paths(files, [_pf("b.py", 20), _pf("a.py", 10)]) == ["a.py", "b.py"]


def test_relative_and_plain_imports_create_edges():
    files = {
        "pkg/db.py": "def get_db():\n    return 1\n",
        "pkg/app.py": "from . import db\n",
        "cli.py": "import pkg.app\n",
    }
    planned = [_pf("cli.py", 1), _pf("pkg/app.py", 2), _pf("pkg/db.py", 3)]
    out = _paths(files, planned)
    assert out == ["pkg/db.py", "pkg/app.py", "cli.py"]


def test_deterministic_across_input_permutations():
    # v2 finding #2: independent-SCC order must come from (role order, path), never
    # from set/dict/traversal order. Same result for every input permutation.
    import itertools

    files = {f"m{i}.py": "x = 1\n" for i in range(5)}
    planned = [_pf(f"m{i}.py", 50) for i in range(5)]
    expected = [f"m{i}.py" for i in range(5)]  # tie on order 50 -> path ascending
    for perm in itertools.permutations(planned):
        assert _paths(files, list(perm)) == expected
