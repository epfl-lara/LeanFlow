"""Preserve every compiler version through adjacent-slot publication and interruption."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent.runtime.harness.isolation import IsolationPolicy
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.workflows.project_setup import (
    toolchain_journal_loss,
    toolchain_publication,
    toolchain_recovery,
)
from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy
from leanflow_cli.workflows.project_setup.process import SetupCancelled, SetupFailure
from leanflow_cli.workflows.project_setup.toolchain_recovery import (
    acknowledge_compiler_publication,
    compiler_recovery_observation,
    recover_compiler_publications,
)


@pytest.fixture
def publication(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    stage = tmp_path / "stage"
    downloaded = stage / "private-elan/toolchains/pin"
    (downloaded / "bin").mkdir(parents=True)
    (downloaded / "bin/lean").write_text("checked compiler bytes")
    target = tmp_path / "native-elan/toolchains/pin"
    policy = SetupExecutionPolicy(
        IsolationPolicy(stage),
        {},
        {},
        deadline=time.monotonic() + 30,
        disk_guard=lambda root: None,
    )
    return downloaded, target, policy


def _publish(publication, **kwargs):
    downloaded, target, policy = publication
    return toolchain_publication.publish_compiler(
        downloaded,
        target,
        toolchain="leanprover/lean4:v4.99.0",
        policy=policy,
        cancel=kwargs.pop("cancel", None),
        **kwargs,
    )


def _journal(publication):
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    paths = list(directory.glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


def test_cross_device_branch_copies_distinct_checked_inodes_and_keeps_private_input(
    publication, monkeypatch
):
    downloaded, target, _ = publication
    expected = entry_identity(downloaded)
    inode = (downloaded / "bin/lean").stat().st_ino
    monkeypatch.setattr(toolchain_publication, "_same_device", lambda source, parent: False)
    result = _publish(publication)
    assert entry_identity(downloaded) == entry_identity(target) == result["sha256"] == expected
    assert (target / "bin/lean").stat().st_ino != inode
    assert _journal(publication)["state"] == "INSTALLED"


def test_no_clobber_preserves_concurrent_native_target_and_checked_adjacent_version(publication):
    downloaded, target, _ = publication
    expected = entry_identity(downloaded)

    def editor(boundary):
        if boundary == "compiler:PREPARED":
            target.mkdir()
            (target / "user").write_text("another installer result")

    with pytest.raises(SetupFailure) as error:
        _publish(publication, checkpoint=editor)
    assert error.value.code == "setup_toolchain_destination_changed"
    journal = _journal(publication)
    assert journal["state"] == "PREPARED"
    assert (target / "user").read_text() == "another installer result"
    assert entry_identity(Path(journal["incoming"])) == expected


@pytest.mark.parametrize("boundary", ["REGISTERED", "PREPARED", "INSTALLED"])
def test_crash_boundaries_keep_complete_owned_compiler_versions(publication, boundary):
    downloaded, target, _ = publication
    expected = entry_identity(downloaded)

    def crash(name):
        if name == "compiler:" + boundary:
            raise SystemExit("process exit")

    with pytest.raises(SystemExit):
        _publish(publication, checkpoint=crash)
    journal = _journal(publication)
    assert journal["state"] == boundary
    assert journal["after"] == expected and journal["before"] is None
    retained = (
        downloaded
        if boundary == "REGISTERED"
        else Path(journal["incoming"]) if boundary == "PREPARED" else target
    )
    assert entry_identity(retained) == expected
    assert not target.exists() or boundary == "INSTALLED"


def test_cancel_after_exclusive_install_keeps_target_and_cannot_report_readiness(publication):
    downloaded, target, _ = publication
    expected = entry_identity(downloaded)
    cancel = threading.Event()

    def stop(boundary):
        if boundary == "compiler:INSTALLED":
            cancel.set()

    with pytest.raises(SetupCancelled):
        _publish(publication, cancel=cancel, checkpoint=stop)
    assert entry_identity(target) == expected
    assert _journal(publication)["state"] == "INSTALLED"


@pytest.mark.parametrize("boundary", ["REGISTERED", "SLOT_READY", "PREPARED", "INSTALLED"])
def test_restart_recovers_owned_orphan_slot_and_keeps_installed_native_target(
    publication, boundary
):
    downloaded, target, _ = publication
    expected = entry_identity(downloaded)

    def crash(name):
        if name == "compiler:" + boundary:
            raise SystemExit("controller death")

    with pytest.raises(SystemExit):
        _publish(publication, checkpoint=crash)
    before = _journal(publication)
    recover_compiler_publications(target, deadline=time.monotonic() + 10)
    after = _journal(publication)
    assert after["after"] == expected
    assert after["state"] == ("INSTALLED" if boundary == "INSTALLED" else "DISPOSED")
    assert not Path(before["slot"]).exists()
    assert target.exists() is (boundary == "INSTALLED")
    assert not target.exists() or entry_identity(target) == expected


def test_recovery_preserves_replaced_or_edited_complete_incoming_version(publication):
    def crash(name):
        if name == "compiler:PREPARED":
            raise SystemExit("controller death")

    with pytest.raises(SystemExit):
        _publish(publication, checkpoint=crash)
    before = _journal(publication)
    file = Path(before["incoming"]) / "bin/lean"
    file.write_text("independent later version")
    with pytest.raises(SetupFailure) as error:
        recover_compiler_publications(publication[1], deadline=time.monotonic() + 10)
    assert error.value.code == "setup_toolchain_recovery_required"
    assert file.read_text() == "independent later version"


@pytest.mark.parametrize("attempt", ["ABC", "x/../y", None])
def test_malformed_compiler_attempt_never_disposes_self_bound_foreign_slot(publication, attempt):
    """Reject path-bearing foreign journals even when their inode and content hashes match."""
    downloaded, target, _ = publication
    slot = target.parent / (".leanflow-install-" + (attempt or ""))
    incoming = slot / "compiler"
    incoming.mkdir(parents=True)
    (incoming / "user").write_bytes(b"independent directory outside valid attempt slots")
    info = slot.stat()
    journal = {
        "schema_version": 1,
        "target": str(target),
        "slot": str(slot),
        "incoming": str(incoming),
        "slot_inode": [info.st_dev, info.st_ino],
        "cleanup_verified": True,
        "state": "PREPARED",
        "before": None,
        "after": entry_identity(incoming),
    }
    if attempt is not None:
        journal["attempt_id"] = attempt
    journal["record_id"] = identity(journal)
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    directory.mkdir(parents=True)
    path = directory / "foreign.json"
    raw = json.dumps(journal).encode()
    path.write_bytes(raw)
    before = entry_identity(slot)
    failure = None
    try:
        recover_compiler_publications(target, deadline=time.monotonic() + 10)
    except SetupFailure as error:
        failure = error
    assert entry_identity(slot) == before, "Malformed attempt disposed an independent directory"
    assert path.read_bytes() == raw, "Malformed attempt rewrote the foreign journal"
    assert failure is not None and failure.code == "setup_toolchain_recovery_required"
    observed = compiler_recovery_observation(path, target)
    assert observed["kind"] == "unreadable_compiler_journal"
    assert observed["versions"] == {"target": None}
    accepted = acknowledge_compiler_publication(
        path, target, observation_id=observed["observation_id"], processes_stopped=True
    )
    assert accepted["versions_removed"] is False
    assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []
    assert entry_identity(slot) == before and path.read_bytes() == raw
    assert downloaded.exists() and not target.exists()


def test_compiler_disposal_capture_preserves_actual_second_process_slot_replacement(publication):
    def crash(name):
        if name == "compiler:PREPARED":
            raise SystemExit("controller death")

    with pytest.raises(SystemExit):
        _publish(publication, checkpoint=crash)
    journal = _journal(publication)
    slot = Path(journal["slot"])
    held = slot.with_name(slot.name + ".owned-held")
    original = entry_identity(slot)

    def replace(name):
        if name == "compiler-disposal:CAPTURE":
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.rename(Path(sys.argv[2])); (p/'compiler').mkdir(parents=True); (p/'compiler/user').write_bytes(b'independent compiler version')",
                    str(slot),
                    str(held),
                ],
                check=True,
            )

    with pytest.raises(SetupFailure, match="Unknown captured compiler"):
        recover_compiler_publications(
            publication[1], deadline=time.monotonic() + 10, checkpoint=replace
        )
    assert entry_identity(held) == original
    assert (slot / "compiler/user").read_bytes() == b"independent compiler version"
    assert not publication[1].exists()
    after = _journal(publication)
    assert after["state"] == "PREPARED"
    assert after["slot_disposal"]["state"] == "PRESERVED"
    assert not Path(after["slot_disposal"]["capture"]).exists()
    target = publication[1]
    path = next((toolchain_publication.leanflow_home() / "toolchain-installations").glob("*.json"))
    observed = compiler_recovery_observation(path, target)
    acknowledge_compiler_publication(
        path, target, observation_id=observed["observation_id"], processes_stopped=True
    )
    assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []
    (slot / "compiler/user").write_bytes(b"changed restored independent version")
    with pytest.raises(SetupFailure, match="disposal versions changed"):
        recover_compiler_publications(target, deadline=time.monotonic() + 10)
    assert entry_identity(held) == original


@pytest.mark.parametrize("edited_attempt", [False, True])
def test_compiler_unknown_capture_and_blocked_restore_are_observation_bound(
    publication, edited_attempt
):
    def crash(name):
        if name == "compiler:PREPARED":
            raise SystemExit("controller death")

    with pytest.raises(SystemExit):
        _publish(publication, checkpoint=crash)
    slot = Path(_journal(publication)["slot"])
    original = entry_identity(slot)
    held = slot.with_name(slot.name + ".owned-held")

    def replace(name):
        if name == "compiler-disposal:CAPTURED":
            capture = Path(_journal(publication)["slot_disposal"]["capture"])
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; c,s,h=map(Path,sys.argv[1:]); c.rename(h); (c/'compiler').mkdir(parents=True); (c/'compiler/user').write_bytes(b'unknown captured version'); s.mkdir(); (s/'blocker').write_bytes(b'independent original slot')",
                    str(capture),
                    str(slot),
                    str(held),
                ],
                check=True,
            )

    target = publication[1]
    with pytest.raises(SetupFailure, match="Unknown captured compiler"):
        recover_compiler_publications(target, deadline=time.monotonic() + 10, checkpoint=replace)
    journal = _journal(publication)
    capture = Path(journal["slot_disposal"]["capture"])
    assert entry_identity(held) == original
    assert (capture / "compiler/user").read_bytes() == b"unknown captured version"
    assert (slot / "blocker").read_bytes() == b"independent original slot"
    path = next((toolchain_publication.leanflow_home() / "toolchain-installations").glob("*.json"))
    if edited_attempt:
        journal["attempt_id"] = "unusable attempt binding"
        journal["record_id"] = identity(
            {key: value for key, value in journal.items() if key != "record_id"}
        )
        path.write_text(json.dumps(journal))
    original_journal = path.read_bytes()
    command = [
        sys.executable,
        "-m",
        "leanflow_cli.workflows.project_setup.toolchain_recovery",
        "inspect",
        "--journal",
        str(path),
        "--target",
        str(target),
    ]
    inspected = subprocess.run(command, check=True, capture_output=True, text=True)
    observed = json.loads(inspected.stdout)
    if edited_attempt:
        assert observed["kind"] == "unreadable_compiler_journal"
    assert observed["retained_disposal_slots"][str(capture)]["sha256"] == entry_identity(capture)
    file = capture / "compiler/user"
    file.write_bytes(b"edited independently again")
    with pytest.raises(SetupFailure, match="observation changed"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=True
        )
    current = compiler_recovery_observation(path, target)
    acknowledge_compiler_publication(
        path, target, observation_id=current["observation_id"], processes_stopped=True
    )
    if edited_attempt:
        assert path.read_bytes() == original_journal
    assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []
    file.write_bytes(b"later capture change")
    with pytest.raises(
        SetupFailure, match="unreadable" if edited_attempt else "disposal versions changed"
    ):
        recover_compiler_publications(target, deadline=time.monotonic() + 10)
    assert file.read_bytes() == b"later capture change" and not target.exists()


@pytest.mark.parametrize(
    "boundary",
    ["CAPTURE_PENDING", "CAPTURED", "DELETE_PENDING", "DELETED", "DISPOSED", "PARTIAL_DELETE"],
)
def test_real_compiler_disposal_process_exit_retains_registered_versions(publication, boundary):
    downloaded, target, _ = publication
    expected = entry_identity(downloaded)
    script = """
