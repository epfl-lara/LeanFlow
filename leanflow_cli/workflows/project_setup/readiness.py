"""Inspect installed setup artifacts and source imports without loading Lean environments."""

from __future__ import annotations

import json
import re
from pathlib import Path

from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings
from leanflow_cli.workflows.project_setup.process import safe_text
from leanflow_cli.workflows.project_setup.scaffold import source_paths
from leanflow_cli.workflows.project_setup.verification import dependency_names, repl_binary


def mathlib_cache_present(root: Path) -> bool:
    """Recognize installed Mathlib entry points without scanning or decompressing its cache."""
    library = root / ".lake/packages/mathlib/.lake/build/lib/lean"
    return all(
        (library / name).is_file() for name in ("Mathlib.olean", "Mathlib/Data/Nat/Basic.olean")
    )


def installed_environment(root: Path) -> bool:
    """Detect an existing environment; protocol verification remains a separate check."""
    if not (root / "lean-toolchain").is_file() or repl_binary(root) is None:
        return False
    try:
        manifest = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("packages"), list):
            return False
        for package in manifest["packages"]:
            if not isinstance(package, dict) or not isinstance(package.get("name"), str):
                return False
            path = (
                root / str(package.get("dir", ""))
                if package.get("type") == "path"
                else root / ".lake/packages" / package["name"]
            )
            if not path.is_dir():
                return False
        return "mathlib" not in dependency_names(root) or mathlib_cache_present(root)
    except (OSError, ValueError, RuntimeError):
        return False


def source_diagnostics(root: Path) -> list[str]:
    """Report unresolved import locations without compiling unfinished user sources."""
    imports: set[str] = set()
    diagnostics: list[str] = []
    for path in source_paths(root):
        try:
            text = _strip_lean_comments_and_strings(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            diagnostics.append(
                f"Cannot inspect imports in {path.relative_to(root)}: {safe_text(str(exc))}"
            )
            continue
        for match in re.finditer(r"^\s*import\s+([^\n]+)", text, re.MULTILINE):
            imports.update(
                name
                for name in match.group(1).split()
                if re.fullmatch(r"[\w.]+", name)
                and name.split(".")[0] not in {"Mathlib", "Init", "Lean", "Std", "Lake"}
            )
    packages = root / ".lake/packages"
    locations = [root, *sorted(packages.iterdir())] if packages.is_dir() else [root]
    for name in sorted(imports):
        module = Path(*name.split("."))
        if not any(
            (location / module.with_suffix(".lean")).is_file()
            or (location / ".lake/build/lib/lean" / module.with_suffix(".olean")).is_file()
            for location in locations
        ):
            diagnostics.append(
                f"Could not locate import {name}; check its source path or dependency."
            )
    return diagnostics
