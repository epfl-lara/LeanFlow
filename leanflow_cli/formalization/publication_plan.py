"""Bind publication ownership and exact before/after hashes to one accepted revision."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from leanflow_cli.formalization.evidence import digest, identifier, identity, normalized_relative


def _paths(values: Iterable[str]) -> list[str]:
    result = [normalized_relative(value) for value in values]
    aliases = [value.casefold() for value in result]
    if len(aliases) != len(set(aliases)):
        raise ValueError("Publication paths collide under portable case folding")
    return sorted(result)


def _permitted(path: str, protected: list[str]) -> bool:
    parts = path.split("/")
    if any(part.casefold() in {".git", ".lake", ".leanflow"} for part in parts):
        return False
    if parts[-1].casefold() in {
        "lakefile.lean",
        "lakefile.toml",
        "lake-manifest.json",
        "lean-toolchain",
    }:
        return False
    key = path.casefold()
    if any(
        key == value.casefold()
        or key.startswith(value.casefold() + "/")
        or value.casefold().startswith(key + "/")
        for value in protected
    ):
        return False
    return (
        path.endswith(".lean")
        or parts[-1] == "Blueprint.md"
        or (len(parts) > 2 and parts[0] == "formalization")
    )


def freeze_publication_plan(
    revision_id: str,
    before: Mapping[str, bytes | None],
    outputs: Mapping[str, bytes],
    *,
    owned_paths: Iterable[str],
    protected_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Freeze accepted bytes and explicit ownership without observing live destinations."""
    identifier(revision_id)
    paths = _paths(outputs)
    if not paths or set(paths) != set(before):
        raise ValueError("Publication requires an exact before/after write set")
    owned = _paths(owned_paths)
    protected = _paths(protected_paths)
    if not set(paths).issubset(owned):
        raise ValueError("Publication contains a path outside the accepted ownership plan")
    if any(not _permitted(path, protected) for path in paths):
        raise ValueError(
            "Publication cannot modify protected sources, configuration or runtime state"
        )
    if any(
        other.casefold().startswith(path.casefold() + "/")
        for path in paths
        for other in paths
        if other != path
    ):
        raise ValueError("A publication file cannot also be another output's directory")
    payload = {
        "schema_version": 1,
        "revision_id": revision_id,
        "owned_paths": owned,
        "protected_paths": protected,
        "writes": {
            path: {
                "before": digest(previous) if (previous := before[path]) is not None else None,
                "after": digest(outputs[path]),
            }
            for path in paths
        },
    }
    return {**payload, "plan_id": identity(payload)}


def validate_publication_plan(
    plan: Mapping[str, Any],
    revision_id: str,
    before: Mapping[str, bytes | None],
    outputs: Mapping[str, bytes],
) -> dict[str, Any]:
    """Reject altered plans and byte payloads instead of adopting a fresh live baseline."""
    expected = freeze_publication_plan(
        revision_id,
        before,
        outputs,
        owned_paths=plan.get("owned_paths", []),
        protected_paths=plan.get("protected_paths", []),
    )
    if dict(plan) != expected:
        raise ValueError("Publication bytes or ownership no longer match the frozen accepted plan")
    return expected
