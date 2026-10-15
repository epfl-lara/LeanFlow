"""Observe and preserve compiler journal loss without inventing its unavailable write plan."""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any, Literal, TypedDict

from agent.runtime.harness.processes import start_identity_matches
from core.home import leanflow_home
from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    identity,
    put_blob,
    read_blob,
    sync_directory,
)
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.workflows.project_setup.cleanup import process_states
from leanflow_cli.workflows.project_setup.process import SetupFailure
from leanflow_cli.workflows.project_setup.toolchain_disposal import disposal_asset_observation


class CompilerJournalLossObservation(TypedDict):
    """Bind unreadable journal bytes without inferring its owner or original write plan."""

    schema_version: int
    kind: Literal["unreadable_compiler_journal"]
    journal: str
    target: str
    journal_sha256: str
    journal_bytes: int
    state: Literal["UNREADABLE"]
    versions: dict[str, str | None]
    original_owner_live: bool | None
    owner_status: Literal["unavailable", "known"]
    known_owner: dict[str, Any] | None
    retained_disposal_slots: dict[str, Any]
    parse_error: str
    remediation: str
    observation_id: str


def _failure(
    message: str, *, journal: Path | None = None, target: Path | None = None
) -> SetupFailure:
    """Attach exact recovery inputs to a fail-closed setup diagnostic."""
    details = "" if journal is None else f" Journal: {journal}. Target: {target}."
    return SetupFailure(
        "setup_toolchain_recovery_required",
        message + details,
        "Inspect this compiler journal with python -m leanflow_cli.workflows.project_setup.toolchain_recovery inspect"
        + (
            ""
            if journal is None
            else f" --journal {shlex.quote(str(journal))} --target {shlex.quote(str(target))}"
        )
        + "; preserve its versions and acknowledge the exact observation after checking stopped processes.",
    )


def _journal_bytes(path: Path) -> bytes:
    """Read only a regular controller journal; preserve unreadable bytes and report its exact path."""
    directory = leanflow_home() / "toolchain-installations"
    if path.parent.resolve() != directory.resolve() or path.is_symlink() or not path.is_file():
        raise _failure("Compiler recovery requires its regular controller journal.", journal=path)
    try:
        return path.read_bytes()
    except OSError as error:
        raise _failure(
            "Restore journal read access before observing its exact bytes.", journal=path
        ) from error


def _json_object(raw: bytes) -> dict[str, Any] | None:
    """Read metadata only when the original JSON object remains parseable."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def known_owner(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Recognize only an exact positive PID and nonempty process-start identity."""
    owner = (metadata or {}).get("owner")
    return (
        owner
        if (
            isinstance(owner, dict)
            and type(owner.get("pid")) is int
            and owner["pid"] > 0
            and isinstance(owner.get("start"), str)
            and bool(owner["start"].strip())
        )
        else None
    )


def _parsed_journal(raw: bytes) -> dict[str, Any] | None:
    """Require safe metadata shapes before legacy inspection or automatic recovery."""
    value = _json_object(raw)
    if (
        value is None
        or not isinstance(value.get("target"), str)
        or "\0" in value["target"]
        or not Path(value["target"]).is_absolute()
    ):
        return None
    if "owner" in value and known_owner(value) is None:
        return None
    if any(
        name in value and not isinstance(value[name], str)
        for name in ("state", "attempt_id", "slot", "incoming", "private", "record_id")
    ):
        return None
    # Legacy journals without slot metadata remain inspectable, but no filesystem
    # recovery path may be derived from a missing or malformed attempt identity.
    if any(name in value for name in ("attempt_id", "slot", "incoming", "slot_disposal")):
        attempt = value.get("attempt_id")
        if not isinstance(attempt, str) or re.fullmatch(r"[0-9a-f]{32}", attempt) is None:
            return None
    if "schema_version" in value and type(value["schema_version"]) is not int:
        return None
    if "cleanup_verified" in value and type(value["cleanup_verified"]) is not bool:
        return None
    if "disposition" in value and not isinstance(value["disposition"], dict):
        return None
    if any(
        name in value and value[name] is not None and not isinstance(value[name], str)
        for name in ("before", "after")
    ):
        return None
    inode = value.get("slot_inode")
    if inode is not None and (
        not isinstance(inode, list)
        or len(inode) != 2
        or any(type(item) is not int or item < 0 for item in inode)
    ):
        return None
    return value


