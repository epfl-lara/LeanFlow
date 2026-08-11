"""Order capacity-limited foreground tool work by verification authority."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator, Mapping
from typing import Any


def foreground_tool_priority(function_name: str, arguments: Mapping[str, Any] | None) -> int:
    """Return a lower scheduling rank for authoritative verification work."""
    name = str(function_name or "")
    args = dict(arguments or {})
    action = str(args.get("action", "") or "").strip().lower().replace("-", "_")
    # If a model bundles an edit and its verification, the edit must become
    # visible before any check observes the source.  The common post-edit batch
    # still places the authoritative target check ahead of diagnostic inspect.
    if name == "apply_verified_patch":
        return -10
    if name == "lean_incremental_check":
        return 0 if action in {"", "check_target"} else 1
    if name in {"lean_verify", "lean_axioms"}:
        return 2
    if name == "lean_inspect":
        return 20
    return 10


class OrderedCapacityGate:
    """Admit known jobs by priority while enforcing a bounded capacity."""

    def __init__(self, capacity: int, priorities: Mapping[int, tuple[int, int]]) -> None:
        self._capacity = max(1, int(capacity))
        self._priorities = dict(priorities)
        self._pending = set(self._priorities)
        self._active = 0
        self._condition = threading.Condition()

    @contextlib.contextmanager
    def admit(self, index: int) -> Iterator[None]:
        """Wait until this indexed job is among the highest-priority capacity."""
        priority = self._priorities[index]
        with self._condition:
            while self._active >= self._capacity or any(
                self._priorities[pending] < priority
                for pending in self._pending
                if pending != index
            ):
                self._condition.wait()
            self._pending.discard(index)
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active = max(0, self._active - 1)
                self._condition.notify_all()
