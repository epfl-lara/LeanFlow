"""LeanFlow compatibility wrapper for LeanProbe incremental checks.

The public LeanFlow tool is still ``lean_incremental_check``.  Internally we
delegate to LeanProbe so LeanFlow gets the maintained LeanInteract-backed
parser, cache, diagnostics, and tactic feedback surface without changing the
agent-facing workflow contract.
"""

from __future__ import annotations

import hashlib
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
from leanflow_cli.lean.lean_command_timeout import configured_hard_timeout_s
from leanflow_cli.lean.lean_diagnostics import diagnostic_items
from leanflow_cli.lean.lean_ephemeral import lean_ephemeral_source_check
from leanflow_cli.lean.lean_helper_ephemeral import check_helper_ephemerally
from leanflow_cli.lean.lean_incremental_axioms import (
    InlineAxiomQuery,
    build_inline_axiom_query,
    parse_inline_axiom_messages,
)
from leanflow_cli.lean.lean_interact_compat import install_linear_repl_reader
from leanflow_cli.lean.lean_parsing import (
    _contains_lean_suggestion_tactic,
    _declaration_line_index_from_text,
    _declaration_matches_target,
    _is_lean_inspection_only_helper_candidate,
    _is_lean_inspection_only_target_candidate,
    _statement_signature_text,
    _strip_lean_comments_and_strings,
)
from leanflow_cli.lean.lean_probe_deadline import (
    LeanProbeDeadlineExceeded,
    call_lean_probe_with_deadline,
)
from leanflow_cli.workflows.project import find_lean_project_root
from leanflow_cli.workflows.research_mode import research_mode_enabled

LOCAL_REPL_CANDIDATES = (
    ".lake/packages/repl",
    ".lake/build",
)
LOCAL_REPL_MISSING = "project-local Lean REPL binary not found; run `leanflow project init`"
LEAN_INCREMENTAL_TIMEOUT_DEFAULT_S: Final[int] = 60
DISPATCH_WORKER_INCREMENTAL_TIMEOUT_FLOOR_S: Final[int] = 900
RESEARCH_INCREMENTAL_TIMEOUT_FLOOR_S: Final[int] = 900
PROFILED_HELPER_TIMEOUT_FLOOR_S: Final[int] = 900
_ADMISSION_DEADLINE_CHARGE_THRESHOLD_S: Final[float] = 0.05

_PROBE: Any | None = None
_PROBE_EVER_STARTED = False

_TIMEOUT_DIAGNOSTIC_MARKERS: Final[tuple[str, ...]] = (
    "maximum number of heartbeats",
    "maxheartbeats",
    "deterministic timeout",
    "wall-clock deadline",
    "timed out",
)


def _import_lean_probe() -> tuple[Any, Any, Any, str]:
    try:
        install_linear_repl_reader()
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
    segmentation_text = _normalize_inline_scoped_declarations(text)
    header, segments = _probe_segment_file(segmentation_text)
    return _repair_option_wrapped_segments(text, header, list(segments))


_SCOPED_COMMAND_PREFIX_RE = r"(?:set_option|variable|include|omit|attribute|open(?:[ \t]+scoped)?)"
_SCOPED_COMMAND_WRAPPER_BEFORE_DECL_RE = re.compile(
    rf"(?m)(^[ \t]*(?:{_SCOPED_COMMAND_PREFIX_RE}[^\n]*\bin" r"(?:[ \t]*\n[ \t]*|[ \t]+))+)\Z"
)
_INLINE_SCOPED_DECLARATION_RE = re.compile(
    rf"(?m)^(?P<wrapper>[ \t]*{_SCOPED_COMMAND_PREFIX_RE}\b[^\n]*\bin)"
    r"(?P<space>[ \t]+)"
    r"(?P<declaration>(?:(?:private|protected|noncomputable|unsafe|partial|nonrec|scoped|local)\s+)*"
    r"(?:theorem|lemma|example|def|abbrev|opaque|axiom|instance|class|structure|inductive)\b)"
)


def _normalize_inline_scoped_declarations(text: str) -> str:
    """Expose same-line scoped declarations to LeanProbe without shifting offsets."""

    def replace(match: re.Match[str]) -> str:
        spacing = match.group("space")
        return (
            match.group("wrapper") + "\n" + (" " * (len(spacing) - 1)) + match.group("declaration")
        )

    return _INLINE_SCOPED_DECLARATION_RE.sub(replace, text)


def _rebuilt_segment(segment: Any, text: str, *, start: int, end: int) -> Any:
    """Return one segment rebuilt over an exact source range."""
    segment_text = text[start:end]
    return LeanIncrementalSegment(
        index=int(segment.index),
        kind=str(segment.kind),
        name=str(segment.name),
        start=start,
        end=end,
        declaration_start=int(getattr(segment, "declaration_start", start) or start),
        start_line=text.count("\n", 0, start) + 1,
        end_line=max(text.count("\n", 0, end), text.count("\n", 0, start) + 1),
        text=segment_text,
        text_hash=hashlib.sha256(segment_text.encode("utf-8")).hexdigest(),
    )


def _repair_option_wrapped_segments(
    text: str,
    header: str,
    segments: list[Any],
) -> tuple[str, list[Any]]:
    """Attach a preceding scoped command such as ``variable ... in`` to its declaration.

    LeanProbe's generic segmenter otherwise places the wrapper in the header
    or previous declaration. Incremental replay then elaborates a dangling
    ``in`` and reports an unexpected EOF against the wrong prerequisite.
    """
    if LeanIncrementalSegment is None or not segments:
        return header, segments
    repaired = list(segments)
    repaired_header = header
    for index, segment in enumerate(tuple(repaired)):
        start = int(getattr(segment, "start", 0) or 0)
        prefix = text[:start]
        match = _SCOPED_COMMAND_WRAPPER_BEFORE_DECL_RE.search(prefix)
        if match is None:
            continue
        wrapper_start = match.start(1)
        if index == 0:
            repaired_header = text[:wrapper_start]
        else:
            previous = repaired[index - 1]
            previous_start = int(getattr(previous, "start", 0) or 0)
            repaired[index - 1] = _rebuilt_segment(
                previous,
                text,
                start=previous_start,
                end=wrapper_start,
            )
        repaired[index] = _rebuilt_segment(
            segment,
            text,
            start=wrapper_start,
            end=int(getattr(segment, "end", start) or start),
        )
    return repaired_header, repaired


