"""Persist bounded run references before prover source inode mutations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from core.utils import atomic_json_write
from leanflow_cli.formalization.evidence import sync_directory
from leanflow_cli.workflows.prover.source import SourceConflictError

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def save_journal(runtime: ProverRuntime, journal: dict[str, Any]) -> None:
    """Flush a bounded run pointer before a project-owned source capture can move."""
    if len(json.dumps(journal).encode("utf-8")) > 64 * 1024 * 1024:
        raise ValueError("Prover source transaction exceeds its 64 MiB recovery limit")
    atomic_json_write(runtime.store.directory / "source-transaction.json", journal)
    sync_directory(runtime.store.directory)


def load_journal(runtime: ProverRuntime) -> dict[str, Any] | None:
    """Reject unreadable, linked, or oversized transaction authority before source I/O."""
    path = runtime.store.directory / "source-transaction.json"
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise SourceConflictError("Prover source transaction journal is unavailable or oversized")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid prover source transaction")
    return value
