"""Define the native harness invocation and result contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .isolation import IsolationPolicy


class BackendError(RuntimeError):
    """Reject an invalid or unavailable native backend before invocation."""


class NativeCleanupUnverified(BackendError):
    """Retain invocation inputs when owned process closure cannot be established."""

    def __init__(self, message: str, *, result: HarnessResult | None = None) -> None:
        """Carry observed native outcome and usage without turning it into cleanup evidence."""
        super().__init__(message)
        self.result = result


class NativeLaunchFailure(BackendError):
    """Report a verified failure before the author process was launched."""

    def __init__(
        self, message: str, *, status: str = "startup_error", session_id: str = ""
    ) -> None:
        """Expose explicit no-launch evidence after every owned helper has closed."""
        if status not in {"startup_error", "cancelled", "deadline_exceeded"}:
            raise ValueError("A verified launch failure requires a prelaunch failure status")
        super().__init__(message)
        self.result = HarnessResult(
            status, session_id=session_id, error=message, cleanup_verified=True
        )


@dataclass(frozen=True)
class HarnessRequest:
    """Describe one native invocation without replacing runtime configuration."""

    backend: str
    prompt: str
    cwd: Path
    model: str = ""
    effort: str = ""
    session_id: str = ""
    env: Mapping[str, str] | None = None
    deadline: float | None = None
    cancelled: Callable[[], bool] | None = None
    mcp_config: Path | None = None
    log_path: Path | None = None
    previous_usage: Mapping[str, Any] | None = None
    tools: bool = True
    restricted_tools: bool = False
    executable: str = ""
    isolation: IsolationPolicy | None = None
    on_started: Callable[[], None] | None = None
    on_admitting: Callable[[], None] | None = None


@dataclass(frozen=True)
class HarnessResult:
    """Return native outcome and reported usage with independently verified cleanup."""

    status: str
    final_response: str = ""
    session_id: str = ""
    usage: dict[str, int] | None = None
    usage_total: dict[str, int] | None = None
    returncode: int | None = None
    error: str = ""
    diagnostics: tuple[str, ...] = field(default_factory=tuple)
    model_settings: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    cleanup_verified: bool = False
