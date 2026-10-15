"""Compact verified baseline history while continuously auditing all preserved versions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.workflows.project_setup import baseline_transaction as transaction
from leanflow_cli.workflows.project_setup import history_archive, invocation_evidence
from leanflow_cli.workflows.prover.source import SourceConflictError


def all_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Keep preserved transaction membership and all retained versions continuously auditable."""
    base = transaction._base(root)
    result = [(path, transaction._load(path)) for path in transaction._history(base)]
    archive = base / "archive"
    membership = history_archive.members(archive)
    active_ids = {record["id"] for _, record in result}
    for identifier in sorted(membership):
        path = archive / f"{identifier}.json"
        history_archive.read(archive, identifier, membership)
        record = transaction._load(path)
        if identifier in active_ids:
            # A crash between preserving the archive and retiring the active
            # name must not discard or reinterpret either version.
            active = next(item for _, item in result if item["id"] == identifier)
            if active != record:
                raise SourceConflictError("Active and archived baseline journals differ")
            continue
        result.append((path, record))
    return result


def compact_history(root: Path, *, threshold: int) -> None:
    """Move only verified terminal journals out of the bounded active set, retaining all audits."""
    base = transaction._base(root)
    active = transaction._history(base)
    if len(active) < threshold:
        return
    if transaction.pending_issue(root):
        raise SourceConflictError("Baseline history requires recovery before compaction")
    for path in active:
        record = transaction._load(path)
        if path.name == "pending.json" or record["state"] not in {"committed", "aborted"}:
            continue
        if not transaction._closed(record):
            continue
        # An external acknowledgment permits a rebuild but never makes unknown
        # invocation inputs disposable or eligible for automatic compaction.
        physically_closed = all(
            invocation_evidence.load_record(root, identifier)[0].get("cleanup_verified") is True
            for identifier in record["invocations"]
        ) and all(
            value.get("cleanup_verified") is True
            for value, _ in invocation_evidence.records(Path(record["stage"])).values()
        )
        if not physically_closed:
            continue
        raw = read_regular_bytes(path, limit=transaction.LIMIT)
        history_archive.preserve(base / "archive", record["id"], raw)
        if read_regular_bytes(path, limit=transaction.LIMIT) != raw:
            raise SourceConflictError("Baseline journal changed during compaction")
        path.unlink()
    invocation_evidence.compact(root)
