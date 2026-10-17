"""Describe interrupted command capture without turning eventual cleanup into acceptance."""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from agent.runtime.harness.models import NativeCleanupUnverified


class CaptureInterruption(NativeCleanupUnverified):
    """Identify only controller-owned capture stop and physical EOF boundaries."""

    def __init__(self, boundary: str, message: str) -> None:
        """Keep the original exception as the explicit cause of an interrupted capture."""
        super().__init__(message)
        self.boundary = boundary


def capture_failure(error: NativeCleanupUnverified) -> dict[str, Any]:
    """Retain a bounded, forcibly redacted cause chain without locals or source text."""
    causes: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    remaining = 8192
    while current is not None and len(causes) < 4 and id(current) not in seen:
        seen.add(id(current))
        message = (
            redact_sensitive_text(str(current), force=True).encode("utf-8", errors="replace")[
                : min(2048, remaining)
            ]
        ).decode("utf-8", errors="ignore")
        remaining -= len(message.encode("utf-8"))
        causes.append(
            {
                "type": type(current).__name__[:128],
                "message": message,
                "frames": [
                    {
                        "file": redact_sensitive_text(Path(frame.filename).name, force=True)[:128],
                        "function": redact_sensitive_text(frame.name, force=True)[:128],
                        "line": frame.lineno,
                    }
                    for frame in traceback.extract_tb(current.__traceback__)[-8:]
                ],
            }
        )
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return {
        "boundary": error.boundary if isinstance(error, CaptureInterruption) else "unknown",
        "leader_exited_before_cleanup": isinstance(error, CaptureInterruption),
        "causes": causes,
        "chain_truncated": current is not None,
    }


def interrupted_capture_note(failure: dict[str, Any]) -> str:
    """Preserve bounded original capture diagnostics when the final ownership close raises."""
    encoded = redact_sensitive_text(json.dumps(failure, ensure_ascii=True), force=True)
    return "Interrupted command capture diagnostic only: " + encoded[:65536]
