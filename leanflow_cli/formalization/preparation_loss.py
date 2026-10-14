"""Acknowledge unavailable preparation journals without reconstructing missing plans or receipts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import identifier, identity, sync_directory
from leanflow_cli.formalization.preparation_audit import retained_identity
from leanflow_cli.formalization.preparation_fs_inputs import (
    CONFIG,
    entry_identity,
    protected_inputs,
)
from leanflow_cli.formalization.preparation_progress import progress_assets
from leanflow_cli.formalization.publication_io import entry_path


def _journal_identity(path: Path) -> str | None:
    """Bind unavailable bytes or an unexpected entry without following an authority alias."""
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        return identity(["link", os.readlink(path)])
    if not path.is_file():
        info = path.lstat()
        return identity(["nonregular", info.st_dev, info.st_ino, info.st_mode])
    return entry_identity(path)


def validate_loss(directory: Path, transaction_id: str, loss: dict[str, Any]) -> None:
    """Validate unavailable authority separately from its unknown original plan."""
    if (
        not isinstance(loss, dict)
        or loss.get("schema_version") != 1
        or loss.get("transaction_id") != identifier(transaction_id)
        or loss.get("original_journal")
        != str(directory / "transactions" / (transaction_id + ".json"))
        or loss.get("state") not in {"CONFLICTED", "RESOLVED"}
        or not isinstance(loss.get("conflicts"), list)
        or any(not isinstance(value, str) for value in loss["conflicts"])
        or not isinstance(loss.get("resolutions"), list)
        or (loss["state"] == "RESOLVED" and not loss["resolutions"])
    ):
        raise ValueError("Preparation journal loss authority changed")
    for resolution in loss["resolutions"]:
        if not isinstance(resolution, dict):
            raise ValueError("Preparation journal loss acknowledgment changed")
        observation = resolution.get("observation", {})
        if (
            not isinstance(observation, dict)
            or set(observation)
            != {
                "transaction_id",
                "destinations",
                "retained_slots",
                "history_id",
                "observation_id",
            }
            or any(
                not isinstance(observation.get(key), dict)
                or any(
                    not isinstance(path, str)
                    or (checksum is not None and not isinstance(checksum, str))
                    for path, checksum in observation[key].items()
                )
                for key in ("destinations", "retained_slots")
            )
            or not isinstance(observation.get("history_id"), str)
            or resolution.get("resolution_id")
            != "prep-loss-resolution-"
            + identity({key: value for key, value in resolution.items() if key != "resolution_id"})
            or resolution.get("choice") != "acknowledge_unavailable_journal"
            or resolution.get("transaction_id") != transaction_id
            or not isinstance(resolution.get("reason"), str)
            or not resolution["reason"].strip()
            or resolution.get("unavailable_journal") is not True
            or observation.get("transaction_id") != transaction_id
            or observation.get("observation_id")
            != identity(
                {key: value for key, value in observation.items() if key != "observation_id"}
            )
        ):
            raise ValueError("Preparation journal loss acknowledgment changed")


def loss_observation(
    root: Path, directory: Path, registry: dict[str, Any], transaction_id: str
) -> dict[str, Any]:
    """Bind current infrastructure, surviving retained versions and every remaining progress record."""
    loss = registry["journal_losses"][transaction_id]
    validate_loss(directory, transaction_id, loss)
    paths = CONFIG | {".lake", ".leanflow/project.yaml"} | set(protected_inputs(root))
    slots = _retained_observation(root, directory, loss)
    payload = {
        "transaction_id": transaction_id,
        "destinations": {
            relative: entry_identity(entry_path(root, relative)) for relative in sorted(paths)
        },
        "retained_slots": slots,
        "history_id": identity({"loss": loss, "selected_transaction": registry.get("selected")}),
    }
    return {**payload, "observation_id": identity(payload)}


def _retained_observation(
    root: Path, directory: Path, loss: dict[str, Any]
) -> dict[str, str | None]:
    """Audit surviving history independently of current canonical infrastructure hashing."""
    transaction_id = loss["transaction_id"]
    journal_path = Path(loss["original_journal"])
    slots = {str(journal_path): _journal_identity(journal_path)}
    slots.update(
        {
            str(directory / path): checksum
            for path, checksum in progress_assets(directory, transaction_id).items()
        }
    )
    retained = entry_path(root, f".leanflow/preparation-transactions/{transaction_id}")
    if retained.is_dir() and not retained.is_symlink():
        slots.update(
            {
                path.relative_to(root).as_posix(): retained_identity(root, path)
                for path in sorted(retained.iterdir())
            }
        )
    elif retained.exists() or retained.is_symlink():
        slots[retained.relative_to(root).as_posix()] = _journal_identity(retained)
    return slots


def observe_journal_loss(
    root: Path, directory: Path, registry: dict[str, Any], transaction_id: str
) -> dict[str, Any]:
    """Queue unknown journal scope while preserving all original ownership and available history."""
    losses = registry.setdefault("journal_losses", {})
    loss = losses.get(transaction_id)
    if loss is None:
        loss = losses[transaction_id] = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "original_journal": str(
                directory / "transactions" / (identifier(transaction_id) + ".json")
            ),
            "original_selection": {
                name: registry.get(name) for name in ("active", "current", "selected")
            },
            "state": "CONFLICTED",
            "conflicts": [
                "Preparation journal unavailable; its plan and receipt cannot be reconstructed"
            ],
            "resolutions": [],
        }
    validate_loss(directory, transaction_id, loss)
    if loss["state"] == "RESOLVED":
        actual = _retained_observation(root, directory, loss)
        prior = loss["resolutions"][-1]["observation"]["retained_slots"]
        if actual != prior:
            loss["state"] = "CONFLICTED"
            issue = (
                "Unavailable preparation journal or retained history changed after acknowledgment"
            )
            if issue not in loss["conflicts"]:
                loss["conflicts"].append(issue)
    if loss["state"] == "CONFLICTED":
        queue = registry.setdefault("conflict_queue", [])
        if transaction_id not in queue:
            queue.append(transaction_id)
        if registry["active"] is None:
            registry["active"] = transaction_id
    return loss


def loss_projection(
    root: Path, directory: Path, registry: dict[str, Any], transaction_id: str
) -> dict[str, Any]:
    """Expose honest missing authority and observable versions through the existing conflict surface."""
    loss = registry["journal_losses"][transaction_id]
    return {
        **loss_observation(root, directory, registry, transaction_id),
        "state": loss["state"],
        "conflicts": list(loss["conflicts"]),
        "resolutions": list(loss["resolutions"]),
        "active": registry["active"] == transaction_id,
        "queued": transaction_id in registry.get("conflict_queue", []),
        "resolvable": loss["state"] == "CONFLICTED",
        "revokes_current_receipt": registry.get("selected") == transaction_id,
        "unavailable_journal": True,
        "original_journal": loss["original_journal"],
        "planned_versions": None,
    }


def resolve_journal_loss(
    root: Path,
    directory: Path,
    registry: dict[str, Any],
    observed: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Acknowledge exact observed loss, preserve actual bytes, and revoke only affected selected setup."""
    transaction_id = observed.get("transaction_id")
    if not isinstance(transaction_id, str):
        raise ValueError("Preparation journal loss requires its exact transaction")
    loss = registry.get("journal_losses", {}).get(transaction_id)
    if (
        not loss
        or loss["state"] != "CONFLICTED"
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        raise ValueError("Preparation journal loss requires its unresolved observation and reason")
    actual = loss_observation(root, directory, registry, transaction_id)
    if observed != actual:
        raise ValueError(
            "Preparation journal loss versions changed; inspect again before acknowledgment"
        )
    revoked = registry.get("selected") == transaction_id
    payload = {
        "transaction_id": transaction_id,
        "observation": actual,
        "choice": "acknowledge_unavailable_journal",
        "reason": reason,
        "unavailable_journal": True,
        "receipt_revoked": revoked,
    }
    loss["resolutions"].append(
        {**payload, "resolution_id": "prep-loss-resolution-" + identity(payload)}
    )
    loss["state"] = "RESOLVED"
    if revoked:
        registry["selected"] = None
        receipt = directory / "receipt.json"
        if receipt.exists():
            receipt.unlink()
            sync_directory(receipt.parent)
    registry["conflict_queue"] = [
        key for key in registry.get("conflict_queue", []) if key != transaction_id
    ]
    if registry["active"] == transaction_id:
        registry["active"] = registry["conflict_queue"][0] if registry["conflict_queue"] else None
    return loss_projection(root, directory, registry, transaction_id)
