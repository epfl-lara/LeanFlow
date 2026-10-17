"""Recover exact preserved setup versions without inventing physical process closure."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
import time
import uuid
from io import StringIO
from pathlib import Path

import pytest

from leanflow_cli.formalization.evidence import durable_json
from leanflow_cli.workflows.project_setup import baseline_recovery as recovery
from leanflow_cli.workflows.project_setup import baseline_transaction as transaction
from leanflow_cli.workflows.project_setup import (
    history_archive,
    invocation_evidence,
    public_process,
    state,
)
from leanflow_cli.workflows.prover import source_mutation


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep test process evidence and canonical bytes separate from user state."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = (tmp_path / "project").resolve()
    root.mkdir()
    return root


def _ack(root: Path) -> dict:
    """Make all external confirmations explicit for one freshly inspected observation."""
    observed = recovery.observation(root)
    return recovery.acknowledge(
        root,
        observation_id=observed["observation_id"],
        processes_stopped=True,
        output_closed=True,
        keep_current=True,
    )


def _conflict(root: Path) -> dict:
    """Keep an editor's canonical version alongside both controller-owned versions."""
    (root / "lean-toolchain").write_bytes(b"original")
    record = transaction.begin(root)
    (Path(record["stage"]) / "lean-toolchain").write_bytes(b"prepared")
    transaction.promote(root, record, {"lean-toolchain": b"original"}, check=lambda: None)
    replacement = root / "editor-save"
    replacement.write_bytes(b"editor")
    replacement.replace(root / "lean-toolchain")
    with pytest.raises(RuntimeError):
        transaction.finish(root, record)
    with pytest.raises(RuntimeError):
        transaction.recover_pending(root)
    return record


def test_exact_editor_conflict_ack_preserves_every_version_and_requires_fresh_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    record = _conflict(root)
    mutation = record["mutations"][0]
    location = source_mutation.directory(root, mutation)
    preserved = {leaf: (location / leaf).read_bytes() for leaf in ("candidate", "displaced")}
    result = _ack(root)
    assert result["cleanup_verified"] is False and result["physical_eof_verified"] is False
    assert result["retain_inputs"] and result["fresh_setup_required"]
    assert (root / "lean-toolchain").read_bytes() == b"editor"
    assert preserved == {leaf: (location / leaf).read_bytes() for leaf in preserved}
    assert transaction.pending_issue(root) is None
    assert state.inspect_project(root)["code"] == "setup_recheck_required"
    fresh = transaction.begin(root)
    (Path(fresh["stage"]) / "lean-toolchain").write_bytes(b"verified-new")
    transaction.promote(root, fresh, {"lean-toolchain": b"editor"}, check=lambda: None)
    transaction.finish(root, fresh)
    assert transaction.pending_issue(root) is None
    assert not recovery.fresh_setup_required(root)
    # A detached descriptor's later edit is never hidden by the acknowledgment.
    (location / "displaced").write_bytes(b"late detached edit")
    assert "acknowledged retained" in (transaction.pending_issue(root) or "")


@pytest.mark.parametrize("target", ["canonical", "retained", "absence"])
def test_observation_is_revoked_by_every_new_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    root = _project(tmp_path, monkeypatch)
    record = _conflict(root)
    observed = recovery.observation(root)
    if target == "canonical":
        (root / "lean-toolchain").write_bytes(b"second editor")
    elif target == "retained":
        (source_mutation.directory(root, record["mutations"][0]) / "displaced").write_bytes(b"late")
    else:
        (root / "lakefile.lean").write_bytes(b"")
    with pytest.raises(ValueError, match="observation changed"):
        recovery.acknowledge(
            root,
            observation_id=observed["observation_id"],
            processes_stopped=True,
            output_closed=True,
            keep_current=True,
        )
    assert not (root / transaction.BASE / "recovery/current.json").exists()


@pytest.mark.parametrize("missing", ["processes_stopped", "output_closed", "keep_current"])
def test_external_ack_requires_each_explicit_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    values = dict(processes_stopped=True, output_closed=True, keep_current=True)
    values[missing] = False
    with pytest.raises(ValueError, match="explicit"):
        recovery.acknowledge(
            root, observation_id=recovery.observation(root)["observation_id"], **values
        )


