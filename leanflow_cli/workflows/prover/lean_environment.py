"""Track local Lean dependency generations without rereading large compiled libraries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tomllib
from collections.abc import Iterable, Iterator
from pathlib import Path

_SKIP = {".git", ".leanflow", ".leanflow-job", ".venv", "node_modules", "__pycache__"}
_CONFIG = {"lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json"}
_ENV = ("PATH", "ELAN_HOME", "ELAN_TOOLCHAIN", "LEAN_PATH")
_CONTENT_FINGERPRINT_LIMIT = 64 * 1024


def _toolchain_roots(project: Path) -> list[Path]:
    """Resolve Elan's configured toolchain, retaining settings in the generation."""
    elan = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))).expanduser()
    settings = elan / "settings.toml"
    config = tomllib.loads(settings.read_text()) if settings.is_file() else {}
    selected = os.environ.get("ELAN_TOOLCHAIN", "")
    if not selected:
        overrides = config.get("overrides", {})
        matches = [
            (Path(path), value)
            for path, value in overrides.items()
            if project.is_relative_to(Path(path))
        ]
        if matches:
            selected = str(max(matches, key=lambda pair: len(pair[0].parts))[1])
    if not selected:
        for directory in (project, *project.parents):
            toolchain = directory / "lean-toolchain"
            if toolchain.is_file():
                selected = toolchain.read_text().strip()
                break
    selected = selected or str(config.get("default_toolchain", ""))
    installed = elan / "toolchains"
    named = installed / selected.replace(":", "---").replace("/", "--")
    # Missing toolchains remain explicit missing roots; installation changes
    # the generation without scanning unrelated installed compiler versions.
    toolchains = [named] if selected else list(installed.glob("*"))
    roots = [settings]
    for toolchain in toolchains:
        roots.extend((toolchain / "lib" / "lean", toolchain / "bin"))
    for program in ("lean", "lake"):
        executable = shutil.which(program)
        if executable:
            binary = Path(executable).resolve()
            roots.extend((binary, binary.parent.parent / "lib" / "lean"))
    return roots


def _files(root: Path, excluded_sources: set[Path]) -> Iterator[Path]:
    """Walk dependency inputs, following aliases once and omitting controller state."""

    def relevant(path: Path) -> bool:
        return (
            path.name in _CONFIG
            or path.name in {"lean", "lake", "settings.toml"}
            or ".olean" in path.name
            or path.suffix in {".so", ".dylib", ".dll", ".a"}
            or (path.suffix == ".lean" and path not in excluded_sources)
        )

    pending = [root]
    seen: set[Path] = set()
    while pending:
        path = pending.pop()
        if not path.is_dir():
            if relevant(path):
                yield path
            continue
        if path.is_symlink():
            yield path
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        with os.scandir(path) as entries:
            for entry in sorted(entries, key=lambda entry: entry.name):
                if entry.name in _SKIP:
                    continue
                child = Path(entry.path)
                if entry.is_dir():
                    pending.append(child)
                elif relevant(child):
                    yield child


def lean_environment_generation(project: Path, *, excluded_sources: Iterable[Path] = ()) -> str:
    """Fingerprint canonical imports, toolchain and loader configuration for local reuse.

    Exact candidate/managed source hashes belong to the caller. This additional
    generation covers unmanaged sources and every installed compiled artifact,
    including transitive imports, plugins, additions, removals and path aliases.
    Exclude managed source paths only when the caller separately keys their
    relevant import closure. Native snapshots use their own absolute root/key.

    Small inputs are content-hashed because local filesystems can coalesce
    consecutive write timestamps. Large library bytes are not reread: device/inode, size and nanosecond change
    and modification times identify versions on a normal local filesystem.
    This is controller-memory reuse, not a portable or persistent proof digest;
    filesystems that conceal mutations from stat are outside this contract.
    Call before and after verification, and again before promotion. External
    writers must still serialize dependency updates with final publication.
    """
    project = project.resolve()
    excluded = {path.resolve() for path in excluded_sources}
    roots = [project, *_toolchain_roots(project)]
    roots.extend(
        project / value for value in os.environ.get("LEAN_PATH", "").split(os.pathsep) if value
    )
    # Lake path dependencies may live outside the project and packages directory.
    manifest = project / "lake-manifest.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text())
        roots.append(project / data.get("packagesDir", ".lake/packages"))
        roots.extend(
            project / package["dir"]
            for package in data.get("packages", [])
            if package.get("type") == "path" and isinstance(package.get("dir"), str)
        )
    fingerprint = hashlib.sha256()
    fingerprint.update(json.dumps({name: os.environ.get(name) for name in _ENV}).encode())
    seen: set[Path] = set()
    minimal_roots: list[Path] = []
    for root in sorted(set(roots), key=lambda path: (len(path.parts), str(path))):
        covered = False
        for parent in minimal_roots:
            if root.resolve().is_relative_to(parent.resolve()):
                relative = root.resolve().relative_to(parent.resolve())
                # Explicit loader roots remain visible inside normally skipped
                # directories; an ancestor walk alone would miss their inputs.
                if not any(part in _SKIP for part in relative.parts):
                    covered = True
                    break
        if not covered:
            minimal_roots.append(root)
    for root in minimal_roots:
        fingerprint.update(str(root).encode())
        fingerprint.update(b"\0")
        if not root.exists():
            fingerprint.update(b"missing\0")
            continue
        for path in _files(root, excluded):
            if path in seen:
                continue
            seen.add(path)
            info = path.lstat()
            symlink = stat.S_ISLNK(info.st_mode)
            hash_content = not symlink and info.st_size <= _CONTENT_FINGERPRINT_LIMIT
            version = (
                str(path),
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                os.readlink(path) if symlink else "",
            )
            fingerprint.update(json.dumps(version).encode())
            fingerprint.update(b"\0")
            if symlink:
                target = path.stat()
                hash_content = (
                    stat.S_ISREG(target.st_mode) and target.st_size <= _CONTENT_FINGERPRINT_LIMIT
                )
                fingerprint.update(
                    repr(
                        (
                            target.st_dev,
                            target.st_ino,
                            target.st_size,
                            target.st_mtime_ns,
                            target.st_ctime_ns,
                        )
                    ).encode()
                )
                fingerprint.update(b"\0")
            if path.name in _CONFIG or path.name == "settings.toml" or hash_content:
                fingerprint.update(path.read_bytes())
                fingerprint.update(b"\0")
    return fingerprint.hexdigest()