def _read_journal(path: Path) -> tuple[dict[str, Any], str]:
    """Require a parsed journal only for operations that use its original write plan."""
    raw = _journal_bytes(path)
    value = _parsed_journal(raw)
    if value is None:
        raise _failure(
            "Compiler publication journal requires raw-loss acknowledgment.", journal=path
        )
    return value, digest(raw)


def _loss_observation(path: Path, target: Path, raw: bytes) -> dict[str, Any]:
    """Expose exact raw bytes and supplied target without inventing parsed ownership."""
    metadata = _json_object(raw)
    owner = known_owner(metadata)
    table = process_states() if owner is not None else {}
    live = bool(
        owner is not None
        and owner["pid"] in table
        and start_identity_matches(table[owner["pid"]][2], owner["start"])
        and owner["pid"] != os.getpid()
    )
    observed: CompilerJournalLossObservation = {
        "schema_version": 1,
        "kind": "unreadable_compiler_journal",
        "journal": str(path),
        "target": str(target),
        "journal_sha256": digest(raw),
        "journal_bytes": len(raw),
        "state": "UNREADABLE",
        "versions": {"target": entry_identity(target)},
        "retained_disposal_slots": disposal_asset_observation(target, metadata, raw_loss=True),
        "original_owner_live": live if owner is not None else None,
        "owner_status": "known" if owner is not None else "unavailable",
        "known_owner": owner,
        "parse_error": "Journal JSON or usable compiler metadata is unavailable",
        "remediation": "Inspect the original raw journal and target locally, confirm every acquisition process stopped, and acknowledge this exact observation. Unknown paths and the original journal remain preserved.",
        "observation_id": "",
    }
    observed["observation_id"] = identity(
        {key: value for key, value in observed.items() if key != "observation_id"}
    )
    return dict(observed)


def _loss_path(path: Path, target: Path, checksum: str, observation_id: str) -> Path:
    """Select immutable acknowledgment authority for exactly one journal/target/byte identity."""
    key = identity([str(path), str(target), checksum, observation_id])
    return contained(path.parent, f"loss-acknowledgments/{key}.json")


def _acknowledged_loss(path: Path, target: Path, raw: bytes) -> bool:
    """Accept only preserved raw loss whose current bytes and supplied target still match."""
    try:
        observed = _loss_observation(path, target, raw)
        authority = _loss_path(path, target, digest(raw), observed["observation_id"])
        if not authority.is_file():
            return False
        value = json.loads(authority.read_bytes())
        return (
            value.get("record_id")
            == identity({key: item for key, item in value.items() if key != "record_id"})
            and value.get("kind") == "unreadable_compiler_journal"
            and value.get("state") == "PRESERVED"
            and value.get("processes_stopped_attested") is True
            and value.get("observation") == observed
            and read_blob(authority.parent / "blobs", value["journal_sha256"]) == raw
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _publish_acknowledgment(path: Path, value: dict[str, Any]) -> None:
    """Publish flushed loss authority exclusively and retain an unequal concurrent version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".loss-ack-", delete=False
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
                raise ValueError("Existing compiler loss acknowledgment changed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def acknowledge_raw_loss(path: Path, target: Path, observed: dict[str, Any]) -> dict[str, Any]:
    """Preserve exact original journal bytes behind immutable observation-bound authority."""
    raw = _journal_bytes(path)
    if _loss_observation(path, target, raw) != observed:
        raise _failure(
            "Compiler journal or target changed after inspection.", journal=path, target=target
        )
    authority = _loss_path(path, target, digest(raw), observed["observation_id"])
    put_blob(authority.parent / "blobs", raw)
    value = {
        "schema_version": 1,
        "kind": "unreadable_compiler_journal",
        "state": "PRESERVED",
        "observation": observed,
        "journal": str(path),
        "target": str(target),
        "journal_sha256": digest(raw),
        "processes_stopped_attested": True,
        "versions_removed": False,
    }
    value["record_id"] = identity(value)
    if _journal_bytes(path) != raw or _loss_observation(path, target, raw) != observed:
        raise _failure("Compiler loss changed before acknowledgment.", journal=path, target=target)
    try:
        _publish_acknowledgment(authority, value)
    except ValueError as error:
        raise _failure(str(error), journal=path, target=target) from error
    return value
