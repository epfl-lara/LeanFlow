"""Resolve preserved publication conflicts against explicit observed content identities."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, TypedDict

from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    identifier,
    identity,
    put_blob,
    read_blob,
    sync_directory,
)
from leanflow_cli.formalization.publication_io import (
    PublicationConflict,
    entry_hash,
    entry_path,
    publication_lock,
)
from leanflow_cli.formalization.publication_journal import (
    audit_retained,
    load_journal,
    load_registry,
    save_journal,
    save_registry,
    settle_terminal,
)
from leanflow_cli.formalization.publication_loss import (
    expected_archive_hashes,
    loss_projection,
    resolve_journal_loss,
)
from leanflow_cli.formalization.publication_registry_loss import (
    LOSS_ID,
    PublicationAuthorityLoss,
    acknowledge_authority_loss,
    authority_loss_projection,
    discover_authority,
)


class ConflictResolutionRequest(TypedDict):
    """Bind a reconciled canonical choice to the exact transaction and all observed bytes."""

    transaction_id: str
    observation_id: str
    destinations: dict[str, str | None]
    retained_slots: dict[str, str | None]
    choice: Literal["keep_current"]
    reason: str


def _observation(root: Path, path: Path, journal: dict[str, Any]) -> dict[str, Any]:
    """Bind current destinations, retained slots, and the previous conflict history."""
    slots = {
        location
        for entry in journal["writes"].values()
        for location in (
            entry["captured"],
            entry["staged"],
            *(slot["path"] for slot in entry["rollback_slots"]),
        )
    }
    archives = expected_archive_hashes(journal)
    slots.update(archives)
    observed_slots = {
        relative: entry_hash(entry_path(path.parent, relative)) for relative in sorted(slots)
    }
    payload = {
        "transaction_id": journal["transaction_id"],
        "destinations": {
            relative: entry_hash(entry_path(root, relative)) for relative in journal["order"]
        },
        "retained_slots": observed_slots,
        "unavailable_versions": {
            relative: expected
            for relative, expected in archives.items()
            if observed_slots[relative] != expected
        },
        "history_id": identity(
            {"conflicts": journal["conflicts"], "resolutions": journal.get("resolutions", [])}
        ),
    }
    return {**payload, "observation_id": identity(payload)}


def _versions(journal: dict[str, Any], observation: dict[str, Any]) -> list[dict[str, Any]]:
    """Project opaque identities for planned and actual byte versions without accepting paths."""
    canonical_slots = {
        slot: relative
        for relative, entry in journal["writes"].items()
        for slot in (
            entry["captured"],
            entry["staged"],
            *(row["path"] for row in entry["rollback_slots"]),
        )
    }
    groups = {
        "destinations": observation["destinations"],
        "retained_slots": observation["retained_slots"],
        "planned_before": {
            relative: entry["before"] for relative, entry in journal["writes"].items()
        },
        "planned_after": {
            relative: entry["after"] for relative, entry in journal["writes"].items()
        },
    }
    result = []
    for group, rows in groups.items():
        for path, checksum in rows.items():
            original = canonical_slots.get(path, path) if group == "retained_slots" else path
            result.append(
                {
                    "version_id": "conflict-version-"
                    + identity(
                        [
                            observation["transaction_id"],
                            observation["observation_id"],
                            group,
                            path,
                            checksum,
                        ]
                    ),
                    "group": group,
                    "display_path": path,
                    "label": f"{group}: {original}",
                    "sha256": checksum,
                    "kind": "lean" if original.endswith(".lean") else "source",
                    "available": checksum not in {None, "nonregular"}
                    and (
                        group not in {"planned_before", "planned_after"}
                        or observation["retained_slots"].get(f"blobs/{checksum}") == checksum
                    ),
                }
            )
    return result


def publication_conflicts(
    root: Path,
    *,
    transaction_id: str | None = None,
    version_offset: int = 0,
    version_limit: int = 64,
) -> dict[str, Any]:
    """Expose frozen-plan and live-version identities even while normal work is blocked."""
    root = root.resolve()
    if (
        type(version_offset) is not int
        or version_offset < 0
        or type(version_limit) is not int
        or not 1 <= version_limit <= 256
    ):
        raise ValueError(
            "Publication conflict version pages require bounded integer offsets/limits"
        )
    if transaction_id is not None:
        identifier(transaction_id)
    with publication_lock(root):
        try:
            registry = load_registry(root)
        except PublicationAuthorityLoss:
            discovery = discover_authority(root)
            return {
                "schema_version": 1,
                "transactions": (
                    [authority_loss_projection(root, discovery)]
                    if transaction_id in {None, LOSS_ID}
                    else []
                ),
            }
        audit_retained(root, registry, raise_conflicts=False)
        results = []
        for key, entry in registry["transactions"].items():
            if transaction_id is not None and transaction_id != key:
                continue
            if entry.get("loss"):
                results.append(loss_projection(root, registry, key))
                continue
            path = contained(root, entry["journal"])
            journal = load_journal(path)
            if journal["conflicts"] or journal.get("resolutions"):
                observation = _observation(root, path, journal)
                versions = _versions(journal, observation)
                results.append(
                    {
                        **observation,
                        "versions": versions[version_offset : version_offset + version_limit],
                        "version_count": len(versions),
                        "version_offset": version_offset,
                        "next_version_offset": (
                            version_offset + version_limit
                            if version_offset + version_limit < len(versions)
                            else None
                        ),
                        "state": journal["state"],
                        "revision_id": journal["revision_id"],
                        "journal": entry["journal"],
                        "retained_directory": path.parent.relative_to(root).as_posix(),
                        "conflicts": list(journal["conflicts"]),
                        "resolutions": list(journal.get("resolutions", [])),
                        "planned_versions": journal["plan"]["writes"],
                        "active": registry["active"] == key,
                        "queued": key in registry.get("conflict_queue", []),
                    }
                )
        return {"schema_version": 1, "transactions": results}


def resolve_conflict_artifact(
    root: Path, transaction_id: str, observation_id: str, version_id: str
) -> dict[str, Any]:
    """Materialize at most eight MiB of an opaque observed version without granting path access."""
    root = root.resolve()
    identifier(transaction_id)
    identifier(version_id)
    with publication_lock(root):
        registry = load_registry(root)
        entry = registry["transactions"].get(transaction_id)
        if not entry:
            raise PublicationConflict("Publication artifact transaction is unknown")
        if entry.get("loss"):
            raise ValueError(
                "Unavailable publication journals have no reconstructible version artifacts"
            )
        journal_path = contained(root, entry["journal"])
        journal = load_journal(journal_path)
        observation = _observation(root, journal_path, journal)
        if observation["observation_id"] != observation_id:
            raise PublicationConflict(
                "Publication artifact observation changed; refresh all versions"
            )
        version = next(
            (row for row in _versions(journal, observation) if row["version_id"] == version_id),
            None,
        )
        if not version or not version["available"]:
            raise ValueError("Publication artifact does not identify an available observed version")
        checksum = version["sha256"]
        if version["group"] in {"planned_before", "planned_after"}:
            source = contained(journal_path.parent, f"blobs/{checksum}")
        else:
            base = root if version["group"] == "destinations" else journal_path.parent
            source = entry_path(base, version["display_path"])
        if source.stat().st_size > 8 * 1024**2:
            raise ValueError(
                "Publication version exceeds the eight MiB viewer bound; inspect it locally before resolving"
            )
        content = source.read_bytes()
        if digest(content) != checksum or _observation(root, journal_path, journal) != observation:
            raise PublicationConflict("Publication artifact changed while freezing its version")
        put_blob(journal_path.parent / "blobs", content)
        read_blob(journal_path.parent / "blobs", checksum)
        kind = "pdf" if content.startswith(b"%PDF-") else version["kind"]
        extension = ".pdf" if kind == "pdf" else ".lean" if kind == "lean" else ".txt"
        viewer = contained(journal_path.parent, "viewer")
        viewer.mkdir(exist_ok=True)
        target = contained(viewer, version_id + extension)
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        except FileExistsError:
            if target.read_bytes() != content:
                raise ValueError("Publication version viewer bytes changed") from None
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(viewer)
        return {"id": version_id, "path": str(target), "sha256": checksum, "kind": kind}


def resolve_publication_conflict(
    root: Path,
    request: ConflictResolutionRequest | Mapping[str, Any],
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Keep user-reconciled canonical entries, archive every version, and revoke acceptance.

    Reconciliation happens in the editor before this operation. No canonical entry
    or retained inode is replaced, so an open descriptor remains observable. A
    new mathematical revision must pass acceptance after any explicit resolution.
    """
    root = root.resolve()
    if (
        set(request)
        != {
            "transaction_id",
            "observation_id",
            "destinations",
            "retained_slots",
            "choice",
            "reason",
        }
        or request.get("choice") != "keep_current"
    ):
        raise ValueError("Unsupported publication conflict resolution request")
    if not isinstance(request.get("reason"), str) or not request["reason"].strip():
        raise ValueError("Publication resolution requires a reconciliation reason")
    transaction_id = identifier(request["transaction_id"])
    with publication_lock(root):
        if transaction_id == LOSS_ID:
            try:
                load_registry(root)
            except PublicationAuthorityLoss:
                registry, resolution = acknowledge_authority_loss(
                    root, request, read_journal=load_journal
                )
                if checkpoint:
                    checkpoint("registry_loss_acknowledgment_prepared")
                save_registry(root, registry, acknowledge_registry_loss=True)
                if checkpoint:
                    checkpoint("registry_loss_acknowledgment_committed")
                return resolution
            raise PublicationConflict("Publication registry has no unresolved authority loss")
        registry = load_registry(root)
        entry = registry["transactions"].get(transaction_id)
        if not entry:
            raise PublicationConflict("Publication resolution transaction is unknown")
        if registry["active"] not in {None, transaction_id} and transaction_id not in registry.get(
            "conflict_queue", []
        ):
            raise PublicationConflict("Resolve the active project publication first")
        if entry.get("loss"):
            resolution = resolve_journal_loss(root, registry, request)
            save_registry(root, registry)
            return resolution
        path = contained(root, entry["journal"])
        journal = load_journal(path)
        if journal["state"] not in {"CONFLICTED", "COMMITTED", "ROLLED_BACK", "RESOLVED"}:
            raise PublicationConflict("Recover the interrupted publication before resolving it")
        if not journal["conflicts"]:
            raise PublicationConflict("Publication transaction has no recorded conflict")
        observation = _observation(root, path, journal)
        if any(
            request.get(name) != observation[name]
            for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
        ):
            raise PublicationConflict(
                "Publication resolution observation changed; refresh all versions"
            )
        blobs: dict[str, str] = {}
        locations: dict[str, str] = {}
        archive_prefix = (
            f"resolution-archives/{uuid.uuid4().hex}/blobs"
            if observation["unavailable_versions"]
            else "blobs"
        )
        for group, base in (("destinations", root), ("retained_slots", path.parent)):
            for relative, expected in observation[group].items():
                if expected == "nonregular":
                    raise PublicationConflict(
                        "Reconcile non-regular publication entries before resolution"
                    )
                if expected is not None:
                    data = entry_path(base, relative).read_bytes()
                    actual = put_blob(contained(path.parent, archive_prefix), data)
                    if actual != expected:
                        raise PublicationConflict(
                            "A publication version changed while archiving resolution"
                        )
                    blobs[f"{group}/{relative}"] = actual
                    if archive_prefix != "blobs":
                        locations[f"{group}/{relative}"] = f"{archive_prefix}/{actual}"
        if checkpoint:
            checkpoint("resolution_versions_archived")
        if _observation(root, path, journal) != observation:
            raise PublicationConflict("Publication versions changed before resolution commitment")
        payload = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "revision_id": journal["revision_id"],
            "observation": observation,
            "choice": request["choice"],
            "reason": request["reason"],
            "version_blobs": blobs,
            "version_locations": locations,
            "unavailable_versions": observation["unavailable_versions"],
            "requires_new_acceptance": True,
        }
        resolution = {**payload, "resolution_id": "resolution-" + identity(payload)}
        journal.setdefault("resolutions", []).append(resolution)
        journal["state"] = "RESOLVED"
        save_journal(path, journal, checkpoint)
        settle_terminal(root, registry, journal)
        if checkpoint:
            checkpoint("resolution_settled")
        return resolution
