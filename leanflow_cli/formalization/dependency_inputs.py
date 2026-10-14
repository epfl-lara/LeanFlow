"""Bind pinned compiled libraries and tooling to explicit, independently checked inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.dependency_files import package_files, tooling_package
from leanflow_cli.formalization.dependency_modules import validate_library_roots
from leanflow_cli.formalization.evidence import contained
from leanflow_cli.workflows.prover.native_snapshot import _EXCLUDED

POLICY = "compiled-library-authority-v2"
_COMPILED = {".olean", ".ilean", ".ir", ".so", ".dylib", ".dll", ".a", ".o", ".bc"}


def _error(name: str, detail: str) -> ValueError:
    return ValueError(
        f"unsupported_dependency_inputs: {name}: {detail}. "
        "Formalization requires recorded declarative inputs or fully pinned compiled libraries; "
        "compiled libraries require independent kernel replay and semantic review before acceptance"
    )


def _package_files(directory: Path, name: str) -> dict[str, str]:
    """Hash every file and admit source/configuration without trusting binary caches."""
    from leanflow_cli.formalization.lean_layout import _static_configuration
    from leanflow_cli.formalization.lean_policy import validate_source

    result: dict[str, str] = {}
    configs = []
    for parent, directories, files in os.walk(directory, followlinks=False):
        if Path(parent) == directory:
            directories[:] = [name for name in directories if name != ".git"]
            files = [name for name in files if name != ".git"]
        for child in sorted([*directories, *files]):
            path = Path(parent) / child
            relative = path.relative_to(directory).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise _error(name, f"aliased or nonregular input {relative}")
            if stat.S_ISDIR(mode):
                continue
            if any(suffix in _COMPILED for suffix in path.suffixes):
                raise _error(name, f"unauthenticated precompiled input {relative}")
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise _error(name, f"input changed while reading {relative}")
            try:
                if relative in {"lakefile.lean", "lakefile.toml"}:
                    _static_configuration(path, data.decode("utf-8"))
                    configs.append(relative)
                elif path.suffix == ".lean":
                    validate_source(data.decode("utf-8"))
            except (ValueError, UnicodeError) as error:
                raise _error(
                    name, f"unsupported source/configuration {relative}: {error}"
                ) from error
            result[relative] = hashlib.sha256(data).hexdigest()
    if not configs:
        raise _error(name, "missing static Lake configuration")
    return result


def dependency_inputs(root: Path) -> dict[str, Any]:
    """Record the external closure without trusting package names or cached binaries.

    Full Git pins use a compiled-library adapter: artifacts are mathematical inputs,
    not claimed source-authenticated build products. Acceptance subsequently replays
    the declarations used by the proposal and exposes compiled semantic dependencies
    to the source reviewer. Tooling is recorded but excluded from checker imports.
    Other dependencies retain the conservative declarative-source contract.
    """
    manifest_path = contained(root, "lake-manifest.json")
    if not manifest_path.is_file():
        return {"policy": POLICY, "packages": {}}
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise _error("manifest", "dependency manifest must be an object")
    packages = manifest.get("packages", [])
    if not isinstance(packages, list):
        raise _error("manifest", "package closure is not a list")
    package_root = contained(root, manifest.get("packagesDir", ".lake/packages"))
    result: dict[str, Any] = {}
    names: set[str] = set()
    directories: set[Path] = set()
    for package in packages:
        name = package.get("name") if isinstance(package, dict) else None
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or any(char in name for char in "/\\")
        ):
            raise _error("manifest", "invalid dependency name")
        if name.casefold() in names:
            raise _error(name, "duplicate dependency identity")
        names.add(name.casefold())
        if package.get("subDir") is not None and package.get("subDir") != "":
            raise _error(name, "nonempty checkout subDir requires an explicit layout adapter")
        if package.get("type") == "path":
            path = contained(root, package.get("dir"))
        elif (
            package.get("type") == "git"
            and isinstance(package.get("rev"), str)
            and package["rev"].strip()
        ):
            path = contained(package_root, name)
        else:
            raise _error(name, "dependency revision is not pinned")
        if not path.is_dir():
            raise _error(name, "pinned dependency is unavailable")
        relative = path.relative_to(root.resolve())
        if any(part in _EXCLUDED for part in relative.parts):
            raise _error(name, "dependency overlaps private project state")
        if any(path == old or path in old.parents or old in path.parents for old in directories):
            raise _error(name, "dependency directories must not overlap")
        directories.add(path)
        revision = package.get("rev", "")
        if (
            package.get("type") == "git"
            and isinstance(revision, str)
            and re.fullmatch(r"[0-9a-fA-F]{40}", revision)
        ):
            try:
                files = package_files(path)
                if not {"lakefile.lean", "lakefile.toml"}.intersection(files):
                    raise ValueError("missing Lake configuration")
                library = contained(path, ".lake/build/lib/lean")
                mode = "tooling" if tooling_package(name, path) else "compiled-library"
            except (OSError, ValueError) as error:
                raise _error(name, str(error)) from error
            result[name] = {
                "mode": mode,
                "directory": relative.as_posix(),
                "revision": revision,
                "files": files,
                "library_dir": (
                    ".lake/build/lib/lean"
                    if library.is_dir()
                    and any(
                        key.startswith(".lake/build/lib/lean/") and key.endswith(".olean")
                        for key in files
                    )
                    else None
                ),
                "authority": "compiled-declarations; source-build-correspondence-unclaimed",
                "manifest": package,
            }
        else:
            result[name] = {
                "mode": "declarative-source",
                "directory": relative.as_posix(),
                "revision": revision,
                "files": _package_files(path, name),
            }
    return {"policy": POLICY, "packages": result}


def compiled_library_paths(root: Path) -> list[Path]:
    """Return conventional pinned library roots without running any Lake configuration.

    Inventory is not acceptance. The independent checker validates every resolved
    module against these exact roots, rejects compiler shadowing and replays the
    needed mathematical declarations; REPL executable modules are intentionally unavailable.
    """
    from leanflow_cli.formalization.environment import _compiler_root

    inventory = dependency_inputs(root)
    libraries = [
        contained(contained(root, record["directory"]), record["library_dir"])
        for _, record in sorted(inventory["packages"].items())
        if record["mode"] == "compiled-library" and record.get("library_dir")
    ]
    validate_library_roots(libraries, _compiler_root(root) / "lib/lean")
    return libraries
