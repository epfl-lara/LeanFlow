"""Reuse source-bound parent verification for immediate helper reinspection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_BANKED_HELPER_STATE_ATTR = "_managed_banked_helper_inspections"


def _canonical_file(value: object, *, project_root: str) -> Path | None:
    """Resolve one file path without requiring a Lean project import."""
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(project_root).expanduser() / path
    return path.resolve(strict=False)


def _source_sha256(path: Path) -> str:
    """Return the current raw source digest or an empty fail-closed marker."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def remember(
    agent: Any,
    *,
    active_file: str,
    helper_verifications: Mapping[str, Mapping[str, Any]],
    project_root: str,
) -> tuple[str, ...]:
    """Remember exact helper gates under the current source revision."""
    path = _canonical_file(active_file, project_root=project_root)
    if path is None:
        return ()
    source_sha256 = _source_sha256(path)
    if not source_sha256:
        return ()
    raw_state = getattr(agent, _BANKED_HELPER_STATE_ATTR, None)
    state = dict(raw_state) if isinstance(raw_state, Mapping) else {}
    remembered: list[str] = []
    for raw_name, raw_verification in helper_verifications.items():
        helper_name = str(raw_name or "").strip()
        verification = dict(raw_verification or {})
        if (
            not helper_name
            or verification.get("ok") is not True
            or int(verification.get("errors", 0) or 0) != 0
            or int(verification.get("sorry", 0) or 0) != 0
        ):
            continue
        state[helper_name] = {
            "helper_symbol": helper_name,
            "active_file": str(path),
            "source_sha256": source_sha256,
            "verification": verification,
        }
        remembered.append(helper_name)
    setattr(agent, _BANKED_HELPER_STATE_ATTR, state)
    return tuple(remembered)


def reused_lean_inspection(
    agent: Any,
    function_name: str,
    arguments: Mapping[str, Any] | None,
    *,
    project_root: str,
) -> dict[str, Any] | None:
    """Return a no-Lean inspection result for an unchanged just-banked helper.

    Only an exact symbol-scoped ``lean_inspect`` is eligible. File-wide
    inspections, changed source, missing gate evidence, or a different helper
    continue to the real Lean service.
    """
    if str(function_name or "") != "lean_inspect":
        return None
    args = dict(arguments or {})
    helper_name = str(args.get("symbol", "") or "").strip()
    requested_file = _canonical_file(
        args.get("target", "") or args.get("file_path", ""),
        project_root=project_root,
    )
    if not helper_name or requested_file is None:
        return None
    raw_state = getattr(agent, _BANKED_HELPER_STATE_ATTR, None)
    state = dict(raw_state) if isinstance(raw_state, Mapping) else {}
    raw_record = state.get(helper_name)
    record = dict(raw_record) if isinstance(raw_record, Mapping) else {}
    if not record or str(record.get("active_file", "") or "") != str(requested_file):
        return None
    current_sha256 = _source_sha256(requested_file)
    if not current_sha256 or current_sha256 != str(record.get("source_sha256", "") or ""):
        state.pop(helper_name, None)
        setattr(agent, _BANKED_HELPER_STATE_ATTR, state)
        return None

    verification = dict(record.get("verification") or {})
    raw_blockers = verification.get("axiom_profile_blockers")
    blockers = (
        [str(item) for item in raw_blockers]
        if isinstance(raw_blockers, Sequence) and not isinstance(raw_blockers, (str, bytes))
        else []
    )
    return {
        "success": True,
        "status": "parent_kernel_verification_reused",
        "target": str(requested_file),
        "project_root": str(Path(project_root).expanduser().resolve(strict=False)),
        "inspection_scope": "symbol",
        "inspected_symbol": helper_name,
        "parent_kernel_verified": True,
        "valid_without_sorry": True,
        "axiom_profile_checked": verification.get("axiom_profile_checked") is True,
        "axiom_profile_blockers": blockers,
        "lean_started": False,
        "source_sha256": current_sha256,
        "diagnostics": json.dumps(
            {
                "items": [],
                "note": "exact parent helper gate already accepted this unchanged declaration",
            },
            ensure_ascii=False,
        ),
        "goals": json.dumps(
            {
                "line_context": helper_name,
                "goals": None,
                "goals_before": [],
                "goals_after": [],
            },
            ensure_ascii=False,
        ),
        "message": (
            "Lean inspection was not rerun: the parent manager already elaborated this exact "
            "helper without sorry and checked its axiom policy at the current source revision. "
            "Use read_file if source text or location is needed; continue with the unresolved target."
        ),
        "verification": verification,
    }
