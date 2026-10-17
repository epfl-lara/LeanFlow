"""Freeze content-addressed formalization evidence and validate project-relative references."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any


def digest(data: bytes | str) -> str:
    """Hash exact bytes, encoding textual records as UTF-8."""
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def identity(value: Any) -> str:
    """Hash a canonical JSON record independently of presentation ordering."""

    def validate_keys(entry: Any) -> None:
        if isinstance(entry, dict):
            if any(not isinstance(key, str) for key in entry):
                raise ValueError("Evidence object keys must be strings")
            for child in entry.values():
                validate_keys(child)
        elif isinstance(entry, (list, tuple)):
            for child in entry:
                validate_keys(child)

    validate_keys(value)
    return digest(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    )


def identifier(value: str) -> str:
    """Reject IDs that could escape a controller-owned record directory."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Invalid formalization identifier")
    return value


def normalized_relative(relative: str) -> str:
    """Require one portable spelling for each project-relative evidence path."""
    if not isinstance(relative, str) or "\\" in relative or "\0" in relative or ":" in relative:
        raise ValueError("Formalization paths must use portable relative names")
    parts = relative.split("/")
    if (
        any(part in {"", "..", "."} for part in parts)
        or unicodedata.normalize("NFC", relative) != relative
    ):
        raise ValueError("Formalization paths must be project-relative")
    return relative


def contained(root: Path, relative: str) -> Path:
    """Resolve a normalized relative location without following source aliases."""
    normalized_relative(relative)
    root = root.resolve()
    path = root.joinpath(*relative.split("/"))
    path.resolve().relative_to(root.resolve())
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError(f"Formalization path traverses a symlink: {relative}")
        cursor = cursor.parent
    return path


def sync_directory(path: Path) -> None:
    """Persist directory entries after publishing durable evidence."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_json(path: Path, value: Any) -> None:
    """Replace a controller-owned JSON record and flush its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(
            (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        )
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def put_blob(directory: Path, content: bytes) -> str:
    """Publish fully flushed immutable evidence without exposing a partial digest file."""
    key = digest(content)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / key
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".blob-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
            sync_directory(directory)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise ValueError("Existing evidence blob does not match its identity") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return key


def read_blob(directory: Path, key: str) -> bytes:
    """Verify immutable evidence before allowing it to influence a gate."""
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("Invalid evidence digest")
    path = contained(directory, key)
    data = path.read_bytes()
    if digest(data) != key:
        raise ValueError("Formalization evidence was modified")
    return data
