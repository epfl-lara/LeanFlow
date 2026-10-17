"""Cache display-only environment hashes behind complete filesystem metadata censuses."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

from core.home import leanflow_home
from leanflow_cli.formalization.environment import _compiler_root, environment_identity
from leanflow_cli.formalization.evidence import durable_json, identity

_PACKAGES = ("core", "agent", "leanflow_cli", "tools", "model_tools", "toolsets", "utils")
_CONFIG = ("lean-toolchain", "lakefile.lean", "lakefile.toml", "lake-manifest.json")


def _tree(directory: Path) -> Iterator[Path]:
    """Enumerate every input entry without following directory aliases or retaining file bytes."""
    yield directory
    if not directory.exists():
        return
    for current, dirs, files in os.walk(directory, followlinks=False):
        for name in sorted([*dirs, *files]):
            path = Path(current) / name
            if path.is_symlink() and path.is_dir():
                raise ValueError("Display hash cache cannot certify a directory alias")
            yield path
        dirs.sort()


def input_metadata(root: Path) -> str:
    """Bind compiler location and all external/controller entry identities, including ctime."""
    if not (root / "lean-toolchain").is_file():
        raise ValueError("No prepared compiler metadata is available")
    compiler = _compiler_root(root)
    base = Path(__file__).resolve().parents[2]
    checksum = hashlib.sha256()
    entries = [root / name for name in _CONFIG]
    for role, paths in (
        ("config", iter(entries)),
        ("compiler-bin", _tree(compiler / "bin")),
        ("compiler-lib", _tree(compiler / "lib/lean")),
        ("packages", _tree(root / ".lake/packages")),
        (
            "checker",
            (path for package in _PACKAGES for path in sorted((base / package).rglob("*.py"))),
        ),
    ):
        for path in paths:
            try:
                info = path.lstat()
                if info.st_ctime_ns % 1_000_000_000 == 0:
                    raise ValueError("Display caching requires precise change timestamps")
                metadata: list[object] = [
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ]
                if stat.S_ISLNK(info.st_mode):
                    target = path.stat()
                    if target.st_ctime_ns % 1_000_000_000 == 0:
                        raise ValueError("Display caching requires precise target timestamps")
                    metadata.extend(
                        [
                            os.readlink(path),
                            target.st_dev,
                            target.st_ino,
                            target.st_size,
                            target.st_mtime_ns,
                            target.st_ctime_ns,
                        ]
                    )
            except FileNotFoundError:
                metadata = ["absent"]
            checksum.update(json.dumps([role, str(path), metadata], separators=(",", ":")).encode())
    checksum.update(
        json.dumps(
            [str(compiler), os.environ.get("LEAN_PATH"), os.environ.get("ELAN_TOOLCHAIN")]
        ).encode()
    )
    return checksum.hexdigest()


def display_environment_identity(
    root: Path, *, checker: Callable[[Path], str] = environment_identity
) -> str:
    """Reuse a byte hash for UI only; acceptance and prover startup always rehash independently."""
    root = root.resolve()
    try:
        before = input_metadata(root)
    except (OSError, ValueError, subprocess.SubprocessError):
        return checker(root)
    directory = leanflow_home() / "formalization-display-environments"
    path = directory / (identity(str(root)) + ".json")
    try:
        cached = json.loads(path.read_text())
        if (
            cached.get("version") == 1
            and cached.get("root") == str(root)
            and cached.get("metadata") == before
            and cached.get("cache_id")
            == identity({k: v for k, v in cached.items() if k != "cache_id"})
        ):
            return str(cached["environment_id"])
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    checked = checker(root)
    if input_metadata(root) != before:
        raise ValueError("Environment changed while preparing display evidence; refresh again")
    payload = {"version": 1, "root": str(root), "metadata": before, "environment_id": checked}
    directory.mkdir(parents=True, exist_ok=True)
    durable_json(path, {**payload, "cache_id": identity(payload)})
    return checked
