"""Screen local tactic candidates with exact LeanProbe target checks."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_attempt_location import _multi_attempt_replacement_candidate
from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings

MULTI_ATTEMPT_PREPARE_TIMEOUT_S = 300
MULTI_ATTEMPT_CANDIDATE_TIMEOUT_S = 30
MULTI_ATTEMPT_FEEDBACK_CHARS = 900

IncrementalCheck = Callable[..., dict[str, Any]]


def _incremental_check_passed(check: Mapping[str, Any]) -> bool:
    """Return whether an incremental result exactly verified its target."""
    if not bool(check.get("success")):
        return False
    verified = check.get(
        "target_verified",
        check.get("verified", check.get("check_passed", check.get("ok", False))),
    )
    return bool(verified) and not bool(check.get("has_sorry"))


def _incremental_check_timed_out(check: Mapping[str, Any]) -> bool:
    """Return whether an incremental result exhausted its deterministic budget."""
    text = " ".join(
        str(check.get(key, "") or "") for key in ("error", "error_code", "output", "status")
    ).lower()
    return bool(check.get("timed_out")) or any(
        marker in text
        for marker in (
            "maximum number of heartbeats",
            "maxheartbeats",
            "deterministic timeout",
            "timed out",
            "timeout",
        )
    )


def _actionable_check_error(check: Mapping[str, Any]) -> str:
    """Return one bounded error-first diagnostic from an incremental check."""
    direct = str(check.get("error", "") or "").strip()
    if direct:
        return direct[:MULTI_ATTEMPT_FEEDBACK_CHARS]
    messages = [
        dict(message) for message in (check.get("messages") or []) if isinstance(message, Mapping)
    ]
    ordered = sorted(
        enumerate(messages),
        key=lambda item: (
            0 if str(item[1].get("severity", "") or "").strip().lower() == "error" else 1,
            item[0],
        ),
    )
    for _, message in ordered:
        text = str(message.get("message", "") or "").strip()
        if text:
            return text[:MULTI_ATTEMPT_FEEDBACK_CHARS]
    return str(check.get("output", "") or "").strip()[:MULTI_ATTEMPT_FEEDBACK_CHARS]


def _error_first_messages(check: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return diagnostics with actionable errors before warnings, preserving ties."""
    messages = [
        dict(message) for message in (check.get("messages") or []) if isinstance(message, Mapping)
    ]
    return [
        message
        for _, message in sorted(
            enumerate(messages),
            key=lambda item: (
                0 if str(item[1].get("severity", "") or "").strip().lower() == "error" else 1,
                item[0],
            ),
        )
    ]


def _exact_check_summary(check: Mapping[str, Any], *, verified: bool) -> dict[str, Any]:
    """Build the bounded exact-check fields exposed by multi-attempt results."""
    return {
        "success": verified,
        "backend_success": bool(check.get("success")),
        "target_verified": verified,
        "status": str(check.get("status", "") or ""),
        "error": _actionable_check_error(check),
        "error_code": str(check.get("error_code", "") or ""),
        "timed_out": _incremental_check_timed_out(check),
        "elapsed_s": check.get("elapsed_s", 0),
    }


def _placeholder_count(text: str) -> int:
    """Return executable placeholder count in one declaration replacement."""
    return len(
        re.findall(
            r"\b(?:sorry|admit|sorryAx)\b",
            _strip_lean_comments_and_strings(str(text or "")),
            flags=re.IGNORECASE,
        )
    )


