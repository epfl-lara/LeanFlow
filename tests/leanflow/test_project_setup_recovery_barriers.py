"""Keep external recovery acknowledgment crash-safe and separate from verified readiness."""

from __future__ import annotations

import json
import multiprocessing
import os
import uuid
from pathlib import Path

import pytest

from leanflow_cli.formalization.evidence import durable_json
from leanflow_cli.workflows.project_setup import baseline_recovery as recovery
from leanflow_cli.workflows.project_setup import baseline_transaction as transaction
from leanflow_cli.workflows.project_setup import invocation_evidence, recovery_barrier, state
from tests.leanflow.test_project_setup_recovery import _ack, _conflict, _project


def test_missing_ack_barrier_never_restores_old_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    _ack(root)
    (root / transaction.BASE / "recovery/current.json").unlink()
    assert state.inspect_project(root)["code"] == "setup_recovery_required"
    with pytest.raises(RuntimeError):
        transaction.begin(root)


def test_failed_fresh_promotion_remains_retryable_without_another_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    _ack(root)
    fresh = transaction.begin(root)
    (Path(fresh["stage"]) / "lean-toolchain").write_bytes(b"fresh")
    transaction.promote(root, fresh, {"lean-toolchain": b"editor"}, check=lambda: None)
    # Simulate a verified-closed network/check failure after canonical promotion.
    transaction.recover_pending(root)
    assert recovery.fresh_setup_required(root)
    assert state.inspect_project(root)["code"] == "setup_recheck_required"
    next_attempt = transaction.begin(root)
    (Path(next_attempt["stage"]) / "lean-toolchain").write_bytes(b"verified")
    transaction.promote(root, next_attempt, {"lean-toolchain": b"fresh"}, check=lambda: None)
    transaction.finish(root, next_attempt)
    assert not recovery.fresh_setup_required(root)


@pytest.mark.parametrize("recorded", ["controller", "descendant"])
def test_present_different_start_frame_cannot_establish_process_absence(recorded: str) -> None:
    value = {
        "controller": {"pid": 100, "start": "Fri Oct 16 09:00:00 2026"},
        "pid": 200,
        "observed_processes": {"300": "Fri Oct 16 09:00:00 2026"},
    }
    pid = 100 if recorded == "controller" else 300
    observed = recovery._process_observation(value, {pid: (400, "S", "Fri Oct 16 00:00:00 2026")})
    assert not observed.get("metadata_available")
    assert observed.get("processes_absent") is not True


def test_exact_prior_invocation_ack_does_not_recheck_a_reused_unrelated_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            "controller": {"pid": 100, "start": "Fri Oct 16 00:00:00 2026"},
            "pid": 200,
            "observed_processes": {"200": "Fri Oct 16 00:00:00 2026"},
        },
    )
    monkeypatch.setattr(recovery, "_cleanup_states", lambda _: {})
    _ack(root)
    monkeypatch.setattr(
        recovery, "_cleanup_states", lambda _: {900: (200, "S", "Sat Oct 17 00:00:00 2026")}
    )
    _ack(root)
    assert json.loads(marker.read_text())["cleanup_verified"] is False


@pytest.mark.parametrize("content", ["[", "[]", "null", '{"schema":1}'])
def test_malformed_current_barrier_is_structured_recovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    _ack(root)
    (root / transaction.BASE / "recovery/current.json").write_text(content)
    result = state.inspect_project(root)
    assert not result["ready"] and result["code"] == "setup_recovery_required"


def test_fully_durable_ack_reports_effect_even_when_final_audit_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    observed = recovery.observation(root)
    monkeypatch.setattr(transaction, "pending_issue", lambda _: "new retained version observed")
    result = recovery.acknowledge(
        root,
        observation_id=observed["observation_id"],
        processes_stopped=True,
        output_closed=True,
        keep_current=True,
    )
    assert result["acknowledgment_committed"] is True
    assert result["ready"] is False
    assert result["recovery_issue"] == "new retained version observed"
    assert result["cleanup_verified"] is False


