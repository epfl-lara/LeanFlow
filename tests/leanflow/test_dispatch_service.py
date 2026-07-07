"""Phase 3 tests: dispatch models (lineage, state machine) + service lifecycle."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.workflows import dispatch_service as ds
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
    return JobSpec(
        job_id=job_id,
        archetype=archetype,
        requester_role=role,
        objective="probe the negation of demo",
        budget=JobBudget(api_steps=20, wall_clock_s=300),
        deliverable="probe_verdict",
        scope={"scratch_only": True},
        parent_job_id=job_id.rpartition(".")[0],
    )


# --- pure models -----------------------------------------------------------


def test_lineage_minting_and_ancestry():
    first = next_job_id([], "run.orchestrator", "deep_search")
    assert first == "run.orchestrator.ds-001"
    second = next_job_id([first], "run.orchestrator", "negation_probe")
    assert second == "run.orchestrator.np-002"
    grandchild = next_job_id([first, second], first, "empirical")
    assert grandchild == "run.orchestrator.ds-001.em-001"

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
    entry = LedgerEntry(spec=_spec("run.np-001"), state="running", agent_session_ids=("a1",))
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
    # The trailing "done" is consume() persisting the consumed flag.
    assert states == ["proposed", "deployed", "running", "done", "done"]


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
    monkeypatch.setattr(ds, "summarize_workflow_agents", lambda **kwargs: [])

    service.reconcile()

    states = {entry.spec.job_id: entry.state for entry in service._load_ledger()}
    assert states["run.np-001"] == "failed"
    # Missing evidence past the patience window (started long ago) -> stuck.
    assert states["run.np-002"] == "stuck"
    # A live pid is live evidence; a fresh job stays within its patience.
    assert states["run.np-003"] == "running"
    assert states["run.np-004"] == "running"


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
