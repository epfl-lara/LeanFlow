"""Persist independent setup ownership, immutable plans, and retained-version history."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import contained, durable_json, identifier, identity
from leanflow_cli.formalization.preparation_audit import retained_identity
from leanflow_cli.formalization.preparation_errors import (
    PreparationAuthorityLoss,
)
from leanflow_cli.formalization.preparation_errors import (
    PreparationConflict as PreparationConflict,
)
from leanflow_cli.formalization.preparation_fs_inputs import (
    entry_identity,
    managed_path,
)
from leanflow_cli.formalization.preparation_history import MAX_CONTROL_BYTES, discover_history
from leanflow_cli.formalization.preparation_loss import observe_journal_loss, validate_loss
from leanflow_cli.formalization.preparation_progress import apply_entry_progress, save_snapshot
from leanflow_cli.formalization.preparation_registry_loss import validate_acknowledgment
from leanflow_cli.formalization.preparation_resolution import (
    archive_intents,
    resolution_archive_paths,
)
from leanflow_cli.formalization.preparation_resolution_progress import apply_archive_intents
from leanflow_cli.formalization.publication_io import entry_path

TERMINAL = {"COMMITTED", "ROLLED_BACK", "RESOLVED"}
STATES = TERMINAL | {"PREPARING", "PREPARED", "PUBLISHING", "ROLLING_BACK", "CONFLICTED"}


def precommit_cleanup(journal: dict[str, Any]) -> dict[str, Any] | None:
    """Validate stopped private acquisition/check scope independently of a default usage flag."""
    marker = journal.get("plan", {}).get("receipt", {}).get("precommit_cleanup")
    if marker is None:
        return None
    stage = marker.get("stage", {}) if isinstance(marker, dict) else {}
    if (
        not isinstance(marker, dict)
        or marker.get("schema_version") != 1
        or marker.get("phase") != "PRECOMMIT"
        or marker.get("cleanup_verified") is not True
        or marker.get("owner") != journal.get("owner")
        or marker.get("scope") != "private acquisition and independent preparation checks"
        or not isinstance(marker.get("stage_id"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", marker["stage_id"])
        or not isinstance(stage, dict)
        or stage.get("path")
        != str(
            preparation_directory(Path(journal["root"])) / "stages" / marker["stage_id"] / "project"
        )
        or any(type(stage.get(key)) is not int or stage[key] < 0 for key in ("device", "inode"))
        or marker.get("marker_id")
        != identity({key: value for key, value in marker.items() if key != "marker_id"})
    ):
        raise PreparationConflict("Preparation precommit child cleanup evidence changed")
    return marker


def preparation_directory(root: Path) -> Path:
    """Locate home-owned infrastructure authority independently of translation publication."""
    key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()
    return leanflow_home() / "formalization-preparation" / key


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_regular_bytes(path, MAX_CONTROL_BYTES))
    except (OSError, ValueError) as error:
        _raise_transient_io(error)
        raise PreparationConflict("Preparation authority is unreadable") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise PreparationConflict("Preparation authority is unsupported")
    return value


def _raise_transient_io(error: BaseException) -> None:
    """Keep temporary I/O failures from becoming durable loss through parser wrappers."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and not (
            isinstance(current, (FileNotFoundError, IsADirectoryError))
            or current.errno in {errno.ENOENT, errno.ENOTDIR, errno.EISDIR}
        ):
            raise current
        current = current.__cause__