def _incremental_environment_failure(payload: Mapping[str, Any] | None) -> bool:
    """Return whether LeanProbe failed while rebuilding the pre-target environment."""
    if not payload:
        return False
    error_code = str(payload.get("error_code", "") or "").strip().lower()
    detail = " ".join(
        str(payload.get(key, "") or "") for key in ("error", "output", "message", "hint")
    ).lower()
    return bool(
        error_code
        in {
            "header_failed",
            "prior_decl_failed",
            "prior_declaration_failed",
        }
        or "failed to build env before target" in detail
    )


def _target_replaced_source(
    source_text: str,
    *,
    theorem_id: str,
    replacement: str,
) -> tuple[str, bool] | None:
    """Return exact full source with only the assigned declaration replaced."""
    try:
        _header, segments = _segment_file(source_text)
    except Exception:
        return None
    segment = _find_segment(segments, theorem_id)
    if segment is None:
        return None
    declaration_start = int(getattr(segment, "declaration_start", -1))
    segment_start = int(getattr(segment, "start", declaration_start))
    end = int(getattr(segment, "end", -1))
    if not 0 <= segment_start <= declaration_start <= end <= len(source_text):
        return None
    candidate = str(replacement or "").strip()
    original = str(getattr(segment, "text", "") or "")
    target_source = candidate or original
    if not target_source:
        return None
    preamble = source_text[segment_start:declaration_start]
    replacement_start = declaration_start
    if preamble.strip() and target_source.lstrip().startswith(preamble.strip()):
        # Inline axiom queries are built from the complete LeanProbe segment,
        # including its doc/attribute preamble. Replace that complete segment
        # instead of retaining and duplicating the original preamble.
        replacement_start = segment_start
    integrated = (
        source_text[:replacement_start] + target_source.rstrip() + "\n\n" + source_text[end:]
    )
    return integrated, _replacement_has_placeholder(target_source)


def _canonical_target_fallback(
    incremental: Mapping[str, Any],
    *,
    source_text: str,
    theorem_id: str,
    replacement: str,
    resolved: Path,
    project_root: Path,
    timeout_s: int,
) -> dict[str, Any]:
    """Replace a prefix-build failure with one canonical exact-source target check."""
    candidate = _target_replaced_source(
        source_text,
        theorem_id=theorem_id,
        replacement=replacement,
    )
    if candidate is None:
        return dict(incremental)
    integrated_source, target_has_placeholder = candidate
    checked = dict(
        lean_ephemeral_source_check(
            integrated_source,
            cwd=project_root,
            timeout_s=max(1, int(timeout_s or 1)),
        )
        or {}
    )
    backend_ok = checked.get("success") is True and checked.get("ok") is True
    elaboration_ran = backend_ok or (
        not bool(checked.get("timed_out"))
        and str(checked.get("failure_kind", "") or "") == "lean_elaboration"
    )
    target_ok = backend_ok and not target_has_placeholder
    incremental_detail = str(
        incremental.get("error", "")
        or incremental.get("output", "")
        or incremental.get("message", "")
        or ""
    )
    return {
        **checked,
        "success": elaboration_ran,
        "ok": target_ok,
        "backend": "lean_exact_ephemeral",
        "tool": "lake_env_lean",
        "action": "check_target",
        "file": str(resolved),
        "target": theorem_id,
        "has_errors": elaboration_ran and not backend_ok,
        "has_sorry": target_has_placeholder,
        "valid_without_sorry": target_ok,
        "canonical_fallback": True,
        "incremental_fallback_error_code": str(incremental.get("error_code", "") or ""),
        "incremental_fallback_reason": incremental_detail[:1000],
        "error_code": (
            ""
            if target_ok
            else (
                "target_placeholder"
                if target_has_placeholder and backend_ok
                else str(checked.get("error_code", "") or "canonical_elaboration_failed")
            )
        ),
    }


def _canonical_feedback_fallback(
    incremental: Mapping[str, Any],
    *,
    source_text: str,
    theorem_id: str,
    replacement: str,
    resolved: Path,
    project_root: Path,
    timeout_s: int,
) -> dict[str, Any]:
    """Return exact full-source diagnostics after incremental feedback replay fails."""
    candidate = _target_replaced_source(
        source_text,
        theorem_id=theorem_id,
        replacement=replacement,
    )
    if candidate is None:
        return dict(incremental)
    integrated_source, target_has_placeholder = candidate
    checked = dict(
        lean_ephemeral_source_check(
            integrated_source,
            cwd=project_root,
            timeout_s=max(1, int(timeout_s or 1)),
        )
        or {}
    )
    backend_ok = checked.get("success") is True and checked.get("ok") is True
    elaboration_ran = backend_ok or (
        not bool(checked.get("timed_out"))
        and str(checked.get("failure_kind", "") or "") == "lean_elaboration"
    )
    output = str(checked.get("output", "") or checked.get("error", "") or "")
    incremental_detail = str(
        incremental.get("error", "")
        or incremental.get("output", "")
        or incremental.get("message", "")
        or ""
    )
    return {
        **checked,
        "success": elaboration_ran,
        "ok": False,
        "backend": "lean_exact_ephemeral",
        "tool": "lake_env_lean",
        "action": "feedback",
        "file": str(resolved),
        "target": theorem_id,
        "has_errors": elaboration_ran and not backend_ok,
        "has_sorry": target_has_placeholder,
        "valid_without_sorry": False,
        "target_verified": False,
        "diagnostic_only": True,
        "feedback_lean": output,
        "canonical_fallback": True,
        "incremental_fallback_error_code": str(incremental.get("error_code", "") or ""),
        "incremental_fallback_reason": incremental_detail[:1000],
        "error_code": (
            ""
            if elaboration_ran
            else str(checked.get("error_code", "") or "canonical_feedback_failed")
        ),
    }


