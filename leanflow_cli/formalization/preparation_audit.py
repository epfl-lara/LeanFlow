"""Reuse retained-version byte hashes only behind complete precise filesystem metadata censuses."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from leanflow_cli.formalization.evidence import durable_json, identity
from leanflow_cli.formalization.preparation_fs_inputs import _entries, entry_identity


def _metadata(path: Path, check: Callable[[], None] | None) -> str:
    result = hashlib.sha256()
    if not path.exists() and not path.is_symlink():
        return identity("absent")
    for entry in _entries(path):
        if check:
            check()
        info = entry.lstat()
        if info.st_ctime_ns % 1_000_000_000 == 0:
            raise ValueError("Retained auditing requires precise change timestamps")
        row: list[Any] = [
            "" if entry == path else entry.relative_to(path).as_posix(),
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ]
        if stat.S_ISLNK(info.st_mode):
            row.append(os.readlink(entry))
        result.update(bytes.fromhex(identity(row)))
    return result.hexdigest()


def retained_identity(
    root: Path, path: Path, *, check: Callable[[], None] | None = None
) -> str | None:
    """Audit all names/inodes/modes/timestamps; rehash bytes whenever any entry changes.

    This cache audits preserved history only. Environment and mathematical
    acceptance continue to stream actual compiler, dependency and source bytes.
    A coarse timestamp filesystem always falls back to independent byte hashing.
    """
    try:
        before = _metadata(path, check)
    except ValueError:
        return entry_identity(path, check=check)
    cache = (
        leanflow_home()
        / "preparation-retained-audit"
        / (identity([str(root.resolve()), str(path.absolute())]) + ".json")
    )
    try:
        saved = json.loads(cache.read_text())
        if (
            saved.get("schema_version") == 1
            and saved.get("metadata") == before
            and saved.get("path") == str(path.absolute())
            and saved.get("cache_id")
            == identity({k: v for k, v in saved.items() if k != "cache_id"})
        ):
            return saved["entry_identity"]
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    checked = entry_identity(path, check=check)
    if _metadata(path, check) != before:
        raise ValueError("Retained preparation version changed while auditing")
    payload = {
        "schema_version": 1,
        "path": str(path.absolute()),
        "metadata": before,
        "entry_identity": checked,
    }
    durable_json(cache, {**payload, "cache_id": identity(payload)})
    return checked
