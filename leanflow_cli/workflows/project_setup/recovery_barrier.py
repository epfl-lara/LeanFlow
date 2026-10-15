"""Bind external setup acknowledgments to positive crash-safe fresh-readiness barriers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import durable_json, identity

LIMIT = 64 * 1024
CONFIGURATION = {"lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json"}


def directory(root: Path) -> Path:
    """Keep every recovery barrier within unaliased baseline infrastructure."""
    from leanflow_cli.workflows.project_setup.baseline_transaction import _base

    path = _base(root) / "recovery"
    if path.is_symlink():
        raise ValueError("Baseline recovery directory is aliased")
    return path


def _read(path: Path) -> dict[str, Any]:
    """Reject malformed control documents instead of treating them as absent barriers."""
    value = json.loads(read_regular_bytes(path, limit=LIMIT))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("Baseline recovery barrier is invalid")
    return value


def _validate(value: dict[str, Any], root: Path) -> None:
    """Require exact root, canonical denominator and explicit incomplete/complete intent."""
    canonical = value.get("canonical")
    if (
        value.get("root") != str(root)
        or re.fullmatch(r"[0-9a-f]{64}", str(value.get("observation_id", ""))) is None
        or not isinstance(canonical, dict)
        or set(canonical) != CONFIGURATION
        or type(value.get("acknowledgment_complete")) is not bool
        or value.get("fresh_setup_required") is not True
        or value.get("retain_inputs") is not True
    ):
        raise ValueError("Baseline recovery barrier binding is invalid")
    for observed in canonical.values():
        if observed is not None and (
            not isinstance(observed, dict)
            or not isinstance(observed.get("inode"), list)
            or len(observed["inode"]) != 2
            or any(type(number) is not int or number < 0 for number in observed["inode"])
            or re.fullmatch(r"[0-9a-f]{64}|nonregular", str(observed.get("sha256", ""))) is None
        ):
            raise ValueError("Baseline recovery canonical observation is invalid")


def current(root: Path, *, required: bool = False) -> dict[str, Any] | None:
    """Require both positive anchor and exact payload, including after deletion or a crash."""
    base = directory(root)
    anchor, target = base.parent / "recovery.anchor", base / "current.json"
    if (
        not anchor.exists()
        and not anchor.is_symlink()
        and not target.exists()
        and not target.is_symlink()
    ):
        if required or (base.exists() and next(base.iterdir(), None) is not None):
            raise ValueError("Baseline recovery current barrier is missing; retain all inputs")
        return None
    expected, value = _read(anchor), _read(target)
    _validate(value, root)
    if (
        expected.get("root") != str(root)
        or expected.get("observation_id") != value["observation_id"]
        or expected.get("binding_sha256") != identity(value)
    ):
        raise ValueError("Baseline recovery barrier is incomplete or changed")
    return value


def write(root: Path, value: dict[str, Any]) -> None:
    """Publish the required anchor first so interruption never removes a prior barrier."""
    _validate(value, root)
    base = directory(root)
    durable_json(
        base.parent / "recovery.anchor",
        {
            "schema": 1,
            "root": str(root),
            "observation_id": value["observation_id"],
            "binding_sha256": identity(value),
        },
    )
    durable_json(base / "current.json", value)


def acknowledged(root: Path, observation_id: str, checksum: str) -> bool:
    """Require completed positive barriers without equating an external claim with EOF."""
    selected = current(root, required=True)
    if selected is None or selected["acknowledgment_complete"] is not True:
        return False
    if re.fullmatch(r"[0-9a-f]{64}", observation_id) is None:
        return False
    value = _read(directory(root) / f"barrier-{observation_id}.json")
    _validate(value, root)
    return bool(
        value["observation_id"] == observation_id
        and value["acknowledgment_complete"] is True
        and identity(value) == checksum
    )
