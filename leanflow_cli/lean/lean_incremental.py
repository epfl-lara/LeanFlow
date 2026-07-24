"""LeanFlow compatibility wrapper for LeanProbe incremental checks.

The public LeanFlow tool is still ``lean_incremental_check``.  Internally we
delegate to LeanProbe so LeanFlow gets the maintained LeanInteract-backed
parser, cache, diagnostics, and tactic feedback surface without changing the
agent-facing workflow contract.
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from core.project_resource_admission import (
    project_lean_heavy_admission,
    project_lean_service_reclaim_enabled,
)
from core.runtime_modes import dispatch_worker_enabled, low_memory_mode_enabled
from leanflow_cli.lean.lean_helper_ephemeral import check_helper_ephemerally
from leanflow_cli.lean.lean_incremental_axioms import (
    InlineAxiomQuery,
    build_inline_axiom_query,
    parse_inline_axiom_messages,
)
from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _declaration_matches_target,
    _statement_signature_text,
    _strip_lean_comments_and_strings,
)
from leanflow_cli.workflows.project import find_lean_project_root
from leanflow_cli.workflows.research_mode import research_mode_enabled

LOCAL_REPL_CANDIDATES = (
    ".lake/packages/repl",
    ".lake/build",
)
LOCAL_REPL_MISSING = "project-local Lean REPL binary not found; run `leanflow project init`"
LEAN_INCREMENTAL_TIMEOUT_DEFAULT_S: Final[int] = 60
DISPATCH_WORKER_INCREMENTAL_TIMEOUT_FLOOR_S: Final[int] = 300
RESEARCH_INCREMENTAL_TIMEOUT_FLOOR_S: Final[int] = 300
PROFILED_HELPER_TIMEOUT_FLOOR_S: Final[int] = 300

_PROBE: Any | None = None


def _import_lean_probe() -> tuple[Any, Any, Any, str]:
    try:
        from lean_probe import LeanIncrementalSegment, LeanProbe
        from lean_probe.core import segment_file
    except Exception as exc:
        return None, None, None, f"lean-probe unavailable: {exc}"
    return LeanProbe, LeanIncrementalSegment, segment_file, ""


LeanProbe, LeanIncrementalSegment, _probe_segment_file, _LEAN_PROBE_IMPORT_ERROR = (
    _import_lean_probe()
)


def _segment_file(text: str) -> tuple[str, list[Any]]:
    if _probe_segment_file is None:
        raise RuntimeError(_LEAN_PROBE_IMPORT_ERROR)
    return _probe_segment_file(text)


def _find_segment(segments: list[Any], theorem_id: str) -> Any | None:
    wanted = str(theorem_id or "").strip()
    if not wanted:
        return None
    short = wanted.split(".")[-1]
    for segment in segments:
        if getattr(segment, "name", "") in {wanted, short}:
            return segment
    return None


def _resolve_project_root(
    cwd: str | Path | None, file_path: str | Path | None = None
) -> Path | None:
    candidates: list[Path] = []
    if cwd:
        candidates.append(Path(cwd).expanduser().resolve())
    if file_path:
        path = Path(file_path).expanduser()
        candidates.append((path if path.is_dir() else path.parent).resolve())
    candidates.append(Path.cwd().resolve())
    for candidate in candidates:
        root = find_lean_project_root(candidate)
        if root is not None:
            return root.resolve()
    return None


def _resolve_file_path(file_path: str | Path, project_root: Path | None) -> Path:
    raw = Path(str(file_path or "")).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    if project_root is not None:
        return (project_root / raw).resolve()
    return raw.resolve()


def _local_repl_dir(project_root: Path) -> Path | None:
    suffix = ".exe" if platform.system() == "Windows" else ""
    for candidate in LOCAL_REPL_CANDIDATES:
        root = project_root / candidate
        binary = root / ".lake" / "build" / "bin" / f"repl{suffix}"
        if binary.is_file():
            return root
    return None


def _probe() -> Any:
    global _PROBE
    if LeanProbe is None:
        raise RuntimeError(_LEAN_PROBE_IMPORT_ERROR)
    if _PROBE is None:
        _PROBE = LeanProbe(auto_build=False)
    return _PROBE


def lean_scratch_check(code: str, *, cwd: str = "", timeout_s: int = 90) -> dict[str, Any]:
    """Run a standalone Lean snippet through the warm LeanProbe REPL.

    The scratch surface for probes/experiments (roadmap: "LeanProbe is the
    guy"): returns the probe's normalized payload (``success`` = the tool
    ran; ``ok`` = elaborated with no errors and no sorry; ``messages``).
    Never touches the project tree; never an acceptance authority.
    """
    try:
        root = _resolve_project_root(cwd)
        if root is None:
            probe = _probe()
            payload = probe.check_code(code, cwd=cwd or None, timeout_s=timeout_s)
            return dict(payload or {})
        with project_lean_heavy_admission(root) as admission:
            reclaimed = False
            try:
                probe = _probe()
                payload = dict(probe.check_code(code, cwd=cwd or None, timeout_s=timeout_s) or {})
            finally:
                if project_lean_service_reclaim_enabled():
                    reclaimed = close_incremental_sessions()
                    if not reclaimed:
                        admission.retain_until_process_exit(
                            "LeanProbe scratch session close failed"
                        )
            payload["resource_admission"] = {
                **admission.to_dict(),
                "incremental_session_reclaimed": reclaimed,
            }
            return payload
    except Exception as exc:
        return {
            "success": False,
            "ok": False,
            "error": str(exc)[:500],
            "messages": [],
        }


def _error_payload(
    *,
    action: str,
    error: str,
    error_code: str,
    file_path: str | Path | None = None,
    target: str = "",
    timed_out: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "ok": False,
        "backend": "lean_interact",
        "tool": "lean_probe",
        "action": action,
        "timed_out": timed_out,
        "error_code": error_code,
        "error": error,
        "output": error,
    }
    if file_path:
        payload["file"] = str(file_path)
    if target:
        payload["target"] = target
    return payload


def _leanflow_action(action: str) -> str:
    normalized = str(action or "check_target").strip().lower().replace("-", "_")
    return {
        "prepare": "prepare_file",
        "prepare_file": "prepare_file",
        "check": "check_target",
        "check_target": "check_target",
        "check_helper": "check_helper",
        "helper": "check_helper",
        "feedback": "feedback",
    }.get(normalized, normalized)


def _effective_incremental_timeout_s(
    timeout_s: int,
    *,
    timeout_ceiling_s: int | None = None,
    profiled_helper: bool = False,
) -> tuple[int, bool, str, int | None]:
    """Return the effective timeout, adjustment policy, and normalized ceiling.

    Research workers own independent Lean service trees, while the foreground
    research profile deliberately reclaims heavy Lean sessions between actors. Their
    next check of a large Mathlib file therefore includes process and environment
    startup. A short timeout kills that server and makes an immediate retry cold again.
    An authoritative parent deadline always caps those floors.
    """
    requested = max(1, int(timeout_s or LEAN_INCREMENTAL_TIMEOUT_DEFAULT_S))
    effective = requested
    policy = "requested"
    if dispatch_worker_enabled() and requested < DISPATCH_WORKER_INCREMENTAL_TIMEOUT_FLOOR_S:
        effective = DISPATCH_WORKER_INCREMENTAL_TIMEOUT_FLOOR_S
        policy = "dispatch_worker_cold_start_floor"
    elif research_mode_enabled() and requested < RESEARCH_INCREMENTAL_TIMEOUT_FLOOR_S:
        effective = RESEARCH_INCREMENTAL_TIMEOUT_FLOOR_S
        policy = "research_cold_start_floor"
    elif profiled_helper and requested < PROFILED_HELPER_TIMEOUT_FLOOR_S:
        effective = PROFILED_HELPER_TIMEOUT_FLOOR_S
        policy = "profiled_helper_cold_start_floor"

    normalized_ceiling = None if timeout_ceiling_s is None else max(1, int(timeout_ceiling_s))
    if normalized_ceiling is not None and effective > normalized_ceiling:
        effective = normalized_ceiling
        policy = (
            "authoritative_deadline_ceiling"
            if policy == "requested"
            else f"{policy}_capped_by_deadline"
        )
    return effective, effective != requested, policy, normalized_ceiling


def _timeout_metadata(
    *,
    requested_timeout_s: int,
    effective_timeout_s: int,
    timeout_adjusted: bool,
    timeout_policy: str,
    timeout_ceiling_s: int | None,
) -> dict[str, Any]:
    """Return stable timeout telemetry, including a ceiling only when supplied."""
    payload: dict[str, Any] = {
        "requested_timeout_s": requested_timeout_s,
        "effective_timeout_s": effective_timeout_s,
        "timeout_adjusted": timeout_adjusted,
        "timeout_policy": timeout_policy,
    }
    if timeout_ceiling_s is not None:
        payload["timeout_ceiling_s"] = timeout_ceiling_s
    return payload


# Generous ceiling on a single `feedback` payload. Only bounds pathological tactic dumps that
# would otherwise dominate the model's context (and be replayed each turn); typical feedback is
# far smaller and untouched. Override with LEANFLOW_INCREMENTAL_FEEDBACK_MAX_CHARS.
_DEFAULT_FEEDBACK_MAX_CHARS = 16000


def _feedback_max_chars() -> int:
    """Character ceiling for a feedback payload (env-overridable, positive int)."""
    try:
        value = int(os.getenv("LEANFLOW_INCREMENTAL_FEEDBACK_MAX_CHARS", "") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else _DEFAULT_FEEDBACK_MAX_CHARS


def _bound_feedback_payload(result: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Trim the per-tactic goal/proof-state list of an oversized feedback payload to fit a budget.

    Keeps the head (where the first failure is) and drops trailing tactics until the serialized
    payload fits; records what was dropped. Non-feedback or already-small payloads are returned as-is.
    """
    try:
        if len(json.dumps(result, ensure_ascii=False)) <= max_chars:
            return result
    except (TypeError, ValueError):
        return result
    tactics = result.get("tactics")
    if not isinstance(tactics, list) or len(tactics) <= 1:
        return result
    for keep in (40, 20, 10, 5, 2, 1):
        if keep >= len(tactics):
            continue
        trimmed = dict(result)
        trimmed["tactics"] = tactics[:keep]
        trimmed["tactics_truncated"] = {"kept": keep, "total": len(tactics)}
        try:
            if len(json.dumps(trimmed, ensure_ascii=False)) <= max_chars:
                return trimmed
        except (TypeError, ValueError):
            return result
    trimmed = dict(result)
    trimmed["tactics"] = tactics[:1]
    trimmed["tactics_truncated"] = {"kept": 1, "total": len(tactics)}
    return trimmed


