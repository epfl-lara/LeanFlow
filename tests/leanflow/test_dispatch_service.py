"""Phase 3 tests: dispatch models (lineage, state machine) + service lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from core.process_identity import PROCESS_TOKEN_ENV, process_token_sha256
from core.toolsets import resolve_multiple_toolsets
from leanflow_cli.workflows import dispatch_service as ds
from leanflow_cli.workflows import research_route_context
from leanflow_cli.workflows.dispatch_models import (
    JobBudget,
    JobSpec,
    LedgerEntry,
    ancestors,
    descendants,
    is_ancestor,
    next_job_id,
)


def _spec(job_id: str, *, role: str = "planner", archetype: str = "negation_probe") -> JobSpec:
    deliverable = {
        "deep_search": "findings_report",
        "empirical": "experiment_result",
        "decomposition": "decomposition_report",
    }.get(archetype, "probe_verdict")
    return JobSpec(
        job_id=job_id,
        archetype=archetype,
        requester_role=role,
        objective="probe the negation of demo",
        budget=JobBudget(api_steps=20, wall_clock_s=300),
        deliverable=deliverable,
        scope={"scratch_only": True},
        parent_job_id=job_id.rpartition(".")[0],
    )


def _successful_helper_check(
    declaration: str,
    *,
    target_symbol: str = "demo",
    active_file: str = "/tmp/Main.lean",
) -> tuple[dict[str, Any], str]:
    """Return exact check_helper arguments and one successful checker result."""
    arguments = {
        "action": "check_helper",
        "theorem_id": target_symbol,
        "file_path": active_file,
        "replacement": declaration,
        "timeout_s": 300,
    }
    result = {
        "success": True,
        "ok": True,
        "action": "check_helper",
        "file": active_file,
        "target": target_symbol,
        "valid_without_sorry": True,
        "has_errors": False,
        "has_sorry": False,
        "verification_scope": "helper_candidate",
        "replacement_matches_target": False,
        "replacement_declarations": ["demo_helper"],
        "elapsed_s": 33.93,
    }
    return arguments, json.dumps(result)


# --- pure models -----------------------------------------------------------


def test_lineage_minting_and_ancestry():
    first = next_job_id([], "run.orchestrator", "deep_search")
    assert first == "run.orchestrator.ds-001"
    second = next_job_id([first], "run.orchestrator", "negation_probe")
    assert second == "run.orchestrator.np-002"
    grandchild = next_job_id([first, second], first, "empirical")
    assert grandchild == "run.orchestrator.ds-001.em-001"
    decomposition = next_job_id([first, second], "run.orchestrator", "decomposition")
    assert decomposition == "run.orchestrator.dc-003"

    assert ancestors(grandchild) == (
        "run",
        "run.orchestrator",
        "run.orchestrator.ds-001",
    )
    assert is_ancestor("run.orchestrator", grandchild)
    assert not is_ancestor("run.orchestrator.np-002", grandchild)
    assert descendants([first, second, grandchild], "run.orchestrator") == (
        first,
        second,
        grandchild,
    )


def test_spec_validation_rejects_non_dispatch_roles_and_bad_budgets():
    assert _spec("run.np-001").validate() == []
    # N2: the manager suggests, the prover escalates — neither dispatches.
    assert any("may not dispatch" in p for p in _spec("run.np-001", role="prover").validate())
    assert any("may not dispatch" in p for p in _spec("run.np-001", role="manager").validate())
    bad_budget = JobSpec(
        job_id="run.np-001",
        archetype="negation_probe",
        requester_role="planner",
        objective="x",
        budget=JobBudget(api_steps=0, wall_clock_s=0),
        deliverable="probe_verdict",
    )
    assert any("budget" in p for p in bad_budget.validate())


def test_decomposition_spec_requires_isolated_proposal_contract():
    valid = _spec("run.decomposer.dc-001", archetype="decomposition")

    assert valid.validate() == []
    wrong_deliverable = replace(valid, deliverable="findings_report")
    assert any("decomposition_report" in problem for problem in wrong_deliverable.validate())
    writable = replace(valid, scope={})
    assert any("scratch_only" in problem for problem in writable.validate())
    reserved = replace(
        _spec("run.ds-001", archetype="deep_search"), deliverable="decomposition_report"
    )
    assert any("reserved" in problem for problem in reserved.validate())


def test_spec_validation_rejects_incomplete_evidence_anchor_before_deploy():
    incomplete = replace(
        _spec("run.ds-001", archetype="deep_search"),
        inputs={"route_anchor_job_id": "run.ds-000"},
    )

    assert any("source finding payload" in problem for problem in incomplete.validate())

    complete = replace(
        incomplete,
        inputs={
            **incomplete.inputs,
            "route_anchor_provenance": {"job_id": "run.ds-000"},
            "route_anchor_finding_summary": '{"deliverable":{"status":"done"}}',
        },
    )
    assert complete.validate() == []


def test_ledger_state_machine_rejects_illegal_transitions():
    entry = LedgerEntry(spec=_spec("run.np-001"))
    deployed = entry.with_state("deployed")
    running = deployed.with_state("running")
    done = running.with_state("done")
    assert done.is_terminal()
    with pytest.raises(ValueError):
        entry.with_state("done")  # proposed -> done skips deploy/run
    with pytest.raises(ValueError):
        done.with_state("running")  # terminal states are final
    stuck = running.with_state("stuck")
    assert stuck.with_state("killed").state == "killed"


def test_entry_round_trip():
    entry = LedgerEntry(
        spec=_spec("run.np-001"),
        state="running",
        agent_session_ids=("a1",),
        launch_nonce="launch-token",
        launch_started_at="2026-07-16T03:00:00+00:00",
        launch_attempt=2,
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
        process_released_at="2026-07-16T03:05:00+00:00",
        process_release_reason="legacy-process-command-mismatch",
        process_release_evidence_sha256="b" * 64,
        process_release_observed_started_at="2026-07-16T03:04:59+00:00",
        process_release_report_key="research-portfolio-capacity-released:abc",
        process_release_reported_at="2026-07-16T03:05:01+00:00",
    )
    assert LedgerEntry.from_mapping(entry.to_mapping()) == entry


# --- service ----------------------------------------------------------------


@pytest.fixture()
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    (tmp_path / ".leanflow").mkdir()
    (tmp_path / ".leanflow" / "project.yaml").write_text("name: t\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    events: list[tuple] = []
    monkeypatch.setattr(
        ds, "append_workflow_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    svc = ds.DispatchService(parent_agent=object(), root_job_id="run")
    svc.test_events = events  # type: ignore[attr-defined]
    return svc


def test_propose_deploy_consume_happy_path(service, monkeypatch):
    monkeypatch.setattr(
        ds.DispatchService,
        "_run_delegate_job",
        lambda self, spec: {
            "status": "done",
            "deliverable": {"summary": "negation proved"},
            "artifact_paths": [],
            "plan_delta": [{"node_id": "n1", "status": "false", "evidence": spec.job_id}],
        },
    )
    minted = service.mint_job_id("negation_probe", role="planner")
    assert minted == "run.planner.np-001"  # <root>.<role-path>.<tag>-<seq>
    spec = _spec(minted)
    service.propose(spec)

    entry = service.deploy(spec.job_id)

    assert entry.state == "done"
    consumed = service.consume(spec.job_id)
    assert consumed["deliverable"]["summary"] == "negation proved"
    assert consumed["plan_delta"][0]["status"] == "false"
    with pytest.raises(RuntimeError, match="already consumed"):
        service.consume(spec.job_id)
    assert service.open_jobs() == []
    states = [kwargs["state"] for _args, kwargs in service.test_events]
    assert states == ["proposed", "deployed", "running", "done"]


def test_dispatch_transaction_refreshes_summary_updated_at(service, monkeypatch):
    """Every ledger mutation must refresh the summary freshness marker."""
    summary_path = service._summary_path()
    ds.atomic_json_write(
        summary_path,
        {"dispatch_ledger": [], "updated_at": "2026-07-16T00:08:18+00:00"},
        sort_keys=True,
    )
    monkeypatch.setattr(ds, "_now_iso", lambda: "2026-07-16T00:11:28+00:00")

    service.propose(_spec("run.planner.np-001"))

    summary = ds.read_json_file(summary_path)
    assert summary["updated_at"] == "2026-07-16T00:11:28+00:00"


def test_shutdown_audit_entries_hydrates_only_process_owning_rows(service, monkeypatch):
    """Shutdown skips cold history but retains every ambiguous process owner."""
    entries = [
        LedgerEntry(
            spec=_spec("run.planner.ds-open", archetype="deep_search"),
            state="running",
        ),
        LedgerEntry(
            spec=_spec("run.planner.ds-modern", archetype="deep_search"),
            state="killed",
            launch_nonce="launch-modern",
            process_id=4201,
            process_group_id=4201,
            process_session_id=4201,
            process_token_sha256="a" * 64,
            process_released_at="2026-07-19T01:00:00+00:00",
            process_release_reason="legacy-process-exited",
        ),
        LedgerEntry(
            spec=_spec("run.planner.ds-legacy", archetype="deep_search"),
            state="killed",
            process_id=4202,
        ),
        LedgerEntry(
            spec=_spec("run.planner.ds-unsafe-release", archetype="deep_search"),
            state="killed",
            process_id=4203,
            process_released_at="2026-07-19T01:00:00+00:00",
            process_release_reason="legacy-pid-reused-after-terminal",
        ),
        LedgerEntry(
            spec=_spec("run.planner.ds-released", archetype="deep_search"),
            state="killed",
            process_id=4204,
            process_released_at="2026-07-19T01:00:00+00:00",
            process_release_reason="legacy-process-exited",
        ),
        LedgerEntry(
            spec=_spec("run.planner.ds-no-process", archetype="deep_search"),
            state="killed",
        ),
        LedgerEntry(
            spec=_spec("run.planner.ds-done", archetype="deep_search"),
            state="done",
            finished_at="2026-07-19T01:00:00+00:00",
            consumed=True,
            result={"deliverable": {"summary": "archived result"}},
        ),
    ]
    for entry in entries:
        service._save_entry(entry)

    hydrated_job_ids: list[str] = []
    original_hydrate = ds.dispatch_ledger_compaction.hydrate_dispatch_record

    def track_hydration(raw, *, state_root):
        hydrated_job_ids.append(str(dict(raw.get("spec") or {}).get("job_id", "")))
        return original_hydrate(raw, state_root=state_root)

    monkeypatch.setattr(
        ds.dispatch_ledger_compaction,
        "hydrate_dispatch_record",
        track_hydration,
    )

    audit = service.shutdown_audit_entries()

    expected = [
        "run.planner.ds-open",
        "run.planner.ds-modern",
        "run.planner.ds-legacy",
        "run.planner.ds-unsafe-release",
    ]
    assert [entry.spec.job_id for entry in audit] == expected
    assert hydrated_job_ids == expected


def test_propose_rejects_duplicates_bad_roles_and_broken_lineage(service):
    spec = _spec("run.np-001")
    service.propose(spec)
    with pytest.raises(ValueError, match="already exists"):
        service.propose(spec)
    with pytest.raises(ValueError, match="may not dispatch"):
        service.propose(_spec("run.np-002", role="prover"))
    # parent_job_id must be the direct dotted parent of job_id.
    broken = JobSpec(
        job_id="run.planner.np-001",
        archetype="negation_probe",
        requester_role="planner",
        objective="x",
        budget=JobBudget(api_steps=5, wall_clock_s=60),
        deliverable="probe_verdict",
        parent_job_id="run",
    )
    with pytest.raises(ValueError, match="direct parent"):
        service.propose(broken)


def test_propose_atomically_reserves_exact_assignment_mathematical_delta(service, tmp_path):
    """Reject an open duplicate delta before a proposed ledger row is appended."""
    active_file = str(tmp_path / "Main.lean")
    base_inputs = {
        "target_symbol": "demo",
        "active_file": active_file,
        "mathematical_delta_signature": "same-mathematical-delta",
    }
    first = replace(
        _spec("run.orchestrator.ds-001", archetype="deep_search"),
        inputs=base_inputs,
    )
    duplicate = replace(
        _spec("run.orchestrator.em-002", archetype="empirical"),
        inputs=base_inputs,
    )

    service.propose(first)
    with pytest.raises(
        ds.MathematicalDeltaReservationConflict,
        match="already reserved by open job",
    ) as raised:
        service.propose(duplicate)

    assert raised.value.winning_job_id == first.job_id
    assert raised.value.delta_signature == "same-mathematical-delta"
    assert isinstance(raised.value, ValueError)

    assert [entry.spec.job_id for entry in service._load_ledger()] == [first.job_id]
    other_assignment = replace(
        duplicate,
        inputs={**base_inputs, "target_symbol": "other"},
    )
    assert service.propose(other_assignment).spec.job_id == duplicate.job_id


def test_deploy_respects_flag_and_cap(service, monkeypatch):
    spec = _spec("run.np-001")
    service.propose(spec)

    monkeypatch.delenv("LEANFLOW_DISPATCH_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="disabled"):
        service.deploy(spec.job_id)

    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    running = LedgerEntry(spec=_spec("run.np-090"), state="running")
    service._save_entry(running)
    service._cap = 1
    with pytest.raises(RuntimeError, match="cap reached"):
        service.deploy(spec.job_id)


def test_backend_failure_marks_failed_with_note(service, monkeypatch):
    monkeypatch.setattr(
        ds.DispatchService,
        "_run_delegate_job",
        lambda self, spec: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    spec = _spec("run.np-001")
    service.propose(spec)

    entry = service.deploy(spec.job_id)

    assert entry.state == "failed"
    assert "backend error" in entry.notes
    with pytest.raises(RuntimeError, match="not done"):
        service.consume(spec.job_id)


def test_async_deploy_harvests_process_result_without_blocking(service, monkeypatch):
    class _Process:
        pid = 4242

    commands: list[list[str]] = []
    launched_env: dict[str, str] = {}

    def launch(command, **kwargs):
        commands.append(command)
        launched_env.update(kwargs["env"])
        return _Process()

    monkeypatch.setattr(ds.subprocess, "Popen", launch)
    monkeypatch.setattr(ds, "_process_seems_alive", lambda pid: True)
    spec = _spec("run.planner.ds-001", archetype="deep_search")
    service.propose(spec)

    running = service.deploy_async(spec.job_id)

    assert running.state == "running"
    assert running.process_id == 4242
    assert running.process_group_id == 4242
    assert running.process_session_id == 4242
    assert running.process_token_sha256 == process_token_sha256(launched_env[PROCESS_TOKEN_ENV])
    assert commands[0][-2:] == ["--parent-pid", str(ds.os.getpid())]
    assert launched_env["LEANFLOW_DISPATCH_WORKER"] == "1"
    assert service.deploy_async(spec.job_id) == running
    assert len(commands) == 1
    result_path = service._async_result_path(spec.job_id, running.launch_nonce)
    result_path.write_text(
        json.dumps(
            {
                "launch_nonce": running.launch_nonce,
                "ok": True,
                "result": {
                    "status": "done",
                    "deliverable": {"summary": "new route"},
                    "artifact_paths": [],
                    "plan_delta": [],
                },
            }
        ),
        encoding="utf-8",
    )

    polled = service.poll(spec.job_id)

    assert polled["state"] == "done"
    assert service.consume(spec.job_id)["deliverable"]["summary"] == "new route"
    states = [kwargs["state"] for _args, kwargs in service.test_events]
    assert states == ["proposed", "deployed", "running", "done"]


def test_async_launch_admission_rejects_inside_summary_transaction(service, monkeypatch):
    """A rejected locked snapshot creates no nonce, spec, or worker process."""
    service._async_launch_admission = lambda _summary: False
    monkeypatch.setattr(
        ds.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("rejected admission must not spawn"),
    )
    spec = _spec("run.planner.ds-001", archetype="deep_search")
    service.propose(spec)

    with pytest.raises(ds.DispatchLaunchAdmissionDeferred):
        service.deploy_async(spec.job_id)

    persisted = service._entry(spec.job_id)
    assert persisted.state == "proposed"
    assert persisted.launch_nonce == ""
    assert persisted.launch_attempt == 0
    assert not service._async_spec_path(spec.job_id).exists()


def test_async_launch_binds_leading_dash_nonce_to_its_option(service, monkeypatch):
    """A URL-safe nonce that starts with a dash must remain one option value."""

    class _Process:
        pid = 4242

    commands: list[list[str]] = []
    generated_tokens = iter(("-starts-with-dash", "worker-process-token"))
    monkeypatch.setattr(ds.secrets, "token_urlsafe", lambda _size: next(generated_tokens))
    monkeypatch.setattr(
        ds.subprocess,
        "Popen",
        lambda command, **_kwargs: (commands.append(command) or _Process()),
    )
    spec = _spec("run.planner.ds-leading-dash", archetype="deep_search")
    service.propose(spec)

    running = service.deploy_async(spec.job_id)

    assert running.state == "running"
    assert running.launch_nonce == "-starts-with-dash"
    assert "--launch-nonce=-starts-with-dash" in commands[0]
    assert "--launch-nonce" not in commands[0]


def test_async_launch_crash_before_spec_stays_durable_and_counts_capacity(service, monkeypatch):
    """The deployed reservation must precede every launch artifact write."""

    class SimulatedCrash(BaseException):
        pass

    first = _spec("run.planner.ds-launching", archetype="deep_search")
    second = _spec("run.planner.ds-blocked", archetype="deep_search")
    service._cap = 1
    service.propose(first)
    service.propose(second)
    monkeypatch.setattr(
        service,
        "_write_async_launch_spec",
        lambda _entry: (_ for _ in ()).throw(SimulatedCrash()),
    )

    with pytest.raises(SimulatedCrash):
        service.deploy_async(first.job_id)

    launching = service._entry(first.job_id)
    spec_path = service._async_spec_path(first.job_id, launching.launch_nonce)
    assert launching.state == "deployed"
    assert launching.launch_nonce
    assert launching.launch_attempt == 1
    assert launching.launch_started_at
    assert launching.started_at == ""
    assert launching.process_id == 0
    assert not spec_path.exists()
    assert service.deploy_async(first.job_id) == launching
    assert service._entry(first.job_id).launch_attempt == 1
    with pytest.raises(RuntimeError, match="cap reached"):
        service.deploy_async(second.job_id)


def test_async_launch_crash_after_spec_is_resumable(service, monkeypatch):
    """A complete nonce-bound spec without an identity remains launch-in-progress."""

    class SimulatedCrash(BaseException):
        pass

    spec = _spec("run.planner.ds-spec-only", archetype="deep_search")
    service.propose(spec)
    monkeypatch.setattr(
        service,
        "_spawn_async_worker",
        lambda _entry: (_ for _ in ()).throw(SimulatedCrash()),
    )

    with pytest.raises(SimulatedCrash):
        service.deploy_async(spec.job_id)

    launching = service._entry(spec.job_id)
    first_nonce = launching.launch_nonce
    spec_path = service._async_spec_path(spec.job_id, launching.launch_nonce)
    envelope = ds.read_json_file(spec_path)
    assert launching.state == "deployed"
    assert envelope["launch_nonce"] == launching.launch_nonce
    assert envelope["spec"]["job_id"] == spec.job_id
    assert launching.process_id == 0

    class Process:
        pid = 4343

    launches: list[int] = []
    monkeypatch.setattr(ds, "ASYNC_LAUNCH_HANDSHAKE_GRACE_S", 0.0)
    monkeypatch.setattr(
        ds.subprocess,
        "Popen",
        lambda *args, **kwargs: (launches.append(1) or Process()),
    )
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)

    resumed = ds.DispatchService(parent_agent=object(), root_job_id="run")
    recovered = resumed.reconcile()

    running = next(entry for entry in recovered if entry.spec.job_id == spec.job_id)
    assert running.state == "running"
    assert running.launch_nonce != first_nonce
    assert running.launch_attempt == 2
    assert running.process_id == 4343
    assert launches == [1]


def test_async_launch_crash_after_popen_is_adopted_without_duplicate(service, monkeypatch):
    """The exact identity receipt closes the Popen-before-ledger-commit gap."""

    class SimulatedCrash(BaseException):
        pass

    class Process:
        pid = 4242

    launches: list[list[str]] = []

    def popen(command, **kwargs):
        launches.append(command)
        return Process()

    spec = _spec("run.planner.ds-adopt", archetype="deep_search")
    service.propose(spec)
    monkeypatch.setattr(ds.subprocess, "Popen", popen)
    monkeypatch.setattr(
        service,
        "_commit_async_running",
        lambda _entry, _identity: (_ for _ in ()).throw(SimulatedCrash()),
    )

    with pytest.raises(SimulatedCrash):
        service.deploy_async(spec.job_id)

    launching = service._entry(spec.job_id)
    identity_payload = ds.read_json_file(
        service._async_identity_path(spec.job_id, launching.launch_nonce)
    )
    assert launching.state == "deployed"
    assert identity_payload["launch_nonce"] == launching.launch_nonce
    assert identity_payload["process_id"] == 4242

    resumed = ds.DispatchService(parent_agent=object(), root_job_id="run")
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)
    reconciled = resumed.reconcile()

    adopted = next(entry for entry in reconciled if entry.spec.job_id == spec.job_id)
    assert adopted.state == "running"
    assert adopted.process_id == 4242
    assert len(launches) == 1


def test_async_launch_crash_after_popen_harvests_completed_nonce_result(service, monkeypatch):
    """A worker result that beat the running commit survives parent recovery."""

    class SimulatedCrash(BaseException):
        pass

    class Process:
        pid = 4242

    launches: list[int] = []
    spec = _spec("run.planner.ds-precommit-result", archetype="deep_search")
    service.propose(spec)
    monkeypatch.setattr(
        ds.subprocess,
        "Popen",
        lambda *args, **kwargs: (launches.append(1) or Process()),
    )
    monkeypatch.setattr(
        service,
        "_commit_async_running",
        lambda _entry, _identity: (_ for _ in ()).throw(SimulatedCrash()),
    )
    with pytest.raises(SimulatedCrash):
        service.deploy_async(spec.job_id)

    launching = service._entry(spec.job_id)
    ds.atomic_json_write(
        service._async_result_path(spec.job_id, launching.launch_nonce),
        {
            "launch_nonce": launching.launch_nonce,
            "ok": True,
            "result": {"status": "done", "deliverable": {"summary": "finished early"}},
        },
        sort_keys=True,
    )
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: False)
    resumed = ds.DispatchService(parent_agent=object(), root_job_id="run")

    recovered = resumed.reconcile()

    done = next(entry for entry in recovered if entry.spec.job_id == spec.job_id)
    assert done.state == "done"
    assert resumed.consume(spec.job_id)["deliverable"]["summary"] == "finished early"
    assert launches == [1]


def test_async_launch_resume_retries_dead_popen_receipt_without_false_running(service, monkeypatch):
    """A dead pre-commit child is retried directly instead of adopted as running."""

    class SimulatedCrash(BaseException):
        pass

    class Process:
        def __init__(self, pid):
            self.pid = pid

    launch_pids = [4242, 4343]
    launches: list[int] = []

    def popen(*args, **kwargs):
        pid = launch_pids.pop(0)
        launches.append(pid)
        return Process(pid)

    spec = _spec("run.planner.ds-dead-precommit", archetype="deep_search")
    service.propose(spec)
    monkeypatch.setattr(ds.subprocess, "Popen", popen)
    monkeypatch.setattr(
        service,
        "_commit_async_running",
        lambda _entry, _identity: (_ for _ in ()).throw(SimulatedCrash()),
    )
    with pytest.raises(SimulatedCrash):
        service.deploy_async(spec.job_id)

    monkeypatch.setattr(
        ds,
        "_dispatch_process_identity_is_live",
        lambda entry: entry.process_id == 4343,
    )
    resumed = ds.DispatchService(parent_agent=object(), root_job_id="run")

    recovered = resumed.reconcile()

    running = next(entry for entry in recovered if entry.spec.job_id == spec.job_id)
    assert running.state == "running"
    assert running.launch_attempt == 2
    assert running.process_id == 4343
    assert launches == [4242, 4343]


def test_async_launch_new_parent_replaces_live_worker_bound_to_crashed_parent(service, monkeypatch):
    """A new parent retries without letting delayed old artifacts clobber it."""

    class SimulatedCrash(BaseException):
        pass

    class Process:
        def __init__(self, pid):
            self.pid = pid

    original_parent_pid = ds.os.getpid()
    launch_pids = [4242, 4343]
    launches: list[int] = []
    replacement_order: list[str] = []

    def popen(*args, **kwargs):
        pid = launch_pids.pop(0)
        launches.append(pid)
        if pid == 4343:
            replacement_order.append("spawn-replacement")
        return Process(pid)

    spec = _spec("run.planner.ds-restart-parent", archetype="deep_search")
    service.propose(spec)
    monkeypatch.setattr(ds.subprocess, "Popen", popen)
    monkeypatch.setattr(
        service,
        "_commit_async_running",
        lambda _entry, _identity: (_ for _ in ()).throw(SimulatedCrash()),
    )
    with pytest.raises(SimulatedCrash):
        service.deploy_async(spec.job_id)
    first = service._entry(spec.job_id)
    old_identity_path = service._async_identity_path(spec.job_id, first.launch_nonce)
    old_result_path = service._async_result_path(spec.job_id, first.launch_nonce)

    terminated: list[int] = []
    monkeypatch.setattr(ds.os, "getpid", lambda: original_parent_pid + 100)
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)
    monkeypatch.setattr(
        ds,
        "_terminate_dispatch_process_and_wait",
        lambda entry: (
            terminated.append(entry.process_id)
            or replacement_order.append("retired-old-worker")
            or True
        ),
    )
    resumed = ds.DispatchService(parent_agent=object(), root_job_id="run")

    recovered = resumed.reconcile()

    running = next(entry for entry in recovered if entry.spec.job_id == spec.job_id)
    assert running.state == "running"
    assert running.launch_attempt == 2
    assert running.process_id == 4343
    assert terminated == [4242]
    assert launches == [4242, 4343]
    assert replacement_order == ["retired-old-worker", "spawn-replacement"]

    current_identity_path = resumed._async_identity_path(spec.job_id, running.launch_nonce)
    current_result_path = resumed._async_result_path(spec.job_id, running.launch_nonce)
    assert current_identity_path != old_identity_path
    assert current_result_path != old_result_path
    assert first.launch_nonce not in old_identity_path.name
    assert running.launch_nonce not in current_result_path.name
    ds.atomic_json_write(
        current_result_path,
        {
            "launch_nonce": running.launch_nonce,
            "ok": True,
            "result": {"status": "done", "deliverable": {"summary": "current result"}},
        },
        sort_keys=True,
    )

    # The old child publishes after the replacement has already completed.
    # Its nonce-derived paths are disjoint from the authoritative artifacts.
    ds.atomic_json_write(
        old_identity_path,
        {
            "launch_nonce": first.launch_nonce,
            "process_id": 9999,
            "process_group_id": 9999,
            "process_session_id": 9999,
            "process_token_sha256": "f" * 64,
            "parent_process_id": original_parent_pid,
        },
        sort_keys=True,
    )
    ds.atomic_json_write(
        old_result_path,
        {
            "launch_nonce": first.launch_nonce,
            "ok": True,
            "result": {"status": "done", "deliverable": {"summary": "stale result"}},
        },
        sort_keys=True,
    )

    assert ds.read_json_file(current_identity_path)["process_id"] == 4343
    assert ds.read_json_file(current_result_path)["launch_nonce"] == running.launch_nonce
    assert resumed.poll(spec.job_id)["state"] == "done"
    assert resumed.consume(spec.job_id)["deliverable"]["summary"] == "current result"
    assert launches == [4242, 4343]


@pytest.mark.parametrize("receipt_parent_kind", ["missing", "different"])
def test_cross_parent_recovery_does_not_replace_unretired_exact_worker(
    service, monkeypatch, receipt_parent_kind
):
    """Missing or different parent ownership requires exact worker retirement."""

    receipt_parent_pid = 0 if receipt_parent_kind == "missing" else os.getpid() + 100
    spec = _spec("run.planner.ds-unretired-parent", archetype="deep_search")
    service.propose(spec)
    launching, reserved = service._reserve_async_launch(spec.job_id)
    assert reserved is True
    service._write_async_launch_spec(launching)
    ds.atomic_json_write(
        service._async_identity_path(spec.job_id, launching.launch_nonce),
        {
            "launch_nonce": launching.launch_nonce,
            "process_id": 4242,
            "process_group_id": 4242,
            "process_session_id": 4242,
            "process_token_sha256": "f" * 64,
            "parent_process_id": receipt_parent_pid,
        },
        sort_keys=True,
    )
    retire_calls: list[int] = []

    def cannot_retire(entry):
        retire_calls.append(entry.process_id)
        return False

    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)
    monkeypatch.setattr(
        ds,
        "_terminate_dispatch_process_and_wait",
        cannot_retire,
    )
    monkeypatch.setattr(
        service,
        "_reserve_async_launch_retry",
        lambda _entry: pytest.fail("replacement nonce rotated before exact process exit"),
    )
    monkeypatch.setattr(
        ds.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("replacement spawned before exact process exit"),
    )

    recovered = service.reconcile()

    entry = next(item for item in recovered if item.spec.job_id == spec.job_id)
    assert entry.state == "deployed"
    assert entry.launch_nonce == launching.launch_nonce
    assert retire_calls == [4242]


def test_stale_launch_exit_probe_fails_closed_on_lookup_errors(monkeypatch):
    """Only ESRCH, never EPERM or transient lookup failure, proves worker exit."""

    entry = LedgerEntry(
        spec=_spec("run.planner.ds-exit-probe", archetype="deep_search"),
        state="running",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="f" * 64,
    )

    monkeypatch.setattr(
        ds.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(PermissionError("denied")),
    )
    assert not ds._dispatch_process_identity_has_exited(entry)

    monkeypatch.setattr(
        ds.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(OSError(5, "transient I/O failure")),
    )
    assert not ds._dispatch_process_identity_has_exited(entry)

    monkeypatch.setattr(
        ds.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError("gone")),
    )
    assert ds._dispatch_process_identity_has_exited(entry)


@pytest.mark.skipif(os.name != "posix", reason="dispatch process groups require POSIX")
def test_stale_launch_termination_escalates_and_waits_for_exact_process_exit(
    monkeypatch,
):
    """A TERM-resistant stale worker is dead before retirement returns success."""

    token = "dispatch-stale-worker-token"
    env = dict(os.environ)
    env[PROCESS_TOKEN_ENV] = token
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(30)"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-real-stale-worker", archetype="deep_search"),
        state="running",
        process_id=process.pid,
        process_group_id=os.getpgid(process.pid),
        process_session_id=os.getsid(process.pid),
        process_token_sha256=process_token_sha256(token),
    )
    monkeypatch.setattr(ds, "ASYNC_LAUNCH_TERMINATION_GRACE_S", 0.1)
    monkeypatch.setattr(ds, "ASYNC_LAUNCH_TERMINATION_POLL_S", 0.01)
    # Sandboxed macOS may deny the token's ``ps eww`` lookup. Keep the real
    # PID/session exit boundary while making ownership validation deterministic.
    monkeypatch.setattr(
        ds,
        "process_identity_matches",
        lambda identity: identity.pid == process.pid
        and not ds._dispatch_process_identity_has_exited(entry),
    )

    try:
        assert ds._dispatch_process_identity_is_live(entry)
        assert ds._terminate_dispatch_process_and_wait(entry)
        assert not ds._dispatch_process_identity_is_live(entry)
    finally:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=5)


def test_async_launch_resume_retries_once_and_suppresses_duplicate_spawn(service, monkeypatch):
    """A nonce CAS lets one restart retry an unstarted launch exactly once."""

    class SimulatedCrash(BaseException):
        pass

    class Process:
        pid = 5252

    spec = _spec("run.planner.ds-retry", archetype="deep_search")
    service.propose(spec)
    original_write = service._write_async_launch_spec
    monkeypatch.setattr(
        service,
        "_write_async_launch_spec",
        lambda _entry: (_ for _ in ()).throw(SimulatedCrash()),
    )
    with pytest.raises(SimulatedCrash):
        service.deploy_async(spec.job_id)
    first_nonce = service._entry(spec.job_id).launch_nonce

    launches: list[int] = []
    monkeypatch.setattr(service, "_write_async_launch_spec", original_write)
    monkeypatch.setattr(ds, "ASYNC_LAUNCH_HANDSHAKE_GRACE_S", 0.0)
    monkeypatch.setattr(
        ds.subprocess,
        "Popen",
        lambda *args, **kwargs: (launches.append(1) or Process()),
    )
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)

    first_reconcile = service.reconcile()
    second_reconcile = service.reconcile()

    running = next(entry for entry in first_reconcile if entry.spec.job_id == spec.job_id)
    assert running.state == "running"
    assert running.launch_nonce != first_nonce
    assert running.launch_attempt == 2
    assert next(entry for entry in second_reconcile if entry.spec.job_id == spec.job_id).state == (
        "running"
    )
    assert launches == [1]


def test_stale_launcher_cannot_write_or_spawn_after_recovery_rotates_nonce(service, monkeypatch):
    """A delayed launcher loses under the per-job lock and ledger nonce fence."""

    class Process:
        pid = 5454

    spec = _spec("run.planner.ds-stale-launcher", archetype="deep_search")
    service.propose(spec)
    stale_entry, reserved = service._reserve_async_launch(spec.job_id)
    assert reserved is True
    assert not service._async_spec_path(spec.job_id).exists()

    launches: list[int] = []
    monkeypatch.setattr(ds, "ASYNC_LAUNCH_HANDSHAKE_GRACE_S", 0.0)
    monkeypatch.setattr(
        ds.subprocess,
        "Popen",
        lambda *args, **kwargs: (launches.append(1) or Process()),
    )
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)

    recovered = service.reconcile()
    running = next(entry for entry in recovered if entry.spec.job_id == spec.job_id)
    authoritative_spec = ds.read_json_file(service._async_spec_path(spec.job_id))
    assert running.state == "running"
    assert running.launch_nonce != stale_entry.launch_nonce
    assert authoritative_spec["launch_nonce"] == running.launch_nonce
    assert service._async_launch_lock_path(spec.job_id).exists()

    monkeypatch.setattr(
        service,
        "_write_async_launch_spec",
        lambda _entry: pytest.fail("stale launcher overwrote the authoritative spec"),
    )
    resumed_stale = service._launch_reserved_async(stale_entry)

    assert resumed_stale == running
    assert ds.read_json_file(service._async_spec_path(spec.job_id)) == authoritative_spec
    assert launches == [1]


def test_nonce_rotation_publishes_spec_fence_before_launcher_crash(service, monkeypatch):
    """Ledger rotation cannot become durable while the shared spec stays stale."""

    class SimulatedCrash(BaseException):
        pass

    spec = _spec("run.planner.ds-rotation-fence", archetype="deep_search")
    service.propose(spec)
    stale_entry, reserved = service._reserve_async_launch(spec.job_id)
    assert reserved is True
    service._publish_async_spec_fence(stale_entry)
    monkeypatch.setattr(ds, "ASYNC_LAUNCH_HANDSHAKE_GRACE_S", 0.0)
    monkeypatch.setattr(
        service,
        "_write_async_launch_spec",
        lambda _entry: (_ for _ in ()).throw(SimulatedCrash()),
    )

    with pytest.raises(SimulatedCrash):
        service.reconcile()

    retried = service._entry(spec.job_id)
    shared_spec = ds.read_json_file(service._async_spec_path(spec.job_id))
    assert retried.state == "deployed"
    assert retried.launch_attempt == 2
    assert retried.launch_nonce != stale_entry.launch_nonce
    assert shared_spec["launch_nonce"] == retried.launch_nonce
    assert shared_spec["spec"]["job_id"] == spec.job_id


def test_per_job_launch_lock_blocks_reconcile_until_running_commit(service, monkeypatch):
    """Reconciliation cannot rotate a nonce while its launcher owns the sidecar."""

    class Process:
        pid = 5555

    spec = _spec("run.planner.ds-launch-lock", archetype="deep_search")
    service.propose(spec)
    original_write = service._write_async_launch_spec
    write_entered = threading.Event()
    allow_write = threading.Event()
    reconcile_done = threading.Event()
    errors: list[BaseException] = []
    launches: list[int] = []

    def blocked_write(entry):
        write_entered.set()
        if not allow_write.wait(timeout=2):
            raise AssertionError("test did not release the launch transaction")
        original_write(entry)

    def run(callable_):
        try:
            callable_()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(service, "_write_async_launch_spec", blocked_write)
    monkeypatch.setattr(
        ds.subprocess,
        "Popen",
        lambda *args, **kwargs: (launches.append(1) or Process()),
    )
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)
    launcher = threading.Thread(target=lambda: run(lambda: service.deploy_async(spec.job_id)))
    reconciler = threading.Thread(
        target=lambda: run(lambda: (service.reconcile(), reconcile_done.set()))
    )

    launcher.start()
    assert write_entered.wait(timeout=2)
    reconciler.start()
    assert not reconcile_done.wait(timeout=0.05)
    allow_write.set()
    launcher.join(timeout=2)
    reconciler.join(timeout=2)

    assert not launcher.is_alive()
    assert not reconciler.is_alive()
    assert errors == []
    assert service._entry(spec.job_id).state == "running"
    assert launches == [1]


@pytest.mark.skipif(ds.fcntl is None, reason="POSIX flock unavailable")
def test_launch_sidecar_excludes_a_second_process(service, tmp_path):
    """The launch sidecar is process-scoped, not only a thread mutex."""
    job_id = "run.planner.ds-cross-process-lock"
    lock_path = service._async_launch_lock_path(job_id)
    marker = tmp_path / "child-acquired"
    script = """
