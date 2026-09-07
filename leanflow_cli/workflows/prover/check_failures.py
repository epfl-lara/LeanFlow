"""Distinguish infrastructure diagnostics from mathematical rejection in nested Lean reports."""

from __future__ import annotations

import re
from typing import Any

_LAKE_CONFIG_LOCK = re.compile(r"[\\/]\.lake[\\/](?:config[\\/]\d+[\\/])?lakefile\.olean\.lock")


def infrastructure_code(result: Any) -> str:
    """Return a known infrastructure code, including nested compile and inspector failures."""
    if not isinstance(result, dict):
        return ""
    code = result.get("error_code")
    if code in {
        "isolation_unavailable",
        "check_setup_failed",
        "lean_interact_start_failed",
        "lean_probe_unavailable",
        "local_repl_missing",
        "check_timeout",
        "check_busy",
        "check_cancelled",
        "lake_config_cache_error",
    }:
        return str(code)
    if result.get("timed_out") is True:
        return "check_timeout"
    diagnostic = "\n".join(
        value for name in ("error", "stderr") if isinstance(value := result.get(name), str)
    )
    if _LAKE_CONFIG_LOCK.search(diagnostic) and any(
        marker in diagnostic.lower() for marker in ("operation not permitted", "permission denied")
    ):
        return "lake_config_cache_error"
    for name in ("compile", "inspect", "kernel_profile"):
        found = infrastructure_code(result.get(name))
        if found:
            return found
    return ""
