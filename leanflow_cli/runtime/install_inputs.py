"""Resolve companion source inputs before installing third-party Python packages."""

from __future__ import annotations

import argparse
import ast
import hashlib
import tomllib
from pathlib import Path

from core.home import leanflow_home


def validate_install_paths(source: Path, bin_dir: Path | None, venv_dir: Path | None) -> None:
    """Reject unsupported metadata and Python-build paths before installation changes."""
    home = leanflow_home()
    paths = [source, home, *([bin_dir] if bin_dir else []), *([venv_dir] if venv_dir else [])]
    if any("\n" in str(path) or "\r" in str(path) for path in paths):
        raise RuntimeError("Installation paths cannot contain line breaks.")
    if "$" in str(home) or (venv_dir is not None and "$" in str(venv_dir)):
        raise RuntimeError(
            "Python package builds do not support dollar signs in runtime paths. "
            "Choose --leanflow-home and --venv-dir paths without '$'. "
            "Source and wrapper paths may contain dollar signs."
        )


def resolve_lean_probe_source(source: Path, requested: Path | None = None) -> Path | None:
    """Validate an explicit or adjacent companion without executing its code."""
    candidate = requested if requested is not None else source.resolve().parent / "LeanProbe"
    if requested is None and not candidate.exists():
        return None
    candidate = candidate.expanduser().resolve()
    try:
        project = tomllib.loads((candidate / "pyproject.toml").read_text())["project"]
        if not isinstance(project, dict):
            raise ValueError("project metadata must be a table")
        module = candidate / "src/lean_probe/__init__.py"
        tree = ast.parse(module.read_text())
        revision = next(
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "API_REVISION"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        )
        if project.get("name") != "lean-probe" or type(revision) is not int or revision < 1:
            raise ValueError("the companion does not provide backend API revision 1")
    except (OSError, ValueError, KeyError, SyntaxError, StopIteration) as exc:
        raise RuntimeError(
            f"LeanProbe source at {candidate} is not compatible: {exc}. "
            "Use the matching LeanProbe 0.4.1 development checkout with --lean-probe-source PATH."
        ) from exc
    return candidate


def companion_identity(source: Path) -> str:
    """Hash actual packaged companion inputs so same-version edits create a new runtime."""
    digest = hashlib.sha256()
    files = [source / name for name in ("pyproject.toml", "README.md", "LICENSE")]
    files.extend((source / "src").rglob("*"))
    for path in sorted(files):
        if (
            path.name.endswith((".pyc", ".pyo"))
            or "__pycache__" in path.parts
            or any(part.endswith(".egg-info") for part in path.parts)
        ):
            continue
        if path.is_symlink():
            raise RuntimeError(f"Companion package inputs cannot be symbolic links: {path}")
        if not path.is_file():
            continue
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> None:
    """Print the optional companion path or fail before mutating an installation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--lean-probe-source", type=Path)
    parser.add_argument("--bin-dir", type=Path)
    parser.add_argument("--venv-dir", type=Path)
    arguments = parser.parse_args()
    try:
        validate_install_paths(arguments.source, arguments.bin_dir, arguments.venv_dir)
        companion = resolve_lean_probe_source(arguments.source, arguments.lean_probe_source)
        if companion is None:
            from leanflow_cli.runtime.install_companion import fetch_pinned_lean_probe

            companion = fetch_pinned_lean_probe()
    except RuntimeError as exc:
        parser.exit(1, f"LeanFlow installation refused: {exc}\n")
    print(str(companion) if companion is not None else "")


if __name__ == "__main__":
    main()