import fcntl
import pathlib
import sys

lock_path = pathlib.Path(sys.argv[1])
marker = pathlib.Path(sys.argv[2])
with lock_path.open("a+b") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    marker.write_text("acquired", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
"""

    with service._async_launch_lock(job_id):
        child = ds.subprocess.Popen(
            [ds.sys.executable, "-c", script, str(lock_path), str(marker)],
            stdout=ds.subprocess.DEVNULL,
            stderr=ds.subprocess.DEVNULL,
        )
        with pytest.raises(ds.subprocess.TimeoutExpired):
            child.wait(timeout=0.1)
        assert not marker.exists()

    assert child.wait(timeout=2) == 0
    assert marker.read_text(encoding="utf-8") == "acquired"


def test_async_harvest_rejects_stale_launch_nonce_then_accepts_current(service, monkeypatch):
    class Process:
        pid = 6262

    monkeypatch.setattr(ds.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)
    spec = _spec("run.planner.ds-stale-result", archetype="deep_search")
    service.propose(spec)
    running = service.deploy_async(spec.job_id)
    result_path = service._async_result_path(spec.job_id, running.launch_nonce)
    result_path.write_text(
        json.dumps(
            {
                "launch_nonce": "stale-launch",
                "ok": True,
                "result": {"status": "done", "deliverable": {"summary": "stale"}},
            }
        ),
        encoding="utf-8",
    )

    assert service.poll(spec.job_id)["state"] == "running"
    assert service._entry(spec.job_id).result == {}

    result_path.write_text(
        json.dumps(
            {
                "launch_nonce": running.launch_nonce,
                "ok": True,
                "result": {"status": "done", "deliverable": {"summary": "current"}},
            }
        ),
        encoding="utf-8",
    )

    assert service.poll(spec.job_id)["state"] == "done"
    assert service.consume(spec.job_id)["deliverable"]["summary"] == "current"


def test_async_harvest_reaps_finished_child(service, monkeypatch):
    class _Process:
        pid = 4242

    reaped: list[tuple[int, bool]] = []
    monkeypatch.setattr(ds.subprocess, "Popen", lambda *args, **kwargs: _Process())
    monkeypatch.setattr(ds, "_process_seems_alive", lambda _pid: True)
    monkeypatch.setattr(
        ds,
        "_reap_process",
        lambda pid, *, block: reaped.append((pid, block)) or True,
    )
    spec = _spec("run.planner.ds-001", archetype="deep_search")
    service.propose(spec)
    running = service.deploy_async(spec.job_id)
    result_path = service._async_result_path(spec.job_id, running.launch_nonce)
    result_path.write_text(
        json.dumps(
            {
                "launch_nonce": running.launch_nonce,
                "ok": True,
                "result": {"status": "done", "deliverable": {"summary": "new route"}},
            }
        ),
        encoding="utf-8",
    )

    service.poll(spec.job_id)

    assert reaped == [(4242, True)]


def test_async_harvest_normalizes_interrupted_result_status(service, monkeypatch):
    """An interrupted worker artifact is cancellation evidence, not failure."""

    class _Process:
        pid = 4242

    monkeypatch.setattr(ds.subprocess, "Popen", lambda *args, **kwargs: _Process())
    monkeypatch.setattr(ds, "_process_seems_alive", lambda _pid: True)
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: True)
    spec = _spec("run.planner.ds-001", archetype="deep_search")
    service.propose(spec)
    running = service.deploy_async(spec.job_id)
    result_path = service._async_result_path(spec.job_id, running.launch_nonce)
    result_path.write_text(
        json.dumps(
            {
                "launch_nonce": running.launch_nonce,
                "ok": True,
                "result": {"status": "interrupted", "deliverable": {"summary": ""}},
            }
        ),
        encoding="utf-8",
    )

    service.poll(spec.job_id)

    entry = service._entry(spec.job_id)
    assert entry.state == "killed"
    assert entry.notes == "worker interrupted: result status interrupted"


def test_async_harvest_normalizes_keyboard_interrupt_artifact(service, monkeypatch):
    """A signal-raised KeyboardInterrupt artifact remains operational cancellation."""

    class _Process:
        pid = 4242

    monkeypatch.setattr(ds.subprocess, "Popen", lambda *args, **kwargs: _Process())
    monkeypatch.setattr(ds, "_process_seems_alive", lambda _pid: True)
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: True)
    spec = _spec("run.planner.ds-001", archetype="deep_search")
    service.propose(spec)
    running = service.deploy_async(spec.job_id)
    result_path = service._async_result_path(spec.job_id, running.launch_nonce)
    result_path.write_text(
        json.dumps(
            {
                "launch_nonce": running.launch_nonce,
                "ok": False,
                "error": "KeyboardInterrupt: ",
            }
        ),
        encoding="utf-8",
    )

    service.poll(spec.job_id)

    entry = service._entry(spec.job_id)
    assert entry.state == "killed"
    assert entry.notes == "worker interrupted: KeyboardInterrupt"


def test_kill_rights_are_ancestor_gated(service):
    spec = _spec("run.planner.np-001")
    service.propose(spec)

    with pytest.raises(PermissionError):
        service.kill(spec.job_id, requester_job_id="run.orchestrator.ds-001")

    outcome = service.kill(spec.job_id, requester_job_id="run.planner")
    assert outcome["killed"] is True
    assert service._entry(spec.job_id).state == "killed"

    other = _spec("run.planner.np-002")
    service.propose(other)
    assert service.kill(other.job_id, requester_job_id="human")["killed"] is True


def test_kill_confirms_process_isolated_worker_exit(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-001", archetype="deep_search"),
        state="running",
        process_id=4242,
    )
    service._save_entry(entry)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    retired: list[int] = []
    monkeypatch.setattr(
        ds,
        "_terminate_dispatch_process_and_wait",
        lambda current: retired.append(current.process_id) or True,
    )

    outcome = service.kill(entry.spec.job_id, requester_job_id="run")

    assert outcome["process_terminated"] is True
    assert outcome["process_reaped"] is True
    assert outcome["process_exit_confirmed"] is True
    assert retired == [4242]
    assert outcome["state"] == "killed"
    assert service._entry(entry.spec.job_id).state == "killed"


def test_kill_refuses_reused_dispatch_process_identity(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-001", archetype="deep_search"),
        state="running",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
    )
    service._save_entry(entry)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    monkeypatch.setattr(ds, "process_identity_matches", lambda _identity: False)

    def unexpected_signal(_process_id: int) -> bool:
        raise AssertionError("mismatched dispatch identity must not be signaled")

    monkeypatch.setattr(ds, "_terminate_process_group", unexpected_signal)

    outcome = service.kill(entry.spec.job_id, requester_job_id="run")

    assert outcome["killed"] is False
    assert outcome["state"] == "running"
    assert outcome["process_terminated"] is False
    assert outcome["process_identity_verified"] is False
    assert outcome["process_exit_confirmed"] is False
    assert service._entry(entry.spec.job_id).state == "running"


def test_kill_refuses_legacy_dispatch_pid_without_identity(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-legacy", archetype="deep_search"),
        state="running",
        process_id=4242,
    )
    service._save_entry(entry)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)

    def unexpected_signal(_process_id: int) -> bool:
        raise AssertionError("legacy PID-only ledger entries must not be signaled")

    monkeypatch.setattr(ds, "_terminate_process_group", unexpected_signal)

    outcome = service.kill(entry.spec.job_id, requester_job_id="run")

    assert outcome["killed"] is False
    assert outcome["state"] == "running"
    assert outcome["process_terminated"] is False
    assert outcome["process_identity_verified"] is False
    assert outcome["process_exit_confirmed"] is False
    assert service._entry(entry.spec.job_id).state == "running"


def test_matching_legacy_worker_stays_blocked_despite_misleading_lstart(service, monkeypatch):
    """A backward-clock or DST-style lstart cannot authorize capacity release."""
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-legacy-matching", archetype="deep_search"),
        state="killed",
        process_id=4242,
        finished_at="2026-07-15T08:11:37+00:00",
    )
    service._save_entry(entry)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    expected_spec = ds._dispatch_job_spec_path(entry.spec.job_id)
    monkeypatch.setattr(
        ds,
        "_read_process_argv",
        lambda _process_id, **_kwargs: (
            sys.executable,
            "-m",
            ds.DISPATCH_WORKER_MODULE,
            "--spec-file",
            expected_spec,
        ),
    )
    monkeypatch.setattr(
        ds,
        "_process_started_at_utc",
        # Deliberately later than the terminal timestamp: this was the unsafe
        # historical signal and can be misleading after a clock transition.
        lambda _process_id: datetime(2026, 7, 15, 9, 11, 39, tzinfo=UTC),
    )

    outcome = service.release_legacy_killed_process_capacity(entry)

    assert outcome == {"released": False, "newly_released": False, "reason": ""}
    persisted = service._entry(entry.spec.job_id)
    assert persisted.process_released_at == ""
    assert persisted.process_release_reason == ""


def test_unrelated_reused_legacy_pid_releases_once_and_persists(service, monkeypatch):
    """An exact unrelated argv mismatch leaves durable release evidence."""
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-legacy-reused", archetype="deep_search"),
        state="killed",
        process_id=4242,
        finished_at="2026-07-15T08:11:37+00:00",
    )
    service._save_entry(entry)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    monkeypatch.setattr(
        ds,
        "_read_process_argv",
        lambda _process_id, **_kwargs: ("/usr/bin/sleep", "300"),
    )
    monkeypatch.setattr(ds, "_process_started_at_utc", lambda _process_id: None)

    first = service.release_legacy_killed_process_capacity(entry)

    assert first["released"] is True
    assert first["newly_released"] is True
    assert first["reason"] == "legacy-process-command-mismatch"
    assert first["report_key"].startswith("research-portfolio-capacity-released:")
    persisted = service._entry(entry.spec.job_id)
    assert persisted.process_released_at
    assert persisted.process_release_reason == "legacy-process-command-mismatch"
    assert persisted.process_release_evidence_sha256
    assert persisted.process_release_report_key == first["report_key"]
    assert persisted.process_id == 4242

    # A restart must trust the durable tombstone even when argv can no longer
    # be observed; this is what prevents the ghost slot recurring.
    monkeypatch.setattr(ds, "_read_process_argv", lambda *_args, **_kwargs: None)
    restarted = ds.DispatchService(parent_agent=object(), root_job_id="run")
    second = restarted.release_legacy_killed_process_capacity(restarted._entry(entry.spec.job_id))

    assert second["released"] is True
    assert second["newly_released"] is False
    assert second["reason"] == "legacy-process-command-mismatch"
    assert second["released_at"] == persisted.process_released_at
    assert second["report_key"] == first["report_key"]


def test_reused_modern_identity_releases_only_after_live_identity_mismatch(service, monkeypatch):
    """A token mismatch plus unrelated argv proves a reused modern PID."""
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-modern-killed", archetype="deep_search"),
        state="killed",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
        finished_at="2026-07-15T08:11:37+00:00",
    )
    service._save_entry(entry)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: False)
    monkeypatch.setattr(
        ds,
        "_read_process_argv",
        lambda _process_id, **_kwargs: ("/usr/bin/sleep", "300"),
    )

    outcome = service.release_legacy_killed_process_capacity(entry)

    assert outcome["released"] is True
    assert outcome["newly_released"] is True
    assert outcome["reason"] == "process-command-mismatch"
    persisted = service._entry(entry.spec.job_id)
    assert persisted.process_released_at
    assert persisted.process_release_reason == "process-command-mismatch"


@pytest.mark.parametrize(
    "prefix",
    [
        (sys.executable, "-u"),
        ("/Applications/Python", "3.12/bin/python"),
    ],
    ids=["python-flag", "darwin-split-executable"],
)
def test_prefixed_matching_dispatch_worker_argv_stays_blocked(service, monkeypatch, prefix):
    """Prefix tokens cannot hide the matching worker module/spec pair."""
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-prefixed-worker", archetype="deep_search"),
        state="killed",
        process_id=4242,
        finished_at="2026-07-15T08:11:37+00:00",
    )
    service._save_entry(entry)
    expected_spec = ds._dispatch_job_spec_path(entry.spec.job_id)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    monkeypatch.setattr(
        ds,
        "_read_process_argv",
        lambda _process_id, **_kwargs: (
            *prefix,
            "-m",
            ds.DISPATCH_WORKER_MODULE,
            "--spec-file",
            expected_spec,
        ),
    )

    outcome = service.release_legacy_killed_process_capacity(entry)

    assert outcome == {"released": False, "newly_released": False, "reason": ""}


def test_unsafe_wall_clock_tombstone_is_revoked_until_command_proves_release(service, monkeypatch):
    """Historical lstart-only tombstones never bypass the new command gate."""
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-unsafe-tombstone", archetype="deep_search"),
        state="killed",
        process_id=4242,
        finished_at="2026-07-15T08:11:37+00:00",
        process_released_at="2026-07-15T08:11:39+00:00",
        process_release_reason="legacy-pid-reused-after-terminal",
    )
    service._save_entry(entry)
    expected_spec = ds._dispatch_job_spec_path(entry.spec.job_id)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    monkeypatch.setattr(
        ds,
        "_read_process_argv",
        lambda _process_id, **_kwargs: (
            sys.executable,
            "-m",
            ds.DISPATCH_WORKER_MODULE,
            "--spec-file",
            expected_spec,
        ),
    )

    outcome = service.release_legacy_killed_process_capacity(entry)

    assert outcome == {"released": False, "newly_released": False, "reason": ""}
    persisted = service._entry(entry.spec.job_id)
    assert persisted.process_released_at == ""
    assert persisted.process_release_reason == ""


def test_linux_process_argv_requires_complete_nul_delimited_payload(tmp_path):
    """Linux cmdline parsing rejects truncation and preserves exact boundaries."""
    proc_root = tmp_path / "proc"
    cmdline = proc_root / "4242" / "cmdline"
    cmdline.parent.mkdir(parents=True)
    cmdline.write_bytes(b"/venv/bin/python\0-m\0leanflow_cli.native.dispatch_worker\0")

    assert ds._read_linux_process_argv(4242, proc_root=proc_root) == (
        "/venv/bin/python",
        "-m",
        ds.DISPATCH_WORKER_MODULE,
    )

    cmdline.write_bytes(b"/venv/bin/python\0-m\0leanflow_cli.native.dispatch_worker")
    assert ds._read_linux_process_argv(4242, proc_root=proc_root) is None


def test_darwin_process_argv_uses_full_width_and_rejects_ambiguous_output(monkeypatch):
    """Darwin argv lookup requests unlimited width and fails closed on bad quoting."""
    calls: list[list[str]] = []

    def completed(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "/venv/bin/python -u -m leanflow_cli.native.dispatch_worker "
                "--spec-file /tmp/job.spec.json\n"
            ),
        )

    monkeypatch.setattr(ds.subprocess, "run", completed)

    assert ds._read_darwin_process_argv(4242) == (
        "/venv/bin/python",
        "-u",
        "-m",
        ds.DISPATCH_WORKER_MODULE,
        "--spec-file",
        "/tmp/job.spec.json",
    )
    assert calls == [["ps", "-ww", "-p", "4242", "-o", "command="]]

    monkeypatch.setattr(
        ds.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="python -m 'unterminated\n",
        ),
    )
    assert ds._read_darwin_process_argv(4242) is None


def test_kill_does_not_misclassify_requested_termination_artifact(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-001", archetype="deep_search"),
        state="running",
        process_id=4242,
    )
    service._save_entry(entry)
    _spec_path, result_path, _log_path = service._async_paths(entry.spec.job_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ds, "_process_seems_alive", lambda _pid: True)

    def terminate_with_worker_artifact(_pid):
        result_path.write_text(
            json.dumps(
                {
                    "ok": False,
                    "error": "NativeTerminationSignal: native process received SIGTERM",
                }
            ),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)
    monkeypatch.setattr(
        ds,
        "_terminate_dispatch_process_and_wait",
        lambda entry: terminate_with_worker_artifact(entry.process_id),
    )

    outcome = service.kill(entry.spec.job_id, requester_job_id="run")

    assert outcome["killed"] is True
    assert outcome["state"] == "killed"
    persisted = service._entry(entry.spec.job_id)
    assert persisted.state == "killed"
    assert persisted.notes == "killed by run"
    assert persisted.result == {}


def test_kill_ignores_prepublished_keyboard_interrupt_artifact(service, monkeypatch):
    """Explicit shutdown owns a concurrent interruption artifact's verdict."""
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-001", archetype="deep_search"),
        state="running",
        process_id=4242,
    )
    service._save_entry(entry)
    _spec_path, result_path, _log_path = service._async_paths(entry.spec.job_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        '{"ok": false, "error": "KeyboardInterrupt: "}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: True)

    outcome = service.kill(entry.spec.job_id, requester_job_id="run")

    assert outcome["killed"] is True
    assert outcome["state"] == "killed"
    persisted = service._entry(entry.spec.job_id)
    assert persisted.state == "killed"
    assert persisted.notes == "killed by run"
    assert persisted.result == {}


def test_kill_harvests_completed_process_result_before_termination(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-001", archetype="deep_search"),
        state="running",
        process_id=4242,
    )
    service._save_entry(entry)
    _spec_path, result_path, _log_path = service._async_paths(entry.spec.job_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        '{"ok": true, "result": {"status": "done", '
        '"deliverable": {"summary": "finished before shutdown"}}}',
        encoding="utf-8",
    )
    terminated: list[int] = []
    monkeypatch.setattr(ds, "_process_seems_alive", lambda _pid: True)
    monkeypatch.setattr(
        ds,
        "_terminate_process_group",
        lambda pid: terminated.append(pid) or True,
    )
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: True)

    outcome = service.kill(entry.spec.job_id, requester_job_id="run")

    assert outcome == {"job_id": entry.spec.job_id, "state": "done", "killed": False}
    assert terminated == []
    assert service.consume(entry.spec.job_id)["deliverable"]["summary"] == (
        "finished before shutdown"
    )


