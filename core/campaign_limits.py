"""Persist optional campaign limits and charge native invocations exactly once."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write


class LimitReached(RuntimeError):
    """Prevent new work after an explicit global limit or unknown capped usage."""


def optional_limit(value: Any, *, integer: bool = False) -> int | float | None:
    """Validate a nonnegative limit without conflating zero, false and unlimited."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("A campaign limit cannot be a boolean")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (integer and not number.is_integer()):
        raise ValueError("A campaign limit must be finite and nonnegative")
    return int(number) if integer else number


class CampaignLimits:
    """Own active wall time, invocation reservations and reported token accounting."""

    def __init__(self, path: Path, limits: Mapping[str, Any]) -> None:
        import json

        self.path = path
        self.lock = threading.RLock()
        self.limits = {
            name: optional_limit(limits.get(name), integer=name != "wall_time_s")
            for name in ("agent_calls", "tokens", "wall_time_s")
        }
        if path.exists():
            self.state = json.loads(path.read_text())
            if self.state.get("version") != 2 or self.state.get("limits") != self.limits:
                raise ValueError("Start a new campaign when changing limits or migrating old runs")
        else:
            self.state = {"version": 2, "limits": self.limits, "elapsed_s": 0.0, "invocations": {}}
        # A restarted controller cannot prove that a reserved process never ran.
        # Keep its call charge and unknown usage rather than silently refunding it.
        for call in self.state["invocations"].values():
            if call["status"] in {"reserved", "running"}:
                call.update(status="finished", usage=None, interrupted=True)
        self.elapsed_before = float(self.state["elapsed_s"])
        self.started = time.monotonic()
        self.persist()

    def elapsed(self) -> float:
        """Return cumulative active time; parallel work shares the same clock."""
        return self.elapsed_before + time.monotonic() - self.started

    def remaining(self) -> float | None:
        """Return the sole execution time limit, if the user configured one."""
        limit = self.limits["wall_time_s"]
        return None if limit is None else max(0.0, limit - self.elapsed())

    def persist(self) -> None:
        """Checkpoint elapsed time even while an invocation emits no events."""
        with self.lock:
            self.state["elapsed_s"] = self.elapsed()
            atomic_json_write(self.path, self.state)

    def reserve(self) -> str:
        """Reserve one invocation before dispatch, including concurrent reservations."""
        with self.lock:
            remaining = self.remaining()
            if remaining is not None and remaining <= 0:
                raise LimitReached("Global campaign time limit reached")
            calls = list(self.state["invocations"].values())
            charged = [call for call in calls if call["status"] != "not_started"]
            ceiling = self.limits["agent_calls"]
            if ceiling is not None and len(charged) >= ceiling:
                raise LimitReached("Global agent-call limit reached")
            tokens = self.limits["tokens"]
            if tokens is not None:
                if any(
                    call["status"] == "finished" and call.get("usage") is None for call in charged
                ):
                    raise LimitReached(
                        "Token usage is unavailable; cannot admit another capped call"
                    )
                used = sum(
                    sum(
                        (call.get("usage") or {}).get(k, 0)
                        for k in ("input_tokens", "output_tokens")
                    )
                    for call in charged
                )
                if used >= tokens:
                    raise LimitReached("Global reported-token limit reached")
            invocation = uuid.uuid4().hex
            self.state["invocations"][invocation] = {"status": "reserved", "usage": None}
            self.persist()
            return invocation

    def started_call(self, invocation: str) -> None:
        """Record successful process creation, leaving internal requests to the harness."""
        with self.lock:
            self.state["invocations"][invocation]["status"] = "running"
            self.persist()

    def finish(self, invocation: str, usage: Mapping[str, int] | None, *, started: bool) -> None:
        """Store one terminal usage delta idempotently; missing usage remains unknown."""
        with self.lock:
            call = self.state["invocations"][invocation]
            if call["status"] in {"finished", "not_started"}:
                return
            call.update(
                status="finished" if started else "not_started",
                usage=dict(usage) if usage is not None else None,
            )
            self.persist()
