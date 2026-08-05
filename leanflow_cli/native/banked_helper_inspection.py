"""Reuse source-bound parent verification for immediate helper reinspection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _declaration_entries_by_name_from_text

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


def _declaration_sha256_by_name(source: str) -> dict[str, str]:
    """Return exact declaration digests keyed by their parsed source names."""
    return {
        name: hashlib.sha256(str(entry.get("text", "") or "").encode("utf-8")).hexdigest()
        for name, entry in _declaration_entries_by_name_from_text(source).items()
        if str(entry.get("text", "") or "").strip()
    }


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
    try:
        declaration_digests = _declaration_sha256_by_name(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        declaration_digests = {}
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
            "declaration_sha256": declaration_digests.get(helper_name, ""),
            "verification": verification,
        }
        remembered.append(helper_name)
    setattr(agent, _BANKED_HELPER_STATE_ATTR, state)
    return tuple(remembered)


def current_verified_helper_names(
    agent: Any,
    *,
    active_file: str,
    project_root: str,
) -> tuple[str, ...]:
    """Return authenticated helpers still present unchanged in current source.

    An exact whole-file revision match supports records created by older LeanFlow
    versions. New records also retain the helper declaration digest, allowing an
    unrelated target edit to change the file while preserving helper authority.
    """
    path = _canonical_file(active_file, project_root=project_root)
    if path is None:
        return ()
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    current_source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    declaration_digests = _declaration_sha256_by_name(source)
    raw_state = getattr(agent, _BANKED_HELPER_STATE_ATTR, None)
    state = dict(raw_state) if isinstance(raw_state, Mapping) else {}
    verified: list[str] = []
    for raw_name, raw_record in state.items():
        helper_name = str(raw_name or "").strip()
        record = dict(raw_record) if isinstance(raw_record, Mapping) else {}
        if (
            not helper_name
            or str(record.get("active_file", "") or "") != str(path)
            or helper_name not in declaration_digests
        ):
            continue
        exact_source = str(record.get("source_sha256", "") or "") == current_source_sha256
        recorded_declaration = str(record.get("declaration_sha256", "") or "")
        exact_declaration = bool(
            recorded_declaration and declaration_digests.get(helper_name) == recorded_declaration
        )
        if exact_source or exact_declaration:
            verified.append(helper_name)
    return tuple(sorted(set(verified)))


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
        # Exact inspection reuse is whole-file scoped, but keep the declaration
        # record available for source-index handoffs. Its own digest still
        # decides whether helper authority survives unrelated source drift.
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