def test_recovers_successful_artifact_that_predates_old_kill(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-001", archetype="deep_search"),
        state="killed",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
        finished_at="2026-07-15T09:40:53+00:00",
        notes="killed by run",
    )
    service._save_entry(entry)
    _spec_path, result_path, _log_path = service._async_paths(entry.spec.job_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        '{"ok": true, "result": {"status": "done", ' '"deliverable": {"summary": "recover me"}}}',
        encoding="utf-8",
    )
    timestamp = datetime(2026, 7, 15, 9, 40, 52, tzinfo=UTC).timestamp()
    ds.os.utime(result_path, (timestamp, timestamp))
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: True)

    recovered = service.recover_completed_artifacts()

    assert [item.spec.job_id for item in recovered] == [entry.spec.job_id]
    assert service._entry(entry.spec.job_id).state == "done"
    assert service.consume(entry.spec.job_id)["deliverable"]["summary"] == "recover me"


def test_recovers_successful_artifact_that_predates_dead_process_failure(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.em-001", archetype="empirical"),
        state="failed",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
        finished_at="2026-07-15T09:40:53+00:00",
        notes="agent process died",
    )
    service._save_entry(entry)
    _spec_path, result_path, _log_path = service._async_paths(entry.spec.job_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        '{"ok": true, "result": {"status": "done", '
        '"deliverable": {"summary": "recover empirical evidence"}}}',
        encoding="utf-8",
    )
    timestamp = datetime(2026, 7, 15, 9, 40, 52, tzinfo=UTC).timestamp()
    ds.os.utime(result_path, (timestamp, timestamp))
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: True)

    recovered = service.recover_completed_artifacts()

    assert [item.spec.job_id for item in recovered] == [entry.spec.job_id]
    assert service._entry(entry.spec.job_id).state == "done"
    assert service.consume(entry.spec.job_id)["deliverable"]["summary"] == (
        "recover empirical evidence"
    )


