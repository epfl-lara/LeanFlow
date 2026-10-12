"""Create reproducible Mathlib projects without overwriting user source files."""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.project import find_lean_project_root
from leanflow_cli.workflows.project_setup.process import SetupFailure

DEFAULT_TOOLCHAIN = "leanprover/lean4:v4.33.1"
MATHLIB_REVISION = "0df444a360eaa60ab8c11dca51a86af692955474"
REPL_REVISION = "bbeedf38e0898869fc3b7c009e1ea877b46204e4"
REPL_URL = "https://github.com/leanprover-community/repl"


def project_root(path: str | Path) -> Path:
    """Resolve nested invocations to the nearest existing Lake project."""
    target = Path(path).expanduser().resolve()
    if target.is_file():
        target = target.parent
    return find_lean_project_root(target) or target


def source_paths(root: Path) -> list[Path]:
    """List user Lean inputs while excluding dependency and generated state."""
    sources: list[Path] = []
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if not name.startswith(".")]
        sources.extend(
            Path(current) / name
            for name in files
            if name.endswith(".lean") and name != "lakefile.lean"
        )
    return sorted(sources)


def read_toolchain(root: Path) -> str:
    """Read an existing toolchain without guessing a replacement."""
    path = root / "lean-toolchain"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def validate_lakefile(root: Path) -> dict[str, Any] | None:
    """Parse declarative Lake configuration; leave executable Lake files untouched."""
    path = root / "lakefile.toml"
    if not path.is_file():
        return None
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise SetupFailure(
            "invalid_lakefile",
            f"Cannot read lakefile.toml: {exc}",
            "Correct lakefile.toml, then retry setup.",
        ) from exc
    if not isinstance(value.get("name"), str) or not value["name"].strip():
        raise SetupFailure(
            "invalid_lakefile",
            "lakefile.toml has no package name.",
            'Add a nonempty name = "..." setting, then retry.',
        )
    return value


def validate_mutable_paths(root: Path) -> None:
    """Reject shared mutable setup paths so another project's files cannot change."""
    paths = [
        root / name
        for name in (
            "lean-toolchain",
            "lakefile.toml",
            "lakefile.lean",
            "lake-manifest.json",
            ".lake",
            ".lake/packages",
            ".lake/build",
            ".leanflow",
            ".leanflow/project.yaml",
        )
    ]
    packages = root / ".lake/packages"
    if packages.is_dir() and not packages.is_symlink():
        for package in packages.iterdir():
            paths.extend(
                package / part
                for part in (
                    ".",
                    ".lake",
                    ".lake/build",
                    ".lake/build/bin",
                    ".lake/build/lib",
                    ".lake/build/lib/lean",
                    "lean-toolchain",
                    "lake-manifest.json",
                )
            )
    for path in paths:
        if path.is_symlink():
            raise SetupFailure(
                "shared_setup_path",
                f"Setup cannot safely modify the symlinked path {path.relative_to(root)}.",
                "Use independent project configuration and dependency build directories, then retry setup.",
            )


def scaffold(
    root: Path, *, name: str | None, preset: str, source_file: str | Path | None, changed: list[str]
) -> None:
    """Add only missing project files and optionally copy one loose Lean file."""
    if preset not in {"mathlib", "lean"}:
        raise SetupFailure(
            "invalid_preset", f"Unknown project preset: {preset}", "Choose mathlib or lean."
        )
    source = Path(source_file).expanduser().resolve() if source_file else None
    if source is not None and (not source.is_file() or source.suffix != ".lean"):
        raise SetupFailure(
            "invalid_source",
            "The selected source is not a readable Lean file.",
            "Choose an existing .lean file and retry.",
        )
    if source is not None:
        destination = root / source.name
        if destination.exists() and destination.resolve() != source:
            if destination.read_bytes() != source.read_bytes():
                raise SetupFailure(
                    "source_conflict",
                    f"{destination.name} already exists with different contents.",
                    "Choose an empty destination folder or rename the file, then retry.",
                )
    existing = (root / "lakefile.toml").is_file() or (root / "lakefile.lean").is_file()
    toolchain = read_toolchain(root)
    if (root / "lean-toolchain").exists() and not toolchain:
        raise SetupFailure(
            "toolchain_empty",
            "lean-toolchain is empty.",
            "Set its Lean toolchain version, then retry setup.",
        )
    if not existing and toolchain and preset == "mathlib" and toolchain != DEFAULT_TOOLCHAIN:
        raise SetupFailure(
            "toolchain_needs_dependencies",
            "This folder already selects a different Lean toolchain.",
            "Add a Lake configuration with Mathlib compatible with that toolchain, or choose an empty folder.",
        )
    root.mkdir(parents=True, exist_ok=True)
    if source is not None and not (root / source.name).exists():
        shutil.copyfile(source, root / source.name)
        changed.append(source.name)
    if existing:
        validate_lakefile(root)
        return
    package = re.sub(r"[^A-Za-z0-9_]", "_", name or root.name).strip("_") or "leanflow_project"
    if not package[0].isalpha():
        package = "project_" + package
    if not source_paths(root):
        starter = root / "Main.lean"
        starter.write_text(
            ("import Mathlib\n\n" if preset == "mathlib" else "")
            + "theorem example_goal : True := by\n  sorry\n",
            encoding="utf-8",
        )
        changed.append(starter.name)
    modules = [
        path.stem
        for path in source_paths(root)
        if path.parent == root and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", path.stem)
    ]
    text = f"name = {json.dumps(package)}\n"
    if modules:
        text += "defaultTargets = " + json.dumps(modules) + "\n"
        for module in modules:
            text += f"\n[[lean_lib]]\nname = {json.dumps(module)}\n"
    if preset == "mathlib":
        text += '\n[[require]]\nname = "mathlib"\ngit = "https://github.com/leanprover-community/mathlib4"\n'
        text += f'rev = "{MATHLIB_REVISION}"\n'
    (root / "lakefile.toml").write_text(text, encoding="utf-8")
    changed.append("lakefile.toml")
    if not toolchain:
        (root / "lean-toolchain").write_text(DEFAULT_TOOLCHAIN + "\n", encoding="utf-8")
        changed.append("lean-toolchain")
