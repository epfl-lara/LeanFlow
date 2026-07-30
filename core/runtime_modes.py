"""Resolve process-scoped runtime resource modes."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_PLANNER_EMPIRICAL_LANE: ContextVar[bool] = ContextVar(
    "leanflow_planner_empirical_lane",
    default=False,
)


def env_flag_enabled(name: str) -> bool:
    """Return whether an environment flag contains a recognized true value."""
    return str(os.getenv(name, "") or "").strip().lower() in _TRUE_VALUES


def low_memory_mode_enabled() -> bool:
    """Return whether heavy optional indexes and warm caches must stay disabled."""
    return env_flag_enabled("LEANFLOW_LOW_MEMORY")


def dispatch_worker_enabled() -> bool:
    """Return whether this process is an isolated background research worker."""
    return env_flag_enabled("LEANFLOW_DISPATCH_WORKER")


def scratch_only_dispatch_worker_enabled() -> bool:
    """Return whether this process is serving a read/check-only research job."""
    return env_flag_enabled("LEANFLOW_DISPATCH_SCRATCH_ONLY")


def empirical_dispatch_worker_enabled() -> bool:
    """Return whether this process owns a scratch-only empirical assignment."""
    archetype = str(os.getenv("LEANFLOW_DISPATCH_ARCHETYPE", "") or "").strip().lower()
    return (
        dispatch_worker_enabled()
        and scratch_only_dispatch_worker_enabled()
        and archetype == "empirical"
    )


def empirical_compute_enabled() -> bool:
    """Return whether the current isolated execution owns empirical compute."""
    return empirical_dispatch_worker_enabled() or _PLANNER_EMPIRICAL_LANE.get()


@contextmanager
def planner_empirical_lane(*, enabled: bool = True) -> Iterator[None]:
    """Grant empirical compute to one synchronous planner child context."""
    token = _PLANNER_EMPIRICAL_LANE.set(bool(enabled))
    try:
        yield
    finally:
        _PLANNER_EMPIRICAL_LANE.reset(token)
