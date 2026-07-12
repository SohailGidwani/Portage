"""R1.1: strict parsing for coordinated multi-file generation."""

import pytest

from portage_agent.agent.nodes.execute import extract_cluster_files


def test_extract_cluster_files_accepts_any_block_order():
    text = (
        "<<<PORTAGE_FILE:tests/conftest.py>>>\n"
        "```python\nclient = 1\n```\n"
        "<<<PORTAGE_END_FILE>>>\n"
        "<<<PORTAGE_FILE:pkg/db.py>>>\n"
        "```python\ndef get_db():\n    return 1\n```\n"
        "<<<PORTAGE_END_FILE>>>\n"
    )
    files = extract_cluster_files(text, ["pkg/db.py", "tests/conftest.py"])
    assert files["pkg/db.py"].startswith("def get_db")
    assert files["tests/conftest.py"] == "client = 1\n"


def test_extract_cluster_files_rejects_missing_or_duplicate_members():
    missing = (
        "<<<PORTAGE_FILE:pkg/db.py>>>\n```python\nx = 1\n```\n"
        "<<<PORTAGE_END_FILE>>>\n"
    )
    with pytest.raises(ValueError, match="missing"):
        extract_cluster_files(missing, ["pkg/db.py", "tests/conftest.py"])

    duplicate = missing + missing
    with pytest.raises(ValueError, match="duplicate"):
        extract_cluster_files(duplicate, ["pkg/db.py"])


def test_extract_cluster_files_rejects_unexpected_path():
    text = (
        "<<<PORTAGE_FILE:other.py>>>\n```python\nx = 1\n```\n"
        "<<<PORTAGE_END_FILE>>>\n"
    )
    with pytest.raises(ValueError, match="unexpected"):
        extract_cluster_files(text, ["pkg/db.py"])
