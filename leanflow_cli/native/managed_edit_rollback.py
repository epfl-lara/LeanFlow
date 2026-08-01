"""Classify hard Lean edit failures and restore their exact source image."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


def check_has_hard_errors(
    manager_check: Mapping[str, Any] | None,
    *,
    timed_out: Callable[[Mapping[str, Any] | None], bool],
) -> bool:
    """Return whether one completed gate contains concrete Lean errors."""
    checked = dict(manager_check or {})
    if not checked or timed_out(checked):
        return False
    nested = checked.get("incremental")
    payloads = [checked, dict(nested) if isinstance(nested, Mapping) else {}]
    for payload in payloads:
        if payload.get("has_errors") is True:
            return True
        for key in ("errors", "error_count"):
            value = payload.get(key)
            if isinstance(value, int) and value > 0:
                return True
            if isinstance(value, str) and value.isdigit() and int(value) > 0:
                return True
        messages = payload.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            for message in messages:
                if (
                    isinstance(message, Mapping)
                    and str(message.get("severity", "") or "").lower() == "error"
                ):
                    return True
    return False


def restore_exact_after_image(
    active_file: str,
    *,
    before_text: str,
    expected_after_sha256: str,
) -> bool:
    """Atomically restore captured text only while its exact after-image is current."""
    if not active_file or not before_text or len(expected_after_sha256) != 64:
        return False
    path = Path(active_file)
    try:
        current = path.read_text(encoding="utf-8")
        if hashlib.sha256(current.encode("utf-8")).hexdigest() != expected_after_sha256:
            return False
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.managed-rollback-",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(before_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        return path.read_text(encoding="utf-8") == before_text
    except OSError:
        return False