def load_journal(root: Path, transaction_id: str) -> dict[str, Any]:
    """Validate every mutation path and immutable preparation plan before recovery."""
    transaction_id = identifier(transaction_id)
    value = _read(contained(preparation_directory(root), f"transactions/{transaction_id}.json"))
    try:
        apply_entry_progress(preparation_directory(root), value)
        apply_archive_intents(preparation_directory(root), value)
    except ValueError as error:
        raise PreparationConflict(str(error)) from error
    plan = value.get("plan", {})
    if (
        value.get("root") != str(root.resolve())
        or value.get("transaction_id") != transaction_id
        or value.get("state") not in STATES
        or value.get("status") != value["state"]
        or value.get("plan_id") != identity(plan)
        or plan.get("transaction_id") != transaction_id
        or plan.get("root") != value["root"]
        or not isinstance(value.get("writes"), dict)
        or set(value["writes"]) != set(plan.get("writes", {}))
        or value.get("order") != sorted(value["writes"])
        or not isinstance(value.get("conflicts"), list)
        or any(not isinstance(item, str) for item in value["conflicts"])
    ):
        raise PreparationConflict("Preparation journal identity changed")
    owner = value.get("owner")
    if owner is not None and (
        not isinstance(owner, dict)
        or type(owner.get("pid")) is not int
        or owner["pid"] <= 0
        or not isinstance(owner.get("start"), str)
        or not owner["start"]
        or owner != plan.get("owner")
    ):
        raise PreparationConflict("Preparation controller owner identity changed")
    if "incoming_cleanup_verified" in value and value["incoming_cleanup_verified"] is not True:
        raise PreparationConflict("Preparation private incoming cleanup evidence changed")
    precommit_cleanup(value)
    basis = value.get("incoming_cleanup_basis")
    if basis is not None and (
        not isinstance(basis, dict)
        or value.get("incoming_cleanup_verified") is not True
        or basis.get("owner") != owner
        or basis.get("kind") not in {"current_controller_verified", "closed_dead_controller"}
        or basis.get("basis_id")
        != identity({key: item for key, item in basis.items() if key != "basis_id"})
        or (
            basis["kind"] == "closed_dead_controller"
            and basis.get("marker_id") != (precommit_cleanup(value) or {}).get("marker_id")
        )
    ):
        raise PreparationConflict("Preparation incoming cleanup basis changed")
    slots = set()
    prefix = f".leanflow/preparation-transactions/{transaction_id}/"
    for relative, row in value["writes"].items():
        entry_path(root, relative)
        if not managed_path(relative, row.get("before")):
            raise PreparationConflict("Preparation journal contains an unowned write")
        if any(
            row.get(key) != plan["writes"][relative].get(key)
            for key in ("before", "after", "incoming", "captured", "kind")
        ):
            raise PreparationConflict("Preparation frozen input changed")
        if row.get("stage") not in {
            "ready",
            "capture_pending",
            "captured",
            "install_pending",
            "installed",
            "rollback_remove_pending",
            "rollback_removed",
            "restore_pending",
            "restored",
        }:
            raise PreparationConflict("Preparation entry state is invalid")
        for key in ("before", "after"):
            checksum = row.get(key)
            if checksum is not None and (
                not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum)
            ):
                raise PreparationConflict("Preparation content identity is invalid")
        for slot in (
            row["incoming"],
            row["captured"],
            *(entry["path"] for entry in row.get("rollback_slots", [])),
        ):
            if not slot.startswith(prefix) or slot in slots:
                raise PreparationConflict("Preparation slots escaped or overlap")
            contained(root, slot)
            slots.add(slot)
        if any(entry.get("expected") != row["after"] for entry in row.get("rollback_slots", [])):
            raise PreparationConflict("Preparation rollback identity changed")
        disposal = row.get("incoming_disposal")
        if disposal is not None and (
            not isinstance(disposal, dict)
            or disposal.get("state") not in {"DELETE_PENDING", "DISPOSED"}
            or disposal.get("path") != row["incoming"]
            or disposal.get("expected") != row["after"]
            or disposal.get("inode") != row.get("incoming_inode")
            or not isinstance(disposal.get("inode"), list)
            or len(disposal["inode"]) != 2
            or any(type(value) is not int for value in disposal["inode"])
            or disposal.get("disposition_id")
            != identity({key: item for key, item in disposal.items() if key != "disposition_id"})
        ):
            raise PreparationConflict("Preparation private incoming disposal identity changed")
        if disposal is not None and "slot" in disposal:
            slot = disposal["slot"]
            if (
                not isinstance(slot, str)
                or not re.fullmatch(
                    re.escape(row["incoming"] + ".disposal-") + r"[a-f0-9]{32}", slot
                )
                or slot in slots
            ):
                raise PreparationConflict(
                    "Preparation private incoming disposal slot identity changed"
                )
            contained(root, slot)
            slots.add(slot)
    resolutions = value.get("resolutions", [])
    if not isinstance(resolutions, list) or (value["state"] == "RESOLVED" and not resolutions):
        raise PreparationConflict("Preparation resolution evidence is missing")
    prior_archives: dict[str, str] = {}
    for intent in archive_intents(value):
        contained(root, intent["path"])
        entry_path(root, intent["source"])
    for resolution in resolutions:
        observation = resolution.get("observation", {})
        if (
            resolution.get("resolution_id")
            != "prep-resolution-"
            + identity({key: item for key, item in resolution.items() if key != "resolution_id"})
            or observation.get("transaction_id") != transaction_id
            or observation.get("observation_id")
            != identity({key: item for key, item in observation.items() if key != "observation_id"})
            or resolution.get("choice") != "keep_current"
            or resolution.get("receipt_revoked") is not True
        ):
            raise PreparationConflict("Preparation resolution identity changed")
        for checksum, relative in resolution["archives"].items():
            if not re.fullmatch(
                re.escape(prefix + "versions/" + checksum) + r"(?:-[0-9]+)?", relative
            ):
                raise PreparationConflict("Preparation archived version escaped its transaction")
            contained(root, relative)
        for relative, expected in resolution.get("unavailable_versions", {}).items():
            if (
                not re.fullmatch(
                    re.escape(prefix + "versions/" + expected) + r"(?:-[0-9]+)?", relative
                )
                or prior_archives.get(relative) != expected
                or observation["retained_slots"].get(relative) == expected
            ):
                raise PreparationConflict("Preparation lost-version acknowledgement changed")
            contained(root, relative)
        prior_archives.update(
            {relative: checksum for checksum, relative in resolution["archives"].items()}
        )
    return value


