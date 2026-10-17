"""Inventory exact dependency bytes without equating cached artifacts with their sources."""

from __future__ import annotations

import hashlib
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

_CODE_SUFFIXES = {".lean", ".olean", ".ilean", ".ir", ".so", ".dylib", ".dll", ".a", ".o", ".bc"}
_CONFIG_NAMES = {"lakefile.toml", "lake-manifest.json", "lean-toolchain"}


def resolve_dependency_file(path: Path, package_root: Path) -> Path:
    """Resolve a confined resource alias for materialization as a private regular file.

    Mathlib ships script aliases. They carry no mathematical authority and may be
    copied by value, but executable Lean inputs and configuration retain strict
    no-alias boundaries. Absolute, escaping, dangling and directory links fail.
    """
    if not path.is_symlink():
        return path
    root = package_root.resolve()
    current = path
    seen: set[Path] = set()
    try:
        for _ in range(64):
            if not current.is_relative_to(root):
                raise ValueError(f"aliased input escapes its package: {path.name}")
            relative = current.relative_to(root)
            if (
                current in seen
                or ".git" in relative.parts
                or any(suffix in _CODE_SUFFIXES for suffix in current.suffixes)
                or current.name in _CONFIG_NAMES
            ):
                raise ValueError(
                    f"aliased executable, configuration or untracked input {path.name}"
                )
            seen.add(current)
            parent = root
            for part in relative.parts[:-1]:
                parent /= part
                if not stat.S_ISDIR(parent.lstat().st_mode):
                    raise ValueError(f"aliased directory input {path.name}")
            mode = current.lstat().st_mode
            if stat.S_ISREG(mode):
                return current
            if not stat.S_ISLNK(mode):
                raise ValueError(f"aliased or nonregular input {path.name}")
            link = os.readlink(current)
            if Path(link).is_absolute():
                raise ValueError(f"aliased absolute dependency input {path.name}")
            if ".git" in Path(link).parts:
                raise ValueError(f"aliased Git metadata input {path.name}")
            unresolved = current.parent / link
            current = Path(os.path.normpath(unresolved))
            if not current.is_relative_to(root):
                raise ValueError(f"aliased input escapes its package: {path.name}")
            parent = unresolved.parent
            while parent != root:
                if not parent.is_relative_to(root) or not stat.S_ISDIR(parent.lstat().st_mode):
                    raise ValueError(f"aliased directory input {path.name}")
                parent = parent.parent
    except (OSError, RuntimeError) as error:
        raise ValueError(f"aliased or unresolved input {path.name}") from error
    raise ValueError(f"dependency alias chain is too long: {path.name}")


def package_files(directory: Path) -> dict[str, str]:
    """Hash regular package inputs and reject aliases, special files and concurrent changes.

    Git metadata is not a mathematical input and is excluded from private checker
    views. Sources, resources, configuration and every compiled artifact are all
    recorded. A Git revision identifies the requested checkout, not the producer
    of a cached binary; independent kernel replay supplies mathematical checking.
    """
    result: dict[str, str] = {}
    for parent, directories, files in os.walk(directory, followlinks=False):
        if Path(parent) == directory:
            directories[:] = sorted(name for name in directories if name != ".git")
            files = [name for name in files if name != ".git"]
        for child in sorted([*directories, *files]):
            path = Path(parent) / child
            relative = path.relative_to(directory).as_posix()
            entry = path.lstat()
            if stat.S_ISDIR(entry.st_mode):
                continue
            target = resolve_dependency_file(path, directory)
            before = target.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"aliased or nonregular input {relative}")
            checksum = hashlib.sha256()
            with target.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError(f"input changed while opening {relative}")
                while block := handle.read(1024 * 1024):
                    checksum.update(block)
            after = target.lstat()
            entry_after = path.lstat()
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
            if any(
                getattr(before, key) != getattr(after, key)
                or getattr(entry, key) != getattr(entry_after, key)
                for key in fields
            ):
                raise ValueError(f"input changed while reading {relative}")
            result[relative] = checksum.hexdigest()
    return result


def tooling_package(name: str, directory: Path) -> bool:
    """Recognize the conventional REPL tool only to exclude its library search path.

    This grants no trust: all bytes remain fingerprinted, and a direct or transitive
    import of a tool module cannot be resolved by the mathematical checker.
    Packages using other layouts receive the full compiled-library checks.
    """
    if name.casefold() != "repl":
        return False
    path = directory / "lakefile.toml"
    if not path.is_file():
        return False
    config: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    libraries = config.get("lean_lib", [])
    executables = config.get("lean_exe", [])
    if not isinstance(executables, list) or not all(
        isinstance(executable, dict) for executable in executables
    ):
        return False
    roles = [(executable.get("name"), executable.get("root")) for executable in executables]
    return (
        config.get("name") == "REPL"
        and isinstance(libraries, list)
        and bool(libraries)
        and all(
            isinstance(library, dict)
            and library.get("name") == "REPL"
            and library.get("roots", ["REPL"]) == ["REPL"]
            for library in libraries
        )
        and roles.count(("repl", "REPL.Main")) == 1
        and roles.count(("test", "Test")) <= 1
        and all(role in (("repl", "REPL.Main"), ("test", "Test")) for role in roles)
    )
