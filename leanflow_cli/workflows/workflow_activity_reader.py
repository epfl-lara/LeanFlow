"""Stream managed-workflow JSONL activity without materializing history."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)

DEFAULT_MAX_JSONL_RECORD_BYTES = 64 * 1024
_DISCARD_CHUNK_BYTES = 64 * 1024
_REVERSE_SCAN_CHUNK_BYTES = 64 * 1024

JsonlPathsFingerprint = tuple[tuple[str, int, int, int, int], ...]


def jsonl_paths_fingerprint(paths: Iterable[Path]) -> JsonlPathsFingerprint:
    """Return a cheap identity/size/mtime fingerprint for ordered JSONL paths."""
    fingerprint: list[tuple[str, int, int, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        fingerprint.append(
            (
                str(path),
                int(stat.st_dev),
                int(stat.st_ino),
                int(stat.st_size),
                int(stat.st_mtime_ns),
            )
        )
    return tuple(fingerprint)


def _discard_record_tail(handle: BinaryIO) -> None:
    """Discard the remainder of one oversized record in bounded chunks."""
    while True:
        chunk = handle.readline(_DISCARD_CHUNK_BYTES)
        if not chunk or chunk.endswith(b"\n"):
            return


def iter_jsonl_dicts(
    paths: Iterable[Path],
    *,
    max_record_bytes: int = DEFAULT_MAX_JSONL_RECORD_BYTES,
) -> Iterator[dict[str, Any]]:
    """Yield valid object records from JSONL paths in the supplied order.

    Ignore missing, unreadable, malformed, and oversized records, matching the
    historical best-effort status-reader contract while bounding JSON decode.
    """
    record_limit = max(1, int(max_record_bytes))
    for path in paths:
        if not path.is_file():
            continue
        oversized_records = 0
        try:
            with path.open("rb") as handle:
                while True:
                    record = handle.readline(record_limit + 1)
                    if not record:
                        break
                    if len(record) > record_limit:
                        oversized_records += 1
                        if not record.endswith(b"\n"):
                            _discard_record_tail(handle)
                        continue
                    try:
                        payload = json.loads(record)
                    except Exception:
                        continue
                    if isinstance(payload, dict):
                        yield payload
        except Exception:
            continue
        if oversized_records:
            logger.debug(
                "Skipped %d oversized workflow activity records in %s (limit=%d bytes)",
                oversized_records,
                path,
                record_limit,
            )


def iter_jsonl_dicts_reverse(
    paths: Iterable[Path],
    *,
    max_record_bytes: int = DEFAULT_MAX_JSONL_RECORD_BYTES,
) -> Iterator[dict[str, Any]]:
    """Yield valid object records newest-first with bounded record reads.

    Scan newline offsets backwards without accumulating an unterminated or
    oversized record. Paths are treated as chronological and therefore read
    in reverse order as well. Concurrent appends after the initial size lookup
    belong to the next read, which is sufficient for best-effort telemetry.
    """
    record_limit = max(1, int(max_record_bytes))
    for path in reversed(tuple(paths)):
        if not path.is_file():
            continue
        try:
            file_size = int(path.stat().st_size)
            with path.open("rb") as handle:
                position = file_size
                record_end = file_size
                while position > 0:
                    chunk_start = max(0, position - _REVERSE_SCAN_CHUNK_BYTES)
                    handle.seek(chunk_start)
                    chunk = handle.read(position - chunk_start)
                    if not chunk:
                        break
                    offset = chunk.rfind(b"\n")
                    while offset >= 0:
                        record_start = chunk_start + offset + 1
                        record_length = record_end - record_start
                        if 0 < record_length <= record_limit:
                            handle.seek(record_start)
                            record = handle.read(record_length)
                            if len(record) == record_length:
                                try:
                                    payload = json.loads(record)
                                except Exception:
                                    pass
                                else:
                                    if isinstance(payload, dict):
                                        yield payload
                        record_end = chunk_start + offset
                        offset = chunk.rfind(b"\n", 0, offset)
                    position = chunk_start

                if 0 < record_end <= record_limit:
                    handle.seek(0)
                    record = handle.read(record_end)
                    if len(record) == record_end:
                        try:
                            payload = json.loads(record)
                        except Exception:
                            pass
                        else:
                            if isinstance(payload, dict):
                                yield payload
        except Exception:
            continue
