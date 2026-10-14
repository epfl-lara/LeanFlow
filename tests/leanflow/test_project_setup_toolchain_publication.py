"""Preserve every compiler version through adjacent-slot publication and interruption."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agent.runtime.harness.isolation import IsolationPolicy
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.workflows.project_setup import toolchain_publication, toolchain_recovery
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
