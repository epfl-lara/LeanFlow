"""Bound and archive the append-only workflow outcome stream."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

_OUTCOME_LOCK = threading.Lock()
_DEFAULT_STREAM_MAX_BYTES = 16 * 1024 * 1024
_DEFAULT_RECORD_MAX_BYTES = 256 * 1024


def _bounded_env_bytes(name: str, default: int, *, minimum: int) -> int:
    """Return one size limit from the environment with a safe lower bound."""
    try:
        return max(minimum, int(str(os.getenv(name, default) or default)))
    except (TypeError, ValueError):
        return default


def _timestamp_slug() -> str:
    """Return a collision-resistant UTC artifact suffix."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


@contextlib.contextmanager
def _outcome_lock(state_root: Path) -> Iterator[None]:
    """Serialize outcome rotation and append across threads and processes."""
    lock_path = state_root / ".outcomes.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _OUTCOME_LOCK, lock_path.open("a+b") as handle:
        locked = False
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
        try:
            yield
        finally:
            if locked and fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _gzip_bytes_atomic(path: Path, payload: bytes) -> None:
    """Write one gzip artifact atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with gzip.open(temporary, "wb", compresslevel=6) as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _externalize_large_entry(
    state_root: Path,
    entry: Mapping[str, Any],
) -> tuple[dict[str, Any], Path | None]:
    """Move an oversized outcome payload to a compressed sidecar."""
    serialized = json.dumps(dict(entry), sort_keys=True).encode("utf-8")
    maximum = _bounded_env_bytes(
        "LEANFLOW_OUTCOME_RECORD_MAX_BYTES",
        _DEFAULT_RECORD_MAX_BYTES,
        minimum=1024,
    )
    if len(serialized) <= maximum:
        return dict(entry), None
    sidecar = state_root / "outcome-payloads" / f"outcome-{_timestamp_slug()}.json.gz"
    _gzip_bytes_atomic(sidecar, serialized)
    payload = entry.get("payload")
    payload_keys = sorted(str(key) for key in payload) if isinstance(payload, Mapping) else []
    compact = {key: value for key, value in entry.items() if key != "payload"}
    compact["payload"] = {
        "externalized": True,
        "artifact": str(sidecar.relative_to(state_root)),
        "encoding": "json+gzip",
        "sha256": hashlib.sha256(serialized).hexdigest(),
        "uncompressed_bytes": len(serialized),
        "payload_keys": payload_keys,
    }
    return compact, sidecar


def _archive_stream_if_needed(path: Path, state_root: Path) -> Path | None:
    """Compress and clear an oversized current stream without losing history."""
    maximum = _bounded_env_bytes(
        "LEANFLOW_OUTCOME_STREAM_MAX_BYTES",
        _DEFAULT_STREAM_MAX_BYTES,
        minimum=1024,
    )
    try:
        if path.stat().st_size < maximum:
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    archive = state_root / "outcomes-archive" / f"outcomes-{_timestamp_slug()}.jsonl.gz"
    _gzip_bytes_atomic(archive, payload)
    # The compressed archive is durable before the active tail is cleared.
    # Keep the stable outcomes.jsonl path for reverse-tail readers.
    path.write_bytes(b"")
    return archive


def append_outcome_entry(
    path: Path,
    entry: Mapping[str, Any],
    *,
    append: Callable[[Path, str], None],
) -> dict[str, str]:
    """Append one bounded outcome and archive the active stream when necessary."""
    state_root = path.parent
    with _outcome_lock(state_root):
        compact, sidecar = _externalize_large_entry(state_root, entry)
        archive = _archive_stream_if_needed(path, state_root)
        append(path, json.dumps(compact, sort_keys=True) + "\n")
    return {
        "archive": str(archive or ""),
        "sidecar": str(sidecar or ""),
    }
