"""Keep lost preparation indexes from hiding retained conflicts or authorizing receipts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization import preparation, preparation_launch
from leanflow_cli.formalization import preparation_transaction as transaction
from leanflow_cli.formalization.evidence import identity
from tests.leanflow.preparation_helpers import inspector_readiness_record


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(preparation, "environment_identity", lambda root: "checked-environment")


def _commit(root: Path, stage: Path, text: str) -> dict:
    root.mkdir(exist_ok=True)
    stage.mkdir()
    (stage / "lakefile.toml").write_text(text)
    receipt = {
        "schema_version": 1,
        "status": "prepared",
        "environment_id": "checked-environment",
        "project_sources": {},
        "inspector_readiness": inspector_readiness_record(),
    }
    receipt["preparation_id"] = "preparation-" + identity(receipt)
    return transaction.commit_preparation(
        root,
        stage,
        managed={"lakefile.toml": transaction.entry_identity(root / "lakefile.toml")},
        protected={},
        receipt=receipt,
        validate=lambda: None,
        cancelled=lambda: False,
    )


@pytest.mark.parametrize("index", ["missing", "older"])
def test_lost_index_cannot_hide_late_retained_conflict(tmp_path, index):
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text("original")
    first = _commit(root, tmp_path / "first", "first prepared")
    registry_path = transaction.preparation_directory(root) / "registry.json"
    old_index = registry_path.read_bytes()
    second = _commit(root, tmp_path / "second", "second prepared")
    retained = root / second["writes"]["lakefile.toml"]["captured"]
    retained.write_text("late descriptor edit")
    canonical = (root / "lakefile.toml").read_bytes()
    with pytest.raises(transaction.PreparationConflict):
        transaction.assert_no_pending_preparation(root)
    if index == "missing":
        registry_path.unlink()
    else:
        registry_path.write_bytes(old_index)
    with pytest.raises(transaction.PreparationConflict, match="registry"):
        transaction.assert_no_pending_preparation(root)
    with pytest.raises(ValueError, match="registry"):
        preparation.assert_preparation_current(root)
    assert preparation.preparation_status(root)["current"] is False
    assert (root / "lakefile.toml").read_bytes() == canonical
    assert retained.read_text() == "late descriptor edit"
    assert (root / first["writes"]["lakefile.toml"]["captured"]).read_text() == "original"


def test_current_receipt_requires_selected_committed_membership(tmp_path):
    root = tmp_path / "project"
    _commit(root, tmp_path / "stage", "prepared")
    assert preparation.assert_preparation_current(root)["status"] == "prepared"
    path = transaction.preparation_directory(root) / "registry.json"
    registry = json.loads(path.read_text())
    registry["selected"] = None
    path.write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="selected|committed"):
        preparation.assert_preparation_current(root)


def test_ready_baseline_cannot_admit_planning_after_registry_loss(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _commit(root, tmp_path / "stage", "prepared")
    (transaction.preparation_directory(root) / "registry.json").unlink()
    monkeypatch.setattr(
        preparation_launch, "inspect_project", lambda root: {"ready": True, "configured": True}
    )
    with pytest.raises(ValueError, match="registry"):
        preparation_launch._require_project_baseline(root)


def _observation(root: Path, transaction_id="preparation-registry-loss") -> dict:
    shown = next(
        row
        for row in transaction.preparation_conflicts(root)["transactions"]
        if row["transaction_id"] == transaction_id
    )
    return {
        key: shown[key]
        for key in (
            "transaction_id",
            "destinations",
            "retained_slots",
            "history_id",
            "observation_id",
        )
    }


@pytest.mark.parametrize("index", ["missing", "older"])
def test_registry_acknowledgment_preserves_versions_and_requires_fresh_commit(tmp_path, index):
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text("original")
    _commit(root, tmp_path / "first", "first prepared")
    directory = transaction.preparation_directory(root)
    old_index = (directory / "registry.json").read_bytes()
    second = _commit(root, tmp_path / "second", "second prepared")
    captured = root / second["writes"]["lakefile.toml"]["captured"]
    captured.write_text("late descriptor edit")
    with pytest.raises(ValueError):
        transaction.assert_no_pending_preparation(root)
    old_receipt = (directory / "receipt.json").read_bytes()
    if index == "missing":
        (directory / "registry.json").unlink()
    else:
        (directory / "registry.json").write_bytes(old_index)
    with pytest.raises(ValueError, match="registry"):
        transaction.recover_preparation(root)
    shown = transaction.preparation_conflicts(root)["transactions"]
    assert shown[0]["unavailable_registry"] is True and shown[0]["planned_versions"] is None
    result = transaction.resolve_preparation_conflict(
        root, _observation(root), "Reviewed index loss; preserve every retained version"
    )
    assert result["state"] == "RESOLVED" and result["requires_new_preparation"]
    assert not (directory / "receipt.json").exists()
    assert any(
        path.read_bytes() == old_receipt
        for path in (directory / "index-history").glob("receipt-*.json")
    )
    assert captured.read_text() == "late descriptor edit"
    assert (root / "lakefile.toml").read_text() == "second prepared"
    # Acknowledging index loss cannot resolve the surviving actual transaction conflict.
    with pytest.raises(ValueError):
        transaction.assert_no_pending_preparation(root)
    transaction.resolve_preparation_conflict(
        root,
        _observation(root, second["transaction_id"]),
        "Reviewed the actual late descriptor version",
    )
    transaction.assert_no_pending_preparation(root)
    with pytest.raises(ValueError, match="Prepare this project"):
        preparation.assert_preparation_current(root)
    fresh = _commit(root, tmp_path / "fresh", "fresh checked infrastructure")
    assert preparation.assert_preparation_current(root) == fresh["plan"]["receipt"]


@pytest.mark.parametrize("changed", ["canonical", "retained", "progress"])
def test_registry_loss_resolution_rejects_changed_observation(tmp_path, changed):
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text("original")
    journal = _commit(root, tmp_path / "stage", "prepared")
    directory = transaction.preparation_directory(root)
    progress = directory / "progress" / journal["transaction_id"] / "old" / "record.json"
    progress.parent.mkdir(parents=True)
    progress.write_text("original progress context")
    (directory / "registry.json").unlink()
    observed = _observation(root)
    target = {
        "canonical": root / "lakefile.toml",
        "retained": root / journal["writes"]["lakefile.toml"]["captured"],
        "progress": progress,
    }[changed]
    target.write_text("changed after inspection")
    with pytest.raises(ValueError, match="observation changed"):
        transaction.resolve_preparation_conflict(root, observed, "Acknowledge")
    assert target.read_text() == "changed after inspection"
    assert not (directory / "registry.json").exists()


def test_restored_index_cannot_undo_acknowledgment_with_same_transaction_set(tmp_path):
    root = tmp_path / "project"
    _commit(root, tmp_path / "stage", "prepared")
    path = transaction.preparation_directory(root) / "registry.json"
    before = path.read_bytes()
    path.unlink()
    transaction.resolve_preparation_conflict(
        root, _observation(root), "Revalidate after lost selection"
    )
    transaction.assert_no_pending_preparation(root)
    path.write_bytes(before)
    with pytest.raises(ValueError, match="registry"):
        transaction.assert_no_pending_preparation(root)


def test_genuinely_fresh_project_and_incidental_empty_slot_do_not_create_authority(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    transaction.assert_no_pending_preparation(root)
    assert transaction.recover_preparation(root) is None
    assert not (root / ".leanflow").exists()
    empty = root / ".leanflow/preparation-transactions" / ("a" * 32)
    empty.mkdir(parents=True)
    (empty.parent / ".DS_Store").write_bytes(b"incidental metadata")
    transaction.assert_no_pending_preparation(root)
    assert transaction.preparation_conflicts(root)["transactions"] == []


@pytest.mark.parametrize("asset", ["receipt.json", "registry.json"])
def test_fifo_control_never_blocks_preparation_admission(tmp_path, asset):
    root = tmp_path / "project"
    _commit(root, tmp_path / "stage", "prepared")
    path = transaction.preparation_directory(root) / asset
    path.unlink()
    os.mkfifo(path)
    code = (
        "from pathlib import Path; "
        "from leanflow_cli.formalization.preparation_transaction import assert_no_pending_preparation; "
        f"root=Path({str(root)!r})\n"
        "try: assert_no_pending_preparation(root)\n"
        "except ValueError as error: print(error)\n"
        "else: raise AssertionError('FIFO control granted admission')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and "registry" in result.stdout
    assert path.is_fifo()


def test_historical_status_reads_headers_without_authority_or_environment_work(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    _commit(root, tmp_path / "stage", "prepared")
    monkeypatch.setattr(
        preparation,
        "assert_no_pending_preparation",
        lambda root: pytest.fail("audit on historical display"),
    )
    monkeypatch.setattr(
        preparation,
        "environment_identity",
        lambda root: pytest.fail("environment on historical display"),
    )
    shown = preparation.preparation_status(root, verify_current=False)
    assert shown["current"] is False and shown["currentness"] == "historical"
    (transaction.preparation_directory(root) / "registry.json").unlink()
    shown = preparation.preparation_status(root, verify_current=False)
    assert shown["current"] is False and shown["currentness"] == "unknown"


def test_direct_discovery_and_reuse_refuse_loss_before_budget_or_plan_work(tmp_path):
    from leanflow_cli.formalization import preparation_discovery, preparation_reuse

    root = tmp_path / "project"
    journal = _commit(root, tmp_path / "stage", "prepared")
    (transaction.preparation_directory(root) / "registry.json").unlink()

    class NoPaidWork:
        def remaining(self):
            pytest.fail("Lost preparation history reached native budgeting")

    with pytest.raises(ValueError, match="registry"):
        preparation_discovery.discover_preparation_plan(
            root,
            {},
            {},
            backend="codex",
            model="unused",
            effort="max",
            environment={},
            budget=NoPaidWork(),
            directory=tmp_path / "campaign",
        )
    with pytest.raises(ValueError, match="registry"):
        preparation_reuse.reuse_preparation(
            root,
            {},
            {},
            journal["plan"]["receipt"],
            budget_path=None,
            cancelled=lambda: False,
        )
    assert not (tmp_path / "campaign").exists()


def test_concurrent_change_during_index_retirement_never_publishes_ack(tmp_path, monkeypatch):
    from leanflow_cli.formalization import preparation_registry_loss as loss

    root = tmp_path / "project"
    _commit(root, tmp_path / "stage", "prepared")
    directory = transaction.preparation_directory(root)
    (directory / "registry.json").unlink()
    observed = _observation(root)
    original = loss.rename_exclusive

    def save_after_retirement(source, target):
        original(source, target)
        (root / "lakefile.toml").write_text("concurrent canonical save")

    monkeypatch.setattr(loss, "rename_exclusive", save_after_retirement)
    with pytest.raises(ValueError, match="changed during"):
        transaction.resolve_preparation_conflict(root, observed, "Acknowledge")
    assert (root / "lakefile.toml").read_text() == "concurrent canonical save"
    assert not (directory / "registry.json").exists()
    assert list((directory / "index-history").glob("receipt-*.json"))
    with pytest.raises(ValueError, match="registry"):
        transaction.assert_no_pending_preparation(root)


def test_recovery_bound_never_uses_truncated_history(tmp_path, monkeypatch):
    from leanflow_cli.formalization import preparation_history

    root = tmp_path / "project"
    _commit(root, tmp_path / "stage", "prepared")
    directory = transaction.preparation_directory(root)
    old = (directory / "registry.json").read_bytes()
    monkeypatch.setattr(preparation_history, "MAX_NAMES", 1)
    with pytest.raises(ValueError, match="bounded automatic recovery"):
        transaction.preparation_conflicts(root)
    assert (directory / "registry.json").read_bytes() == old


def test_self_valid_receipt_without_matching_selected_plan_blocks_startup(tmp_path):
    root = tmp_path / "project"
    _commit(root, tmp_path / "stage", "prepared")
    path = transaction.preparation_directory(root) / "receipt.json"
    receipt = json.loads(path.read_text())
    receipt.pop("preparation_id")
    receipt["source_bundle"] = "surviving unindexed receipt"
    receipt["preparation_id"] = "preparation-" + identity(receipt)
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="registry"):
        transaction.assert_no_pending_preparation(root)
    with pytest.raises(ValueError, match="registry"):
        preparation.assert_preparation_current(root)