def _ack_until_write_boundary(root_text: str, home: str, stop: int, trace_text: str) -> None:
    """Exit after exact durable effects without running any model or external command."""
    root, trace = Path(root_text), Path(trace_text)
    os.environ["LEANFLOW_HOME"] = home
    recovery._cleanup_states = lambda _: {}
    original_write = durable_json
    original_sync = recovery.sync_directory
    original_rename = recovery.rename_exclusive
    count = 0

    def checkpoint(label: str) -> None:
        nonlocal count
        count += 1
        with trace.open("a") as handle:
            handle.write(json.dumps({"boundary": count, "label": label}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if count == stop:
            os._exit(91)

    def write(path: Path, value: object) -> None:
        original_write(path, value)
        checkpoint("durable:" + path.name)

    def sync(path: Path) -> None:
        original_sync(path)
        checkpoint("preserved-intent")

    def rename(source: Path, destination: Path) -> None:
        original_rename(source, destination)
        checkpoint("terminal-journal")

    recovery.durable_json = write
    recovery_barrier.durable_json = write
    transaction.durable_json = write
    recovery.sync_directory = sync
    recovery.rename_exclusive = rename
    _ack(root)


@pytest.mark.parametrize("boundary", range(1, 14))
def test_ack_process_death_at_each_durable_boundary_never_rolls_back_keep_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: int
) -> None:
    root = _project(tmp_path, monkeypatch)
    (root / "lean-toolchain").write_bytes(b"original")
    record = transaction.begin(root)
    (Path(record["stage"]) / "lean-toolchain").write_bytes(b"keep-current")
    transaction.promote(root, record, {"lean-toolchain": b"original"}, check=lambda: None)
    # Actual crash window: installation completed while the terminal journal
    # still says promoting. An acknowledgment must never authorize its rollback.
    record["state"] = "promoting"
    transaction._save(root, record)
    identifier = uuid.uuid4().hex
    marker = invocation_evidence.register(root, identifier)
    transaction.register_invocation(root, identifier)
    durable_json(
        marker,
        {
            "id": identifier,
            "root": str(root),
            "cleanup_verified": False,
            "controller": {"pid": 424242, "start": "Fri Oct 16 00:00:00 2026"},
            "pid": 424244,
            "observed_processes": {"424244": "Fri Oct 16 00:00:00 2026"},
        },
    )
    state.state_directory(root).mkdir(parents=True, exist_ok=True)
    state.save_result(root, {"status": "ready"})
    monkeypatch.setattr(state, "installed_environment", lambda _: True)
    trace = tmp_path / "crash-boundaries.jsonl"
    child = multiprocessing.get_context("spawn").Process(
        target=_ack_until_write_boundary,
        args=(str(root), str(tmp_path / "home"), boundary, str(trace)),
    )
    child.start()
    try:
        child.join(10)
        assert child.exitcode == (91 if boundary <= 12 else 0)
    finally:
        if child.is_alive():
            child.kill()
            child.join(5)
    entries = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(entries) == min(boundary, 12)
    assert state.inspect_project(root)["ready"] is False
    try:
        transaction.recover_pending(root)
    except (RuntimeError, OSError, ValueError):
        pass
    assert (root / "lean-toolchain").read_bytes() == b"keep-current"
    assert json.loads(marker.read_text())["cleanup_verified"] is False
    assert Path(record["stage"]).exists()
    if boundary >= 12:
        assert recovery.fresh_setup_required(root)


@pytest.mark.parametrize("missing", ["anchor", "entire_recovery", "completed_barrier"])
def test_positive_ack_anchors_are_required_after_current_payload_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    import shutil

    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    result = _ack(root)
    if missing == "anchor":
        (root / transaction.BASE / "recovery.anchor").unlink()
    elif missing == "entire_recovery":
        shutil.rmtree(root / transaction.BASE / "recovery")
    else:
        (root / transaction.BASE / "recovery" / f"barrier-{result['observation_id']}.json").unlink()
    assert state.inspect_project(root)["code"] == "setup_recovery_required"
    assert (root / "lean-toolchain").read_bytes() == b"editor"


def test_completion_marker_requires_a_committed_fresh_parent_bound_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    acknowledged = _ack(root)
    durable_json(
        root / transaction.BASE / "recovery/consumed.json",
        {
            "schema": 1,
            "observation_id": acknowledged["observation_id"],
            "verified_transaction": uuid.uuid4().hex,
        },
    )
    assert state.inspect_project(root)["code"] == "setup_recovery_required"


def test_public_ack_reply_distinguishes_committed_effect_from_final_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from leanflow_cli.cli.project_command import handle_project, register_project_parser

    root = _project(tmp_path, monkeypatch)
    _conflict(root)
    observed = recovery.observation(root)
    parser = argparse.ArgumentParser()
    register_project_parser(parser.add_subparsers())
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
    monkeypatch.setattr(transaction, "pending_issue", lambda _: "late retained edit")
    assert handle_project(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "acknowledged_external_NOT_VERIFIED"
    assert report["acknowledgment_committed"] is True and report["ready"] is False
    assert report["recovery_issue"] == "late retained edit"