def test_does_not_recover_artifact_published_after_kill(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-001", archetype="deep_search"),
        state="killed",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
        finished_at="2026-07-15T09:40:53+00:00",
    )
    service._save_entry(entry)
    _spec_path, result_path, _log_path = service._async_paths(entry.spec.job_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        '{"ok": true, "result": {"status": "done", ' '"deliverable": {"summary": "too late"}}}',
        encoding="utf-8",
    )
    timestamp = datetime(2026, 7, 15, 9, 41, 0, tzinfo=UTC).timestamp()
    ds.os.utime(result_path, (timestamp, timestamp))
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: True)

    assert service.recover_completed_artifacts() == []
    assert service._entry(entry.spec.job_id).state == "killed"


def test_reconcile_marks_dead_and_missing_agents(service, monkeypatch):
    dead_proc = LedgerEntry(
        spec=_spec("run.np-001"),
        state="running",
        process_id=999_999,
        started_at="2026-07-07T00:00:00+00:00",
    )
    no_evidence = LedgerEntry(
        spec=_spec("run.np-002"),
        state="running",
        agent_session_ids=("ghost",),
        started_at="2026-07-07T00:00:00+00:00",
    )
    live_proc = LedgerEntry(
        spec=_spec("run.np-003"),
        state="running",
        process_id=1,
        started_at="2026-07-07T00:00:00+00:00",
    )
    fresh_no_evidence = LedgerEntry(
        spec=_spec("run.np-004"),
        state="running",
        agent_session_ids=("ghost2",),
        started_at=ds._now_iso(),  # just started: patience window still open
    )
    service._save_entry(dead_proc)
    service._save_entry(no_evidence)
    service._save_entry(live_proc)
    service._save_entry(fresh_no_evidence)
    monkeypatch.setattr(ds, "_process_seems_alive", lambda pid: pid == 1)
    monkeypatch.setattr(ds, "process_identity_matches", lambda identity: identity.pid == 1)
    terminated: list[int] = []

    def terminate(entry: LedgerEntry) -> bool:
        terminated.append(entry.process_id)
        return True

    monkeypatch.setattr(ds, "_terminate_dispatch_process_and_wait", terminate)
    monkeypatch.setattr(ds, "summarize_workflow_agents", lambda **kwargs: [])

    service.reconcile()

    states = {entry.spec.job_id: entry.state for entry in service._load_ledger()}
    assert states["run.np-001"] == "failed"
    # Missing evidence past the patience window (started long ago) -> stuck.
    assert states["run.np-002"] == "stuck"
    # A process-isolated worker still obeys its hard wall-clock budget.
    assert states["run.np-003"] == "failed"
    assert terminated == [1]
    assert states["run.np-004"] == "running"


