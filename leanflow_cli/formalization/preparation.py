"""Prepare, verify and bind infrastructure before native mathematical translation starts."""

from __future__ import annotations

import json
import math
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.environment import environment_identity
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.inspector_readiness import valid_inspector_readiness
from leanflow_cli.formalization.preparation_history import MAX_CONTROL_BYTES
from leanflow_cli.formalization.preparation_lifecycle import private_stage
from leanflow_cli.formalization.preparation_membership import require_selected_preparation
from leanflow_cli.formalization.preparation_plan import (
    default_preparation_plan,
    validate_preparation_plan,
)
from leanflow_cli.formalization.preparation_run import prepare_private_stage
from leanflow_cli.formalization.preparation_transaction import (
    assert_no_pending_preparation,
    entry_identity,
    preparation_directory,
    recover_preparation,
)
from leanflow_cli.formalization.publication_io import publication_lock
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.runtime.project_operations import project_setup
from leanflow_cli.runtime.storage import ensure_storage_capacity
from leanflow_cli.workflows.project_setup.process import Progress
from leanflow_cli.workflows.project_setup.scaffold import validate_mutable_paths
from leanflow_cli.workflows.prover.source import project_sources


def _stored_receipt(path: Path) -> dict[str, Any] | None:
    """Read a bounded historical receipt without treating its self-hash as authority."""
    if not path.exists() and not path.is_symlink():
        return None
    receipt = json.loads(read_regular_bytes(path, MAX_CONTROL_BYTES))
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("status") != "prepared"
        or receipt.get("preparation_id")
        != "preparation-"
        + identity({key: value for key, value in receipt.items() if key != "preparation_id"})
    ):
        raise ValueError("Preparation receipt is corrupt or unsupported")
    return receipt


def load_preparation_receipt(root: Path) -> dict[str, Any] | None:
    """Read receipt authority only after locked history audit and positive committed selection."""
    root = root.resolve()
    path = preparation_directory(root) / "receipt.json"
    if not path.exists() and not path.is_symlink():
        assert_no_pending_preparation(root)
        return None
    with publication_lock(root):
        assert_no_pending_preparation(root)
        receipt = _stored_receipt(path)
        if receipt is not None:
            require_selected_preparation(root, receipt)
        return receipt


def assert_preparation_current(
    root: Path,
    expected_id: str | None = None,
    expected_environment: str | None = None,
    *,
    check_project_sources: bool = True,
) -> dict[str, Any]:
    """Require exact checked infrastructure before starting or expanding translation."""
    receipt = load_preparation_receipt(root)
    if receipt is None:
        raise ValueError("Prepare this project before source translation")
    if not valid_inspector_readiness(receipt.get("inspector_readiness")):
        raise RepreparationRequired(
            "Prepared Lean compatibility was not checked by the current inspector; prepare again before translation"
        )
    if expected_id is not None and receipt["preparation_id"] != expected_id:
        raise ValueError("Selected project preparation changed; restart the translation")
    if check_project_sources:
        current = environment_identity(root)
    else:
        from leanflow_cli.formalization.environment_display import display_environment_identity

        current = display_environment_identity(root, checker=environment_identity)
    if current != receipt["environment_id"] or (
        expected_environment is not None and current != expected_environment
    ):
        raise ValueError(
            "Prepared compiler, dependencies or checker changed; prepare and review again"
        )
    if check_project_sources:
        if _source_identity(root) != receipt["project_sources"]:
            raise ValueError("Prepared project source changed; estimate and check setup again")
    return receipt


def _source_identity(root: Path) -> dict[str, str | None]:
    """Fingerprint local mathematical files without re-reading dependency infrastructure."""
    return {
        path.relative_to(root).as_posix(): entry_identity(path)
        for path in project_sources(root)
        if path.name != "lakefile.lean"
    }


def preparation_status(root: Path, *, verify_current: bool = True) -> dict[str, Any]:
    """Expose actionable readiness without interpreting a historical setup success as current."""
    if not verify_current:
        try:
            directory = preparation_directory(root)
            receipt = _stored_receipt(directory / "receipt.json")
            if receipt is None:
                return {
                    "status": "preparation_required",
                    "current": False,
                    "currentness": "unknown",
                    "receipt": None,
                    "reason": "No stored preparation receipt",
                }
            registry = json.loads(
                read_regular_bytes(directory / "registry.json", MAX_CONTROL_BYTES)
            )
            if (
                not isinstance(registry, dict)
                or registry.get("schema_version") != 1
                or registry.get("root") != str(root.resolve())
            ):
                raise ValueError("Preparation registry header is corrupt or unsupported")
            return {
                "status": "historical",
                "current": False,
                "currentness": "historical",
                "receipt": receipt,
                "reason": "Stored preparation only; current history, compiler and dependencies have not been checked",
            }
        except (OSError, ValueError, TypeError) as error:
            return {
                "status": "preparation_unknown",
                "current": False,
                "currentness": "unknown",
                "receipt": None,
                "reason": str(error),
            }
    try:
        receipt = assert_preparation_current(root, check_project_sources=False)
        if _source_identity(root) != receipt["project_sources"]:
            raise ValueError(
                "Prepared project source changed; estimate and check setup before the next translation"
            )
        return {"status": "prepared", "current": True, "receipt": receipt, "reason": ""}
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        return {
            "status": "preparation_required",
            "current": False,
            "receipt": None,
            "reason": str(error),
        }


def prepare_document_project(
    root: Path,
    record: dict[str, Any],
    *,
    plan: dict[str, Any] | None = None,
    preset: str = "mathlib",
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    timeout_s: float = 1800,
    network_allowed: bool = True,
    budget_path: Path | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Prepare one exact plan under exclusive ownership, with all acquisition staged privately."""
    root = root.resolve()
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("Preparation needs a positive finite time budget")
    plan = validate_preparation_plan(
        root, record, plan or default_preparation_plan(root, record, preset=preset)
    )
    if plan["blockers"]:
        return {
            "status": "preparation_required",
            "accepted": False,
            "plan": plan,
            "error": "; ".join(plan["blockers"]),
        }
    cancel = cancel or threading.Event()
    deadline = time.monotonic() + timeout_s

    def cancelled() -> bool:
        return cancel.is_set() or time.monotonic() >= deadline

    with project_setup(root):
        recover_preparation(root)
        from leanflow_cli.formalization.publication import (
            assert_no_pending_publication,
            recover_pending_publication,
        )

        recover_pending_publication(root)
        assert_no_pending_publication(root)
        from leanflow_cli.formalization.preparation_reuse import reuse_preparation

        try:
            previous = assert_preparation_current(root)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError):
            previous = None
        if previous:
            reused = reuse_preparation(
                root,
                record,
                plan,
                previous,
                budget_path=budget_path,
                cancelled=cancelled,
                checkpoint=checkpoint,
            )
            if reused:
                return reused
        validate_mutable_paths(root)
        ensure_storage_capacity(root)
        with private_stage(root) as lease:
            return prepare_private_stage(
                root,
                record,
                plan,
                lease=lease,
                progress=progress,
                cancel=cancel,
                deadline=deadline,
                cancelled=cancelled,
                network_allowed=network_allowed,
                budget_path=budget_path,
                checkpoint=checkpoint,
            )
