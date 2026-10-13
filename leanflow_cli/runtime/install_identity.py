"""Fingerprint installed source and companion inputs for immutable runtime reuse."""

from __future__ import annotations

import hashlib
import os
import platform
import sys
from pathlib import Path

_SOURCE_DIRS = (
    "agent",
    "core",
    "leanflow_cli",
    "tools",
    "leanflow_skills",
    "leanflow_specs",
    "scripts",
)
_SOURCE_FILES = (
    "pyproject.toml",
    "README.md",
    "leanflow_agent.py",
    "run_agent.py",
    "model_tools.py",
    "toolsets.py",
    "utils.py",
)
_EXCLUDED = {"__pycache__", ".lake", ".venv", "node_modules", ".git"}


def source_identity(
    source: Path, *, mode: str = "editable", lean_probe_source: Path | None = None
) -> str:
    """Hash installed source inputs without depending on Git or copying caches."""
    source = source.resolve()
    digest = hashlib.sha256()
    digest.update(
        f"v1:{sys.version_info[:2]}:{platform.system()}:{platform.machine()}:{mode}".encode()
    )
    files = [source / name for name in _SOURCE_FILES if (source / name).is_file()]
    for name in _SOURCE_DIRS:
        directory = source / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        for current, directories, names in os.walk(directory, followlinks=False):
            directories[:] = sorted(
                item
                for item in directories
                if item not in _EXCLUDED and not (Path(current) / item).is_symlink()
            )
            files.extend(
                Path(current) / item
                for item in names
                if not item.endswith((".pyc", ".pyo")) and not (Path(current) / item).is_symlink()
            )
    for path in sorted(files):
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        digest.update(b"\0")
    if lean_probe_source is not None:
        from leanflow_cli.runtime.install_inputs import companion_identity

        digest.update(b"lean-probe-source\0")
        digest.update(companion_identity(lean_probe_source.resolve()).encode())
    return digest.hexdigest()[:24]