def load_registry(root: Path) -> dict[str, Any]:
    """Read all setup attempts so older retained descriptors never lose their audit owner."""
    path = preparation_directory(root) / "registry.json"
    discovery = discover_history(root, preparation_directory(root))
    if not path.exists() and not path.is_symlink():
        if discovery["evidence"]:
            raise PreparationAuthorityLoss(discovery)
        return {
            "schema_version": 1,
            "root": str(root.resolve()),
            "active": None,
            "current": None,
            "selected": None,
            "transactions": [],
            "conflict_queue": [],
            "journal_losses": {},
            "index_history": {},
        }
    try:
        value = _read(path)
    except PreparationConflict as error:
        raise PreparationAuthorityLoss(discovery) from error
    if (
        value.get("root") != str(root.resolve())
        or not isinstance(value.get("transactions"), list)
        or len(value["transactions"]) != len(set(value["transactions"]))
    ):
        raise PreparationConflict("Preparation registry identity changed")
    if (
        discovery["unavailable_controls"]
        or set(discovery["transactions"]) - set(value["transactions"])
        or (value.get("index_history", {}) != discovery["index_history"])
    ):
        raise PreparationAuthorityLoss(discovery)
    validate_acknowledgment(value)
    queue = value.setdefault("conflict_queue", [])
    if (
        not isinstance(queue, list)
        or any(not isinstance(key, str) or key not in value["transactions"] for key in queue)
        or len(queue) != len(set(queue))
    ):
        raise PreparationConflict("Preparation conflict ownership changed")
    for key in ("active", "current", "selected"):
        if value.get(key) is not None and value[key] not in value["transactions"]:
            raise PreparationConflict("Preparation registry lost transaction ownership")
    losses = value.setdefault("journal_losses", {})
    if not isinstance(losses, dict) or any(key not in value["transactions"] for key in losses):
        raise PreparationConflict("Preparation journal loss ownership changed")
    changed = False
    journals = {}
    for transaction_id in value["transactions"]:
        try:
            journal = load_journal(root, transaction_id)
        except (
            PreparationConflict,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ) as error:
            _raise_transient_io(error)
            journal = None
        if journal is None or transaction_id in losses:
            before = identity(value)
            try:
                if transaction_id in losses:
                    validate_loss(
                        preparation_directory(root), transaction_id, losses[transaction_id]
                    )
                observe_journal_loss(root, preparation_directory(root), value, transaction_id)
            except ValueError as error:
                raise PreparationConflict(str(error)) from error
            changed = changed or before != identity(value)
            continue
        if (
            journal["state"] not in TERMINAL
            and value["active"] != transaction_id
            and not (journal["state"] == "CONFLICTED" and transaction_id in queue)
        ):
            raise PreparationConflict("Preparation registry lost pending transaction ownership")
        journals[transaction_id] = journal
    receipt_path = preparation_directory(root) / "receipt.json"
    if receipt_path.exists() and value.get("selected") not in losses:
        try:
            receipt = json.loads(read_regular_bytes(receipt_path, MAX_CONTROL_BYTES))
        except (OSError, ValueError) as error:
            _raise_transient_io(error)
            raise PreparationAuthorityLoss(discovery) from error
        allowed = [
            journal["plan"]["receipt"]
            for key, journal in journals.items()
            if key == value.get("selected")
            or (
                key == value.get("active")
                and journal["state"] == "COMMITTED"
                and not journal["conflicts"]
                and key
                not in value.get("registry_loss_acknowledgment", {}).get(
                    "discovered_transactions", []
                )
            )
        ]
        if receipt not in allowed:
            raise PreparationAuthorityLoss(discovery)
    if changed:
        save_registry(root, value)
    return value


