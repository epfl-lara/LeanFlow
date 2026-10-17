"""Retain authenticated append-only receipt events behind revision-local projections."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from leanflow_cli.formalization.evidence import contained, durable_json, identifier, identity
from leanflow_cli.formalization.publication_case_paths import assert_receipt_control_path


def _event_path(directory: Path, revision: str, receipt_id: str) -> Path:
    return contained(
        directory, f"receipt-history/{identifier(revision)}/{identifier(receipt_id)}.json"
    )


def _event(directory: Path, revision: str, receipt_id: str) -> dict[str, Any]:
    """Authenticate a stored receipt event independently of its mutable projection."""
    assert_receipt_control_path(directory, f"receipt-history/{revision}/{receipt_id}.json")
    value = json.loads(_event_path(directory, revision, receipt_id).read_text())
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "revision_id",
        "previous_receipt_id",
        "payload",
        "receipt_id",
    }:
        raise ValueError("Invalid formalization receipt event")
    if (
        value["schema_version"] != 1
        or value["revision_id"] != revision
        or value["receipt_id"] != receipt_id
        or receipt_id
        != "receipt-" + identity({key: item for key, item in value.items() if key != "receipt_id"})
        or not isinstance(value["payload"], dict)
        or value["payload"].get("revision_id") != revision
        or "receipt_id" in value["payload"]
    ):
        raise ValueError("Formalization receipt event bytes differ from their identity")
    previous = value["previous_receipt_id"]
    if previous is not None:
        identifier(previous)
    return value


def receipt_events(record: dict[str, Any], receipt_id: str | None = None) -> list[dict[str, Any]]:
    """Authenticate the complete predecessor chain and return events in chronological order."""
    directory = Path(record["_directory"])
    revision = identifier(record["revision_id"])
    if receipt_id is None:
        projection = _projection(directory, revision)
        if projection is None:
            return []
        receipt_id = projection["receipt_id"]
    events = []
    seen: set[str] = set()
    while receipt_id is not None:
        if receipt_id in seen:
            raise ValueError("Formalization receipt history contains a cycle")
        seen.add(receipt_id)
        event = _event(directory, revision, receipt_id)
        events.append(event)
        receipt_id = event["previous_receipt_id"]
    return list(reversed(events))


def _projection(directory: Path, revision: str) -> dict[str, Any] | None:
    path = contained(directory, f"receipts/{revision}.json")
    if not path.is_file():
        return None
    assert_receipt_control_path(directory, f"receipts/{revision}.json")
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("revision_id") != revision
        or not isinstance(value.get("receipt_id"), str)
    ):
        raise ValueError("Formalization receipt projection has no authenticated event")
    return value


def load_receipt(record: dict[str, Any]) -> dict[str, Any] | None:
    """Read the selected receipt only after authenticating its full retained history."""
    directory = Path(record["_directory"])
    revision = identifier(record["revision_id"])
    projection = _projection(directory, revision)
    if projection is None:
        return None
    events = receipt_events(record, projection["receipt_id"])
    if projection != {**events[-1]["payload"], "receipt_id": events[-1]["receipt_id"]}:
        raise ValueError("Formalization receipt projection differs from its immutable event")
    return projection


@contextmanager
def _receipt_lock(directory: Path) -> Iterator[None]:
    """Serialize receipt appends without requiring or reentering the document lock."""
    assert_receipt_control_path(directory, "receipt-history.lock")
    directory.mkdir(parents=True, exist_ok=True)
    with contained(directory, "receipt-history.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _store_event(directory: Path, event: dict[str, Any]) -> None:
    assert_receipt_control_path(
        directory, f"receipt-history/{event['revision_id']}/{event['receipt_id']}.json"
    )
    path = _event_path(directory, event["revision_id"], event["receipt_id"])
    if path.exists():
        if json.loads(path.read_text()) != event:
            raise ValueError("An immutable formalization receipt event was modified")
    else:
        durable_json(path, event)


def save_receipt(
    target: dict[str, Any] | Path, value: dict[str, Any], *, select: bool = True
) -> dict[str, Any]:
    """Append a receipt or select a prepared event using predecessor compare-and-swap.

    A returned receipt_id identifies immutable payload bytes. To create a changed
    receipt, omit that ID; to select a prepared receipt, pass it back unchanged.
    Preparing publication with select=False leaves the staged receipt selected.
    """
    directory = Path(target["_directory"]) if isinstance(target, dict) else target
    revision = identifier(value["revision_id"])
    if isinstance(target, dict) and target["revision_id"] != revision:
        raise ValueError("Receipt does not bind its target revision")
    record = {"_directory": str(directory), "revision_id": revision}
    with _receipt_lock(directory):
        current = load_receipt(record)
        previous = current["receipt_id"] if current else None
        if "receipt_id" in value:
            history = receipt_events(record, value["receipt_id"])
            event = history[-1]
            if value != {**event["payload"], "receipt_id": event["receipt_id"]}:
                raise ValueError("Prepared receipt payload differs from its immutable event")
            if select and previous not in {event["previous_receipt_id"], event["receipt_id"]}:
                raise ValueError("Selected receipt changed before prepared receipt selection")
        elif current and value == {
            key: item for key, item in current.items() if key != "receipt_id"
        }:
            return current
        else:
            content = {
                "schema_version": 1,
                "revision_id": revision,
                "previous_receipt_id": previous,
                "payload": value,
            }
            event = {**content, "receipt_id": "receipt-" + identity(content)}
            _store_event(directory, event)
        projection = {**event["payload"], "receipt_id": event["receipt_id"]}
        if select:
            durable_json(contained(directory, f"receipts/{revision}.json"), projection)
        return projection


def copy_receipts(original: Path, destination: Path) -> None:
    """Copy authenticated history while retaining any existing destination selection.

    Private selections may have advanced since publication. A committed staged
    projection is reconciled separately with transaction evidence and a CAS.
    """
    for name in ("receipt-history", "receipts"):
        assert_receipt_control_path(original, name)
        assert_receipt_control_path(destination, name)
    history = contained(original, "receipt-history")
    if history.exists():
        for revision_path in sorted(history.iterdir()):
            revision = identifier(revision_path.name)
            parent = contained(original, f"receipt-history/{revision}")
            if not parent.is_dir():
                raise ValueError("Invalid receipt history revision directory")
            for path in sorted(parent.iterdir()):
                record = {"_directory": str(original), "revision_id": revision}
                for event in receipt_events(record, identifier(path.stem)):
                    _store_event(destination, event)
    receipts = contained(original, "receipts")
    if receipts.exists():
        for path in sorted(receipts.iterdir()):
            revision = identifier(path.stem)
            receipt = load_receipt({"_directory": str(original), "revision_id": revision})
            if receipt is None:
                raise ValueError("Receipt projection disappeared during rehydration")
            with _receipt_lock(destination):
                target = contained(destination, f"receipts/{revision}.json")
                if target.exists():
                    load_receipt({"_directory": str(destination), "revision_id": revision})
                else:
                    durable_json(target, receipt)
