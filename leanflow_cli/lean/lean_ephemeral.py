"""Run exact-project Lean checks against ephemeral full-source harnesses."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import IO, Any

from core.project_resource_admission import (
    ProjectLeanAdmission,
    ProjectLeanAdmissionRetained,
    project_lean_heavy_admission,
)
from leanflow_cli.lean.lean_diagnostics import diagnostic_items

_OUTPUT_LIMIT_BYTES = 1024 * 1024
_OUTPUT_EDGE_BYTES = 448 * 1024
_AXIOM_OUTPUT_LIMIT_BYTES = 128 * 1024
_RETRYABLE_PROJECT_OUTPUT = (
    "unknown module prefix",
    "object file",
    ".olean does not exist",
    "no such file or directory",
    "missing lake manifest",
    "unknown package",
    "failed to load module",
)


def _outside_project_temp_dir(project_root: Path) -> Path | None:
    """Return a writable system-temp directory outside the project tree."""
    candidates = [
        Path(tempfile.gettempdir()),
        Path("/var/tmp"),
        Path.home() / "Library" / "Caches",
        Path.home() / ".cache",
    ]
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            resolved.relative_to(project_root)
        except ValueError:
            if resolved.is_dir() and os.access(resolved, os.W_OK):
                return resolved
        except OSError:
            continue
    return None


def _stop_process_group(process: subprocess.Popen[Any]) -> None:
    """Terminate one isolated Lean process group, escalating after a short wait."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        with contextlib.suppress(OSError):
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.communicate(timeout=1)


def _bounded_output(handle: IO[bytes]) -> tuple[str, bool]:
    """Return bounded head/tail output while retaining axiom-profile lines."""
    handle.flush()
    size = handle.seek(0, os.SEEK_END)
    handle.seek(0)
    if size <= _OUTPUT_LIMIT_BYTES:
        return handle.read().decode("utf-8", errors="replace"), False

    head = handle.read(_OUTPUT_EDGE_BYTES)
    handle.seek(max(0, size - _OUTPUT_EDGE_BYTES))
    tail = handle.read(_OUTPUT_EDGE_BYTES)
    axiom_lines: list[bytes] = []
    axiom_bytes = 0
    handle.seek(0)
    for line in handle:
        if b"depends on axioms" not in line and b"does not depend on any axioms" not in line:
            continue
        bounded = line[:65536]
        if axiom_bytes + len(bounded) > _AXIOM_OUTPUT_LIMIT_BYTES:
            break
        axiom_lines.append(bounded)
        axiom_bytes += len(bounded)
    marker = b"\n[leanflow: exact-project output truncated]\n"
    retained = b"".join(axiom_lines)
    combined = head + marker + retained + marker + tail
    return combined.decode("utf-8", errors="replace"), True


def _retryable_project_failure(output: str) -> bool:
    """Return whether diagnostics show an unavailable project import environment."""
    normalized = str(output or "").lower()
    return any(pattern in normalized for pattern in _RETRYABLE_PROJECT_OUTPUT)


def _concise_failure_error(output: str) -> str:
    """Return the first Lean error instead of an earlier warning prefix."""
    for item in diagnostic_items(output):
        if str(item.get("severity", "") or "").strip().lower() != "error":
            continue
        message = str(item.get("message", "") or "").strip()
        line = item.get("line")
        if message and isinstance(line, int):
            return f"Lean error at line {line}: {message}"[:2000]
        if message:
            return message[:2000]
    return str(output or "").strip()[:500]


def _reclaim_incremental_before_exact_check(admission: ProjectLeanAdmission) -> bool:
    """Close this process's LeanProbe before starting one-shot Lean."""
    from leanflow_cli.lean.lean_incremental import close_incremental_sessions

    reclaimed = close_incremental_sessions()
    if not reclaimed:
        admission.retain_until_process_exit(
            "owned LeanProbe session close failed before ephemeral Lean verification"
        )
    return reclaimed


