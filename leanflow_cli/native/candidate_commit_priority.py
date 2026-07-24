"""Recognize clean temporary candidates that warrant foreground commit priority."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence, Set
from pathlib import Path
from typing import Any

from core.project_resource_admission import MAX_FOREGROUND_HANDOFF_LEASE_S
from leanflow_cli.lean.lean_parsing import _declaration_line_index_from_text

CANDIDATE_COMMIT_HANDOFF_DEFAULT_S = 60.0
_CANDIDATE_COMMIT_HANDOFF_ENV = "LEANFLOW_CANDIDATE_COMMIT_HANDOFF_S"


def _configured_handoff_seconds() -> float:
    """Return the bounded candidate-to-commit priority window."""
    raw = str(
        os.getenv(
            _CANDIDATE_COMMIT_HANDOFF_ENV,
            CANDIDATE_COMMIT_HANDOFF_DEFAULT_S,
        )
        or ""
    ).strip()
    try:
        configured = float(raw)
    except ValueError:
        configured = CANDIDATE_COMMIT_HANDOFF_DEFAULT_S
    if not math.isfinite(configured):
        configured = CANDIDATE_COMMIT_HANDOFF_DEFAULT_S
    return max(0.0, min(MAX_FOREGROUND_HANDOFF_LEASE_S, configured))


def _payload(result: str) -> dict[str, Any]:
    """Return one JSON object from a registry tool result."""
    try:
        parsed = json.loads(str(result or ""))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _canonical_file(value: object, *, cwd: str) -> str:
    """Resolve a tool or assignment path without requiring it to exist."""
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        base = str(cwd or os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd()).strip()
        path = Path(base).expanduser() / path
    return str(path.resolve(strict=False))


def _checked_axioms_are_allowed(
    payload: Mapping[str, Any],
    allowed_axioms: Set[str],
) -> bool:
    """Return whether complete inline axiom evidence is present and clean."""
    if (
        payload.get("axiom_profile_requested") is not True
        or payload.get("axiom_profile_checked") is not True
        or "axiom_profile_axioms" not in payload
        or str(payload.get("axiom_profile_error", "") or "").strip()
    ):
        return False
    raw_axioms = payload.get("axiom_profile_axioms")
    if isinstance(raw_axioms, (str, bytes)) or not isinstance(raw_axioms, Sequence):
        return False
    axioms = {str(value or "").strip() for value in raw_axioms if str(value or "").strip()}
    if axioms - {str(value or "").strip() for value in allowed_axioms}:
        return False
    blockers = payload.get("axiom_profile_blockers")
    if blockers not in (None, [], ()):
        return False
    return True


def handoff_seconds(
    function_name: str,
    arguments: Mapping[str, Any] | None,
    result: str,
    *,
    assignment: Mapping[str, Any] | None,
    allowed_axioms: Set[str],
    pending_helper: Mapping[str, Any] | None = None,
) -> float:
    """Return commit-priority seconds for one fully clean assigned candidate.

    Every condition is fail-closed. A clean single-declaration helper check is
    assignment-bound even when the model authored it directly; malformed,
    multi-declaration, incomplete-profile, or stale checks retain the normal
    one-second foreground handoff instead of receiving this extended lease.
    """
    if str(function_name or "") != "lean_incremental_check":
        return 0.0
    args = dict(arguments or {})
    checked = _payload(result)
    current = dict(assignment or {})
    action = str(checked.get("action", "") or args.get("action", "") or "")
    action = action.strip().lower().replace("-", "_")
    target = str(current.get("target_symbol", "") or "").strip()
    requested_target = str(
        args.get("theorem_id", "") or args.get("target_symbol", "") or ""
    ).strip()
    checked_target = str(checked.get("target", "") or "").strip()
    replacement = str(args.get("replacement", "") or "").strip()
    replacement_source_names = tuple(
        str(entry.get("name", "") or "").strip()
        for entry in _declaration_line_index_from_text(replacement)
        if str(entry.get("name", "") or "").strip()
    )
    cwd = str(args.get("cwd", "") or "").strip()
    active_file = _canonical_file(current.get("active_file"), cwd=cwd)
    requested_file = _canonical_file(
        args.get("file_path", "") or args.get("active_file", ""),
        cwd=cwd,
    )
    checked_file = _canonical_file(checked.get("file", ""), cwd=cwd)
    raw_declarations = checked.get("replacement_declarations")
    replacement_declaration_names = (
        tuple(str(name or "").strip() for name in raw_declarations)
        if isinstance(raw_declarations, Sequence) and not isinstance(raw_declarations, (str, bytes))
        else ()
    )
    replacement_declarations = {name for name in replacement_declaration_names if name}

    if action == "check_helper":
        pending = dict(pending_helper or {})
        pending_target = str(pending.get("target_symbol", "") or "").strip()
        pending_file = _canonical_file(pending.get("active_file", ""), cwd=cwd)
        helper_name = str(pending.get("helper_name", "") or "").strip()
        pending_declaration = str(pending.get("declaration", "") or "").strip()
        active_pending = bool(
            str(pending.get("state", "") or "").strip() == "ready_to_integrate"
            and pending_target
            and pending_target == target
            and pending_file
            and pending_file == active_file
        )
        if (
            not replacement
            or not target
            or requested_target != target
            or checked_target != target
            or not active_file
            or requested_file != active_file
            or checked_file != active_file
            or len(replacement_declaration_names) != 1
            or len(replacement_declarations) != 1
            or replacement_source_names != replacement_declaration_names
            or checked.get("success") is not True
            or checked.get("ok") is not True
            or checked.get("valid_without_sorry") is not True
            or checked.get("has_errors") is not False
            or checked.get("has_sorry") is not False
            or bool(checked.get("timed_out"))
            or checked.get("replacement_matches_target") is not False
            or str(checked.get("verification_scope", "") or "") != "helper_candidate"
            or not _checked_axioms_are_allowed(checked, allowed_axioms)
        ):
            return 0.0
        if active_pending and (
            not helper_name
            or helper_name not in replacement_declarations
            or not pending_declaration
            or replacement != pending_declaration
        ):
            # A ready durable candidate owns the exact integration window.
            # Do not let a different scratch helper displace it merely because
            # that helper also elaborates in isolation.
            return 0.0
        return _configured_handoff_seconds()

    if (
        action != "check_target"
        or not replacement
        or not target
        or requested_target != target
        or checked_target != target
        or not active_file
        or requested_file != active_file
        or checked_file != active_file
        or checked.get("success") is not True
        or checked.get("ok") is not True
        or checked.get("valid_without_sorry") is not True
        or checked.get("has_errors") is not False
        or checked.get("has_sorry") is not False
        or bool(checked.get("timed_out"))
        or checked.get("replacement_matches_target") is not True
        or str(checked.get("verification_scope", "") or "") != "target_candidate"
        or target not in replacement_declarations
        or not _checked_axioms_are_allowed(checked, allowed_axioms)
    ):
        return 0.0
    return _configured_handoff_seconds()
