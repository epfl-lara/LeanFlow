"""Bind one native run to immutable source identity and cumulative campaign accounting."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.campaign_limits import (
    AccountingError,
    CampaignLimits,
    assert_accounting_resolved,
    optional_limit,
)
from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import contained, durable_json, identity

_LIMIT_NAMES = ("agent_calls", "tokens", "wall_time_s")
_BOUND = 8 * 1024 * 1024
_MISSING = "Legacy or missing campaign accounting; use Start to start a new campaign and retain prior history"


class CampaignOwner:
    """Keep serial ownership until both the caller and its exact checkpoint worker close."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._lock = threading.Lock()
        self._parent_closed = False
        self._worker_closed = True

    def attach_worker(self) -> None:
        """Retain accounting ownership before starting the sole checkpoint worker."""
        with self._lock:
            self._worker_closed = False

    def close(self, *, worker: bool = False) -> None:
        """Release exactly once after the parent and any admitted writer have returned."""
        with self._lock:
            if worker:
                self._worker_closed = True
            else:
                self._parent_closed = True
            if self._parent_closed and self._worker_closed and self._descriptor >= 0:
                os.close(self._descriptor)
                self._descriptor = -1


def _directory(root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid native workflow run ID")
    return contained(root, ".leanflow/native-workflows/" + run_id)


def _read(path: Path) -> dict[str, Any]:
    """Read one bounded, unaliased accounting object without changing it."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Campaign accounting must be an unaliased regular file")
        data = read_regular_bytes(path, _BOUND)
        after = path.lstat()
        version = lambda s: (
            s.st_dev,
            s.st_ino,
            s.st_mode,
            s.st_nlink,
            s.st_size,
            s.st_mtime_ns,
            s.st_ctime_ns,
        )
        if version(before) != version(after):
            raise ValueError("Campaign accounting changed while reading")
        value = json.loads(data)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            "Missing or malformed campaign accounting; start a new campaign"
        ) from error
    if not isinstance(value, dict):
        raise ValueError("Malformed campaign accounting")
    return value


def _limits(value: Mapping[str, Any]) -> dict[str, int | float | None]:
    return {
        name: optional_limit(value.get(name), integer=name != "wall_time_s")
        for name in _LIMIT_NAMES
    }


def _assert_pre_author_directory(directory: Path) -> None:
    """Allow only the narrow controller-owned preflight accounting artifacts."""
    allowed = {
        "campaign-accounting.json",
        "campaign-accounting.lock",
        "controller.lock",
        "limits.json",
    }
    if directory.exists() and any(path.name not in allowed for path in directory.iterdir()):
        raise ValueError("Unadmitted author workspace exists; retain it and start a new campaign")


@contextmanager
def campaign_lock(root: Path, run_id: str) -> Iterator[CampaignOwner]:
    """Serialize accounting owners without holding the project-setup controller lock."""
    directory = _directory(root, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = contained(
        root, directory.relative_to(root.resolve()).as_posix() + "/campaign-accounting.lock"
    )
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    owner = CampaignOwner(descriptor)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError("Invalid campaign accounting lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("This campaign already has an active accounting owner") from error
        yield owner
    finally:
        owner.close()


def load_campaign(root: Path, run_id: str) -> dict[str, Any]:
    """Validate the exact per-run accounting binding without consulting preparation freshness."""
    path = _directory(root, run_id) / "campaign-accounting.json"
    if not path.exists():
        raise ValueError(_MISSING)
    binding = _read(path)
    fields = {
        "schema_version",
        "root",
        "run_id",
        "document_id",
        "source_bundle",
        "selection_token",
        "backend",
        "source_draft_only",
        "effective_draft",
        "limits",
        "budget_relative",
        "binding_id",
    }
    if (
        set(binding) != fields
        or type(binding["schema_version"]) is not int
        or binding["schema_version"] != 1
        or binding["root"] != str(root.resolve())
        or binding["run_id"] != run_id
        or not isinstance(binding["document_id"], str)
        or not binding["document_id"]
        or not isinstance(binding["source_bundle"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", binding["source_bundle"])
        or not isinstance(binding["selection_token"], str)
        or not binding["selection_token"]
        or binding["backend"] not in {"codex", "claude", "lemex"}
        or type(binding["source_draft_only"]) is not bool
        or type(binding["effective_draft"]) is not bool
        or not isinstance(binding["limits"], dict)
        or set(binding["limits"]) != set(_LIMIT_NAMES)
        or _limits(binding["limits"]) != binding["limits"]
        or binding["binding_id"]
        != identity({k: v for k, v in binding.items() if k != "binding_id"})
    ):
        raise ValueError("Malformed campaign binding; start a new campaign")
    _budget_path(root, binding)
    return binding


def _budget_path(root: Path, binding: Mapping[str, Any]) -> Path:
    relative = binding["budget_relative"]
    path = contained(root, relative)
    if not (
        re.fullmatch(r"\.leanflow/formalization-campaigns/[0-9a-f]{32}/budget\.json", relative)
        or relative == f".leanflow/native-workflows/{binding['run_id']}/limits.json"
    ):
        raise ValueError("Campaign budget path is outside its bound run")
    return path


def campaign_budget(root: Path, binding: dict[str, Any]) -> Path:
    """Require the positively bound existing ledger, retaining unknown or exhausted usage."""
    if load_campaign(root, binding["run_id"]) != binding:
        raise ValueError("Campaign binding changed")
    path = _budget_path(root, binding)
    ledger = _read(path)
    elapsed = ledger.get("elapsed_s")
    calls = ledger.get("invocations")
    if (
        ledger.get("version") != 2
        or ledger.get("campaign_binding_id") != binding["binding_id"]
        or ledger.get("limits") != binding["limits"]
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
        or not isinstance(calls, dict)
    ):
        raise ValueError("Malformed campaign budget; start a new campaign")
    assert_accounting_resolved(ledger)
    for key, call in calls.items():
        if (
            not isinstance(key, str)
            or not isinstance(call, dict)
            or call.get("status") not in {"reserved", "running", "finished", "not_started"}
        ):
            raise ValueError("Malformed campaign invocation")
        usage = call.get("usage")
        if usage is not None and (
            not isinstance(usage, dict)
            or any(type(number) is not int or number < 0 for number in usage.values())
        ):
            raise ValueError("Malformed campaign usage")
    return path


def select_campaign(
    root: Path,
    record: Mapping[str, Any],
    *,
    run_id: str,
    limits: Mapping[str, Any],
    backend: str,
    selection_token: str,
    source_draft_only: bool,
    effective_draft: bool,
    resume: bool,
    budget_path: Path | None = None,
) -> dict[str, Any]:
    """Create a fresh binding or reuse its exact ledger before any paid preparation work."""
    directory = _directory(root, run_id)
    expected = {
        "schema_version": 1,
        "root": str(root.resolve()),
        "run_id": run_id,
        "document_id": record["document_id"],
        "source_bundle": record["source_bundle"],
        "selection_token": selection_token,
        "backend": backend,
        "source_draft_only": source_draft_only,
        "effective_draft": effective_draft,
        "limits": _limits(limits),
    }
    path = directory / "campaign-accounting.json"
    if resume:
        binding = load_campaign(root, run_id)
        if expected["limits"] != binding["limits"]:
            raise ValueError("Start a new campaign using Start when changing limits")
        if any(binding[key] != value for key, value in expected.items()):
            raise ValueError("Campaign source or run identity changed; start a new campaign")
        selected = campaign_budget(root, binding)
        if budget_path is not None and budget_path.absolute() != selected:
            raise ValueError("Requested budget differs from the bound campaign")
        validate_author_history(root, binding, state_path=directory / "state.json")
        return binding
    if path.exists() or path.is_symlink() or (directory / "state.json").exists():
        raise ValueError(
            "This campaign already exists; resume it explicitly or start a new campaign"
        )
    _assert_pre_author_directory(directory)
    if budget_path is None:
        relative = ".leanflow/formalization-campaigns/" + uuid.uuid4().hex + "/budget.json"
    else:
        relative = f".leanflow/native-workflows/{run_id}/limits.json"
        if budget_path.absolute() != contained(root, relative):
            raise ValueError("Only the exact native run budget can be selected")
    binding = {**expected, "budget_relative": relative}
    binding["binding_id"] = identity(binding)
    ledger_path = _budget_path(root, binding)
    if ledger_path.exists() or ledger_path.is_symlink():
        raise ValueError("An unbound campaign ledger already exists; start a new campaign")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    budget = CampaignLimits(ledger_path, binding["limits"])
    budget.state["campaign_binding_id"] = binding["binding_id"]
    budget.persist()
    durable_json(path, binding)
    return load_campaign(root, run_id)


def validate_author_history(root: Path, binding: dict[str, Any], *, state_path: Path) -> bool:
    """Read existing admission history before paid preparation without creating or repairing it."""
    directory = _directory(root, binding["run_id"])
    if state_path != directory / "state.json":
        raise ValueError("Author state differs from the bound native run")
    marker_path = directory / "author-admitted.json"
    expected = {
        "schema_version": 1,
        "binding_id": binding["binding_id"],
        "run_id": binding["run_id"],
    }
    if marker_path.exists() or marker_path.is_symlink():
        if _read(marker_path) != expected:
            raise ValueError("Author admission identity changed; start a new campaign")
        if not state_path.exists():
            raise ValueError(
                "Admitted author state is missing; retain inputs for recovery or start a new campaign"
            )
        state = _read(state_path)
        if (
            state.get("run_id") != binding["run_id"]
            or state.get("campaign_binding_id") != binding["binding_id"]
        ):
            raise ValueError("Author checkpoint does not match campaign accounting")
        if state.get("accounting_unresolved"):
            raise AccountingError("Author accounting is unresolved; retain its ledger and draft")
        if state.get("native_cleanup_unverified") or state.get("independent_cleanup_unverified"):
            raise ValueError(
                "Prior native process closure is unverified; retain its private inputs and start a separate campaign"
            )
        if (
            type(state.get("version")) is not int
            or state["version"] != 2
            or state.get("backend") != binding["backend"]
            or state.get("workflow") != "formalize"
        ):
            raise ValueError(
                "Start a new native run when changing backends or migrating legacy runs"
            )
        for field, expected_mode in (
            ("source_draft_intent", binding["source_draft_only"]),
            ("source_draft_only", binding["effective_draft"]),
        ):
            value = state.get(field, False)
            if type(value) is not bool or value != expected_mode:
                raise ValueError(
                    "Author checkpoint source-draft mode changed; start a new campaign"
                )
        if state.get("formalization_selection_token") != binding["selection_token"]:
            raise ValueError("Formalization selection changed; restart from its selected revision")
        baseline = _checkpoint_baseline(state, "baseline")
        if "formalization_baseline" in state:
            _checkpoint_baseline(state, "formalization_baseline")
        from leanflow_cli.native.harness_artifacts import source_files

        if source_files(root) != baseline:
            raise ValueError("Canonical sources changed; start a new workflow")
        return True
    if state_path.exists() or state_path.is_symlink():
        raise ValueError("Legacy author state has no campaign admission; start a new campaign")
    _assert_pre_author_directory(directory)
    return False


def _checkpoint_baseline(state: Mapping[str, Any], name: str) -> dict[str, bytes]:
    """Validate the existing checkpoint byte-map format without coercing malformed values."""
    value = state.get(name)
    if not isinstance(value, dict) or any(
        not isinstance(path, str) or not isinstance(data, str) for path, data in value.items()
    ):
        raise ValueError("Author checkpoint has a missing or malformed " + name)
    try:
        return {path: bytes.fromhex(data) for path, data in value.items()}
    except ValueError as error:
        raise ValueError("Author checkpoint has malformed " + name + " bytes") from error


def admit_author(root: Path, binding: dict[str, Any], *, resume: bool, state_path: Path) -> bool:
    """Mark first author admission before state creation, never recreating a lost checkpoint."""
    campaign_budget(root, binding)
    if validate_author_history(root, binding, state_path=state_path):
        if not resume:
            raise ValueError("Author admission identity changed; start a new campaign")
        return True
    marker_path = _directory(root, binding["run_id"]) / "author-admitted.json"
    expected = {
        "schema_version": 1,
        "binding_id": binding["binding_id"],
        "run_id": binding["run_id"],
    }
    durable_json(marker_path, expected)
    return False
