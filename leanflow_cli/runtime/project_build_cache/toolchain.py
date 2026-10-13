"""Fingerprint selected compiler bytes and confined native library aliases for project caching."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from leanflow_cli.runtime.project_build_cache.freshness import InputGuard


@lru_cache(maxsize=32768)
def _toolchain_digest(path: Path, stamp: tuple[int, ...]) -> str:
    """Memoize immutable external toolchain bytes by inode and complete change metadata."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _toolchain_file_digest(
    path: Path, *, guard: InputGuard | None = None, toolchain_root: Path | None = None
) -> str:
    """Detect replacement and same-size rewrites before reusing a compiler content digest."""
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode):
        if toolchain_root is None:
            raise ValueError("Compiler aliases require an exact selected toolchain root")
        return _toolchain_alias_digest(path, toolchain_root, guard=guard)
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("Project build caching requires regular toolchain files")
    stamp = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
    digest = _toolchain_digest(path, stamp)
    if guard is not None:
        guard.record_file(path, status)
    return digest


def _toolchain_alias_digest(path: Path, root: Path, *, guard: InputGuard | None = None) -> str:
    """Hash file aliases only within the selected compiler, preserving link and target identity.

    Project sources, outputs and dependencies still reject all aliases. Native
    toolchains legitimately use versioned shared-library aliases on Linux.
    """
    selected = root.resolve(strict=True)
    aliases: list[tuple[Path, os.stat_result, str]] = []
    current = path
    seen: set[Path] = set()
    try:
        while True:
            current = current.parent.resolve(strict=True) / current.name
            if not current.is_relative_to(selected):
                raise ValueError("Compiler alias escapes the selected toolchain")
            if current in seen:
                raise ValueError("Compiler alias cycle cannot use project build caching")
            seen.add(current)
            status = current.lstat()
            if not stat.S_ISLNK(status.st_mode):
                break
            link = os.readlink(current)
            aliases.append((current, status, link))
            target = Path(link)
            current = target if target.is_absolute() else current.parent / target
        target_hash = _toolchain_file_digest(current, guard=guard)
        resolved = current.resolve(strict=True)
        retained = guard if guard is not None else InputGuard()
        for alias, before, link in aliases:
            retained.record_symlink(alias, before, link, resolved)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "Compiler alias is missing, cyclic or outside the selected toolchain"
        ) from exc
    return hashlib.sha256(
        json.dumps(
            {
                "aliases": [
                    (alias.relative_to(selected).as_posix(), link) for alias, _, link in aliases
                ],
                "target": resolved.relative_to(selected).as_posix(),
                "target_sha256": target_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _compiler_identity(
    project: Path, environment: Mapping[str, str], *, guard: InputGuard | None = None
) -> dict[str, Any]:
    """Resolve the exact selected Lean and Lake binaries through Elan without compiling."""
    toolchain = (project / "lean-toolchain").read_text(encoding="utf-8").strip()
    if not toolchain:
        raise ValueError("Project build caching requires an explicit Lean toolchain")
    elan = shutil.which("elan", path=environment.get("PATH"))
    if elan is None:
        raise ValueError("Project build caching requires resolvable Elan executables")
    binaries: dict[str, Any] = {"toolchain": toolchain}
    selected_root: Path | None = None
    for name in ("lean", "lake"):
        result = subprocess.run(
            [elan, "which", name],
            cwd=project,
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            raise ValueError(f"Cannot resolve the selected {name} compiler for project caching")
        executable = Path(result.stdout.strip())
        compiler_root = executable.parent.parent.resolve(strict=True)
        if executable.parent.name != "bin" or (
            selected_root is not None and selected_root != compiler_root
        ):
            raise ValueError("Lean and Lake must belong to the same selected toolchain")
        selected_root = compiler_root
        if not executable.resolve(strict=True).is_relative_to(compiler_root):
            raise ValueError("Compiler executable escapes the selected toolchain")
        binaries[f"{name}_path"] = str(executable.resolve(strict=True))
        binaries[f"{name}_sha256"] = _toolchain_file_digest(
            executable, guard=guard, toolchain_root=compiler_root
        )
    invoked_lake = shutil.which("lake", path=environment.get("PATH"))
    if invoked_lake is None or not (
        os.path.samefile(invoked_lake, binaries["lake_path"])
        or os.path.samefile(invoked_lake, elan)
    ):
        raise ValueError(
            "Project build caching requires the selected Lake executable or standard Elan proxy"
        )
    binaries["elan_sha256"] = _toolchain_file_digest(Path(elan).resolve(strict=True), guard=guard)
    assert selected_root is not None
    libraries = selected_root / "lib"
    runtime: dict[str, str] = {}
    if libraries.is_symlink() or not libraries.is_dir():
        raise ValueError("Project build caching requires a private complete toolchain library tree")
    for directory, directories, names in os.walk(libraries, followlinks=False):
        if guard is not None:
            guard.record_directory(Path(directory), [*directories, *names])
        if any((Path(directory) / name).is_symlink() for name in directories):
            raise ValueError(
                "Project build caching cannot fingerprint linked toolchain library directories"
            )
        for name in names:
            path = Path(directory) / name
            if (
                name.endswith(
                    (
                        ".olean",
                        ".olean.private",
                        ".olean.server",
                        ".ir",
                        ".ir.sig",
                        ".so",
                        ".dylib",
                        ".a",
                        ".dll",
                        ".o",
                        ".o.export",
                        ".o.server",
                    )
                )
                or ".so." in name
            ):
                runtime[path.relative_to(libraries).as_posix()] = _toolchain_file_digest(
                    path, guard=guard, toolchain_root=selected_root
                )
    if not runtime:
        raise ValueError("Project build caching requires available core Lean runtime artifacts")
    binaries["core_runtime"] = runtime
    return binaries
