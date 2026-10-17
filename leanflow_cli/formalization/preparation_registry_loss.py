"""Acknowledge exact preparation-index loss while preserving versions and revoking old selection."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import identifier, identity, sync_directory
from leanflow_cli.formalization.preparation_errors import PreparationConflict
from leanflow_cli.formalization.preparation_fs_inputs import CONFIG, protected_inputs
from leanflow_cli.formalization.preparation_history import (
    MAX_CONTROL_BYTES,
    discover_history,
    observed_entry,
)
from leanflow_cli.formalization.publication_io import rename_exclusive

LOSS_ID = "preparation-registry-loss"
OBSERVATION_FIELDS = (
    "transaction_id",
    "destinations",
    "retained_slots",
    "history_id",
    "observation_id",
)


def registry_loss_projection(
    root: Path, directory: Path, discovery: dict[str, Any]
) -> dict[str, Any]:
    """Bind every surviving owned record and actual version without guessing its missing plan."""
    budget = [0, 0]
    retained = {}
    for name in discovery["evidence"]:
        scope, relative = name.split(":", 1)
        retained[name] = observed_entry((directory if scope == "home" else root) / relative, budget)
    retained["home:registry.json"] = observed_entry(directory / "registry.json", budget)
    destinations = {
        name: observed_entry(root / name, budget)
        for name in sorted(
            CONFIG | {".lake", ".leanflow/project.yaml"} | set(protected_inputs(root))
        )
    }
    payload = {
        "transaction_id": LOSS_ID,
        "destinations": destinations,
        "retained_slots": retained,
        "history_id": identity(discovery),
    }
    return {
        **payload,
        "observation_id": identity(payload),
        "state": "CONFLICTED",
        "conflicts": [
            "Preparation registry lost complete history; acknowledge actual versions and prepare again"
        ],
        "resolutions": [],
        "active": True,
        "queued": True,
        "resolvable": True,
        "revokes_current_receipt": True,
        "unavailable_registry": True,
        "planned_versions": None,
    }


def validate_acknowledgment(registry: dict[str, Any]) -> None:
    """Validate lost-index reconciliation without restoring historical receipt authority."""
    value = registry.get("registry_loss_acknowledgment")
    if value is None:
        return
    if not isinstance(value, dict):
        raise PreparationConflict("Preparation registry-loss acknowledgment changed")
    payload = {key: item for key, item in value.items() if key != "resolution_id"}
    observation = value.get("observation", {})
    transactions = value.get("discovered_transactions")
    if (
        value.get("schema_version") != 1
        or value.get("resolution_id") != "prep-registry-loss-" + identity(payload)
        or value.get("requires_new_preparation") is not True
        or value.get("transaction_id") != LOSS_ID
        or value.get("choice") != "acknowledge_registry_loss"
        or not isinstance(value.get("reason"), str)
        or not value["reason"].strip()
        or not isinstance(observation, dict)
        or set(observation) != set(OBSERVATION_FIELDS)
        or observation.get("transaction_id") != LOSS_ID
        or observation.get("observation_id")
        != identity({key: item for key, item in observation.items() if key != "observation_id"})
        or not isinstance(transactions, list)
        or any(key not in registry["transactions"] for key in transactions)
        or registry.get("selected") in transactions
    ):
        raise PreparationConflict("Preparation registry-loss acknowledgment changed")


def _create_index(path: Path, registry: dict[str, Any]) -> None:
    """Publish a flushed new index exclusively, preserving a concurrent restoration."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".preparation-index-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write((json.dumps(registry, sort_keys=True, indent=2) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise PreparationConflict(
                "Preparation registry was restored; preserve it and inspect again"
            ) from error
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def acknowledge_registry_loss(
    root: Path,
    directory: Path,
    observed: dict[str, Any],
    reason: str,
    *,
    read_journal: Callable[[Path, str], dict[str, Any]],
    observe_journal_loss: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Recover history ownership only, preserve old controls, and require newly checked setup."""
    discovery = discover_history(root, directory)
    actual = registry_loss_projection(root, directory, discovery)
    if observed != {key: actual[key] for key in OBSERVATION_FIELDS}:
        raise PreparationConflict("Preparation registry-loss observation changed; inspect again")
    registry: dict[str, Any] = {
        "schema_version": 1,
        "root": str(root.resolve()),
        "active": None,
        "current": None,
        "selected": None,
        "transactions": list(discovery["transactions"]),
        "conflict_queue": [],
        "journal_losses": {},
    }
    for key in registry["transactions"]:
        identifier(key)
        try:
            journal = read_journal(root, key)
        except (OSError, ValueError, KeyError, TypeError):
            observe_journal_loss(root, directory, registry, key)
            continue
        if journal["state"] not in {"COMMITTED", "ROLLED_BACK", "RESOLVED"} or (
            journal["conflicts"] and journal["state"] != "RESOLVED"
        ):
            registry["conflict_queue"].append(key)
    registry["conflict_queue"] = list(dict.fromkeys(registry["conflict_queue"]))
    registry["active"] = registry["conflict_queue"][0] if registry["conflict_queue"] else None
    if registry_loss_projection(root, directory, discover_history(root, directory)) != actual:
        raise PreparationConflict(
            "Preparation registry-loss versions changed before acknowledgment"
        )
    payload = {
        "schema_version": 1,
        "transaction_id": LOSS_ID,
        "observation": {key: actual[key] for key in OBSERVATION_FIELDS},
        "choice": "acknowledge_registry_loss",
        "reason": reason,
        "requires_new_preparation": True,
        "discovered_transactions": registry["transactions"].copy(),
    }
    registry["registry_loss_acknowledgment"] = {
        **payload,
        "resolution_id": "prep-registry-loss-" + identity(payload),
    }
    retired: dict[str, str] = {}
    expected_retained = dict(actual["retained_slots"])
    for name in ("registry.json", "receipt.json"):
        path = directory / name
        expected = actual["retained_slots"].get("home:" + name)
        if expected is None:
            if path.exists() or path.is_symlink():
                raise PreparationConflict("Preparation control reappeared before acknowledgment")
            continue
        if observed_entry(path, [0, 0]) != expected:
            raise PreparationConflict("Preparation control changed before retirement")
        archive = (
            directory
            / "index-history"
            / (name.removesuffix(".json") + "-" + uuid.uuid4().hex + ".json")
        )
        archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        rename_exclusive(path, archive)
        sync_directory(path.parent)
        sync_directory(archive.parent)
        if observed_entry(archive, [0, 0]) != expected:
            raise PreparationConflict(
                "Retired preparation control changed; preserve actual versions"
            )
        # Nonregular controls remain preserved and fail closed rather than becoming approval.
        data = read_regular_bytes(archive, MAX_CONTROL_BYTES)
        retired[archive.relative_to(directory).as_posix()] = hashlib.sha256(data).hexdigest()
        expected_retained.pop("home:" + name, None)
        expected_retained["home:" + archive.relative_to(directory).as_posix()] = expected
    after = discover_history(root, directory)
    expected_retained["home:registry.json"] = None
    final_observation = registry_loss_projection(root, directory, after)
    if (
        final_observation["destinations"] != actual["destinations"]
        or final_observation["retained_slots"] != expected_retained
        or after["transactions"] != discovery["transactions"]
    ):
        raise PreparationConflict(
            "Preparation versions changed during index acknowledgment; preserve all history"
        )
    registry["index_history"] = after["index_history"]
    if any(registry["index_history"].get(name) != checksum for name, checksum in retired.items()):
        raise PreparationConflict("Retired preparation index history changed before publication")
    _create_index(directory / "registry.json", registry)
    return {
        **{key: actual[key] for key in OBSERVATION_FIELDS},
        "state": "RESOLVED",
        "unavailable_registry": True,
        "requires_new_preparation": True,
        "resolution": registry["registry_loss_acknowledgment"],
    }
