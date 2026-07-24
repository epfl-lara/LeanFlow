"""Resolve process-scoped runtime resource modes."""

from __future__ import annotations

import os

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


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
