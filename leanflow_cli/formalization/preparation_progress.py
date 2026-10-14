"""Persist bounded entry progress independently of immutable preparation plans and receipts."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    durable_json,
    identifier,
    identity,
    normalized_relative,
)

ENTRY_FIELDS = {"stage", "incoming_inode", "rollback_slots", "incoming_disposal"}


def _generation(journal: dict[str, Any]) -> str | None:
    """Reject progress that cannot belong to one atomic full snapshot."""
    value = journal.get("progress_generation")
    if value is not None and (
        not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value)
    ):
        raise ValueError("Preparation progress generation changed")
    return value


def _entry_path(directory: Path, journal: dict[str, Any], relative: str) -> Path:
    """Bind one mutable entry to the current snapshot and frozen transaction identity."""
    transaction = identifier(journal["transaction_id"])
    generation = _generation(journal)
    if generation is None:
        raise ValueError("Preparation entry progress requires an authoritative snapshot")
    normalized_relative(relative)
    if relative not in journal["writes"]:
        raise ValueError("Preparation progress contains an unknown write")
    return contained(directory, f"progress/{transaction}/{generation}/{digest(relative)}.json")


def save_snapshot(directory: Path, journal: dict[str, Any]) -> None:
    """Rotate progress only after a complete replacement snapshot is durable.

    Old progress stays available if a process dies before the new snapshot is
    published. A successful snapshot already contains every applied entry and
    references an empty generation, so older progress cannot override recovery.
    """
    transaction = identifier(journal["transaction_id"])
    generation = uuid.uuid4().hex
    value = {**journal, "progress_generation": generation}
    durable_json(contained(directory, f"transactions/{transaction}.json"), value)
    journal["progress_generation"] = generation


def save_entry_progress(
    directory: Path,
    journal: dict[str, Any],
    relative: str,
    checkpoint: Callable[[str], None] | None = None,
) -> None:
    """Persist one complete mutable row before exposing its filesystem checkpoint."""
    if _generation(journal) is None:
        # A legacy transaction receives one full snapshot before incremental use.
        save_snapshot(directory, journal)
    row = journal["writes"][relative]
    payload = {
        "schema_version": 1,
        "transaction_id": journal["transaction_id"],
        "plan_id": journal["plan_id"],
        "generation": journal["progress_generation"],
        "state": journal["state"],
        "relative": relative,
        "entry": {key: value for key, value in row.items() if key in ENTRY_FIELDS},
    }
    durable_json(
        _entry_path(directory, journal, relative),
        {**payload, "progress_id": identity(payload)},
    )
    if checkpoint:
        checkpoint("journal:" + journal["state"])


def apply_entry_progress(directory: Path, journal: dict[str, Any]) -> None:
    """Overlay current progress before ordinary journal path, inode, and state validation.

    This requires the full journal: entry records cannot reconstruct a missing
    immutable plan, receipt, source-protection map, or controller ownership.
    """
    generation = _generation(journal)
    if generation is None:
        return
    transaction = identifier(journal["transaction_id"])
    parent = contained(directory, f"progress/{transaction}/{generation}")
    if not parent.exists():
        return
    if not parent.is_dir() or not isinstance(journal.get("writes"), dict):
        raise ValueError("Preparation progress ownership is unreadable")
    expected_names = {digest(relative) + ".json" for relative in journal["writes"]}
    if any(path.name not in expected_names for path in parent.glob("*.json")):
        raise ValueError("Preparation progress contains an unknown write")
    for relative, row in journal["writes"].items():
        path = _entry_path(directory, journal, relative)
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise ValueError("Preparation entry progress is unreadable") from error
        if not isinstance(value, dict):
            raise ValueError("Preparation entry progress is invalid")
        payload = {key: item for key, item in value.items() if key != "progress_id"}
        entry = value.get("entry")
        if (
            value.get("schema_version") != 1
            or value.get("transaction_id") != transaction
            or value.get("plan_id") != journal["plan_id"]
            or value.get("generation") != generation
            or value.get("state") != journal["state"]
            or value.get("relative") != relative
            or value.get("progress_id") != identity(payload)
            or not isinstance(entry, dict)
            or set(entry) - ENTRY_FIELDS
            or not {"stage", "rollback_slots"} <= set(entry)
            or not isinstance(row, dict)
        ):
            raise ValueError("Preparation entry progress identity changed")
        for key in ENTRY_FIELDS:
            row.pop(key, None)
        row.update(entry)


def progress_assets(directory: Path, transaction_id: str) -> dict[str, str]:
    """Hash surviving progress assets for exact loss acknowledgment without reconstructing authority."""
    transaction = identifier(transaction_id)
    parent = contained(directory, f"progress/{transaction}")
    if not parent.exists():
        return {}
    if not parent.is_dir():
        raise ValueError("Preparation progress history is not a directory")
    result = {}
    for path in sorted(parent.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        checked = contained(directory, relative)
        if checked.is_file():
            result[relative] = digest(checked.read_bytes())
        elif not checked.is_dir():
            raise ValueError("Preparation progress history contains an unknown entry")
    return result
