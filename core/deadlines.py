"""Represent optional campaign deadlines without sentinel values or minimum extensions."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CANCELLED: ContextVar[Callable[[], bool] | None] = ContextVar("operation_cancelled", default=None)


@contextmanager
def operation_cancellation(callback: Callable[[], bool]) -> Iterator[None]:
    """Bind cancellation to a controller operation without sharing mutable global state."""
    token = _CANCELLED.set(callback)
    try:
        yield
    finally:
        _CANCELLED.reset(token)


def operation_cancelled() -> bool:
    """Return whether the controller cancelled the operation on this execution context."""
    callback = _CANCELLED.get()
    return callback is not None and callback()


def deadline_after(seconds: float | None) -> float | None:
    """Return an absolute monotonic deadline; None means no deadline, zero means expired."""
    if seconds is None:
        return None
    if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("A time limit must be finite and nonnegative, or unset")
    return time.monotonic() + seconds


def remaining_seconds(deadline: float | None) -> float | None:
    """Return remaining campaign time without granting an expired operation extra time."""
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def cap_seconds(*limits: float | None) -> float | None:
    """Choose the earliest configured limit while retaining an unlimited result."""
    finite = [value for value in limits if value is not None]
    return min(finite) if finite else None


def wait_slice(deadline: float | None, maximum: float = 0.2) -> float:
    """Bound a polling wait, independently of whether execution has a time limit."""
    remaining = remaining_seconds(deadline)
    return maximum if remaining is None else min(maximum, remaining)