def _command_controller(root_text: str, home_text: str) -> None:
    """Run an actual separately grouped child while the parent controller can be killed."""
    root = Path(root_text)
    os.environ["LEANFLOW_HOME"] = home_text
    transaction.begin(root)
    with public_process.bind_invocation_root(root):
        public_process.run_public_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=root,
            log=StringIO(),
            cancel=None,
            progress=None,
            phase="test",
        )


def test_dead_controller_ack_refuses_live_group_then_preserves_unknown_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    controller = multiprocessing.get_context("spawn").Process(
        target=_command_controller,
        args=(str(root), str(tmp_path / "home")),
    )
    controller.start()
    pid = None
    try:
        deadline = time.monotonic() + 10
        marker = None
        while time.monotonic() < deadline:
            paths = list(invocation_evidence.directory(root).glob("*.json"))
            if paths:
                marker = paths[0]
                value = json.loads(marker.read_text())
                if value.get("pid") and value.get("observed_processes"):
                    pid = value["pid"]
                    break
            time.sleep(0.02)
        assert pid and marker
        os.kill(controller.pid, signal.SIGKILL)
        controller.join(5)
        assert controller.exitcode == -signal.SIGKILL
        with pytest.raises(ValueError, match="no live"):
            _ack(root)
        os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = recovery.observation(root)
            if all(
                item["processes"].get("processes_absent")
                for item in observed["uncertain_invocations"].values()
            ):
                break
            time.sleep(0.05)
        result = _ack(root)
        assert result["cleanup_verified"] is False
        assert json.loads(marker.read_text())["cleanup_verified"] is False
        assert invocation_evidence.closed(root)
        assert not transaction.pending_issue(root)
        assert recovery.fresh_setup_required(root)
        invocation_evidence.compact(root)
        assert marker.exists(), "Unknown inputs must never be compacted or disposed"
    finally:
        if controller.is_alive():
            controller.kill()
            controller.join(5)
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_missing_ledger_and_legacy_unknown_identity_cannot_be_acknowledged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    identifier = uuid.uuid4().hex
    marker = invocation_evidence.register(root, identifier)
    with pytest.raises((ValueError, OSError)):
        recovery.observation(root)
    durable_json(marker, {"id": identifier, "root": str(root), "cleanup_verified": False})
    with pytest.raises(ValueError, match="available metadata"):
        _ack(root)


def test_more_than_256_closed_commands_are_preserved_and_missing_archive_revokes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    identifiers = []
    for _ in range(258):
        identifier = uuid.uuid4().hex
        identifiers.append(identifier)
        path = invocation_evidence.register(root, identifier)
        durable_json(path, {"id": identifier, "root": str(root), "cleanup_verified": True})
    assert invocation_evidence.closed(root)
    assert len(invocation_evidence.expected(root)) == 2
    archived = invocation_evidence.directory(root) / "archive"
    assert len(history_archive.members(archived)) == 256
    assert invocation_evidence.closed(root, required=[identifiers[0]])
    (archived / f"{identifiers[0]}.json").unlink()
    with pytest.raises(OSError):
        invocation_evidence.closed(root)


def test_archived_transaction_versions_are_still_continuously_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(transaction, "MAX_HISTORY", 3)
    # The discovery helper's default argument is intentionally fixed separately.
    (root / "lean-toolchain").write_bytes(b"old")
    first = transaction.begin(root)
    (Path(first["stage"]) / "lean-toolchain").write_bytes(b"new")
    transaction.promote(root, first, {"lean-toolchain": b"old"}, check=lambda: None)
    transaction.finish(root, first)
    for _ in range(3):
        record = transaction.begin(root)
        transaction.promote(root, record, {}, check=lambda: None)
        transaction.finish(root, record)
    archived = root / transaction.BASE / "archive" / f"{first['id']}.json"
    assert archived.exists()
    original = source_mutation.directory(root, first["mutations"][0]) / "displaced"
    original.write_bytes(b"late descriptor")
    assert "Retained" in (transaction.pending_issue(root) or "")


def test_legacy_locale_identity_mismatch_never_proves_controller_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    identifier = uuid.uuid4().hex
    marker = invocation_evidence.register(root, identifier)
    durable_json(
        marker,
        {
            "id": identifier,
            "root": str(root),
            "cleanup_verified": False,
            "controller": {"pid": 123, "start": "金 10月 16 00:00:00 2026"},
            "pid": None,
        },
    )
    monkeypatch.setattr(
        recovery, "_cleanup_states", lambda _: {123: (123, "S", "Fri Oct 16 00:00:00 2026")}
    )
    with pytest.raises(ValueError, match="identity|locale|format"):
        recovery.observation(root)


