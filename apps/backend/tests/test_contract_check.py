"""contract_violations(): export-presence + pinned-shape AST gate (R1 Task 6).

Also covers the Task 6 review fixes (task-6-review.md):
  F1 — draft selection (_pick_draft) must not let an unparseable repair beat a valid draft.
  F2 — presence check must see definitions inside module-level try/if blocks (compat
       shims), but NOT inside `if TYPE_CHECKING:` (never runs at import time)."""
from portage_agent.agent.nodes.execute import (
    _pick_draft,
    all_generation_violations,
    contract_violations,
)


def _pin(symbol, original, target_note="keep the original shape", kind="function"):
    return {"module": "db.py", "symbol": symbol, "kind": kind, "original": original,
            "target_note": target_note, "notes": "", "call_sites": [],
            "shape": ({"required_positional": 0, "required_keyword_only": [],
                       "is_async": False, "is_generator": False}
                      if kind == "function" else {})}


def test_missing_export_flagged():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    v = contract_violations("def other():\n    pass\n", m, "db.py")
    assert v and "get_db" in v[0] and "missing" in v[0]


def test_undefined_global_is_rejected_before_runtime():
    bad = "def init_app(app):\n    return Path(__file__).parent\n"
    assert all_generation_violations(bad, {}, "db.py") == [
        "db.py: undefined global name `Path`; import or define it",
    ]
    good = "from pathlib import Path\ndef init_app(app):\n    return Path(__file__).parent\n"
    assert all_generation_violations(good, {}, "db.py") == []


def test_undefined_global_check_defers_to_wildcard_import():
    source = "from framework import *\ndef route():\n    return injected_name\n"
    assert all_generation_violations(source, {}, "views.py") == []


def test_reexport_and_assignment_count_as_defined():
    m = {"db.py::get_db": _pin("get_db", "def get_db()"),
         "db.py::router": _pin("router", "router = APIRouter()", kind="variable")}
    src = "from impl import get_db\nrouter = object()\n"
    assert contract_violations(src, m, "db.py") == []


def test_grown_required_args_flagged_when_shape_kept():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    v = contract_violations("def get_db(request):\n    pass\n", m, "db.py")
    assert v and "required arg" in v[0]


def test_grown_args_ok_when_pin_redefines_shape():
    m = {"db.py::get_db": _pin("get_db", "def get_db()",
                               target_note="becomes a yield dependency")}
    assert contract_violations("def get_db(request):\n    yield 1\n", m, "db.py") == []


def test_async_original_not_falsely_flagged():
    # v2 bug: the checker re-parsed the signature STRING, which serialized async
    # originals as sync — shape facts come from the manifest now.
    m = {"svc.py::fetch": _pin("fetch", "async def fetch(url)",
                               kind="function")}
    m["svc.py::fetch"]["shape"] = {"required_positional": 1,
                                   "required_keyword_only": [],
                                   "is_async": True, "is_generator": False}
    m["svc.py::fetch"]["module"] = "svc.py"
    assert contract_violations("async def fetch(url):\n    return url\n", m, "svc.py") == []


def test_generator_flip_flagged():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    m["db.py::get_db"]["shape"] = {"required_positional": 0,
                                   "required_keyword_only": [],
                                   "is_async": False, "is_generator": True}
    v = contract_violations("def get_db():\n    return 1\n", m, "db.py")
    assert v and "generator" in v[0]


def test_decorator_factory_must_still_return_a_local_wrapper():
    pin = _pin("login_required", "def login_required(view)") | {
        "module": "auth.py",
        "preserve_shape": True,
        "shape": {
            "required_positional": 1,
            "required_keyword_only": [],
            "is_async": False,
            "is_generator": False,
            "returns_nested_function": True,
        },
    }
    manifest = {"auth.py::login_required": pin}

    bad = "def login_required(request):\n    return request.session\n"
    assert any(
        "wrapper/decorator" in violation
        for violation in contract_violations(bad, manifest, "auth.py")
    )

    good = (
        "def login_required(view):\n"
        "    def wrapped(**kwargs):\n"
        "        return view(**kwargs)\n"
        "    return wrapped\n"
    )
    assert contract_violations(good, manifest, "auth.py") == []


def test_new_required_kwonly_flagged():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    v = contract_violations("def get_db(*, timeout):\n    pass\n", m, "db.py")
    assert v and "keyword-only" in v[0]


def test_unparseable_flags_everything():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    v = contract_violations("def broken(:\n", m, "db.py")
    assert v and "unparseable" in v[0]


def test_other_modules_pins_ignored():
    m = {"auth.py::login": _pin("login", "def login()") | {"module": "auth.py"}}
    assert contract_violations("x = 1\n", m, "db.py") == []


def test_custom_pin_can_preserve_shape_and_require_companion_export():
    pin = _pin("get_db", "def get_db()", target_note="keep helper; add dependency")
    pin |= {
        "preserve_shape": True,
        "target_kind": "function",
        "additional_exports": ["get_db_dep"],
    }
    manifest = {"db.py::get_db": pin}
    violations = contract_violations("def get_db(request):\n    return request\n", manifest,
                                     "db.py")
    assert any("required arg" in v for v in violations)
    assert any("get_db_dep" in v for v in violations)


