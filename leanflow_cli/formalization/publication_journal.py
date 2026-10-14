"""Persist project-wide publication ownership and validate recoverable transaction records."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import (
    contained,
    durable_json,
    identifier,
    identity,
    normalized_relative,
)
from leanflow_cli.formalization.publication_io import PublicationConflict, entry_hash, entry_path

STATES = {
    "PREPARING",
    "PREPARED",
    "PUBLISHING",
    "ROLLING_BACK",
    "COMMITTED",
    "ROLLED_BACK",
    "CONFLICTED",
}


def read_json(path: Path) -> dict[str, Any]:
    """Reject corrupt control records without guessing a transaction state."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise PublicationConflict(f"Publication evidence is unreadable: {path}") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise PublicationConflict(f"Unsupported publication evidence: {path}")
    return value


def registry_path(root: Path) -> Path:
    return contained(root, ".leanflow/formalization-publications.json")


def load_registry(root: Path) -> dict[str, Any]:
    """Read the project authority shared by every revision and consumer."""
    path = registry_path(root)
    if not path.exists():
        return {"schema_version": 1, "active": None, "transactions": {}, "selected_revision": None}
    value = read_json(path)
    if not isinstance(value.get("transactions"), dict) or value.get("active") not in {
        None,
        *value["transactions"],
    }:
        raise PublicationConflict("Invalid project publication registry")
    for key, record in value["transactions"].items():
        identifier(key)
        if not isinstance(record, dict) or not str(record.get("journal", "")).startswith(
            ".leanflow/"
        ):
            raise PublicationConflict("Invalid project publication transaction location")
        contained(root, record["journal"])
        journal = load_journal(contained(root, record["journal"]))
        if journal["transaction_id"] != key:
            raise PublicationConflict("Project registry transaction identity changed")
        if journal["state"] not in {"COMMITTED", "ROLLED_BACK"} and value["active"] != key:
            raise PublicationConflict("Project registry lost pending publication ownership")
    return value


def save_registry(root: Path, registry: dict[str, Any]) -> None:
    durable_json(registry_path(root), registry)


def validate_journal(journal: dict[str, Any]) -> None:
    """Validate recovery identities and every recorded slot before acting on paths."""
    identifier(journal.get("transaction_id", ""))
    identifier(journal.get("revision_id", ""))
    if journal.get("state") not in STATES or journal.get("status") != journal["state"]:
        raise PublicationConflict("Invalid publication transaction state")
    plan = journal.get("plan")
    if not isinstance(plan, dict):
        raise PublicationConflict("Publication has no frozen plan")
    plan_payload = {key: value for key, value in plan.items() if key != "plan_id"}
    if plan.get("plan_id") != identity(plan_payload) or journal.get("plan_id") != plan["plan_id"]:
        raise PublicationConflict("Publication plan identity changed")
    if plan.get("revision_id") != journal["revision_id"] or set(journal.get("writes", {})) != set(
        plan.get("writes", {})
    ):
        raise PublicationConflict("Publication write set changed")
    if journal.get("order") != sorted(journal["writes"]):
        raise PublicationConflict("Publication mutation order changed")
    if not isinstance(journal.get("conflicts"), list) or any(
        not isinstance(issue, str) for issue in journal["conflicts"]
    ):
        raise PublicationConflict("Invalid publication conflict record")
    aliases: set[str] = set()
    slots: set[str] = set()
    for relative, entry in journal["writes"].items():
        normalized_relative(relative)
        if relative.casefold() in aliases:
            raise PublicationConflict("Publication contains colliding path spellings")
        aliases.add(relative.casefold())
        if entry.get("stage") not in {
            "staged",
            "capture_pending",
            "captured",
            "install_pending",
            "installed",
            "rollback_remove_pending",
            "rollback_removed",
            "restore_pending",
            "restored",
        }:
            raise PublicationConflict("Invalid publication entry state")
        if any(entry.get(key) != plan["writes"][relative].get(key) for key in ("before", "after")):
            raise PublicationConflict("Publication before/after identity changed")
        for key in ("before", "after"):
            if entry[key] is not None and not re.fullmatch(r"[a-f0-9]{64}", entry[key]):
                raise PublicationConflict("Publication contains an invalid content identity")
        for key, prefix in (("captured", "displaced/"), ("staged", "staging/")):
            value = normalized_relative(entry.get(key, ""))
            if not value.startswith(prefix):
                raise PublicationConflict("Publication recovery slot escaped its owned directory")
            if value in slots:
                raise PublicationConflict("Publication recovery slots overlap")
            slots.add(value)
        for slot in entry.get("rollback_slots", []):
            value = normalized_relative(slot.get("path", ""))
            if (
                not value.startswith("displaced/")
                or value in slots
                or slot.get("expected") != entry["after"]
            ):
                raise PublicationConflict("Invalid rollback slot")
            slots.add(value)
    for entry in journal.get("created_dirs", []):
        value = normalized_relative(entry["path"])
        if not any(path.startswith(value + "/") for path in journal["writes"]):
            raise PublicationConflict("Unrelated project directory in publication journal")


