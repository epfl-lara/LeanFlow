"""Resolve managed storage limits without changing native runtime settings."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

GIB = 1024**3


@dataclass(frozen=True)
class StoragePolicy:
    """Describe a soft cache target and the free-space admission reserve."""

    cache_target_gib: float = 20
    min_free_gib: float = 10
    automatic_cleanup: bool = True

    def __post_init__(self) -> None:
        for value in (self.cache_target_gib, self.min_free_gib):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("storage limits must be finite nonnegative GiB values")
        if not isinstance(self.automatic_cleanup, bool):
            raise ValueError("storage.automatic_cleanup must be true or false")

    @property
    def cache_target_bytes(self) -> int:
        """Return the managed cache target in bytes."""
        return int(self.cache_target_gib * GIB)

    @property
    def min_free_bytes(self) -> int:
        """Return the admission free-space reserve in bytes."""
        return int(self.min_free_gib * GIB)

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> StoragePolicy:
        """Read explicit storage settings and reject malformed limits."""
        section = config.get("storage", {})
        if not isinstance(section, Mapping):
            raise ValueError("storage configuration must be a mapping")
        values: dict[str, float] = {}
        for name, default in (("cache_target_gib", 20), ("min_free_gib", 10)):
            raw = section.get(name, default)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"storage.{name} must be a nonnegative GiB number")
            values[name] = float(raw)
        automatic = section.get("automatic_cleanup", True)
        if not isinstance(automatic, bool):
            raise ValueError("storage.automatic_cleanup must be true or false")
        return cls(**values, automatic_cleanup=automatic)


def storage_policy(config: Mapping[str, object] | None = None) -> StoragePolicy:
    """Resolve storage limits through the standard LeanFlow configuration."""
    if config is None:
        from leanflow_cli.config import load_config

        config = load_config()
    return StoragePolicy.from_config(config)
