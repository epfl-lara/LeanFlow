"""Read ordinary storage settings before a fresh interpreter has YAML installed."""

from __future__ import annotations

import re
from pathlib import Path

from core.home import leanflow_home
from leanflow_cli.runtime.storage.policy import StoragePolicy


def bootstrap_storage_policy(path: Path | None = None) -> StoragePolicy:
    """Honor scalar block settings, refusing ambiguous YAML rather than ignoring it."""
    path = path or leanflow_home() / "config.yaml"
    if not path.exists():
        return StoragePolicy()
    values: dict[str, object] = {}
    section = False
    seen = False
    indentation: int | None = None
    for line in path.read_text().splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        header = re.match(r"^(?:storage|'storage'|\"storage\")\s*:(.*)$", line)
        if header:
            if seen:
                raise ValueError("Duplicate storage configuration")
            if header[1].split("#", 1)[0].strip():
                raise ValueError("Use a scalar block mapping for bootstrap storage configuration")
            section = True
            seen = True
            continue
        if not section:
            continue
        if not line[0].isspace():
            section = False
            continue
        width = len(line) - len(line.lstrip())
        indentation = indentation if indentation is not None else width
        if width != indentation:
            raise ValueError("Nested bootstrap storage configuration is unsupported")
        name, separator, raw = text.partition(":")
        if not separator:
            raise ValueError("Invalid bootstrap storage configuration")
        raw = raw.split("#", 1)[0].strip()
        if name in {"cache_target_gib", "min_free_gib"}:
            if name in values:
                raise ValueError(f"Duplicate storage.{name}")
            try:
                values[name] = float(raw)
            except ValueError as exc:
                raise ValueError(f"storage.{name} must be a nonnegative GiB number") from exc
        elif name == "automatic_cleanup":
            if raw.lower() not in {"true", "false"}:
                raise ValueError("storage.automatic_cleanup must be true or false")
            values[name] = raw.lower() == "true"
    return StoragePolicy.from_config({"storage": values})
