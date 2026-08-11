"""Publish verified companion sources as importable Lean modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_services import lean_verify


def materialize_verified_support_module(
    file_path: str,
    *,
    project_root: str,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Build a verified support source so importing modules see its current declarations."""
    path = Path(file_path).expanduser()
    result = lean_verify(
        target=str(path),
        cwd=project_root,
        mode="module",
        timeout_s=max(1.0, float(timeout_s)),
    )
    return result.to_dict()
