"""Record unavailable publication journals without inventing the mathematical evidence they held."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import identifier, identity, normalized_relative
from leanflow_cli.formalization.publication_io import PublicationConflict, entry_hash, entry_path
from leanflow_cli.formalization.publication_registry_inventory import discover_current_authority


def expected_archive_hashes(journal: Mapping[str, Any]) -> dict[str, str]:
    """Retain every expected planned or acknowledged archive identity after missing bytes are reported."""
    hashes = {
        checksum
        for entry in journal["writes"].values()
        for checksum in (entry["before"], entry["after"])
        if checksum is not None
    }
    expected = {f"blobs/{checksum}": checksum for checksum in sorted(hashes)}
    for resolution in journal.get("resolutions", []):
        locations = resolution.get("version_locations", {})
        for name, checksum in resolution["version_blobs"].items():
            expected[locations.get(name, f"blobs/{checksum}")] = checksum
    return expected


def observe_journal_loss(root: Path, registry: dict[str, Any], key: str) -> dict[str, Any]:
    """Retain original registry ownership and bind unknown scope to existing published authority."""
    record = registry["transactions"][key]
    loss = record.get("loss")
    if loss is None:
        selected = registry.get("selected_revision") or {}
        known_revision = record.get("revision_id") or (
            selected.get("revision_id") if selected.get("transaction_id") == key else None
        )
        revisions = {identifier(known_revision)} if known_revision else set()
        authority_paths = set(record.get("write_paths", []))
        if not known_revision:
            if selected.get("revision_id"):
                revisions.add(identifier(selected["revision_id"]))
            for item in registry["transactions"].values():
                if item.get("revision_id"):
                    revisions.add(identifier(item["revision_id"]))
            current = discover_current_authority(root)
            revisions.update(current["revision_ids"])
            authority_paths.update(current["evidence"])
        loss = record["loss"] = {
            "schema_version": 1,
            "transaction_id": key,
            "revision_id": known_revision or "unknown",
            "revision_ids": sorted(revisions),
            "authority_paths": sorted(authority_paths),
            "original_journal": record["journal"],
            "state": "CONFLICTED",
            "conflicts": [
                "Publication journal unavailable; its historical versions and scope cannot be reconstructed"
            ],
            "resolutions": [],
        }
    validate_loss(key, record)
    actual = entry_hash(entry_path(root, record["journal"]))
    if (
        loss["state"] == "RESOLVED"
        and actual != loss["resolutions"][-1]["observation"]["retained_slots"][record["journal"]]
    ):
        loss["state"] = "CONFLICTED"
        issue = "Unavailable publication journal observation changed after acknowledgment"
        if issue not in loss["conflicts"]:
            loss["conflicts"].append(issue)
    if loss["state"] == "CONFLICTED":
        queue = registry.setdefault("conflict_queue", [])
        if key not in queue:
            queue.append(key)
        if registry["active"] is None:
            registry["active"] = key
    return loss


def validate_loss(key: str, record: Mapping[str, Any]) -> None:
    """Validate the loss record separately from the unavailable journal's unknown plan."""
    loss = record.get("loss")
    if (
        not isinstance(loss, dict)
        or loss.get("schema_version") != 1
        or loss.get("transaction_id") != key
        or loss.get("original_journal") != record.get("journal")
        or loss.get("state") not in {"CONFLICTED", "RESOLVED"}
    ):
        raise PublicationConflict("Invalid publication evidence-loss record")
    identifier(loss.get("revision_id", ""))
    revisions = loss.get("revision_ids")
    paths = loss.get("authority_paths")
    if (
        not isinstance(revisions, list)
        or not isinstance(paths, list)
        or any(not isinstance(value, str) for value in [*revisions, *paths])
        or len(revisions) != len(set(revisions))
        or len(paths) != len(set(paths))
    ):
        raise PublicationConflict("Invalid publication evidence-loss scope")
    for revision in revisions:
        identifier(revision)
    for path in paths:
        normalized_relative(path)
    if (
        not isinstance(loss.get("conflicts"), list)
        or any(not isinstance(issue, str) for issue in loss["conflicts"])
        or not isinstance(loss.get("resolutions"), list)
    ):
        raise PublicationConflict("Invalid publication evidence-loss history")
    if loss["state"] == "RESOLVED" and not loss["resolutions"]:
        raise PublicationConflict("Publication evidence-loss acknowledgment missing")
    for resolution in loss["resolutions"]:
        if not isinstance(resolution, dict):
            raise PublicationConflict("Invalid publication evidence-loss acknowledgment")
        payload = {name: value for name, value in resolution.items() if name != "resolution_id"}
        observation = resolution.get("observation", {})
        if (
            not isinstance(observation, dict)
            or resolution.get("schema_version") != 1
            or resolution.get("resolution_id") != "resolution-" + identity(payload)
            or resolution.get("transaction_id") != key
            or resolution.get("revision_id") != loss["revision_id"]
            or resolution.get("choice") != "keep_current"
            or not isinstance(resolution.get("reason"), str)
            or not resolution["reason"].strip()
            or resolution.get("revoked_revision_ids") != revisions
            or resolution.get("unavailable_journal") is not True
            or resolution.get("requires_new_acceptance") is not True
            or observation.get("transaction_id") != key
            or observation.get("observation_id")
            != identity(
                {name: value for name, value in observation.items() if name != "observation_id"}
            )
        ):
            raise PublicationConflict("Publication evidence-loss acknowledgment changed")
        expected_maps = {"destinations": set(paths), "retained_slots": {record["journal"]}}
        for name, names in expected_maps.items():
            observed = observation.get(name)
            if (
                not isinstance(observed, dict)
                or set(observed) != names
                or any(
                    value is not None
                    and value != "nonregular"
                    and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value))
                    for value in observed.values()
                )
            ):
                raise PublicationConflict("Publication evidence-loss observation scope changed")