def _canonical_file_fallback(
    incremental: Mapping[str, Any],
    *,
    source_text: str,
    resolved: Path,
    project_root: Path,
    timeout_s: int,
) -> dict[str, Any]:
    """Replace a cached prefix-build failure with one exact full-source check."""
    checked = dict(
        lean_ephemeral_source_check(
            source_text,
            cwd=project_root,
            timeout_s=max(1, int(timeout_s or 1)),
        )
        or {}
    )
    backend_ok = checked.get("success") is True and checked.get("ok") is True
    elaboration_ran = backend_ok or (
        not bool(checked.get("timed_out"))
        and str(checked.get("failure_kind", "") or "") == "lean_elaboration"
    )
    source_has_placeholder = _replacement_has_placeholder(source_text)
    incremental_detail = str(
        incremental.get("error", "")
        or incremental.get("output", "")
        or incremental.get("message", "")
        or ""
    )
    return {
        **checked,
        "success": elaboration_ran,
        "ok": backend_ok and not source_has_placeholder,
        "backend": "lean_exact_ephemeral",
        "tool": "lake_env_lean",
        "action": "check_file",
        "file": str(resolved),
        "has_errors": elaboration_ran and not backend_ok,
        "has_sorry": source_has_placeholder,
        "valid_without_sorry": backend_ok and not source_has_placeholder,
        "canonical_fallback": True,
        "incremental_fallback_error_code": str(incremental.get("error_code", "") or ""),
        "incremental_fallback_reason": incremental_detail[:1000],
        "error_code": (
            ""
            if backend_ok
            else str(checked.get("error_code", "") or "canonical_elaboration_failed")
        ),
    }


def _canonical_output_messages(output: str) -> list[dict[str, Any]]:
    """Return ordered exact-Lean output lines with actionable severities restored."""
    messages: list[dict[str, Any]] = []
    for line in str(output or "").splitlines():
        if not line.strip():
            continue
        parsed = diagnostic_items(line)
        if parsed:
            messages.append(parsed[0])
            continue
        bare = re.match(
            r"^\s*(?P<severity>error|warning)(?:\([^)]*\))?:\s*(?P<message>.*)$",
            line,
            flags=re.IGNORECASE,
        )
        if bare:
            messages.append(
                {
                    "severity": bare.group("severity").lower(),
                    "message": bare.group("message").strip(),
                }
            )
            continue
        messages.append({"severity": "information", "message": line})
    return messages


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
    global _PROBE, _PROBE_EVER_STARTED
    if LeanProbe is None:
        raise RuntimeError(_LEAN_PROBE_IMPORT_ERROR)
    if _PROBE is None:
        # LeanProbe calls its module-local segmenter directly. Route that call
        # through LeanFlow's compatibility repairs so scoped commands such as
        # ``variable ... in`` remain attached to the declaration they govern.
        # Without this, the wrapper is replayed at the end of the preceding
        # segment and a valid file fails incrementally with an unexpected EOF.
        from lean_probe import probe as lean_probe_runtime

        lean_probe_runtime.segment_file = _segment_file
        _PROBE = LeanProbe(auto_build=False)
        _PROBE_EVER_STARTED = True
    return _PROBE


