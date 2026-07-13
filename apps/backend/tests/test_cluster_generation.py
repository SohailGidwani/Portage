"""R1.1: strict parsing for coordinated multi-file generation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import portage_agent.agent.nodes.execute as execute_module
from portage_agent.agent.nodes.execute import (
    ClusterOutputError,
    _migrate_cluster,
    _migrate_file,
    extract_cluster_files,
)
from portage_agent.recipes.base import PlannedFile


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


@pytest.mark.asyncio
async def test_cluster_parse_error_preserves_spent_usage(tmp_path, monkeypatch):
    llm = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(
        text="<<<PORTAGE_FILE:pkg/app.py>>>\n```python\nx = 1\n```",
        prompt_tokens=10, completion_tokens=2, cost_usd=0.01,
    )))
    monkeypatch.setattr(execute_module, "get_llm", lambda: llm)
    recipe = SimpleNamespace(
        name="test", system_prompt=lambda: "system",
        build_cluster_prompt=lambda **kwargs: "user",
    )

    with pytest.raises(ClusterOutputError, match="missing paths") as caught:
        await _migrate_cluster(
            recipe,
            planned_files=[
                PlannedFile("pkg/app.py", "app_factory"),
                PlannedFile("pkg/db.py", "support"),
            ],
            sources={"pkg/app.py": "", "pkg/db.py": ""},
            context={}, model="model", manifest={}, seam_plan={},
            binding_root=str(tmp_path),
        )

    assert caught.value.usage == {
        "prompt_tokens": 10, "completion_tokens": 2, "cost_usd": 0.01,
    }


@pytest.mark.asyncio
async def test_file_repair_can_use_the_rejected_draft_as_source(tmp_path, monkeypatch):
    seen = {}
    llm = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(
        text="```python\nx = 2\n```", prompt_tokens=4, completion_tokens=2,
        cost_usd=0.005,
    )))
    monkeypatch.setattr(execute_module, "get_llm", lambda: llm)

    def prompt(**kwargs):
        seen["source"] = kwargs["source"]
        return "repair"

    recipe = SimpleNamespace(
        system_prompt=lambda: "system", build_user_prompt=prompt,
    )
    content, _ = await _migrate_file(
        recipe, str(tmp_path), path="pkg/app.py", role="app_factory",
        model="model", subtasks=[], context={}, verify_errors="fix it",
        source_override="x = 1\n",
    )

    assert seen["source"] == "x = 1\n"
    assert content == "x = 2\n"
