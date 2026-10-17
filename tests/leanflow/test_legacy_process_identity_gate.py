"""Retain persisted inputs when an extant owner has an uncomparable legacy locale identity."""

from __future__ import annotations

import json
import os

import pytest

from leanflow_cli.formalization import (
    preparation_disposal,
    preparation_lifecycle,
    preparation_transaction,
)
from leanflow_cli.workflows.project_setup import toolchain_journal_loss, toolchain_recovery


@pytest.mark.parametrize("legacy", ["金 10月 16 12:00:00 2026", "ven. oct. 16 12:00:00 2026"])
@pytest.mark.parametrize(
    "caller",
    [
        "compiler_observe",
        "compiler_raw_loss",
        "preparation_disposal",
        "private_precommit",
        "preparation_commit",
    ],
)
def test_extant_legacy_owner_cannot_be_classified_dead_by_a_new_c_probe(
    tmp_path, monkeypatch, legacy, caller
):
    """Exercise actual ownership gates before they may dispose or acknowledge retained versions."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state"))
    root, staged = tmp_path / "project", tmp_path / "stage"
    root.mkdir()
    staged.mkdir()
    current = "Fri Oct 16 12:00:00 2026"
    owner = {"pid": 411, "start": legacy}
    table = {411: (411, "S", current), os.getpid(): (os.getpid(), "S", current)}
    for module in (
        toolchain_recovery,
        toolchain_journal_loss,
        preparation_disposal,
        preparation_lifecycle,
        preparation_transaction,
    ):
        monkeypatch.setattr(module, "process_states", lambda: table)
    target = tmp_path / "compiler"
    journal = {"target": str(target), "owner": owner, "state": "REGISTERED"}
    raw = json.dumps(journal).encode()
    monkeypatch.setattr(toolchain_recovery, "_journal_bytes", lambda path: raw)
    saved = []
    monkeypatch.setattr(
        preparation_disposal,
        "load_registry",
        lambda project: {"transactions": ["fixture"], "journal_losses": {}, "current": "another"},
    )
    monkeypatch.setattr(
        preparation_disposal,
        "load_journal",
        lambda *args: {"state": "ROLLED_BACK", "owner": owner, "writes": {}},
    )
    monkeypatch.setattr(
        preparation_disposal,
        "precommit_cleanup",
        lambda journal: {"marker_id": "previously-closed"},
    )
    monkeypatch.setattr(preparation_disposal, "save_journal", lambda *args: saved.append(args))
    with pytest.raises(ValueError, match="uncomparable legacy date"):
        if caller == "compiler_observe":
            toolchain_recovery.compiler_recovery_observation(tmp_path / "journal.json", target)
        elif caller == "compiler_raw_loss":
            toolchain_journal_loss._loss_observation(tmp_path / "journal.json", target, raw)
        elif caller == "preparation_disposal":
            preparation_disposal.dispose_preparation_incoming(root, cleanup_verified=True)
        elif caller == "private_precommit":
            info = staged.stat()
            lease = preparation_lifecycle.PrivateStage(
                staged,
                staged,
                {
                    "pid": os.getpid(),
                    "start": legacy,
                    "stage_device": info.st_dev,
                    "stage_inode": info.st_ino,
                    "stage_id": "fixture",
                },
                cleanup_verified=True,
                cleanup_observed=True,
            )
            lease.precommit_cleanup()
        else:
            preparation_transaction.commit_preparation(
                root,
                staged,
                managed={},
                protected={},
                receipt={"precommit_cleanup": {"owner": {"pid": os.getpid(), "start": legacy}}},
                validate=lambda: None,
                cancelled=lambda: False,
            )
    assert not saved
    assert root.is_dir() and staged.is_dir()
