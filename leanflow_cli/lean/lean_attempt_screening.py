"""Screen local tactic candidates with exact LeanProbe target checks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_attempt_location import _multi_attempt_replacement_candidate
from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings

MULTI_ATTEMPT_PREPARE_TIMEOUT_S = 300
MULTI_ATTEMPT_CANDIDATE_TIMEOUT_S = 30
MULTI_ATTEMPT_FEEDBACK_CHARS = 900
MULTI_ATTEMPT_PROVIDER_MAX_CHARS = 6000

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


def compact_multi_attempt_payload(
    result: Mapping[str, Any],
    *,
    max_chars: int = MULTI_ATTEMPT_PROVIDER_MAX_CHARS,
) -> dict[str, Any]:
    """Project tactic screening into bounded model context while audit keeps full details."""
    payload = dict(result)
    cap = max(2_000, int(max_chars or MULTI_ATTEMPT_PROVIDER_MAX_CHARS))
    try:
        serialized = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return payload
    exact_checks: list[dict[str, Any]] = []
    for check in payload.get("exact_checks") or []:
        if not isinstance(check, Mapping):
            continue
        compact = {
            key: value
            for key, value in check.items()
            if key
            in {
                "snippet",
                "theorem_id",
                "unrelated_placeholder_anchors",
                "local_goal_verified",
                "success",
                "backend_success",
                "target_verified",
                "status",
                "error",
                "error_code",
                "timed_out",
                "elapsed_s",
            }
        }
        if compact.get("error"):
            compact["error"] = str(compact["error"])[:MULTI_ATTEMPT_FEEDBACK_CHARS]
        exact_checks.append(compact)

    keep_fields = {
        "success",
        "degraded_reasons",
        "file_path",
        "line",
        "column",
        "attempts",
        "requested_line",
        "line_adjustment",
        "duplicate_attempts_removed",
        "backend_success",
        "backend_tool",
        "screening_backend",
        "target_verified",
        "verified_attempts",
        "local_goal_verified",
        "locally_verified_attempts",
        "status",
        "prepare",
        "action_required",
        "timed_out",
        "error",
        "error_code",
    }
    projected = {key: value for key, value in payload.items() if key in keep_fields}
    projected["exact_checks"] = exact_checks
    item_count = len(payload.get("items") or []) if isinstance(payload.get("items"), list) else 0
    if item_count:
        projected["items_truncated"] = {"kept": 0, "total": item_count}
    exact_check_count = len(exact_checks)
    projected.update(
        {
            "provider_context_projected": True,
            "audit_payload_preserved": True,
            "audit_payload_chars": len(serialized),
            "audit_payload_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "projected_fields_omitted": sorted(
                set(payload).difference(projected).difference({"items"})
            ),
        }
    )
    while len(json.dumps(projected, ensure_ascii=False)) > cap and exact_checks:
        if len(exact_checks) > 1:
            exact_checks.pop()
            continue
        error = str(exact_checks[0].get("error", "") or "")
        if len(error) > 300:
            exact_checks[0]["error"] = error[:300]
            continue
        break
    if len(exact_checks) < exact_check_count:
        projected["exact_checks_truncated"] = {
            "kept": len(exact_checks),
            "total": exact_check_count,
        }
    if len(json.dumps(projected, ensure_ascii=False)) > cap:
        projected = {
            key: value
            for key, value in projected.items()
            if key
            in {
                "success",
                "file_path",
                "line",
                "status",
                "target_verified",
                "local_goal_verified",
                "action_required",
                "exact_checks",
                "exact_checks_truncated",
                "items_truncated",
                "provider_context_projected",
                "audit_payload_preserved",
                "audit_payload_chars",
                "audit_payload_sha256",
            }
        }
        projected["projection_emergency_compacted"] = True
    return projected


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