def _normalize_payload(payload: dict[str, Any], action: str) -> dict[str, Any]:
    result = dict(payload)
    result["action"] = action
    result.setdefault("backend", "lean_interact")
    result.setdefault("tool", "lean_probe")
    result["command"] = f"lean_probe {action}"
    if action == "feedback":
        result = _bound_feedback_payload(result, _feedback_max_chars())
    return result


def _inline_axiom_query_for_target(
    source_text: str,
    *,
    theorem_id: str,
    replacement: str,
) -> InlineAxiomQuery | None:
    """Build an inline axiom query for the exact declaration LeanProbe will check."""
    try:
        _header, segments = _segment_file(source_text)
    except Exception:
        return None
    segment = _find_segment(segments, theorem_id)
    if segment is None:
        return None
    declaration_source = (
        str(replacement or "").strip() or str(getattr(segment, "text", "") or "").strip()
    )
    return build_inline_axiom_query(
        declaration_source,
        target=str(getattr(segment, "name", "") or theorem_id).strip(),
        requested_target=theorem_id,
    )


def _attach_inline_axiom_profile(
    result: dict[str, Any],
    query: InlineAxiomQuery,
) -> dict[str, Any]:
    """Attach complete marked axiom evidence, or an explicit unavailable verdict."""
    updated = dict(result)
    updated.update(
        {
            "axiom_profile_requested": True,
            "axiom_profile_checked": False,
            "axiom_profile_axioms": [],
            "axiom_profile_target": query.target,
            "axiom_profile_requested_target": query.requested_target,
            "axiom_profile_declaration_sha256": query.declaration_sha256,
        }
    )
    raw_messages = updated.get("messages")
    if not isinstance(raw_messages, list) or not all(
        isinstance(item, Mapping) for item in raw_messages
    ):
        updated["axiom_profile_error"] = "LeanProbe did not return structured axiom messages"
        return updated
    profile, error = parse_inline_axiom_messages(raw_messages, query)
    if profile is None:
        updated["axiom_profile_error"] = error
        return updated

    # Keep acceptance diagnostics equivalent to an ordinary exact check. The
    # marker and #print messages are retained separately as gate evidence.
    proof_messages = [
        *raw_messages[: profile.message_start],
        *raw_messages[profile.message_end + 1 :],
    ]
    updated["messages"] = proof_messages
    updated["output"] = "\n".join(
        f"{item.get('severity', '')}: {item.get('message', '')}".strip()
        for item in proof_messages
        if str(item.get("message", "") or "").strip()
    )
    updated.update(
        {
            "axiom_profile_checked": True,
            "axiom_profile_axioms": list(profile.axioms),
            "axiom_profile_output": profile.output[:2000],
            "axiom_profile_error": "",
        }
    )
    return updated