def screen_multi_attempts_with_lean_probe(
    *,
    path: Path,
    line: int,
    column: int | None,
    attempts: Sequence[str],
    cwd: str,
    check_incrementally: IncrementalCheck,
) -> dict[str, Any] | None:
    """Check replaceable tactic-hole candidates directly with LeanProbe.

    Return ``None`` when the source position cannot be represented as a complete
    declaration replacement, allowing the caller to use positional LSP screening.
    Once a replaceable hole is found, deterministic timeouts remain on this
    bounded path and never trigger the more expensive LSP fallback.
    """
    replacements: list[tuple[str, str, str, int]] = []
    for snippet in attempts:
        replacement = _multi_attempt_replacement_candidate(path, line, column, snippet)
        if replacement is None:
            return None
        theorem_id, declaration = replacement
        replacements.append((snippet, theorem_id, declaration, _placeholder_count(declaration)))
    if not replacements:
        return None

    theorem_id = replacements[0][1]
    if any(candidate_theorem != theorem_id for _, candidate_theorem, _, _ in replacements):
        return None

    prepared = check_incrementally(
        action="prepare_file",
        file_path=str(path),
        theorem_id=theorem_id,
        cwd=cwd,
        include_tactics=False,
        timeout_s=MULTI_ATTEMPT_PREPARE_TIMEOUT_S,
    )
    if not bool(prepared.get("success")):
        if not _incremental_check_timed_out(prepared):
            return None
        return {
            "success": False,
            "backend_success": False,
            "backend_tool": "lean_probe",
            "screening_backend": "lean_probe",
            "target_verified": False,
            "verified_attempts": [],
            "exact_checks": [],
            "items": [],
            "status": "lean_probe_prepare_timeout",
            "timed_out": True,
            "error": str(prepared.get("error", "") or ""),
            "error_code": str(prepared.get("error_code", "") or ""),
            "action_required": (
                "LeanProbe preparation timed out. Keep the local proof step bounded and "
                "simplify or decompose the declaration before retrying."
            ),
        }

    items: list[dict[str, Any]] = []
    exact_checks: list[dict[str, Any]] = []
    verified_attempts: list[str] = []
    locally_verified_attempts: list[str] = []
    for index, (snippet, candidate_theorem, declaration, anchor_count) in enumerate(replacements):
        check = check_incrementally(
            action="check_target",
            file_path=str(path),
            theorem_id=candidate_theorem,
            cwd=cwd,
            replacement=declaration,
            include_tactics=False,
            timeout_s=MULTI_ATTEMPT_CANDIDATE_TIMEOUT_S,
            allow_placeholders_for_elaboration=anchor_count > 0,
        )
        verified = _incremental_check_passed(check)
        locally_verified = bool(
            anchor_count > 0
            and check.get("elaborated_with_placeholders") is True
            and check.get("replacement_matches_target") is True
            and str(check.get("verification_scope", "") or "") == "target_candidate"
            and not _incremental_check_timed_out(check)
        )
        exact_check = _exact_check_summary(check, verified=verified)
        exact_checks.append(
            {
                "snippet": snippet,
                "theorem_id": candidate_theorem,
                "unrelated_placeholder_anchors": anchor_count,
                "local_goal_verified": locally_verified,
                **exact_check,
            }
        )
        items.append(
            {
                "snippet": snippet,
                "goals": None,
                "goals_available": False,
                "diagnostics": _error_first_messages(check),
                "timed_out": exact_check["timed_out"],
                "probe_closed_goal": verified,
                "verified": verified,
                "local_goal_verified": locally_verified,
                "candidate_status": (
                    "target_verified"
                    if verified
                    else (
                        "local_goal_verified"
                        if locally_verified
                        else ("timed_out" if exact_check["timed_out"] else "rejected")
                    )
                ),
                "unrelated_placeholder_anchors": anchor_count,
                "exact_check": exact_check,
            }
        )
        if verified:
            verified_attempts.append(snippet)
        elif locally_verified:
            locally_verified_attempts.append(snippet)
        if verified or locally_verified:
            for skipped_snippet, _, _, _ in replacements[index + 1 :]:
                items.append(
                    {
                        "snippet": skipped_snippet,
                        "goals": None,
                        "goals_available": False,
                        "diagnostics": [],
                        "timed_out": False,
                        "probe_closed_goal": False,
                        "verified": False,
                        "local_goal_verified": False,
                        "candidate_status": "screening_skipped",
                        "screening_skipped": (
                            "earlier exact candidate verified"
                            if verified
                            else "earlier local-goal candidate verified"
                        ),
                    }
                )
            break

    target_verified = bool(verified_attempts)
    local_goal_verified = bool(locally_verified_attempts)
    success = target_verified or local_goal_verified
    payload: dict[str, Any] = {
        "success": success,
        "backend_success": True,
        "backend_tool": "lean_probe",
        "screening_backend": "lean_probe",
        "target_verified": target_verified,
        "verified_attempts": verified_attempts,
        "local_goal_verified": local_goal_verified,
        "locally_verified_attempts": locally_verified_attempts,
        "exact_checks": exact_checks,
        "items": items,
        "status": (
            "verified_candidate"
            if target_verified
            else (
                "locally_verified_candidate"
                if local_goal_verified
                else "screened_no_verified_candidate"
            )
        ),
        "prepare": {
            "success": bool(prepared.get("success")),
            "elapsed_s": prepared.get("elapsed_s", 0),
            "cache": dict(prepared.get("cache") or {}),
        },
    }
    if local_goal_verified:
        payload["action_required"] = (
            "The candidate closes the selected local goal with unrelated holes held as typed "
            "placeholders. It is not target-verified: apply the concrete tactic through the "
            "managed edit path and continue the remaining holes."
        )
    elif not success:
        payload["action_required"] = (
            "No tactic is exact-target verified. Use the returned LeanProbe diagnostics, "
            "simplify the local goal, or choose a structurally different route."
        )
    return payload