import os, sys, time
from pathlib import Path
from agent.runtime.harness.isolation import IsolationPolicy
from leanflow_cli.workflows.project_setup import toolchain_disposal, toolchain_publication, toolchain_recovery
from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy
from leanflow_cli.workflows.project_setup.toolchain_install import _installation_owner
downloaded, target = map(Path, sys.argv[1:3])
boundary = sys.argv[3]
policy = SetupExecutionPolicy(IsolationPolicy(downloaded.parents[2]), {}, {}, deadline=time.monotonic()+30, disk_guard=lambda _: None)
def published(name):
    if name == 'compiler:PREPARED': raise SystemExit()
try:
    toolchain_publication.publish_compiler(downloaded, target, toolchain='leanprover/lean4:v4.99.0', policy=policy, cancel=None, checkpoint=published)
except SystemExit: pass
if boundary == 'PARTIAL_DELETE':
    def interrupted(path):
        (path/'compiler/bin/lean').unlink()
        os._exit(91)
    toolchain_disposal.shutil.rmtree = interrupted
def disposing(name):
    if name == 'compiler-disposal:' + boundary: os._exit(91)
with _installation_owner(time.monotonic()+10, None):
    toolchain_recovery.recover_compiler_publications(target, deadline=time.monotonic()+10, checkpoint=disposing)
