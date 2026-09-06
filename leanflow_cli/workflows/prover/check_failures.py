"""Distinguish infrastructure diagnostics from mathematical rejection in nested Lean reports."""

from __future__ import annotations

from typing import Any


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
    }:
        return str(code)
    if result.get("timed_out") is True:
        return "check_timeout"
    for name in ("compile", "inspect", "kernel_profile"):
        found = infrastructure_code(result.get(name))
        if found:
            return found
    return ""