def test_reconcile_keeps_timeout_capacity_until_exact_process_exit(service, monkeypatch):
    """An unconfirmed timeout retirement must not admit a replacement worker."""
    service._cap = 1
    timed_out = LedgerEntry(
        spec=_spec("run.planner.ds-timeout", archetype="deep_search"),
        state="running",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
        started_at="2026-07-07T00:00:00+00:00",
    )
    service._save_entry(timed_out)
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: True)
    exit_confirmed = False

    def terminate_and_wait(_entry):
        return exit_confirmed

    monkeypatch.setattr(ds, "_terminate_dispatch_process_and_wait", terminate_and_wait)
    # Keep the pre-fix implementation from signaling an unrelated test PID.
    monkeypatch.setattr(ds, "_terminate_process_group", lambda _pid: True)
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: False)

    service.reconcile()

    pending = service._entry(timed_out.spec.job_id)
    assert pending.state == "running"
    assert pending.notes == (
        "wall-clock budget exhausted; worker termination pending exact process exit"
    )
    assert [entry.spec.job_id for entry in service.open_jobs()] == [timed_out.spec.job_id]

    replacement = _spec("run.planner.ds-replacement", archetype="deep_search")
    service.propose(replacement)
    with pytest.raises(RuntimeError, match="dispatch cap reached"):
        service._reserve_async_launch(replacement.job_id)

    exit_confirmed = True
    service.reconcile()

    retired = service._entry(timed_out.spec.job_id)
    assert retired.state == "failed"
    assert retired.notes == "wall-clock budget exhausted; worker process exit confirmed"
    _reserved, created = service._reserve_async_launch(replacement.job_id)
    assert created is True


def test_reconcile_does_not_harvest_timeout_signal_artifact_before_exit(service, monkeypatch):
    """A SIGTERM artifact cannot make a still-live timed-out worker terminal."""
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-timeout-artifact", archetype="deep_search"),
        state="running",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="b" * 64,
        started_at="2026-07-07T00:00:00+00:00",
        notes=ds.WALL_CLOCK_TERMINATION_PENDING_NOTE,
    )
    service._save_entry(entry)
    _spec_path, result_path, _log_path = service._async_paths(entry.spec.job_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "ok": False,
                "error": "InterruptedError: dispatch worker received SIGTERM",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: False)
    monkeypatch.setattr(ds, "_dispatch_process_identity_has_exited", lambda _entry: False)

    service.reconcile()

    pending = service._entry(entry.spec.job_id)
    assert pending.state == "running"
    assert pending.notes == ds.WALL_CLOCK_TERMINATION_PENDING_NOTE


@pytest.mark.skipif(os.name != "posix", reason="dispatch process groups require POSIX")
def test_reconcile_kills_term_resistant_timeout_before_freeing_capacity(service, monkeypatch):
    """A TERM-resistant timed-out worker is SIGKILLed before failure is durable."""
    token = "dispatch-wall-clock-term-resistant-worker"
    env = dict(os.environ)
    env[PROCESS_TOKEN_ENV] = token
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(30)"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        entry = LedgerEntry(
            spec=_spec("run.planner.ds-real-timeout", archetype="deep_search"),
            state="running",
            process_id=process.pid,
            process_group_id=os.getpgid(process.pid),
            process_session_id=os.getsid(process.pid),
            process_token_sha256=process_token_sha256(token),
            started_at="2026-07-07T00:00:00+00:00",
        )
        service._save_entry(entry)
        monkeypatch.setattr(ds, "ASYNC_LAUNCH_TERMINATION_GRACE_S", 0.1)
        monkeypatch.setattr(ds, "ASYNC_LAUNCH_TERMINATION_POLL_S", 0.01)
        monkeypatch.setattr(
            ds,
            "process_identity_matches",
            lambda identity: identity.pid == process.pid
            and not ds._dispatch_process_identity_has_exited(entry),
        )
        service.reconcile()

        retired = service._entry(entry.spec.job_id)
        assert retired.state == "failed"
        assert retired.notes == "wall-clock budget exhausted; worker process exit confirmed"
        assert ds._dispatch_process_identity_has_exited(entry)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def test_reconcile_harvests_result_before_dead_process_verdict(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.em-001", archetype="empirical"),
        state="running",
        process_id=4242,
        started_at="2026-07-15T09:30:00+00:00",
    )
    service._save_entry(entry)
    _spec_path, result_path, _log_path = service._async_paths(entry.spec.job_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        '{"ok": true, "result": {"status": "done", '
        '"deliverable": {"summary": "published before zombie"}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ds, "_process_seems_alive", lambda _pid: False)
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: True)

    reconciled = service.reconcile()

    assert reconciled[0].state == "done"
    assert service._entry(entry.spec.job_id).state == "done"
    assert service.consume(entry.spec.job_id)["deliverable"]["summary"] == (
        "published before zombie"
    )


def test_reconcile_grants_exiting_worker_result_publication_grace(service, monkeypatch):
    """A worker result published just after a failed liveness probe must win."""
    entry = LedgerEntry(
        spec=_spec("run.planner.em-002", archetype="empirical"),
        state="running",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
        started_at=ds._now_iso(),
    )
    service._save_entry(entry)
    _spec_path, result_path, _log_path = service._async_paths(entry.spec.job_id)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: False)
    monkeypatch.setattr(ds, "_reap_process", lambda _pid, *, block: True)
    published = False

    def publish_result(_delay: float) -> None:
        nonlocal published
        if published:
            return
        published = True
        result_path.write_text(
            '{"ok": true, "result": {"status": "done", '
            '"deliverable": {"summary": "published while exiting"}}}',
            encoding="utf-8",
        )

    monkeypatch.setattr(ds.time, "sleep", publish_result)

    reconciled = service.reconcile()

    assert reconciled[0].state == "done"
    assert service._entry(entry.spec.job_id).notes == ""
    assert service.consume(entry.spec.job_id)["deliverable"]["summary"] == (
        "published while exiting"
    )
    announced_states = [kwargs["state"] for _args, kwargs in service.test_events]
    assert "failed" not in announced_states


def test_reconcile_fails_crashed_worker_after_bounded_publication_grace(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-crashed", archetype="deep_search"),
        state="running",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
        started_at=ds._now_iso(),
    )
    service._save_entry(entry)
    monkeypatch.setattr(ds, "_dispatch_process_identity_is_live", lambda _entry: False)
    monkeypatch.setattr(ds, "ASYNC_RESULT_PUBLICATION_GRACE_S", 0.25)
    monkeypatch.setattr(ds, "ASYNC_RESULT_RECHECK_INTERVAL_S", 0.1)
    clock = [0.0]
    sleeps: list[float] = []

    def advance_clock(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(ds.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ds.time, "sleep", advance_clock)

    reconciled = service.reconcile()

    assert reconciled[0].state == "failed"
    assert reconciled[0].notes == "agent process died"
    assert sum(sleeps) == pytest.approx(0.25)
    assert all(delay <= 0.1 for delay in sleeps)


def test_reconcile_process_workers_do_not_scan_agent_activity(service, monkeypatch):
    entry = LedgerEntry(
        spec=_spec("run.planner.ds-activity-free", archetype="deep_search"),
        state="running",
        process_id=4242,
        started_at=ds._now_iso(),
    )
    service._save_entry(entry)
    monkeypatch.setattr(ds, "_process_seems_alive", lambda _pid: True)
    monkeypatch.setattr(ds, "process_identity_matches", lambda _identity: True)

    def forbid_summary_scan(**_kwargs):
        raise AssertionError("process-isolated reconciliation scanned activity")

    monkeypatch.setattr(ds, "summarize_workflow_agents", forbid_summary_scan)

    reconciled = service.reconcile()

    assert reconciled[0].state == "running"


def test_descendant_process_ids_include_new_session_children(monkeypatch):
    monkeypatch.setattr(
        ds.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="1 0\n10 1\n20 10\n30 20\n40 10\n99 1\n"),
    )

    descendants = ds._descendant_process_ids(10)

    assert set(descendants) == {20, 30, 40}
    assert descendants.index(30) < descendants.index(20)


def test_terminate_process_group_signals_descendants_in_separate_sessions(monkeypatch):
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(ds, "_descendant_process_ids", lambda _pid: [30, 20])
    monkeypatch.setattr(
        ds.os,
        "killpg",
        lambda pid, sig: calls.append(("group", pid, sig)),
    )
    monkeypatch.setattr(
        ds.os,
        "kill",
        lambda pid, sig: calls.append(("pid", pid, sig)),
    )

    assert ds._terminate_process_group(10) is True
    assert calls == [
        ("group", 10, ds.signal.SIGTERM),
        ("pid", 30, ds.signal.SIGTERM),
        ("pid", 20, ds.signal.SIGTERM),
    ]