def loss_observation(root: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Expose exact available authority and the unavailable journal identity for acknowledgment."""
    loss = record["loss"]
    payload = {
        "transaction_id": loss["transaction_id"],
        "destinations": {
            path: entry_hash(entry_path(root, path)) for path in loss["authority_paths"]
        },
        "retained_slots": {record["journal"]: entry_hash(entry_path(root, record["journal"]))},
        "history_id": identity(loss),
    }
    return {**payload, "observation_id": identity(payload)}


def loss_projection(root: Path, registry: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Display honest absence of planned versions while retaining the original registry path."""
    record = registry["transactions"][key]
    loss = record["loss"]
    return {
        **loss_observation(root, record),
        "state": loss["state"],
        "revision_id": loss["revision_id"],
        "journal": record["journal"],
        "retained_directory": str(Path(record["journal"]).parent),
        "conflicts": list(loss["conflicts"]),
        "resolutions": list(loss["resolutions"]),
        "planned_versions": None,
        "versions": [],
        "version_count": 0,
        "version_offset": 0,
        "next_version_offset": None,
        "unavailable_journal": True,
        "revoked_revision_ids": loss["revision_ids"],
        "active": registry["active"] == key,
        "queued": key in registry.get("conflict_queue", []),
    }


def resolve_journal_loss(
    root: Path, registry: dict[str, Any], request: Mapping[str, Any]
) -> dict[str, Any]:
    """Acknowledge exact observed loss, keep current bytes, and require fresh mathematical acceptance."""
    key = request["transaction_id"]
    record = registry["transactions"][key]
    loss = record["loss"]
    if loss["state"] != "CONFLICTED":
        raise PublicationConflict("Publication evidence loss has no unresolved observation")
    observation = loss_observation(root, record)
    if any(
        request.get(name) != observation[name]
        for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
    ):
        raise PublicationConflict("Publication loss observation changed; refresh all versions")
    payload = {
        "schema_version": 1,
        "transaction_id": key,
        "revision_id": loss["revision_id"],
        "observation": observation,
        "choice": "keep_current",
        "reason": request["reason"],
        "unavailable_journal": True,
        "requires_new_acceptance": True,
        "revoked_revision_ids": loss["revision_ids"],
    }
    resolution = {**payload, "resolution_id": "resolution-" + identity(payload)}
    loss["resolutions"].append(resolution)
    loss["state"] = "RESOLVED"
    selected = registry.get("selected_revision") or {}
    if selected.get("transaction_id") == key or selected.get("revision_id") in loss["revision_ids"]:
        registry["selected_revision"] = None
    revoked = registry.setdefault("revoked_revisions", [])
    for revision in loss["revision_ids"]:
        revoked.append(
            {
                "transaction_id": key,
                "revision_id": revision,
                "resolution_id": resolution["resolution_id"],
                "reason": "Publication evidence unavailable; a new mathematical acceptance is required",
            }
        )
    registry["conflict_queue"] = [
        item for item in registry.setdefault("conflict_queue", []) if item != key
    ]
    if registry["active"] == key:
        registry["active"] = registry["conflict_queue"][0] if registry["conflict_queue"] else None
    return resolution
