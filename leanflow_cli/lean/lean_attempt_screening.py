"""Screen local tactic candidates with exact LeanProbe target checks."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_attempt_location import _multi_attempt_replacement_candidate

MULTI_ATTEMPT_PREPARE_TIMEOUT_S = 300
MULTI_ATTEMPT_CANDIDATE_TIMEOUT_S = 30

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


def _exact_check_summary(check: Mapping[str, Any], *, verified: bool) -> dict[str, Any]:
    """Build the bounded exact-check fields exposed by multi-attempt results."""
    return {
        "success": bool(check.get("success")),
        "target_verified": verified,
        "status": str(check.get("status", "") or ""),
        "error": str(check.get("error", "") or ""),
        "error_code": str(check.get("error_code", "") or ""),
        "timed_out": _incremental_check_timed_out(check),
        "elapsed_s": check.get("elapsed_s", 0),
    }


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
    replacements: list[tuple[str, str, str]] = []
    for snippet in attempts:
        replacement = _multi_attempt_replacement_candidate(path, line, column, snippet)
        if replacement is None:
            return None
        theorem_id, declaration = replacement
        replacements.append((snippet, theorem_id, declaration))
    if not replacements:
        return None

    theorem_id = replacements[0][1]
    if any(candidate_theorem != theorem_id for _, candidate_theorem, _ in replacements):
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
    for index, (snippet, candidate_theorem, declaration) in enumerate(replacements):
        check = check_incrementally(
            action="check_target",
            file_path=str(path),
            theorem_id=candidate_theorem,
            cwd=cwd,
            replacement=declaration,
            include_tactics=False,
            timeout_s=MULTI_ATTEMPT_CANDIDATE_TIMEOUT_S,
        )
        verified = _incremental_check_passed(check)
        exact_check = _exact_check_summary(check, verified=verified)
        exact_checks.append(
            {
                "snippet": snippet,
                "theorem_id": candidate_theorem,
                **exact_check,
            }
        )
        items.append(
            {
                "snippet": snippet,
                "goals": [],
                "diagnostics": list(check.get("messages") or []),
                "timed_out": exact_check["timed_out"],
                "probe_closed_goal": verified,
                "verified": verified,
                "exact_check": exact_check,
            }
        )
        if verified:
            verified_attempts.append(snippet)
            for skipped_snippet, _, _ in replacements[index + 1 :]:
                items.append(
                    {
                        "snippet": skipped_snippet,
                        "goals": [],
                        "diagnostics": [],
                        "timed_out": False,
                        "probe_closed_goal": False,
                        "verified": False,
                        "screening_skipped": "earlier exact candidate verified",
                    }
                )
            break

    success = bool(verified_attempts)
    payload: dict[str, Any] = {
        "success": success,
        "backend_success": True,
        "backend_tool": "lean_probe",
        "screening_backend": "lean_probe",
        "target_verified": success,
        "verified_attempts": verified_attempts,
        "exact_checks": exact_checks,
        "items": items,
        "status": "verified_candidate" if success else "screened_no_verified_candidate",
        "prepare": {
            "success": bool(prepared.get("success")),
            "elapsed_s": prepared.get("elapsed_s", 0),
            "cache": dict(prepared.get("cache") or {}),
        },
    }
    if not success:
        payload["action_required"] = (
            "No tactic is exact-target verified. Use the returned LeanProbe diagnostics, "
            "simplify the local goal, or choose a structurally different route."
        )
    return payload
