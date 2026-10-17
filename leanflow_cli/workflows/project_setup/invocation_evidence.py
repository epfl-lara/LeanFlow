"""Bind public setup process admission to bounded expected closure records."""

from __future__ import annotations

import hashlib
import json
import re
from itertools import islice
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import digest, durable_json
from leanflow_cli.workflows.project_setup import history_archive

LIMIT = 64 * 1024
MAX_INVOCATIONS = 256
_ID = re.compile(r"[0-9a-f]{32}")


def directory(root: Path) -> Path:
    """Locate command records without creating state during readiness inspection."""
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]
    return leanflow_home() / "setup" / key / "commands"


def _index(root: Path) -> Path:
    return directory(root).parent / "command-index.json"


def expected(root: Path) -> list[str]:
    """Read expected invocation identities; never infer their absence from a glob."""
    path = _index(root)
    if not path.exists() and not path.is_symlink():
        return []
    record = json.loads(read_regular_bytes(path, limit=LIMIT))
    identifiers = record.get("invocations") if isinstance(record, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("schema") != 1
        or record.get("root") != str(root.resolve())
        or not isinstance(identifiers, list)
        or len(identifiers) > MAX_INVOCATIONS
        or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in identifiers)
        or len(identifiers) != len(set(identifiers))
    ):
        raise ValueError(f"Invalid public invocation index: {path}")
    return identifiers


def register(root: Path, identifier: str) -> Path:
    """Persist the expected marker before any process admission can take effect."""
    if not _ID.fullmatch(identifier):
        raise ValueError("Invalid public invocation identity")
    identifiers = expected(root)
    if len(identifiers) >= MAX_INVOCATIONS:
        compact(root)
        identifiers = expected(root)
    if identifier in identifiers or len(identifiers) >= MAX_INVOCATIONS:
        raise ValueError("Public invocation index requires bounded manual inspection")
    directory(root).mkdir(parents=True, exist_ok=True, mode=0o700)
    durable_json(
        _index(root),
        {"schema": 1, "root": str(root.resolve()), "invocations": [*identifiers, identifier]},
    )
    return directory(root) / f"{identifier}.json"


def load_record(
    root: Path, identifier: str, membership: dict[str, str] | None = None
) -> tuple[dict[str, Any], bytes]:
    """Read active or positively archived invocation evidence without reconstructing it."""
    path = directory(root) / f"{identifier}.json"
    if path.exists() or path.is_symlink():
        raw = read_regular_bytes(path, limit=LIMIT)
    else:
        raw = history_archive.read(directory(root) / "archive", identifier, membership)
    value: Any = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"Invalid public invocation record: {path}")
    return value, raw


def record_closed(root: Path, identifier: str, value: dict[str, Any], raw: bytes) -> bool:
    """Keep external acknowledgment distinct from controller-verified physical closure."""
    if value.get("cleanup_verified") is True:
        return True
    path = directory(root).parent / "recovery-acknowledgments" / f"{identifier}.json"
    if not path.exists() and not path.is_symlink():
        return False
    acknowledgement = json.loads(read_regular_bytes(path, limit=LIMIT))
    valid = bool(
        isinstance(acknowledgement, dict)
        and acknowledgement.get("schema") == 1
        and acknowledgement.get("id") == identifier
        and acknowledgement.get("root") == str(root.resolve())
        and acknowledgement.get("ledger_sha256") == digest(raw)
        and acknowledgement.get("external_processes_stopped") is True
        and acknowledgement.get("external_output_closed") is True
        and acknowledgement.get("cleanup_verified") is False
        and acknowledgement.get("retain_inputs") is True
    )
    if not valid:
        return False
    from leanflow_cli.workflows.project_setup.recovery_barrier import acknowledged

    recovery_root = acknowledgement.get("recovery_root")
    if not isinstance(recovery_root, str) or not Path(recovery_root).is_absolute():
        return False
    return acknowledged(
        Path(recovery_root),
        str(acknowledgement.get("observation_id", "")),
        str(acknowledgement.get("barrier_sha256", "")),
    )


def records(
    root: Path, *, required: list[str] | None = None
) -> dict[str, tuple[dict[str, Any], bytes]]:
    """Collect the bounded active set plus exact journal-bound archived identities."""
    identifiers = set(expected(root)) | set(required or [])
    paths = list(islice(directory(root).glob("*.json"), MAX_INVOCATIONS + 1))
    if len(paths) > MAX_INVOCATIONS or len(identifiers) > MAX_INVOCATIONS * 2:
        raise ValueError("Public invocation history exceeds its bounded inspection")
    identifiers.update(path.stem for path in paths)
    membership = history_archive.members(directory(root) / "archive")
    return {
        identifier: load_record(root, identifier, membership) for identifier in sorted(identifiers)
    }


def closed(root: Path, *, required: list[str] | None = None) -> bool:
    """Require indexed or journal-bound evidence, including exact archived membership."""
    try:
        selected = records(root, required=required)
    except (FileNotFoundError, ValueError):
        return False
    archive = directory(root) / "archive"
    membership = history_archive.members(archive)
    for identifier in membership:
        raw = history_archive.read(archive, identifier, membership)
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("cleanup_verified") is not True
            or value.get("id") != identifier
            or value.get("root") != str(root.resolve())
        ):
            return False
    expected_ids = set(expected(root)) | set(required or [])
    for identifier, (value, raw) in selected.items():
        if not record_closed(root, identifier, value, raw):
            return False
        if identifier in expected_ids and (
            value.get("id") != identifier or value.get("root") != str(root.resolve())
        ):
            return False
    return True


def compact(root: Path) -> None:
    """Archive only physically verified closed records; retain every uncertain marker."""
    identifiers = expected(root)
    kept = []
    for identifier in identifiers:
        value, raw = load_record(root, identifier)
        if value.get("cleanup_verified") is not True:
            kept.append(identifier)
            continue
        if value.get("id") != identifier or value.get("root") != str(root.resolve()):
            raise ValueError("Cannot compact an unbound public invocation record")
        history_archive.preserve(directory(root) / "archive", identifier, raw)
        path = directory(root) / f"{identifier}.json"
        if path.exists():
            if read_regular_bytes(path, limit=LIMIT) != raw:
                raise ValueError("Closed invocation changed during compaction")
            path.unlink()
    durable_json(_index(root), {"schema": 1, "root": str(root.resolve()), "invocations": kept})