def lean_scratch_check(code: str, *, cwd: str = "", timeout_s: int = 90) -> dict[str, Any]:
    """Run a standalone Lean snippet through the warm LeanProbe REPL.

    The scratch surface for probes and experiments returns the normalized
    LeanProbe payload (``success`` = the tool
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
        "check_file": "check_file",
        "file": "check_file",
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
    elif (
        research_mode_enabled()
        and _PROBE is None
        and requested < RESEARCH_INCREMENTAL_TIMEOUT_FLOOR_S
    ):
        effective = RESEARCH_INCREMENTAL_TIMEOUT_FLOOR_S
        policy = "research_cold_start_floor"
    elif profiled_helper and requested < PROFILED_HELPER_TIMEOUT_FLOOR_S:
        effective = PROFILED_HELPER_TIMEOUT_FLOOR_S
        policy = "profiled_helper_cold_start_floor"

    normalized_ceiling = None if timeout_ceiling_s is None else max(1, int(timeout_ceiling_s))
    hard_ceiling = configured_hard_timeout_s()
    if hard_ceiling is not None:
        normalized_ceiling = (
            hard_ceiling if normalized_ceiling is None else min(normalized_ceiling, hard_ceiling)
        )
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
_DEFAULT_PROVIDER_CHECK_MAX_CHARS = 6000


def _feedback_max_chars() -> int:
    """Character ceiling for a feedback payload (env-overridable, positive int)."""
    try:
        value = int(os.getenv("LEANFLOW_INCREMENTAL_FEEDBACK_MAX_CHARS", "") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else _DEFAULT_FEEDBACK_MAX_CHARS


def _provider_check_max_chars() -> int:
    """Return the model-facing check-result ceiling without shrinking audit evidence."""
    try:
        value = int(os.getenv("LEANFLOW_INCREMENTAL_PROVIDER_MAX_CHARS", "") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else _DEFAULT_PROVIDER_CHECK_MAX_CHARS


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


def _truncate_diagnostic_text(value: Any, max_chars: int) -> str:
    """Return bounded diagnostic text while preserving its beginning and end."""
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    marker = "\n...[diagnostic text truncated]...\n"
    available = max(0, max_chars - len(marker))
    head = max(1, (available * 2) // 3)
    tail = max(0, available - head)
    return text[:head] + marker + (text[-tail:] if tail else "")


def _bound_failed_check_payload(result: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Bound failed target/helper diagnostics before replaying them to the model.

    LeanProbe can return the whole annotated declaration plus a tactic state
    for every line. The replacement is already present in the tool request, so
    retain the first errors and a small proof-state sample instead of replaying
    a second declaration-sized trace on every later API call.
    """
    if result.get("ok") is not False or str(result.get("action", "") or "") not in {
        "check_target",
        "check_helper",
    }:
        return result
    result = dict(result)
    first_error = next(
        (
            str(message.get("message", "") or "").strip()
            for message in list(result.get("messages") or [])
            if isinstance(message, Mapping)
            and str(message.get("severity", "") or "").strip().lower() == "error"
            and str(message.get("message", "") or "").strip()
        ),
        "",
    )
    if first_error:
        result["error"] = first_error
    try:
        if len(json.dumps(result, ensure_ascii=False)) <= max_chars:
            return result
    except (TypeError, ValueError):
        return result

    bounded = _bound_feedback_payload(dict(result), max_chars)
    tactics = bounded.get("tactics")
    if isinstance(tactics, list) and len(tactics) > 2:
        bounded["tactics"] = tactics[:2]
        bounded["tactics_truncated"] = {"kept": 2, "total": len(tactics)}

    feedback = bounded.get("feedback_lean")
    if feedback:
        bounded["feedback_lean"] = _truncate_diagnostic_text(
            feedback,
            max(1200, max_chars // 3),
        )

    messages = bounded.get("messages")
    if isinstance(messages, list):
        messages = sorted(
            enumerate(messages),
            key=lambda item: (
                {
                    "error": 0,
                    "warning": 1,
                }.get(
                    (
                        str(item[1].get("severity", "") or "").lower()
                        if isinstance(item[1], Mapping)
                        else ""
                    ),
                    2,
                ),
                item[0],
            ),
        )
        prioritized_messages = [message for _index, message in messages]
        compact_messages: list[Any] = []
        for message in prioritized_messages[:8]:
            if not isinstance(message, dict):
                compact_messages.append(message)
                continue
            compact = dict(message)
            if "message" in compact:
                compact["message"] = _truncate_diagnostic_text(compact["message"], 1600)
            compact_messages.append(compact)
        bounded["messages"] = compact_messages
        if len(prioritized_messages) > len(compact_messages):
            bounded["messages_truncated"] = {
                "kept": len(compact_messages),
                "total": len(prioritized_messages),
            }

    for field in ("error", "output"):
        if bounded.get(field):
            bounded[field] = _truncate_diagnostic_text(bounded[field], 2400)

    try:
        still_oversized = len(json.dumps(bounded, ensure_ascii=False)) > max_chars
    except (TypeError, ValueError):
        still_oversized = False
    if still_oversized:
        tactics = bounded.pop("tactics", None)
        if isinstance(tactics, list):
            previous = dict(bounded.get("tactics_truncated") or {})
            bounded["tactics_truncated"] = {
                "kept": 0,
                "total": int(previous.get("total", len(tactics)) or len(tactics)),
            }
        if bounded.get("feedback_lean"):
            bounded["feedback_lean"] = _truncate_diagnostic_text(
                bounded["feedback_lean"],
                1200,
            )
        messages = bounded.get("messages")
        if isinstance(messages, list):
            total_messages = int(
                dict(bounded.get("messages_truncated") or {}).get("total", len(messages))
                or len(messages)
            )
            compact_messages = []
            for message in messages[:4]:
                if not isinstance(message, dict):
                    compact_messages.append(message)
                    continue
                compact = dict(message)
                if "message" in compact:
                    compact["message"] = _truncate_diagnostic_text(compact["message"], 800)
                compact_messages.append(compact)
            bounded["messages"] = compact_messages
            bounded["messages_truncated"] = {
                "kept": len(compact_messages),
                "total": total_messages,
            }
        for field in ("error", "output"):
            if bounded.get(field):
                bounded[field] = _truncate_diagnostic_text(bounded[field], 800)
    bounded["diagnostic_payload_truncated"] = True
    bounded["diagnostic_payload_max_chars"] = max_chars
    return bounded


def _compact_provider_messages(messages: Any) -> tuple[list[Any], int]:
    """Return error-first bounded diagnostics and their original count."""
    if not isinstance(messages, list):
        return [], 0
    ordered = sorted(
        enumerate(messages),
        key=lambda item: (
            (
                0
                if isinstance(item[1], Mapping)
                and str(item[1].get("severity", "") or "").lower() == "error"
                else 1
            ),
            item[0],
        ),
    )
    compact_messages: list[Any] = []
    for _index, message in ordered[:4]:
        if not isinstance(message, Mapping):
            compact_messages.append(_truncate_diagnostic_text(message, 800))
            continue
        compact = {
            key: value
            for key, value in message.items()
            if key in {"severity", "message", "start", "end", "file_start", "file_end"}
        }
        if compact.get("message"):
            compact["message"] = _truncate_diagnostic_text(compact["message"], 1200)
        compact_messages.append(compact)
    return compact_messages, len(messages)


def _provider_relevant_goals(payload: Mapping[str, Any]) -> list[str]:
    """Extract a small distinct goal sample from a LeanProbe tactic trace."""
    tactics = payload.get("tactics")
    if not isinstance(tactics, list):
        return []
    goals: list[str] = []
    for tactic in tactics:
        if not isinstance(tactic, Mapping) or not tactic.get("goals"):
            continue
        goal = _truncate_diagnostic_text(tactic["goals"], 1200)
        if goal not in goals:
            goals.append(goal)
        if len(goals) == 2:
            break
    return goals


def compact_check_payload(
    result: Mapping[str, Any],
    *,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Project target/helper evidence into bounded, error-first model context.

    The tool executor records the original result before applying this model-
    facing projection. Retain verdict, identity, timing, and the first useful
    diagnostics while leaving complete tactic traces in the durable audit log.
    """
    payload = dict(result)
    if str(payload.get("action", "") or "") not in {
        "check_target",
        "check_helper",
    }:
        return payload
    cap = max(2_000, int(max_chars or _provider_check_max_chars()))
    tactics = payload.get("tactics")
    try:
        serialized = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return payload

    keep_fields = {
        "success",
        "ok",
        "action",
        "backend",
        "tool",
        "command",
        "file",
        "target",
        "target_kind",
        "target_range",
        "cache",
        "elapsed_s",
        "valid_without_sorry",
        "has_errors",
        "has_sorry",
        "timed_out",
        "retryable",
        "replacement_matches_target",
        "replacement_declarations",
        "verification_scope",
        "anchor_target",
        "anchor_temporary_sorry",
        "axiom_profile_requested",
        "axiom_profile_checked",
        "axiom_profile_axioms",
        "axiom_profile_blockers",
        "axiom_profile_error",
        "requested_timeout_s",
        "effective_timeout_s",
        "timeout_adjusted",
        "timeout_policy",
        "timeout_ceiling_s",
        "leanflow_timing",
        "error_code",
        "status",
        "diagnostic_only",
        "proof_progress",
        "helper_elaborated",
        "inspection_completed",
        "message",
        "action_required",
        "target_verified",
    }
    projected = {key: value for key, value in payload.items() if key in keep_fields}
    verified = payload.get("ok") is True and payload.get("valid_without_sorry") is not False
    projected["verification_status"] = "verified" if verified else "not_verified"

    messages, message_total = _compact_provider_messages(payload.get("messages"))
    anchor_messages, anchor_message_total = _compact_provider_messages(
        payload.get("anchor_messages")
    )
    if messages:
        projected["messages"] = messages
    if anchor_messages:
        projected["anchor_messages"] = anchor_messages
    if message_total > len(messages):
        projected["messages_truncated"] = {
            "kept": len(messages),
            "total": message_total,
        }
    if anchor_message_total > len(anchor_messages):
        projected["anchor_messages_truncated"] = {
            "kept": len(anchor_messages),
            "total": anchor_message_total,
        }

    error_messages = [
        str(message.get("message", "") or "")
        for message in messages + anchor_messages
        if isinstance(message, Mapping)
        and str(message.get("severity", "") or "").lower() == "error"
        and message.get("message")
    ]
    actionable_error = str(payload.get("error", "") or "").strip()
    if not actionable_error and error_messages:
        actionable_error = error_messages[0]
    if not actionable_error and not verified:
        actionable_error = str(payload.get("output", "") or "").strip()
    if actionable_error:
        projected["actionable_error"] = _truncate_diagnostic_text(actionable_error, 1800)

    relevant_goals = _provider_relevant_goals(payload)
    if relevant_goals:
        projected["relevant_goals"] = relevant_goals

    if not actionable_error and payload.get("output"):
        projected["output"] = _truncate_diagnostic_text(payload["output"], 1200)
    if not verified and not actionable_error and payload.get("feedback_lean"):
        projected["feedback_lean"] = _truncate_diagnostic_text(payload["feedback_lean"], 1200)
    if isinstance(tactics, list):
        projected["tactics_truncated"] = {"kept": 0, "total": len(tactics)}
    omitted = sorted(set(payload).difference(projected).difference({"tactics"}))
    projected.update(
        {
            "provider_context_projected": True,
            "audit_payload_preserved": True,
            "audit_payload_chars": len(serialized),
            "audit_payload_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "projected_fields_omitted": omitted,
        }
    )
    while len(json.dumps(projected, ensure_ascii=False)) > cap:
        if projected.pop("feedback_lean", None) is not None:
            continue
        goals = projected.get("relevant_goals")
        if isinstance(goals, list) and len(goals) > 1:
            projected["relevant_goals"] = goals[:1]
            continue
        if projected.pop("anchor_messages", None) is not None:
            continue
        messages = projected.get("messages")
        if isinstance(messages, list) and len(messages) > 1:
            projected["messages"] = messages[:1]
            continue
        if projected.get("actionable_error"):
            projected["actionable_error"] = _truncate_diagnostic_text(
                projected["actionable_error"], 800
            )
            if len(json.dumps(projected, ensure_ascii=False)) <= cap:
                break
        projected.pop("messages", None)
        projected.pop("relevant_goals", None)
        break
    if len(json.dumps(projected, ensure_ascii=False)) > cap:
        essential_fields = {
            "success",
            "ok",
            "action",
            "file",
            "target",
            "valid_without_sorry",
            "has_errors",
            "has_sorry",
            "timed_out",
            "elapsed_s",
            "error_code",
            "status",
            "verification_status",
            "actionable_error",
            "relevant_goals",
            "tactics_truncated",
            "provider_context_projected",
            "audit_payload_preserved",
            "audit_payload_chars",
            "audit_payload_sha256",
        }
        projected = {key: value for key, value in projected.items() if key in essential_fields}
        if projected.get("actionable_error"):
            projected["actionable_error"] = _truncate_diagnostic_text(
                projected["actionable_error"], 500
            )
        goals = projected.get("relevant_goals")
        if isinstance(goals, list):
            projected["relevant_goals"] = [
                _truncate_diagnostic_text(goal, 500) for goal in goals[:1]
            ]
        projected["projection_emergency_compacted"] = True
    return projected


def compact_successful_check_payload(
    result: Mapping[str, Any],
    *,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Preserve the compatibility name for the generalized check projection."""
    return compact_check_payload(result, max_chars=max_chars)


def _normalize_payload(payload: dict[str, Any], action: str) -> dict[str, Any]:
    """Normalize LeanProbe metadata and recover timeout semantics from diagnostics."""
    result = dict(payload)
    result["action"] = action
    result.setdefault("backend", "lean_interact")
    result.setdefault("tool", "lean_probe")
    result["command"] = f"lean_probe {action}"
    diagnostic_text = " ".join(
        (
            *(str(result.get(key, "") or "") for key in ("error", "output", "message")),
            *(
                str(item.get("message", "") or "")
                for item in (result.get("messages") or [])
                if isinstance(item, Mapping)
            ),
        )
    ).lower()
    diagnostic_timeout = any(marker in diagnostic_text for marker in _TIMEOUT_DIAGNOSTIC_MARKERS)
    if diagnostic_timeout and not bool(result.get("timed_out")):
        # LeanProbe can report heartbeat exhaustion as an ordinary Lean error
        # while leaving its transport-level timeout flag false.
        result["timed_out"] = True
        result["timed_out_inferred_from_diagnostics"] = True
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


def _mark_inspection_only_helper_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep inspection evidence while denying reusable-helper verification fields."""
    result = dict(payload)
    helper_elaborated = bool(result.get("ok"))
    result.update(
        {
            "ok": False,
            "target_verified": False,
            "valid_without_sorry": False,
            "status": "inspection_only_helper",
            "diagnostic_only": True,
            "proof_progress": False,
            "helper_elaborated": helper_elaborated,
            "inspection_completed": bool(result.get("success")) and not _payload_has_errors(result),
            "error_code": "inspection_only_helper_candidate",
            "message": (
                "The dummy helper elaborated only as an environment/type inspection wrapper. "
                "Its diagnostics remain available, but it is not a verified reusable lemma."
            ),
            "action_required": (
                "Use the returned declaration/type information. If a checked local `have` is "
                "useful, resubmit its exact proposition as a substantive, mathematically named "
                "helper so it remains reusable across turns and context compression; if the "
                "`have` is already in assigned source, `lean_extract_have` can promote it."
            ),
        }
    )
    return result


def _mark_inspection_only_target_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve target diagnostics without treating instrumentation as proof progress."""
    result = dict(payload)
    result.update(
        {
            "ok": False,
            "target_verified": False,
            "valid_without_sorry": False,
            "status": "inspection_only_target",
            "diagnostic_only": True,
            "proof_progress": False,
            "inspection_completed": bool(result.get("success")) and not _payload_has_errors(result),
            "error_code": "inspection_only_target_candidate",
            "message": (
                "The assigned-target replacement contains temporary Lean inspection "
                "commands. Its diagnostics remain available, but it is not a production "
                "proof candidate."
            ),
            "action_required": (
                "Use the returned proof state, then submit a clean target replacement "
                "without `trace_state`, `#check`, `#print`, `#eval`, `#reduce`, or `run_cmd`."
            ),
        }
    )
    return result


def _replacement_has_placeholder(replacement: str) -> bool:
    """Return whether executable replacement source contains a proof placeholder."""
    stripped = _strip_lean_comments_and_strings(str(replacement or ""))
    return bool(re.search(r"\b(?:sorry|admit|sorryAx)\b", stripped, flags=re.IGNORECASE))


def _target_source_text(source_text: str, theorem_id: str) -> str:
    """Return the exact parsed target declaration text when available."""
    entry = next(
        (
            item
            for item in _declaration_line_index_from_text(source_text)
            if _declaration_matches_target(item, theorem_id)
        ),
        None,
    )
    return str(entry.get("text", "") or "") if entry is not None else ""


def _suggestion_rejection_payload(
    *,
    action: str,
    file_path: Path,
    theorem_id: str,
    replacement_metadata: Mapping[str, Any],
    requested_timeout_s: int,
    effective_timeout_s: int,
    timeout_adjusted: bool,
    timeout_policy: str,
    timeout_ceiling_s: int | None,
    operation_started: float,
) -> dict[str, Any]:
    """Reject diagnostic suggestion tactics as proof-verification candidates."""
    error = (
        "suggestion tactics are diagnostic-only; submit the suggested concrete term "
        "for verification"
    )
    return {
        "success": True,
        "ok": False,
        "backend": "deterministic_preflight",
        "tool": "lean_incremental_check",
        "action": action,
        "file": str(file_path),
        "target": theorem_id,
        "valid_without_sorry": False,
        "target_verified": False,
        "has_errors": False,
        "has_sorry": False,
        "timed_out": False,
        "retryable": False,
        "error": error,
        "error_code": "suggestion_tactic_diagnostic_only",
        "output": error,
        "lean_started": False,
        "status": "suggestion_tactic_diagnostic_only",
        "diagnostic_only": True,
        "proof_progress": False,
        **dict(replacement_metadata),
        **_timeout_metadata(
            requested_timeout_s=requested_timeout_s,
            effective_timeout_s=effective_timeout_s,
            timeout_adjusted=timeout_adjusted,
            timeout_policy=timeout_policy,
            timeout_ceiling_s=timeout_ceiling_s,
        ),
        "leanflow_timing": {
            "total_s": round(max(0.0, time.monotonic() - operation_started), 3),
            "admission_wait_s": 0.0,
            "probe_call_s": 0.0,
            "session_reclaim_s": 0.0,
            "postprocess_s": 0.0,
        },
    }


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
    if output and not list(result.get("messages") or []):
        result["messages"] = _canonical_output_messages(output)
    first_error = next(
        (
            str(message.get("message", "") or "").strip()
            for message in list(result.get("messages") or [])
            if isinstance(message, Mapping)
            and str(message.get("severity", "") or "").strip().lower() == "error"
            and str(message.get("message", "") or "").strip()
        ),
        "",
    )
    if first_error:
        result["error"] = first_error
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


def _discard_timed_out_probe(probe: Any) -> None:
    """Detach a timed-out probe generation without reacquiring its call lock."""
    global _PROBE
    if _PROBE is probe:
        _PROBE = None


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
    allow_placeholders_for_elaboration: bool = False,
) -> dict[str, Any]:
    """Dispatch an incremental Lean check through the appropriate exact backend.

    ``include_axiom_profile`` embeds one marker-isolated ``#print axioms`` in
    an exact target check, or selects the exact one-shot helper harness for a
    helper candidate. Complete parsed evidence is returned alongside the
    ordinary verdict; missing or malformed output fails closed.

    ``timeout_ceiling_s`` is an internal parent-deadline cap. It is deliberately
    absent from the model-facing tool schema so only authoritative callers can
    shorten cold-start floors.

    ``allow_placeholders_for_elaboration`` is likewise internal. Decomposition
    uses it to distinguish a placeholder template that elaborates from a
    complete proof candidate; ordinary acceptance checks remain fail-closed.
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
    inspection_only_helper = bool(
        leanflow_action == "check_helper" and _is_lean_inspection_only_helper_candidate(replacement)
    )
    inspection_only_target = bool(
        leanflow_action == "check_target" and _is_lean_inspection_only_target_candidate(replacement)
    )
    if leanflow_action == "check_helper" and re.search(
        r"(?m)^\s*#print\s+prefix\b",
        replacement,
    ):
        payload = _error_payload(
            action=leanflow_action,
            error=(
                "Broad `#print prefix` inspection is disabled because it produces large, "
                "slow environment dumps. Inspect an exact declaration with `#check`, "
                "`lean_outline`, or a bounded local search."
            ),
            error_code="broad_print_prefix_rejected",
            file_path=resolved,
            target=theorem_id,
        )
        payload.update(
            {
                "status": "bounded_symbol_inspection_required",
                "lean_started": False,
                "search_progress": False,
            }
        )
        return payload
    if include_axiom_profile and leanflow_action not in {"check_target", "check_helper"}:
        return _error_payload(
            action=leanflow_action,
            error="axiom profiles require action=check_target or action=check_helper",
            error_code="inline_axiom_profile_unsupported_action",
            file_path=resolved,
            target=theorem_id,
        )
    if leanflow_action == "check_file" and replacement.strip():
        return _error_payload(
            action=leanflow_action,
            error="check_file reads the current file and does not accept replacement",
            error_code="file_replacement_unsupported",
            file_path=resolved,
            target=theorem_id,
        )
    probe_action = leanflow_action
    probe_replacement = replacement
    replacement_metadata: dict[str, Any] = {}
    current_candidate = ""
    if leanflow_action == "check_file":
        current_candidate = source_text
    elif leanflow_action == "check_target" and not replacement.strip():
        current_candidate = _target_source_text(source_text, theorem_id)
    if current_candidate and _contains_lean_suggestion_tactic(current_candidate):
        return _suggestion_rejection_payload(
            action=leanflow_action,
            file_path=resolved,
            theorem_id=theorem_id,
            replacement_metadata={
                "verification_scope": (
                    "target_candidate" if leanflow_action == "check_target" else "file"
                )
            },
            requested_timeout_s=requested_timeout_s,
            effective_timeout_s=effective_timeout_s,
            timeout_adjusted=timeout_adjusted,
            timeout_policy=timeout_policy,
            timeout_ceiling_s=normalized_timeout_ceiling_s,
            operation_started=operation_started,
        )
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
        if _contains_lean_suggestion_tactic(replacement):
            return _suggestion_rejection_payload(
                action=leanflow_action,
                file_path=resolved,
                theorem_id=theorem_id,
                replacement_metadata=replacement_metadata,
                requested_timeout_s=requested_timeout_s,
                effective_timeout_s=effective_timeout_s,
                timeout_adjusted=timeout_adjusted,
                timeout_policy=timeout_policy,
                timeout_ceiling_s=normalized_timeout_ceiling_s,
                operation_started=operation_started,
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
            if inspection_only_helper:
                result = _mark_inspection_only_helper_payload(result)
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
        if leanflow_action == "check_target" and _contains_lean_suggestion_tactic(replacement):
            return _suggestion_rejection_payload(
                action=leanflow_action,
                file_path=resolved,
                theorem_id=theorem_id,
                replacement_metadata=replacement_metadata,
                requested_timeout_s=requested_timeout_s,
                effective_timeout_s=effective_timeout_s,
                timeout_adjusted=timeout_adjusted,
                timeout_policy=timeout_policy,
                timeout_ceiling_s=normalized_timeout_ceiling_s,
                operation_started=operation_started,
            )
        if (
            leanflow_action == "check_target"
            and _replacement_has_placeholder(replacement)
            and not allow_placeholders_for_elaboration
        ):
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
    probe_deadline: LeanProbeDeadlineExceeded | None = None
    with project_lean_heavy_admission(project_root) as admission:
        admission_wait_s = max(0.0, time.monotonic() - admission_started)
        # Lock setup and scheduler jitter can consume a few milliseconds even
        # without meaningful contention. Do not shave that noise from an
        # advertised cold-start floor; charge only an admission delay large
        # enough to matter to the end-to-end deadline.
        charged_admission_wait_s = (
            admission_wait_s if admission_wait_s >= _ADMISSION_DEADLINE_CHARGE_THRESHOLD_S else 0.0
        )
        probe_timeout_s = max(
            0.01,
            float(effective_timeout_s) - charged_admission_wait_s,
        )
        probe_started = time.monotonic()
        try:
            probe = _probe()
            if leanflow_action == "prepare_file":
                payload = call_lean_probe_with_deadline(
                    probe,
                    "prepare_file",
                    resolved,
                    deadline_s=probe_timeout_s,
                    theorem_id=theorem_id,
                    cwd=project_root,
                    timeout_s=probe_timeout_s,
                )
            elif leanflow_action == "check_file":
                _header, segments = _segment_file(source_text)
                final_segment = segments[-1] if segments else None
                final_target = str(getattr(final_segment, "name", "") or "")
                if final_target:
                    # Checking the final declaration makes LeanProbe replay every
                    # changed predecessor through its per-declaration cache. Ask
                    # for tactics up front so an intentional final `sorry` does
                    # not trigger the ordinary failed-target diagnostic rerun.
                    payload = call_lean_probe_with_deadline(
                        probe,
                        "check_target",
                        resolved,
                        deadline_s=probe_timeout_s,
                        theorem_id=final_target,
                        cwd=project_root,
                        replacement="",
                        include_tactics=True,
                        timeout_s=probe_timeout_s,
                    )
                else:
                    payload = call_lean_probe_with_deadline(
                        probe,
                        "prepare_file",
                        resolved,
                        deadline_s=probe_timeout_s,
                        theorem_id="",
                        cwd=project_root,
                        timeout_s=probe_timeout_s,
                    )
            elif probe_action == "check_target":
                payload = call_lean_probe_with_deadline(
                    probe,
                    "check_target",
                    resolved,
                    deadline_s=probe_timeout_s,
                    theorem_id=theorem_id,
                    cwd=project_root,
                    replacement=probe_replacement,
                    include_tactics=include_tactics,
                    timeout_s=probe_timeout_s,
                )
            elif leanflow_action == "feedback":
                payload = call_lean_probe_with_deadline(
                    probe,
                    "feedback",
                    resolved,
                    deadline_s=probe_timeout_s,
                    theorem_id=theorem_id,
                    cwd=project_root,
                    replacement=replacement,
                    timeout_s=probe_timeout_s,
                )
            else:
                return _error_payload(
                    action=leanflow_action,
                    error=f"unsupported lean_incremental_check action: {action}",
                    error_code="unsupported_action",
                    file_path=resolved,
                    target=theorem_id,
                )
        except LeanProbeDeadlineExceeded as exc:
            probe_deadline = exc
            _discard_timed_out_probe(probe)
            payload = _error_payload(
                action=leanflow_action,
                error=str(exc),
                error_code="lean_probe_wall_clock_timeout",
                file_path=resolved,
                target=theorem_id,
                timed_out=True,
            )
            payload.update(
                {
                    "retryable": True,
                    "probe_worker_stopped": exc.worker_stopped,
                    "probe_sessions_terminated": exc.sessions_terminated,
                }
            )
        finally:
            probe_call_s = max(0.0, time.monotonic() - probe_started)
            # Releasing only the file slot would be unsound if LeanProbe kept a
            # multi-gigabyte LSP child alive after returning its response.
            if probe_deadline is not None:
                reclaimed_incremental_session = bool(
                    probe_deadline.worker_stopped and probe_deadline.sessions_terminated
                )
                if not reclaimed_incremental_session:
                    admission.retain_until_process_exit(
                        "LeanProbe deadline cancellation did not fully stop owned work"
                    )
            elif project_lean_service_reclaim_enabled():
                reclaim_started = time.monotonic()
                reclaimed_incremental_session = close_incremental_sessions()
                session_reclaim_s = max(0.0, time.monotonic() - reclaim_started)
                if not reclaimed_incremental_session:
                    admission.retain_until_process_exit(
                        "LeanProbe incremental session close failed"
                    )
    postprocess_started = time.monotonic()
    result = _normalize_payload(payload, leanflow_action)
    fallback_timeout_s = max(
        0.01,
        float(effective_timeout_s) - max(0.0, time.monotonic() - operation_started),
    )
    if _incremental_environment_failure(result):
        if leanflow_action == "check_helper":
            result = _normalize_profiled_helper_payload(
                check_helper_ephemerally(
                    source_text=source_text,
                    helper_source=replacement,
                    theorem_id=theorem_id,
                    file_path=resolved,
                    project_root=project_root,
                    anchor_skeleton=anchor_skeleton,
                    timeout_s=fallback_timeout_s,
                )
            )
            result.update(
                {
                    "canonical_fallback": True,
                    "incremental_fallback_error_code": str(payload.get("error_code", "") or ""),
                    "incremental_fallback_reason": str(
                        payload.get("error", "")
                        or payload.get("output", "")
                        or payload.get("message", "")
                        or ""
                    )[:1000],
                }
            )
        elif leanflow_action == "check_file":
            result = _canonical_file_fallback(
                result,
                source_text=source_text,
                resolved=resolved,
                project_root=project_root,
                timeout_s=fallback_timeout_s,
            )
        elif leanflow_action == "check_target":
            result = _canonical_target_fallback(
                result,
                source_text=source_text,
                theorem_id=theorem_id,
                replacement=(probe_replacement if inline_axiom_query is not None else replacement),
                resolved=resolved,
                project_root=project_root,
                timeout_s=fallback_timeout_s,
            )
            if inline_axiom_query is not None and result.get("canonical_fallback"):
                output = str(result.get("output", "") or "")
                result["messages"] = _canonical_output_messages(output)
        elif leanflow_action == "feedback":
            result = _canonical_feedback_fallback(
                result,
                source_text=source_text,
                theorem_id=theorem_id,
                replacement=replacement,
                resolved=resolved,
                project_root=project_root,
                timeout_s=fallback_timeout_s,
            )
    if inline_axiom_query is not None:
        result = _attach_inline_axiom_profile(result, inline_axiom_query)
    if leanflow_action == "check_helper":
        result = _normalize_helper_check_payload(
            result,
            helper_source=replacement,
            anchor_target=theorem_id,
        )
        if inspection_only_helper:
            result = _mark_inspection_only_helper_payload(result)
    elif inspection_only_target:
        result = _mark_inspection_only_target_payload(result)
    result.update(replacement_metadata)
    if (
        leanflow_action == "check_target"
        and allow_placeholders_for_elaboration
        and _replacement_has_placeholder(replacement)
    ):
        elaborated = bool(result.get("success")) and not _payload_has_errors(result)
        result.update(
            {
                "ok": False,
                "target_verified": False,
                "valid_without_sorry": False,
                "has_sorry": True,
                "local_elaboration_only": True,
                "elaborated_with_placeholders": elaborated,
            }
        )
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
    return _bound_failed_check_payload(result, _feedback_max_chars())
