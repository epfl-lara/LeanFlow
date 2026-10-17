"""Persist bounded archive intents without reconstructing authority from a lost full journal."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    identifier,
    identity,
    sync_directory,
)


def _directory(directory: Path, journal: dict[str, Any]) -> Path:
    """Bind record paths to the existing full transaction and immutable plan."""
    transaction = identifier(journal["transaction_id"])
    plan_id = journal.get("plan_id")
    if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{64}", plan_id):
        raise ValueError("Preparation archive intent plan is invalid")
    return contained(directory, f"archive-intents/{transaction}/{plan_id}")


def _publish_record(path: Path, value: dict[str, Any]) -> None:
    """Publish flushed immutable metadata exclusively and preserve a concurrent creator's bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".archive-intent-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(
                (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            )
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
            sync_directory(path.parent)
        except FileExistsError:
            checked = contained(path.parent, path.name)
            if not checked.is_file() or json.loads(checked.read_bytes()) != value:
                raise ValueError("Existing preparation archive intent changed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_archive_intent(directory: Path, journal: dict[str, Any], intent: dict[str, Any]) -> None:
    """Register one immutable target before creating bytes without rewriting the full plan."""
    path = _directory(directory, journal) / (digest(intent["path"]) + ".json")
    payload = {
        "schema_version": 1,
        "transaction_id": journal["transaction_id"],
        "plan_id": journal["plan_id"],
        "root": journal["root"],
        "intent": intent,
    }
    value = {**payload, "record_id": identity(payload)}
    _publish_record(path, value)


def apply_archive_intents(directory: Path, journal: dict[str, Any]) -> None:
    """Overlay records only after the immutable full plan remains available and exact."""
    if journal.get("plan_id") != identity(journal.get("plan")):
        raise ValueError("Preparation archive intent lost its full immutable plan")
    parent = _directory(directory, journal)
    if not parent.exists():
        return
    if not parent.is_dir():
        raise ValueError("Preparation archive intent history is unreadable")
    rows = journal.setdefault("archive_copy_intents", [])
    if not isinstance(rows, list):
        raise ValueError("Preparation archive intent snapshot is invalid")
    known = {row["path"]: row for row in rows}
    for path in sorted(parent.iterdir()):
        if path.name.startswith(".archive-intent-"):
            continue
        checked = contained(directory, path.relative_to(directory).as_posix())
        if not checked.is_file():
            raise ValueError("Preparation archive intent history contains an unknown entry")
        value = json.loads(checked.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("Preparation archive intent record is invalid")
        intent = value.get("intent")
        if (
            set(value)
            != {"schema_version", "transaction_id", "plan_id", "root", "intent", "record_id"}
            or value.get("schema_version") != 1
            or value.get("transaction_id") != journal["transaction_id"]
            or value.get("plan_id") != journal["plan_id"]
            or value.get("root") != journal["root"]
            or value.get("record_id")
            != identity({key: item for key, item in value.items() if key != "record_id"})
            or not isinstance(intent, dict)
            or not isinstance(intent.get("path"), str)
            or path.name != digest(intent["path"]) + ".json"
            or (intent["path"] in known and known[intent["path"]] != intent)
        ):
            raise ValueError("Preparation archive intent record identity changed")
        if intent["path"] not in known:
            rows.append(intent)
            known[intent["path"]] = intent


def archive_intent_assets(directory: Path, transaction_id: str) -> dict[str, str]:
    """Bind surviving raw records for loss acknowledgment without reconstructing their plan."""
    parent = contained(directory, f"archive-intents/{identifier(transaction_id)}")
    if not parent.exists():
        return {}
    if not parent.is_dir():
        raise ValueError("Preparation archive intent history is not a directory")
    result = {}
    for path in sorted(parent.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        checked = contained(directory, relative)
        if checked.is_file():
            result[relative] = digest(checked.read_bytes())
        elif not checked.is_dir():
            raise ValueError("Preparation archive intent history contains an unknown entry")
    return result