raise AssertionError('exit checkpoint was not reached')
"""
    result = subprocess.run([sys.executable, "-c", script, str(downloaded), str(target), boundary])
    assert result.returncode == 91
    before = _journal(publication)
    slot = Path(before["slot"])
    capture = Path(before["slot_disposal"]["capture"])
    assert before["after"] == expected and not target.exists()
    if boundary == "PARTIAL_DELETE":
        assert capture.exists() and not (capture / "compiler/bin/lean").exists()
        with pytest.raises(SetupFailure, match="Unknown captured compiler"):
            recover_compiler_publications(target, deadline=time.monotonic() + 10)
        assert slot.exists() and not (slot / "compiler/bin/lean").exists()
    else:
        recover_compiler_publications(target, deadline=time.monotonic() + 10)
        after = _journal(publication)
        assert after["state"] == after["slot_disposal"]["state"] == "DISPOSED"
        assert not slot.exists() and not capture.exists()
        assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []


def test_raw_compiler_loss_observation_binds_adjacent_orphan_capture(publication):
    downloaded, target, _ = publication
    target.parent.mkdir(parents=True)
    capture = target.parent / (".leanflow-install-" + "a" * 32 + ".disposal-" + "b" * 32)
    capture.mkdir()
    (capture / "user").write_bytes(b"retained compiler capture")
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    directory.mkdir(parents=True)
    path = directory / "lost.json"
    raw = b'{"unreadable'
    path.write_bytes(raw)
    observed = compiler_recovery_observation(path, target)
    assert observed["retained_disposal_slots"][str(capture)]["sha256"] == entry_identity(capture)
    (capture / "user").write_bytes(b"changed retained compiler capture")
    with pytest.raises(SetupFailure, match="observation changed"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=True
        )
    current = compiler_recovery_observation(path, target)
    acknowledge_compiler_publication(
        path, target, observation_id=current["observation_id"], processes_stopped=True
    )
    assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []
    (capture / "user").write_bytes(b"capture changed after acknowledgment")
    with pytest.raises(SetupFailure, match="unreadable"):
        recover_compiler_publications(target, deadline=time.monotonic() + 10)
    assert path.read_bytes() == raw and downloaded.exists() and capture.exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_readable_compiler_acknowledgment_ignores_other_attempt_captures(publication, legacy):
    downloaded, target, _ = publication
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    if legacy:
        directory.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        journal = {
            "schema_version": 1,
            "target": str(target),
            "state": "PREPARED",
            "after": entry_identity(downloaded),
        }
        journal["record_id"] = identity(journal)
        path = directory / "legacy.json"
        path.write_text(json.dumps(journal))
    else:

        def crash(name):
            if name == "compiler:PREPARED":
                raise SystemExit("controller death")

        with pytest.raises(SystemExit):
            _publish(publication, checkpoint=crash)
        path = next(directory.glob("*.json"))
    observed = compiler_recovery_observation(path, target)
    other = target.parent / (".leanflow-install-" + "a" * 32 + ".disposal-" + "b" * 32)
    other.mkdir()
    (other / "user").write_bytes(b"other attempt's retained version")
    assert compiler_recovery_observation(path, target) == observed
    accepted = acknowledge_compiler_publication(
        path, target, observation_id=observed["observation_id"], processes_stopped=True
    )
    # Older acknowledgments did not include this new observation field.
    accepted["disposition"]["observation"].pop("retained_disposal_slots")
    accepted["record_id"] = identity(
        {key: value for key, value in accepted.items() if key != "record_id"}
    )
    path.write_text(json.dumps(accepted))
    (other / "user").write_bytes(b"other attempt changed independently")
    assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []
    assert (other / "user").read_bytes() == b"other attempt changed independently"


@pytest.mark.parametrize("boundary", ["SLOT_CREATED", "legacy"])
def test_unbound_compiler_slot_has_observation_bound_preserving_recovery(publication, boundary):
    downloaded, target, _ = publication
    expected = entry_identity(downloaded)
    if boundary == "SLOT_CREATED":

        def crash(name):
            if name == "compiler:SLOT_CREATED":
                raise SystemExit("death before inode journal")

        with pytest.raises(SystemExit):
            _publish(publication, checkpoint=crash)
    else:
        directory = toolchain_publication.leanflow_home() / "toolchain-installations"
        directory.mkdir(parents=True)
        journal = {
            "schema_version": 1,
            "target": str(target),
            "state": "PREPARED",
            "after": expected,
            "private": str(downloaded),
        }
        journal["record_id"] = identity(journal)
        (directory / "legacy.json").write_text(json.dumps(journal))
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    path = next(directory.glob("*.json"))
    before = _journal(publication)
    with pytest.raises(SetupFailure) as error:
        recover_compiler_publications(target, deadline=time.monotonic() + 10)
    assert error.value.code == "setup_toolchain_recovery_required"
    observed = compiler_recovery_observation(path, target)
    with pytest.raises(SetupFailure, match="stopped acquisition"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=False
        )
    accepted = acknowledge_compiler_publication(
        path, target, observation_id=observed["observation_id"], processes_stopped=True
    )
    assert accepted["state"] == "PRESERVED" and not accepted["disposition"]["incoming_removed"]
    assert entry_identity(downloaded) == expected and not target.exists()
    if boundary == "SLOT_CREATED":
        assert Path(before["slot"]).is_dir()
    assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []


def test_compiler_acknowledgment_rejects_changed_observation_and_live_controller(
    publication, monkeypatch
):
    def crash(name):
        if name == "compiler:PREPARED":
            raise SystemExit("death")

    with pytest.raises(SystemExit):
        _publish(publication, checkpoint=crash)
    target = publication[1]
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    path = next(directory.glob("*.json"))
    observed = compiler_recovery_observation(path, target)
    journal = _journal(publication)
    file = Path(journal["incoming"]) / "bin/lean"
    file.write_text("preserved independent compiler bytes")
    with pytest.raises(SetupFailure, match="observation changed"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=True
        )
    assert file.read_text() == "preserved independent compiler bytes"
    journal["owner"] = {"pid": 987654, "start": "original live process"}
    journal["record_id"] = identity(
        {key: value for key, value in journal.items() if key != "record_id"}
    )
    path.write_text(json.dumps(journal))
    monkeypatch.setattr(
        toolchain_recovery, "process_states", lambda: {987654: (0, 0, "original live process")}
    )
    observed = compiler_recovery_observation(path, target)
    with pytest.raises(SetupFailure, match="stopped acquisition"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=True
        )
    assert _journal(publication)["state"] == "PREPARED" and file.exists()


@pytest.mark.parametrize("raw", [b'{"target": "truncated', b"[]", b"\xff", b"{}"])
def test_unreadable_compiler_journal_has_exact_raw_loss_acknowledgment(publication, raw):
    downloaded, target, _ = publication
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    directory.mkdir(parents=True)
    path = directory / "broken.json"
    path.write_bytes(raw)
    with pytest.raises(SetupFailure) as failure:
        recover_compiler_publications(target, deadline=time.monotonic() + 10)
    assert str(path) in str(failure.value) and str(target) in str(failure.value)
    observed = compiler_recovery_observation(path, target)
    assert observed["kind"] == "unreadable_compiler_journal"
    assert observed["original_owner_live"] is None and observed["owner_status"] == "unavailable"
    with pytest.raises(SetupFailure, match="stopped acquisition"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=False
        )
    changed = raw + b" "
    path.write_bytes(changed)
    with pytest.raises(SetupFailure, match="observation changed"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=True
        )
    path.write_bytes(raw)
    target.mkdir(parents=True)
    (target / "user").write_text("Independent native target")
    with pytest.raises(SetupFailure, match="observation changed"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=True
        )
    current = compiler_recovery_observation(path, target)
    before = entry_identity(target)
    accepted = acknowledge_compiler_publication(
        path, target, observation_id=current["observation_id"], processes_stopped=True
    )
    assert accepted["kind"] == "unreadable_compiler_journal" and accepted["state"] == "PRESERVED"
    assert accepted["versions_removed"] is False and path.read_bytes() == raw
    assert entry_identity(target) == before and downloaded.is_dir()
    assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []
    with pytest.raises(SetupFailure):
        recover_compiler_publications(
            target.with_name("different-pin"), deadline=time.monotonic() + 10
        )
    (target / "user").write_text("Changed target after acknowledgment")
    with pytest.raises(SetupFailure):
        recover_compiler_publications(target, deadline=time.monotonic() + 10)
    fresh = compiler_recovery_observation(path, target)
    acknowledge_compiler_publication(
        path, target, observation_id=fresh["observation_id"], processes_stopped=True
    )
    assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []
    assert len(list((directory / "loss-acknowledgments").glob("*.json"))) == 2
    path.write_bytes(raw + b" ")
    with pytest.raises(SetupFailure):
        recover_compiler_publications(target, deadline=time.monotonic() + 10)


def test_compiler_raw_loss_cli_returns_json_and_rejects_stale_observation(publication):
    _, target, _ = publication
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    directory.mkdir(parents=True)
    path = directory / "broken.json"
    path.write_text("{")
    command = [sys.executable, "-m", "leanflow_cli.workflows.project_setup.toolchain_recovery"]
    common = ["--journal", str(path), "--target", str(target)]
    result = subprocess.run(
        [*command, "inspect", *common], capture_output=True, text=True, env=os.environ.copy()
    )
    assert result.returncode == 0 and not result.stderr
    observed = json.loads(result.stdout)
    path.write_text("{ ")
    result = subprocess.run(
        [
            *command,
            "acknowledge",
            *common,
            "--observation",
            observed["observation_id"],
            "--confirm-processes-stopped",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and not result.stderr
    error = json.loads(result.stdout)
    assert error["success"] is False and "observation changed" in error["error"]
    assert error["journal"] == str(path) and error["target"] == str(target)


@pytest.mark.parametrize("attempt", [None, "ABC", "x/../y"])
def test_raw_loss_rejects_readable_known_live_owner_without_inventing_target(
    publication, monkeypatch, attempt
):
    _, target, _ = publication
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    directory.mkdir(parents=True)
    path = directory / "unusable-target.json"
    metadata = {"owner": {"pid": 987654, "start": "known owner"}}
    if attempt is not None:
        metadata.update(target=str(target), attempt_id=attempt)
    raw = json.dumps(metadata).encode()
    path.write_bytes(raw)
    monkeypatch.setattr(
        toolchain_journal_loss, "process_states", lambda: {987654: (0, 0, "known owner")}
    )
    observed = compiler_recovery_observation(path, target)
    assert observed["kind"] == "unreadable_compiler_journal"
    assert observed["owner_status"] == "known" and observed["original_owner_live"] is True
    with pytest.raises(SetupFailure, match="stopped acquisition"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=True
        )
    assert path.read_bytes() == raw and not target.exists()


def test_raw_loss_ack_exclusive_publication_preserves_actual_concurrent_creator(
    publication, monkeypatch
):
    _, target, _ = publication
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    directory.mkdir(parents=True)
    path = directory / "broken.json"
    raw = b'{"truncated'
    path.write_bytes(raw)
    observed = compiler_recovery_observation(path, target)
    actual_link = toolchain_journal_loss.os.link
    winners = []

    def competitor(source, destination, **kwargs):
        destination = Path(destination)
        if destination.suffix == ".json" and destination.parent.name == "loss-acknowledgments":
            script = "import json, os, sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({'independent_creator_pid': os.getpid()}))"
            result = subprocess.run([sys.executable, "-c", script, str(destination)])
            assert result.returncode == 0
            winners.append((destination, destination.read_bytes()))
        return actual_link(source, destination, **kwargs)

    monkeypatch.setattr(toolchain_journal_loss.os, "link", competitor)
    with pytest.raises(SetupFailure, match="Existing compiler loss acknowledgment changed"):
        acknowledge_compiler_publication(
            path, target, observation_id=observed["observation_id"], processes_stopped=True
        )
    assert len(winners) == 1 and winners[0][0].read_bytes() == winners[0][1]
    assert json.loads(winners[0][1])["independent_creator_pid"] != os.getpid()
    assert path.read_bytes() == raw and not target.exists()
    blob = next((directory / "loss-acknowledgments/blobs").iterdir())
    assert blob.read_bytes() == raw
    with pytest.raises(SetupFailure):
        recover_compiler_publications(target, deadline=time.monotonic() + 10)


@pytest.mark.parametrize(
    "invalid",
    [
        {"owner": {"pid": [], "start": "unusable"}},
        {"owner": {"pid": True, "start": "unusable"}},
        {"owner": {"pid": "123", "start": "unusable"}},
        {"owner": {"pid": -1, "start": "unusable"}},
        {"owner": {"pid": 1, "start": []}},
        {"owner": []},
        {"owner": None},
        {"state": []},
        {"target": []},
        {"target": "relative-target"},
        {"slot": []},
        {"incoming": {}},
        {"attempt_id": None},
        {"attempt_id": ""},
        {"attempt_id": "ABC"},
        {"attempt_id": "A" * 32},
        {"attempt_id": "a" * 31},
        {"attempt_id": "x/../y"},
        {"disposition": []},
        {"slot_inode": [True, 1]},
    ],
)
def test_malformed_compiler_metadata_cli_uses_exact_raw_loss(publication, invalid):
    _, target, _ = publication
    directory = toolchain_publication.leanflow_home() / "toolchain-installations"
    directory.mkdir(parents=True)
    path = directory / "malformed.json"
    raw = json.dumps({"target": str(target), **invalid}).encode()
    path.write_bytes(raw)
    command = [sys.executable, "-m", "leanflow_cli.workflows.project_setup.toolchain_recovery"]
    common = ["--journal", str(path), "--target", str(target)]
    shown = subprocess.run([*command, "inspect", *common], capture_output=True, text=True)
    assert shown.returncode == 0 and shown.stderr == ""
    observed = json.loads(shown.stdout)
    assert (
        observed["kind"] == "unreadable_compiler_journal"
        and observed["owner_status"] == "unavailable"
    )
    with pytest.raises(SetupFailure):
        recover_compiler_publications(target, deadline=time.monotonic() + 10)
    stale = subprocess.run(
        [*command, "acknowledge", *common, "--observation", "wrong", "--confirm-processes-stopped"],
        capture_output=True,
        text=True,
    )
    assert stale.returncode == 1 and not stale.stderr
    assert "observation changed" in json.loads(stale.stdout)["error"]
    accepted = subprocess.run(
        [
            *command,
            "acknowledge",
            *common,
            "--observation",
            observed["observation_id"],
            "--confirm-processes-stopped",
        ],
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0 and not accepted.stderr
    assert json.loads(accepted.stdout)["versions_removed"] is False
    assert path.read_bytes() == raw and not target.exists()
    assert recover_compiler_publications(target, deadline=time.monotonic() + 10) == []