def save_registry(root: Path, registry: dict[str, Any]) -> None:
    durable_json(preparation_directory(root) / "registry.json", registry)


def save_journal(
    root: Path, journal: dict[str, Any], checkpoint: Callable[[str], None] | None = None
) -> None:
    """Commit one authoritative historical journal before its observable checkpoint."""
    journal["status"] = journal["state"]
    directory = preparation_directory(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    save_snapshot(directory, journal)
    if checkpoint:
        checkpoint("journal:" + journal["state"])


def settle(root: Path, registry: dict[str, Any], journal: dict[str, Any]) -> None:
    """Select checked setup only after commitment; resolved conflicts revoke selected readiness."""
    lost_selection = registry.get("registry_loss_acknowledgment", {}).get(
        "discovered_transactions", []
    )
    if (
        journal["state"] == "COMMITTED"
        and registry["active"] == journal["transaction_id"]
        and journal["transaction_id"] not in lost_selection
    ):
        registry["selected"] = journal["transaction_id"]
        durable_json(preparation_directory(root) / "receipt.json", journal["plan"]["receipt"])
    if journal["state"] == "RESOLVED":
        if registry.get("selected") == journal["transaction_id"]:
            registry["selected"] = None
            receipt = preparation_directory(root) / "receipt.json"
            if receipt.exists():
                receipt.unlink()
                from leanflow_cli.formalization.evidence import sync_directory

                sync_directory(receipt.parent)
        registry["conflict_queue"] = [
            key
            for key in registry.setdefault("conflict_queue", [])
            if key != journal["transaction_id"]
        ]
    if journal["state"] in TERMINAL and registry["active"] == journal["transaction_id"]:
        registry["active"] = None
    if registry["active"] is None and registry.get("conflict_queue"):
        registry["active"] = registry["conflict_queue"][0]
    save_registry(root, registry)


def observed_versions(root: Path, journal: dict[str, Any]) -> dict[str, Any]:
    """Bind transaction/history identity plus every actual destination and retained slot."""
    slots = {
        slot
        for row in journal["writes"].values()
        for slot in (
            row["captured"],
            row["incoming"],
            *(item["path"] for item in row.get("rollback_slots", [])),
        )
    }
    slots.update(
        row["incoming_disposal"]["slot"]
        for row in journal["writes"].values()
        if row.get("incoming_disposal", {}).get("slot")
    )
    slots.update(
        path
        for resolution in journal.get("resolutions", [])
        for path in resolution["archives"].values()
    )
    slots.update(resolution_archive_paths(root, journal))
    value = {
        "transaction_id": journal["transaction_id"],
        "destinations": {
            relative: entry_identity(entry_path(root, relative)) for relative in journal["order"]
        },
        "retained_slots": {
            slot: retained_identity(root, entry_path(root, slot)) for slot in sorted(slots)
        },
        "history_id": identity(
            {"conflicts": journal["conflicts"], "resolutions": journal["resolutions"]}
        ),
    }
    return {**value, "observation_id": identity(value)}


def audit_retained(root: Path, registry: dict[str, Any], *, raise_conflicts: bool = True) -> None:
    """Audit all prior retained versions and resolution archives before workflow admission."""
    queue = registry.setdefault("conflict_queue", [])
    for transaction_id in registry["transactions"]:
        if transaction_id in registry.get("journal_losses", {}):
            continue
        journal = load_journal(root, transaction_id)
        if journal["state"] == "CONFLICTED":
            if transaction_id not in queue:
                queue.append(transaction_id)
                save_registry(root, registry)
            continue
        if journal["state"] not in TERMINAL:
            continue
        resolved = journal["state"] == "RESOLVED"
        acknowledged = (
            journal["resolutions"][-1]["observation"]["retained_slots"] if resolved else {}
        )
        issues = []
        acknowledged_archives = (
            journal["resolutions"][-1]["observation"]["retained_slots"] if resolved else {}
        )
        for resolution in journal["resolutions"]:
            for expected, path in resolution["archives"].items():
                actual = retained_identity(root, entry_path(root, path))
                expected = acknowledged_archives.get(path, expected)
                if actual != expected:
                    issues.append(
                        f"Archived preparation resolution version missing or changed: {path}"
                    )
        completed = {
            path: expected
            for resolution in journal["resolutions"]
            for expected, path in resolution["archives"].items()
        }
        for intent in archive_intents(journal):
            path = intent["path"]
            actual = retained_identity(root, entry_path(root, path))
            expected = acknowledged.get(path, completed.get(path, intent["expected"]))
            if resolved and actual != expected:
                issues.append(f"Registered preparation archive version changed: {path}")
        if resolved:
            versions_prefix = f".leanflow/preparation-transactions/{transaction_id}/versions/"
            known = set(acknowledged) | set(completed)
            for path in resolution_archive_paths(root, journal):
                actual = retained_identity(root, entry_path(root, path))
                expected = acknowledged.get(path, completed.get(path))
                if (path not in known and actual is not None) or (
                    path in known and path.startswith(versions_prefix) and actual != expected
                ):
                    issues.append(f"Preserved preparation archive version changed: {path}")
        for relative, row in journal["writes"].items():
            slots = [
                {"path": row["captured"], "expected": row["before"]},
                *row.get("rollback_slots", []),
            ]
            if resolved:
                slots.append({"path": row["incoming"], "expected": row["after"]})
            elif journal["state"] == "ROLLED_BACK":
                slots.append(
                    {
                        "path": row["incoming"],
                        "expected": (
                            None
                            if row.get("incoming_disposal", {}).get("state") == "DISPOSED"
                            else row["after"]
                        ),
                    }
                )
            disposal = row.get("incoming_disposal", {})
            if disposal.get("slot"):
                slots.append(
                    {
                        "path": disposal["slot"],
                        "expected": None if disposal["state"] == "DISPOSED" else row["after"],
                    }
                )
            for slot in slots:
                actual = retained_identity(root, entry_path(root, slot["path"]))
                expected = acknowledged.get(slot["path"], slot["expected"])
                if (
                    (resolved and actual != expected)
                    or (not resolved and actual is not None and actual != expected)
                    or (journal["state"] == "COMMITTED" and expected is not None and actual is None)
                ):
                    issue = f"Retained user version changed: {relative} ({slot['path']})"
                    issues.append(issue)
        if resolved and transaction_id in queue and not issues:
            settle(root, registry, journal)
            queue = registry["conflict_queue"]
            continue
        if transaction_id in queue and not issues:
            issues.append("Retained-version audit was interrupted; inspect all versions")
        if issues or (journal["conflicts"] and not resolved):
            for issue in issues:
                if issue not in journal["conflicts"]:
                    journal["conflicts"].append(issue)
            if transaction_id not in queue:
                queue.append(transaction_id)
            if registry["active"] is None:
                registry["active"] = transaction_id
            save_registry(root, registry)
            journal["state"] = "CONFLICTED"
            save_journal(root, journal)
    if queue and raise_conflicts:
        if queue[0] in registry.get("journal_losses", {}):
            raise PreparationConflict(
                "Preparation journal unavailable; inspect preparation-conflicts and acknowledge exact observed loss"
            )
        first = load_journal(root, queue[0])
        raise PreparationConflict(
            (first["conflicts"][0] if first["conflicts"] else "Preparation conflict unresolved")
            + "; inspect queued transactions: "
            + ", ".join(queue)
        )
