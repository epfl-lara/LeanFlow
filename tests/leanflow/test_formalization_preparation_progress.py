"""Keep large preparation writes linear while preserving exact progress across process crashes."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization import preparation_progress as progress
from leanflow_cli.formalization import preparation_transaction as transaction
from leanflow_cli.formalization.evidence import digest, identity
from leanflow_cli.formalization.preparation_journal import load_journal, load_registry


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))


def _journal():
    row = {
        "before": "a" * 64,
        "after": "b" * 64,
        "incoming": ".leanflow/preparation-transactions/transaction/0.incoming",
        "captured": ".leanflow/preparation-transactions/transaction/0.before",
        "kind": "infrastructure",
        "stage": "ready",
        "rollback_slots": [],
    }
    return {
        "schema_version": 1,
        "transaction_id": "transaction",
        "plan_id": "c" * 64,
        "state": "PUBLISHING",
        "writes": {".lake/value": row},
    }


def test_progress_preserves_complete_mutable_row_and_snapshot_rotation(tmp_path):
    journal = _journal()
    progress.save_snapshot(tmp_path, journal)
    original = copy.deepcopy(journal)
    row = journal["writes"][".lake/value"]
    row.update(stage="installed", incoming_inode=[1, 23])
    progress.save_entry_progress(tmp_path, journal, ".lake/value")
    progress.apply_entry_progress(tmp_path, original)
    assert original["writes"] == journal["writes"]
    old_generation = journal["progress_generation"]
    journal["state"] = "COMMITTED"
    progress.save_snapshot(tmp_path, journal)
    assert journal["progress_generation"] != old_generation
    loaded = json.loads((tmp_path / "transactions/transaction.json").read_text())
    progress.apply_entry_progress(tmp_path, loaded)
    assert loaded == journal
    assert progress.progress_assets(tmp_path, "transaction")


def test_failed_snapshot_rotation_keeps_prior_snapshot_and_entry_progress(tmp_path, monkeypatch):
    journal = _journal()
    progress.save_snapshot(tmp_path, journal)
    row = journal["writes"][".lake/value"]
    row.update(stage="install_pending", incoming_inode=[1, 23])
    progress.save_entry_progress(tmp_path, journal, ".lake/value")
    generation = journal["progress_generation"]
    journal["state"] = "COMMITTED"

    def fail(*args):
        raise OSError("snapshot replacement failed")

    monkeypatch.setattr(progress, "durable_json", fail)
    with pytest.raises(OSError, match="replacement failed"):
        progress.save_snapshot(tmp_path, journal)
    assert journal["progress_generation"] == generation
    loaded = json.loads((tmp_path / "transactions/transaction.json").read_text())
    progress.apply_entry_progress(tmp_path, loaded)
    assert loaded["state"] == "PUBLISHING"
    assert loaded["writes"][".lake/value"] == row


@pytest.mark.parametrize("changed", ["transaction_id", "plan_id", "state", "relative", "entry"])
def test_progress_cannot_change_snapshot_binding_or_immutable_ownership(tmp_path, changed):
    journal = _journal()
    progress.save_snapshot(tmp_path, journal)
    progress.save_entry_progress(tmp_path, journal, ".lake/value")
    path = next((tmp_path / "progress").rglob("*.json"))
    value = json.loads(path.read_text())
    if changed == "entry":
        value["entry"]["before"] = "d" * 64
    else:
        value[changed] = "different"
    value["progress_id"] = identity(
        {key: item for key, item in value.items() if key != "progress_id"}
    )
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="identity changed"):
        progress.apply_entry_progress(tmp_path, journal)


def _files(root: Path, staged: Path, count: int):
    root.mkdir()
    staged.mkdir()
    source = root / "User.lean"
    source.write_text("theorem user_source : True := by sorry\n")
    managed = {}
    for index in range(count):
        relative = f".lake/build/lib/lean/Module{index:06}.olean"
        for parent, data in ((root, b"old build"), (staged, b"checked build")):
            path = parent / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        managed[relative] = transaction.entry_identity(root / relative)
    return managed


def test_thousands_of_changed_files_write_linear_durable_journal_bytes(
    tmp_path, monkeypatch, record_property
):
    durable = progress.durable_json
    measurements = []
    counters = {"bytes": 0, "fsync": 0}

    def measured(path, value):
        counters["bytes"] += len(
            (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        )
        durable(path, value)

    def sampled_fsync(descriptor):
        counters["fsync"] += 1

    monkeypatch.setattr(progress, "durable_json", measured)
    # This measures the durability calls and actual serialized bytes without
    # turning a complexity regression into a hardware-dependent fsync benchmark.
    # Separate process-crash tests below use the real durable writer.
    monkeypatch.setattr(os, "fsync", sampled_fsync)
    for count in (1000, 2000):
        counters.update(bytes=0, fsync=0)
        root, staged = tmp_path / f"root-{count}", tmp_path / f"stage-{count}"
        managed = _files(root, staged, count)
        receipt = {
            "schema_version": 1,
            "preparation_id": "checked-receipt",
            "components": {name: "c" * 64 for name in managed},
        }
        journal = transaction.commit_preparation(
            root,
            staged,
            managed=managed,
            protected=transaction.protected_inputs(root),
            receipt=receipt,
            validate=lambda: None,
            cancelled=lambda: False,
        )
        assert journal["state"] == "COMMITTED"
        loaded = load_journal(root, journal["transaction_id"])
        assert loaded["writes"] == journal["writes"]
        assert (root / "User.lean").read_text() == "theorem user_source : True := by sorry\n"
        measurements.append(dict(counters))
        record_property(f"journal_bytes_{count}", counters["bytes"])
        record_property(f"durability_calls_{count}", counters["fsync"])
    assert measurements[1]["bytes"] < measurements[0]["bytes"] * 2.5, measurements
    assert measurements[1]["fsync"] < measurements[0]["fsync"] * 2.5, measurements


def test_legacy_snapshot_has_no_inferred_progress_and_can_start_incremental_writes(tmp_path):
    journal = _journal()
    unchanged = copy.deepcopy(journal)
    progress.apply_entry_progress(tmp_path, journal)
    assert journal == unchanged
    journal["writes"][".lake/value"].update(stage="installed", incoming_inode=[1, 23])
    progress.save_entry_progress(tmp_path, journal, ".lake/value")
    assert (tmp_path / "transactions/transaction.json").is_file()
    loaded = json.loads((tmp_path / "transactions/transaction.json").read_text())
    progress.apply_entry_progress(tmp_path, loaded)
    assert loaded["writes"] == journal["writes"]


def test_rollback_of_large_changed_sets_also_writes_linear_progress(
    tmp_path, monkeypatch, record_property
):
    durable = progress.durable_json
    measurements = []
    counters = {"bytes": 0}

    def measured(path, value):
        counters["bytes"] += len(
            (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        )
        durable(path, value)

    def reject():
        raise ValueError("independent readiness rejected")

    monkeypatch.setattr(progress, "durable_json", measured)
    monkeypatch.setattr(os, "fsync", lambda descriptor: None)
    for count in (512, 1024):
        counters["bytes"] = 0
        root, staged = tmp_path / f"root-{count}", tmp_path / f"stage-{count}"
        managed = _files(root, staged, count)
        with pytest.raises(ValueError, match="readiness rejected"):
            transaction.commit_preparation(
                root,
                staged,
                managed=managed,
                protected=transaction.protected_inputs(root),
                receipt={
                    "schema_version": 1,
                    "preparation_id": "receipt",
                    "components": {name: "c" * 64 for name in managed},
                },
                validate=reject,
                cancelled=lambda: False,
            )
        journal = transaction.load_preparation_transaction(root)
        assert journal["state"] == "ROLLED_BACK"
        assert all(
            transaction.entry_identity(root / name) == before for name, before in managed.items()
        )
        assert load_registry(root)["selected"] is None
        measurements.append(counters["bytes"])
        record_property(f"forward_and_rollback_bytes_{count}", counters["bytes"])
    assert measurements[1] < measurements[0] * 2.5, measurements


def test_surviving_progress_never_reconstructs_missing_raw_snapshot(tmp_path):
    journal = _journal()
    progress.save_snapshot(tmp_path, journal)
    progress.save_entry_progress(tmp_path, journal, ".lake/value")
    assets = progress.progress_assets(tmp_path, "transaction")
    raw = tmp_path / "transactions/transaction.json"
    raw.unlink()
    assert progress.progress_assets(tmp_path, "transaction") == assets
    assert not raw.exists()


def test_atomic_progress_preserves_open_descriptor_user_save_during_rollback(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    managed = _files(root, staged, 3)
    original = root / ".lake/build/lib/lean/Module000001.olean"
    with original.open("r+b") as descriptor:

        def edit(boundary):
            if boundary == "installed:.lake/build/lib/lean/Module000001.olean":
                descriptor.write(b"concurrent descriptor save")
                descriptor.truncate()
                descriptor.flush()

        with pytest.raises(transaction.PreparationConflict):
            transaction.commit_preparation(
                root,
                staged,
                managed=managed,
                protected=transaction.protected_inputs(root),
                receipt={"schema_version": 1, "preparation_id": "receipt"},
                validate=lambda: None,
                cancelled=lambda: False,
                checkpoint=edit,
            )
        journal = transaction.load_preparation_transaction(root)
        assert journal["state"] == "CONFLICTED"
        locations = [
            original,
            root / journal["writes"][original.relative_to(root).as_posix()]["captured"],
        ]
        assert any(
            path.exists() and path.read_bytes() == b"concurrent descriptor save"
            for path in locations
        )
        assert (root / "User.lean").read_text() == "theorem user_source : True := by sorry\n"


WORKER = r"""
import os, sys
from pathlib import Path
from leanflow_cli.formalization import preparation_transaction as tx
root, staged = Path(sys.argv[1]), Path(sys.argv[2])
boundary, target = sys.argv[3], sys.argv[4]
replace = os.replace
def interrupted_replace(source, destination):
    if '/progress/' in str(destination):
        if boundary == 'before_progress_replace':
            os._exit(92)
        replace(source, destination)
        if boundary == 'after_progress_replace':
            os._exit(92)
    else:
        replace(source, destination)
