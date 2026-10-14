"""Dispose stopped private incoming assets without dropping changed or retained user history."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from leanflow_cli.formalization import preparation_disposal
from leanflow_cli.formalization import preparation_transaction as transaction
from leanflow_cli.formalization.preparation_journal import load_journal, save_journal


@pytest.fixture
def rolled_back(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root, staged = tmp_path / "project", tmp_path / "stage"
    for path in (root, staged):
        (path / ".lake").mkdir(parents=True)
    (root / "lakefile.toml").write_text('name = "original"\n')
    (staged / "lakefile.toml").write_text('name = "prepared"\n')
    (root / ".lake/cache").write_bytes(b"old cache")
    (staged / ".lake/cache").write_bytes(b"checked new cache")

    def stop(name):
        if name == "prepared":
            raise ValueError("Stop before canonical writes")

    with pytest.raises(ValueError, match="Stop before canonical"):
        transaction.commit_preparation(
            root,
            staged,
            managed={
                name: transaction.entry_identity(root / name) for name in (".lake", "lakefile.toml")
            },
            protected={},
            receipt={},
            validate=lambda: None,
            cancelled=lambda: False,
            checkpoint=stop,
        )
    journal = transaction.load_preparation_transaction(root)
    assert journal["state"] == "ROLLED_BACK"
    return root, staged, journal


def test_unverified_cleanup_retains_every_private_input(rolled_back):
    root, _, journal = rolled_back
    with pytest.raises(transaction.PreparationConflict, match="Verify preparation"):
        transaction.dispose_preparation_incoming(root, cleanup_verified=False)
    assert all((root / row["incoming"]).exists() for row in journal["writes"].values())


def test_exact_owned_incoming_is_disposed_with_durable_idempotent_evidence(rolled_back):
    root, _, journal = rolled_back
    before = transaction.entry_identity(root / ".lake")
    results = transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    assert len(results) == 2 and {row["state"] for row in results} == {"DISPOSED"}
    saved = load_journal(root, journal["transaction_id"])
    assert all(row["incoming_disposal"]["state"] == "DISPOSED" for row in saved["writes"].values())
    assert all(not (root / row["incoming"]).exists() for row in saved["writes"].values())
    assert transaction.entry_identity(root / ".lake") == before
    assert (root / "lakefile.toml").read_text() == 'name = "original"\n'
    assert transaction.dispose_preparation_incoming(root, cleanup_verified=True) == []
    transaction.assert_no_pending_preparation(root)


@pytest.mark.parametrize("change", ["descriptor", "replace", "unrecorded"])
def test_changed_replaced_or_unrecorded_incoming_becomes_inspectable_conflict(rolled_back, change):
    root, _, journal = rolled_back
    row = journal["writes"]["lakefile.toml"]
    incoming = root / row["incoming"]
    if change == "descriptor":
        with incoming.open("a") as descriptor:
            descriptor.write("late descriptor save\n")
            descriptor.flush()
    elif change == "replace":
        original = incoming.read_bytes()
        replacement = incoming.with_name("replacement")
        replacement.write_bytes(original)
        replacement.chmod(incoming.stat().st_mode)
        os.replace(replacement, incoming)
    else:
        del row["incoming_inode"]
        save_journal(root, journal)
    actual = incoming.read_bytes()
    results = transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    assert {item["state"] for item in results} == {"DISPOSED", "PRESERVED"}
    assert incoming.read_bytes() == actual
    assert load_journal(root, journal["transaction_id"])["state"] == "CONFLICTED"
    assert transaction.preparation_conflicts(root)["transactions"][0]["resolvable"] is True


def test_interruption_after_unlink_recovers_disposal_intent_without_touching_other_versions(
    rolled_back, monkeypatch
):
    root, _, journal = rolled_back
    actual_save = preparation_disposal.save_entry_progress
    interrupted = []

    def crash_after_unlink(project, current, relative):
        if (
            any(
                row.get("incoming_disposal", {}).get("state") == "DISPOSED"
                for row in current["writes"].values()
            )
            and not interrupted
        ):
            interrupted.append(True)
            raise SystemExit("Stopped after disposal, before final journal")
        actual_save(project, current, relative)

    monkeypatch.setattr(preparation_disposal, "save_entry_progress", crash_after_unlink)
    with pytest.raises(SystemExit):
        transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    saved = load_journal(root, journal["transaction_id"])
    assert any(
        row.get("incoming_disposal", {}).get("state") == "DELETE_PENDING"
        for row in saved["writes"].values()
    )
    monkeypatch.setattr(preparation_disposal, "save_entry_progress", actual_save)
    transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    transaction.assert_no_pending_preparation(root)


def test_disposal_never_removes_captured_or_rollback_user_versions(rolled_back):
    root, _, journal = rolled_back
    row = journal["writes"]["lakefile.toml"]
    captured = root / row["captured"]
    captured.write_bytes(b"user captured version")
    rollback = captured.with_name("rollback-user")
    rollback.write_bytes(b"retained rollback user version")
    row["rollback_slots"] = [{"path": str(rollback.relative_to(root)), "expected": row["after"]}]
    save_journal(root, journal)
    transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    assert captured.read_bytes() == b"user captured version"
    assert rollback.read_bytes() == b"retained rollback user version"
    with pytest.raises(transaction.PreparationConflict, match="Retained user version"):
        transaction.assert_no_pending_preparation(root)


def test_disposal_authority_cannot_be_changed_to_another_slot(rolled_back):
    root, _, journal = rolled_back
    transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    path = (
        transaction.preparation_directory(root)
        / "transactions"
        / (journal["transaction_id"] + ".json")
    )
    saved = load_journal(root, journal["transaction_id"])
    saved["writes"]["lakefile.toml"]["incoming_disposal"]["path"] = "User.lean"
    save_journal(root, saved)
    with pytest.raises(transaction.PreparationConflict, match="disposal identity"):
        load_journal(root, journal["transaction_id"])


def test_current_cleanup_does_not_authorize_an_earlier_controller_incoming(
    rolled_back, monkeypatch
):
    root, _, journal = rolled_back
    monkeypatch.setattr(
        preparation_disposal,
        "process_states",
        lambda: {os.getpid(): (os.getpid(), "S", "another controller start")},
    )
    results = transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    assert len(results) == 2 and {row["state"] for row in results} == {"PRESERVED"}
    assert all((root / row["incoming"]).exists() for row in journal["writes"].values())
    assert load_journal(root, journal["transaction_id"])["state"] == "CONFLICTED"


def test_late_descriptor_edit_after_disposal_intent_is_preserved(rolled_back, monkeypatch):
    root, _, journal = rolled_back
    incoming = root / journal["writes"]["lakefile.toml"]["incoming"]
    actual_save = preparation_disposal.save_entry_progress
    changed = []

    def save_then_edit(project, current, relative):
        actual_save(project, current, relative)
        disposal = current["writes"]["lakefile.toml"].get("incoming_disposal", {})
        if disposal.get("state") == "DELETE_PENDING" and not changed:
            with incoming.open("a") as descriptor:
                descriptor.write("late descriptor save after disposal intent\n")
                descriptor.flush()
            changed.append(True)

    monkeypatch.setattr(preparation_disposal, "save_entry_progress", save_then_edit)
    transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    assert incoming.read_text().endswith("late descriptor save after disposal intent\n")
    assert load_journal(root, journal["transaction_id"])["state"] == "CONFLICTED"


def test_recreated_disposed_slot_is_audited_even_when_it_has_identical_bytes(rolled_back):
    root, _, journal = rolled_back
    incoming = root / journal["writes"]["lakefile.toml"]["incoming"]
    content = incoming.read_bytes()
    mode = incoming.stat().st_mode
    transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    incoming.write_bytes(content)
    incoming.chmod(mode)
    with pytest.raises(transaction.PreparationConflict, match="Retained user version"):
        transaction.assert_no_pending_preparation(root)
    assert incoming.read_bytes() == content


CRASH_WORKER = r"""
import os, subprocess, sys, threading, time
from pathlib import Path
from agent.runtime.harness.isolation import IsolationPolicy
from leanflow_cli.formalization.preparation_lifecycle import private_stage
from leanflow_cli.formalization.preparation_transaction import commit_preparation, entry_identity
from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy
from leanflow_cli.formalization.preparation_resources import supervise_preparation
root=Path(sys.argv[1])
with private_stage(root) as lease:
    stage=lease.path
    (stage/'.lake').mkdir()
    (stage/'.lake/cache').write_bytes(b'checked private compiler output')
    (stage/'lakefile.toml').write_text('name = "prepared"\n')
    policy=SetupExecutionPolicy(IsolationPolicy(stage),{},{},deadline=time.monotonic()+30,disk_guard=lambda root:None)
    lease.require_cleanup()
    with supervise_preparation(policy):
        child=subprocess.Popen(['sleep','60'],start_new_session=True)
        threading.Event().wait(0.3)
    child.wait(timeout=5)
    lease.verify_cleanup(policy.evidence())
    receipt={'schema_version':1,'precommit_cleanup':lease.precommit_cleanup()}
    def checkpoint(name):
        if name=='copied:.lake':
            os._exit(91)
    commit_preparation(root,stage,managed={name:entry_identity(root/name) for name in ('.lake','lakefile.toml')},protected={},receipt=receipt,validate=lambda:None,cancelled=lambda:False,checkpoint=checkpoint)
