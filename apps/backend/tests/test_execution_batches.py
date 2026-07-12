"""Dependency batches are deterministic and keep coupled seams atomic."""

from types import SimpleNamespace

from portage_agent.agent.nodes.execute import (
    expand_to_verifiable_batch,
    select_execution_batch,
)


def _task(
    path: str, order: int, status: str = "pending", tests: list[str] | None = None,
    role: str = "support",
) -> SimpleNamespace:
    return SimpleNamespace(
        target_path=path, order_index=order, status=status, type=role,
        verify_spec={"affected_tests": tests or []},
    )


def test_next_dependency_file_is_a_single_batch():
    tasks = [_task("db.py", 0), _task("app.py", 10), _task("test_app.py", 20)]
    assert select_execution_batch(tasks, []) == ["db.py"]


def test_coupled_unit_runs_atomically_when_its_first_dependency_is_next():
    tasks = [_task("compat.py", 0, "done"), _task("db.py", 10), _task("app.py", 20)]
    units = [{"id": "seam", "paths": ["db.py", "app.py"]}]
    assert select_execution_batch(tasks, units) == ["db.py", "app.py"]


def test_later_unit_does_not_jump_an_earlier_independent_dependency():
    tasks = [_task("models.py", 10), _task("app.py", 20), _task("conftest.py", 30)]
    units = [{"id": "seam", "paths": ["app.py", "conftest.py"]}]
    assert select_execution_batch(tasks, units) == ["models.py"]


def test_empty_blast_radius_accumulates_until_a_real_test_boundary():
    tasks = [
        _task("db.py", 10), _task("views.py", 20), _task("app.py", 30),
        _task("conftest.py", 40), _task("test_app.py", 50, tests=["test_app.py"]),
    ]
    units = [{"id": "seam", "paths": ["db.py", "app.py", "conftest.py"]}]
    assert expand_to_verifiable_batch(tasks, units, ["db.py", "app.py", "conftest.py"]) == [
        "db.py", "views.py", "app.py", "conftest.py", "test_app.py",
    ]


def test_deterministic_adapter_remains_its_own_foundation_batch():
    tasks = [
        _task("compat.py", 0, role="test_compat"),
        _task("app.py", 10, tests=["test_app.py"]),
    ]
    assert expand_to_verifiable_batch(tasks, [], ["compat.py"]) == ["compat.py"]
