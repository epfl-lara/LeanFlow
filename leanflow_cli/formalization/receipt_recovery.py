"""Reconcile the private receipt projection after an already committed publication."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, digest, identifier
from leanflow_cli.formalization.publication_journal import load_journal, load_registry
from leanflow_cli.formalization.receipt_history import (
    copy_receipts,
    load_receipt,
    receipt_events,
    save_receipt,
)


def reconcile_committed_receipt(root: Path, record: dict[str, Any]) -> None:
    """Select only a committed published child of the currently staged receipt.

    A rejection, another selected revision, or a later receipt branch remains
    authoritative. This repairs a crash after durable COMMITTED but before the
    private published projection was selected; it never creates review evidence.
    """
    revision = identifier(record["revision_id"])
    document = identifier(record["document_id"])
    private = Path(record["_directory"])
    portable = contained(root, f"formalization/{document}")
    if private == portable or not (portable / "current.json").is_file():
        return
    selected = private / "current.json"
    if not selected.is_file() or json.loads(selected.read_text()).get("revision_id") != revision:
        return
    if json.loads((portable / "current.json").read_text()).get("revision_id") != revision:
        return
    current = load_receipt(record)
    if not current or current.get("status") != "accepted_staged":
        return
    remote = {**record, "_directory": str(portable)}
    published = load_receipt(remote)
    if not published or published.get("status") != "published":
        return
    event = receipt_events(remote)[-1]
    if event["previous_receipt_id"] != current["receipt_id"]:
        return
    if {k: v for k, v in current.items() if k not in {"status", "receipt_id"}} != {
        k: v for k, v in published.items() if k not in {"status", "receipt_id"}
    }:
        raise ValueError("Committed publication receipt differs from staged review evidence")
    expected = {
        f"formalization/{document}/receipts/{revision}.json": digest(
            (portable / "receipts" / f"{revision}.json").read_bytes()
        ),
        f"formalization/{document}/current.json": digest((portable / "current.json").read_bytes()),
    }
    registry = load_registry(root)
    committed = False
    for entry in registry["transactions"].values():
        journal = load_journal(contained(root, entry["journal"]))
        if (
            journal["state"] == "COMMITTED"
            and not journal["conflicts"]
            and journal["revision_id"] == revision
        ):
            if all(
                journal["writes"].get(name, {}).get("after") == value
                for name, value in expected.items()
            ):
                committed = True
                break
    if not committed:
        return
    copy_receipts(portable, private)
    try:
        save_receipt(record, published)
    except ValueError:
        # Another controller may reject this candidate while history is copied.
        # Preserve its later decision instead of replaying an older acceptance.
        if load_receipt(record) != current:
            return
        raise