def _normalized_statement_signature(entry: dict[str, Any]) -> str:
    """Return a whitespace-stable signature for one parsed declaration."""
    statement = _statement_signature_text(str(entry.get("text", "") or ""))
    stripped = _strip_lean_comments_and_strings(statement)
    return re.sub(r"\s+", " ", stripped).strip()


def _replacement_target_metadata(
    source_text: str,
    replacement: str,
    theorem_id: str,
) -> dict[str, Any]:
    """Classify whether a replacement preserves the assigned declaration identity and statement."""
    replacement_entries = _declaration_line_index_from_text(str(replacement or ""))
    replacement_entry = next(
        (entry for entry in replacement_entries if _declaration_matches_target(entry, theorem_id)),
        None,
    )
    replacement_names = [
        str(entry.get("name", "") or "").strip()
        for entry in replacement_entries
        if str(entry.get("name", "") or "").strip()
    ]
    metadata: dict[str, Any] = {
        "replacement_matches_target": False,
        "replacement_declarations": replacement_names,
        "verification_scope": "scratch_replacement",
    }
    if replacement_entry is None:
        metadata["replacement_mismatch_reason"] = "replacement does not declare the assigned target"
        return metadata

    source_entry = next(
        (
            entry
            for entry in _declaration_line_index_from_text(str(source_text or ""))
            if _declaration_matches_target(entry, theorem_id)
        ),
        None,
    )
    if source_entry is None:
        metadata["replacement_mismatch_reason"] = "assigned target is absent from the source file"
        return metadata

    source_signature = _normalized_statement_signature(source_entry)
    replacement_signature = _normalized_statement_signature(replacement_entry)
    if not source_signature or replacement_signature != source_signature:
        metadata["replacement_mismatch_reason"] = (
            "replacement changes the assigned target statement"
        )
        return metadata

    metadata.update(
        {
            "replacement_matches_target": True,
            "replacement_mismatch_reason": "",
            "verification_scope": "target_candidate",
        }
    )
    return metadata


