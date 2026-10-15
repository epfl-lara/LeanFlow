"""Discover lost publication indexes without reconstructing mathematical approval from files."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    identifier,
    identity,
    sync_directory,
)
from leanflow_cli.formalization.publication_io import (
    PublicationConflict,
    entry_path,
    mkdir_durable,
    rename_exclusive,
)
from leanflow_cli.formalization.publication_loss import observe_journal_loss

from .publication_registry_inventory import (
    INDEX_HISTORY,
    REGISTRY_RELATIVE,
    PublicationAuthorityLoss,  # noqa: F401 -- public error consumed by callers
    bounded_control_bytes,
    discover_authority,
)

LOSS_ID = "publication-registry-loss"


def create_registry(path: Path, registry: Mapping[str, Any]) -> None:
    """Publish a flushed new index without replacing concurrently restored authority."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".publication-index-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write((json.dumps(registry, sort_keys=True, indent=2) + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())
    try:
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise PublicationConflict(
                "Publication registry was restored before registration; preserved it without replacement"
            ) from error
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def replace_lost_registry(root: Path, registry: Mapping[str, Any]) -> None:
    """Retain an exactly observed stale index and publish fresh authority without clobbering a new entry."""
    retired = registry["registry_loss_acknowledgment"].get("retired_registry")
    path = entry_path(root, REGISTRY_RELATIVE)
    if retired:
        data = bounded_control_bytes(path, [0])
        if not isinstance(data, bytes) or digest(data) != retired["sha256"]:
            raise PublicationConflict(
                "Publication registry reappeared or changed before loss acknowledgment"
            )
        archive = entry_path(root, retired["path"])
        mkdir_durable(archive.parent)
        rename_exclusive(path, archive)
        sync_directory(path.parent)
        sync_directory(archive.parent)
        actual = bounded_control_bytes(archive, [0])
        if not isinstance(actual, bytes) or digest(actual) != retired["sha256"]:
            try:
                rename_exclusive(archive, path)
                sync_directory(path.parent)
                sync_directory(archive.parent)
            except OSError:
                pass
            raise PublicationConflict(
                "Publication registry changed during retirement; preserved its actual bytes"
            )
    elif path.exists() or path.is_symlink():
        raise PublicationConflict("Publication registry reappeared before loss acknowledgment")
    create_registry(path, registry)


def authority_loss_projection(root: Path, discovery: Mapping[str, Any]) -> dict[str, Any]:
    """Expose exact surviving controls and canonical destinations for explicit reconciliation."""
    budget = [0]
    destinations = {}
    for path in discovery["write_paths"]:
        data = bounded_control_bytes(entry_path(root, path), budget)
        destinations[path] = digest(data) if isinstance(data, bytes) else data
    payload = {
        "transaction_id": LOSS_ID,
        "destinations": destinations,
        "retained_slots": dict(discovery["evidence"]),
        "history_id": discovery["discovery_id"],
    }
    return {
        **payload,
        "observation_id": identity(payload),
        "state": "CONFLICTED",
        "revision_id": "unknown",
        "journal": ".leanflow/formalization-publications.json",
        "retained_directory": ".leanflow/formalization-publication-transactions",
        "conflicts": [
            (
                "Publication registry incomplete; surviving transaction and retired index history must be acknowledged and reaccepted"
                if REGISTRY_RELATIVE in discovery["evidence"]
                else "Publication registry unavailable; surviving local or imported authority must be acknowledged and reaccepted"
            )
        ],
        "resolutions": [],
        "planned_versions": None,
        "versions": [],
        "version_count": 0,
        "version_offset": 0,
        "next_version_offset": None,
        "unavailable_registry": True,
        "revoked_revision_ids": discovery["revision_ids"],
        "active": True,
        "queued": True,
    }


def validate_acknowledgment(registry: Mapping[str, Any]) -> None:
    """Authenticate the durable loss acknowledgment without reviving a selected revision."""
    acknowledgment = registry.get("registry_loss_acknowledgment")
    if acknowledgment is None:
        return
    if not isinstance(acknowledgment, dict):
        raise PublicationConflict("Invalid publication registry-loss acknowledgment")
    payload = {name: value for name, value in acknowledgment.items() if name != "resolution_id"}
    observation = acknowledgment.get("observation", {})
    discovered = acknowledgment.get("discovered_transactions", {})
    if (
        acknowledgment.get("schema_version") != 1
        or acknowledgment.get("resolution_id") != "resolution-" + identity(payload)
        or acknowledgment.get("transaction_id") != LOSS_ID
        or acknowledgment.get("choice") != "keep_current"
        or acknowledgment.get("requires_new_acceptance") is not True
        or acknowledgment.get("unavailable_registry") is not True
        or not isinstance(observation, dict)
        or observation.get("observation_id")
        != identity(
            {name: value for name, value in observation.items() if name != "observation_id"}
        )
        or not isinstance(discovered, dict)
        or not isinstance(acknowledgment.get("revoked_revision_ids"), list)
    ):
        raise PublicationConflict("Publication registry-loss acknowledgment changed")
    for key, path in discovered.items():
        identifier(key)
        if (
            not isinstance(path, str)
            or registry["transactions"].get(key, {}).get("journal") != path
        ):
            raise PublicationConflict("Publication loss recovery transaction ownership changed")
    for revision in acknowledgment["revoked_revision_ids"]:
        identifier(revision)
        if not any(
            item.get("revision_id") == revision for item in registry.get("revoked_revisions", [])
        ):
            raise PublicationConflict("Publication registry-loss revocation was removed")
    retired = acknowledgment.get("retired_registry")
    if retired is not None and (
        not isinstance(retired, dict)
        or set(retired) != {"path", "sha256"}
        or not isinstance(retired["path"], str)
        or not retired["path"].startswith(INDEX_HISTORY + "/")
        or not retired["path"].endswith(".json")
        or retired["sha256"] != observation.get("retained_slots", {}).get(REGISTRY_RELATIVE)
    ):
        raise PublicationConflict("Publication registry retirement ownership changed")
    if retired is not None:
        try:
            identifier(retired["path"].removeprefix(INDEX_HISTORY + "/").removesuffix(".json"))
        except ValueError as error:
            raise PublicationConflict("Publication registry retirement path changed") from error
    if (registry.get("selected_revision") or {}).get("transaction_id") in discovered:
        raise PublicationConflict("Publication registry loss cannot restore historical selection")


def acknowledge_authority_loss(
    root: Path,
    request: Mapping[str, Any],
    *,
    read_journal: Callable[[Path], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover only journal ownership and revoke all observed historical acceptance."""
    discovery = discover_authority(root)
    observation = authority_loss_projection(root, discovery)
    if not discovery["evidence"] or any(
        request.get(name) != observation[name]
        for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
    ):
        raise PublicationConflict(
            "Publication authority-loss observation changed; refresh retained evidence"
        )
    registry: dict[str, Any] = {
        "schema_version": 1,
        "active": None,
        "transactions": {},
        "selected_revision": None,
        "conflict_queue": [],
        "revoked_revisions": [],
    }
    pending = []
    unavailable = []
    for key, relative in discovery["transactions"].items():
        entry: dict[str, Any] = {"journal": relative}
        registry["transactions"][key] = entry
        try:
            journal = read_journal(contained(root, relative))
            if journal["transaction_id"] != key:
                raise PublicationConflict("Discovered journal transaction identity changed")
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            # The existing unavailable-journal mechanism will require its own
            # exact observation; the missing index acknowledgment grants none.
            pending.append(key)
            unavailable.append(key)
            continue
        entry.update(
            revision_id=journal["revision_id"],
            plan_id=journal["plan_id"],
            write_paths=list(journal["writes"]),
        )
        if journal["state"] not in {"COMMITTED", "ROLLED_BACK", "RESOLVED"} or journal["conflicts"]:
            pending.append(key)
    payload = {
        "schema_version": 1,
        "transaction_id": LOSS_ID,
        "revision_id": "unknown",
        "observation": {
            name: observation[name]
            for name in (
                "transaction_id",
                "destinations",
                "retained_slots",
                "history_id",
                "observation_id",
            )
        },
        "choice": "keep_current",
        "reason": request["reason"],
        "unavailable_registry": True,
        "requires_new_acceptance": True,
        "revoked_revision_ids": discovery["revision_ids"],
        "discovered_transactions": discovery["transactions"],
        "retired_registry": (
            {
                "path": f"{INDEX_HISTORY}/{uuid.uuid4().hex}.json",
                "sha256": discovery["evidence"][REGISTRY_RELATIVE],
            }
            if REGISTRY_RELATIVE in discovery["evidence"]
            else None
        ),
    }
    resolution = {**payload, "resolution_id": "resolution-" + identity(payload)}
    registry["registry_loss_acknowledgment"] = resolution
    registry["revoked_revisions"] = [
        {
            "transaction_id": LOSS_ID,
            "revision_id": revision,
            "resolution_id": resolution["resolution_id"],
            "reason": "Publication registry unavailable; a new mathematical acceptance is required",
        }
        for revision in discovery["revision_ids"]
    ]
    registry["conflict_queue"] = pending
    registry["active"] = pending[0] if pending else None
    for key in unavailable:
        observe_journal_loss(root, registry, key)
    if (
        discover_authority(root) != discovery
        or authority_loss_projection(root, discovery) != observation
    ):
        raise PublicationConflict(
            "Publication authority-loss evidence changed before acknowledgment"
        )
    return registry, resolution
