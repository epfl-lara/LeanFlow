"""Pure environment/config readers for the native managed runner.

Extracted verbatim from ``native_runner.py`` (refactor Phase 2, step 1 — the true-leaf cluster).
These helpers depend only on the standard library and each other, so they live here and are
re-exported from ``native_runner`` for backwards compatibility. They resolve the EPFLemma env
namespace with its legacy ``OPENGAUSS_``/``GAUSS_`` fallbacks in one place.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "_utc_now_isoformat",
    "_read_text_env",
    "_read_native_env",
    "_read_int_env",
    "_managed_home",
    "_project_root",
    "_workflow_kind",
]


def _utc_now_isoformat() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _read_text_env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _read_native_env(name: str, default: str = "") -> str:
    return _read_text_env(f"EPFLEMMA_NATIVE_{name}", default)


def _read_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = _read_text_env(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def _managed_home() -> Path:
    return Path(_read_text_env("EPFLEMMA_HOME", str(Path.home() / ".epflemma"))).expanduser()


def _project_root() -> str:
    return _read_text_env("EPFLEMMA_PROJECT_ROOT", os.getcwd())


def _workflow_kind() -> str:
    return _read_native_env("WORKFLOW_KIND", "workflow").strip().lower()
