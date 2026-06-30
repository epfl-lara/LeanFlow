#!/usr/bin/env python3
"""Read-before-edit freshness tracking.

Implements the D2 freshness contract: edits should be grounded in content the
agent has actually seen. We record a content hash whenever a file is read and
consult it before a patch is applied so a stale-context edit (the file changed
on disk since the agent last read it) can be rejected before it overwrites
newer content.

Two verdicts, deliberately asymmetric so we never break workflows that legitimately
patch without a prior read while still catching the genuinely dangerous case:

  * "never_read" -> a *soft* warning. The agent may have written the file itself,
    or be applying a known patch; we surface a nudge to read first but allow it.
  * "stale"      -> a *hard* reject. The on-disk content no longer matches what
    the agent last read, so the edit is almost certainly based on a stale view.

Usage:
    from tools.utilities.read_freshness import (
        record_read, check_freshness, FreshnessVerdict,
    )

    record_read(task_id, path, content)                  # after read_file
    verdict = check_freshness(task_id, path, on_disk)     # before patch
    if verdict.status == "stale":
        ...  # reject, tell the model to re-read
"""

import hashlib
import os
import threading
from dataclasses import dataclass

# Per (task_id, normalized_path) -> sha256 of the raw content last read.
_freshness_lock = threading.Lock()
_read_hashes: dict[tuple[str, str], str] = {}


@dataclass
class FreshnessVerdict:
    """Outcome of a read-before-edit freshness check.

    status is one of:
      "fresh"      -> hash matches the last read (or no tracking applies)
      "never_read" -> file was never read in this task (soft warning)
      "stale"      -> on-disk content changed since the last read (hard reject)
    """

    status: str
    message: str | None = None


def _normalize(path: str) -> str:
    """Normalize a path so the same file hashes to the same key across calls.

    Resolves to an absolute path (expanduser for ~, abspath for relative -> cwd-relative
    absolute, which also normpaths) so that reading ``src/F.lean`` and later patching the same
    file by its absolute path map to the SAME key. Symlinks are deliberately not resolved — the
    read and patch paths only need to agree, and both flow through this function.
    """
    return os.path.abspath(os.path.expanduser(str(path)))


def hash_text(content: str) -> str:
    """Return a stable sha256 hex digest of file content."""
    return hashlib.sha256(content.encode("utf-8", errors="surrogatepass")).hexdigest()


def record_read(task_id: str, path: str, content: str) -> None:
    """Record the hash of content the agent just read for this file."""
    key = (task_id or "default", _normalize(path))
    with _freshness_lock:
        _read_hashes[key] = hash_text(content)


def check_freshness(task_id: str, path: str, current_content: str) -> FreshnessVerdict:
    """Compare on-disk content against the last read for this (task, path).

    Returns a verdict the caller uses to warn (never_read) or reject (stale).
    """
    key = (task_id or "default", _normalize(path))
    with _freshness_lock:
        last_hash = _read_hashes.get(key)

    if last_hash is None:
        return FreshnessVerdict(
            status="never_read",
            message=(
                f"You are editing {path} without having read it in this session. "
                "Read the file first so your edit is based on its current content."
            ),
        )

    if hash_text(current_content) != last_hash:
        return FreshnessVerdict(
            status="stale",
            message=(
                f"{path} changed on disk since you last read it. Your edit would be "
                "based on stale content. Re-read the file with read_file, then redo "
                "your edit against the current content."
            ),
        )

    return FreshnessVerdict(status="fresh")


def note_write(task_id: str, path: str, content: str) -> None:
    """Update the tracked hash after the tool itself writes the file.

    Keeps a subsequent edit-in-the-same-region flow from tripping "stale" on the
    agent's own just-applied change. Mirrors record_read for the post-write state.
    """
    record_read(task_id, path, content)


def clear_freshness(task_id: str | None = None) -> None:
    """Clear freshness tracking for one task, or all tasks when task_id is None."""
    with _freshness_lock:
        if task_id is None:
            _read_hashes.clear()
            return
        for key in [k for k in _read_hashes if k[0] == task_id]:
            _read_hashes.pop(key, None)