"""


def test_real_dead_controller_cleanup_recovers_then_reprepares_without_a_new_conflict(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    (root / ".lake").mkdir(parents=True)
    (root / ".lake/cache").write_bytes(b"original cache")
    (root / "lakefile.toml").write_text('name = "original"\n')
    crashed = subprocess.run(
        [sys.executable, "-c", CRASH_WORKER, str(root)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert crashed.returncode == 91, crashed.stderr
    old = transaction.recover_preparation(root)
    assert old["state"] == "ROLLED_BACK"
    incoming = root / old["writes"][".lake"]["incoming"]
    assert incoming.exists()
    stage = tmp_path / "second-stage"
    stage.mkdir()
    (stage / "lakefile.toml").write_text('name = "second"\n')
    current = transaction.commit_preparation(
        root,
        stage,
        managed={"lakefile.toml": transaction.entry_identity(root / "lakefile.toml")},
        protected={},
        receipt={},
        validate=lambda: None,
        cancelled=lambda: False,
    )
    results = transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    assert results == [{"path": old["writes"][".lake"]["incoming"], "state": "DISPOSED"}]
    assert not incoming.exists()
    saved = load_journal(root, old["transaction_id"])
    assert saved["incoming_cleanup_basis"]["kind"] == "closed_dead_controller"
    assert (
        transaction.load_preparation_transaction(root)["transaction_id"]
        == current["transaction_id"]
    )
    transaction.assert_no_pending_preparation(root)
    transaction.recover_preparation(root)
    assert transaction.dispose_preparation_incoming(root, cleanup_verified=True) == []


def test_default_usage_flag_is_not_precommit_closure_for_dead_controller(rolled_back, monkeypatch):
    root, _, journal = rolled_back
    journal["plan"]["receipt"]["resources"] = {"cleanup_verified": True}
    journal["plan_id"] = preparation_disposal.identity(journal["plan"])
    save_journal(root, journal)
    monkeypatch.setattr(
        preparation_disposal,
        "process_states",
        lambda: {os.getpid(): (os.getpid(), "S", "another controller start")},
    )
    results = transaction.dispose_preparation_incoming(root, cleanup_verified=True)
    assert {row["state"] for row in results} == {"PRESERVED"}
    assert all((root / row["incoming"]).exists() for row in journal["writes"].values())
