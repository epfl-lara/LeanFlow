"""Derive Lean dependency artifact identity without requiring installed packages."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import tomllib
from pathlib import Path
from typing import Any

_SCHEMA = 1
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
# Root targets do not change dependency artifacts. Keep every other setting,
# including unfamiliar future settings, rather than guessing which options matter.
_PROJECT_ONLY = {
    "name",
    "version",
    "description",
    "keywords",
    "homepage",
    "license",
    "licenseFiles",
    "readmeFile",
    "defaultTargets",
    "srcDir",
    "lean_lib",
    "lean_exe",
    "script",
}
_BUILD_ENV = (
    "ELAN_TOOLCHAIN",
    "LEAN_CC",
    "LEAN_CXX",
    "LEAN_AR",
    "LEAN_SYSROOT",
    "LEAN_OPTS",
    "LAKE_OPTS",
    "LAKE_BUILD_ARGS",
    "LAKE_ARTIFACT_CACHE",
    "LAKE_RESTORE_ARTIFACTS",
    "LEAN_PLATFORM_TARGET",
    "CFLAGS",
    "CXXFLAGS",
    "CPPFLAGS",
    "LDFLAGS",
    "LEAN_CFLAGS",
    "LEAN_LDFLAGS",
)


def _regular(path: Path) -> bytes:
    """Read configuration without following links or accepting oversized files."""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Dependency configuration must be a regular file: {path.name}")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(f"Dependency configuration is too large: {path.name}")
    return path.read_bytes()


def _canonical_root(project_root: Path) -> Path:
    """Reject private agent workspaces and linked project/cache ownership boundaries."""
    if project_root.is_symlink():
        raise ValueError("Cannot share dependencies from a linked project")
    root = project_root.resolve(strict=True)
    for parent in (root, *root.parents):
        if (parent / ".leanflow-job").exists() or (
            parent / ".leanflow-dependencies-released"
        ).exists():
            raise ValueError("Private agent workspaces cannot publish shared dependencies")
    for path in (root / ".lake", root / ".lake/packages"):
        if path.is_symlink():
            raise ValueError("Shared dependencies require private project package directories")
    if (root / ".lake/package-overrides.json").exists():
        raise ValueError("Package overrides cannot use the shared dependency cache")
    return root


def _identity(root: Path) -> dict[str, Any] | None:
    """Describe artifact compatibility using inputs available before installation."""
    manifest_path = root / "lake-manifest.json"
    toolchain_path = root / "lean-toolchain"
    if not manifest_path.exists() or not toolchain_path.exists():
        return None
    manifest = json.loads(_regular(manifest_path))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("packages"), list):
        raise ValueError("Invalid locked dependency manifest")
    if (
        manifest.get("packagesDir", ".lake/packages") != ".lake/packages"
        or manifest.get("lakeDir", ".lake") != ".lake"
    ):
        raise ValueError("Custom package directories cannot use the shared dependency cache")
    packages = manifest["packages"]
    names: set[str] = set()
    unsupported = False
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("Invalid locked dependency entry")
        name = package.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
            raise ValueError(
                "Dependency names must be unique and confined to their package directory"
            )
        names.add(name)
        for field in ("subDir", "configFile", "manifestFile"):
            value = package.get(field)
            if value is not None and (
                not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts
            ):
                raise ValueError(f"Dependency {name} configuration must stay inside its checkout")
        if package.get("type") != "git":
            unsupported = True
            continue
        revision, url = package.get("rev"), package.get("url")
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ValueError(f"Dependency {name} must be pinned to a complete Git revision")
        if not isinstance(url, str) or not url:
            raise ValueError(f"Dependency {name} is missing its repository identity")
    if not packages or unsupported:
        return None
    configurations = [
        root / name for name in ("lakefile.toml", "lakefile.lean") if (root / name).exists()
    ]
    if len(configurations) != 1:
        raise ValueError("Shared dependencies require exactly one Lake configuration")
    configuration_path = configurations[0]
    content = _regular(configuration_path)
    if configuration_path.suffix == ".toml":
        configuration = tomllib.loads(content.decode())
        if (
            configuration.get("packagesDir", ".lake/packages") != ".lake/packages"
            or configuration.get("manifestFile", "lake-manifest.json") != "lake-manifest.json"
        ):
            raise ValueError("Custom manifest/package paths cannot use the shared dependency cache")
        build_settings: Any = {
            key: value for key, value in configuration.items() if key not in _PROJECT_ONLY
        }
    else:
        # Lean DSL files can execute arbitrary build logic; full-file equality is
        # the conservative compatibility boundary until Lake exposes typed inputs.
        build_settings = {"lakefile_sha256": hashlib.sha256(content).hexdigest()}
    toolchain = _regular(toolchain_path).decode().strip()
    if not toolchain:
        raise ValueError("A shared dependency cache requires an explicit Lean toolchain")
    environment = {name: os.environ[name] for name in _BUILD_ENV if name in os.environ}
    selected = environment.get("ELAN_TOOLCHAIN", toolchain)
    elan = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))).expanduser()
    executable = elan / "toolchains" / selected.replace("/", "--").replace(":", "---") / "bin/lean"
    compiler_hash = None
    if executable.is_file():
        with executable.open("rb") as stream:
            compiler_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "schema_version": _SCHEMA,
        "toolchain": toolchain,
        "compiler_sha256": compiler_hash,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "build_settings": build_settings,
        "build_environment": environment,
        "packages": sorted(packages, key=lambda item: item["name"]),
    }


def _key(identity: dict[str, Any]) -> str:
    """Hash canonical inputs without project paths or current artifact presence."""
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