def _target_sorry_skeleton(source_text: str, theorem_id: str) -> str:
    """Return the existing target signature closed by a temporary sorry body."""
    entry = next(
        (
            item
            for item in _declaration_line_index_from_text(str(source_text or ""))
            if _declaration_matches_target(item, theorem_id)
        ),
        None,
    )
    if entry is None:
        return ""
    signature = _statement_signature_text(str(entry.get("text", "") or "")).strip()
    return f"{signature} := by\n  sorry" if signature else ""


def _payload_has_errors(payload: dict[str, Any]) -> bool:
    """Return whether a LeanProbe payload contains an elaboration error."""
    if payload.get("success") is False or payload.get("has_errors") is True:
        return True
    for key in ("errors", "error_count"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return True
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return True
    for key in ("messages", "items", "diagnostics"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("items") or value.get("messages") or []
        if isinstance(value, list) and any(
            isinstance(item, dict)
            and str(item.get("severity", "") or "").strip().lower() == "error"
            for item in value
        ):
            return True
    return False


def _normalize_helper_check_payload(
    payload: dict[str, Any],
    *,
    helper_source: str,
    anchor_target: str,
) -> dict[str, Any]:
    """Reframe an anchored target check as non-authoritative helper validation.

    LeanProbe needs an existing declaration as the replacement anchor. The
    synthetic anchor deliberately ends in ``sorry``; only errors and
    placeholders in ``helper_source`` determine whether the helper candidate
    itself is valid.
    """
    result = dict(payload)
    stripped_helper = _strip_lean_comments_and_strings(str(helper_source or ""))
    helper_has_placeholder = bool(
        re.search(r"\b(?:sorry|admit|sorryAx)\b", stripped_helper, flags=re.IGNORECASE)
    )
    backend_has_errors = _payload_has_errors(result)
    helper_ok = not helper_has_placeholder and not backend_has_errors
    raw_messages = result.get("messages")
    if isinstance(raw_messages, list):
        anchor_messages = [
            item
            for item in raw_messages
            if isinstance(item, dict)
            and "declaration uses 'sorry'" in str(item.get("message", "") or "").strip().lower()
        ]
        if anchor_messages:
            result["anchor_messages"] = anchor_messages
            result["messages"] = [item for item in raw_messages if item not in anchor_messages]
    result.update(
        {
            "ok": helper_ok,
            "valid_without_sorry": helper_ok,
            "has_sorry": helper_has_placeholder,
            "anchor_target": anchor_target,
            "anchor_temporary_sorry": True,
            "anchor_backend_ok": bool(payload.get("ok")),
        }
    )
    if helper_ok:
        result["error"] = ""
        result["error_code"] = ""
        result["output"] = "helper candidate elaborated without errors or placeholders"
    elif helper_has_placeholder and not result.get("error"):
        result["error"] = "helper candidate contains sorry/admit"
        result["error_code"] = "helper_placeholder"
        result["output"] = result["error"]
    return result


def _replacement_has_placeholder(replacement: str) -> bool:
    """Return whether executable replacement source contains a proof placeholder."""
    stripped = _strip_lean_comments_and_strings(str(replacement or ""))
    return bool(re.search(r"\b(?:sorry|admit|sorryAx)\b", stripped, flags=re.IGNORECASE))


def _placeholder_rejection_payload(
    *,
    action: str,
    file_path: Path,
    theorem_id: str,
    replacement_metadata: Mapping[str, Any],
    include_axiom_profile: bool,
    requested_timeout_s: int,
    effective_timeout_s: int,
    timeout_adjusted: bool,
    timeout_policy: str,
    timeout_ceiling_s: int | None,
    operation_started: float,
) -> dict[str, Any]:
    """Reject a placeholder-bearing acceptance candidate without starting Lean."""
    helper = action == "check_helper"
    error = (
        "helper candidate contains sorry/admit"
        if helper
        else "target candidate contains sorry/admit"
    )
    result: dict[str, Any] = {
        "success": True,
        "ok": False,
        "backend": "deterministic_preflight",
        "tool": "lean_incremental_check",
        "action": action,
        "file": str(file_path),
        "target": theorem_id,
        "valid_without_sorry": False,
        "has_errors": False,
        "has_sorry": True,
        "timed_out": False,
        "retryable": False,
        "error_code": "helper_placeholder" if helper else "target_placeholder",
        "error": error,
        "output": error,
        "lean_started": False,
        **dict(replacement_metadata),
        **_timeout_metadata(
            requested_timeout_s=requested_timeout_s,
            effective_timeout_s=effective_timeout_s,
            timeout_adjusted=timeout_adjusted,
            timeout_policy=timeout_policy,
            timeout_ceiling_s=timeout_ceiling_s,
        ),
    }
    if include_axiom_profile:
        result.update(
            {
                "axiom_profile_requested": True,
                "axiom_profile_checked": False,
                "axiom_profile_axioms": [],
                "axiom_profile_blockers": [],
                "axiom_profile_error": "axiom profile skipped for placeholder-bearing candidate",
            }
        )
    result["leanflow_timing"] = {
        "total_s": round(max(0.0, time.monotonic() - operation_started), 3),
        "admission_wait_s": 0.0,
        "probe_call_s": 0.0,
        "session_reclaim_s": 0.0,
        "postprocess_s": 0.0,
    }
    return result


def _normalize_profiled_helper_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return complete fail-closed axiom evidence for an exact helper check."""
    result = dict(payload)
    output = str(result.get("output", "") or "")
    output_truncated = bool(result.get("output_truncated", False))
    raw_axioms = result.get("axiom_profile_axioms")
    if raw_axioms is None:
        raw_axioms = result.get("axioms")
    raw_blockers = result.get("axiom_profile_blockers")
    profile_error = str(result.get("axiom_profile_error", "") or "").strip()
    profile_complete = bool(
        result.get("axiom_profile_checked") is True
        and isinstance(raw_axioms, list)
        and isinstance(raw_blockers, list)
        and not profile_error
    )
    axioms = [str(value) for value in raw_axioms] if isinstance(raw_axioms, list) else []
    blockers = [str(value) for value in raw_blockers] if isinstance(raw_blockers, list) else []
    if not profile_complete and not profile_error:
        profile_error = "helper candidate has no complete auditable axiom result"
    result.update(
        {
            "axiom_profile_requested": True,
            "axiom_profile_checked": profile_complete,
            "axiom_profile_axioms": axioms,
            "axiom_profile_blockers": blockers,
            "axiom_profile_error": profile_error,
            "output_truncated": output_truncated,
        }
    )
    if profile_complete and not blockers:
        return result

    result["ok"] = False
    result["valid_without_sorry"] = False
    if blockers:
        if not str(result.get("error", "") or "").strip():
            result["error"] = "helper candidate depends on disallowed axioms: " + ", ".join(
                blockers
            )
        if not str(result.get("error_code", "") or "").strip():
            result["error_code"] = "helper_axiom_profile"
    else:
        if not str(result.get("error", "") or "").strip():
            result["error"] = profile_error
        if not str(result.get("error_code", "") or "").strip():
            result["error_code"] = "helper_axiom_profile_unavailable"
    # Exact elaboration output carries the useful tail diagnostics. Preserve it
    # and its backend truncation flag instead of replacing it with the bounded
    # 500-character ``error`` summary when axiom profiling cannot run.
    if not output:
        result["output"] = str(result.get("error", "") or profile_error)
        result["output_truncated"] = False
    return result


def close_incremental_sessions() -> bool:
    """Close the owned LeanProbe session and report verified API completion.

    Keep the probe reference on failure so admission can retain its project
    slot instead of falsely claiming the resident service disappeared.
    """
    global _PROBE
    if _PROBE is None:
        return True
    probe = _PROBE
    try:
        probe.close()
    except Exception:
        return False
    if _PROBE is probe:
        _PROBE = None
    return True


def lean_incremental_capabilities(cwd: str | Path | None = None) -> dict[str, Any]:
    """Return a dict reporting availability of incremental Lean checking and any degradation reasons. Detects project root, local REPL binary, and LeanProbe capabilities; includes active sessions and max code sessions from the probe."""
    if low_memory_mode_enabled():
        return {
            "available": False,
            "project_root": str(_resolve_project_root(cwd) or ""),
            "repl_dir": "",
            "active_sessions": [],
            "code_sessions": [],
            "max_code_sessions": 0,
            "degraded_reasons": ["incremental Lean cache disabled by low-memory mode"],
            "degraded_codes": ["low_memory_mode"],
        }
    project_root = _resolve_project_root(cwd)
    repl_dir = _local_repl_dir(project_root) if project_root else None
    degraded: list[str] = []
    degraded_codes: list[str] = []
    if _LEAN_PROBE_IMPORT_ERROR:
        degraded.append(_LEAN_PROBE_IMPORT_ERROR)
        degraded_codes.append("lean_probe_unavailable")
    if project_root is None:
        degraded.append("Lean project root not detected")
        degraded_codes.append("no_project_root")
    elif repl_dir is None:
        degraded.append(LOCAL_REPL_MISSING)
        degraded_codes.append("local_repl_missing")

    probe_payload: dict[str, Any] = {}
    admission_payload: dict[str, object] = {}
    if not _LEAN_PROBE_IMPORT_ERROR:
        try:
            if project_root is None:
                probe_payload = dict(_probe().capabilities(project_root))
            else:
                with project_lean_heavy_admission(project_root) as admission:
                    reclaimed = False
                    try:
                        probe_payload = dict(_probe().capabilities(project_root))
                    finally:
                        if project_lean_service_reclaim_enabled():
                            reclaimed = close_incremental_sessions()
                            if not reclaimed:
                                admission.retain_until_process_exit(
                                    "LeanProbe capability session close failed"
                                )
                    admission_payload = {
                        **admission.to_dict(),
                        "incremental_session_reclaimed": reclaimed,
                    }
        except Exception as exc:
            degraded.append(f"LeanProbe capabilities unavailable: {exc}")
            degraded_codes.append("lean_probe_capabilities_failed")

    active_sessions = list(probe_payload.get("active_sessions") or [])
    return {
        "available": not degraded and bool(project_root) and bool(repl_dir),
        "project_root": str(project_root or ""),
        "repl_dir": str(repl_dir or ""),
        "active_sessions": active_sessions,
        "code_sessions": list(probe_payload.get("code_sessions") or []),
        "max_code_sessions": probe_payload.get("max_code_sessions", 0),
        "resource_admission": admission_payload,
        "degraded_reasons": degraded,
        "degraded_codes": degraded_codes,
    }


def lean_incremental_check(
    *,
    action: str,
    file_path: str,
    theorem_id: str = "",
    cwd: str = "",
    replacement: str = "",
    include_tactics: bool = False,
    include_axiom_profile: bool = False,
    timeout_s: int = LEAN_INCREMENTAL_TIMEOUT_DEFAULT_S,
    timeout_ceiling_s: int | None = None,
) -> dict[str, Any]:
    """Dispatch an incremental Lean check through the appropriate exact backend.

    ``include_axiom_profile`` embeds one marker-isolated ``#print axioms`` in
    an exact target check, or selects the exact one-shot helper harness for a
    helper candidate. Complete parsed evidence is returned alongside the
    ordinary verdict; missing or malformed output fails closed.

    ``timeout_ceiling_s`` is an internal parent-deadline cap. It is deliberately
    absent from the model-facing tool schema so only authoritative callers can
    shorten cold-start floors.
    """
    operation_started = time.monotonic()
    leanflow_action = _leanflow_action(action)
    requested_timeout_s = max(1, int(timeout_s or LEAN_INCREMENTAL_TIMEOUT_DEFAULT_S))
    ephemeral_helper_check = leanflow_action == "check_helper" and (
        dispatch_worker_enabled() or include_axiom_profile
    )
    (
        effective_timeout_s,
        timeout_adjusted,
        timeout_policy,
        normalized_timeout_ceiling_s,
    ) = _effective_incremental_timeout_s(
        requested_timeout_s,
        timeout_ceiling_s=timeout_ceiling_s,
        profiled_helper=ephemeral_helper_check,
    )
    if low_memory_mode_enabled() and not ephemeral_helper_check:
        return _error_payload(
            action=leanflow_action,
            error="incremental Lean cache disabled by low-memory mode",
            error_code="low_memory_mode",
            file_path=file_path,
            target=theorem_id,
        )
    project_root = _resolve_project_root(cwd, file_path)
    if project_root is None:
        return _error_payload(
            action=leanflow_action,
            error="Lean project root not detected",
            error_code="no_project_root",
        )

    resolved = _resolve_file_path(file_path, project_root)
    if not resolved.is_file():
        return _error_payload(
            action=leanflow_action,
            error="Lean file not found",
            error_code="file_not_found",
            file_path=resolved,
        )

    source_text = resolved.read_text(encoding="utf-8")
    if include_axiom_profile and leanflow_action not in {"check_target", "check_helper"}:
        return _error_payload(
            action=leanflow_action,
            error="axiom profiles require action=check_target or action=check_helper",
            error_code="inline_axiom_profile_unsupported_action",
            file_path=resolved,
            target=theorem_id,
        )
    probe_action = leanflow_action
    probe_replacement = replacement
    replacement_metadata: dict[str, Any] = {}
    if leanflow_action == "check_helper":
        if not theorem_id:
            return _error_payload(
                action=leanflow_action,
                error="check_helper requires theorem_id for an existing anchor declaration",
                error_code="missing_anchor_target",
                file_path=resolved,
            )
        if not replacement.strip():
            return _error_payload(
                action=leanflow_action,
                error="check_helper requires a complete helper declaration in replacement",
                error_code="missing_helper_replacement",
                file_path=resolved,
                target=theorem_id,
            )
        anchor_skeleton = _target_sorry_skeleton(source_text, theorem_id)
        if not anchor_skeleton:
            return _error_payload(
                action=leanflow_action,
                error=f"anchor declaration {theorem_id!r} not found",
                error_code="anchor_target_not_found",
                file_path=resolved,
                target=theorem_id,
            )
        probe_action = "check_target"
        probe_replacement = f"{replacement.rstrip()}\n\n{anchor_skeleton}"
        replacement_metadata = _replacement_target_metadata(
            source_text,
            replacement,
            theorem_id,
        )
        replacement_metadata.update(
            {
                "verification_scope": "helper_candidate",
                "anchor_target": theorem_id,
                # A helper is intentionally distinct from the assigned target;
                # identity mismatch is therefore not an error in this scope.
                "replacement_mismatch_reason": "",
            }
        )
        if _replacement_has_placeholder(replacement):
            return _placeholder_rejection_payload(
                action=leanflow_action,
                file_path=resolved,
                theorem_id=theorem_id,
                replacement_metadata=replacement_metadata,
                include_axiom_profile=include_axiom_profile,
                requested_timeout_s=requested_timeout_s,
                effective_timeout_s=effective_timeout_s,
                timeout_adjusted=timeout_adjusted,
                timeout_policy=timeout_policy,
                timeout_ceiling_s=normalized_timeout_ceiling_s,
                operation_started=operation_started,
            )
        if ephemeral_helper_check:
            try:
                result = check_helper_ephemerally(
                    source_text=source_text,
                    helper_source=replacement,
                    theorem_id=theorem_id,
                    file_path=resolved,
                    project_root=project_root,
                    anchor_skeleton=anchor_skeleton,
                    timeout_s=effective_timeout_s,
                )
            except Exception as exc:
                result = _error_payload(
                    action=leanflow_action,
                    error=f"ephemeral helper verification failed: {str(exc)[:400]}",
                    error_code="ephemeral_helper_failed",
                    file_path=resolved,
                    target=theorem_id,
                )
            result = _normalize_profiled_helper_payload(result)
            result.update(replacement_metadata)
            result.update(
                _timeout_metadata(
                    requested_timeout_s=requested_timeout_s,
                    effective_timeout_s=effective_timeout_s,
                    timeout_adjusted=timeout_adjusted,
                    timeout_policy=timeout_policy,
                    timeout_ceiling_s=normalized_timeout_ceiling_s,
                )
            )
            return result
    elif replacement and leanflow_action in {"check_target", "feedback"}:
        replacement_metadata = _replacement_target_metadata(
            source_text,
            replacement,
            theorem_id,
        )
        if leanflow_action == "check_target" and _replacement_has_placeholder(replacement):
            return _placeholder_rejection_payload(
                action=leanflow_action,
                file_path=resolved,
                theorem_id=theorem_id,
                replacement_metadata=replacement_metadata,
                include_axiom_profile=include_axiom_profile,
                requested_timeout_s=requested_timeout_s,
                effective_timeout_s=effective_timeout_s,
                timeout_adjusted=timeout_adjusted,
                timeout_policy=timeout_policy,
                timeout_ceiling_s=normalized_timeout_ceiling_s,
                operation_started=operation_started,
            )

    inline_axiom_query: InlineAxiomQuery | None = None
    if include_axiom_profile:
        inline_axiom_query = _inline_axiom_query_for_target(
            source_text,
            theorem_id=theorem_id,
            replacement=probe_replacement,
        )
        if inline_axiom_query is None:
            result = _error_payload(
                action=leanflow_action,
                error="could not construct an exact inline axiom query for the target",
                error_code="inline_axiom_query_unavailable",
                file_path=resolved,
                target=theorem_id,
            )
            result.update(
                {
                    "axiom_profile_requested": True,
                    "axiom_profile_checked": False,
                    "axiom_profile_axioms": [],
                }
            )
            return result
        probe_replacement = inline_axiom_query.source

    repl_dir = _local_repl_dir(project_root)
    if repl_dir is None:
        return _error_payload(
            action=leanflow_action,
            error=LOCAL_REPL_MISSING,
            error_code="local_repl_missing",
            file_path=resolved,
            target=theorem_id,
        )

    if _LEAN_PROBE_IMPORT_ERROR:
        return _error_payload(
            action=leanflow_action,
            error=_LEAN_PROBE_IMPORT_ERROR,
            error_code="lean_probe_unavailable",
            file_path=resolved,
            target=theorem_id,
        )

    reclaimed_incremental_session = False
    admission_started = time.monotonic()
    admission_wait_s = 0.0
    probe_call_s = 0.0
    session_reclaim_s = 0.0
    with project_lean_heavy_admission(project_root) as admission:
        admission_wait_s = max(0.0, time.monotonic() - admission_started)
        probe_started = time.monotonic()
        try:
            if leanflow_action == "prepare_file":
                payload = _probe().prepare_file(
                    resolved,
                    theorem_id=theorem_id,
                    cwd=project_root,
                    timeout_s=effective_timeout_s,
                )
            elif probe_action == "check_target":
                payload = _probe().check_target(
                    resolved,
                    theorem_id=theorem_id,
                    cwd=project_root,
                    replacement=probe_replacement,
                    include_tactics=include_tactics,
                    timeout_s=effective_timeout_s,
                )
            elif leanflow_action == "feedback":
                payload = _probe().feedback(
                    resolved,
                    theorem_id=theorem_id,
                    cwd=project_root,
                    replacement=replacement,
                    timeout_s=effective_timeout_s,
                )
            else:
                return _error_payload(
                    action=leanflow_action,
                    error=f"unsupported lean_incremental_check action: {action}",
                    error_code="unsupported_action",
                    file_path=resolved,
                    target=theorem_id,
                )
        finally:
            probe_call_s = max(0.0, time.monotonic() - probe_started)
            # Releasing only the file slot would be unsound if LeanProbe kept a
            # multi-gigabyte LSP child alive after returning its response.
            if project_lean_service_reclaim_enabled():
                reclaim_started = time.monotonic()
                reclaimed_incremental_session = close_incremental_sessions()
                session_reclaim_s = max(0.0, time.monotonic() - reclaim_started)
                if not reclaimed_incremental_session:
                    admission.retain_until_process_exit(
                        "LeanProbe incremental session close failed"
                    )
    postprocess_started = time.monotonic()
    result = _normalize_payload(payload, leanflow_action)
    if inline_axiom_query is not None:
        result = _attach_inline_axiom_profile(result, inline_axiom_query)
    if leanflow_action == "check_helper":
        result = _normalize_helper_check_payload(
            result,
            helper_source=replacement,
            anchor_target=theorem_id,
        )
    result.update(replacement_metadata)
    result.update(
        {
            **_timeout_metadata(
                requested_timeout_s=requested_timeout_s,
                effective_timeout_s=effective_timeout_s,
                timeout_adjusted=timeout_adjusted,
                timeout_policy=timeout_policy,
                timeout_ceiling_s=normalized_timeout_ceiling_s,
            ),
            "resource_admission": {
                **admission.to_dict(),
                "incremental_session_reclaimed": reclaimed_incremental_session,
            },
        }
    )
    postprocess_s = max(0.0, time.monotonic() - postprocess_started)
    result["leanflow_timing"] = {
        "total_s": round(max(0.0, time.monotonic() - operation_started), 3),
        "admission_wait_s": round(admission_wait_s, 3),
        "probe_call_s": round(probe_call_s, 3),
        "session_reclaim_s": round(session_reclaim_s, 3),
        "postprocess_s": round(postprocess_s, 3),
    }
    return result