def test_258_terminal_setups_do_not_copy_lifetime_history_into_new_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    first = transaction.begin(root)
    invocation = uuid.uuid4().hex
    marker = invocation_evidence.register(root, invocation)
    transaction.register_invocation(root, invocation)
    durable_json(marker, {"id": invocation, "root": str(root), "cleanup_verified": True})
    transaction.promote(root, first, {}, check=lambda: None)
    transaction.finish(root, first)
    assert first["invocations"] == [invocation]
    # Seed closed history from a real completed journal. Exercise the expensive
    # production compaction once, rather than repeat 255 identical audit cycles.
    for _ in range(254):
        identifier = uuid.uuid4().hex
        stage = root / transaction.BASE / "stages" / identifier
        stage.mkdir()
        historical = {**first, "id": identifier, "stage": str(stage)}
        (root / transaction.BASE / f"{identifier}.json").write_text(json.dumps(historical))
    assert len(transaction.all_records(root)) == 255
    assert not history_archive.members(root / transaction.BASE / "archive")
    for _ in range(3):
        record = transaction.begin(root)
        transaction.promote(root, record, {}, check=lambda: None)
        transaction.finish(root, record)
        assert not record["invocations"]
    assert len(history_archive.members(root / transaction.BASE / "archive")) == 255
    assert len(transaction.all_records(root)) == 258
    assert transaction.pending_issue(root) is None
    assert (root / transaction.BASE / "archive" / f"{first['id']}.json").exists()
    assert invocation_evidence.closed(root, required=[invocation])


def test_acknowledged_canonical_late_edit_reblocks_before_a_fresh_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    _ack(root)
    (root / "lake-manifest.json").write_bytes(b"")
    assert "canonical" in (transaction.pending_issue(root) or "")
    with pytest.raises(RuntimeError):
        transaction.begin(root)


def test_archive_missing_header_and_late_record_changes_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    monkeypatch.setattr(invocation_evidence, "MAX_INVOCATIONS", 2)
    saved = []
    for _ in range(3):
        identifier = uuid.uuid4().hex
        saved.append(identifier)
        marker = invocation_evidence.register(root, identifier)
        durable_json(marker, {"id": identifier, "root": str(root), "cleanup_verified": True})
    archive = invocation_evidence.directory(root) / "archive"
    record = archive / f"{saved[0]}.json"
    raw = record.read_bytes()
    record.write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="changed"):
        invocation_evidence.closed(root)
    record.write_bytes(raw)
    header = archive / "head.json"
    header.unlink()
    assert not invocation_evidence.closed(root)


def test_public_recovery_cli_exposes_hash_bound_inspect_and_explicit_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    from leanflow_cli.cli.project_command import handle_project, register_project_parser

    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    parser = argparse.ArgumentParser()
    register_project_parser(parser.add_subparsers())
    args = parser.parse_args(["project", "recovery", "inspect", str(root), "--json"])
    assert handle_project(args) == 0
    observed = json.loads(capsys.readouterr().out)
    args = parser.parse_args(
        [
            "project",
            "recovery",
            "acknowledge",
            str(root),
            "--observation",
            observed["observation_id"],
            "--confirm-processes-stopped",
            "--confirm-output-closed",
            "--keep-current",
            "--json",
        ]
    )
    assert handle_project(args) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["cleanup_verified"] is False and reply["fresh_setup_required"] is True


def test_unrecorded_launch_group_never_becomes_absence_from_a_dead_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, monkeypatch)
    identifier = uuid.uuid4().hex
    marker = invocation_evidence.register(root, identifier)
    durable_json(
        marker,
        {
            "id": identifier,
            "root": str(root),
            "cleanup_verified": False,
            "controller": {"pid": 424242, "start": "Fri Oct 16 00:00:00 2026"},
            "state": "admitting",
            "pid": None,
            "observed_processes": {},
        },
    )
    monkeypatch.setattr(
        recovery, "_cleanup_states", lambda _: {1: (1, "S", "Fri Oct 16 00:00:00 2026")}
    )
    with pytest.raises(ValueError, match="available metadata"):
        _ack(root)
    assert json.loads(marker.read_text())["cleanup_verified"] is False
