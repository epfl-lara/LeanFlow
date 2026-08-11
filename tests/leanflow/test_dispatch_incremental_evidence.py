"""Tests for durable checked-helper evidence at dispatch interruption boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.native import runtime_cleanup
from leanflow_cli.workflows import dispatch_incremental_evidence as evidence
from leanflow_cli.workflows import dispatch_ledger_compaction, research_route_context
from leanflow_cli.workflows import dispatch_service as ds
from leanflow_cli.workflows.dispatch_models import JobBudget, JobSpec, LedgerEntry
from tools.implementations import delegate_tool


def _spec(job_id: str = "run.orchestrator.ds-001") -> JobSpec:
    return JobSpec(
        job_id=job_id,
        archetype="deep_search",
        requester_role="orchestrator",
        objective="find a distinct checked helper",
        budget=JobBudget(api_steps=4, wall_clock_s=60),
        deliverable="findings_report",
        inputs={
            "target_symbol": "erdos_242",
            "active_file": "/tmp/FormalConjectures/Erdos242.lean",
            "assignment_statement_sha256": "a" * 64,
        },
        scope={"scratch_only": True},
        parent_job_id=job_id.rpartition(".")[0],
    )


def _helper(declaration: str = "lemma checked_leaf : True := by trivial") -> dict[str, Any]:
    return {
        "anchor_target_symbol": "erdos_242",
        "active_file": "/tmp/FormalConjectures/Erdos242.lean",
        "declaration": declaration,
        "declaration_sha256": hashlib.sha256(declaration.encode("utf-8")).hexdigest(),
        "worker_check": {
            "tool": "lean_incremental_check",
            "action": "check_helper",
            "valid_without_sorry": True,
            "has_errors": False,
            "has_sorry": False,
            "verification_scope": "helper_candidate",
            "replacement_matches_target": False,
            "replacement_declarations": ["checked_leaf"],
            "elapsed_s": 2.5,
        },
        "parent_recheck_required": True,
    }


@pytest.fixture()
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    (tmp_path / ".leanflow").mkdir()
    (tmp_path / ".leanflow" / "project.yaml").write_text("name: t\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    monkeypatch.setattr(ds, "append_workflow_activity", lambda *_args, **_kwargs: None)
    return ds.DispatchService(parent_agent=object(), root_job_id="run")


def _journal(service: ds.DispatchService, entry: LedgerEntry) -> Path:
    path = service._async_incremental_evidence_path(
        entry.spec.job_id,
        entry.launch_nonce,
    )
    assert evidence.publish_checked_helpers(
        path,
        launch_nonce=entry.launch_nonce,
        spec=entry.spec,
        helpers=[_helper()],
    )
    return path


def test_journal_is_nonce_spec_bound_and_rejects_tampering(tmp_path):
    path = tmp_path / "worker.evidence.json"
    spec = _spec()

    assert evidence.publish_checked_helpers(
        path,
        launch_nonce="launch-1",
        spec=spec,
        helpers=[_helper()],
    )
    assert evidence.load_checked_helpers(
        path,
        launch_nonce="launch-1",
        spec=spec,
    ) == [_helper()]
    assert (
        evidence.load_checked_helpers(
            path,
            launch_nonce="launch-2",
            spec=spec,
        )
        == []
    )
    changed_revision = JobSpec.from_mapping(
        {
            **spec.to_mapping(),
            "inputs": {**spec.inputs, "assignment_statement_sha256": "b" * 64},
        }
    )
    assert (
        evidence.load_checked_helpers(
            path,
            launch_nonce="launch-1",
            spec=changed_revision,
        )
        == []
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["checked_helpers"][0]["declaration"] += "\n-- altered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        evidence.load_checked_helpers(
            path,
            launch_nonce="launch-1",
            spec=spec,
        )
        == []
    )


def test_journal_keeps_only_newest_eight_distinct_helpers(tmp_path):
    path = tmp_path / "worker.evidence.json"
    helpers = [_helper(f"lemma checked_leaf_{index} : True := by trivial") for index in range(10)]

    assert evidence.publish_checked_helpers(
        path,
        launch_nonce="launch",
        spec=_spec(),
        helpers=helpers,
    )

    loaded = evidence.load_checked_helpers(path, launch_nonce="launch", spec=_spec())
    assert len(loaded) == evidence.MAX_CHECKED_HELPERS
    assert "checked_leaf_0" not in loaded[0]["declaration"]
    assert "checked_leaf_2" in loaded[0]["declaration"]
    assert "checked_leaf_9" in loaded[-1]["declaration"]


def test_journal_binding_survives_terminal_route_context_compaction(tmp_path):
    path = tmp_path / "worker.evidence.json"
    original = _spec()
    original = JobSpec.from_mapping(
        {
            **original.to_mapping(),
            "objective": (
                original.objective
                + f'\n\n{research_route_context.ROUTE_CONTEXT_MARKER}\n{{"version":3}}'
            ),
            "inputs": {
                **original.inputs,
                research_route_context.ROUTE_CONTEXT_INPUT_KEY: {
                    "version": research_route_context.CONTEXT_VERSION,
                    "consumed_target_facts": [],
                },
            },
        }
    )
    assert evidence.publish_checked_helpers(
        path,
        launch_nonce="launch",
        spec=original,
        helpers=[_helper()],
    )
    record = {"state": "killed", "spec": original.to_mapping()}
    dispatch_ledger_compaction.compact_terminal_dispatch_records([record])
    compacted = JobSpec.from_mapping(record["spec"])

    assert compacted != original
    assert evidence.load_checked_helpers(
        path,
        launch_nonce="launch",
        spec=compacted,
    ) == [_helper()]


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask")
    or not hasattr(signal, "pthread_kill")
    or not hasattr(signal, "SIGTERM"),
    reason="POSIX signal masking is unavailable",
)
def test_secondary_thread_sigterm_retries_journal_then_preserves_termination(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "worker.evidence.json"
    real_atomic_write = evidence.atomic_json_write
    commit_calls = 0
    commit_entered = threading.Event()
    signal_sent = threading.Event()

    def send_thread_signal():
        assert commit_entered.wait(timeout=2)
        signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
        signal_sent.set()

    sender = threading.Thread(target=send_thread_signal, name="preexisting-signal-sender")
    sender.start()

    def signal_mid_commit(target, payload, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            commit_entered.set()
            assert signal_sent.wait(timeout=2)
            # Give CPython a bytecode boundary to run a signal delivered via
            # the pre-existing unblocked thread before the first write ends.
            time.sleep(0.01)
        real_atomic_write(target, payload, **kwargs)

    monkeypatch.setattr(evidence, "atomic_json_write", signal_mid_commit)
    handlers = runtime_cleanup.install_native_termination_handlers()
    try:
        with pytest.raises(runtime_cleanup.NativeTerminationSignal) as raised:
            evidence.publish_checked_helpers(
                path,
                launch_nonce="launch",
                spec=_spec(),
                helpers=[_helper()],
            )
    finally:
        sender.join(timeout=2)
        runtime_cleanup.restore_native_termination_handlers(handlers)

    assert not sender.is_alive()
    assert raised.value.signum == signal.SIGTERM
    assert commit_calls == 2
    assert evidence.load_checked_helpers(
        path,
        launch_nonce="launch",
        spec=_spec(),
    ) == [_helper()]


def test_worker_check_is_journaled_before_interrupted_backend_returns(monkeypatch, tmp_path):
    """Pin the live bug: successful helper check precedes backend interruption."""
    spec = _spec()
    path = tmp_path / "worker.evidence.json"

    def publish(helpers):
        evidence.publish_checked_helpers(
            path,
            launch_nonce="launch",
            spec=spec,
            helpers=helpers,
        )

    def interrupted_delegate(*_args, **kwargs):
        callback = kwargs["post_tool_result_callback"]
        declaration = _helper()["declaration"]
        callback(
            "lean_incremental_check",
            {
                "action": "check_helper",
                "theorem_id": "erdos_242",
                "file_path": "/tmp/FormalConjectures/Erdos242.lean",
                "replacement": declaration,
            },
            json.dumps(
                {
                    "success": True,
                    "ok": True,
                    "action": "check_helper",
                    "file": "/tmp/FormalConjectures/Erdos242.lean",
                    "target": "erdos_242",
                    "valid_without_sorry": True,
                    "has_errors": False,
                    "has_sorry": False,
                    "verification_scope": "helper_candidate",
                    "replacement_matches_target": False,
                    "replacement_declarations": ["checked_leaf"],
                    "elapsed_s": 2.5,
                }
            ),
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(delegate_tool, "delegate_task", interrupted_delegate)
    service = ds.DispatchService(
        parent_agent=object(),
        incremental_evidence_sink=publish,
    )

    with pytest.raises(KeyboardInterrupt):
        service._run_delegate_job(spec)

    assert evidence.load_checked_helpers(
        path,
        launch_nonce="launch",
        spec=spec,
    ) == [_helper()]


def test_parent_harvests_journal_after_keyboard_interrupt_artifact(service, monkeypatch):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="running",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="c" * 64,
    )
    service._save_entry(entry)
    _journal(service, entry)
    result_path = service._async_result_path(spec.job_id, entry.launch_nonce)
    result_path.write_text(
        json.dumps(
            {
                "launch_nonce": entry.launch_nonce,
                "ok": False,
                "error": "KeyboardInterrupt: ",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: True)

    assert service.poll(spec.job_id)["state"] == "done"

    consumed = service.consume(spec.job_id)
    deliverable = consumed["deliverable"]
    assert deliverable["status"] == "interrupted_with_worker_checked_helper_evidence"
    assert deliverable["checked_helpers"] == [_helper()]
    assert deliverable["evidence_authority"] == "worker_observation_only"
    assert deliverable["parent_recheck_required"] is True
    assert consumed["plan_delta"] == []


def test_complete_result_absorbs_then_removes_redundant_journal(service, monkeypatch):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="running",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="c" * 64,
    )
    service._save_entry(entry)
    path = _journal(service, entry)
    result_path = service._async_result_path(spec.job_id, entry.launch_nonce)
    result_path.write_text(
        json.dumps(
            {
                "launch_nonce": entry.launch_nonce,
                "ok": True,
                "result": {
                    "status": "done",
                    "deliverable": {"summary": "complete model report"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: True)

    assert service.poll(spec.job_id)["state"] == "done"

    persisted = service._entry(spec.job_id)
    assert persisted.result["deliverable"]["checked_helpers"] == [_helper()]
    assert persisted.result["deliverable"]["parent_recheck_required"] is True
    assert not path.exists()


def test_complete_result_does_not_free_capacity_before_exact_worker_exit(service, monkeypatch):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="running",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="c" * 64,
    )
    service._save_entry(entry)
    result_path = service._async_result_path(spec.job_id, entry.launch_nonce)
    ds.atomic_json_write(
        result_path,
        {
            "launch_nonce": entry.launch_nonce,
            "ok": True,
            "result": {"status": "done", "deliverable": {"summary": "published"}},
        },
        sort_keys=True,
    )
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: False)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)

    assert service.poll(spec.job_id)["state"] == "running"
    assert service._entry(spec.job_id).result == {}


def test_complete_result_uses_exact_exit_when_reap_owner_is_unavailable(service, monkeypatch):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="running",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="c" * 64,
    )
    service._save_entry(entry)
    result_path = service._async_result_path(spec.job_id, entry.launch_nonce)
    ds.atomic_json_write(
        result_path,
        {
            "launch_nonce": entry.launch_nonce,
            "ok": True,
            "result": {"status": "done", "deliverable": {"summary": "published"}},
        },
        sort_keys=True,
    )
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: False)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: True)

    assert service.poll(spec.job_id)["state"] == "done"
    assert service._entry(spec.job_id).result["deliverable"]["summary"] == "published"


def test_parent_harvests_journal_after_dead_worker_without_final_result(service, monkeypatch):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="running",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="c" * 64,
    )
    service._save_entry(entry)
    _journal(service, entry)
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: False)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: True)
    monkeypatch.setattr(ds, "ASYNC_RESULT_PUBLICATION_GRACE_S", 0.0)

    reconciled = service.reconcile()

    recovered = next(item for item in reconciled if item.spec.job_id == spec.job_id)
    assert recovered.state == "done"
    assert recovered.result["partial_worker_evidence"] is True
    assert recovered.result["deliverable"]["parent_recheck_required"] is True


def test_deployed_same_parent_keeps_evidence_reserved_when_exit_is_unconfirmed(
    service,
    monkeypatch,
):
    """A transient identity miss must not harvest or rotate a possibly live worker."""
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="deployed",
        launch_nonce="launch",
        launch_started_at="2026-07-18T02:09:00+00:00",
        launch_attempt=1,
    )
    service._save_entry(entry)
    _journal(service, entry)
    ds.atomic_json_write(
        service._async_identity_path(spec.job_id, entry.launch_nonce),
        {
            "version": 1,
            "launch_nonce": entry.launch_nonce,
            "process_id": 4242,
            "process_group_id": 4242,
            "process_session_id": 4242,
            "process_token_sha256": "c" * 64,
            "parent_process_id": os.getpid(),
        },
        sort_keys=True,
    )
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: False)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    monkeypatch.setattr(
        ds,
        "_terminate_dispatch_process_and_wait",
        lambda _entry: pytest.fail("same-parent ambiguous worker must not be retired"),
    )
    monkeypatch.setattr(
        service,
        "_reserve_async_launch_retry",
        lambda _entry: pytest.fail("ambiguous worker must not rotate its nonce"),
    )

    recovered = service._recover_deployed_launch(entry, retry_if_stale=True)

    assert recovered.state == "deployed"
    assert recovered.launch_nonce == entry.launch_nonce
    assert service._entry(spec.job_id).state == "deployed"
    assert service._entry(spec.job_id).result == {}


def test_parent_shutdown_harvests_journal_after_confirmed_worker_exit(service, monkeypatch):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="running",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="c" * 64,
    )
    service._save_entry(entry)
    _journal(service, entry)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    monkeypatch.setattr(ds, "_terminate_dispatch_process_and_wait", lambda _entry: True)

    outcome = service.kill(spec.job_id, requester_job_id="run")

    assert outcome["state"] == "done"
    assert outcome["killed"] is False
    persisted = service._entry(spec.job_id)
    assert persisted.result["partial_worker_evidence"] is True
    assert persisted.result["plan_delta"] == []


def test_resume_recovers_journal_that_predates_killed_verdict(service, monkeypatch):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="killed",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="c" * 64,
        finished_at="2026-07-18T02:09:33+00:00",
        notes="killed by run",
    )
    service._save_entry(entry)
    path = _journal(service, entry)
    timestamp = datetime(2026, 7, 18, 2, 9, 17, tzinfo=UTC).timestamp()
    os.utime(path, (timestamp, timestamp))
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: True)

    recovered = service.recover_completed_artifacts()

    assert [item.spec.job_id for item in recovered] == [spec.job_id]
    persisted = service._entry(spec.job_id)
    assert persisted.state == "done"
    assert persisted.result["deliverable"]["checked_helpers"] == [_helper()]
    assert persisted.result["deliverable"]["parent_recheck_required"] is True


@pytest.mark.parametrize(
    ("exact_identity", "exit_confirmed"),
    [(True, False), (False, True)],
)
def test_resume_rejects_journal_without_exact_confirmed_process_exit(
    service,
    monkeypatch,
    exact_identity,
    exit_confirmed,
):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="killed",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242 if exact_identity else 0,
        process_session_id=4242 if exact_identity else 0,
        process_token_sha256="c" * 64 if exact_identity else "",
        finished_at="2026-07-18T02:09:33+00:00",
        notes="killed by run",
    )
    service._save_entry(entry)
    path = _journal(service, entry)
    timestamp = datetime(2026, 7, 18, 2, 9, 17, tzinfo=UTC).timestamp()
    os.utime(path, (timestamp, timestamp))
    monkeypatch.setattr(
        ds,
        "_dispatch_process_identity_has_exited",
        lambda _entry: exit_confirmed,
    )

    assert service.recover_completed_artifacts() == []
    assert service._entry(spec.job_id).state == "killed"
    assert path.exists()


def test_resume_complete_result_absorbs_and_discards_preverdict_journal(service, monkeypatch):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="killed",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="c" * 64,
        finished_at="2026-07-18T02:09:33+00:00",
        notes="killed by run",
    )
    service._save_entry(entry)
    journal_path = _journal(service, entry)
    result_path = service._async_result_path(spec.job_id, entry.launch_nonce)
    ds.atomic_json_write(
        result_path,
        {
            "launch_nonce": entry.launch_nonce,
            "ok": True,
            "result": {
                "status": "done",
                "deliverable": {"summary": "complete before terminal verdict"},
            },
        },
        sort_keys=True,
    )
    journal_timestamp = datetime(2026, 7, 18, 2, 9, 17, tzinfo=UTC).timestamp()
    result_timestamp = datetime(2026, 7, 18, 2, 9, 20, tzinfo=UTC).timestamp()
    os.utime(journal_path, (journal_timestamp, journal_timestamp))
    os.utime(result_path, (result_timestamp, result_timestamp))
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: True)

    recovered = service.recover_completed_artifacts()

    assert [item.spec.job_id for item in recovered] == [spec.job_id]
    persisted = service._entry(spec.job_id)
    assert persisted.state == "done"
    assert persisted.result["deliverable"]["checked_helpers"] == [_helper()]
    assert persisted.result["deliverable"]["parent_recheck_required"] is True
    assert not journal_path.exists()


def test_resume_rejects_complete_result_while_exact_worker_exit_is_unconfirmed(
    service,
    monkeypatch,
):
    spec = _spec()
    entry = LedgerEntry(
        spec=spec,
        state="killed",
        launch_nonce="launch",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="c" * 64,
        finished_at="2026-07-18T02:09:33+00:00",
        notes="killed by run",
    )
    service._save_entry(entry)
    result_path = service._async_result_path(spec.job_id, entry.launch_nonce)
    ds.atomic_json_write(
        result_path,
        {
            "launch_nonce": entry.launch_nonce,
            "ok": True,
            "result": {"status": "done", "deliverable": {"summary": "too early"}},
        },
        sort_keys=True,
    )
    timestamp = datetime(2026, 7, 18, 2, 9, 20, tzinfo=UTC).timestamp()
    os.utime(result_path, (timestamp, timestamp))
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)

    assert service.recover_completed_artifacts() == []
    assert service._entry(spec.job_id).state == "killed"
