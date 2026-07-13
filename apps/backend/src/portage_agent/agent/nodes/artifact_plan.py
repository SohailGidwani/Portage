"""Validation and reconstruction for recipe-proposed target architecture artifacts."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from portage_agent.recipes.base import MAX_CREATED_ARTIFACTS, PlannedFile, Subtask

from .redaction import is_denied_path

MAX_PURPOSE_CHARS = 600
MAX_INSTRUCTION_CHARS = 2400
_KINDS = {"function", "class", "variable"}
_SIGNATURE_NAME = {
    "class": re.compile(r"^\s*class\s+([A-Za-z_]\w*)\b"),
    "function": re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\b"),
}


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.isidentifier():
        raise ValueError(f"{label} must be a valid Python identifier")
    return value


def _artifact_path(value: object, existing: set[str]) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("artifact path must be a nonempty string")
    candidate = PurePosixPath(value)
    normalized = candidate.as_posix()
    if candidate.is_absolute() or normalized != value or ".." in candidate.parts:
        raise ValueError(f"artifact path must be normalized and repository-relative: {value}")
    if candidate.suffix != ".py":
        raise ValueError(f"created artifact must be a Python file: {value}")
    if is_denied_path(value):
        raise ValueError(f"artifact path is denied by repository safety policy: {value}")
    if value in existing:
        raise ValueError(f"create artifact collides with an existing source file: {value}")
    return value


def parse_artifact_plan(
    text: str, *, existing_files: set[str], rewrite_paths: set[str],
    max_artifacts: int = MAX_CREATED_ARTIFACTS,
) -> list[dict]:
    """Parse and strictly validate one architect JSON response.

    The accepted top-level value is a list. Returning ``[]`` is a valid architectural
    decision. Any invalid member rejects the entire plan so partially trusted model output
    can never create files.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"architect response is not strict JSON: {exc.msg}") from exc
    if not isinstance(raw, list):
        raise ValueError("architect response must be a JSON list")
    if len(raw) > max_artifacts:
        raise ValueError(
            f"architect proposed {len(raw)} artifacts; maximum is {max_artifacts}"
        )

    proposed_paths: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"artifact {index} must be a JSON object")
        proposed_paths.append(_artifact_path(item.get("path"), existing_files))
    if len(set(proposed_paths)) != len(proposed_paths):
        raise ValueError("architect proposed duplicate artifact paths")
    known_paths = rewrite_paths | set(proposed_paths)

    plan: list[dict] = []
    for index, (item, path) in enumerate(zip(raw, proposed_paths, strict=True)):
        purpose = item.get("purpose")
        instructions = item.get("instructions")
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError(f"artifact {index} purpose must be nonempty")
        if len(purpose) > MAX_PURPOSE_CHARS:
            raise ValueError(f"artifact {index} purpose exceeds {MAX_PURPOSE_CHARS} chars")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(f"artifact {index} instructions must be nonempty")
        if len(instructions) > MAX_INSTRUCTION_CHARS:
            raise ValueError(
                f"artifact {index} instructions exceed {MAX_INSTRUCTION_CHARS} chars"
            )
        role = item.get("role", "support")
        _identifier(role, f"artifact {index} role")
        capabilities = item.get("capabilities", [])
        if not isinstance(capabilities, list):
            raise ValueError(f"artifact {index} capabilities must be a list")
        normalized_capabilities = [
            _identifier(value, f"artifact {index} capability")
            for value in capabilities
        ]
        if len(set(normalized_capabilities)) != len(normalized_capabilities):
            raise ValueError(f"artifact {index} has duplicate capabilities")

        exports = item.get("exports")
        if not isinstance(exports, list) or not exports:
            raise ValueError(f"artifact {index} must declare at least one export")
        normalized_exports: list[dict] = []
        export_names: set[str] = set()
        for export_index, export in enumerate(exports):
            if not isinstance(export, dict):
                raise ValueError(
                    f"artifact {index} export {export_index} must be an object"
                )
            name = _identifier(
                export.get("name"), f"artifact {index} export {export_index} name",
            )
            if name in export_names:
                raise ValueError(f"artifact {index} has duplicate export {name}")
            export_names.add(name)
            kind = export.get("kind")
            if kind not in _KINDS:
                raise ValueError(
                    f"artifact {index} export {name} kind must be one of {sorted(_KINDS)}"
                )
            members = export.get("members", [])
            if not isinstance(members, list):
                raise ValueError(f"artifact {index} export {name} members must be a list")
            normalized_members = [
                _identifier(member, f"artifact {index} export {name} member")
                for member in members
            ]
            if len(set(normalized_members)) != len(normalized_members):
                raise ValueError(f"artifact {index} export {name} has duplicate members")
            signature = export.get("signature", "")
            if not isinstance(signature, str) or len(signature) > 300:
                raise ValueError(f"artifact {index} export {name} has invalid signature")
            pattern = _SIGNATURE_NAME.get(kind)
            match = pattern.match(signature) if pattern else None
            if match and match.group(1) != name:
                raise ValueError(
                    f"artifact {index} export {name} signature declares "
                    f"{match.group(1)}; the frozen export name must be consistent"
                )
            normalized_exports.append({
                "name": name,
                "kind": kind,
                "signature": signature,
                "members": normalized_members,
            })

        consumers = item.get("consumers", [])
        depends_on = item.get("depends_on", [])
        if not isinstance(consumers, list) or not all(
            isinstance(value, str) for value in consumers
        ):
            raise ValueError(f"artifact {index} consumers must be a string list")
        if not isinstance(depends_on, list) or not all(
            isinstance(value, str) for value in depends_on
        ):
            raise ValueError(f"artifact {index} depends_on must be a string list")
        unknown_consumers = set(consumers) - rewrite_paths
        if unknown_consumers:
            raise ValueError(
                f"artifact {index} has unknown/non-rewrite consumers: "
                f"{sorted(unknown_consumers)}"
            )
        unknown_dependencies = set(depends_on) - known_paths
        if unknown_dependencies:
            raise ValueError(
                f"artifact {index} has unknown dependencies: {sorted(unknown_dependencies)}"
            )
        if path in depends_on:
            raise ValueError(f"artifact {index} cannot depend on itself")
        overlap = set(consumers) & set(depends_on)
        if overlap:
            raise ValueError(
                f"artifact {index} cannot depend on its declared consumers: "
                f"{sorted(overlap)}"
            )
        if len(set(consumers)) != len(consumers) or len(set(depends_on)) != len(depends_on):
            raise ValueError(f"artifact {index} has duplicate relationships")

        plan.append({
            "path": path,
            "role": role,
            "purpose": purpose.strip(),
            "instructions": instructions.strip(),
            "capabilities": normalized_capabilities,
            "exports": normalized_exports,
            "consumers": consumers,
            "depends_on": depends_on,
        })

    proposed = set(proposed_paths)
    pending = {
        item["path"]: set(item["depends_on"]) & proposed for item in plan
    }
    while ready := {path for path, dependencies in pending.items() if not dependencies}:
        pending = {
            path: dependencies - ready
            for path, dependencies in pending.items() if path not in ready
        }
    if pending:
        raise ValueError(
            f"artifact dependencies contain a cycle: {sorted(pending)}"
        )
    return plan


def artifact_planned_files(plan: list[dict]) -> list[PlannedFile]:
    """Rehydrate frozen JSON artifact proposals into normal recipe tasks."""
    return [
        PlannedFile(
            path=item["path"],
            role=item.get("role", "support"),
            subtasks=[Subtask(
                "create_artifact",
                "Create a planned target-architecture artifact",
                item["instructions"],
            )],
            order=5,
            action="create",
            purpose=item["purpose"],
            artifact_contract={
                "exports": item["exports"],
                "capabilities": item.get("capabilities", []),
                "consumers": item["consumers"],
                "depends_on": item["depends_on"],
                "instructions": item["instructions"],
            },
        )
        for item in plan
    ]
