"""Recover once from positively closed capture interruption using a fresh compiler invocation."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from agent.runtime.harness.models import NativeCleanupUnverified
from core.deadlines import operation_cancelled, remaining_seconds
from leanflow_cli.formalization.evidence import digest
from leanflow_cli.workflows.prover.lean_environment import _files


def compiler_version(compiler_library: Path) -> dict[str, tuple[Any, ...]]:
    """Snapshot compiler versions in memory without rereading large immutable libraries."""
    result = {}
    for root in (compiler_library.parent.parent / "bin", compiler_library):
        for path in _files(root, set(), include_sources=True):
            info, target = path.lstat(), path.stat()
            result[str(path)] = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                os.readlink(path) if path.is_symlink() else "",
                target.st_dev,
                target.st_ino,
                target.st_size,
                target.st_mtime_ns,
                target.st_ctime_ns,
                (
                    digest(path.read_bytes())
                    if stat.S_ISREG(target.st_mode) and target.st_size <= 64 * 1024
                    else ""
                ),
            )
    return result


def workspace_version(workspace: Path) -> dict[str, tuple[Any, ...]]:
    """Bind preexisting header workspace entries without treating access-time reads as edits."""
    result = {}
    for path in workspace.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Aliased or nonregular header workspace input")
        info = path.lstat()
        result[str(path.relative_to(workspace))] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            digest(path.read_bytes()) if path.is_file() else "",
        )
    return result


def reset_failed_outputs(workspace: Path, setup: Path, setup_bytes: bytes, output: Path) -> None:
    """Delete only known private regular outputs after verified process and physical EOF closure."""
    known = {
        output,
        Path(str(output) + ".server"),
        Path(str(output) + ".private"),
        output.with_suffix(".ir"),
        Path(str(output.with_suffix(".ir")) + ".sig"),
    }
    directories = {
        parent for path in known for parent in path.parents if parent.is_relative_to(workspace)
    }
    if setup.is_symlink() or setup.read_bytes() != setup_bytes or setup.stat().st_nlink != 1:
        raise ValueError("Compiler modified its controller-owned setup record")
    for path in workspace.rglob("*"):
        allowed = (
            path == setup
            or (path in known and path.is_file() and path.stat().st_nlink == 1)
            or (path in directories and path.is_dir())
        )
        if path.is_symlink() or not allowed:
            raise ValueError("Unexpected failed compiler output; retain inputs")
    for path in known:
        if path.exists():
            path.unlink()


def _safe_interruption(result: dict[str, Any]) -> bool:
    """Require prior normal leader exit and final verified ownership and pipe closure."""
    failure = result.get("capture_failure")
    cleanup = result.get("capture_cleanup")
    return (
        result.get("success") is False
        and result.get("ok") is False
        and result.get("error_code") == "check_capture_failed"
        and result.get("timed_out") is not True
        and isinstance(failure, dict)
        and failure.get("boundary") == "owned_stop"
        and failure.get("leader_exited_before_cleanup") is True
        and type(failure.get("leader_returncode")) is int
        and failure["leader_returncode"] == 0
        and isinstance(cleanup, dict)
        and all(
            cleanup.get(name) is True
            for name in ("closed", "processes_verified", "output_eof_verified")
        )
    )


def _attempt(result: dict[str, Any]) -> dict[str, Any]:
    """Keep bounded failure provenance while never reusing captured compiler output."""
    fields = (
        "success",
        "ok",
        "error_code",
        "returncode",
        "timed_out",
        "capture_failure",
        "capture_cleanup",
        "stdout_truncated",
        "stderr_truncated",
    )
    return {
        **{name: result[name] for name in fields if name in result},
        **{
            name: redact_sensitive_text(str(result[name]), force=True)[:2048]
            for name in ("error", "stderr")
            if result.get(name)
        },
    }


class BuildCapture:
    """Spend at most one fresh capture retry across all commands in one project build."""

    def __init__(
        self,
        *,
        deadline: float | None,
        cancelled: Callable[[], bool] | None,
        verify_inputs: Callable[[], None],
    ) -> None:
        """Keep the original deadline, cancellation and frozen-input guard for both attempts."""
        self.deadline = deadline
        self.cancelled = cancelled
        self.verify_inputs = verify_inputs
        self.recovery: list[dict[str, Any]] = []

    def _stop_reason(self) -> str:
        """Consult the unchanged outer cancellation policy and original monotonic deadline."""
        if operation_cancelled() or (self.cancelled is not None and self.cancelled()):
            return "cancelled"
        remaining = remaining_seconds(self.deadline)
        return "deadline_expired" if remaining is not None and remaining <= 0 else ""

    def run(
        self,
        command: Callable[..., dict[str, Any]],
        *,
        reset_outputs: Callable[[], None],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Relaunch only an eligible, fully closed failed capture with unchanged inputs."""
        result = command(
            **kwargs, timeout_s=remaining_seconds(self.deadline), cancelled=self.cancelled
        )
        if self.recovery or not _safe_interruption(result):
            return result
        record: dict[str, Any] = {"first_attempt": _attempt(result), "fresh_attempt": None}
        self.recovery.append(record)
        reason = self._stop_reason()
        if reason:
            record["retry_skipped"] = reason
            return result
        try:
            self.verify_inputs()
            reset_outputs()
            self.verify_inputs()
        except (OSError, ValueError) as error:
            record["retry_skipped"] = "inputs_or_outputs_changed"
            record["guard_error"] = redact_sensitive_text(str(error), force=True)[:2048]
            return result
        reason = self._stop_reason()
        if reason:
            record["retry_skipped"] = reason
            return result
        try:
            fresh = command(
                **kwargs, timeout_s=remaining_seconds(self.deadline), cancelled=self.cancelled
            )
        except NativeCleanupUnverified as error:
            record["fresh_attempt"] = {
                "success": False,
                "cleanup_unverified": True,
                "error": redact_sensitive_text(str(error), force=True)[:2048],
            }
            error.add_note("Capture recovery diagnostic only: " + json.dumps(self.recovery))
            raise
        record["fresh_attempt"] = _attempt(fresh)
        try:
            self.verify_inputs()
        except (OSError, ValueError) as error:
            record["guard_error"] = redact_sensitive_text(str(error), force=True)[:2048]
            return {
                "success": False,
                "ok": False,
                "error_code": "check_inputs_changed",
                "error": "Pinned compilation inputs changed during capture recovery",
            }
        reason = self._stop_reason()
        if reason:
            record["post_capture_stop"] = reason
            return {
                "success": False,
                "ok": False,
                "error_code": "check_cancelled" if reason == "cancelled" else "check_timeout",
                "error": "The original compilation operation stopped during capture recovery",
                "timed_out": reason == "deadline_expired",
            }
        return fresh

    def result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Attach both attempt outcomes without promoting the interrupted first attempt."""
        return {**result, "capture_recovery": self.recovery} if self.recovery else result
