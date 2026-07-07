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
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.project import find_lean_project_root

LOCAL_REPL_CANDIDATES = (
    ".lake/packages/repl",
    ".lake/build",
)
LOCAL_REPL_MISSING = "project-local Lean REPL binary not found; run `leanflow project init`"

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
        probe = _probe()
        payload = probe.check_code(code, cwd=cwd or None, timeout_s=timeout_s)
        return dict(payload or {})
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
        "feedback": "feedback",
    }.get(normalized, normalized)


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


def close_incremental_sessions() -> None:
    global _PROBE
    if _PROBE is not None:
        try:
            _PROBE.close()
        finally:
            _PROBE = None


def lean_incremental_capabilities(cwd: str | Path | None = None) -> dict[str, Any]:
    """Return a dict reporting availability of incremental Lean checking and any degradation reasons. Detects project root, local REPL binary, and LeanProbe capabilities; includes active sessions and max code sessions from the probe."""
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
    if not _LEAN_PROBE_IMPORT_ERROR:
        try:
            probe_payload = dict(_probe().capabilities(project_root))
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
    timeout_s: int = 60,
) -> dict[str, Any]:
    """Dispatch an incremental Lean check (prepare_file, check_target, or feedback) via LeanProbe. Validates project root, file existence, and local REPL; routes the action to the appropriate LeanProbe method and normalizes the result payload."""
    epf_action = _leanflow_action(action)
    project_root = _resolve_project_root(cwd, file_path)
    if project_root is None:
        return _error_payload(
            action=epf_action,
            error="Lean project root not detected",
            error_code="no_project_root",
        )

    resolved = _resolve_file_path(file_path, project_root)
    if not resolved.is_file():
        return _error_payload(
            action=epf_action,
            error="Lean file not found",
            error_code="file_not_found",
            file_path=resolved,
        )

    repl_dir = _local_repl_dir(project_root)
    if repl_dir is None:
        return _error_payload(
            action=epf_action,
            error=LOCAL_REPL_MISSING,
            error_code="local_repl_missing",
            file_path=resolved,
            target=theorem_id,
        )

    if _LEAN_PROBE_IMPORT_ERROR:
        return _error_payload(
            action=epf_action,
            error=_LEAN_PROBE_IMPORT_ERROR,
            error_code="lean_probe_unavailable",
            file_path=resolved,
            target=theorem_id,
        )

    if epf_action == "prepare_file":
        payload = _probe().prepare_file(
            resolved,
            theorem_id=theorem_id,
            cwd=project_root,
            timeout_s=timeout_s,
        )
    elif epf_action == "check_target":
        payload = _probe().check_target(
            resolved,
            theorem_id=theorem_id,
            cwd=project_root,
            replacement=replacement,
            include_tactics=include_tactics,
            timeout_s=timeout_s,
        )
    elif epf_action == "feedback":
        payload = _probe().feedback(
            resolved,
            theorem_id=theorem_id,
            cwd=project_root,
            replacement=replacement,
            timeout_s=timeout_s,
        )
    else:
        return _error_payload(
            action=epf_action,
            error=f"unsupported lean_incremental_check action: {action}",
            error_code="unsupported_action",
            file_path=resolved,
            target=theorem_id,
        )
    return _normalize_payload(payload, epf_action)
