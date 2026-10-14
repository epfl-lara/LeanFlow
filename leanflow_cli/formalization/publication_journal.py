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
from leanflow_cli.formalization.publication_loss import (
    expected_archive_hashes,
    observe_journal_loss,
    validate_loss,
)

STATES = {
    "PREPARING",
    "PREPARED",
    "PUBLISHING",
    "ROLLING_BACK",
    "COMMITTED",
    "ROLLED_BACK",
    "CONFLICTED",
    "RESOLVED",
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
        return {
            "schema_version": 1,
            "active": None,
            "transactions": {},
            "selected_revision": None,
            "conflict_queue": [],
        }
    value = read_json(path)
    if not isinstance(value.get("transactions"), dict) or value.get("active") not in {
        None,
        *value["transactions"],
    }:
        raise PublicationConflict("Invalid project publication registry")
    queue = value.setdefault("conflict_queue", [])
    if (
        not isinstance(queue, list)
        or any(not isinstance(key, str) or key not in value["transactions"] for key in queue)
        or len(queue) != len(set(queue))
    ):
        raise PublicationConflict("Invalid project publication conflict ownership")
    for key, record in value["transactions"].items():
        identifier(key)
        if not isinstance(record, dict) or not str(record.get("journal", "")).startswith(
            ".leanflow/"
        ):
            raise PublicationConflict("Invalid project publication transaction location")
        contained(root, record["journal"])
        path = contained(root, record["journal"])
        if record.get("loss"):
            validate_loss(key, record)
            continue
        if not path.is_file():
            continue
        journal = load_journal(path)
        if journal["transaction_id"] != key:
            raise PublicationConflict("Project registry transaction identity changed")
        if (
            journal["state"] not in {"COMMITTED", "ROLLED_BACK", "RESOLVED"}
            and value["active"] != key
            and not (journal["state"] == "CONFLICTED" and key in queue)
        ):
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
    if journal.get("retained_conflict_from") not in {None, "COMMITTED", "ROLLED_BACK", "RESOLVED"}:
        raise PublicationConflict("Invalid retained publication conflict origin")
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
    resolutions = journal.get("resolutions", [])
    if not isinstance(resolutions, list) or (journal["state"] == "RESOLVED" and not resolutions):
        raise PublicationConflict("Publication resolution evidence is missing")
    slots.update(expected_archive_hashes(journal))
    for resolution in resolutions:
        payload = {key: value for key, value in resolution.items() if key != "resolution_id"}
        observation = resolution.get("observation", {})
        if (
            resolution.get("resolution_id") != "resolution-" + identity(payload)
            or resolution.get("transaction_id") != journal["transaction_id"]
            or resolution.get("revision_id") != journal["revision_id"]
            or resolution.get("choice") != "keep_current"
            or resolution.get("requires_new_acceptance") is not True
            or observation.get("transaction_id") != journal["transaction_id"]
            or observation.get("observation_id")
            != identity(
                {key: value for key, value in observation.items() if key != "observation_id"}
            )
            or set(observation.get("destinations", {})) != set(journal["writes"])
            or set(observation.get("retained_slots", {})) - slots
        ):
            raise PublicationConflict("Publication resolution identity changed")
        expected_blobs = {}
        for group in ("destinations", "retained_slots"):
            for relative, checksum in observation[group].items():
                normalized_relative(relative)
                if checksum is not None:
                    if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
                        raise PublicationConflict(
                            "Publication resolution contains an invalid version"
                        )
                    expected_blobs[f"{group}/{relative}"] = checksum
        if resolution.get("version_blobs") != expected_blobs:
            raise PublicationConflict("Publication resolution version history changed")
        locations = resolution.get("version_locations", {})
        if not isinstance(locations, dict) or set(locations) - set(expected_blobs):
            raise PublicationConflict("Publication resolution archive locations changed")
        for name, relative in locations.items():
            normalized_relative(relative)
            if not re.fullmatch(
                r"resolution-archives/[a-f0-9]{32}/blobs/" + expected_blobs[name], relative
            ):
                raise PublicationConflict(
                    "Publication resolution archive escaped its owned directory"
                )


def load_journal(path: Path) -> dict[str, Any]:
    journal = read_json(path)
    validate_journal(journal)
    return journal


def current_journal(directory: Path, *, root: Path | None = None) -> Path | None:
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
    if value.get("journal_scope") == "project":
        expected = (
            f".leanflow/formalization-publication-transactions/{transaction_id}/publication.json"
        )
        if value.get("journal") != expected or root is None:
            raise PublicationConflict("Invalid project-owned publication journal pointer")
        return contained(root, expected)
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
    if journal["state"] == "RESOLVED":
        if (
            registry["active"] == journal["transaction_id"]
            or (registry.get("selected_revision") or {}).get("transaction_id")
            == journal["transaction_id"]
        ):
            registry["selected_revision"] = None
        resolution = journal["resolutions"][-1]
        revocation = {
            "transaction_id": journal["transaction_id"],
            "revision_id": journal["revision_id"],
            "resolution_id": resolution["resolution_id"],
            "reason": "Publication conflict resolved; a new mathematical acceptance is required",
        }
        revoked = registry.setdefault("revoked_revisions", [])
        if revocation not in revoked:
            revoked.append(revocation)
        registry["conflict_queue"] = [
            key
            for key in registry.setdefault("conflict_queue", [])
            if key != journal["transaction_id"]
        ]
    if (
        journal["state"] == "RESOLVED"
        or (journal["state"] in {"COMMITTED", "ROLLED_BACK"} and not journal.get("conflicts"))
    ) and registry["active"] == journal["transaction_id"]:
        registry["active"] = None
    if registry["active"] is None and registry.get("conflict_queue"):
        registry["active"] = registry["conflict_queue"][0]
    save_registry(root, registry)


def audit_retained(root: Path, registry: dict[str, Any], *, raise_conflicts: bool = True) -> None:
    """Surface late writes through retained inodes instead of silently orphaning user edits."""
    queue = registry.setdefault("conflict_queue", [])
    for key, record in registry["transactions"].items():
        path = contained(root, record["journal"])
        if record.get("loss") or not path.is_file():
            observe_journal_loss(root, registry, key)
            save_registry(root, registry)
            continue
        journal = load_journal(path)
        if journal["state"] == "CONFLICTED":
            if key not in queue:
                queue.append(key)
                save_registry(root, registry)
            continue
        if journal["state"] not in {"COMMITTED", "ROLLED_BACK", "RESOLVED"}:
            continue
        resolved = journal["state"] == "RESOLVED"
        acknowledged = (
            journal["resolutions"][-1]["observation"]["retained_slots"] if resolved else {}
        )
        issues = []
        latest = journal["resolutions"][-1] if resolved else {}
        archive_hashes = expected_archive_hashes(journal)
        archives_prepared = journal.get(
            "archives_prepared",
            journal["state"] == "COMMITTED"
            or any(entry["stage"] != "staged" for entry in journal["writes"].values()),
        )
        if not archives_prepared and not journal.get("resolutions"):
            archive_hashes = {}
        for slot, expected in archive_hashes.items():
            actual = entry_hash(entry_path(path.parent, slot))
            acknowledged_loss = latest.get("unavailable_versions", {}).get(
                slot
            ) == expected and actual == acknowledged.get(slot)
            if actual != expected and not acknowledged_loss:
                issues.append(
                    f"Publication archive unavailable or changed: {slot}; expected {expected}"
                )
        for relative, entry in journal["writes"].items():
            slots = [
                {"path": entry["captured"], "expected": entry["before"]},
                *entry.get("rollback_slots", []),
            ]
            if resolved:
                slots.append({"path": entry["staged"], "expected": entry["after"]})
            for slot in slots:
                actual = entry_hash(entry_path(path.parent, slot["path"]))
                expected = acknowledged.get(slot["path"], slot["expected"])
                missing_committed = (
                    journal["state"] == "COMMITTED"
                    and entry["stage"] == "installed"
                    and slot["path"] == entry["captured"]
                    and expected is not None
                    and actual is None
                )
                if (
                    (resolved and actual != expected)
                    or missing_committed
                    or (not resolved and actual is not None and actual != expected)
                ):
                    issue = f"Late user edit preserved for {relative} in {slot['path']}"
                    issues.append(issue)
        if resolved and key in queue and not issues:
            # A durable resolution can precede registry settlement, including
            # when another queued conflict owns the active pointer.
            settle_terminal(root, registry, journal)
            queue = registry["conflict_queue"]
            continue
        if key in queue and not issues:
            # Ownership is saved before the reopened journal. A crash at that
            # boundary must remain inspectable rather than silently admit work.
            issues.append("Retained-version audit was interrupted; inspect all versions")
        if issues or (journal.get("conflicts") and not resolved):
            for issue in issues:
                if issue not in journal["conflicts"]:
                    journal["conflicts"].append(issue)
            if key not in queue:
                queue.append(key)
            if registry["active"] is None:
                registry["active"] = key
            save_registry(root, registry)
            journal["retained_conflict_from"] = journal["state"]
            journal["state"] = "CONFLICTED"
            save_journal(path, journal)
    if queue and raise_conflicts:
        entry = registry["transactions"][queue[0]]
        first = entry.get("loss") or load_journal(contained(root, entry["journal"]))
        raise PublicationConflict(
            (first["conflicts"][0] if first["conflicts"] else "Publication conflict unresolved")
            + "; inspect queued transactions: "
            + ", ".join(queue)
        )