def test_target_kind_change_is_flagged():
    pin = _pin("get_db", "def get_db()") | {
        "preserve_shape": True,
        "target_kind": "function",
        "additional_exports": [],
    }
    violations = contract_violations("get_db = object()\n", {"db.py::get_db": pin}, "db.py")
    assert any("target kind changed" in v for v in violations)


# --- F2: presence check must see conditional/try-block module-level definitions ---


def test_try_except_shim_def_counts_as_defined():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    src = (
        "try:\n"
        "    from impl import get_db\n"
        "except ImportError:\n"
        "    def get_db():\n"
        "        return None\n"
    )
    assert contract_violations(src, m, "db.py") == []


def test_if_else_conditional_def_counts_as_defined():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    src = (
        "import sys\n"
        "if sys.version_info >= (3, 12):\n"
        "    def get_db():\n"
        "        return 1\n"
        "else:\n"
        "    def get_db():\n"
        "        return 2\n"
    )
    assert contract_violations(src, m, "db.py") == []


def test_type_checking_only_def_still_flagged():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    def get_db():\n"
        "        ...\n"
    )
    v = contract_violations(src, m, "db.py")
    assert v and "get_db" in v[0] and "missing" in v[0]


def test_conditional_def_shape_violation_still_flagged():
    # F2's fix must not loosen the shape check — a conditionally-defined symbol whose
    # shape violates the pin is still flagged.
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    src = (
        "try:\n"
        "    def get_db(request):\n"
        "        return request\n"
        "except Exception:\n"
        "    pass\n"
    )
    v = contract_violations(src, m, "db.py")
    assert v and "required arg" in v[0]


# --- F1: repair-draft selection must be parse-guarded ---


def test_pick_draft_valid_repair_with_fewer_violations_wins():
    m = {"db.py::get_db": _pin("get_db", "def get_db()"),
         "db.py::helper": _pin("helper", "def helper()")}
    content1 = "x = 1\n"  # both missing -> 2 violations
    broken1 = contract_violations(content1, m, "db.py")
    assert len(broken1) == 2
    content2 = "def get_db():\n    pass\n"  # helper still missing -> 1 violation
    chosen, broken = _pick_draft(content1, broken1, content2, m, "db.py")
    assert chosen == content2
    assert len(broken) == 1


def test_pick_draft_unparseable_repair_never_wins():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    content1 = "def get_db(a):\n    pass\n"  # valid python, 1 shape violation
    broken1 = contract_violations(content1, m, "db.py")
    assert broken1
    content2 = "I cannot help with that request."
    chosen, broken = _pick_draft(content1, broken1, content2, m, "db.py")
    assert chosen == content1
    assert broken == broken1


def test_pick_draft_parseable_repair_wins_tie():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    content1 = "def get_db(a):\n    pass\n"  # 1 violation: grown required args
    broken1 = contract_violations(content1, m, "db.py")
    assert len(broken1) == 1
    content2 = "def get_db(*, timeout):\n    pass\n"  # different 1 violation: new kwonly
    chosen, broken = _pick_draft(content1, broken1, content2, m, "db.py")
    assert len(broken) == 1
    assert chosen == content2


def test_pick_draft_unparseable_draft1_parseable_repair_wins():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    content1 = "def broken(:\n"
    broken1 = contract_violations(content1, m, "db.py")
    assert len(broken1) == 1 and "unparseable" in broken1[0]
    content2 = "def get_db():\n    pass\n"  # 0 violations
    chosen, broken = _pick_draft(content1, broken1, content2, m, "db.py")
    assert chosen == content2
    assert broken == []


def test_pick_draft_unparseable_draft1_loses_even_to_worse_scoring_parseable_repair():
    # F1b: an unparseable draft 1 collapses to exactly 1 violation, so a parseable
    # repair with >=2 real violations used to LOSE the count comparison and the
    # guaranteed-crash draft got written. Parseable must beat unparseable
    # unconditionally, regardless of violation counts.
    m = {"db.py::get_db": _pin("get_db", "def get_db()"),
         "db.py::helper": _pin("helper", "def helper()")}
    content1 = "def broken(:\n"
    broken1 = contract_violations(content1, m, "db.py")
    assert len(broken1) == 1 and "unparseable" in broken1[0]
    content2 = "x = 1\n"  # parseable, but both pins missing -> 2 violations
    broken2 = contract_violations(content2, m, "db.py")
    assert len(broken2) == 2
    chosen, broken = _pick_draft(content1, broken1, content2, m, "db.py")
    assert chosen == content2
    assert broken == broken2


def test_pick_draft_both_unparseable_keeps_draft1():
    m = {"db.py::get_db": _pin("get_db", "def get_db()")}
    content1 = "def broken(:\n"
    broken1 = contract_violations(content1, m, "db.py")
    content2 = "I cannot help with that request."
    chosen, broken = _pick_draft(content1, broken1, content2, m, "db.py")
    assert chosen == content1
    assert broken == broken1
