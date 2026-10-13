"""Fingerprint relocatable private project inputs and exact dependency artifact bytes."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.runtime.dependency_cache.checkout import _git
from leanflow_cli.runtime.project_build_cache.freshness import InputGuard
from leanflow_cli.runtime.project_build_cache.toolchain import _compiler_identity

_EXCLUDED = {
    ".git",
    ".lake",
    ".leanflow-job",
    ".leanflow-dependencies-released",
    ".leanflow-cache-owner",
}
_ENVIRONMENT_KEYS = (
    "PATH",
    "HOME",
    "ELAN_HOME",
    "ELAN_TOOLCHAIN",
    "LEAN_PATH",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
)


def file_digest(path: Path, *, guard: InputGuard | None = None) -> str:
    """Hash a regular private file without following links or sharing writable inodes."""
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        raise ValueError(f"Project cache inputs must be independent regular files: {path}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if guard is not None:
        guard.record_file(path, status)
    return digest


def tree_manifest(
    root: Path, *, excluded: set[str] | None = None, guard: InputGuard | None = None
) -> dict[str, Any]:
    """Record every regular file and directory; reject symbolic links and special files."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Project cache inputs require a private directory: {root}")
    files: dict[str, Any] = {}
    directories: list[str] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        if guard is not None:
            guard.record_directory(current, [*names, *filenames], excluded=excluded)
        names[:] = sorted(name for name in names if not excluded or name not in excluded)
        for name in names:
            child = current / name
            if child.is_symlink() or not child.is_dir():
                raise ValueError(f"Project cache inputs cannot contain linked directories: {child}")
            directories.append(child.relative_to(root).as_posix())
        for name in sorted(filenames):
            if excluded and name in excluded:
                continue
            path = current / name
            files[path.relative_to(root).as_posix()] = {
                "sha256": file_digest(path, guard=guard),
                "size": path.stat().st_size,
                "executable": bool(path.stat().st_mode & 0o111),
            }
    return {"directories": sorted(directories), "files": files}


def _dependencies(project: Path, *, guard: InputGuard | None = None) -> dict[str, Any]:
    """Require a complete clean Git lock closure and fingerprint effective private artifacts."""
    manifest_path = project / "lake-manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Project caching cannot follow a linked dependency manifest")
    manifest = (
        json.loads(manifest_path.read_bytes()) if manifest_path.exists() else {"packages": []}
    )
    if not isinstance(manifest, dict) or not isinstance(manifest.get("packages"), list):
        raise ValueError("Project caching requires a locked dependency manifest")
    if (
        manifest.get("packagesDir", ".lake/packages") != ".lake/packages"
        or manifest.get("lakeDir", ".lake") != ".lake"
    ):
        raise ValueError("Project caching does not support custom dependency directories")
    packages = project / ".lake/packages"
    expected: set[str] = set()
    dependencies: dict[str, Any] = {}
    for package in manifest["packages"]:
        if not isinstance(package, dict) or package.get("type") != "git":
            raise ValueError(
                "Project caching requires clean Git dependencies; path packages are unsupported"
            )
        name, revision = package.get("name"), package.get("rev")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
            or name in expected
            or not isinstance(revision, str)
            or len(revision) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in revision)
            or not isinstance(package.get("url"), str)
            or not package["url"]
        ):
            raise ValueError("Project caching requires unique confined pinned Git dependencies")
        for field in ("subDir", "configFile", "manifestFile"):
            value = package.get(field)
            if value is not None and (
                not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts
            ):
                raise ValueError("Dependency configuration must stay within its private checkout")
        expected.add(name)
        checkout = packages / name
        if checkout.is_symlink() or not checkout.is_dir() or (checkout / ".git").is_symlink():
            raise ValueError("Project dependencies must retain private Git provenance")
        if not (checkout / ".git").is_dir() or (checkout / ".git/objects/info/alternates").exists():
            raise ValueError("Project dependencies cannot reference external Git object stores")
        provenance = _git(checkout, "rev-parse", "--show-toplevel", "HEAD").splitlines()
        if provenance != [str(checkout.resolve()), revision] or _git(
            checkout, "status", "--porcelain", "--untracked-files=all"
        ):
            raise ValueError("Edited or unlocked dependencies cannot use project build caching")
        dependencies[name] = {
            "lock": package,
            "effective_files": tree_manifest(checkout, excluded={".git"}, guard=guard),
        }
    if packages.is_symlink() or (
        packages.exists() and {path.name for path in packages.iterdir()} != expected
    ):
        raise ValueError("Installed dependencies must match the complete locked closure")
    if expected and not packages.is_dir():
        raise ValueError("Locked dependency sources are unavailable")
    if guard is not None:
        lake = project / ".lake"
        if lake.is_dir():
            guard.record_directory(lake, [path.name for path in lake.iterdir()], excluded={"build"})
        if packages.is_dir():
            guard.record_directory(packages, sorted(expected))
    return dependencies


def build_identity(
    project: Path,
    modules: Sequence[str],
    environment: Mapping[str, str],
    *,
    guard: InputGuard | None = None,
) -> dict[str, Any]:
    """Fingerprint opt-in TOML projects under the explicit relocatable-build contract.

    Arbitrary elaborator IO cannot be inferred from source text. The opt-in asserts
    that Lean modules declare their inputs and do not embed snapshot-specific paths.
    Lake still validates its original traces after every restore.
    """
    if os.environ.get("LEANFLOW_PROJECT_BUILD_CACHE", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise ValueError(
            "Project build caching requires LEANFLOW_PROJECT_BUILD_CACHE=1 and relocatable inputs"
        )
    if (project / "lakefile.lean").exists() or not (project / "lakefile.toml").is_file():
        raise ValueError("Project build caching currently supports TOML-only Lake configurations")
    configuration = tomllib.loads((project / "lakefile.toml").read_text(encoding="utf-8"))
    for name, default in (
        ("buildDir", ".lake/build"),
        ("lakeDir", ".lake"),
        ("packagesDir", ".lake/packages"),
        ("manifestFile", "lake-manifest.json"),
    ):
        if configuration.get(name, default) != default:
            raise ValueError(
                "Project build caching requires standard Lake output and dependency paths"
            )
    if (project / ".lake/package-overrides.json").exists():
        raise ValueError("Project build caching does not support dependency overrides")
    inherited = {name: environment[name] for name in _ENVIRONMENT_KEYS if name in environment}
    if inherited.get("LEAN_PATH"):
        raise ValueError("Project build caching cannot account for external LEAN_PATH modules")
    if project.is_symlink() or (project / ".lake").is_symlink():
        raise ValueError("Project build caching cannot follow linked workspace boundaries")
    return {
        "schema_version": 1,
        "modules": list(modules),
        "source_files": tree_manifest(project, excluded=_EXCLUDED, guard=guard),
        "dependencies": _dependencies(project, guard=guard),
        "compiler": (
            _compiler_identity(project, inherited, guard=guard)
            if guard is not None
            else _compiler_identity(project, inherited)
        ),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "sandbox_environment": inherited,
        "sandbox_policy": "isolated-lake-no-network-private-temporary-home-v1",
        "relocatable_build_contract": True,
    }


def identity_key(identity: Mapping[str, Any]) -> str:
    """Hash the complete effective source, dependency, compiler and sandbox identity."""
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
