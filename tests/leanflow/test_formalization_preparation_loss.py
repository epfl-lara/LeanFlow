"""Acknowledge missing preparation authority without fabricating journals or dropping surviving history."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli.formalization import preparation_transaction as transaction
from leanflow_cli.formalization.evidence import durable_json, identity
from leanflow_cli.formalization.preparation_journal import load_journal, load_registry


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))


def _commit(root: Path, stage: Path, text: str) -> dict:
    root.mkdir(exist_ok=True)
    stage.mkdir()
    (stage / "lakefile.toml").write_text(text)
    return transaction.commit_preparation(
        root,
        stage,
        managed={"lakefile.toml": transaction.entry_identity(root / "lakefile.toml")},
        protected={},
        receipt={"schema_version": 1, "preparation_id": text},
        validate=lambda: None,
        cancelled=lambda: False,
    )


def _path(root: Path, journal: dict) -> Path:
    return (
        transaction.preparation_directory(root)
        / "transactions"
        / (journal["transaction_id"] + ".json")
    )


def _observation(root: Path, transaction_id: str) -> dict:
    row = next(
        item
        for item in transaction.preparation_conflicts(root)["transactions"]
        if item["transaction_id"] == transaction_id
    )
    assert row["unavailable_journal"] is True and row["planned_versions"] is None
    return {
        key: row[key]
        for key in (
            "transaction_id",
            "observation_id",
            "destinations",
            "retained_slots",
            "history_id",
        )
    }


def test_missing_selected_journal_is_inspectable_acknowledgeable_and_repreparable(tmp_path):
    root = tmp_path / "project"
    (root).mkdir()
    (root / "lakefile.toml").write_text("original")
    first = _commit(root, tmp_path / "stage", "prepared")
    captured = root / first["writes"]["lakefile.toml"]["captured"]
    original = captured.read_bytes()
    journal_path = _path(root, first)
    journal_path.unlink()
    with pytest.raises(transaction.PreparationConflict, match="journal unavailable"):
        transaction.assert_no_pending_preparation(root)
    observed = _observation(root, first["transaction_id"])
    assert observed["retained_slots"][str(journal_path)] is None
    resolved = transaction.resolve_preparation_conflict(
        root, observed, "Acknowledge lost journal; preserve all surviving versions"
    )
    assert resolved["unavailable_journal"] and resolved["state"] == "RESOLVED"
    assert not journal_path.exists() and captured.read_bytes() == original
    assert not (transaction.preparation_directory(root) / "receipt.json").exists()
    with pytest.raises(transaction.PreparationConflict):
        load_journal(root, first["transaction_id"])
    transaction.assert_no_pending_preparation(root)
    transaction.recover_preparation(root)
    next_journal = _commit(root, tmp_path / "next-stage", "fresh checked infrastructure")
    assert load_registry(root)["selected"] == next_journal["transaction_id"]
    transaction.assert_no_pending_preparation(root)


def test_missing_historical_journal_preserves_unrelated_current_receipt(tmp_path):
    root = tmp_path / "project"
    first = _commit(root, tmp_path / "first-stage", "first")
    current = _commit(root, tmp_path / "second-stage", "second")
    receipt = transaction.preparation_directory(root) / "receipt.json"
    checked = receipt.read_bytes()
    _path(root, first).unlink()
    observed = _observation(root, first["transaction_id"])
    transaction.resolve_preparation_conflict(
        root, observed, "Acknowledge unrelated old journal loss"
    )
    assert load_registry(root)["selected"] == current["transaction_id"]
    assert receipt.read_bytes() == checked
    transaction.assert_no_pending_preparation(root)


def test_journal_loss_requires_exact_current_observation_and_keeps_corrupt_bytes(tmp_path):
    root = tmp_path / "project"
    first = _commit(root, tmp_path / "stage", "prepared")
    path = _path(root, first)
    corrupt = b"actual incomplete journal bytes preserved"
    path.write_bytes(corrupt)
    observed = _observation(root, first["transaction_id"])
    (root / "lakefile.toml").write_text("concurrent user save")
    with pytest.raises(ValueError, match="versions changed"):
        transaction.resolve_preparation_conflict(root, observed, "Acknowledge")
    transaction.resolve_preparation_conflict(
        root,
        _observation(root, first["transaction_id"]),
        "Reviewed actual changed bytes and journal loss",
    )
    assert path.read_bytes() == corrupt
    assert (root / "lakefile.toml").read_text() == "concurrent user save"
    transaction.assert_no_pending_preparation(root)


def test_acknowledged_journal_loss_reopens_when_retained_version_changes(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text("original")
    first = _commit(root, tmp_path / "first-stage", "prepared")
    captured = root / first["writes"]["lakefile.toml"]["captured"]
    _path(root, first).unlink()
    transaction.resolve_preparation_conflict(
        root, _observation(root, first["transaction_id"]), "Acknowledged lost historical authority"
    )
    captured.write_text("later historical descriptor edit")
    with pytest.raises(transaction.PreparationConflict, match="journal unavailable"):
        transaction.assert_no_pending_preparation(root)
    transaction.resolve_preparation_conflict(
        root,
        _observation(root, first["transaction_id"]),
        "Reviewed changed surviving historical version",
    )
    assert captured.read_text() == "later historical descriptor edit"
    assert len(load_registry(root)["journal_losses"][first["transaction_id"]]["resolutions"]) == 2
    transaction.assert_no_pending_preparation(root)


@pytest.mark.parametrize("malformed", [None, [], {"destinations": []}])
def test_malformed_loss_acknowledgment_is_a_conservative_conflict(tmp_path, malformed):
    root = tmp_path / "project"
    first = _commit(root, tmp_path / "stage", "prepared")
    _path(root, first).unlink()
    transaction.resolve_preparation_conflict(
        root, _observation(root, first["transaction_id"]), "Reviewed unavailable journal"
    )
    registry = load_registry(root)
    loss = registry["journal_losses"][first["transaction_id"]]
    resolution = loss["resolutions"][0]
    resolution["observation"] = malformed
    resolution["resolution_id"] = "prep-loss-resolution-" + identity(
        {key: value for key, value in resolution.items() if key != "resolution_id"}
    )
    durable_json(transaction.preparation_directory(root) / "registry.json", registry)
    with pytest.raises(transaction.PreparationConflict, match="acknowledgment changed"):
        load_registry(root)
    assert not _path(root, first).exists()
    assert (root / "lakefile.toml").read_text() == "prepared"