def test_patience_policy_requires_both_clauses():
    from datetime import UTC, datetime

    now = datetime(2026, 7, 7, 1, 0, 0, tzinfo=UTC)
    started = "2026-07-07T00:00:00+00:00"  # 3600s ago
    # Over the wall clock (1.5 * 600 = 900s) AND quiet -> stuck.
    assert ds.patience_exceeded(started_at=started, wall_clock_s=600, now=now, last_event_age_s=700)
    # Over the wall clock but the stream is fresh (long Lake build) -> patient.
    assert not ds.patience_exceeded(
        started_at=started, wall_clock_s=600, now=now, last_event_age_s=30
    )
    # Quiet but within the wall clock -> patient.
    assert not ds.patience_exceeded(
        started_at=started, wall_clock_s=6000, now=now, last_event_age_s=10_000
    )


def test_process_worker_wall_clock_is_a_hard_budget():
    from datetime import UTC, datetime

    now = datetime(2026, 7, 7, 1, 0, 0, tzinfo=UTC)
    started = "2026-07-07T00:00:00+00:00"

    assert ds.wall_clock_exceeded(started_at=started, wall_clock_s=600, now=now)
    assert not ds.wall_clock_exceeded(started_at=started, wall_clock_s=7200, now=now)


def test_delegate_backend_isolates_budget(service, monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return '{"results": [{"status": "ok", "summary": "did it", "api_calls": 7}]}'

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    spec = _spec("run.np-001")

    result = service._run_delegate_job(spec)

    assert captured["isolate_budget"] is True
    assert captured["max_iterations"] == spec.budget.api_steps
    assert result["status"] == "done"
    assert result["deliverable"]["summary"] == "did it"


def test_delegate_backend_preserves_structured_provider_reset(service, monkeypatch):
    """The process worker result keeps provider admission metadata intact."""
    deadline = int(datetime.now(UTC).timestamp()) + 600

    def fake_delegate_task(**_kwargs):
        return json.dumps(
            {
                "results": [
                    {
                        "status": "failed",
                        "summary": "",
                        "error": "Codex usage limit reached",
                        "provider_retry_after": {
                            "kind": "usage_limit_reached",
                            "retry_after_seconds": 600,
                            "unavailable_until_epoch": deadline,
                        },
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    result = service._run_delegate_job(_spec("run.ds-provider", archetype="deep_search"))

    assert result["status"] == "failed"
    assert result["provider_retry_after"]["unavailable_until_epoch"] == deadline
    assert result["provider_globally_unavailable"] is True
    assert result["provider_retries_exhausted"] is True


def test_delegate_backend_persists_bounded_sanitized_provider_error(service, monkeypatch):
    """An empty failed worker must retain a safe cause in ledger and activity."""
    secret = "sk-" + "a" * 40
    provider_error = "servers overloaded; Authorization: Bearer " + secret + "; " + "detail " * 400

    def fake_delegate_task(**_kwargs):
        return json.dumps({"error": provider_error})

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    spec = _spec("run.ds-error", archetype="deep_search")
    service.propose(spec)

    entry = service.deploy(spec.job_id)

    assert entry.state == "failed"
    assert entry.result["status"] == "error"
    assert entry.result["api_calls"] == 0
    assert "servers overloaded" in entry.result["error"]
    assert secret not in entry.result["error"]
    assert "[REDACTED]" in entry.result["error"]
    assert len(entry.result["error"]) <= ds.DELEGATE_ERROR_DETAIL_CAP
    assert "servers overloaded" in entry.notes
    assert secret not in entry.notes
    final_event = service.test_events[-1][1]
    assert final_event["state"] == "failed"
    assert "servers overloaded" in final_event["notes"]
    assert secret not in final_event["notes"]


def test_scratch_delegate_uses_read_check_only_lean_tools(service, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": '{"findings_report":{"summary":"checked inline"}}',
                        "api_calls": 1,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    spec = replace(
        _spec("run.ds-readonly", archetype="deep_search"),
        toolsets=("web", "lean"),
    )

    service._run_delegate_job(spec)

    assert captured["toolsets"] == ["web-research", "lean-research"]
    assert "Scratch-only isolation contract" in captured["context"]
    assert "Do not create, modify, rename, or delete any project file" in captured["context"]
    assert "lean_incremental_check's inline replacement" in captured["context"]
    assert "nested LLM advisor tools are not delegated" in captured["context"]
    assert "computations that emit results to stdout" not in captured["context"]
    resolved = set(resolve_multiple_toolsets(captured["toolsets"]))
    assert "lean_incremental_check" in resolved
    assert resolved.isdisjoint({"lean_reasoning_help", "lean_decompose_helpers"})


def test_empirical_delegate_receives_only_dedicated_compute_toolset(service, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": '{"experiment_result":{"summary":"bounded evidence"}}',
                        "api_calls": 1,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    spec = replace(
        _spec("run.em-compute", archetype="empirical"),
        deliverable="experiment_result",
        toolsets=("terminal", "lean", "empirical-compute"),
    )

    service._run_delegate_job(spec)

    assert captured["toolsets"] == ["lean-research", "empirical-compute"]
    assert "Use empirical_compute" in captured["context"]
    assert "Arbitrary Python through terminal remains denied" in captured["context"]
    assert "full exact declaration as replacement" in captured["context"]
    assert "captures successful calls automatically" in captured["context"]


def test_decomposition_delegate_returns_only_source_backed_parent_proposals(service, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": json.dumps(
                            {
                                "decomposition_report": {
                                    "source_basis": [
                                        {
                                            "id": "src-local",
                                            "kind": "local",
                                            "reference": "Main.demo_aux",
                                            "summary": "Supplies the divisibility reduction.",
                                        }
                                    ],
                                    "subgoals": [
                                        {
                                            "id": "sg-div",
                                            "statement": "∀ n, P n → Q n",
                                            "purpose": "Separate the arithmetic reduction.",
                                            "source_refs": ["src-local"],
                                            "dependencies": [],
                                            "difficulty_reduction": "Removes the outer witness.",
                                        }
                                    ],
                                    "dependency_proposals": [
                                        {
                                            "source": "sg-div",
                                            "target": "target",
                                            "kind": "split_of",
                                            "rationale": "The target follows after specialization.",
                                            "source_refs": ["src-local"],
                                        }
                                    ],
                                    "graph_updates": [{"status": "proved"}],
                                    "plan_delta": [{"node_id": "forbidden-child-write"}],
                                }
                            }
                        ),
                        "api_calls": 2,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    spec = replace(
        _spec("run.dc-source-backed", archetype="decomposition"),
        toolsets=("web", "lean"),
        inputs={"target_symbol": "demo", "active_file": "Main.lean"},
    )

    result = service._run_delegate_job(spec)

    assert captured["toolsets"] == ["web-research", "lean-research"]
    resolved_tools = set(resolve_multiple_toolsets(captured["toolsets"]))
    assert {"web_search", "web_fetch", "lean_incremental_check"} <= resolved_tools
    assert resolved_tools.isdisjoint(
        {
            "web_download",
            "repo_clone",
            "apply_verified_patch",
            "write_file",
            "patch",
        }
    )
    assert "Decomposition is proposal-only" in captured["context"]
    assert "parent process is the sole writer" in captured["context"]
    assert ds.DECOMPOSITION_REPORT_CONTRACT in captured["context"]
    assert "source_basis: at most 4 records" in captured["context"]
    assert "subgoals: at most 5 records" in captured["context"]
    assert "dependency_proposals: at most 8 records" in captured["context"]
    assert "source_refs or dependencies: at most 4 ids" in captured["context"]
    assert "literal JSON string `target`" in captured["context"]
    assert "never the theorem name" in captured["context"]
    report = result["deliverable"]
    assert report["schema_version"] == ds.DECOMPOSITION_REPORT_SCHEMA_VERSION
    assert report["status"] == "proposal_parent_review_required"
    assert report["subgoals"][0]["proof_shape"] == "∀ n, P n → Q n"
    assert report["subgoals"][0]["source_refs"] == ["src-local"]
    assert report["dependency_proposals"][0]["target"] == "target"
    assert report["parent_state_write_required"] is True
    assert report["child_state_mutated"] is False
    assert "graph_updates" not in report
    assert result["plan_delta"] == []
    assert result["artifact_paths"] == []


def test_decomposition_toolsets_fail_closed_against_injected_writer_groups():
    spec = replace(
        _spec("run.dc-hostile-tools", archetype="decomposition"),
        toolsets=("web", "lean", "file", "terminal", "search", "leanflow-native"),
    )

    delegated = ds._delegate_toolsets(spec)
    resolved = set(resolve_multiple_toolsets(delegated or []))

    assert delegated == ["web-research", "lean-research"]
    assert resolved.isdisjoint(
        {
            "web_download",
            "repo_clone",
            "apply_verified_patch",
            "write_file",
            "patch",
            "terminal",
            "lean_reasoning_help",
            "lean_decompose_helpers",
        }
    )


@pytest.mark.parametrize(
    ("archetype", "expected"),
    [
        ("prover", ["lean-research"]),
        ("empirical", ["lean-research", "empirical-compute"]),
        ("deep_search", ["web-research", "lean-research"]),
        ("negation_probe", ["lean-research"]),
        ("decomposition", ["web-research", "lean-research"]),
    ],
)
def test_empty_scratch_toolsets_resolve_to_explicit_archetype_allowlist(archetype, expected):
    spec = replace(_spec(f"run.{archetype}", archetype=archetype), toolsets=())

    delegated = ds._delegate_toolsets(spec)

    assert delegated == expected
    assert delegated
    resolved = set(resolve_multiple_toolsets(delegated))
    assert "lean_incremental_check" in resolved
    assert resolved.isdisjoint(
        {
            "terminal",
            "write_file",
            "patch",
            "apply_verified_patch",
            "lean_reasoning_help",
            "lean_decompose_helpers",
        }
    )


def test_scratch_toolsets_reject_entirely_disallowed_surface(service, monkeypatch):
    provider_called = False

    def fake_delegate_task(**_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("delegate provider must not start")

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    spec = replace(
        _spec("run.ds-hostile", archetype="deep_search"),
        toolsets=("file", "terminal", "leanflow-native"),
    )

    with pytest.raises(RuntimeError, match="requested no toolsets permitted"):
        service._run_delegate_job(spec)
    assert provider_called is False


def test_decomposition_deliverable_drops_unbacked_subgoals():
    report = ds._delegate_deliverable(
        json.dumps(
            {
                "decomposition_report": {
                    "source_basis": [],
                    "subgoals": [
                        {
                            "id": "sg-invented",
                            "statement": "False",
                            "source_refs": ["missing-source"],
                        }
                    ],
                    "dependency_proposals": [],
                }
            }
        ),
        "decomposition_report",
    )

    assert report["status"] == "incomplete_unverified"
    assert report["source_basis"] == []
    assert report["subgoals"] == []
    assert report["dependency_proposals"] == []
    assert report["contract_issues"]


@pytest.mark.parametrize(
    "summary",
    [
        "not JSON",
        "[]",
        '{"decomposition_report":[]}',
        '{"deliverable":"not an object"}',
    ],
)
def test_decomposition_deliverable_marks_malformed_output_incomplete(summary):
    report = ds._delegate_deliverable(summary, "decomposition_report")

    assert report["status"] == "incomplete_unverified"
    assert report["source_basis"] == []
    assert report["subgoals"] == []
    assert report["dependency_proposals"] == []
    assert report["contract_issues"]
    assert "summary" not in report


def test_empirical_deliverable_accepts_top_level_schema_discriminator():
    """Preserve an em-705-style report whose schema name is a scalar field."""
    report = ds._delegate_deliverable(
        json.dumps(
            {
                "deliverable": "experiment_result",
                "status": "new_fixed_instance_checked_not_target_completion",
                "bounded_experiment": {
                    "instance": {"a": 3, "n": 8, "x": 3, "y": 25, "z": 600},
                    "bounds": {"a": [3, 3], "n": [8, 8]},
                },
                "issues": ["finite coverage only"],
            }
        ),
        "experiment_result",
    )

    assert report["status"] == "new_fixed_instance_checked_not_target_completion"
    assert report["bounded_experiment"]["instance"] == {
        "a": 3,
        "n": 8,
        "x": 3,
        "y": 25,
        "z": 600,
    }
    assert report["bounded_experiment"]["bounds"] == {"a": [3, 3], "n": [8, 8]}
    assert report["issues"] == ["finite coverage only"]
    assert "deliverable" not in report


def test_decomposition_backend_fails_an_ok_turn_with_malformed_report(service, monkeypatch):
    def fake_delegate_task(**_kwargs):
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": "I found a promising split but did not return JSON.",
                        "api_calls": 1,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)

    result = service._run_delegate_job(_spec("run.dc-malformed", archetype="decomposition"))

    assert result["status"] == "failed"
    assert result["deliverable"]["status"] == "incomplete_unverified"
    assert result["deliverable"]["contract_issues"]
    assert "source-backed contract" in result["error"]


def test_decomposition_deliverable_rejects_unapproved_source_kind():
    report = ds._delegate_deliverable(
        json.dumps(
            {
                "decomposition_report": {
                    "source_basis": [
                        {
                            "id": "src-hearsay",
                            "kind": "model_memory",
                            "reference": "an uncited recollection",
                        }
                    ],
                    "subgoals": [
                        {
                            "id": "sg-a",
                            "statement": "A",
                            "source_refs": ["src-hearsay"],
                            "difficulty_reduction": "Removes one quantifier.",
                        }
                    ],
                    "dependency_proposals": [
                        {
                            "source": "sg-a",
                            "target": "target",
                            "kind": "split_of",
                            "rationale": "A would imply the target.",
                            "source_refs": ["src-hearsay"],
                        }
                    ],
                }
            }
        ),
        "decomposition_report",
    )

    assert report["status"] == "incomplete_unverified"
    assert report["source_basis"] == []
    assert any("allowed kind" in issue for issue in report["contract_issues"])


def test_decomposition_deliverable_requires_target_connectivity():
    report = ds._delegate_deliverable(
        json.dumps(
            {
                "decomposition_report": {
                    "source_basis": [
                        {
                            "id": "src-local",
                            "kind": "local",
                            "reference": "Main.demo",
                        }
                    ],
                    "subgoals": [
                        {
                            "id": "sg-a",
                            "statement": "A",
                            "source_refs": ["src-local"],
                            "difficulty_reduction": "Eliminates the outer witness.",
                        },
                        {
                            "id": "sg-b",
                            "statement": "B",
                            "source_refs": ["src-local"],
                            "difficulty_reduction": "Reduces to a finite case split.",
                        },
                    ],
                    "dependency_proposals": [
                        {
                            "source": "sg-a",
                            "target": "sg-b",
                            "kind": "split_of",
                            "rationale": "A supplies B, but neither is linked to the target.",
                            "source_refs": ["src-local"],
                        }
                    ],
                }
            }
        ),
        "decomposition_report",
    )

    assert report["status"] == "incomplete_unverified"
    assert any("dependency path to target" in issue for issue in report["contract_issues"])


def test_decomposition_deliverable_accepts_kind_aware_target_connectivity():
    report = ds._delegate_deliverable(
        json.dumps(
            {
                "decomposition_report": {
                    "source_basis": [
                        {
                            "id": "src-local",
                            "kind": "local",
                            "reference": "Main.demo",
                        }
                    ],
                    "subgoals": [
                        {
                            "id": "sg-a",
                            "statement": "A",
                            "source_refs": ["src-local"],
                            "difficulty_reduction": "Removes the existential witness.",
                        },
                        {
                            "id": "sg-b",
                            "statement": "B",
                            "source_refs": ["src-local"],
                            "difficulty_reduction": "Reduces to a local congruence.",
                        },
                    ],
                    "dependency_proposals": [
                        {
                            "source": "target",
                            "target": "sg-a",
                            "kind": "depends_on",
                            "rationale": "The target depends on A.",
                            "source_refs": ["src-local"],
                        },
                        {
                            "source": "sg-b",
                            "target": "sg-a",
                            "kind": "split_of",
                            "rationale": "B is one source-backed split of A.",
                            "source_refs": ["src-local"],
                        },
                    ],
                }
            }
        ),
        "decomposition_report",
    )

    assert report["status"] == "proposal_parent_review_required"


def test_decomposition_deliverable_requires_strict_difficulty_reduction():
    report = ds._delegate_deliverable(
        json.dumps(
            {
                "decomposition_report": {
                    "source_basis": [
                        {
                            "id": "src-local",
                            "kind": "local",
                            "reference": "Main.demo",
                        }
                    ],
                    "subgoals": [
                        {
                            "id": "sg-a",
                            "statement": "A",
                            "source_refs": ["src-local"],
                            "difficulty_reduction": "",
                        }
                    ],
                    "dependency_proposals": [
                        {
                            "source": "sg-a",
                            "target": "target",
                            "kind": "split_of",
                            "rationale": "A would imply the target.",
                            "source_refs": ["src-local"],
                        }
                    ],
                }
            }
        ),
        "decomposition_report",
    )

    assert report["status"] == "partial_proposal_parent_review_required"
    assert any("difficulty_reduction" in issue for issue in report["contract_issues"])


def test_decomposition_deliverable_marks_field_truncation_partial():
    report = ds._delegate_deliverable(
        json.dumps(
            {
                "decomposition_report": {
                    "source_basis": [
                        {
                            "id": "src-local",
                            "kind": "local",
                            "reference": "R" * (ds.DECOMPOSITION_REFERENCE_CAP + 1),
                        }
                    ],
                    "subgoals": [
                        {
                            "id": "sg-a",
                            "statement": "P" * (ds.DECOMPOSITION_STATEMENT_CAP + 1),
                            "source_refs": ["src-local"],
                            "difficulty_reduction": "Eliminates one quantifier.",
                        }
                    ],
                    "dependency_proposals": [
                        {
                            "source": "sg-a",
                            "target": "target",
                            "kind": "split_of",
                            "rationale": "r" * (ds.DECOMPOSITION_RATIONALE_CAP + 1),
                            "source_refs": ["src-local"],
                        }
                    ],
                }
            }
        ),
        "decomposition_report",
    )

    assert report["status"] == "partial_proposal_parent_review_required"
    assert len(report["source_basis"][0]["reference"]) == ds.DECOMPOSITION_REFERENCE_CAP
    assert len(report["subgoals"][0]["statement"]) == ds.DECOMPOSITION_STATEMENT_CAP
    assert len(report["dependency_proposals"][0]["rationale"]) == ds.DECOMPOSITION_RATIONALE_CAP
    issues = report["contract_issues"]
    assert any("source_basis[0].reference" in issue for issue in issues)
    assert any("subgoals[0].statement" in issue for issue in issues)
    assert any("dependency_proposals[0].rationale" in issue for issue in issues)


def test_large_decomposition_deliverable_preserves_bounded_structure():
    source_basis = [
        {
            "id": f"src-{index}",
            "kind": "local",
            "reference": "Main." + "r" * 5000,
            "summary": "s" * 5000,
        }
        for index in range(12)
    ]
    subgoals = [
        {
            "id": f"sg-{index}",
            "statement": "P" * 5000,
            "purpose": "p" * 5000,
            "source_refs": ["src-0"],
            "dependencies": [],
            "difficulty_reduction": "d" * 5000,
        }
        for index in range(12)
    ]
    dependencies = [
        {
            "source": f"sg-{index}",
            "target": "target",
            "kind": "split_of",
            "rationale": "r" * 5000,
            "source_refs": ["src-0"],
        }
        for index in range(12)
    ]

    report = ds._delegate_deliverable(
        json.dumps(
            {
                "decomposition_report": {
                    "source_basis": source_basis,
                    "subgoals": subgoals,
                    "dependency_proposals": dependencies,
                }
            }
        ),
        "decomposition_report",
    )

    assert len(json.dumps(report, ensure_ascii=False, sort_keys=True)) <= ds.DELIVERABLE_JSON_CAP
    assert 0 < len(report["source_basis"]) <= ds.DECOMPOSITION_MAX_SOURCES
    assert 0 < len(report["subgoals"]) <= ds.DECOMPOSITION_MAX_SUBGOALS
    assert 0 < len(report["dependency_proposals"]) <= (ds.DECOMPOSITION_MAX_DEPENDENCY_PROPOSALS)
    assert "summary" not in report
    assert any("durable limit" in issue for issue in report["contract_issues"])


def test_final_decomposition_assembly_preserves_graph_context_and_exact_helpers(
    service, monkeypatch, tmp_path
):
    active_file = str(tmp_path / "Main.lean")
    replacement = "private lemma demo_helper : True := by\n" + "  exact True.intro\n" * 2200
    arguments, raw_check = _successful_helper_check(
        replacement,
        target_symbol="demo",
        active_file=active_file,
    )
    route_context = {
        "assignment": {"target_symbol": "demo", "active_file": active_file},
        "recent_failed_proof_shapes": [
            {
                "attempt": index,
                "proof_shape": "rw [very_large_failed_shape]; " + "x" * 4000,
                "reason": "kernel rejected " + "y" * 4000,
            }
            for index in range(8)
        ],
    }
    report = {
        "decomposition_report": {
            "source_basis": [
                {
                    "id": f"src-{index}",
                    "kind": "local",
                    "reference": "Main." + "r" * 1000,
                    "summary": "s" * 1000,
                }
                for index in range(8)
            ],
            "subgoals": [
                {
                    "id": f"sg-{index}",
                    "statement": "P" * 2000,
                    "purpose": "p" * 1000,
                    "source_refs": ["src-0"],
                    "difficulty_reduction": "d" * 1000,
                }
                for index in range(8)
            ],
            "dependency_proposals": [
                {
                    "source": f"sg-{index}",
                    "target": "target",
                    "kind": "split_of",
                    "rationale": "r" * 1000,
                    "source_refs": ["src-0"],
                }
                for index in range(8)
            ],
        }
    }

    def fake_delegate_task(**kwargs):
        kwargs["post_tool_result_callback"](
            "lean_incremental_check",
            arguments,
            raw_check,
        )
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": json.dumps(report),
                        "api_calls": 3,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    spec = replace(
        _spec("run.dc-final-cap", archetype="decomposition"),
        toolsets=("web", "lean"),
        inputs={
            "target_symbol": "demo",
            "active_file": active_file,
            research_route_context.ROUTE_CONTEXT_INPUT_KEY: route_context,
        },
    )

    result = service._run_delegate_job(spec)
    deliverable = result["deliverable"]

    assert result["status"] == "done"
    assert deliverable["source_basis"]
    assert deliverable["subgoals"]
    assert deliverable["dependency_proposals"]
    assert research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY in deliverable
    assert deliverable[ds.CHECKED_HELPERS_KEY][0]["declaration"] == replacement
    assert deliverable["structured_decomposition_exceeds_deliverable_cap"] is True
    assert "summary" not in deliverable


def test_decomposition_deliverable_rejects_dependency_cycles():
    report = ds._delegate_deliverable(
        json.dumps(
            {
                "decomposition_report": {
                    "source_basis": [
                        {
                            "id": "src-local",
                            "kind": "local",
                            "reference": "Main.demo",
                        }
                    ],
                    "subgoals": [
                        {
                            "id": "sg-a",
                            "statement": "A",
                            "source_refs": ["src-local"],
                            "dependencies": ["sg-b"],
                        },
                        {
                            "id": "sg-b",
                            "statement": "B",
                            "source_refs": ["src-local"],
                            "dependencies": ["sg-a"],
                        },
                    ],
                    "dependency_proposals": [
                        {
                            "source": "sg-b",
                            "target": "target",
                            "kind": "split_of",
                            "rationale": "B is the final source-backed cut.",
                            "source_refs": ["src-local"],
                        },
                        {
                            "source": "target",
                            "target": "sg-b",
                            "kind": "depends_on",
                            "rationale": "This reverse edge would close a cycle.",
                            "source_refs": ["src-local"],
                        },
                    ],
                }
            }
        ),
        "decomposition_report",
    )

    assert report["status"] == "partial_proposal_parent_review_required"
    assert report["subgoals"][0]["dependencies"] == ["sg-b"]
    assert report["subgoals"][1]["dependencies"] == []
    assert report["dependency_proposals"] == [
        {
            "source": "sg-b",
            "target": "target",
            "kind": "split_of",
            "rationale": "B is the final source-backed cut.",
            "source_refs": ["src-local"],
        }
    ]
    assert any("cycle" in issue for issue in report["contract_issues"])


def test_empirical_helper_check_survives_summary_loss_as_parent_owned_artifact(
    service, monkeypatch, tmp_path
):
    """Reproduce em-438: exact checked source must not depend on final prose."""
    active_file = str(tmp_path / "ErdosProblems" / "242.lean")
    replacement = "private lemma demo_helper : True := by\n" + "  exact True.intro\n" * 2200
    arguments, raw_check = _successful_helper_check(
        replacement,
        active_file=active_file,
    )
    captured: dict[str, Any] = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        callback = kwargs["post_tool_result_callback"]
        callback("lean_incremental_check", arguments, raw_check)
        # Repeated tool telemetry is possible; canonical transport deduplicates it.
        callback("lean_incremental_check", arguments, raw_check)
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": json.dumps(
                            {
                                "experiment_result": {
                                    "status": "construction_found",
                                    "summary": "A bounded congruence construction checked.",
                                }
                            }
                        ),
                        "api_calls": 5,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    spec = replace(
        _spec("run.em-438", archetype="empirical"),
        deliverable="experiment_result",
        inputs={"target_symbol": "demo", "active_file": active_file},
    )

    result = service._run_delegate_job(spec)

    assert callable(captured["post_tool_result_callback"])
    assert result["artifact_paths"] == []
    deliverable = result["deliverable"]
    assert len(deliverable["checked_helpers"]) == 1
    helper = deliverable["checked_helpers"][0]
    assert helper["declaration"] == replacement
    assert len(replacement) > ds.DELIVERABLE_JSON_CAP
    assert helper["declaration_sha256"] == hashlib.sha256(replacement.encode()).hexdigest()
    assert helper["anchor_target_symbol"] == "demo"
    assert helper["active_file"] == active_file
    assert helper["worker_check"]["verification_scope"] == "helper_candidate"
    assert helper["worker_check"]["replacement_matches_target"] is False
    assert helper["parent_recheck_required"] is True
    assert deliverable["checked_helper_status"] == ds.CHECKED_HELPER_STATUS
    assert deliverable["parent_recheck_required"] is True
    assert deliverable["exact_code_exceeds_deliverable_cap"] is True
    evidence = research_route_context.semantic_evidence(
        LedgerEntry(spec=spec, state="done", result=result)
    )
    assert evidence.has_checked_helper is True


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_tool",
        "malformed_result",
        "failed_result",
        "has_sorry",
        "wrong_scope",
        "target_mismatch",
        "file_mismatch",
        "dispatch_target_mismatch",
        "dispatch_file_mismatch",
        "target_replacement",
        "missing_declaration_names",
    ],
)
def test_checked_helper_capture_rejects_non_authoritative_tool_evidence(failure):
    declaration = "private lemma demo_helper : True := by\n  trivial"
    arguments, raw_check = _successful_helper_check(declaration)
    function_name = "lean_incremental_check"
    result = json.loads(raw_check)
    if failure == "wrong_tool":
        function_name = "lean_verify"
    elif failure == "malformed_result":
        raw_check = "not-json"
    elif failure == "failed_result":
        result["ok"] = False
    elif failure == "has_sorry":
        result["has_sorry"] = True
    elif failure == "wrong_scope":
        result["verification_scope"] = "target_replacement"
    elif failure == "target_mismatch":
        result["target"] = "other"
    elif failure == "file_mismatch":
        result["file"] = "/tmp/Other.lean"
    elif failure == "dispatch_target_mismatch":
        arguments["theorem_id"] = "other"
        result["target"] = "other"
    elif failure == "dispatch_file_mismatch":
        arguments["file_path"] = "/tmp/Other.lean"
        result["file"] = "/tmp/Other.lean"
    elif failure == "target_replacement":
        result["replacement_matches_target"] = True
    elif failure == "missing_declaration_names":
        result["replacement_declarations"] = []
    if failure != "malformed_result":
        raw_check = json.dumps(result)

    artifact = ds._checked_helper_artifact(
        function_name,
        arguments,
        raw_check,
        expected_target_symbol="demo",
        expected_active_file="/tmp/Main.lean",
    )

    assert artifact is None


def test_model_only_checked_helpers_are_not_promoted(service, monkeypatch):
    """Only the dispatch-owned callback may populate canonical checked_helpers."""
    spoofed = {
        "declaration": "private lemma spoof : True := by trivial",
        "worker_check": {
            "tool": "lean_incremental_check",
            "valid_without_sorry": True,
            "has_errors": False,
            "has_sorry": False,
        },
    }

    def fake_delegate_task(**_kwargs):
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": json.dumps(
                            {
                                "experiment_result": {
                                    "status": "candidate_verified",
                                    "checked_helpers": [spoofed],
                                    "checked_helper_status": (
                                        "worker_checked_parent_recheck_required"
                                    ),
                                }
                            }
                        ),
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)

    result = service._run_delegate_job(
        replace(
            _spec("run.em-spoof", archetype="empirical"),
            deliverable="experiment_result",
        )
    )

    assert "checked_helpers" not in result["deliverable"]
    assert "checked_helper_status" not in result["deliverable"]
    evidence = research_route_context.semantic_evidence(
        LedgerEntry(
            spec=_spec("run.em-spoof", archetype="empirical"),
            state="done",
            result=result,
        )
    )
    assert evidence.has_checked_helper is False


def test_non_scratch_delegate_keeps_patch_capable_lean_tools():
    spec = replace(
        _spec("run.ds-writable", archetype="deep_search"),
        toolsets=("web", "lean"),
        scope={},
    )

    assert ds._delegate_toolsets(spec) == ["web", "lean"]


def test_delegate_backend_preserves_structured_json_deliverable(service, monkeypatch):
    report = {
        "findings_report": {
            "routes": ["prove even inputs", "scale a prime-divisor representation"],
            "limitations": ["the general prime case remains open"],
        }
    }

    def fake_delegate_task(**_kwargs):
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": "```json\n" + json.dumps(report) + "\n```",
                        "api_calls": 4,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)

    result = service._run_delegate_job(_spec("run.ds-001", archetype="deep_search"))

    assert result["deliverable"] == report["findings_report"]


def test_delegate_backend_receives_and_returns_explicit_parent_route_context(service, monkeypatch):
    """The isolated model sees real recent attempts and its finding retains them."""
    captured: dict[str, Any] = {}
    route_context = {
        "assignment": {"target_symbol": "demo", "active_file": "/tmp/Main.lean"},
        "recent_research_routes": [
            {
                "job_id": "run.orchestrator.ds-001",
                "archetype": "deep_search",
                "route_key": "formal-library-grounding",
                "state": "done",
                "objective": "search for a reusable factor lemma",
                "result_excerpt": "no uniform factor lemma found",
            }
        ],
        "recent_failed_proof_shapes": [
            {
                "attempt": 4,
                "proof_shape": "rw [hden]; exact fixed_witness",
                "reason": "kernel rejected",
            }
        ],
    }
    spec = replace(
        _spec("run.ds-context", archetype="deep_search"),
        inputs={research_route_context.ROUTE_CONTEXT_INPUT_KEY: route_context},
    )

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": json.dumps(
                            {"findings_report": {"summary": "a distinct modular split"}}
                        ),
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)

    result = service._run_delegate_job(spec)

    assert "Authoritative bounded parent route/proof-shape context JSON" in captured["context"]
    assert "formal-library-grounding" in captured["context"]
    assert "rw [hden]; exact fixed_witness" in captured["context"]
    attached = result["deliverable"][research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY]
    assert attached["recent_research_routes"][0]["route_key"] == ("formal-library-grounding")
    assert attached["recent_failed_proof_shapes"][0]["attempt"] == 4
    assert attached["sha256"]