def _admission_failure(
    error: str,
    *,
    resource_admission: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Return a retryable failure when exact Lean cannot own the project slot."""
    return {
        "success": False,
        "ok": False,
        "timed_out": False,
        "retryable": True,
        "failure_kind": "resource_admission_retained",
        "error": str(error or "Lean resource admission unavailable")[:500],
        "output": "",
        "messages": [],
        "resource_admission": dict(resource_admission or {}),
    }


def lean_ephemeral_source_check(
    source: str,
    *,
    cwd: str | Path,
    timeout_s: int = 120,
) -> dict[str, Any]:
    """Elaborate a full source copy with the project's exact Lake environment.

    Place the copy in a system-temp directory outside the project so the check
    cannot create, replace, or leave a Lean source artifact in the authoritative
    tree.  The child owns an isolated process group so timeouts reap Lake and all
    descendants before the harness is removed.
    """
    project_root = Path(cwd).expanduser().resolve()
    temp_dir = _outside_project_temp_dir(project_root)
    if not project_root.is_dir() or temp_dir is None:
        return {
            "success": False,
            "ok": False,
            "timed_out": False,
            "retryable": True,
            "failure_kind": "infrastructure_unavailable",
            "error": "no writable system-temp directory exists outside the Lean project",
            "output": "",
            "messages": [],
        }

    temp_path: Path | None = None
    process: subprocess.Popen[Any] | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".lean",
            prefix="leanflow-source-check-",
            dir=temp_dir,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(source)
        command = ["lake", "env", "lean", str(temp_path)]
        with tempfile.TemporaryFile(mode="w+b", dir=temp_dir) as output_handle:
            try:
                with project_lean_heavy_admission(project_root) as admission:
                    reclaimed = _reclaim_incremental_before_exact_check(admission)
                    if not reclaimed:
                        admission_payload = admission.to_dict()
                        return _admission_failure(
                            str(admission_payload.get("retention_reason", "") or ""),
                            resource_admission={
                                **admission_payload,
                                "incremental_session_reclaimed": False,
                            },
                        )
                    process = subprocess.Popen(
                        command,
                        cwd=str(project_root),
                        stdout=output_handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=os.name != "nt",
                    )
                    try:
                        process.wait(timeout=max(1, int(timeout_s)))
                    except subprocess.TimeoutExpired:
                        _stop_process_group(process)
                        partial, truncated = _bounded_output(output_handle)
                        return {
                            "success": False,
                            "ok": False,
                            "timed_out": True,
                            "retryable": True,
                            "failure_kind": "infrastructure_timeout",
                            "returncode": 124,
                            "command": command,
                            "error": "exact-project Lean source check timed out",
                            "output": partial,
                            "output_truncated": truncated,
                            "messages": [],
                            "resource_admission": {
                                **admission.to_dict(),
                                "incremental_session_reclaimed": reclaimed,
                            },
                        }
                    output, truncated = _bounded_output(output_handle)
                    resource_admission = {
                        **admission.to_dict(),
                        "incremental_session_reclaimed": reclaimed,
                    }
            except ProjectLeanAdmissionRetained as exc:
                return _admission_failure(
                    str(exc),
                    resource_admission={
                        "project_root": exc.project_root,
                        "retained_until_process_exit": True,
                        "retention_reason": exc.reason,
                    },
                )
        returncode = int(process.returncode or 0)
        retryable = returncode != 0 and _retryable_project_failure(output)
        return {
            "success": returncode == 0,
            "ok": returncode == 0,
            "timed_out": False,
            "retryable": retryable,
            "failure_kind": (
                "project_environment_unavailable"
                if retryable
                else ("lean_elaboration" if returncode else "")
            ),
            "returncode": returncode,
            "command": command,
            "error": "" if returncode == 0 else _concise_failure_error(output),
            "output": output,
            "output_truncated": truncated,
            "messages": [],
            "resource_admission": resource_admission,
        }
    except (OSError, ValueError) as exc:
        if process is not None:
            _stop_process_group(process)
        return {
            "success": False,
            "ok": False,
            "timed_out": False,
            "retryable": True,
            "failure_kind": "infrastructure_unavailable",
            "error": str(exc)[:500],
            "output": "",
            "messages": [],
        }
    finally:
        # Parent interruption bypasses the ordinary timeout and exception
        # handlers. Reap the isolated group before deleting its source harness
        # so Lake/Lean cannot survive native-runner shutdown as orphaned work.
        if process is not None and process.returncode is None:
            _stop_process_group(process)
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