def load_journal(path: Path) -> dict[str, Any]:
    journal = read_json(path)
    validate_journal(journal)
    return journal


def current_journal(directory: Path) -> Path | None:
    """Resolve the latest per-revision pointer without discarding older attempts."""
    pointer = contained(directory, "publication.json")
    if not pointer.exists():
        return None
    value = read_json(pointer)
    if "state" in value:
        raise PublicationConflict(
            "Legacy publication journal requires explicit migration before recovery"
        )
    transaction_id = identifier(value.get("transaction_id", ""))
    expected = f"transactions/{transaction_id}/publication.json"
    if value.get("journal") != expected:
        raise PublicationConflict("Invalid publication journal pointer")
    return contained(directory, expected)


def save_journal(
    path: Path, journal: dict[str, Any], checkpoint: Callable[[str], None] | None = None
) -> None:
    """Write the authority before exposing a new mutation boundary."""
    journal["status"] = journal["state"]
    durable_json(path, journal)
    if checkpoint:
        checkpoint(f"journal:{journal['state']}")


def settle_terminal(root: Path, registry: dict[str, Any], journal: dict[str, Any]) -> None:
    """Clear active ownership only after its durable terminal marker exists."""
    if (
        journal["state"] == "COMMITTED"
        and not journal.get("conflicts")
        and registry["active"] == journal["transaction_id"]
    ):
        registry["selected_revision"] = {
            key: journal[key] for key in ("transaction_id", "revision_id", "plan_id")
        }
    if (
        journal["state"] in {"COMMITTED", "ROLLED_BACK"}
        and not journal.get("conflicts")
        and registry["active"] == journal["transaction_id"]
    ):
        registry["active"] = None
    save_registry(root, registry)


def audit_retained(root: Path, registry: dict[str, Any]) -> None:
    """Surface late writes through retained inodes instead of silently orphaning user edits."""
    for key, record in registry["transactions"].items():
        path = contained(root, record["journal"])
        journal = load_journal(path)
        if journal["state"] not in {"COMMITTED", "ROLLED_BACK"}:
            continue
        for relative, entry in journal["writes"].items():
            slots = [
                {"path": entry["captured"], "expected": entry["before"]},
                *entry.get("rollback_slots", []),
            ]
            for slot in slots:
                actual = entry_hash(entry_path(path.parent, slot["path"]))
                if actual is not None and actual != slot["expected"]:
                    issue = f"Late user edit preserved for {relative} in {slot['path']}"
                    if issue not in journal["conflicts"]:
                        journal["conflicts"].append(issue)
                        save_journal(path, journal)
                    if registry["active"] is None:
                        registry["active"] = key
                    save_registry(root, registry)
                    raise PublicationConflict(f"{issue}; inspect {path}")
        if journal.get("conflicts"):
            raise PublicationConflict(f"Publication conflict remains unresolved; inspect {path}")
