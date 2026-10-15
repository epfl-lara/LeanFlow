"""Exclude dependency setup from active workflows without writing inside the project."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from core.process_identity import (
    _process_token,
    current_process_identity,
    process_identity_from_mapping,
    process_identity_matches,
    process_token_sha256,
)

_TERMINAL_PHASES = frozenset(
    {"completed", "exited", "stopped", "interrupted", "cancelled", "failed", "error", "dead"}
)


class ProjectBusyError(RuntimeError):
    """Report project ownership conflicts without changing the owner's state."""


@contextmanager
def _project_lock(root: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold a nonblocking cross-process lock outside the canonical project root."""
    key = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()
    directory = leanflow_home() / "project-operations"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{key}.lock").open("a") as handle:
        try:
            fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            message = (
                "A workflow is running or project setup is already in progress. "
                "Wait for it to finish, then retry setup."
                if exclusive
                else "Project setup is in progress. Wait for setup to finish, then start the run again."
            )
            raise ProjectBusyError(message) from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _process_is_live(payload: Mapping[str, Any]) -> bool:
    """Revalidate modern process identity and conservatively retain legacy PID ownership."""
    identity = process_identity_from_mapping(payload)
    if identity.verifiable:
        return process_identity_matches(identity)
    if identity.pid <= 1:
        return False
    try:
        os.kill(identity.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _check_status(path: Path, root: Path, *, require_root: bool = False) -> None:
    """Refuse mutation when a persisted nonterminal workflow still owns a live process."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, ValueError) as error:
        raise ProjectBusyError(
            f"Cannot verify workflow ownership in {path}: {error}. "
            "Resolve the unreadable status before retrying setup."
        ) from error
    if not isinstance(payload, dict):
        raise ProjectBusyError(f"Cannot verify workflow ownership: invalid status in {path}.")
    recorded_root = payload.get("project_root")
    if require_root and not recorded_root:
        return
    if recorded_root and Path(str(recorded_root)).expanduser().resolve() != root:
        return
    if payload.get("terminal") is True or str(payload.get("phase", "")) in _TERMINAL_PHASES:
        return
    if str(payload.get("status", "")) in _TERMINAL_PHASES:
        return
    identity = process_identity_from_mapping(payload)
    current = current_process_identity()
    if (
        payload.get("owner_role") == "formalization-preflight"
        and payload.get("workflow_kind") == "formalize"
        and payload.get("phase") == "starting"
        and identity.verifiable
        and identity == current
        and process_token_sha256(_process_token(current.pid)) == current.token_sha256
    ):
        # This exact preflight controller must prepare its own dependencies;
        # foreign owners and every normal workflow still exclude setup.
        return
    if _process_is_live(payload):
        raise ProjectBusyError(
            "A workflow is running in this project. Wait for it to finish or stop it "
            "before retrying setup."
        )


@contextmanager
def _legacy_controllers(root: Path) -> Iterator[None]:
    """Retain existing controller locks so pre-upgrade workflows are protected too."""
    state = root / ".leanflow" / "workflow-state"
    locks = [state / "prover" / "controller.lock"]
    locks.extend(sorted((root / ".leanflow" / "native-workflows").glob("*/controller.lock")))
    with ExitStack() as owners:
        for path in locks:
            if path.is_symlink():
                raise ProjectBusyError(
                    f"Cannot safely verify the symlinked controller lock {path}."
                )
            try:
                handle = owners.enter_context(path.open("rb"))
            except FileNotFoundError:
                continue
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ProjectBusyError(
                    "A workflow is running in this project. Wait for it to finish or stop it "
                    "before retrying setup."
                ) from error
        _check_status(state / "live_status.json", root)
        for path in sorted((state / "activity" / "run-metadata").glob("*.json")):
            _check_status(path, root)
        _check_status(
            leanflow_home() / "workflow-state" / "live_status.json", root, require_root=True
        )
        yield


@contextmanager
def project_setup(root: Path) -> Iterator[None]:
    """Reserve setup exclusively before creating files, dependencies, or project logs."""
    canonical = root.expanduser().resolve()
    with _project_lock(canonical, exclusive=True), _legacy_controllers(canonical):
        yield


@contextmanager
def project_activity(root: Path) -> Iterator[None]:
    """Keep setup excluded through the complete workflow, including terminal publication."""
    with _project_lock(root.expanduser().resolve(), exclusive=False):
        from leanflow_cli.formalization.preparation_transaction import assert_no_pending_preparation

        assert_no_pending_preparation(root)
        yield