os.replace = interrupted_replace
def checkpoint(name):
    if name == target:
        os._exit(92)
managed = {path.relative_to(root).as_posix():tx.entry_identity(path) for path in (root/'.lake').rglob('*.olean')}
tx.commit_preparation(root,staged,managed=managed,protected=tx.protected_inputs(root),receipt={'schema_version':1,'preparation_id':'receipt'},validate=lambda:None,cancelled=lambda:False,checkpoint=checkpoint)
"""


@pytest.mark.parametrize(
    ("boundary", "target"),
    [
        ("before_progress_replace", ""),
        ("after_progress_replace", ""),
        ("", "captured:.lake/build/lib/lean/Module000001.olean"),
        ("", "moved:.lake/build/lib/lean/Module000001.olean"),
        ("", "installed:.lake/build/lib/lean/Module000001.olean"),
        ("", "journal:COMMITTED"),
    ],
)
def test_real_process_exit_preserves_progress_and_exact_rollback(tmp_path, boundary, target):
    root, staged = tmp_path / "root", tmp_path / "stage"
    original = _files(root, staged, 3)
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(root), str(staged), boundary, target],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 92, result.stderr
    registry = load_registry(root)
    journal = load_journal(root, registry["current"])
    first = journal["writes"][".lake/build/lib/lean/Module000000.olean"]
    if boundary == "before_progress_replace":
        assert "incoming_inode" not in first
    elif boundary == "after_progress_replace":
        assert len(first["incoming_inode"]) == 2
    transaction.recover_preparation(root)
    for name, checksum in original.items():
        actual = transaction.entry_identity(root / name)
        assert actual != checksum if target == "journal:COMMITTED" else actual == checksum
    assert (root / "User.lean").read_text() == "theorem user_source : True := by sorry\n"
    transaction.assert_no_pending_preparation(root)


def test_lost_interrupted_progress_preserves_versions_without_inventing_readiness(tmp_path):
    root, staged = tmp_path / "root", tmp_path / "stage"
    original = _files(root, staged, 3)
    relative = ".lake/build/lib/lean/Module000001.olean"
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(root), str(staged), "", "moved:" + relative],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 92, result.stderr
    registry = load_registry(root)
    directory = transaction.preparation_directory(root)
    raw_path = directory / "transactions" / (registry["current"] + ".json")
    baseline = json.loads(raw_path.read_text())
    entry_path = (
        directory
        / "progress"
        / registry["current"]
        / baseline["progress_generation"]
        / (digest(relative) + ".json")
    )
    entry_path.unlink()
    loaded = load_journal(root, registry["current"])
    assert loaded["writes"][relative] == baseline["writes"][relative]
    assert loaded["writes"][relative]["stage"] == "ready"
    assert registry["selected"] is None
    with pytest.raises(transaction.PreparationConflict):
        transaction.recover_preparation(root)
    current = root / relative
    captured = root / baseline["writes"][relative]["captured"]
    assert current.read_bytes() == b"checked build"
    assert transaction.entry_identity(captured) == original[relative]
    assert load_registry(root)["selected"] is None
    assert not (directory / "receipt.json").exists()
