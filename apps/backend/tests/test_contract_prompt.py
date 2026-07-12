"""contract_sections(): DEFINES/CALLS prompt fragment from the manifest (R1 Task 5)."""
from portage_agent.agent.nodes.execute import contract_sections

MANIFEST = {
    "db.py::get_db": {
        "module": "db.py", "symbol": "get_db", "kind": "function",
        "original": "def get_db()",
        "target_note": "get_db: becomes ONE yield dependency",
        "notes": "", "call_sites": ["rows = get_db().all()"],
    },
}


def test_defines_side_states_target_not_just_original():
    frag = contract_sections(MANIFEST, "db.py")
    assert "DEFINES" in frag
    assert "yield dependency" in frag           # the TARGET decision
    assert "def get_db()" in frag               # original shown for reference
    assert "rows = get_db().all()" in frag      # current call sites to honor/adapt


def test_calls_side_for_a_consumer_file():
    frag = contract_sections(MANIFEST, "blog.py", consumed={"db.py::get_db"})
    assert "CALLS" in frag and "yield dependency" in frag
    assert "DEFINES" not in frag                # blog.py defines nothing contracted


def test_empty_manifest_is_silent():
    assert contract_sections({}, "db.py") == ""


def test_defines_side_lists_required_companion_exports():
    manifest = {
        "db.py::get_db": {
            **MANIFEST["db.py::get_db"],
            "additional_exports": ["get_db_dep"],
        }
    }
    frag = contract_sections(manifest, "db.py")
    assert "REQUIRED ADDITIONAL EXPORTS: get_db_dep" in frag