def test_deliverable_cap_preserves_bounded_parent_route_context():
    """Large worker prose is trimmed before the explicit novelty-audit context."""
    context = research_route_context.attach_parent_route_context(
        {},
        {
            "assignment": {"target_symbol": "demo", "active_file": "/tmp/Main.lean"},
            "recent_failed_proof_shapes": [
                {
                    "attempt": 5,
                    "proof_shape": "rw [hden]; exact fixed_witness",
                    "reason": "kernel rejected",
                }
            ],
        },
    )[research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY]

    capped = ds._cap_deliverable_preserving_exact_code(
        {
            "summary": "x" * ds.DELIVERABLE_JSON_CAP,
            research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY: context,
        }
    )

    assert research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY in capped
    assert (
        capped[research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY][
            "recent_failed_proof_shapes"
        ][0]["attempt"]
        == 5
    )
    assert len(json.dumps(capped, ensure_ascii=False, sort_keys=True)) <= (ds.DELIVERABLE_JSON_CAP)


def test_delegate_backend_promotes_managed_boundary_with_evidence(service, monkeypatch):
    """A hard route boundary becomes a consumable finding with JobSpec provenance."""
    spec = JobSpec(
        job_id="run.orchestrator.ds-242",
        archetype="deep_search",
        requester_role="orchestrator",
        objective="research the residual class",
        budget=JobBudget(api_steps=40, wall_clock_s=300),
        deliverable="findings_report",
        inputs={
            "target_symbol": "erdos_242_residual_mod_seven_eq_five",
            "active_file": "/tmp/ErdosProblems/242.lean",
            "route_key": "history-refresh:abc",
            "route_signature": "route-signature-242",
        },
        scope={"scratch_only": True},
        parent_job_id="run.orchestrator",
    )
    raw_handoff = {
        "kind": "managed_search_route_boundary",
        "boundary_marker": "[leanflow-native workflow step boundary]",
        "completed_tool_calls": 4,
        "evidence": [
            {
                "tool": "web_fetch",
                "arguments": '{"url":"https://example.test/paper"}',
                "result_excerpt": "A congruence construction reduces the residue class.",
            }
        ],
        "reasoning": ["Try the factor-pair certificate next."],
    }

    def fake_delegate_task(**_kwargs):
        return json.dumps(
            {
                "results": [
                    {
                        "status": "interrupted",
                        "summary": "",
                        "api_calls": 14,
                        "interrupted_handoff": raw_handoff,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)

    result = service._run_delegate_job(spec)

    assert result["status"] == "done"
    deliverable = result["deliverable"]
    assert deliverable["status"] == "interrupted_with_evidence"
    assert deliverable["route_boundary"]["provenance"] == {
        "job_id": spec.job_id,
        "target_symbol": "erdos_242_residual_mod_seven_eq_five",
        "active_file": "/tmp/ErdosProblems/242.lean",
        "route_key": "history-refresh:abc",
        "route_signature": "route-signature-242",
    }
    assert deliverable["next_route"]["kind"] == "synthesize_preserved_evidence"


def test_delegate_backend_keeps_empty_managed_boundary_interrupted(service, monkeypatch):
    """An empty boundary cannot fabricate a successful dispatch result."""

    def fake_delegate_task(**_kwargs):
        return json.dumps(
            {
                "results": [
                    {
                        "status": "interrupted",
                        "summary": "",
                        "api_calls": 14,
                        "interrupted_handoff": {
                            "kind": "managed_search_route_boundary",
                            "boundary_marker": "[leanflow-native workflow step boundary]",
                            "completed_tool_calls": 0,
                            "evidence": [],
                        },
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)

    result = service._run_delegate_job(_spec("run.ds-empty", archetype="deep_search"))

    assert result["status"] == "interrupted"
    assert result["deliverable"] == {"summary": ""}


def test_deep_search_preserves_full_checked_replacement_and_requires_parent_recheck(
    service, monkeypatch
):
    replacement = "theorem demo : True := by\n  " + "exact True.intro\n  " * 2200
    captured: dict[str, Any] = {}
    report = {
        "findings_report": {
            "status": "candidate_verified",
            "checked_replacements": [
                {
                    "target_symbol": "demo",
                    "replacement": replacement,
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "replacement_matches_target": True,
                    },
                }
            ],
            "summary": "A long exact candidate was checked in the worker.",
        }
    }

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": json.dumps(report),
                        "api_calls": 3,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)

    result = service._run_delegate_job(_spec("run.ds-checked", archetype="deep_search"))
    deliverable = result["deliverable"]

    assert deliverable["checked_replacements"][0]["replacement"] == replacement
    assert len(replacement) > ds.DELIVERABLE_STRING_CAP
    assert deliverable["checked_replacement_status"] == ("worker_checked_parent_recheck_required")
    assert deliverable["parent_recheck_required"] is True
    assert deliverable["exact_code_exceeds_deliverable_cap"] is True
    assert ds.CHECKED_REPLACEMENT_CONTRACT in captured["context"]
    assert "never infer it from the declaration name" in captured["context"]
    assert "parent will re-run Lean" in captured["context"]
    assert "checked_replacements contract is target-only" in captured["context"]
    assert "action=check_helper" in captured["context"]


def test_deep_search_downgrades_checked_claim_without_exact_contract(service, monkeypatch):
    report = {
        "findings_report": {
            "status": "alternate_candidate_found",
            "candidate": "by\n  exact True.intro",
            "lean_check": (
                "lean_incremental_check accepted; valid_without_sorry=true, "
                "has_errors=false, has_sorry=false"
            ),
        }
    }

    def fake_delegate_task(**_kwargs):
        return json.dumps(
            {
                "results": [
                    {
                        "status": "ok",
                        "summary": json.dumps(report),
                        "api_calls": 2,
                    }
                ]
            }
        )

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)

    deliverable = service._run_delegate_job(_spec("run.ds-incomplete", archetype="deep_search"))[
        "deliverable"
    ]

    assert deliverable["status"] == "incomplete_unverified"
    assert deliverable["reported_status"] == "alternate_candidate_found"
    assert deliverable["checked_replacements"] == []
    assert "omitted" in " ".join(deliverable["checked_replacement_contract_issues"])


def test_auxiliary_helper_verification_does_not_claim_target_replacement():
    """A ds-703-style helper check must retain its partial-result status."""
    payload = ds.enforce_checked_replacement_contract(
        {
            "status": "checked_partial_parametric_delta_not_target_completion",
            "verification": {
                "action": "check_helper",
                "kind": "auxiliary_helper",
                "tool": "lean_incremental_check",
                "valid_without_sorry": True,
                "has_errors": False,
                "has_sorry": False,
                "result": "success",
            },
            "checked_helpers": [
                {
                    "declaration": "private lemma helper : True := by trivial",
                    "worker_check": {
                        "action": "check_helper",
                        "verification_scope": "helper_candidate",
                        "replacement_matches_target": False,
                        "valid_without_sorry": True,
                    },
                }
            ],
        },
        expected_target_symbol="demo",
    )

    assert payload["status"] == "checked_partial_parametric_delta_not_target_completion"
    assert "reported_status" not in payload
    assert "checked_replacement_status" not in payload
    assert "checked_replacement_contract_issues" not in payload


def test_checked_replacement_for_wrong_dispatched_target_is_unverified():
    payload = ds.enforce_checked_replacement_contract(
        {
            "status": "verified",
            "checked_replacements": [
                {
                    "target_symbol": "other",
                    "replacement": "by\n  exact True.intro",
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "replacement_matches_target": True,
                    },
                }
            ],
        },
        expected_target_symbol="demo",
    )

    assert payload["status"] == "incomplete_unverified"
    assert payload["checked_replacements"] == []
    assert payload["unchecked_replacements"][0]["replacement"] == ("by\n  exact True.intro")
    assert "does not match dispatched target" in " ".join(
        payload["checked_replacement_contract_issues"]
    )


@pytest.mark.parametrize("spoofed_tool", ["lean_verify", "custom_lean_checker"])
def test_checked_replacement_rejects_spoofed_checker_identity(spoofed_tool):
    payload = ds.enforce_checked_replacement_contract(
        {
            "status": "candidate_verified",
            "checked_replacements": [
                {
                    "target_symbol": "demo",
                    "replacement": "theorem demo : True := by\n  exact True.intro",
                    "worker_check": {
                        "tool": spoofed_tool,
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "replacement_matches_target": True,
                    },
                }
            ],
        },
        expected_target_symbol="demo",
    )

    assert payload["status"] == "incomplete_unverified"
    assert payload["reported_status"] == "candidate_verified"
    assert payload["checked_replacements"] == []
    assert payload["unchecked_replacements"][0]["worker_check"]["tool"] == spoofed_tool
    assert "worker_check.tool must be lean_incremental_check" in " ".join(
        payload["checked_replacement_contract_issues"]
    )


@pytest.mark.parametrize("match_metadata", [False, None], ids=["mismatch", "omitted"])
def test_checked_replacement_with_same_name_but_unverified_statement_match_is_downgraded(
    match_metadata,
):
    worker_check = {
        "tool": "lean_incremental_check",
        "valid_without_sorry": True,
        "has_errors": False,
        "has_sorry": False,
    }
    if match_metadata is not None:
        worker_check["replacement_matches_target"] = match_metadata
    payload = ds.enforce_checked_replacement_contract(
        {
            "status": "candidate_verified",
            "checked_replacements": [
                {
                    # This reproduces the ds-299 failure mode: the declaration
                    # name matches while its proposition is a scratch
                    # countermodel rather than the assigned theorem.
                    "target_symbol": "demo",
                    "replacement": "theorem demo : False := by\n  contradiction",
                    "worker_check": worker_check,
                }
            ],
        },
        expected_target_symbol="demo",
    )

    assert payload["status"] == "incomplete_unverified"
    assert payload["reported_status"] == "candidate_verified"
    assert payload["checked_replacements"] == []
    assert payload["unchecked_replacements"][0]["replacement"] == (
        "theorem demo : False := by\n  contradiction"
    )
    assert "replacement_matches_target must be true" in " ".join(
        payload["checked_replacement_contract_issues"]
    )
