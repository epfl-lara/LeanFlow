"""Reject ambiguous first-root module lookup across compiler and compiled libraries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained


def output_paths(root: Path, layout: dict[str, Any], packages: list[Path]) -> tuple[Path, Path]:
    """Reject output/input overlap in either direction before making anything writable."""
    build = contained(root, layout["build_dir"])
    library = contained(root, layout["lean_lib_dir"])
    if (
        build == root
        or not library.is_relative_to(build)
        or any(
            output.is_relative_to(package) or package.is_relative_to(output)
            for output in (build, library)
            for package in packages
        )
        or any(
            part in {".git", ".leanflow", ".codex", ".claude"}
            for part in build.relative_to(root).parts
        )
    ):
        raise ValueError(
            "Private Lean outputs must stay within a build directory disjoint from dependencies"
        )
    return build, library


def module_roots(library: Path) -> set[str]:
    """Match Lean's root selection, including empty root directories that capture imports."""
    return {
        path.name if path.is_dir() else path.stem
        for path in library.iterdir()
        if path.is_dir() or path.suffix == ".olean"
    }


def validate_library_roots(libraries: list[Path], compiler: Path) -> None:
    """Require disjoint top-level mathematical namespaces before any module lookup."""
    owners = dict.fromkeys(module_roots(compiler), "pinned compiler")
    for library in libraries:
        for name in sorted(module_roots(library)):
            if name in owners:
                raise ValueError(
                    f"Ambiguous module root {name}: {library} conflicts with {owners[name]}; "
                    "Lean resolves whole module roots from the first matching directory"
                )
            owners[name] = str(library)
