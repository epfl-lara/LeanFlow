"""Background research portfolio lifecycle tests."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from core.process_identity import PROCESS_TOKEN_ENV, process_token_sha256
from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import (
    campaign_epoch,
    dispatch_ledger_compaction,
    dispatch_service,
    orchestrator,
    plan_state,
    research_finding_priority,
    research_findings,
    research_portfolio,
    research_route_context,
    workflow_json_io,
)
from leanflow_cli.workflows.dispatch_models import (
    ASSIGNMENT_REVISION_INPUT_KEY,
    JobBudget,
    JobSpec,
    LedgerEntry,
)


def _seed_completed_research_job(
    *,
    campaign_id: str = "campaign-demo",
    target_symbol: str = "demo",
    active_file: str = "Main.lean",
    deliverable: dict | None = None,
) -> tuple[dispatch_service.DispatchService, str]:
    """Persist one completed, unconsumed research job for crash-order tests."""
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    spec = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=0,
    )
    service.propose(spec)
    service._transition(spec.job_id, "deployed")
    service._transition(
        spec.job_id,
        "running",
        started_at=dispatch_service._now_iso(),
    )
    service._transition(
        spec.job_id,
        "done",
        finished_at=dispatch_service._now_iso(),
        result={
            "status": "done",
            "deliverable": deliverable or {"summary": "durable route evidence"},
            "artifact_paths": [".leanflow/research/ResearchDemo.lean"],
            "plan_delta": [],
        },
    )
    return service, spec.job_id


def _consumed_research_entry(
    *,
    campaign_id: str,
    suffix: str,
    target_symbol: str,
    active_file: str,
    summary: str,
    created_at: str = "",
    finished_at: str = "",
) -> LedgerEntry:
    """Return one exact, consumed ledger result for archive-paging tests."""
    job_id = f"{campaign_id}.orchestrator.{suffix}"
    return LedgerEntry(
        spec=JobSpec(
            job_id=job_id,
            archetype="deep_search",
            requester_role="orchestrator",
            objective=f"research {target_symbol}: {summary}",
            budget=JobBudget(api_steps=2, wall_clock_s=30),
            deliverable="findings_report",
            inputs={
                "campaign_id": campaign_id,
                "target_symbol": target_symbol,
                "active_file": active_file,
            },
            parent_job_id=f"{campaign_id}.orchestrator",
        ),
        state="done",
        created_at=created_at,
        finished_at=finished_at,
        result={"status": "done", "deliverable": {"summary": summary}},
        consumed=True,
    )


def _pre_compaction_finding_and_compacted_ledger(
    entry: LedgerEntry,
) -> tuple[dict, dict, str]:
    """Return one finding copied before its terminal ledger objective shrinks."""
    full_objective = (
        f"{entry.spec.objective}\n\n{research_route_context.ROUTE_CONTEXT_MARKER}\n"
        "Prior route history copied into the worker prompt."
    )
    original = replace(entry, spec=replace(entry.spec, objective=full_objective))
    finding = research_findings.build_finding_record(
        original,
        original.result,
        entries=[original],
    )
    compacted_ledger = original.to_mapping()
    dispatch_ledger_compaction.compact_terminal_dispatch_records([compacted_ledger])
    return finding, compacted_ledger, full_objective


def _anchored_followup_entry(
    *,
    campaign_id: str,
    source_job_id: str,
    target_symbol: str,
    active_file: str,
    state: str,
    consumed: bool = False,
    substantive: bool = True,
) -> LedgerEntry:
    """Return one evidence-synthesis worker anchored to an exact source."""
    helper_name = "checked_followup_helper"
    declaration = f"private lemma {helper_name} : True := by trivial"
    deliverable = (
        {
            "checked_helper_status": "worker_checked_parent_recheck_required",
            "parent_recheck_required": True,
            "checked_helpers": [
                {
                    "anchor_target_symbol": target_symbol,
                    "active_file": active_file,
                    "declaration": declaration,
                    "declaration_sha256": sha256(declaration.encode()).hexdigest(),
                    "parent_recheck_required": True,
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "action": "check_helper",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "verification_scope": "helper_candidate",
                        "replacement_matches_target": False,
                        "replacement_declarations": [helper_name],
                    },
                }
            ],
        }
        if substantive
        else {}
    )
    return LedgerEntry(
        spec=JobSpec(
            job_id=f"{campaign_id}.orchestrator.em-followup",
            archetype="empirical",
            requester_role="orchestrator",
            objective="translate exact source evidence into one checked helper",
            budget=JobBudget(api_steps=4, wall_clock_s=60),
            deliverable="experiment_result",
            inputs={
                "campaign_id": campaign_id,
                "target_symbol": target_symbol,
                "active_file": active_file,
                "route_key": "evidence-to-helper",
                "route_mode": "evidence_synthesis",
                "route_anchor_job_id": source_job_id,
                "route_anchor_consumption_key": f"consume:{source_job_id}",
                "route_anchor_provenance": {
                    "job_id": source_job_id,
                    "target_symbol": target_symbol,
                    "active_file": active_file,
                },
            },
            parent_job_id=f"{campaign_id}.orchestrator",
        ),
        state=state,
        result={"status": "done", "deliverable": deliverable} if state == "done" else {},
        consumed=consumed,
    )


def test_active_anchored_followup_reserves_exact_source_from_foreground():
    """Do not let foreground and background synthesize the same finding."""
    campaign_id = "campaign-demo"
    source_job_id = f"{campaign_id}.orchestrator.em-source"
    target_symbol = "demo"
    active_file = "Main.lean"
    followup = _anchored_followup_entry(
        campaign_id=campaign_id,
        source_job_id=source_job_id,
        target_symbol=target_symbol,
        active_file=active_file,
        state="running",
    )

    plan = research_portfolio._anchored_followup_delivery_plan(
        (followup,),
        ({"job_id": source_job_id},),
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert plan["deferred_source_job_ids"] == [source_job_id]
    assert plan["priority_followup_job_ids"] == []
    assert plan["source_receipts_by_followup"] == {}


@pytest.mark.parametrize("state", ["failed", "killed"])
def test_failed_anchored_followup_restores_source_delivery(state):
    """Operationally terminal follow-ups cannot consume their source finding."""
    campaign_id = "campaign-demo"
    source_job_id = f"{campaign_id}.orchestrator.em-source"
    target_symbol = "demo"
    active_file = "Main.lean"
    followup = _anchored_followup_entry(
        campaign_id=campaign_id,
        source_job_id=source_job_id,
        target_symbol=target_symbol,
        active_file=active_file,
        state=state,
    )

    plan = research_portfolio._anchored_followup_delivery_plan(
        (followup,),
        ({"job_id": source_job_id},),
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert plan["deferred_source_job_ids"] == []
    assert plan["source_receipts_by_followup"] == {}


def test_substantive_anchored_followup_prioritizes_result_and_consumes_source_receipt():
    """Deliver checked synthesis once and retire its exact source marker with it."""
    campaign_id = "campaign-demo"
    source_job_id = f"{campaign_id}.orchestrator.em-source"
    target_symbol = "demo"
    active_file = "Main.lean"
    followup = _anchored_followup_entry(
        campaign_id=campaign_id,
        source_job_id=source_job_id,
        target_symbol=target_symbol,
        active_file=active_file,
        state="done",
        consumed=True,
    )
    findings = (
        {"job_id": source_job_id},
        {
            "job_id": followup.spec.job_id,
            "target_symbol": target_symbol,
            "active_file": active_file,
            "deliverable": dict(followup.result["deliverable"]),
        },
    )

    plan = research_portfolio._anchored_followup_delivery_plan(
        (followup,),
        findings,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert plan["deferred_source_job_ids"] == []
    assert plan["priority_followup_job_ids"] == [followup.spec.job_id]
    assert plan["source_receipts_by_followup"] == {followup.spec.job_id: [source_job_id]}


def test_evidence_synthesis_receipt_coupling_requires_actionable_checked_helper():
    """Preserve a source unless its synthesis produced an actionable checked helper."""
    campaign_id = "campaign-demo"
    source_job_id = f"{campaign_id}.orchestrator.em-source"
    target_symbol = "demo"
    active_file = "Main.lean"
    source_finding = {
        "job_id": source_job_id,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "deliverable": {"construction": "source formula that still needs synthesis"},
    }
    checked_followup = _anchored_followup_entry(
        campaign_id=campaign_id,
        source_job_id=source_job_id,
        target_symbol=target_symbol,
        active_file=active_file,
        state="done",
        consumed=True,
    )
    nonclosing_deliverable = {
        "completion_status": "incomplete_unverified",
        "obstruction": "the attempted translation left a different dependency open",
    }
    nonclosing_followup = replace(
        checked_followup,
        result={"status": "done", "deliverable": nonclosing_deliverable},
    )
    ineligible_finding = {
        "job_id": nonclosing_followup.spec.job_id,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "deliverable": nonclosing_deliverable,
        "semantic_novelty": {
            "version": research_route_context.SEMANTIC_NOVELTY_VERSION,
            "classification": "nonclosing",
            "progress_anchor_eligible": False,
            "progress_anchor_reason": "explicit_nonclosing_result",
        },
    }

    preserved = research_portfolio.prepare_anchored_foreground_findings(
        (source_finding, ineligible_finding),
        summary={"dispatch_ledger": [nonclosing_followup.to_mapping()]},
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert [finding["job_id"] for finding in preserved] == [
        source_job_id,
        nonclosing_followup.spec.job_id,
    ]
    assert all("route_anchor_delivery" not in finding for finding in preserved)
    assert research_portfolio.foreground_delivery_job_ids(preserved[0]) == (source_job_id,)
    assert research_portfolio.foreground_delivery_job_ids(preserved[1]) == (
        nonclosing_followup.spec.job_id,
    )
    assert "source formula that still needs synthesis" in research_findings.prompt_payload(
        preserved
    )

    checked_finding = {
        "job_id": checked_followup.spec.job_id,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "deliverable": dict(checked_followup.result["deliverable"]),
    }
    coupled = research_portfolio.prepare_anchored_foreground_findings(
        (source_finding, checked_finding),
        summary={"dispatch_ledger": [checked_followup.to_mapping()]},
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert [finding["job_id"] for finding in coupled] == [checked_followup.spec.job_id]
    assert research_findings.foreground_use_role(coupled[0]) == "actionable"
    assert research_portfolio.foreground_delivery_job_ids(coupled[0]) == (
        checked_followup.spec.job_id,
        source_job_id,
    )


def test_unconsumed_substantive_followup_defers_source_until_harvest():
    """Keep the source reserved across the result-to-finding commit window."""
    campaign_id = "campaign-demo"
    source_job_id = f"{campaign_id}.orchestrator.em-source"
    target_symbol = "demo"
    active_file = "Main.lean"
    followup = _anchored_followup_entry(
        campaign_id=campaign_id,
        source_job_id=source_job_id,
        target_symbol=target_symbol,
        active_file=active_file,
        state="done",
        consumed=False,
    )

    plan = research_portfolio._anchored_followup_delivery_plan(
        (followup,),
        ({"job_id": source_job_id},),
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert plan["deferred_source_job_ids"] == [source_job_id]


def _mixed_timeout_mathematical_deliverable() -> dict[str, object]:
    """Return mathematical route evidence accompanied by one local tool timeout."""
    return {
        "completion_status": "incomplete_unverified",
        "local_mechanism": {
            "certificate": "erdos_242_factor_pair_certificate",
            "why_it_closes": "Reduce the target to one exact factor-pair identity.",
        },
        "partial_coverage_blueprint": {
            "immediate_branch": {
                "condition": "q = 5*t",
                "factorization": "168*q+25 = 5*(168*t+5)",
            },
            "remaining_work": "The other four residue classes remain unresolved.",
        },
        "research_tooling": {
            "decomposition": "Helper-decomposition request timed out.",
            "lean_checks": "No target replacement was claimed checked.",
        },
        "tested_construction_analysis": [
            {
                "choice": "r = 3",
                "modular_obstruction": (
                    "The evident factors cannot supply the required residue modulo 3."
                ),
            }
        ],
    }


def _seed_legacy_killed_process(
    service: dispatch_service.DispatchService,
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    suffix: str = "ds-legacy",
    process_id: int = 4242,
) -> LedgerEntry:
    """Persist one terminal legacy PID-only worker for release tests."""
    entry = LedgerEntry(
        spec=JobSpec(
            job_id=f"{campaign_id}.orchestrator.{suffix}",
            archetype="deep_search",
            requester_role="orchestrator",
            objective=f"research prior assignment {target_symbol}",
            budget=JobBudget(api_steps=8, wall_clock_s=60),
            deliverable="findings_report",
            inputs={
                "campaign_id": campaign_id,
                "target_symbol": target_symbol,
                "active_file": active_file,
            },
            scope={"scratch_only": True},
            parent_job_id=f"{campaign_id}.orchestrator",
        ),
        state="killed",
        process_id=process_id,
        created_at="2026-07-15T08:01:12+00:00",
        started_at="2026-07-15T08:01:13+00:00",
        finished_at="2026-07-15T08:11:37+00:00",
    )
    service._save_entry(entry)
    return entry


def _checked_residue_lane_entry(
    *,
    job_id: str,
    target_symbol: str,
    active_file: str,
    modulus: int,
    residue: int,
    consumed: bool = False,
) -> LedgerEntry:
    """Return one checked empirical residue result with a stable proof mechanism."""
    helper_name = f"checked_mod_{modulus}_eq_{residue}"
    declaration = (
        f"private lemma {helper_name} (t : Nat) (h : t % {modulus} = {residue}) : "
        "True := by\n  exact True.intro"
    )
    objective = f"Check the residue t % {modulus} = {residue}."
    spec = JobSpec(
        job_id=job_id,
        archetype="empirical",
        requester_role="orchestrator",
        objective=objective,
        budget=JobBudget(api_steps=2, wall_clock_s=30),
        deliverable="experiment_result",
        inputs={
            "target_symbol": target_symbol,
            "active_file": active_file,
            "route_key": f"checked-residue-{modulus}-{residue}",
            "route_signature": research_portfolio._stable_route_signature(
                archetype="empirical",
                target_symbol=target_symbol,
                active_file=active_file,
                objective=objective,
            ),
        },
        parent_job_id=job_id.rpartition(".")[0],
    )
    return LedgerEntry(
        spec=spec,
        state="done",
        finished_at="2026-07-17T12:00:00+00:00",
        result={
            "status": "done",
            "deliverable": {
                "status": "candidate_verified",
                "checked_helpers": [
                    {
                        "anchor_target_symbol": target_symbol,
                        "active_file": active_file,
                        "declaration": declaration,
                        "declaration_sha256": sha256(declaration.encode("utf-8")).hexdigest(),
                        "worker_check": {
                            "tool": "lean_incremental_check",
                            "action": "check_helper",
                            "valid_without_sorry": True,
                            "has_errors": False,
                            "has_sorry": False,
                            "replacement_matches_target": False,
                        },
                    }
                ],
            },
        },
        consumed=consumed,
    )


def _checked_obstruction_lane_entry(
    *,
    job_id: str,
    target_symbol: str,
    active_file: str,
) -> LedgerEntry:
    """Return one checked parametric obstruction without finite branch coverage."""
    declaration = (
        "private lemma periodic_sieve_countermodel (s : Nat) : "
        "(1001 * s + 30) % 5 ≠ 2 := by\n  omega"
    )
    objective = "Prove whether the current finite sieve has a periodic complement."
    return LedgerEntry(
        spec=JobSpec(
            job_id=job_id,
            archetype="empirical",
            requester_role="orchestrator",
            objective=objective,
            budget=JobBudget(api_steps=2, wall_clock_s=30),
            deliverable="experiment_result",
            inputs={
                "target_symbol": target_symbol,
                "active_file": active_file,
                "route_key": "history-refresh:checked-obstruction",
                "route_signature": research_portfolio._stable_route_signature(
                    archetype="empirical",
                    target_symbol=target_symbol,
                    active_file=active_file,
                    objective=objective,
                ),
            },
            parent_job_id=job_id.rpartition(".")[0],
        ),
        state="done",
        finished_at="2026-07-17T12:01:00+00:00",
        result={
            "status": "done",
            "deliverable": {
                "status": "researched_not_closed",
                "new_proof_shape": (
                    "A checked periodic countermodel to the current finite sieve; "
                    "investigate the uncovered parametric family."
                ),
                "checked_helpers": [
                    {
                        "anchor_target_symbol": target_symbol,
                        "active_file": active_file,
                        "declaration": declaration,
                        "declaration_sha256": sha256(declaration.encode("utf-8")).hexdigest(),
                        "worker_check": {
                            "tool": "lean_incremental_check",
                            "action": "check_helper",
                            "valid_without_sorry": True,
                            "has_errors": False,
                            "has_sorry": False,
                            "replacement_matches_target": False,
                        },
                    }
                ],
            },
        },
    )


def _seed_epoch_cooled_lanes(
    *,
    service: dispatch_service.DispatchService,
    target_symbol: str,
    active_file: str,
    campaign_epoch: int,
    archetypes: tuple[str, ...] | None = None,
) -> list[LedgerEntry]:
    """Persist one consumed semantic-cooldown producer per research archetype."""
    entries: list[LedgerEntry] = []
    for archetype in archetypes or tuple(research_portfolio._ROUTE_FOCUSES):
        route_key, route_focus = research_portfolio._ROUTE_FOCUSES[archetype][0]
        spec = research_portfolio._job_spec(
            service,
            archetype=archetype,
            generation=1,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=2,
            route_key=route_key,
            route_focus=route_focus,
            campaign_epoch_number=campaign_epoch,
        )
        entry = LedgerEntry(
            spec=spec,
            state="done",
            finished_at="2026-07-17T12:00:00+00:00",
            result={
                "status": "done",
                "deliverable": {"summary": f"spent {archetype} direction"},
            },
            consumed=True,
        )
        service._save_entry(entry)
        entries.append(entry)
    return entries


def test_portfolio_records_finding_before_marking_ledger_consumed(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service, job_id = _seed_completed_research_job()
    original_consume = dispatch_service.DispatchService.consume
    observed_order: list[str] = []

    def assert_finding_is_durable_first(self, candidate_job_id):
        summary = dispatch_service.read_json_file(self._summary_path())
        assert [item["job_id"] for item in summary.get("research_findings") or []] == [
            candidate_job_id
        ]
        assert self._entry(candidate_job_id).consumed is False
        observed_order.append(candidate_job_id)
        return original_consume(self, candidate_job_id)

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "consume",
        assert_finding_is_durable_first,
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
        workers=0,
    )

    assert status["consumed"] == [job_id]
    assert observed_order == [job_id]
    assert service._entry(job_id).consumed is True
    persisted = dispatch_service.read_json_file(service._summary_path())["research_findings"][0]
    assert persisted["semantic_novelty"]["version"] == (
        research_route_context.SEMANTIC_NOVELTY_VERSION
    )


def test_portfolio_reservation_consumes_completion_without_refilling(monkeypatch, tmp_path):
    """A pending planner gets the next freed actor slot without losing findings."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service, job_id = _seed_completed_research_job()
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        lambda *_args, **_kwargs: pytest.fail("reserved capacity must not be refilled"),
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=4,
        workers=2,
        refill=False,
    )

    assert status["active"] == 0
    assert status["active_jobs"] == []
    assert status["launched"] == []
    assert status["consumed"] == [job_id]
    assert status["refill_deferred"] is True
    assert status["replacement_pending"] is True
    assert status["replacement_slots"] == 2
    assert service._entry(job_id).consumed is True
    persisted = workflow_json_io.read_json_file(service._summary_path())
    intent = persisted[research_portfolio.PENDING_REPLACEMENT_STATE_KEY]
    assert intent["reason"] == "planner_capacity_reserved"
    assert intent["trigger_job_ids"] == [job_id]


def test_provider_usage_limit_reaps_but_never_refills_portfolio(monkeypatch, tmp_path):
    """A reset-aware worker failure cannot become an immediate relaunch loop."""
    now_epoch = 1_784_496_783
    monkeypatch.setattr(
        research_portfolio,
        "_utc_now",
        lambda: datetime.fromtimestamp(now_epoch, tz=UTC),
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service, job_id = _seed_completed_research_job()
    retry_after = {
        "kind": "usage_limit_reached",
        "retry_after_seconds": 453097,
        "unavailable_until_epoch": 1784949880,
        "resets_at_epoch": 1784949879,
        "reported_resets_in_seconds": 453096,
        "timing_consistent": True,
        "timing_clamped": False,
        "source": "exception.body",
    }
    completed = service._entry(job_id)
    service._save_entry(
        replace(
            completed,
            state="failed",
            result={
                "status": "failed",
                "error": "Codex usage limit reached",
                "provider_retry_after": retry_after,
                "provider_globally_unavailable": True,
            },
            consumed=False,
        )
    )
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        lambda *_args, **_kwargs: pytest.fail("provider outage must suppress replacement"),
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=4,
        workers=2,
    )

    assert status["consumed"] == []
    assert status["launched"] == []
    assert status["provider_unavailable"] is True
    assert status["provider_retry_after"] == retry_after
    persisted = workflow_json_io.read_json_file(service._summary_path())
    intent = persisted[research_portfolio.PENDING_REPLACEMENT_STATE_KEY]
    assert intent["reason"] == "provider_usage_limit"


def test_foreground_provider_pause_overtakes_an_inflight_parent_poll(monkeypatch, tmp_path):
    """A pause published during route selection blocks the final process launch."""
    now_epoch = 1_700_000_000
    monkeypatch.setattr(
        research_portfolio,
        "_utc_now",
        lambda: datetime.fromtimestamp(now_epoch, tz=UTC),
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service, _job_id = _seed_completed_research_job()
    retry_after = {
        "kind": "usage_limit_reached",
        "retry_after_seconds": 601,
        "unavailable_until_epoch": now_epoch + 601,
    }
    pause_checks = 0

    def pause_published_during_poll(**_kwargs):
        nonlocal pause_checks
        pause_checks += 1
        return {} if pause_checks == 1 else retry_after

    monkeypatch.setattr(
        research_portfolio,
        "_active_campaign_provider_usage_limit",
        pause_published_during_poll,
    )
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "propose",
        lambda *_args, **_kwargs: pytest.fail(
            "a newly published provider pause must win before proposal"
        ),
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=4,
        workers=2,
    )

    assert pause_checks >= 2
    assert status["launched"] == []
    assert status["provider_unavailable"] is True
    assert status["provider_retry_after"] == retry_after
    persisted = workflow_json_io.read_json_file(service._summary_path())
    assert persisted[research_portfolio.PENDING_REPLACEMENT_STATE_KEY]["reason"] == (
        "provider_usage_limit"
    )


def test_provider_pause_after_proposal_kills_row_before_deploy(monkeypatch, tmp_path):
    """A reset winning after proposal leaves no open row or worker process."""
    now_epoch = 1_700_000_000
    monkeypatch.setattr(
        research_portfolio,
        "_utc_now",
        lambda: datetime.fromtimestamp(now_epoch, tz=UTC),
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service, original_job_id = _seed_completed_research_job()
    retry_after = {
        "kind": "usage_limit_reached",
        "retry_after_seconds": 601,
        "unavailable_until_epoch": now_epoch + 601,
    }
    pause_checks = 0

    def pause_published_after_proposal(**_kwargs):
        nonlocal pause_checks
        pause_checks += 1
        return retry_after if pause_checks >= 3 else {}

    monkeypatch.setattr(
        research_portfolio,
        "_active_campaign_provider_usage_limit",
        pause_published_after_proposal,
    )
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        lambda *_args, **_kwargs: pytest.fail("pause must kill proposal before deploy"),
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=4,
        workers=2,
    )

    assert pause_checks >= 3
    assert status["launched"] == []
    assert status["provider_retry_after"] == retry_after
    new_entries = [entry for entry in service.entries() if entry.spec.job_id != original_job_id]
    assert len(new_entries) == 1
    assert new_entries[0].state == "killed"


def test_locked_provider_admission_retires_proposal_without_process(monkeypatch, tmp_path):
    """The summary-locked final fence rejects even after both outer checks."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service, original_job_id = _seed_completed_research_job()
    monkeypatch.setattr(
        research_portfolio,
        "_active_campaign_provider_usage_limit",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        research_portfolio,
        "_summary_allows_provider_launch",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "_spawn_async_worker",
        lambda *_args, **_kwargs: pytest.fail("locked rejection must not spawn"),
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=4,
        workers=2,
    )

    assert status["launched"] == []
    assert status["provider_unavailable"] is True
    assert "provider_retry_after" not in status
    new_entries = [entry for entry in service.entries() if entry.spec.job_id != original_job_id]
    assert len(new_entries) == 1
    assert new_entries[0].state == "killed"


def test_planner_deferred_replacement_survives_epoch_refresh_and_refills_once(
    monkeypatch, tmp_path
):
    """A planner-reserved vacancy rolls forward once without duplicate routes."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-planner-epoch-refill"
    active_file = str(tmp_path / "Main.lean")
    service, completed_job_id = _seed_completed_research_job(
        campaign_id=campaign_id,
        active_file=active_file,
    )
    completed_entry = service._entry(completed_job_id)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda event, _message, **details: events.append((event, details)),
    )

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )

    deferred = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="demo",
        active_file=active_file,
        attempt_count=4,
        workers=2,
        refill=False,
    )
    repeated = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="demo",
        active_file=active_file,
        attempt_count=4,
        workers=2,
        refill=False,
    )

    assert deferred["consumed"] == [completed_job_id]
    assert deferred["replacement_pending"] is True
    assert repeated["consumed"] == []
    assert repeated["replacement_intent_id"] == deferred["replacement_intent_id"]
    assert [event for event, _details in events].count(
        "research-portfolio-replacement-deferred"
    ) == 1

    def set_fresh_epoch(summary):
        summary["campaign"] = {
            "campaign_id": campaign_id,
            "epoch": 2,
        }

    workflow_json_io.update_json_file(service._summary_path(), set_fresh_epoch)
    killed = research_portfolio.refresh_portfolio_for_epoch(
        campaign_id=campaign_id,
        target_symbol="demo",
        active_file=active_file,
        previous_epoch=1,
        new_epoch=2,
        reason="route-no-graph-progress",
    )

    assert killed == []
    refreshed_service = dispatch_service.DispatchService(root_job_id=campaign_id)
    replacements = [
        entry
        for entry in refreshed_service.entries()
        if entry.spec.job_id != completed_job_id and not entry.is_terminal()
    ]
    assert len(replacements) == 2
    replacement_signatures = {entry.spec.inputs["route_signature"] for entry in replacements}
    assert len(replacement_signatures) == 2
    assert completed_entry.spec.inputs["route_signature"] not in replacement_signatures
    assert {entry.spec.inputs["campaign_epoch"] for entry in replacements} == {2}
    persisted = workflow_json_io.read_json_file(service._summary_path())
    assert research_portfolio.PENDING_REPLACEMENT_STATE_KEY not in persisted
    assert [event for event, _details in events].count(
        "research-portfolio-replacement-fulfilled"
    ) == 1

    stable = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="demo",
        active_file=active_file,
        attempt_count=4,
        workers=2,
    )
    assert stable["launched"] == []
    assert stable["active"] == 2


def test_epoch_refresh_can_defer_pending_replacement(monkeypatch, tmp_path):
    """Preserve a replacement obligation without launching during construction."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "campaign-deferred-epoch")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-deferred-epoch"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    intent = {
        "intent_id": "replacement-demo",
        "campaign_id": campaign_id,
        "target_symbol": "demo",
        "active_file": active_file,
        "attempt_count": 4,
        "workers": 2,
    }
    workflow_json_io.update_json_file(
        service._summary_path(),
        lambda payload: payload.update({research_portfolio.PENDING_REPLACEMENT_STATE_KEY: intent}),
    )
    launches: list[dict] = []
    monkeypatch.setattr(
        research_portfolio,
        "_maintain_portfolio_once",
        lambda **kwargs: launches.append(kwargs) or {},
    )

    killed = research_portfolio.refresh_portfolio_for_epoch(
        campaign_id=campaign_id,
        target_symbol="demo",
        active_file=active_file,
        previous_epoch=1,
        new_epoch=2,
        reason="construction-debt",
        refill=False,
    )

    assert killed == []
    assert launches == []
    persisted = workflow_json_io.read_json_file(service._summary_path())
    assert persisted[research_portfolio.PENDING_REPLACEMENT_STATE_KEY]["intent_id"] == (
        "replacement-demo"
    )


def test_planner_race_rollback_retires_only_exact_replacement(monkeypatch, tmp_path):
    """Rollback frees the raced launch without preempting older research."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("activity unavailable")),
    )
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    job_ids: list[str] = []
    for generation in (1, 2):
        spec = research_portfolio._job_spec(
            service,
            archetype="deep_search" if generation == 1 else "empirical",
            generation=generation,
            target_symbol="demo",
            active_file="Main.lean",
            attempt_count=2,
        )
        service.propose(spec)
        service._transition(
            spec.job_id,
            "deployed",
            launch_nonce=f"rollback-nonce-{generation}",
            launch_started_at=dispatch_service._now_iso(),
            launch_attempt=1,
        )
        service._transition(
            spec.job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )
        job_ids.append(spec.job_id)

    result = research_portfolio.rollback_replacement_launches(
        campaign_id="campaign-demo",
        job_ids=[job_ids[1]],
    )

    assert result["released"] == [job_ids[1]]
    assert result["killed"] == [job_ids[1]]
    assert result["still_active"] == []
    assert service._entry(job_ids[0]).state == "running"
    assert service._entry(job_ids[1]).state == "killed"


def test_planner_race_rollback_rejects_foreign_prover_and_legacy_before_signal(
    monkeypatch, tmp_path
):
    """Untrusted job IDs cannot widen planner rollback termination authority."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    current = dispatch_service.DispatchService(root_job_id="campaign-current")
    foreign = dispatch_service.DispatchService(root_job_id="campaign-foreign")

    foreign_spec = research_portfolio._job_spec(
        foreign,
        archetype="deep_search",
        generation=1,
        target_symbol="other",
        active_file="Other.lean",
        attempt_count=0,
    )
    foreign.propose(foreign_spec)
    foreign._transition(
        foreign_spec.job_id,
        "deployed",
        launch_nonce="foreign-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )

    prover_spec = JobSpec(
        job_id=current.mint_job_id("prover", role="orchestrator"),
        archetype="prover",
        requester_role="orchestrator",
        objective="Prove a protected foreground helper.",
        budget=JobBudget(api_steps=10, wall_clock_s=60),
        deliverable="prove_outcome",
        inputs={"campaign_id": "campaign-current"},
    )
    current.propose(prover_spec)
    current._transition(
        prover_spec.job_id,
        "deployed",
        launch_nonce="prover-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )

    legacy_spec = research_portfolio._job_spec(
        current,
        archetype="empirical",
        generation=2,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=2,
    )
    current.propose(legacy_spec)
    current._transition(legacy_spec.job_id, "deployed")
    protected_ids = [foreign_spec.job_id, prover_spec.job_id, legacy_spec.job_id]
    monkeypatch.setattr(
        dispatch_service,
        "_terminate_dispatch_process_and_wait",
        lambda _entry: pytest.fail("unauthorized rollback reached process termination"),
    )

    result = research_portfolio.rollback_replacement_launches(
        campaign_id="campaign-current",
        job_ids=protected_ids,
    )

    assert result == {
        "requested": protected_ids,
        "released": [],
        "killed": [],
        "still_active": protected_ids,
    }
    assert all(current._entry(job_id).state == "deployed" for job_id in protected_ids)


def test_saturated_portfolio_releases_only_newest_job_for_planner(monkeypatch, tmp_path):
    """A plan route gets one actor slot without exceeding capacity or touching foreground work."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    job_ids: list[str] = []
    for generation, archetype in ((1, "deep_search"), (2, "empirical")):
        spec = research_portfolio._job_spec(
            service,
            archetype=archetype,
            generation=generation,
            target_symbol="demo",
            active_file="Main.lean",
            attempt_count=2,
        )
        service.propose(spec)
        service._transition(
            spec.job_id,
            "deployed",
            launch_nonce=f"nonce-{generation}",
            launch_started_at=dispatch_service._now_iso(),
            launch_attempt=1,
        )
        job_ids.append(spec.job_id)

    result = research_portfolio.reserve_planner_actor_slot(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        workers=2,
    )

    assert result["capacity"] == 2
    assert result["active_before"] == job_ids
    assert result["requested"] == [job_ids[1]]
    assert result["released"] == [job_ids[1]]
    assert result["killed"] == [job_ids[1]]
    assert result["active_after"] == [job_ids[0]]
    assert result["slot_reserved"] is True
    assert result["foreground_untouched"] is True
    assert service._entry(job_ids[0]).state == "deployed"
    assert service._entry(job_ids[1]).state == "killed"
    assert events[0][0][0] == "research-portfolio-planner-preemption"


def test_planner_reservation_does_not_preempt_when_slot_is_already_free(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    spec = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    service.propose(spec)
    service._transition(spec.job_id, "deployed")

    result = research_portfolio.reserve_planner_actor_slot(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        workers=2,
    )

    assert result["requested"] == []
    assert result["active_after"] == [spec.job_id]
    assert result["slot_reserved"] is True
    assert service._entry(spec.job_id).state == "deployed"


def test_planner_reservation_does_not_count_proposed_job_as_actor(monkeypatch, tmp_path):
    """Ledger-only proposals do not own provider capacity or displace live work."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    proposed = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    running = research_portfolio._job_spec(
        service,
        archetype="empirical",
        generation=2,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    service.propose(proposed)
    service.propose(running)
    service._transition(running.job_id, "deployed")

    result = research_portfolio.reserve_planner_actor_slot(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        workers=2,
    )

    assert result["active_before"] == [running.job_id]
    assert result["requested"] == []
    assert result["slot_reserved"] is True
    assert service._entry(proposed.job_id).state == "proposed"
    assert service._entry(running.job_id).state == "deployed"


def test_planner_reservation_ignores_only_proposed_jobs(monkeypatch, tmp_path):
    """A proposal-only ledger leaves every configured actor slot available."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    proposed = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    service.propose(proposed)

    result = research_portfolio.reserve_planner_actor_slot(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        workers=1,
    )

    assert result["active_before"] == []
    assert result["requested"] == []
    assert result["slot_reserved"] is True
    assert service._entry(proposed.job_id).state == "proposed"


def test_planner_reservation_never_preempts_foreign_campaign(monkeypatch, tmp_path):
    """A foreign worker is a capacity blocker, never a cancellation target."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    current = dispatch_service.DispatchService(root_job_id="campaign-current")
    owned = research_portfolio._job_spec(
        current,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    current.propose(owned)
    current._transition(
        owned.job_id,
        "deployed",
        launch_nonce="owned-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )

    foreign_service = dispatch_service.DispatchService(root_job_id="campaign-foreign")
    foreign = research_portfolio._job_spec(
        foreign_service,
        archetype="empirical",
        generation=2,
        target_symbol="other",
        active_file="Other.lean",
        attempt_count=0,
    )
    foreign_service.propose(foreign)
    foreign_service._transition(
        foreign.job_id,
        "deployed",
        launch_nonce="foreign-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )

    result = research_portfolio.reserve_planner_actor_slot(
        campaign_id="campaign-current",
        target_symbol="demo",
        active_file="Main.lean",
        workers=2,
    )

    assert result["active_before"] == [owned.job_id, foreign.job_id]
    assert result["requested"] == [owned.job_id]
    assert result["slot_reserved"] is True
    assert current._entry(owned.job_id).state == "killed"
    assert current._entry(foreign.job_id).state == "deployed"


def test_planner_reservation_kills_nothing_when_protected_actors_saturate(monkeypatch, tmp_path):
    """An unattainable slot never destroys research in a futile preemption."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    current = dispatch_service.DispatchService(root_job_id="campaign-current")
    owned = research_portfolio._job_spec(
        current,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    current.propose(owned)
    current._transition(
        owned.job_id,
        "deployed",
        launch_nonce="owned-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )

    foreign_service = dispatch_service.DispatchService(root_job_id="campaign-foreign")
    foreign_ids: list[str] = []
    for generation, archetype in ((1, "deep_search"), (2, "empirical")):
        foreign = research_portfolio._job_spec(
            foreign_service,
            archetype=archetype,
            generation=generation,
            target_symbol="other",
            active_file="Other.lean",
            attempt_count=0,
        )
        foreign_service.propose(foreign)
        foreign_service._transition(
            foreign.job_id,
            "deployed",
            launch_nonce=f"foreign-{generation}",
            launch_started_at=dispatch_service._now_iso(),
            launch_attempt=1,
        )
        foreign_ids.append(foreign.job_id)

    result = research_portfolio.reserve_planner_actor_slot(
        campaign_id="campaign-current",
        target_symbol="demo",
        active_file="Main.lean",
        workers=2,
    )

    assert result["active_before"] == [owned.job_id, *foreign_ids]
    assert result["requested"] == []
    assert result["killed"] == []
    assert result["slot_reserved"] is False
    assert current._entry(owned.job_id).state == "deployed"
    assert all(current._entry(job_id).state == "deployed" for job_id in foreign_ids)


def test_planner_reservation_never_preempts_same_campaign_prover(monkeypatch, tmp_path):
    """Planner reservation leaves proof-producing foreground jobs untouched."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    portfolio = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    service.propose(portfolio)
    service._transition(
        portfolio.job_id,
        "deployed",
        launch_nonce="portfolio-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )
    prover = JobSpec(
        job_id=service.mint_job_id("prover", role="orchestrator"),
        archetype="prover",
        requester_role="orchestrator",
        objective="Prove a useful foreground helper.",
        budget=JobBudget(api_steps=10, wall_clock_s=60),
        deliverable="prove_outcome",
        inputs={"campaign_id": "campaign-demo"},
    )
    service.propose(prover)
    service._transition(
        prover.job_id,
        "deployed",
        launch_nonce="prover-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )

    result = research_portfolio.reserve_planner_actor_slot(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        workers=2,
    )

    assert result["requested"] == [portfolio.job_id]
    assert result["slot_reserved"] is True
    assert service._entry(portfolio.job_id).state == "killed"
    assert service._entry(prover.job_id).state == "deployed"


def test_planner_reservation_skips_unverifiable_newest_worker(monkeypatch, tmp_path):
    """An unsafe legacy blocker cannot hide an older nonce-bound reservation."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    safe = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    service.propose(safe)
    service._transition(
        safe.job_id,
        "deployed",
        launch_nonce="safe-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )
    legacy = research_portfolio._job_spec(
        service,
        archetype="empirical",
        generation=2,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    service.propose(legacy)
    service._transition(legacy.job_id, "deployed")

    result = research_portfolio.reserve_planner_actor_slot(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        workers=2,
    )

    assert result["requested"] == [safe.job_id]
    assert result["slot_reserved"] is True
    assert service._entry(safe.job_id).state == "killed"
    assert service._entry(legacy.job_id).state == "deployed"


def test_planner_race_rollback_keeps_capacity_when_exact_process_survives(monkeypatch, tmp_path):
    """A failed exact retirement leaves the replacement durably nonterminal."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    spec = research_portfolio._job_spec(
        service,
        archetype="empirical",
        generation=2,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=2,
    )
    service.propose(spec)
    service._transition(
        spec.job_id,
        "deployed",
        launch_nonce="survives-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )
    running = service._transition(
        spec.job_id,
        "running",
        started_at=dispatch_service._now_iso(),
    )
    service._save_entry(
        replace(
            running,
            process_id=4242,
            process_group_id=4242,
            process_session_id=4242,
            process_token_sha256="a" * 64,
        )
    )
    monkeypatch.setattr(
        dispatch_service,
        "_terminate_dispatch_process_and_wait",
        lambda _entry: False,
    )

    result = research_portfolio.rollback_replacement_launches(
        campaign_id="campaign-demo",
        job_ids=[spec.job_id],
    )

    assert result == {
        "requested": [spec.job_id],
        "released": [],
        "killed": [],
        "still_active": [spec.job_id],
    }
    assert service._entry(spec.job_id).state == "running"


@pytest.mark.skipif(os.name != "posix", reason="dispatch process groups require POSIX")
def test_planner_race_rollback_escalates_until_exact_process_exits(monkeypatch, tmp_path):
    """A TERM-resistant replacement is SIGKILLed before capacity is released."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    token = "planner-race-term-resistant-worker"
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
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    spec = research_portfolio._job_spec(
        service,
        archetype="empirical",
        generation=2,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=2,
    )
    service.propose(spec)
    service._transition(
        spec.job_id,
        "deployed",
        launch_nonce="term-resistant-nonce",
        launch_started_at=dispatch_service._now_iso(),
        launch_attempt=1,
    )
    running = service._transition(
        spec.job_id,
        "running",
        started_at=dispatch_service._now_iso(),
    )
    running = replace(
        running,
        process_id=process.pid,
        process_group_id=os.getpgid(process.pid),
        process_session_id=os.getsid(process.pid),
        process_token_sha256=process_token_sha256(token),
    )
    service._save_entry(running)
    monkeypatch.setattr(dispatch_service, "ASYNC_LAUNCH_TERMINATION_GRACE_S", 0.1)
    monkeypatch.setattr(dispatch_service, "ASYNC_LAUNCH_TERMINATION_POLL_S", 0.01)
    # Sandboxed macOS can deny ``ps eww`` token lookup. Preserve the real
    # PID/session exit probe while making ownership validation deterministic.
    monkeypatch.setattr(
        dispatch_service,
        "process_identity_matches",
        lambda identity: identity.pid == process.pid,
    )

    try:
        result = research_portfolio.rollback_replacement_launches(
            campaign_id="campaign-demo",
            job_ids=[spec.job_id],
        )

        assert result["released"] == [spec.job_id]
        assert result["killed"] == [spec.job_id]
        assert result["still_active"] == []
        assert service._entry(spec.job_id).state == "killed"
        assert dispatch_service._dispatch_process_identity_has_exited(running)
    finally:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=5)


def test_portfolio_backpressures_refill_at_undelivered_finding_cap(monkeypatch, tmp_path):
    """Provider downtime cannot drive an unbounded completed-finding stream."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    target_symbol = "erdos_242"
    active_file = str(tmp_path / "Erdos242.lean")
    findings = [
        {
            "job_id": f"campaign-backpressure.orchestrator.ds-{index:03d}",
            "campaign_id": "campaign-backpressure",
            "target_symbol": target_symbol,
            "active_file": active_file,
            "deliverable": {"summary": f"pending evidence {index}"},
        }
        for index in range(research_findings.DELIVERY_BACKLOG_CAP)
    ]
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {"research_findings": findings},
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-backpressure",
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=4,
        workers=2,
    )

    assert status == {
        "active": 0,
        "active_jobs": [],
        "launched": [],
        "consumed": [],
        "delivery_backpressure": True,
        "delivery_backlog": research_findings.DELIVERY_BACKLOG_CAP,
    }
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    backpressure = persisted[research_portfolio.DELIVERY_BACKPRESSURE_STATE_KEY]
    assert backpressure == {
        "active": True,
        "campaign_id": "campaign-backpressure",
        "scope": "active_delivery_target",
        "target_symbol": target_symbol,
        "active_file": active_file,
        "backlog": research_findings.DELIVERY_BACKLOG_CAP,
        "cap": research_findings.DELIVERY_BACKLOG_CAP,
        "updated_at": backpressure["updated_at"],
    }


def test_inactive_archived_obligations_do_not_backpressure_new_target_after_restart(
    monkeypatch, tmp_path
):
    """Old scopes remain lossless in the ledger without occupying the active window."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-active-scope"
    active_file = str(tmp_path / "Main.lean")
    entries = [
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix=f"ds-{index:03d}",
            target_symbol=f"old_target_{index % 4}",
            active_file=active_file,
            summary=f"pending evidence {index}",
            created_at=f"2026-01-01T00:{index:02d}:00+00:00",
        )
        for index in range(research_findings.DELIVERY_BACKLOG_CAP + 8)
    ]
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": [entry.to_mapping() for entry in entries],
            "research_findings": [
                research_findings.build_finding_record(entry, entry.result, entries=entries)
                for entry in entries
            ],
        },
    )

    report = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol="brand_new_target",
        active_file=active_file,
    )

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    first = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="brand_new_target",
        active_file=active_file,
        attempt_count=0,
        workers=1,
    )

    assert report["dematerialized"] == research_findings.DELIVERY_BACKLOG_CAP + 8
    assert report["active_delivery_backlog"] == 0
    assert first.get("delivery_backpressure") is not True
    assert len(first["launched"]) == 1
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    assert persisted["research_findings"] == []
    assert research_portfolio.DELIVERY_BACKPRESSURE_STATE_KEY not in persisted
    assert len(persisted[research_findings.FINDING_MIGRATION_KEY]["records"]) == len(entries)

    restarted = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol="another_new_target",
        active_file=active_file,
    )
    assert restarted["active_delivery_backlog"] == 0
    assert restarted["materialized"] == 0


def test_split_ancestor_backlog_reserves_capacity_for_new_child_research(monkeypatch, tmp_path):
    """Inherited evidence cannot occupy the child's entire research window."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-split-capacity"
    active_file = str(tmp_path / "Main.lean")
    parent = "parent_target"
    child = "child_target"
    parent_id = plan_state.node_id_for(parent, active_file)
    child_id = plan_state.node_id_for(child, active_file)
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(id=parent_id, name=parent, file=active_file),
            plan_state.GraphNode(id=child_id, name=child, file=active_file),
        ),
        edges=(plan_state.GraphEdge(source=child_id, target=parent_id, kind="split_of"),),
    )
    entries = [
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix=f"ds-{index:03d}",
            target_symbol=parent,
            active_file=active_file,
            summary=f"inherited evidence {index}",
            created_at=f"2026-01-01T00:{index:02d}:00+00:00",
        )
        for index in range(research_findings.DELIVERY_BACKLOG_CAP + 8)
    ]
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": [entry.to_mapping() for entry in entries],
            "research_findings": [
                research_findings.build_finding_record(entry, entry.result, entries=entries)
                for entry in entries[: research_findings.DELIVERY_BACKLOG_CAP]
            ],
        },
    )

    first = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=child,
        active_file=active_file,
        blueprint=blueprint,
    )

    assert first["active_delivery_backlog"] == research_findings.INHERITED_DELIVERY_BACKLOG_CAP
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    undelivered = research_findings.relevant_findings(
        persisted,
        target_symbol=child,
        active_file=active_file,
        blueprint=blueprint,
        limit=None,
    )
    assert len(undelivered) == research_findings.INHERITED_DELIVERY_BACKLOG_CAP

    delivered = [
        research_findings.delivery_key(finding["job_id"], child) for finding in undelivered[:4]
    ]
    persisted[research_findings.DELIVERY_STATE_KEY] = {
        "campaign_id": campaign_id,
        "research_findings_delivered": delivered,
    }
    workflow_json_io.write_json_file(research_findings._summary_path(), persisted)
    second = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=child,
        active_file=active_file,
        blueprint=blueprint,
    )
    assert second["active_delivery_backlog"] == research_findings.INHERITED_DELIVERY_BACKLOG_CAP

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(plan_state, "load_blueprint", lambda: blueprint)
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    status = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=child,
        active_file=active_file,
        attempt_count=0,
        workers=1,
    )

    assert status.get("delivery_backpressure") is not True
    assert len(status["launched"]) == 1


def test_exact_child_findings_precede_inherited_findings():
    """Fresh child evidence reaches the foreground before ancestor history."""
    active_file = "/tmp/Main.lean"
    parent = "parent_target"
    child = "child_target"
    parent_id = plan_state.node_id_for(parent, active_file)
    child_id = plan_state.node_id_for(child, active_file)
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(id=parent_id, name=parent, file=active_file),
            plan_state.GraphNode(id=child_id, name=child, file=active_file),
        ),
        edges=(plan_state.GraphEdge(source=child_id, target=parent_id, kind="split_of"),),
    )
    summary = {
        "research_findings": [
            {
                "job_id": "campaign.ds-parent",
                "target_symbol": parent,
                "active_file": active_file,
                "deliverable": {"summary": "older inherited route"},
            },
            {
                "job_id": "campaign.ds-child",
                "target_symbol": child,
                "active_file": active_file,
                "deliverable": {"summary": "fresh exact-child route"},
            },
        ]
    }

    selected = research_findings.relevant_findings(
        summary,
        target_symbol=child,
        active_file=active_file,
        blueprint=blueprint,
        limit=None,
    )
    limited = research_findings.relevant_findings(
        summary,
        target_symbol=child,
        active_file=active_file,
        blueprint=blueprint,
        limit=1,
    )

    assert [finding["job_id"] for finding in selected] == [
        "campaign.ds-child",
        "campaign.ds-parent",
    ]
    assert [finding["job_id"] for finding in limited] == ["campaign.ds-child"]


def test_deferred_inherited_result_emits_archive_activity(monkeypatch, tmp_path):
    """A consumed deferred result remains visible as an archive transition."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-archive-observability"
    active_file = str(tmp_path / "Main.lean")
    parent = "parent_target"
    child = "child_target"
    parent_id = plan_state.node_id_for(parent, active_file)
    child_id = plan_state.node_id_for(child, active_file)
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(id=parent_id, name=parent, file=active_file),
            plan_state.GraphNode(id=child_id, name=child, file=active_file),
        ),
        edges=(plan_state.GraphEdge(source=child_id, target=parent_id, kind="split_of"),),
    )
    visible = [
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix=f"ds-{index:03d}",
            target_symbol=parent,
            active_file=active_file,
            summary=f"visible inherited evidence {index}",
        )
        for index in range(research_findings.INHERITED_DELIVERY_BACKLOG_CAP)
    ]
    deferred = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="ds-deferred",
        target_symbol=parent,
        active_file=active_file,
        summary="new deferred inherited evidence",
    )
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "research_findings": [
                research_findings.build_finding_record(entry, entry.result, entries=visible)
                for entry in visible
            ]
        },
    )
    activities: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda event, _message, **details: activities.append((event, details)),
    )

    materialized = research_portfolio._record_finding(
        deferred,
        deferred.result,
        entries=[*visible, deferred],
        delivery_target_symbol=child,
        delivery_active_file=active_file,
        blueprint=blueprint,
    )

    assert materialized is False
    assert activities == [
        (
            "research-finding-archived",
            {
                "archive_event_key": (
                    f"research-finding-archived:{campaign_id}:{deferred.spec.job_id}"
                ),
                "job_id": deferred.spec.job_id,
                "campaign_id": campaign_id,
                "archetype": "deep_search",
                "target_symbol": parent,
                "active_file": active_file,
                "archive_status": "deferred_capacity",
                "archive_reason": "inherited delivery window is at capacity",
            },
        )
    ]
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    record = persisted[research_findings.FINDING_MIGRATION_KEY]["records"][deferred.spec.job_id]
    assert record["status"] == "deferred_capacity"


def test_deferred_archive_activity_retries_after_post_commit_append_failure(monkeypatch, tmp_path):
    """A failed activity append leaves the unconsumed result observable on retry."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-archive-retry"
    active_file = str(tmp_path / "Main.lean")
    entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="ds-retry",
        target_symbol="inactive_parent",
        active_file=active_file,
        summary="durable deferred evidence",
    )
    calls: list[tuple[str, dict]] = []

    def fail_append(*_args, **_kwargs):
        raise OSError("activity stream unavailable")

    monkeypatch.setattr(research_portfolio, "append_workflow_activity", fail_append)
    with pytest.raises(OSError, match="activity stream unavailable"):
        research_portfolio._record_finding(
            entry,
            entry.result,
            entries=[entry],
            delivery_target_symbol="current_child",
            delivery_active_file=active_file,
        )

    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda event, _message, **details: calls.append((event, details)),
    )
    materialized = research_portfolio._record_finding(
        entry,
        entry.result,
        entries=[entry],
        delivery_target_symbol="current_child",
        delivery_active_file=active_file,
    )

    assert materialized is False
    assert calls[0][0] == "research-finding-archived"
    assert calls[0][1]["archive_event_key"] == (
        f"research-finding-archived:{campaign_id}:{entry.spec.job_id}"
    )


def test_mixed_timeout_mathematical_finding_is_materialized(monkeypatch, tmp_path):
    """A nested advisor timeout cannot archive a substantive research report."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-mixed-timeout"
    target_symbol = "erdos_242_residual_mod_seven_eq_one_normalized"
    active_file = str(tmp_path / "242.lean")
    entry = replace(
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix="ds-mixed",
            target_symbol=target_symbol,
            active_file=active_file,
            summary="mixed mathematical report",
        ),
        result={
            "status": "done",
            "deliverable": _mixed_timeout_mathematical_deliverable(),
        },
        consumed=False,
    )
    activities: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda event, _message, **details: activities.append((event, details)),
    )

    materialized = research_portfolio._record_finding(
        entry,
        entry.result,
        entries=[entry],
        delivery_target_symbol=target_symbol,
        delivery_active_file=active_file,
    )

    assert materialized is True
    assert [event for event, _details in activities] == ["research-finding"]
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    finding = next(
        item for item in persisted["research_findings"] if item["job_id"] == entry.spec.job_id
    )
    assert finding["semantic_novelty"]["classification"] == "novel"
    archive = persisted[research_findings.FINDING_MIGRATION_KEY]["records"][entry.spec.job_id]
    assert archive["status"] == "materialized_current"


def test_assignment_migration_rehydrates_old_cached_mixed_timeout_false_negative(
    monkeypatch, tmp_path
):
    """A substance-version bump recomputes a consumed mixed-evidence result."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-rehydrate-mixed-timeout"
    target_symbol = "erdos_242_residual_mod_seven_eq_one_normalized"
    active_path = tmp_path / "242.lean"
    active_path.write_text("theorem placeholder : True := by trivial\n", encoding="utf-8")
    active_file = str(active_path)
    entry = replace(
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix="ds-cached-false",
            target_symbol=target_symbol,
            active_file=active_file,
            summary="cached mixed mathematical report",
        ),
        result={
            "status": "done",
            "deliverable": _mixed_timeout_mathematical_deliverable(),
        },
    )
    stale_record = research_findings._archive_record(
        entry,
        status="archived_non_substantive",
        reason="ledger result has no mathematical evidence",
        substantive=False,
    )
    stale_record["substance_version"] = research_findings.FINDING_SUBSTANCE_VERSION - 1
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": [entry.to_mapping()],
            "research_findings": [],
            research_findings.FINDING_MIGRATION_KEY: {
                "version": research_findings.FINDING_ARCHIVE_VERSION,
                "campaign_id": campaign_id,
                "records": {entry.spec.job_id: stale_record},
            },
        },
    )

    report = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert report["reconstructed_job_ids"] == [entry.spec.job_id]
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    assert [item["job_id"] for item in persisted["research_findings"]] == [entry.spec.job_id]
    record = persisted[research_findings.FINDING_MIGRATION_KEY]["records"][entry.spec.job_id]
    assert record["substantive"] is True
    assert record["substance_version"] == research_findings.FINDING_SUBSTANCE_VERSION
    assert record["status"] == "materialized_current"


def test_assignment_migration_recovers_only_substantive_undelivered_consumed_results(
    monkeypatch, tmp_path
):
    """A stopped capped summary recovers its active evidence without reviving old targets."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-stopped"
    active_target = "erdos_242_residual_mod_seven_eq_zero"
    active_file = str(tmp_path / "FormalConjectures" / "242.lean")

    def consumed_entry(
        suffix: str,
        *,
        target: str = active_target,
        campaign: str = campaign_id,
        deliverable: dict,
    ) -> LedgerEntry:
        job_id = f"{campaign}.orchestrator.{suffix}"
        return LedgerEntry(
            spec=JobSpec(
                job_id=job_id,
                archetype="deep_search",
                requester_role="orchestrator",
                objective=f"research {target}",
                budget=JobBudget(api_steps=2, wall_clock_s=30),
                deliverable="findings_report",
                inputs={
                    "campaign_id": campaign,
                    "target_symbol": target,
                    "active_file": active_file,
                },
                parent_job_id=f"{campaign}.orchestrator",
            ),
            state="done",
            result={"status": "done", "deliverable": deliverable},
            consumed=True,
        )

    recoverable = consumed_entry(
        "ds-recover",
        deliverable={
            "status": "candidate_verified",
            "verification": "worker claimed a check but omitted exact proof text",
            "summary": "derive the t % 17 = 9 branch",
        },
    )
    already_delivered = consumed_entry(
        "ds-delivered",
        deliverable={"summary": "already reached foreground"},
    )
    already_present = consumed_entry(
        "ds-present",
        deliverable={"summary": "already durable"},
    )
    operational = consumed_entry(
        "ds-timeout",
        deliverable={"status": "failed", "error": "provider timed out"},
    )
    empty = consumed_entry("ds-empty", deliverable={"status": "done"})
    inactive = consumed_entry(
        "ds-inactive",
        target="resolved_old_target",
        deliverable={"summary": "old target evidence"},
    )
    other_campaign = consumed_entry(
        "ds-other",
        campaign="campaign-other",
        deliverable={"summary": "unrelated campaign evidence"},
    )

    delivered_history = [
        {
            "job_id": f"{campaign_id}.orchestrator.em-history-{index:03d}",
            "campaign_id": campaign_id,
            "target_symbol": "resolved_old_target",
            "active_file": active_file,
            "deliverable": {"summary": f"delivered history {index}"},
        }
        for index in range(research_findings.DURABLE_FINDING_HISTORY_CAP - 1)
    ]
    delivered_markers = [
        research_findings.delivery_key(item["job_id"], item["target_symbol"])
        for item in delivered_history
    ]
    delivered_markers.append(
        research_findings.delivery_key(already_delivered.spec.job_id, active_target)
    )
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": [
                entry.to_mapping()
                for entry in (
                    recoverable,
                    already_delivered,
                    already_present,
                    operational,
                    empty,
                    inactive,
                    other_campaign,
                )
            ],
            "research_findings": [
                *delivered_history,
                research_findings.build_finding_record(
                    already_present,
                    already_present.result,
                    entries=[already_present],
                ),
            ],
            research_findings.DELIVERY_STATE_KEY: {
                "campaign_id": campaign_id,
                "research_findings_delivered": delivered_markers,
            },
        },
    )

    first = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=active_target,
        active_file=active_file,
    )
    second = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=active_target,
        active_file=active_file,
    )

    assert first["reconstructed_job_ids"] == [recoverable.spec.job_id]
    assert first["already_present"] == 1
    assert first["already_delivered"] == 1
    assert first["skipped_non_substantive"] == 2
    assert second["reconstructed"] == 0
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    persisted_ids = [item["job_id"] for item in persisted["research_findings"]]
    assert recoverable.spec.job_id in persisted_ids
    assert inactive.spec.job_id not in persisted_ids
    assert other_campaign.spec.job_id not in persisted_ids
    assert operational.spec.job_id not in persisted_ids
    assert empty.spec.job_id not in persisted_ids
    # The synthetic delivered-history rows have no ledger payload. They are
    # quarantined correctness state and therefore survive the nominal history cap.
    assert len(persisted_ids) == research_findings.DURABLE_FINDING_HISTORY_CAP + 1
    assert {item["job_id"] for item in delivered_history}.issubset(persisted_ids)
    recovered = next(
        item for item in persisted["research_findings"] if item["job_id"] == recoverable.spec.job_id
    )
    assert recovered["campaign_id"] == campaign_id
    assert recovered["deliverable"]["status"] == "incomplete_unverified"
    assert recovered["deliverable"]["checked_replacements"] == []
    # The archive keeps the last state-changing report. The second no-op is
    # returned to the caller but does not rewrite a large shared summary just
    # to replace this observational counter with zero.
    assert persisted[research_findings.FINDING_MIGRATION_KEY]["reconstructed"] == 1
    assert second["state_changed"] is False


def test_archive_materializes_only_twelve_active_and_defers_two_inactive_copies(
    monkeypatch, tmp_path
):
    """A global 66-result archive exposes only the 12 findings due to this scope."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-erdos242"
    active_target = "erdos_242_residual_mod_seven_eq_zero"
    active_file_path = tmp_path / "FormalConjectures" / "ErdosProblems" / "242.lean"
    active_file_path.parent.mkdir(parents=True)
    source = "theorem erdos_242 : True := by\n  sorry\n"
    active_file_path.write_text(source, encoding="utf-8")
    active_file = str(active_file_path)
    active_entries = [
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix=f"ds-active-{index:03d}",
            target_symbol=active_target,
            active_file=active_file,
            summary=f"active evidence {index}",
            created_at=f"2026-01-01T00:{index:02d}:00+00:00",
        )
        for index in range(12)
    ]
    inactive_entries = [
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix=f"ds-inactive-{index:03d}",
            target_symbol=f"inactive_target_{index % 3}",
            active_file=active_file,
            summary=f"inactive evidence {index}",
            created_at=f"2026-01-02T00:{index:02d}:00+00:00",
        )
        for index in range(54)
    ]
    entries = [*active_entries, *inactive_entries]
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": [entry.to_mapping() for entry in entries],
            "research_findings": [
                *[
                    research_findings.build_finding_record(
                        entry,
                        entry.result,
                        entries=entries,
                    )
                    for entry in active_entries
                ],
                *[
                    research_findings.build_finding_record(
                        entry,
                        entry.result,
                        entries=entries,
                    )
                    for entry in inactive_entries[:2]
                ],
            ],
        },
    )

    before_source = sha256(active_file_path.read_bytes()).hexdigest()
    report = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=active_target,
        active_file=active_file,
    )
    after_source = sha256(active_file_path.read_bytes()).hexdigest()

    assert report["materialized"] == 0
    assert report["dematerialized"] == 2
    assert report["active_delivery_backlog"] == 12
    assert report["archive_records"] == 66
    assert before_source == after_source
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    assert len(persisted["research_findings"]) == 12
    assert {finding["target_symbol"] for finding in persisted["research_findings"]} == {
        active_target
    }
    assert (
        research_findings.campaign_delivery_backlog_count(
            persisted,
            campaign_id=campaign_id,
            target_symbol=active_target,
            active_file=active_file,
        )
        == 12
    )
    records = persisted[research_findings.FINDING_MIGRATION_KEY]["records"]
    assert all(
        records[entry.spec.job_id]["status"] == "deferred_inactive"
        for entry in inactive_entries[:2]
    )
    assert all(
        records[entry.spec.job_id]["status"] == "archived_available"
        for entry in inactive_entries[2:]
    )


def test_archive_pages_more_than_thirty_two_findings_oldest_first_after_ack(monkeypatch, tmp_path):
    """Acknowledging one full page deterministically exposes the next page."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-paging"
    target = "demo"
    active_file = str(tmp_path / "Main.lean")
    entries = [
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix=f"ds-{index:03d}",
            target_symbol=target,
            active_file=active_file,
            summary=f"evidence page item {index}",
            created_at=f"2026-01-01T00:{index:02d}:00+00:00",
        )
        for index in range(40)
    ]
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {"dispatch_ledger": [entry.to_mapping() for entry in entries]},
    )

    first = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )
    first_ids = [entry.spec.job_id for entry in entries[:32]]
    assert first["materialized_job_ids"] == first_ids
    assert first["deferred_capacity"] == 8
    assert first["active_delivery_backlog"] == 32
    assert first["archive_updates"] == 40

    summary = workflow_json_io.read_json_file(research_findings._summary_path())
    summary[research_findings.DELIVERY_STATE_KEY] = {
        "campaign_id": campaign_id,
        "research_findings_delivered": [
            research_findings.delivery_key(job_id, target) for job_id in first_ids
        ],
    }
    workflow_json_io.write_json_file(research_findings._summary_path(), summary)

    second = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )
    expected_second_page = [entry.spec.job_id for entry in entries[32:]]
    assert second["materialized_job_ids"] == expected_second_page
    assert second["deferred_capacity"] == 0
    assert second["active_delivery_backlog"] == 8
    restarted = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )
    assert restarted["materialized"] == 0
    assert restarted["active_delivery_backlog"] == 8
    assert restarted["archive_updates"] == 0


def test_descendant_ack_does_not_ack_origin_and_reopened_parent_rematerializes(
    monkeypatch, tmp_path
):
    """A child receipt suppresses only that child, never its parent obligation."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-split"
    active_file = str(tmp_path / "Main.lean")
    parent = "erdos_242"
    child = "erdos_242_residual"
    unrelated = "other_target"
    parent_id = plan_state.node_id_for(parent, active_file)
    child_id = plan_state.node_id_for(child, active_file)
    unrelated_id = plan_state.node_id_for(unrelated, active_file)
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(id=parent_id, name=parent, file=active_file),
            plan_state.GraphNode(id=child_id, name=child, file=active_file),
            plan_state.GraphNode(id=unrelated_id, name=unrelated, file=active_file),
        ),
        edges=(plan_state.GraphEdge(source=child_id, target=parent_id, kind="split_of"),),
    )
    entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="ds-parent",
        target_symbol=parent,
        active_file=active_file,
        summary="parent evidence inherited by the split child",
    )
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {"dispatch_ledger": [entry.to_mapping()]},
    )

    child_report = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=child,
        active_file=active_file,
        blueprint=blueprint,
    )
    assert child_report["materialized_job_ids"] == [entry.spec.job_id]
    summary = workflow_json_io.read_json_file(research_findings._summary_path())
    child_marker = research_findings.delivery_key(entry.spec.job_id, child)
    summary[research_findings.DELIVERY_STATE_KEY] = {
        "campaign_id": campaign_id,
        "research_findings_delivered": [child_marker],
    }
    workflow_json_io.write_json_file(research_findings._summary_path(), summary)

    switched = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=unrelated,
        active_file=active_file,
        blueprint=blueprint,
    )
    assert switched["dematerialized_job_ids"] == [entry.spec.job_id]
    assert (
        workflow_json_io.read_json_file(research_findings._summary_path())["research_findings"]
        == []
    )

    reopened = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=parent,
        active_file=active_file,
        blueprint=blueprint,
    )
    assert reopened["materialized_job_ids"] == [entry.spec.job_id]
    assert reopened["active_delivery_backlog"] == 1
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    markers = research_findings.durable_delivery_markers(persisted)
    assert child_marker in markers
    assert research_findings.delivery_key(entry.spec.job_id, parent) not in markers


def test_archive_quarantines_hash_mismatch_and_missing_or_malformed_ledger(monkeypatch, tmp_path):
    """Unsafe materialized copies remain durable but never enter a prover prompt."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-quarantine"
    target = "demo"
    active_file = str(tmp_path / "Main.lean")
    entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="ds-exact",
        target_symbol=target,
        active_file=active_file,
        summary="exact ledger evidence",
    )
    tampered = research_findings.build_finding_record(entry, entry.result, entries=[entry])
    tampered["deliverable"] = {"summary": "tampered materialized evidence"}
    missing = {
        "job_id": f"{campaign_id}.orchestrator.ds-missing",
        "campaign_id": campaign_id,
        "target_symbol": target,
        "active_file": active_file,
        "deliverable": {"summary": "finding whose ledger payload is gone"},
    }
    raw_ledger = [entry.to_mapping(), {"spec": "malformed-ledger-row"}]
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": raw_ledger,
            "research_findings": [tampered, missing],
        },
    )

    report = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert report["quarantined"] == 3
    assert report["active_delivery_backlog"] == 0
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    assert persisted["dispatch_ledger"] == raw_ledger
    assert [item["job_id"] for item in persisted["research_findings"]] == [
        entry.spec.job_id,
        missing["job_id"],
    ]
    records = persisted[research_findings.FINDING_MIGRATION_KEY]["records"]
    assert records[entry.spec.job_id]["status"].startswith("quarantined_")
    assert records[missing["job_id"]]["status"] == "quarantined_missing_ledger"
    assert any(record["status"] == "quarantined_malformed_ledger" for record in records.values())
    assert (
        research_findings.relevant_findings(
            persisted,
            target_symbol=target,
            active_file=active_file,
        )
        == ()
    )


def test_archive_canonicalizes_authenticated_pre_compaction_objective_idempotently(
    monkeypatch,
    tmp_path,
):
    """Accept exact old prompt bytes after terminal objective compaction once."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-objective-upgrade"
    target = "demo"
    active_file = str(tmp_path / "Main.lean")
    entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="ds-objective",
        target_symbol=target,
        active_file=active_file,
        summary="authenticated objective migration",
    )
    finding, compacted_ledger, full_objective = _pre_compaction_finding_and_compacted_ledger(entry)
    assert finding["objective"] == full_objective
    assert compacted_ledger["spec"]["objective"] == entry.spec.objective
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": [compacted_ledger],
            "research_findings": [finding],
        },
    )

    first = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )
    second = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert first["quarantined"] == 0
    assert first["active_delivery_backlog"] == 1
    assert first["state_changed"] is True
    assert second["state_changed"] is False
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    assert persisted["research_findings"][0]["objective"] == entry.spec.objective
    record = persisted[research_findings.FINDING_MIGRATION_KEY]["records"][entry.spec.job_id]
    assert record["status"] == "materialized_current"


@pytest.mark.parametrize("corruption", ["wrong_digest", "semantic_mismatch"])
def test_archive_quarantines_unauthenticated_pre_compaction_objective(
    monkeypatch,
    tmp_path,
    corruption,
):
    """Reject an old objective unless both its digest and semantics agree."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = f"campaign-objective-{corruption}"
    target = "demo"
    active_file = str(tmp_path / "Main.lean")
    entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="ds-objective",
        target_symbol=target,
        active_file=active_file,
        summary="objective mismatch evidence",
    )
    finding, compacted_ledger, full_objective = _pre_compaction_finding_and_compacted_ledger(entry)
    if corruption == "wrong_digest":
        compacted_ledger["spec"]["inputs"]["objective_sha256"] = "0" * 64
    else:
        full_objective = (
            f"research a different semantic route\n\n"
            f"{research_route_context.ROUTE_CONTEXT_MARKER}\nOld context."
        )
        finding["objective"] = full_objective
        compacted_ledger["spec"]["inputs"]["objective_sha256"] = sha256(
            full_objective.encode("utf-8")
        ).hexdigest()
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": [compacted_ledger],
            "research_findings": [finding],
        },
    )

    report = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert report["quarantined"] == 1
    assert report["active_delivery_backlog"] == 0
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    assert persisted["research_findings"][0]["objective"] == full_objective
    record = persisted[research_findings.FINDING_MIGRATION_KEY]["records"][entry.spec.job_id]
    assert record["status"] == "quarantined_materialized_evidence_hash_mismatch"


def test_archive_noop_preserves_large_summary_and_new_evidence_forces_write(
    monkeypatch,
    tmp_path,
):
    """Stable quarantine totals do not rewrite the shared summary each poll."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-noop-migration"
    target = "demo"
    active_file = str(tmp_path / "Main.lean")
    missing = {
        "job_id": f"{campaign_id}.orchestrator.ds-missing",
        "campaign_id": campaign_id,
        "target_symbol": target,
        "active_file": active_file,
        "deliverable": {"summary": "quarantined evidence whose ledger row is absent"},
    }
    summary_path = research_findings._summary_path()
    workflow_json_io.write_json_file(
        summary_path,
        {
            "padding": "x" * 1_000_000,
            "dispatch_ledger": [],
            "research_findings": [missing],
        },
    )
    original_write = workflow_json_io.atomic_json_write
    writes: list[dict] = []

    def record_write(path, payload, *, sort_keys):
        writes.append(dict(payload))
        original_write(path, payload, sort_keys=sort_keys)

    monkeypatch.setattr(workflow_json_io, "atomic_json_write", record_write)

    first = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )
    first_mtime = summary_path.stat().st_mtime_ns
    first_bytes = summary_path.read_bytes()
    second = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert first["quarantined"] == second["quarantined"] == 1
    assert first["state_changed"] is True
    assert second["state_changed"] is False
    assert second["archive_updates"] == 0
    assert len(writes) == 1
    assert summary_path.stat().st_mtime_ns == first_mtime
    assert summary_path.read_bytes() == first_bytes

    new_entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="ds-new",
        target_symbol=target,
        active_file=active_file,
        summary="new exact evidence invalidates the migration no-op",
    )
    persisted = workflow_json_io.read_json_file(summary_path)
    persisted["dispatch_ledger"].append(new_entry.to_mapping())
    original_write(summary_path, persisted, sort_keys=True)
    writes_before_migration = len(writes)

    third = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert third["state_changed"] is True
    assert third["materialized_job_ids"] == [new_entry.spec.job_id]
    assert len(writes) == writes_before_migration + 1


def test_archive_migration_semantic_work_is_linear_then_zero_on_noop(
    monkeypatch,
    tmp_path,
):
    """Parse each ledger result once and reuse versioned archive decisions."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-linear-migration"
    target = "demo"
    active_file = str(tmp_path / "Main.lean")
    entry_count = 96
    entries = [
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix=f"ds-{index:03d}",
            target_symbol=target,
            active_file=active_file,
            summary=f"independent evidence item {index}",
        )
        for index in range(entry_count)
    ]
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {"dispatch_ledger": [entry.to_mapping() for entry in entries]},
    )

    original_evidence = research_route_context.semantic_evidence
    original_classify = research_route_context.classify_semantic_novelty
    evidence_calls = 0
    classification_calls = 0

    def count_evidence(entry):
        nonlocal evidence_calls
        evidence_calls += 1
        return original_evidence(entry)

    def count_classification(entry, candidates, **kwargs):
        nonlocal classification_calls
        classification_calls += 1
        return original_classify(entry, candidates, **kwargs)

    monkeypatch.setattr(research_route_context, "semantic_evidence", count_evidence)
    monkeypatch.setattr(
        research_route_context,
        "classify_semantic_novelty",
        count_classification,
    )

    first = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert first["materialized"] == research_findings.DELIVERY_BACKLOG_CAP
    assert classification_calls == research_findings.DELIVERY_BACKLOG_CAP
    # One substance scan per ledger row plus one normalized view per finding;
    # no classifier may reparse the whole prefix for every materialization.
    assert evidence_calls <= entry_count + research_findings.DELIVERY_BACKLOG_CAP
    first_evidence_calls = evidence_calls
    first_classification_calls = classification_calls

    second = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert second["materialized"] == 0
    assert second["state_changed"] is False
    assert evidence_calls == first_evidence_calls
    assert classification_calls == first_classification_calls


def test_archive_migration_reclassifies_only_a_policy_stale_finding(
    monkeypatch,
    tmp_path,
):
    """Refresh one stale novelty record without reclassifying stable findings."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-stale-novelty"
    target = "demo"
    active_file = str(tmp_path / "Main.lean")
    entries = [
        _consumed_research_entry(
            campaign_id=campaign_id,
            suffix=f"ds-{index:03d}",
            target_symbol=target,
            active_file=active_file,
            summary=f"policy evidence item {index}",
        )
        for index in range(40)
    ]
    summary_path = research_findings._summary_path()
    workflow_json_io.write_json_file(
        summary_path,
        {"dispatch_ledger": [entry.to_mapping() for entry in entries]},
    )
    research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )
    summary = workflow_json_io.read_json_file(summary_path)
    stale_job_id = summary["research_findings"][-1]["job_id"]
    summary["research_findings"][-1]["semantic_novelty"]["version"] = 0
    workflow_json_io.write_json_file(summary_path, summary)

    original_classify = research_route_context.classify_semantic_novelty
    classified_job_ids: list[str] = []

    def count_classification(entry, candidates, **kwargs):
        classified_job_ids.append(entry.spec.job_id)
        return original_classify(entry, candidates, **kwargs)

    monkeypatch.setattr(
        research_route_context,
        "classify_semantic_novelty",
        count_classification,
    )

    refreshed = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert refreshed["state_changed"] is True
    assert classified_job_ids == [stale_job_id]
    persisted = workflow_json_io.read_json_file(summary_path)
    stale = next(
        finding for finding in persisted["research_findings"] if finding["job_id"] == stale_job_id
    )
    assert stale["semantic_novelty"]["version"] == (research_route_context.SEMANTIC_NOVELTY_VERSION)

    classified_job_ids.clear()
    repeated = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )
    assert repeated["state_changed"] is False
    assert classified_job_ids == []


def test_archive_noop_detector_persists_legacy_finding_provenance_repair(
    monkeypatch,
    tmp_path,
):
    """Finding-only normalization invalidates an otherwise stable migration index."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-provenance-repair"
    target = "demo"
    active_file = str(tmp_path / "Main.lean")
    entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="ds-legacy",
        target_symbol=target,
        active_file=active_file,
        summary="legacy evidence with recoverable exact provenance",
    )
    summary_path = research_findings._summary_path()
    workflow_json_io.write_json_file(
        summary_path,
        {
            "dispatch_ledger": [entry.to_mapping()],
            "research_findings": [
                research_findings.build_finding_record(
                    entry,
                    entry.result,
                    entries=[entry],
                )
            ],
        },
    )
    initial = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )
    assert initial["state_changed"] is True

    legacy_summary = workflow_json_io.read_json_file(summary_path)
    legacy_finding = legacy_summary["research_findings"][0]
    legacy_finding.pop("campaign_id")
    legacy_finding.pop("target_symbol")
    legacy_finding.pop("active_file")
    workflow_json_io.write_json_file(summary_path, legacy_summary)
    original_write = workflow_json_io.atomic_json_write
    writes: list[dict] = []

    def record_write(path, payload, *, sort_keys):
        writes.append(dict(payload))
        original_write(path, payload, sort_keys=sort_keys)

    monkeypatch.setattr(workflow_json_io, "atomic_json_write", record_write)

    repaired = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert repaired["state_changed"] is True
    assert repaired["archive_updates"] == 0
    assert repaired["materialized"] == 0
    assert len(writes) == 1
    persisted_finding = workflow_json_io.read_json_file(summary_path)["research_findings"][0]
    assert persisted_finding["campaign_id"] == campaign_id
    assert persisted_finding["target_symbol"] == target
    assert persisted_finding["active_file"] == active_file

    repeated = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )
    assert repeated["state_changed"] is False
    assert len(writes) == 1


def test_durable_compaction_never_evicts_a_quarantined_delivered_copy():
    """Quarantine is correctness state even when its origin has a receipt."""
    target = "demo"
    quarantined_job = "campaign.ds-quarantined"
    history = [
        {
            "job_id": f"campaign.ds-history-{index:03d}",
            "target_symbol": target,
            "deliverable": {"summary": f"delivered history {index}"},
        }
        for index in range(research_findings.DURABLE_FINDING_HISTORY_CAP + 1)
    ]
    quarantined = {
        "job_id": quarantined_job,
        "target_symbol": target,
        "deliverable": {"summary": "hash-mismatched materialized copy"},
    }
    all_findings = [quarantined, *history]
    summary = {
        "research_findings": all_findings,
        research_findings.DELIVERY_STATE_KEY: {
            "research_findings_delivered": [
                research_findings.delivery_key(item["job_id"], target) for item in all_findings
            ]
        },
        research_findings.FINDING_MIGRATION_KEY: {
            "records": {
                quarantined_job: {"status": "quarantined_materialized_evidence_hash_mismatch"}
            }
        },
    }

    assert research_findings.compact_durable_findings(summary) == 1

    retained_ids = {item["job_id"] for item in summary["research_findings"]}
    assert quarantined_job in retained_ids
    assert len(retained_ids) == research_findings.DURABLE_FINDING_HISTORY_CAP + 1


def test_durable_delivered_history_matches_delivery_backlog_window():
    """Retain only the newest backlog-sized window of acknowledged copies."""
    target = "demo"
    history = [
        {
            "job_id": f"campaign.ds-history-{index:03d}",
            "target_symbol": target,
            "deliverable": {"summary": f"delivered history {index}"},
        }
        for index in range(research_findings.DELIVERY_BACKLOG_CAP + 5)
    ]
    summary = {
        "research_findings": history,
        research_findings.DELIVERY_STATE_KEY: {
            "research_findings_delivered": [
                research_findings.delivery_key(item["job_id"], target) for item in history
            ]
        },
    }

    assert research_findings.DURABLE_FINDING_HISTORY_CAP == (research_findings.DELIVERY_BACKLOG_CAP)
    assert research_findings.compact_durable_findings(summary) == 0
    assert [item["job_id"] for item in summary["research_findings"]] == [
        item["job_id"] for item in history[-research_findings.DELIVERY_BACKLOG_CAP :]
    ]


def test_durable_history_window_never_evicts_undelivered_findings():
    """Treat every unacknowledged finding as correctness state beyond the cap."""
    target = "demo"
    undelivered = [
        {
            "job_id": f"campaign.ds-undelivered-{index:03d}",
            "target_symbol": target,
            "deliverable": {"summary": f"undelivered evidence {index}"},
        }
        for index in range(research_findings.DELIVERY_BACKLOG_CAP + 7)
    ]
    delivered = [
        {
            "job_id": f"campaign.ds-delivered-{index:03d}",
            "target_symbol": target,
            "deliverable": {"summary": f"delivered evidence {index}"},
        }
        for index in range(research_findings.DELIVERY_BACKLOG_CAP + 7)
    ]
    summary = {
        "research_findings": [*delivered, *undelivered],
        research_findings.DELIVERY_STATE_KEY: {
            "research_findings_delivered": [
                research_findings.delivery_key(item["job_id"], target) for item in delivered
            ]
        },
    }

    assert research_findings.compact_durable_findings(summary) == len(undelivered)
    retained_ids = {item["job_id"] for item in summary["research_findings"]}
    assert {item["job_id"] for item in undelivered} <= retained_ids
    assert len(summary["research_findings"]) == (
        len(undelivered) + research_findings.DURABLE_FINDING_HISTORY_CAP
    )


def test_archive_upgrades_expected_checked_contract_drift_without_false_quarantine(
    monkeypatch, tmp_path
):
    """A stricter deterministic policy may canonicalize, but not discard, old evidence."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-policy-upgrade"
    target = "demo"
    active_file = str(tmp_path / "Main.lean")
    entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="ds-legacy-check",
        target_symbol=target,
        active_file=active_file,
        summary="legacy checked candidate",
    )
    raw_result = dict(entry.result)
    raw_result["deliverable"] = {
        "status": "partial_checked_delta",
        "summary": "legacy checked candidate",
        "checked_replacements": [
            {
                "target_symbol": target,
                "replacement": "theorem demo : True := by\n  trivial",
                "worker_check": {
                    "tool": "lean_incremental_check",
                    "has_errors": False,
                    "has_sorry": False,
                    "valid_without_sorry": True,
                },
            }
        ],
    }
    entry = replace(entry, result=raw_result)
    legacy = research_findings.build_finding_record(entry, raw_result, entries=[entry])
    legacy["deliverable"] = dict(raw_result["deliverable"])
    legacy.pop("archive_result_sha256")
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": [entry.to_mapping()],
            "research_findings": [legacy],
        },
    )

    report = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert report["quarantined"] == 0
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    finding = persisted["research_findings"][0]
    assert finding["archive_result_sha256"]
    assert finding["deliverable"]["checked_replacements"] == []
    assert len(finding["deliverable"]["unchecked_replacements"]) == 1
    assert finding["deliverable"]["checked_replacement_status"] == "incomplete_unverified"


def test_runner_reconciles_assignment_archive_on_each_delivery_tick(monkeypatch):
    """Repeated scans page newly available archive slots after acknowledgements."""
    calls: list[dict] = []
    activities: list[tuple[str, str, dict]] = []
    blueprint = plan_state.Blueprint()

    def migrate(**kwargs):
        calls.append(kwargs)
        return {
            **kwargs,
            "reconstructed": 2,
            "reconstructed_job_ids": [
                "campaign.orchestrator.ds-001",
                "campaign.orchestrator.em-002",
            ],
        }

    monkeypatch.setattr(
        runner.research_findings,
        "migrate_consumed_findings_for_assignment",
        migrate,
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, message, **details: activities.append((event, message, details)),
    )
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: blueprint)
    state = {
        "campaign_id": "campaign",
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "/tmp/Main.lean",
        },
    }

    first = runner._migrate_research_findings_for_assignment(state, None)
    second = runner._migrate_research_findings_for_assignment(state, None)

    assert first["reconstructed"] == 2
    assert second["reconstructed"] == 2
    assert calls == [
        {
            "campaign_id": "campaign",
            "target_symbol": "demo",
            "active_file": "/tmp/Main.lean",
            "blueprint": blueprint,
        },
        {
            "campaign_id": "campaign",
            "target_symbol": "demo",
            "active_file": "/tmp/Main.lean",
            "blueprint": blueprint,
        },
    ]
    assert [activity[0] for activity in activities] == [
        "research-finding-migration",
        "research-finding-migration",
    ]
    assert activities[-1][2]["reconstructed_job_ids"] == [
        "campaign.orchestrator.ds-001",
        "campaign.orchestrator.em-002",
    ]


def test_runner_reports_new_deferred_archive_pointer_without_materialization(monkeypatch):
    """A capacity-deferred consumed result is visible on its first migration."""
    activities: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runner.research_findings,
        "migrate_consumed_findings_for_assignment",
        lambda **kwargs: {
            **kwargs,
            "materialized": 0,
            "dematerialized": 0,
            "quarantined": 0,
            "deferred_capacity": 1,
            "archive_updates": 1,
        },
    )
    monkeypatch.setattr(runner.plan_state, "load_blueprint", plan_state.Blueprint)
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, _message, **details: activities.append((event, details)),
    )

    report = runner._migrate_research_findings_for_assignment(
        {
            "campaign_id": "campaign",
            "current_queue_assignment": {
                "target_symbol": "child",
                "active_file": "/tmp/Main.lean",
            },
        },
        None,
    )

    assert report["deferred_capacity"] == 1
    assert activities == [("research-finding-migration", report)]


def test_runner_suppresses_unchanged_quarantine_migration_activity(monkeypatch):
    """A stable quarantine inventory is state, not a new activity transition."""
    activities: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runner.research_findings,
        "migrate_consumed_findings_for_assignment",
        lambda **kwargs: {
            **kwargs,
            "materialized": 0,
            "dematerialized": 0,
            "quarantined": 100,
            "archive_updates": 0,
            "state_changed": False,
        },
    )
    monkeypatch.setattr(runner.plan_state, "load_blueprint", plan_state.Blueprint)
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event, _message, **details: activities.append((event, details)),
    )

    report = runner._migrate_research_findings_for_assignment(
        {
            "campaign_id": "campaign",
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": "/tmp/Main.lean",
            },
        },
        None,
    )

    assert report["quarantined"] == 100
    assert activities == []


def test_legacy_provenance_allows_safe_acknowledgement_after_restart(monkeypatch, tmp_path):
    """An exact ledger spec recovers the origin before compacting legacy evidence."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-resume"
    job_id = f"{campaign_id}.orchestrator.ds-001"
    target_symbol = "split_helper"
    active_file = str(tmp_path / "Main.lean")
    spec = JobSpec(
        job_id=job_id,
        archetype="deep_search",
        requester_role="orchestrator",
        objective="legacy job",
        budget=JobBudget(api_steps=2, wall_clock_s=30),
        deliverable="findings_report",
        inputs={"target_symbol": target_symbol, "active_file": active_file},
        parent_job_id=f"{campaign_id}.orchestrator",
    )
    summary = {
        "dispatch_ledger": [
            LedgerEntry(
                spec=spec,
                state="done",
                consumed=True,
            ).to_mapping()
        ],
        "research_findings": [
            {
                "job_id": job_id,
                "deliverable": {"summary": "legacy checked observation"},
            }
        ],
        research_findings.DELIVERY_STATE_KEY: {
            "campaign_id": campaign_id,
            "research_findings_delivered": [research_findings.delivery_key(job_id, target_symbol)],
        },
    }

    assert research_findings.compact_durable_findings(summary) == 0
    finding = summary["research_findings"][0]
    assert finding["campaign_id"] == campaign_id
    assert finding["target_symbol"] == target_symbol
    assert finding["active_file"] == active_file
    assert "delivery_acknowledged" not in finding
    workflow_json_io.write_json_file(research_findings._summary_path(), summary)

    restarted = workflow_json_io.read_json_file(research_findings._summary_path())
    assert (
        research_findings.campaign_delivery_backlog_count(
            restarted,
            campaign_id=campaign_id,
        )
        == 0
    )


def test_concurrent_parent_polls_consume_completed_job_once(monkeypatch, tmp_path):
    """Heartbeat and tool-result maintenance serialize the full handoff."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service, job_id = _seed_completed_research_job()
    original_consume = dispatch_service.DispatchService.consume
    consume_entered = threading.Event()
    release_consume = threading.Event()
    second_started = threading.Event()
    consume_calls: list[str] = []
    statuses: list[dict] = []
    errors: list[BaseException] = []

    def slow_consume(self, candidate_job_id):
        consume_calls.append(candidate_job_id)
        consume_entered.set()
        assert release_consume.wait(timeout=2)
        return original_consume(self, candidate_job_id)

    def maintain(*, second: bool = False):
        if second:
            second_started.set()
        try:
            statuses.append(
                research_portfolio.maintain_portfolio(
                    campaign_id="campaign-demo",
                    target_symbol="demo",
                    active_file="Main.lean",
                    attempt_count=0,
                    workers=0,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(dispatch_service.DispatchService, "consume", slow_consume)
    first = threading.Thread(target=maintain)
    first.start()
    assert consume_entered.wait(timeout=2)

    second = threading.Thread(target=lambda: maintain(second=True))
    second.start()
    assert second_started.wait(timeout=2)
    assert second.is_alive()
    assert consume_calls == [job_id]

    release_consume.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert consume_calls == [job_id]
    assert sorted(len(status["consumed"]) for status in statuses) == [0, 1]
    summary = dispatch_service.read_json_file(service._summary_path())
    assert [item["job_id"] for item in summary["research_findings"]] == [job_id]
    assert service._entry(job_id).consumed is True


def test_portfolio_downgrades_legacy_checked_claim_without_exact_replacement(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    _service, job_id = _seed_completed_research_job(
        deliverable={
            "status": "candidate_verified",
            "verification": "lean_incremental_check passed; valid_without_sorry=true",
            "summary": "worker found a proof but omitted the code",
        }
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
        workers=0,
    )

    assert status["consumed"] == [job_id]
    summary = dispatch_service.read_json_file(
        dispatch_service.DispatchService(root_job_id="campaign-demo")._summary_path()
    )
    finding = summary["research_findings"][0]
    deliverable = finding["deliverable"]
    assert deliverable["status"] == "incomplete_unverified"
    assert deliverable["reported_status"] == "candidate_verified"
    assert deliverable["checked_replacements"] == []
    assert finding["semantic_novelty"]["has_checked_helper"] is False


def test_portfolio_persists_exact_checked_replacement_without_truncation(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    replacement = "by\n  " + "exact True.intro\n  " * 2200
    _service, job_id = _seed_completed_research_job(
        deliverable={
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
        }
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
        workers=0,
    )

    assert status["consumed"] == [job_id]
    summary = dispatch_service.read_json_file(
        dispatch_service.DispatchService(root_job_id="campaign-demo")._summary_path()
    )
    persisted = summary["research_findings"][0]["deliverable"]
    assert persisted["checked_replacements"][0]["replacement"] == replacement
    assert persisted["parent_recheck_required"] is True


def test_finding_write_failure_keeps_job_retryable_after_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service, job_id = _seed_completed_research_job()
    original_update = research_portfolio.update_json_file

    def fail_finding_write(_path, _mutate):
        raise OSError("simulated summary write failure")

    monkeypatch.setattr(research_portfolio, "update_json_file", fail_finding_write)
    with pytest.raises(OSError, match="simulated summary write failure"):
        research_portfolio.maintain_portfolio(
            campaign_id="campaign-demo",
            target_symbol="demo",
            active_file="Main.lean",
            attempt_count=0,
            workers=0,
        )

    failed_summary = dispatch_service.read_json_file(service._summary_path())
    assert failed_summary.get("research_findings") in (None, [])
    assert service._entry(job_id).consumed is False

    monkeypatch.setattr(research_portfolio, "update_json_file", original_update)
    restarted = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
        workers=0,
    )

    assert restarted["consumed"] == [job_id]
    summary = dispatch_service.read_json_file(service._summary_path())
    assert [item["job_id"] for item in summary["research_findings"]] == [job_id]
    assert (
        dispatch_service.DispatchService(root_job_id="campaign-demo")._entry(job_id).consumed
        is True
    )


def test_consume_failure_retries_idempotently_after_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service, job_id = _seed_completed_research_job()
    original_consume = dispatch_service.DispatchService.consume

    def fail_consume(_self, _job_id):
        raise OSError("simulated ledger write failure")

    monkeypatch.setattr(dispatch_service.DispatchService, "consume", fail_consume)
    with pytest.raises(OSError, match="simulated ledger write failure"):
        research_portfolio.maintain_portfolio(
            campaign_id="campaign-demo",
            target_symbol="demo",
            active_file="Main.lean",
            attempt_count=0,
            workers=0,
        )

    persisted = dispatch_service.read_json_file(service._summary_path())
    assert [item["job_id"] for item in persisted["research_findings"]] == [job_id]
    assert service._entry(job_id).consumed is False

    monkeypatch.setattr(dispatch_service.DispatchService, "consume", original_consume)
    restarted = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
        workers=0,
    )

    assert restarted["consumed"] == [job_id]
    summary = dispatch_service.read_json_file(service._summary_path())
    assert [item["job_id"] for item in summary["research_findings"]] == [job_id]
    assert (
        dispatch_service.DispatchService(root_job_id="campaign-demo")._entry(job_id).consumed
        is True
    )


def test_portfolio_launches_one_grounding_job_then_expands_and_replaces(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )

    first = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
        workers=2,
    )
    assert first["active"] == 1
    assert len(first["launched"]) == 1
    assert ".ds-" in first["launched"][0]

    service = dispatch_service.DispatchService(root_job_id="campaign-demo", cap=2)
    first_job = first["launched"][0]
    first_entry = service._entry(first_job)
    assert research_route_context.ROUTE_CONTEXT_MARKER in first_entry.spec.objective
    assert first_entry.spec.inputs[research_route_context.ROUTE_CONTEXT_INPUT_KEY][
        "assignment"
    ] == {"target_symbol": "demo", "active_file": "Main.lean"}
    service._transition(
        first_job,
        "done",
        finished_at=dispatch_service._now_iso(),
        result={
            "status": "done",
            "deliverable": {
                "summary": "route one exhausted; try invariant h",
                "files_modified": [".leanflow/research/ResearchDemo.lean"],
            },
            "artifact_paths": [],
            "plan_delta": [],
        },
    )

    expanded = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=2,
        workers=2,
    )

    assert expanded["active"] == 2
    assert expanded["consumed"] == [first_job]
    assert len(expanded["launched"]) == 2
    assert any(".ds-" in job_id for job_id in expanded["launched"])
    assert any(".em-" in job_id for job_id in expanded["launched"])
    entries = service.entries()
    consumed = next(entry for entry in entries if entry.spec.job_id == first_job)
    assert consumed.consumed is True
    summary = dispatch_service.read_json_file(service._summary_path())
    assert summary["research_findings"][0]["target_symbol"] == "demo"
    assert summary["research_findings"][0]["active_file"] == "Main.lean"
    assert summary["research_findings"][0]["artifact_paths"] == [
        ".leanflow/research/ResearchDemo.lean"
    ]
    deep_objectives = [
        entry.spec.objective for entry in entries if entry.spec.archetype == "deep_search"
    ]
    assert len(deep_objectives) == len(set(deep_objectives))


def test_portfolio_second_launch_treats_running_sibling_as_coordination_context(
    monkeypatch, tmp_path
):
    """Reproduce ds-524/em-525 without inventing evidence for the first worker."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )

    launched = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=2,
        workers=2,
    )

    assert len(launched["launched"]) == 2
    service = dispatch_service.DispatchService(root_job_id="campaign-demo", cap=2)
    entries = service.entries()
    deep_search = next(entry for entry in entries if entry.spec.archetype == "deep_search")
    empirical = next(entry for entry in entries if entry.spec.archetype == "empirical")
    context = empirical.spec.inputs[research_route_context.ROUTE_CONTEXT_INPUT_KEY]
    sibling = next(
        record
        for record in context["recent_research_routes"]
        if record["job_id"] == deep_search.spec.job_id
    )

    assert sibling["state"] == "running"
    assert sibling["objective"] == research_route_context.semantic_worker_objective(
        deep_search.spec.objective
    )
    assert sibling["result_excerpt"] == (
        "Active job; no terminal result or mathematical evidence is available yet."
    )
    assert "no_classified_mathematical_semantics" not in empirical.spec.objective
    assert "Evidence-only non-closing prior route" not in empirical.spec.objective


def test_concurrent_portfolio_ticks_adopt_atomic_delta_reservation_winner(monkeypatch, tmp_path):
    """Model two parent processes racing from stale portfolio snapshots."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    rendezvous = threading.Barrier(2, timeout=5)
    original_propose = dispatch_service.DispatchService.propose

    def mint_unique_job_id(self, archetype, *, role, parent_job_id=""):
        del archetype, role, parent_job_id
        suffix = threading.current_thread().name.rsplit("-", 1)[-1]
        return f"{self.root_job_id}.orchestrator.ds-race-{suffix}"

    def racing_propose(self, spec):
        rendezvous.wait()
        return original_propose(self, spec)

    def atomic_fake_deploy(self, job_id):
        def mutate(ledger):
            index = self._find(ledger, job_id)
            assert index >= 0
            current = dispatch_service.LedgerEntry.from_mapping(ledger[index])
            if current.state == "proposed":
                current = current.with_state("deployed").with_state(
                    "running",
                    started_at=dispatch_service._now_iso(),
                )
                ledger[index] = current.to_mapping()
            return current, []

        return self._transaction(mutate)

    monkeypatch.setattr(
        research_portfolio,
        "_select_distinct_route",
        lambda *_args, **_kwargs: (
            "forced-concurrent-delta",
            "investigate one forced concurrent mathematical delta",
            "",
        ),
    )
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "mint_job_id",
        mint_unique_job_id,
    )
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "propose",
        racing_propose,
    )
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        atomic_fake_deploy,
    )

    statuses: list[dict] = []
    errors: list[BaseException] = []

    def maintain() -> None:
        try:
            statuses.append(
                research_portfolio._maintain_portfolio_once(
                    campaign_id="campaign-delta-race",
                    target_symbol="demo",
                    active_file=str(tmp_path / "Main.lean"),
                    attempt_count=0,
                    workers=1,
                    refill=True,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=maintain, name=f"portfolio-racer-{index}") for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(statuses) == 2
    entries = dispatch_service.DispatchService(root_job_id="campaign-delta-race").entries()
    assert len(entries) == 1
    winner = entries[0]
    assert winner.state == "running"
    assert all(status["active_jobs"] == [winner.spec.job_id] for status in statuses)
    assert sorted(len(status["launched"]) for status in statuses) == [0, 1]


def test_portfolio_proposal_propagates_unrelated_value_error(monkeypatch, tmp_path):
    """Only the typed optimistic-reservation conflict is recoverable."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "propose",
        lambda _self, _spec: (_ for _ in ()).throw(ValueError("unrelated validation error")),
    )

    with pytest.raises(ValueError, match="unrelated validation error"):
        research_portfolio._maintain_portfolio_once(
            campaign_id="campaign-unrelated-value-error",
            target_symbol="demo",
            active_file=str(tmp_path / "Main.lean"),
            attempt_count=0,
            workers=1,
            refill=True,
        )


def test_failure_backoff_hydrates_rebuilt_terminal_history_silently(monkeypatch, tmp_path):
    """Cold history hydration restores state without replaying old events."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-history"
    target_symbol = "erdos_242_residual_mod_seven_eq_zero"
    active_file = str(tmp_path / "ErdosProblems/242.lean")
    now = datetime(2026, 7, 17, 11, 17, 9, tzinfo=UTC)

    def terminal(
        suffix: str,
        state: str,
        finished_at: str,
        *,
        substantive: bool = False,
    ) -> LedgerEntry:
        result = (
            {"status": "done", "deliverable": {"summary": "checked modular route"}}
            if substantive
            else {
                "status": state,
                "deliverable": {},
                "error": "provider timeout" if state in {"failed", "stuck"} else "",
            }
        )
        return LedgerEntry(
            spec=JobSpec(
                job_id=f"{campaign_id}.orchestrator.{suffix}",
                archetype="deep_search",
                requester_role="orchestrator",
                objective=f"research {target_symbol}",
                budget=JobBudget(api_steps=2, wall_clock_s=30),
                deliverable="findings_report",
                inputs={
                    "campaign_id": campaign_id,
                    "target_symbol": target_symbol,
                    "active_file": active_file,
                },
                parent_job_id=f"{campaign_id}.orchestrator",
            ),
            state=state,
            created_at=finished_at,
            finished_at=finished_at,
            result=result,
        )

    # This is the live restart shape: several old cooldown/clear transitions
    # followed by a later terminal worker. The ledger is the lossless history;
    # rebuilding its derived circuit must not narrate the whole history again.
    entries = [
        terminal("ds-103", "failed", "2026-07-15T13:42:09+00:00"),
        terminal("ds-104", "failed", "2026-07-15T13:45:22+00:00"),
        terminal("ds-143", "failed", "2026-07-15T18:56:18+00:00"),
        terminal("ds-145", "done", "2026-07-15T19:02:00+00:00", substantive=True),
        terminal("ds-153", "failed", "2026-07-15T19:40:25+00:00"),
        terminal("ds-157", "done", "2026-07-15T19:42:00+00:00", substantive=True),
        terminal("ds-343", "killed", "2026-07-17T09:38:02+00:00"),
    ]
    summary_path = dispatch_service.DispatchService(root_job_id=campaign_id)._summary_path()
    raw_ledger = [entry.to_mapping() for entry in entries]
    workflow_json_io.write_json_file(summary_path, {"dispatch_ledger": raw_ledger})

    hydrated = research_portfolio._reconcile_failure_backoff(
        entries,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        archetypes=["deep_search"],
        now=now,
    )

    assert hydrated["transitions"] == []
    summary = workflow_json_io.read_json_file(summary_path)
    scope_key = research_portfolio._failure_scope_key(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        archetype="deep_search",
    )
    scope = summary[research_portfolio.FAILURE_BACKOFF_STATE_KEY]["scopes"][scope_key]
    assert scope["consecutive_failures"] == 0
    assert scope["last_terminal_job_id"].endswith(".ds-343")
    assert scope["last_failure_job_id"].endswith(".ds-153")
    assert summary["dispatch_ledger"] == raw_ledger
    circuit_snapshot = summary[research_portfolio.FAILURE_BACKOFF_STATE_KEY]

    replayed = research_portfolio._reconcile_failure_backoff(
        entries,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        archetypes=["deep_search"],
        now=now,
    )
    assert replayed["transitions"] == []
    assert (
        workflow_json_io.read_json_file(summary_path)[research_portfolio.FAILURE_BACKOFF_STATE_KEY]
        == circuit_snapshot
    )

    # A deterministic summary rebuild may retain the raw ledger while losing
    # this derived cache. Rehydration converges to the same circuit silently.
    rebuilt_summary = workflow_json_io.read_json_file(summary_path)
    rebuilt_summary.pop(research_portfolio.FAILURE_BACKOFF_STATE_KEY)
    workflow_json_io.write_json_file(summary_path, rebuilt_summary)
    rebuilt = research_portfolio._reconcile_failure_backoff(
        entries,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        archetypes=["deep_search"],
        now=now,
    )
    assert rebuilt["transitions"] == []
    rebuilt_summary = workflow_json_io.read_json_file(summary_path)
    assert rebuilt_summary[research_portfolio.FAILURE_BACKOFF_STATE_KEY] == circuit_snapshot
    assert rebuilt_summary["dispatch_ledger"] == raw_ledger


def test_failure_backoff_emits_each_new_terminal_transition_once(monkeypatch, tmp_path):
    """A persisted observation baseline keeps 15/60/300/900 transitions exact."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-new-transitions"
    target_symbol = "demo"
    active_file = str(tmp_path / "Main.lean")
    now = datetime(2026, 7, 17, 10, 0, 0, tzinfo=UTC)

    baseline = research_portfolio._reconcile_failure_backoff(
        [],
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        archetypes=["deep_search"],
        now=now,
    )
    assert baseline["transitions"] == []

    entries: list[LedgerEntry] = []
    observed_delays: list[int] = []
    for index, expected_delay in enumerate((15, 60, 300, 900), start=1):
        job_id = f"{campaign_id}.orchestrator.ds-{index:03d}"
        entries.append(
            LedgerEntry(
                spec=JobSpec(
                    job_id=job_id,
                    archetype="deep_search",
                    requester_role="orchestrator",
                    objective="research demo",
                    budget=JobBudget(api_steps=2, wall_clock_s=30),
                    deliverable="findings_report",
                    inputs={
                        "campaign_id": campaign_id,
                        "target_symbol": target_symbol,
                        "active_file": active_file,
                    },
                    parent_job_id=f"{campaign_id}.orchestrator",
                ),
                state="failed",
                created_at=now.isoformat(),
                finished_at=now.isoformat(),
                result={
                    "status": "failed",
                    "deliverable": {},
                    "error": "provider timeout",
                },
            )
        )
        transition = research_portfolio._reconcile_failure_backoff(
            entries,
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            archetypes=["deep_search"],
            now=now,
        )
        assert [item["job_id"] for item in transition["transitions"]] == [job_id]
        assert transition["transitions"][0]["delay_seconds"] == expected_delay
        observed_delays.append(transition["transitions"][0]["delay_seconds"])
        duplicate = research_portfolio._reconcile_failure_backoff(
            entries,
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            archetypes=["deep_search"],
            now=now,
        )
        assert duplicate["transitions"] == []

    assert observed_delays == [15, 60, 300, 900]

    recovered_job_id = f"{campaign_id}.orchestrator.ds-005"
    entries.append(
        LedgerEntry(
            spec=JobSpec(
                job_id=recovered_job_id,
                archetype="deep_search",
                requester_role="orchestrator",
                objective="research demo",
                budget=JobBudget(api_steps=2, wall_clock_s=30),
                deliverable="findings_report",
                inputs={
                    "campaign_id": campaign_id,
                    "target_symbol": target_symbol,
                    "active_file": active_file,
                },
                parent_job_id=f"{campaign_id}.orchestrator",
            ),
            state="done",
            created_at=now.isoformat(),
            finished_at=now.isoformat(),
            result={"status": "done", "deliverable": {"summary": "checked route"}},
        )
    )
    cleared = research_portfolio._reconcile_failure_backoff(
        entries,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        archetypes=["deep_search"],
        now=now,
    )
    assert cleared["transitions"] == [
        {
            "kind": "cleared",
            "scope_key": cleared["transitions"][0]["scope_key"],
            "archetype": "deep_search",
            "job_id": recovered_job_id,
            "previous_failures": 4,
            "reason": "completed result",
        }
    ]
    assert (
        research_portfolio._reconcile_failure_backoff(
            entries,
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            archetypes=["deep_search"],
            now=now,
        )["transitions"]
        == []
    )


def test_empty_worker_failure_backoff_persists_without_heartbeat_relaunch(monkeypatch, tmp_path):
    """Empty failures cool only their exact lane and successful work clears it."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    clock = {"now": datetime(2026, 7, 17, 10, 0, 0, tzinfo=UTC)}
    monkeypatch.setattr(research_portfolio, "_utc_now", lambda: clock["now"])
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda kind, _message, **kwargs: events.append((kind, kwargs)),
    )

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            # Portfolio backoff uses the virtual clock below, while dispatch
            # reconciliation correctly uses wall time for worker liveness.
            # Keep fake running workers fresh in that independent clock domain.
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    campaign_id = "campaign-backoff"
    target_symbol = "erdos_242"
    active_file = str(tmp_path / "ErdosProblems/242.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id, cap=2)
    initial = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=0,
        workers=2,
    )
    failed_job = initial["launched"][0]
    secret = "sk-" + "b" * 40
    service._transition(
        failed_job,
        "failed",
        finished_at=clock["now"].isoformat(),
        result={
            "status": "error",
            "deliverable": {
                research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY: {
                    "assignment": {"target_symbol": target_symbol},
                }
            },
            "artifact_paths": [],
            "plan_delta": [],
            "api_calls": 0,
            "error": f"servers overloaded; Authorization: Bearer {secret}",
        },
        notes="worker result status: error",
    )

    cooled = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )

    # The empirical lane starts immediately, while the failed deep-search lane
    # remains deliberately idle instead of being relaunched every heartbeat.
    assert len(cooled["launched"]) == 1
    assert ".em-" in cooled["launched"][0]
    assert cooled["failure_backoff"]["deep_search"]["consecutive_failures"] == 1
    assert cooled["failure_backoff"]["deep_search"]["delay_seconds"] == 15
    assert secret not in cooled["failure_backoff"]["deep_search"]["reason"]
    assert "[REDACTED]" in cooled["failure_backoff"]["deep_search"]["reason"]

    heartbeat = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )
    assert heartbeat["launched"] == []
    backoff_events = [kind for kind, _payload in events if kind.endswith("failure-backoff")]
    assert backoff_events == ["research-portfolio-failure-backoff"]

    summary = workflow_json_io.read_json_file(service._summary_path())
    state = summary[research_portfolio.FAILURE_BACKOFF_STATE_KEY]
    scope_key = research_portfolio._failure_scope_key(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        archetype="deep_search",
    )
    scope = state["scopes"][scope_key]
    assert scope["last_failure_job_id"] == failed_job
    assert scope["next_retry_at"] == "2026-07-17T10:00:15+00:00"
    assert secret not in json.dumps(scope)
    assert (
        research_portfolio._reconcile_failure_backoff(
            service.entries(),
            campaign_id=campaign_id,
            target_symbol="different_assignment",
            active_file=active_file,
            archetypes=["deep_search"],
            now=clock["now"],
        )["blocked"]
        == {}
    )

    # At the persisted deadline one retry is allowed. A second empty failure
    # escalates the same durable circuit to the next delay without sleeping.
    clock["now"] += timedelta(seconds=15)
    retry = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )
    assert retry["failure_backoff_retries"] == ["deep_search"]
    retry_job = retry["launched"][0]
    service._transition(
        retry_job,
        "failed",
        finished_at=clock["now"].isoformat(),
        result={
            "status": "error",
            "deliverable": {"summary": ""},
            "artifact_paths": [],
            "plan_delta": [],
            "api_calls": 0,
            "error": "provider timeout",
        },
    )
    escalated = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )
    assert escalated["launched"] == []
    assert escalated["failure_backoff"]["deep_search"]["consecutive_failures"] == 2
    assert escalated["failure_backoff"]["deep_search"]["delay_seconds"] == 60

    # A completed retry clears the consecutive-empty streak and is consumed
    # and replaced in the same orchestration tick.
    clock["now"] += timedelta(seconds=60)
    second_retry = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )
    completed_job = second_retry["launched"][0]
    service._transition(
        completed_job,
        "done",
        finished_at=clock["now"].isoformat(),
        result={
            "status": "done",
            "deliverable": {"summary": "new exact modular route"},
            "artifact_paths": [],
            "plan_delta": [],
        },
    )
    clock["now"] += timedelta(seconds=1)
    recovered = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )
    assert recovered["consumed"] == [completed_job]
    assert len(recovered["launched"]) == 1
    assert "failure_backoff" not in recovered
    persisted = workflow_json_io.read_json_file(service._summary_path())
    assert (
        persisted[research_portfolio.FAILURE_BACKOFF_STATE_KEY]["scopes"][scope_key][
            "consecutive_failures"
        ]
        == 0
    )


def test_route_signature_ignores_generation_and_attempt_nonces(tmp_path):
    """Volatile counters must not disguise an otherwise identical research route."""
    first = research_portfolio._stable_route_signature(
        archetype="deep_search",
        target_symbol="demo",
        active_file=str(tmp_path / "Main.lean"),
        objective="Research demo; generation 1; attempt 2: search the same route.",
    )
    repeated = research_portfolio._stable_route_signature(
        archetype="deep_search",
        target_symbol="demo",
        active_file=str(tmp_path / "Main.lean"),
        objective="Research demo; generation 91; attempt 44: search the same route.",
    )

    assert repeated == first


def test_saturated_second_worker_lane_rotates_to_negation(monkeypatch, tmp_path):
    """A semantic repeat relinquishes its slot instead of minting a new digest."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-semantic-rotation"
    target_symbol = "demo"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    prior = _checked_residue_lane_entry(
        job_id=f"{campaign_id}.orchestrator.em-prior",
        target_symbol=target_symbol,
        active_file=active_file,
        modulus=41,
        residue=10,
        consumed=True,
    )
    repeated = _checked_residue_lane_entry(
        job_id=f"{campaign_id}.orchestrator.em-repeat",
        target_symbol=target_symbol,
        active_file=active_file,
        modulus=83,
        residue=41,
    )
    killed_empirical = LedgerEntry(
        spec=replace(
            repeated.spec,
            job_id=f"{campaign_id}.orchestrator.em-killed",
            inputs={
                **repeated.spec.inputs,
                "route_key": "epoch-refresh-cleanup",
                "route_signature": "cleanup-is-not-semantics",
            },
        ),
        state="killed",
        finished_at="2026-07-17T12:01:30+00:00",
    )
    deep_spec = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
    )
    for entry in (
        prior,
        repeated,
        killed_empirical,
        LedgerEntry(
            spec=deep_spec,
            state="running",
            started_at="2026-07-17T12:02:00+00:00",
        ),
    ):
        service._save_entry(entry)

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "poll",
        lambda self, job_id: {"job_id": job_id, "state": self._entry(job_id).state},
    )

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(dispatch_service.DispatchService, "deploy_async", fake_deploy_async)

    status = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )

    assert status["consumed"] == [repeated.spec.job_id]
    assert len(status["launched"]) == 1
    replacement = service._entry(status["launched"][0])
    assert replacement.spec.archetype == "negation_probe"
    assert {service._entry(job_id).spec.archetype for job_id in status["active_jobs"]} == {
        "deep_search",
        "negation_probe",
    }
    cooldown = status["semantic_lane_cooldowns"]["empirical"]
    assert cooldown["job_id"] == repeated.spec.job_id
    assert cooldown["reason"] == "repeated_mechanism_without_material_coverage"


def test_spent_negation_rotation_launches_decomposition_without_changing_baseline(
    monkeypatch, tmp_path
):
    """The second fallback is decomposition after empirical then negation."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-decomposition-rotation"
    target_symbol = "demo"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    prior = _checked_residue_lane_entry(
        job_id=f"{campaign_id}.orchestrator.em-prior",
        target_symbol=target_symbol,
        active_file=active_file,
        modulus=41,
        residue=10,
        consumed=True,
    )
    repeated = _checked_residue_lane_entry(
        job_id=f"{campaign_id}.orchestrator.em-repeat",
        target_symbol=target_symbol,
        active_file=active_file,
        modulus=83,
        residue=41,
    )
    killed_empirical = LedgerEntry(
        spec=replace(
            repeated.spec,
            job_id=f"{campaign_id}.orchestrator.em-killed",
            inputs={
                **repeated.spec.inputs,
                "route_key": "epoch-refresh-cleanup",
                "route_signature": "cleanup-is-not-semantics",
            },
        ),
        state="killed",
        finished_at="2026-07-17T12:01:30+00:00",
    )
    negation_spec = research_portfolio._job_spec(
        service,
        archetype="negation_probe",
        generation=1,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
    )
    deep_spec = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
    )
    negation = LedgerEntry(
        spec=negation_spec,
        state="done",
        finished_at="2026-07-17T12:01:00+00:00",
        result={
            "status": "done",
            "deliverable": {
                "status": "inconclusive",
                "summary": "Bounded negation tactics found no contradiction.",
            },
            "artifact_paths": [],
            "plan_delta": [],
        },
    )
    for entry in (
        prior,
        repeated,
        killed_empirical,
        negation,
        LedgerEntry(
            spec=deep_spec,
            state="running",
            started_at="2026-07-17T12:02:00+00:00",
        ),
    ):
        service._save_entry(entry)

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "poll",
        lambda self, job_id: {"job_id": job_id, "state": self._entry(job_id).state},
    )

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(dispatch_service.DispatchService, "deploy_async", fake_deploy_async)

    status = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )

    assert research_portfolio._desired_archetypes(2, 2) == ["deep_search", "empirical"]
    assert research_portfolio._desired_archetypes(2, 4) == [
        "deep_search",
        "empirical",
        "negation_probe",
        "decomposition",
    ]
    assert len(status["launched"]) == 1
    replacement = service._entry(status["launched"][0])
    assert replacement.spec.archetype == "decomposition"
    assert replacement.spec.deliverable == "decomposition_report"
    assert replacement.spec.toolsets == ("web-research", "lean-research")
    assert replacement.spec.scope["scratch_only"] is True
    assert replacement.spec.job_id.rpartition(".")[2].startswith("dc-")
    assert status["semantic_lane_cooldowns"]["negation_probe"]["reason"] == (
        "no_classified_mathematical_semantics"
    )
    assert {service._entry(job_id).spec.archetype for job_id in status["active_jobs"]} == {
        "deep_search",
        "decomposition",
    }


def test_decomposition_routes_never_repeat_assignment_objectives(tmp_path):
    """Finite and fallback decomposition turns have unique durable objectives."""
    service = dispatch_service.DispatchService(root_job_id="campaign-decomposition-routes")
    target_symbol = "demo"
    active_file = str(tmp_path / "Main.lean")
    entries: list[LedgerEntry] = []
    signatures: list[str] = []
    objectives: list[str] = []
    for generation in range(1, len(research_portfolio._ROUTE_FOCUSES["decomposition"]) + 3):
        route_key, route_focus, anchor_job_id = research_portfolio._select_distinct_route(
            entries,
            archetype="decomposition",
            generation=generation,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        assert anchor_job_id == ""
        spec = research_portfolio._job_spec(
            service,
            archetype="decomposition",
            generation=generation,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=2,
            route_key=route_key,
            route_focus=route_focus,
        )
        entry = LedgerEntry(spec=spec, state="failed", notes="proposal route spent")
        entries.append(entry)
        service._save_entry(entry)
        signatures.append(str(spec.inputs["route_signature"]))
        objectives.append(research_portfolio._normalized_route_objective(spec.objective))

    assert len(signatures) == len(set(signatures))
    assert len(objectives) == len(set(objectives))
    assert [str(entry.spec.inputs["route_key"]) for entry in entries[:3]] == [
        route_key for route_key, _focus in research_portfolio._ROUTE_FOCUSES["decomposition"]
    ]
    assert all(
        str(entry.spec.inputs["route_key"]).startswith("history-refresh:") for entry in entries[3:]
    )


def test_empirical_history_refresh_requires_cross_instance_mechanism(tmp_path):
    """A spent empirical lane cannot request another isolated finite witness."""
    target_symbol = "demo"
    active_file = str(tmp_path / "Main.lean")
    entries: list[LedgerEntry] = []
    for generation, (route_key, route_focus) in enumerate(
        research_portfolio._ROUTE_FOCUSES["empirical"],
        start=1,
    ):
        objective = research_portfolio._job_objective(
            target_symbol=target_symbol,
            active_file=active_file,
            generation=generation,
            focus=route_focus,
        )
        spec = JobSpec(
            job_id=f"campaign-empirical-routes.orchestrator.em-{generation:03d}",
            archetype="empirical",
            requester_role="orchestrator",
            objective=objective,
            budget=JobBudget(api_steps=2, wall_clock_s=30),
            deliverable="experiment_result",
            inputs={
                "target_symbol": target_symbol,
                "active_file": active_file,
                "route_key": route_key,
                "route_signature": research_portfolio._stable_route_signature(
                    archetype="empirical",
                    target_symbol=target_symbol,
                    active_file=active_file,
                    objective=objective,
                ),
            },
            parent_job_id="campaign-empirical-routes.orchestrator",
        )
        entries.append(LedgerEntry(spec=spec, state="failed", notes="route spent"))

    route_key, route_focus, anchor_job_id = research_portfolio._select_distinct_route(
        entries,
        archetype="empirical",
        generation=len(entries) + 1,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert route_key.startswith("history-refresh:")
    assert anchor_job_id == ""
    assert "cross-instance invariant or parametric construction" in route_focus
    assert "do not return another isolated fixed or bounded instance" in route_focus
    assert "next uncovered instance" not in route_focus


def test_semantic_lane_cooldown_is_scoped_to_exact_assignment(tmp_path):
    """A saturated theorem cannot cool another target or another source file."""
    active_file = str(tmp_path / "Main.lean")
    entries = [
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-prior",
            target_symbol="demo",
            active_file=active_file,
            modulus=41,
            residue=10,
            consumed=True,
        ),
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-repeat",
            target_symbol="demo",
            active_file=active_file,
            modulus=83,
            residue=41,
        ),
    ]

    exact = research_portfolio._semantic_lane_cooldowns(
        entries,
        target_symbol="demo",
        active_file=active_file,
    )
    other_target = research_portfolio._semantic_lane_cooldowns(
        entries,
        target_symbol="other_demo",
        active_file=active_file,
    )
    other_file = research_portfolio._semantic_lane_cooldowns(
        entries,
        target_symbol="demo",
        active_file=str(tmp_path / "Other.lean"),
    )

    assert exact["empirical"]["classification"] == "mechanism_repeat"
    assert exact["empirical"]["reason"] == "repeated_mechanism_without_material_coverage"
    assert other_target == {}
    assert other_file == {}


def test_explicit_nonclosing_negation_result_rotates_the_lane(tmp_path):
    """A checked but non-closing negation corollary spends its portfolio lane."""
    target_symbol = "erdos_242_residual_mod_seven_eq_one"
    active_file = str(tmp_path / "Erdos242.lean")
    declaration = (
        "private lemma erdos_242_no_counterexample_bounded :\n"
        "    ¬ ∃ k : ℕ, k ≤ 84 ∧ ¬ True := by\n"
        "  simp"
    )
    objective = "Check a bounded negation corollary for the current target."
    entry = LedgerEntry(
        spec=JobSpec(
            job_id="campaign.orchestrator.np-nonclosing",
            archetype="negation_probe",
            requester_role="orchestrator",
            objective=objective,
            budget=JobBudget(api_steps=2, wall_clock_s=30),
            deliverable="negation_probe",
            inputs={
                "target_symbol": target_symbol,
                "active_file": active_file,
                "route_key": "bounded-formal-negation",
                "route_signature": research_portfolio._stable_route_signature(
                    archetype="negation_probe",
                    target_symbol=target_symbol,
                    active_file=active_file,
                    objective=objective,
                ),
            },
            parent_job_id="campaign.orchestrator",
        ),
        state="done",
        finished_at="2026-07-18T10:37:21+00:00",
        result={
            "status": "done",
            "deliverable": {
                "status": "bounded_negation_checked_nonclosing",
                "checked_helpers": [
                    {
                        "anchor_target_symbol": target_symbol,
                        "active_file": active_file,
                        "declaration": declaration,
                        "declaration_sha256": sha256(declaration.encode("utf-8")).hexdigest(),
                        "worker_check": {
                            "tool": "lean_incremental_check",
                            "action": "check_helper",
                            "valid_without_sorry": True,
                            "has_errors": False,
                            "has_sorry": False,
                            "replacement_matches_target": False,
                        },
                    }
                ],
            },
        },
    )

    cooldowns = research_portfolio._semantic_lane_cooldowns(
        [entry],
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert cooldowns["negation_probe"]["classification"] == "nonclosing"
    assert cooldowns["negation_probe"]["reason"] == "explicit_nonclosing_result"


def test_semantic_lane_cooldown_fails_closed_across_assignment_revisions(tmp_path):
    """Legacy or stale-statement evidence cannot cool the current declaration."""
    active_file = str(tmp_path / "Main.lean")
    legacy_entries = [
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-prior",
            target_symbol="demo",
            active_file=active_file,
            modulus=41,
            residue=10,
            consumed=True,
        ),
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-repeat",
            target_symbol="demo",
            active_file=active_file,
            modulus=83,
            residue=41,
        ),
    ]

    assert (
        research_portfolio._semantic_lane_cooldowns(
            legacy_entries,
            target_symbol="demo",
            active_file=active_file,
            assignment_revision="current-statement-sha",
        )
        == {}
    )

    stale_entries = [
        replace(
            entry,
            spec=replace(
                entry.spec,
                inputs={
                    **entry.spec.inputs,
                    ASSIGNMENT_REVISION_INPUT_KEY: "old-statement-sha",
                },
            ),
        )
        for entry in legacy_entries
    ]
    assert (
        research_portfolio._semantic_lane_cooldowns(
            stale_entries,
            target_symbol="demo",
            active_file=active_file,
            assignment_revision="current-statement-sha",
        )
        == {}
    )

    current_entries = [
        replace(
            entry,
            spec=replace(
                entry.spec,
                inputs={
                    **entry.spec.inputs,
                    ASSIGNMENT_REVISION_INPUT_KEY: "current-statement-sha",
                },
            ),
        )
        for entry in legacy_entries
    ]
    cooldowns = research_portfolio._semantic_lane_cooldowns(
        current_entries,
        target_symbol="demo",
        active_file=active_file,
        assignment_revision="current-statement-sha",
    )
    assert cooldowns["empirical"]["reason"] == ("repeated_mechanism_without_material_coverage")


def test_empty_killed_row_cannot_erase_latest_empirical_saturation(tmp_path):
    """Epoch cleanup remains operational metadata, never fresh lane semantics."""
    active_file = str(tmp_path / "Main.lean")
    prior = _checked_residue_lane_entry(
        job_id="campaign.orchestrator.em-prior",
        target_symbol="demo",
        active_file=active_file,
        modulus=41,
        residue=10,
        consumed=True,
    )
    saturated = _checked_residue_lane_entry(
        job_id="campaign.orchestrator.em-saturated",
        target_symbol="demo",
        active_file=active_file,
        modulus=83,
        residue=41,
    )
    killed = LedgerEntry(
        spec=replace(
            saturated.spec,
            job_id="campaign.orchestrator.em-killed",
            inputs={
                **saturated.spec.inputs,
                "route_key": "epoch-refresh-cleanup",
                "route_signature": "cleanup-is-not-semantics",
            },
        ),
        state="killed",
        finished_at="2026-07-17T12:03:00+00:00",
    )

    cooldowns = research_portfolio._semantic_lane_cooldowns(
        [prior, saturated, killed],
        target_symbol="demo",
        active_file=active_file,
    )

    assert cooldowns["empirical"]["job_id"] == saturated.spec.job_id
    assert cooldowns["empirical"]["reason"] == ("repeated_mechanism_without_material_coverage")


def test_semantic_lane_uses_completion_time_with_stable_legacy_fallback(tmp_path):
    """Overlapping rows rotate by completion time, never ledger insertion order."""
    active_file = str(tmp_path / "Main.lean")
    prior = replace(
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-prior",
            target_symbol="demo",
            active_file=active_file,
            modulus=41,
            residue=10,
            consumed=True,
        ),
        created_at="2026-07-17T11:58:00+00:00",
        finished_at="2026-07-17T11:59:00+00:00",
    )
    missing_timestamp = replace(
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-missing",
            target_symbol="demo",
            active_file=active_file,
            modulus=59,
            residue=12,
        ),
        created_at="2026-07-17T11:59:30+00:00",
        finished_at="",
    )
    malformed_timestamp = replace(
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-malformed",
            target_symbol="demo",
            active_file=active_file,
            modulus=61,
            residue=13,
        ),
        created_at="2026-07-17T11:59:45+00:00",
        finished_at="not-an-iso-timestamp",
    )
    slow_z = replace(
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-z",
            target_symbol="demo",
            active_file=active_file,
            modulus=83,
            residue=41,
        ),
        created_at="2026-07-17T12:00:00+00:00",
        finished_at="2026-07-17T12:05:00+00:00",
    )
    fast = replace(
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-fast",
            target_symbol="demo",
            active_file=active_file,
            modulus=89,
            residue=43,
        ),
        created_at="2026-07-17T12:01:00+00:00",
        finished_at="2026-07-17T12:03:00+00:00",
    )
    tied_a = replace(
        _checked_residue_lane_entry(
            job_id="campaign.orchestrator.em-a",
            target_symbol="demo",
            active_file=active_file,
            modulus=97,
            residue=47,
        ),
        created_at="2026-07-17T12:02:00+00:00",
        finished_at="2026-07-17T12:05:00+00:00",
    )
    killed = LedgerEntry(
        spec=replace(
            fast.spec,
            job_id="campaign.orchestrator.em-killed",
            inputs={
                **fast.spec.inputs,
                "route_key": "epoch-refresh-cleanup",
                "route_signature": "cleanup-is-not-semantics",
            },
        ),
        state="killed",
        created_at="2026-07-17T12:03:00+00:00",
        finished_at="2026-07-17T12:06:00+00:00",
    )

    cooldowns = research_portfolio._semantic_lane_cooldowns(
        [
            prior,
            missing_timestamp,
            malformed_timestamp,
            slow_z,
            fast,
            tied_a,
            killed,
        ],
        target_symbol="demo",
        active_file=active_file,
    )

    assert cooldowns["empirical"]["job_id"] == slow_z.spec.job_id
    assert cooldowns["empirical"]["reason"] == ("repeated_mechanism_without_material_coverage")


def test_novel_checked_obstruction_seeds_exactly_one_focused_refresh(tmp_path):
    """A checked countermodel gets one dependency audit, never a generic cascade."""
    campaign_id = "campaign-checked-obstruction"
    target_symbol = "demo"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    entries: list[LedgerEntry] = []
    for generation, (route_key, route_focus) in enumerate(
        research_portfolio._ROUTE_FOCUSES["empirical"][:2],
        start=1,
    ):
        spec = research_portfolio._job_spec(
            service,
            archetype="empirical",
            generation=generation,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=2,
            route_key=route_key,
            route_focus=route_focus,
        )
        entry = LedgerEntry(
            spec=spec,
            state="done",
            result={"status": "done", "deliverable": {"summary": "spent grounding route"}},
        )
        service._save_entry(entry)
        entries.append(entry)
    obstruction = _checked_obstruction_lane_entry(
        job_id=f"{campaign_id}.orchestrator.em-obstruction",
        target_symbol=target_symbol,
        active_file=active_file,
    )
    entries.append(obstruction)

    route_key, route_focus, anchor_job_id = research_portfolio._select_distinct_route(
        entries,
        archetype="empirical",
        generation=4,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert route_key == f"refresh-after:{obstruction.spec.job_id}"
    assert anchor_job_id == obstruction.spec.job_id
    assert "unresolved dependency" in route_focus
    anchored_spec = research_portfolio._job_spec(
        service,
        archetype="empirical",
        generation=4,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        route_key=route_key,
        route_focus=route_focus,
        route_anchor_job_id=anchor_job_id,
        route_anchor_entry=obstruction,
    )
    entries.append(
        LedgerEntry(
            spec=anchored_spec,
            state="done",
            result={"status": "done", "deliverable": {}},
        )
    )

    next_key, _next_focus, next_anchor = research_portfolio._select_distinct_route(
        entries,
        archetype="empirical",
        generation=5,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert next_key.startswith("history-refresh:")
    assert next_key != route_key
    assert next_anchor == ""


def test_portfolio_replacements_do_not_anchor_unclassified_route_prose(monkeypatch, tmp_path):
    """Unclassified summaries extend history without masquerading as progress."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    observed_signatures: list[str] = []
    observed_route_keys: list[str] = []

    for index in range(len(research_portfolio._ROUTE_FOCUSES["deep_search"]) + 2):
        status = research_portfolio.maintain_portfolio(
            campaign_id="campaign-demo",
            target_symbol="demo",
            active_file="Main.lean",
            attempt_count=0,
            workers=1,
        )
        assert len(status["launched"]) == 1
        job_id = status["launched"][0]
        entry = service._entry(job_id)
        observed_signatures.append(str(entry.spec.inputs["route_signature"]))
        observed_route_keys.append(str(entry.spec.inputs["route_key"]))
        service._transition(
            job_id,
            "done",
            finished_at=dispatch_service._now_iso(),
            result={
                "status": "done",
                "deliverable": {"summary": f"route finding {index}"},
                "artifact_paths": [],
                "plan_delta": [],
            },
        )

    assert len(observed_signatures) == len(set(observed_signatures))
    assert observed_route_keys[: len(research_portfolio._ROUTE_FOCUSES["deep_search"])] == [
        route_key for route_key, _focus in research_portfolio._ROUTE_FOCUSES["deep_search"]
    ]
    assert all(
        route_key.startswith("history-refresh:")
        for route_key in observed_route_keys[
            len(research_portfolio._ROUTE_FOCUSES["deep_search"]) :
        ]
    )
    refresh_entries = [
        entry
        for entry in service.entries()
        if str(entry.spec.inputs.get("route_key", "")).startswith("history-refresh:")
    ]
    assert refresh_entries
    for entry in refresh_entries:
        assert entry.spec.inputs["route_anchor_job_id"] == ""
        assert "route_anchor_provenance" not in entry.spec.inputs
        assert "route_anchor_finding_summary" not in entry.spec.inputs


def test_portfolio_route_history_is_scoped_to_the_exact_target(monkeypatch, tmp_path):
    """A different theorem may start at the first route even in the same campaign."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    first = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="first_target",
        active_file="Main.lean",
        attempt_count=0,
        workers=1,
    )
    first_id = first["launched"][0]

    transitioned = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="second_target",
        active_file="Main.lean",
        attempt_count=0,
        workers=1,
    )
    second_id = transitioned["launched"][0]
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")

    assert service._entry(first_id).state == "killed"
    assert service._entry(first_id).spec.inputs["route_key"] == "formal-library-grounding"
    assert service._entry(second_id).spec.inputs["route_key"] == "formal-library-grounding"
    assert (
        service._entry(first_id).spec.inputs["route_signature"]
        != service._entry(second_id).spec.inputs["route_signature"]
    )


def test_route_refresh_does_not_anchor_to_empty_interrupted_result(monkeypatch, tmp_path):
    """Operational interruptions are history, but not mathematical evidence to audit."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    entries = []
    for generation in range(1, len(research_portfolio._ROUTE_FOCUSES["deep_search"]) + 1):
        spec = research_portfolio._job_spec(
            service,
            archetype="deep_search",
            generation=generation,
            target_symbol="demo",
            active_file="Main.lean",
            attempt_count=0,
        )
        entries.append(
            dispatch_service.LedgerEntry(
                spec=spec,
                state="failed",
                result={
                    "status": "interrupted",
                    "deliverable": {"summary": ""},
                },
                notes="worker result status: interrupted",
            )
        )

    route_key, _focus, anchor_job_id = research_portfolio._select_distinct_route(
        entries,
        archetype="deep_search",
        generation=4,
        target_symbol="demo",
        active_file="Main.lean",
    )

    assert route_key.startswith("history-refresh:")
    assert anchor_job_id == ""


def test_portfolio_consumes_boundary_evidence_and_launches_synthesis_route(monkeypatch, tmp_path):
    """Parent harvest must preserve evidence and replace search with synthesis."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    initial = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="erdos_242_residual_mod_seven_eq_five",
        active_file="ErdosProblems/242.lean",
        attempt_count=0,
        workers=1,
    )
    first_job_id = initial["launched"][0]
    first = service._entry(first_job_id)
    boundary = dispatch_service._managed_boundary_deliverable(
        first.spec,
        {
            "kind": "managed_search_route_boundary",
            "boundary_marker": "[leanflow-native workflow step boundary]",
            "completed_tool_calls": 3,
            "evidence": [
                {
                    "tool": "lean_proof_context",
                    "arguments": '{"theorem_id":"erdos_242_residual_mod_seven_eq_five"}',
                    "result_excerpt": (
                        "Use erdos_242_factor_pair_certificate after splitting k modulo 35."
                    ),
                }
            ],
            "reasoning": ["Turn the residue observation into a checked helper."],
        },
    )
    service._transition(
        first_job_id,
        "done",
        finished_at=dispatch_service._now_iso(),
        result={
            "status": "done",
            "deliverable": boundary,
            "artifact_paths": [],
            "plan_delta": [],
        },
    )

    refreshed = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="erdos_242_residual_mod_seven_eq_five",
        active_file="ErdosProblems/242.lean",
        attempt_count=0,
        workers=1,
    )

    assert refreshed["consumed"] == [first_job_id]
    replacement_id = refreshed["launched"][0]
    replacement = service._entry(replacement_id)
    assert replacement.spec.inputs["route_key"] == f"handoff-synthesis-after:{first_job_id}"
    assert replacement.spec.inputs["route_anchor_job_id"] == first_job_id
    assert replacement.spec.inputs["route_mode"] == "evidence_synthesis"
    assert replacement.spec.inputs["route_anchor_provenance"]["job_id"] == first_job_id
    assert "factor_pair_certificate" in replacement.spec.inputs["route_anchor_finding_summary"]
    assert replacement.spec.inputs["route_anchor_consumption_key"]
    assert "without broad web/library search" in replacement.spec.objective
    assert "factor_pair_certificate" in replacement.spec.objective
    assert replacement.spec.inputs["route_signature"] != first.spec.inputs["route_signature"]
    summary = dispatch_service.read_json_file(service._summary_path())
    finding = next(item for item in summary["research_findings"] if item["job_id"] == first_job_id)
    assert finding["deliverable"]["status"] == "interrupted_with_evidence"

    replacement_boundary = dispatch_service._managed_boundary_deliverable(
        replacement.spec,
        {
            "kind": "managed_search_route_boundary",
            "boundary_marker": "[leanflow-native workflow step boundary]",
            "completed_tool_calls": 2,
            "evidence": [
                {
                    "tool": "lean_check",
                    "arguments": '{"code":"example : True := by trivial"}',
                    "result_excerpt": "The candidate compiles but does not close the target.",
                }
            ],
            "reasoning": ["The synthesis lane exhausted its bounded turn."],
        },
    )
    service._transition(
        replacement_id,
        "done",
        finished_at=dispatch_service._now_iso(),
        result={
            "status": "done",
            "deliverable": replacement_boundary,
            "artifact_paths": [],
            "plan_delta": [],
        },
    )

    rotated = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="erdos_242_residual_mod_seven_eq_five",
        active_file="ErdosProblems/242.lean",
        attempt_count=0,
        workers=1,
    )

    rotated_entry = service._entry(rotated["launched"][0])
    assert not str(rotated_entry.spec.inputs["route_key"]).startswith("handoff-synthesis-after:")


def test_empirical_evidence_to_helper_carries_live_em121_finding_into_em122_prompt(
    monkeypatch, tmp_path
):
    """The live em-121 witnesses must reach em-122 without a rediscovery sweep."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-demo"
    target = "erdos_242_residual_mod_seven_eq_five"
    active_file = str(tmp_path / "FormalConjectures/ErdosProblems/242.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    minted = iter(
        (
            f"{campaign_id}.orchestrator.em-120",
            f"{campaign_id}.orchestrator.em-121",
            f"{campaign_id}.orchestrator.ds-119",
        )
    )
    monkeypatch.setattr(service, "mint_job_id", lambda *_args, **_kwargs: next(minted))
    small_case_spec = research_portfolio._job_spec(
        service,
        archetype="empirical",
        generation=53,
        target_symbol=target,
        active_file=active_file,
        attempt_count=2,
        route_key="small-case-invariant",
        route_focus="test small cases and identify the strongest plausible invariant",
    )
    em121_spec = research_portfolio._job_spec(
        service,
        archetype="empirical",
        generation=54,
        target_symbol=target,
        active_file=active_file,
        attempt_count=2,
        route_key="boundary-counterexample-probe",
        route_focus="probe boundary cases and assumptions for counterexamples",
    )
    assert em121_spec.toolsets == ("lean-research", "empirical-compute")
    active_deep_search_spec = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=64,
        target_symbol=target,
        active_file=active_file,
        attempt_count=2,
        route_key="history-refresh:live",
        route_focus="investigate a distinct formal route",
    )
    em121_deliverable = {
        "boundary_cases": [
            {"k": 5, "lean_verified": True, "n": 121, "t": 0, "witness": [42, 154, 363]},
            {
                "k": 12,
                "lean_verified": True,
                "n": 289,
                "t": 1,
                "witness": [102, 289, 1734],
            },
        ],
        "gap_analysis": (
            "Existing easy cases handle t%5 in {2,3,4}; unresolved residues are {0,1}. "
            "Both boundary residues have Lean-verified witnesses."
        ),
        "mode": "boundary-counterexample-probe",
        "parameterization": "k = 7*t + 5 and 24*k+1 = 168*t+121",
        "sweep": {
            "cases_tested": 21,
            "counterexamples_found": 0,
            "range": "t=0..20",
        },
        "target": target,
    }
    service._save_entry(
        dispatch_service.LedgerEntry(
            spec=small_case_spec,
            state="done",
            result={
                "status": "done",
                "deliverable": {"summary": "small-case sweep completed"},
            },
            consumed=True,
        )
    )
    service._save_entry(
        dispatch_service.LedgerEntry(
            spec=em121_spec,
            state="done",
            result={
                "status": "done",
                "deliverable": em121_deliverable,
                "artifact_paths": [],
                "plan_delta": [],
            },
        )
    )
    service._save_entry(
        dispatch_service.LedgerEntry(
            spec=active_deep_search_spec,
            state="running",
            started_at=dispatch_service._now_iso(),
        )
    )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "poll",
        lambda self, job_id: {"job_id": job_id, "state": self._entry(job_id).state},
    )
    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "mint_job_id",
        lambda *_args, **_kwargs: f"{campaign_id}.orchestrator.em-122",
    )

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(dispatch_service.DispatchService, "deploy_async", fake_deploy_async)

    maintained = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )

    em121_id = em121_spec.job_id
    assert maintained["consumed"] == [em121_id]
    assert maintained["launched"] == [f"{campaign_id}.orchestrator.em-122"]
    em122 = service._entry(maintained["launched"][0])
    assert em122.spec.inputs["route_key"] == "evidence-to-helper"
    assert em122.spec.inputs["route_anchor_job_id"] == em121_id
    assert em122.spec.inputs["route_mode"] == "evidence_synthesis"
    provenance = em122.spec.inputs["route_anchor_provenance"]
    assert provenance["job_id"] == em121_id
    assert provenance["route_key"] == "boundary-counterexample-probe"
    source_finding = json.loads(em122.spec.inputs["route_anchor_finding_summary"])
    assert source_finding["deliverable"]["boundary_cases"][0]["witness"] == [42, 154, 363]
    assert source_finding["deliverable"]["sweep"]["cases_tested"] == 21
    assert em122.spec.inputs["route_anchor_finding_truncated"] is False
    assert "do not rerun its sweep" in em122.spec.objective
    assert research_portfolio._anchor_already_consumed(
        service.entries(),
        route_key="evidence-to-helper",
        anchor=service._entry(em121_id),
    )

    captured: dict[str, object] = {}

    def fake_delegate_task(**kwargs):
        captured.update(kwargs)
        return '{"results":[{"status":"completed","summary":"{}","api_calls":1}]}'

    import tools.implementations.delegate_tool as delegate_tool

    monkeypatch.setattr(delegate_tool, "delegate_task", fake_delegate_task)
    dispatch_service.DispatchService(parent_agent=object())._run_delegate_job(em122.spec)

    prompt_context = str(captured["context"])
    assert "consume it directly instead of rediscovering it" in prompt_context
    assert em121_id in prompt_context
    assert '"witness":[42,154,363]' in prompt_context


def test_anchored_followup_reserves_source_only_while_it_can_subsume_delivery():
    """An em-646-style source waits for em-647, but survives a failed follow-up."""
    campaign_id = "campaign-demo"
    target = "erdos_242_residual_mod_seven_eq_one_normalized_of_mod_five_two"
    active_file = "/tmp/FormalConjectures/ErdosProblems/242.lean"
    source_id = f"{campaign_id}.orchestrator.em-646"
    followup_id = f"{campaign_id}.orchestrator.em-647"

    def spec(job_id: str, *, anchored: bool = False) -> JobSpec:
        inputs = {
            "campaign_id": campaign_id,
            "target_symbol": target,
            "active_file": active_file,
        }
        if anchored:
            inputs.update(
                {
                    "route_key": "evidence-to-helper",
                    "route_mode": "evidence_synthesis",
                    "route_anchor_job_id": source_id,
                    "route_anchor_consumption_key": "em646-to-helper",
                    "route_anchor_provenance": {
                        "job_id": source_id,
                        "target_symbol": target,
                        "active_file": active_file,
                    },
                }
            )
        return JobSpec(
            job_id=job_id,
            archetype="empirical",
            requester_role="orchestrator",
            objective="synthesize the anchored source" if anchored else "find a parametric class",
            budget=JobBudget(api_steps=8, wall_clock_s=600),
            deliverable="experiment_result",
            inputs=inputs,
            parent_job_id=f"{campaign_id}.orchestrator",
        )

    source = LedgerEntry(
        spec=spec(source_id),
        state="done",
        result={"status": "done", "deliverable": {"construction": "q = 85*t + 72"}},
        consumed=True,
    )
    running_followup = LedgerEntry(
        spec=spec(followup_id, anchored=True),
        state="running",
        result={},
    )
    stale_campaign_entry = LedgerEntry(
        spec=JobSpec(
            job_id="campaign-old.orchestrator.ds-001",
            archetype="deep_search",
            requester_role="orchestrator",
            objective="historical campaign evidence",
            budget=JobBudget(api_steps=1, wall_clock_s=30),
            deliverable="findings_report",
            inputs={
                "campaign_id": "campaign-old",
                "target_symbol": target,
                "active_file": active_file,
            },
            parent_job_id="campaign-old.orchestrator",
        ),
        state="done",
        result={"status": "done", "deliverable": {"summary": "historical"}},
        consumed=True,
    )
    source_finding = {
        "job_id": source_id,
        "target_symbol": target,
        "active_file": active_file,
        "deliverable": {"construction": "q = 85*t + 72"},
    }
    unrelated_before = {
        "job_id": f"{campaign_id}.orchestrator.ds-640",
        "target_symbol": target,
        "active_file": active_file,
        "deliverable": {"obstruction": "unrelated earlier evidence"},
    }
    unrelated_after = {
        "job_id": f"{campaign_id}.orchestrator.ds-648",
        "target_symbol": target,
        "active_file": active_file,
        "deliverable": {"obstruction": "unrelated later evidence"},
    }
    findings = (unrelated_before, source_finding, unrelated_after)

    reserved = research_portfolio.prepare_anchored_foreground_findings(
        findings,
        summary={
            "dispatch_ledger": [
                stale_campaign_entry.to_mapping(),
                source.to_mapping(),
                running_followup.to_mapping(),
            ],
        },
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert [finding["job_id"] for finding in reserved] == [
        unrelated_before["job_id"],
        unrelated_after["job_id"],
    ]

    failed_followup = replace(
        running_followup,
        state="failed",
        result={"status": "failed", "deliverable": {}},
        notes="provider failed before producing mathematical evidence",
    )
    restored = research_portfolio.prepare_anchored_foreground_findings(
        findings,
        summary={"dispatch_ledger": [source.to_mapping(), failed_followup.to_mapping()]},
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert [finding["job_id"] for finding in restored] == [
        unrelated_before["job_id"],
        source_id,
        unrelated_after["job_id"],
    ]


def test_substantive_anchored_followup_leads_and_couples_source_receipt():
    """Deliver em-647 first and retire em-646 through the same foreground receipt."""
    campaign_id = "campaign-demo"
    target = "erdos_242_residual_mod_seven_eq_one_normalized_of_mod_five_two"
    active_file = "/tmp/FormalConjectures/ErdosProblems/242.lean"
    source_id = f"{campaign_id}.orchestrator.em-646"
    followup_id = f"{campaign_id}.orchestrator.em-647"
    common_inputs = {
        "campaign_id": campaign_id,
        "target_symbol": target,
        "active_file": active_file,
    }
    helper_name = "erdos_242_q_85t_72"
    helper_declaration = f"private lemma {helper_name} : True := by trivial"
    helper_deliverable = {
        "checked_helper_status": "worker_checked_parent_recheck_required",
        "parent_recheck_required": True,
        "checked_helpers": [
            {
                "anchor_target_symbol": target,
                "active_file": active_file,
                "declaration": helper_declaration,
                "declaration_sha256": sha256(helper_declaration.encode()).hexdigest(),
                "parent_recheck_required": True,
                "worker_check": {
                    "tool": "lean_incremental_check",
                    "action": "check_helper",
                    "valid_without_sorry": True,
                    "has_errors": False,
                    "has_sorry": False,
                    "verification_scope": "helper_candidate",
                    "replacement_matches_target": False,
                    "replacement_declarations": [helper_name],
                },
            }
        ],
        "summary": "checked q = 85*t + 72 helper",
    }
    source = LedgerEntry(
        spec=JobSpec(
            job_id=source_id,
            archetype="empirical",
            requester_role="orchestrator",
            objective="find a parametric class",
            budget=JobBudget(api_steps=8, wall_clock_s=600),
            deliverable="experiment_result",
            inputs=common_inputs,
            parent_job_id=f"{campaign_id}.orchestrator",
        ),
        state="done",
        result={"status": "done", "deliverable": {"construction": "q = 85*t + 72"}},
        consumed=True,
    )
    followup = LedgerEntry(
        spec=JobSpec(
            job_id=followup_id,
            archetype="empirical",
            requester_role="orchestrator",
            objective="turn the anchored class into a checked helper",
            budget=JobBudget(api_steps=8, wall_clock_s=600),
            deliverable="experiment_result",
            inputs={
                **common_inputs,
                "route_key": "evidence-to-helper",
                "route_mode": "evidence_synthesis",
                "route_anchor_job_id": source_id,
                "route_anchor_consumption_key": "em646-to-helper",
                "route_anchor_provenance": {
                    "job_id": source_id,
                    "target_symbol": target,
                    "active_file": active_file,
                    "finding_sha256": "source-sha256",
                },
            },
            parent_job_id=f"{campaign_id}.orchestrator",
        ),
        state="done",
        result={
            "status": "done",
            "deliverable": helper_deliverable,
        },
        consumed=True,
    )
    unrelated = {
        "job_id": f"{campaign_id}.orchestrator.ds-640",
        "target_symbol": target,
        "active_file": active_file,
        "deliverable": {"obstruction": "earlier unrelated evidence"},
    }
    source_finding = {
        "job_id": source_id,
        "target_symbol": target,
        "active_file": active_file,
        "deliverable": {"construction": "q = 85*t + 72"},
    }
    followup_finding = {
        "job_id": followup_id,
        "target_symbol": target,
        "active_file": active_file,
        "deliverable": helper_deliverable,
    }

    prepared = research_portfolio.prepare_anchored_foreground_findings(
        (unrelated, source_finding, followup_finding),
        summary={"dispatch_ledger": [source.to_mapping(), followup.to_mapping()]},
        campaign_id=campaign_id,
        target_symbol=target,
        active_file=active_file,
    )

    assert [finding["job_id"] for finding in prepared] == [followup_id, unrelated["job_id"]]
    anchor_delivery = prepared[0]["route_anchor_delivery"]
    assert anchor_delivery["source_job_id"] == source_id
    assert anchor_delivery["route_anchor_consumption_key"] == "em646-to-helper"
    assert anchor_delivery["route_anchor_provenance"]["job_id"] == source_id
    assert "consumes" in anchor_delivery["policy"]
    assert research_portfolio.foreground_delivery_job_ids(prepared[0]) == (
        followup_id,
        source_id,
    )


def test_assignment_transition_retires_stale_worker_and_refills_current_target(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )

    first = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="eq_four",
        active_file="Main.lean",
        attempt_count=0,
        workers=1,
    )
    stale_job = first["launched"][0]

    transitioned = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="eq_five",
        active_file="Main.lean",
        attempt_count=0,
        workers=1,
    )

    assert transitioned["active"] == 1
    assert len(transitioned["launched"]) == 1
    replacement = transitioned["launched"][0]
    assert replacement != stale_job
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    assert service._entry(stale_job).state == "killed"
    assert service._entry(replacement).state == "running"
    assert service._entry(replacement).spec.inputs["target_symbol"] == "eq_five"


def test_assignment_transition_does_not_overlap_unretired_exact_process(monkeypatch, tmp_path):
    """A stale assignment keeps its actor slot until exact process exit."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(dispatch_service.DispatchService, "deploy_async", fake_deploy_async)
    first = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="eq_four",
        active_file="Main.lean",
        attempt_count=0,
        workers=1,
    )
    stale_job = first["launched"][0]
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    service._save_entry(
        replace(
            service._entry(stale_job),
            process_id=4242,
            process_group_id=4242,
            process_session_id=4242,
            process_token_sha256="a" * 64,
        )
    )
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_has_exited",
        lambda _entry: False,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_is_live",
        lambda _entry: True,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_terminate_dispatch_process_and_wait",
        lambda _entry: False,
    )

    transitioned = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="eq_five",
        active_file="Main.lean",
        attempt_count=0,
        workers=1,
    )

    assert transitioned["active"] == 1
    assert transitioned["active_jobs"] == [stale_job]
    assert transitioned["launched"] == []
    assert service._entry(stale_job).state == "running"


def test_scope_entry_releases_other_dispatch_worker_pid_without_signal_and_launches(
    monkeypatch, tmp_path
):
    """A different worker spec frees the ghost lane without signaling its PID."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-scope-legacy-pid"
    target_symbol = "erdos_242_family_one_yz_candidates"
    active_file = str(tmp_path / "ErdosProblems" / "242.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    legacy_entry = _seed_legacy_killed_process(
        service,
        campaign_id=campaign_id,
        target_symbol="no_witness_ten_sixty_one",
        active_file=active_file,
        suffix="ds-047",
    )
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_has_exited",
        lambda _entry: False,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_read_process_argv",
        lambda _process_id, **_kwargs: (
            sys.executable,
            "-m",
            dispatch_service.DISPATCH_WORKER_MODULE,
            "--spec-file",
            str(tmp_path / ".leanflow" / "workflow-state" / "dispatch-jobs" / "other.spec.json"),
        ),
    )
    monkeypatch.setattr(
        dispatch_service,
        "_terminate_dispatch_process_and_wait",
        lambda _entry: pytest.fail("a mismatched PID must never be signaled"),
    )

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda event_type, message, **details: events.append((event_type, message, details)),
    )
    monkeypatch.setattr(research_portfolio, "read_workflow_activity", lambda **_kwargs: [])
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_enabled", lambda: True)
    monkeypatch.setattr(runner, "_maybe_sync_plan_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_statement_fidelity_audit", lambda *args, **kwargs: "pass")
    monkeypatch.setattr(
        runner, "_migrate_research_findings_for_assignment", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(runner, "_take_research_findings_prompt", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        runner,
        "_research_portfolio_poll_request",
        lambda *args, **kwargs: runner._ResearchPortfolioPollRequest(
            campaign_id=campaign_id,
            campaign_epoch=1,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=0,
            workers=2,
        ),
    )
    monkeypatch.setattr(
        runner.campaign_epoch,
        "pending_worker_refresh",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        runner, "_publish_research_portfolio_completion_events", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        runner, "_retry_deferred_scratch_artifact_cleanup", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        runner,
        "_orchestrator_consult",
        lambda *args, **kwargs: orchestrator.OrchestratorRoute(
            route="direct-prove",
            reason="start foreground proving while grounding runs",
        ),
    )
    state: dict = {}

    prompt = runner._research_scope_entry_setup(
        "start",
        state,
        {"target_symbol": target_symbol, "active_file": active_file},
    )

    portfolio = state["research_portfolio"]
    assert portfolio["active"] == 1
    assert len(portfolio["launched"]) == 1
    launched = service._entry(portfolio["launched"][0])
    assert launched.state == "running"
    assert launched.spec.archetype == "deep_search"
    assert launched.spec.inputs["target_symbol"] == target_symbol
    legacy = service._entry(legacy_entry.spec.job_id)
    assert legacy.process_release_reason == "legacy-dispatch-worker-spec-mismatch"
    assert legacy.process_released_at
    assert legacy.process_release_reported_at
    release_events = [
        event for event in events if event[0] == "research-portfolio-capacity-released"
    ]
    assert len(release_events) == 1
    assert release_events[0][2]["job_id"] == legacy_entry.spec.job_id
    assert release_events[0][2]["release_report_key"] == legacy.process_release_report_key
    assert "[ORCHESTRATOR SCOPE-ENTRY ROUTE]" in prompt


def test_release_activity_failure_does_not_block_launch_and_retries(monkeypatch, tmp_path):
    """A durable unreported tombstone retries without reclaiming its actor slot."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-release-activity-retry"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    legacy = _seed_legacy_killed_process(
        service,
        campaign_id=campaign_id,
        target_symbol="old_target",
        active_file=active_file,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_has_exited",
        lambda _entry: True,
    )

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    monkeypatch.setattr(research_portfolio, "read_workflow_activity", lambda **_kwargs: [])
    release_attempts = 0
    release_events: list[dict] = []

    def flaky_append(event_type, _message, **details):
        nonlocal release_attempts
        if event_type != "research-portfolio-capacity-released":
            return
        release_attempts += 1
        if release_attempts == 1:
            raise OSError("activity stream unavailable")
        release_events.append(details)

    monkeypatch.setattr(research_portfolio, "append_workflow_activity", flaky_append)

    first = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="current_target",
        active_file=active_file,
        attempt_count=0,
        workers=1,
    )

    assert len(first["launched"]) == 1
    after_failure = service._entry(legacy.spec.job_id)
    assert after_failure.process_release_reason == "legacy-process-exited"
    assert after_failure.process_release_reported_at == ""

    second = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="current_target",
        active_file=active_file,
        attempt_count=0,
        workers=1,
    )

    assert second["active"] == 1
    assert release_attempts == 2
    assert len(release_events) == 1
    persisted = service._entry(legacy.spec.job_id)
    assert persisted.process_release_reported_at
    assert release_events[0]["release_report_key"] == persisted.process_release_report_key
    assert release_events[0]["release_reason"] == "legacy-process-exited"


def test_terminal_modern_killed_process_bypasses_legacy_release_transaction(
    monkeypatch,
    tmp_path,
):
    """Exact identities retire directly without rewriting the full ledger."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-modern-terminal"
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    modern = replace(
        _seed_legacy_killed_process(
            service,
            campaign_id=campaign_id,
            target_symbol="old_target",
            active_file=str(tmp_path / "Main.lean"),
        ),
        launch_nonce="modern-launch",
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
    )
    service._save_entry(modern)
    monkeypatch.setattr(
        service,
        "release_legacy_killed_process_capacity",
        lambda _entry: pytest.fail("modern identity entered the legacy ledger transaction"),
    )
    retired: list[str] = []
    monkeypatch.setattr(
        dispatch_service,
        "_terminate_dispatch_process_and_wait",
        lambda entry: retired.append(entry.spec.job_id) or True,
    )

    assert research_portfolio._terminal_killed_process_released(modern, service=service)
    assert retired == [modern.spec.job_id]


def test_terminal_modern_killed_reused_pid_releases_on_command_mismatch(
    monkeypatch,
    tmp_path,
):
    """A reused PID/group/session triple cannot pin a killed modern worker."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-modern-reused-pid"
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    modern = replace(
        _seed_legacy_killed_process(
            service,
            campaign_id=campaign_id,
            target_symbol="old_target",
            active_file=str(tmp_path / "Main.lean"),
        ),
        launch_nonce="modern-launch",
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
    )
    service._save_entry(modern)
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_has_exited",
        lambda _entry: False,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_is_live",
        lambda _entry: False,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_read_process_argv",
        lambda _process_id, **_kwargs: ("/usr/bin/unrelated-service",),
    )
    monkeypatch.setattr(
        dispatch_service,
        "_terminate_dispatch_process_and_wait",
        lambda _entry: False,
    )

    assert research_portfolio._terminal_killed_process_released(modern, service=service)
    persisted = service._entry(modern.spec.job_id)
    assert persisted.process_release_reason == "process-command-mismatch"
    assert persisted.process_released_at


def test_release_activity_append_marker_crash_deduplicates_on_retry(monkeypatch, tmp_path):
    """An append-success/marker-crash window is repaired without a duplicate."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-release-activity-crash"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    legacy = _seed_legacy_killed_process(
        service,
        campaign_id=campaign_id,
        target_symbol="old_target",
        active_file=active_file,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_has_exited",
        lambda _entry: True,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_terminate_dispatch_process_and_wait",
        lambda _entry: pytest.fail("released capacity must not enter termination"),
    )
    persisted_events: list[dict] = []

    def append_event(event_type, _message, **details):
        persisted_events.append(
            {
                "timestamp": "2026-07-18T10:00:00+00:00",
                "type": event_type,
                "details": details,
            }
        )

    monkeypatch.setattr(research_portfolio, "append_workflow_activity", append_event)
    monkeypatch.setattr(
        research_portfolio,
        "read_workflow_activity",
        lambda **_kwargs: list(persisted_events),
    )
    original_mark = dispatch_service.DispatchService.mark_process_release_reported
    marker_calls = 0

    def fail_first_marker(self, **kwargs):
        nonlocal marker_calls
        marker_calls += 1
        if marker_calls == 1:
            raise OSError("simulated crash after append")
        return original_mark(self, **kwargs)

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "mark_process_release_reported",
        fail_first_marker,
    )

    assert research_portfolio._terminal_killed_process_released(legacy, service=service)
    assert service._entry(legacy.spec.job_id).process_release_reported_at == ""
    assert research_portfolio._terminal_killed_process_released(
        service._entry(legacy.spec.job_id),
        service=service,
    )

    assert len(persisted_events) == 1
    assert marker_calls == 2
    reported = service._entry(legacy.spec.job_id)
    assert reported.process_release_reported_at == "2026-07-18T10:00:00+00:00"


def test_research_scope_entry_starts_grounding_and_records_route_before_foreground(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_enabled", lambda: True)
    monkeypatch.setattr(
        runner, "_maybe_sync_plan_state", lambda *args, **kwargs: calls.append("plan")
    )
    monkeypatch.setattr(
        runner,
        "_maybe_statement_fidelity_audit",
        lambda *args, **kwargs: calls.append("fidelity") or "pass",
    )
    monkeypatch.setattr(
        runner, "_maintain_research_portfolio", lambda *args, **kwargs: calls.append("grounding")
    )
    monkeypatch.setattr(
        runner,
        "_take_research_findings_prompt",
        lambda *args, **kwargs: calls.append("findings") or "[findings]",
    )
    monkeypatch.setattr(
        runner,
        "_orchestrator_consult",
        lambda *args, **kwargs: calls.append("route")
        or orchestrator.OrchestratorRoute(
            route="direct-prove",
            reason="start foreground proof while grounding runs",
        ),
    )
    state: dict = {}

    prompt = runner._research_scope_entry_setup("start", state, {"target_symbol": "demo"})

    assert calls == ["plan", "fidelity", "grounding", "findings", "route"]
    assert state["orchestrator_scope_entered"] is True
    assert "[findings]" in prompt
    assert "[ORCHESTRATOR SCOPE-ENTRY ROUTE]" in prompt
    assert "direct-prove" in prompt


def test_research_scope_entry_applies_the_authoritative_route_before_prompt(monkeypatch):
    calls: list[tuple[str, str]] = []
    selected = orchestrator.OrchestratorRoute(
        route="plan",
        reason="refresh the current target strategy",
    )
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_enabled", lambda: True)
    monkeypatch.setattr(runner, "_maybe_sync_plan_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_statement_fidelity_audit", lambda *args: "pass")
    monkeypatch.setattr(runner, "_migrate_research_findings_for_assignment", lambda *args: {})
    monkeypatch.setattr(runner, "_maintain_research_portfolio", lambda *args: None)
    monkeypatch.setattr(runner, "_take_research_findings_prompt", lambda *args: "")
    monkeypatch.setattr(
        runner,
        "_orchestrator_consult",
        lambda *args, **kwargs: calls.append(("consult", selected.route)) or selected,
    )

    def apply(route, history, *_args, **_kwargs):
        calls.append(("apply", route.route))
        history.append(
            {
                "role": "user",
                "content": "[LEANFLOW ORCHESTRATOR ROUTE: plan]\n- planner phase applied",
            }
        )
        return "continue"

    monkeypatch.setattr(runner, "_apply_orchestrator_route_with_completion", apply)
    state: dict = {}

    prompt = runner._research_scope_entry_setup(
        "",
        state,
        {"target_symbol": "demo"},
        agent=object(),
        apply_route=True,
    )

    assert calls == [("consult", "plan"), ("apply", "plan")]
    assert state[runner._RESEARCH_SCOPE_ENTRY_ACTION_KEY] == "continue"
    assert "[ORCHESTRATOR SCOPE-ENTRY ROUTE]\n- route: plan" in prompt
    assert "planner phase applied" in prompt
    assert "route: decompose" not in prompt


def test_fourth_scope_entry_route_rolls_epoch_before_startup_provider_call(monkeypatch, tmp_path):
    """A startup scope consult must not defer its fourth-route rollover."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "campaign-scope-boundary")
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_enabled", lambda: True)
    monkeypatch.setattr(runner, "_maybe_sync_plan_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_statement_fidelity_audit", lambda *args, **kwargs: "pass")
    monkeypatch.setattr(runner, "_maintain_research_portfolio", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "_take_research_findings_prompt", lambda *args, **kwargs: "[findings]"
    )

    state: dict = {}
    for route_name in ("direct-prove", "plan", "decompose"):
        runner.campaign_epoch.record_route_decision(state, route=route_name)

    def fourth_consult(*args, **kwargs):
        runner.campaign_epoch.record_route_decision(state, route="direct-prove")
        return orchestrator.OrchestratorRoute(
            route="direct-prove",
            reason="continue the current proof shape",
        )

    monkeypatch.setattr(runner, "_orchestrator_consult", fourth_consult)
    prompt = runner._research_scope_entry_setup("start", state, {"target_symbol": "demo"})

    assert state["orchestrator_routes_used"] == 4
    assert state["campaign_epoch_requested"] == "route-no-graph-progress"
    assert "[findings]" in prompt
    assert "[ORCHESTRATOR SCOPE-ENTRY ROUTE]" not in prompt

    roll_calls: list[str] = []

    def fake_roll(*args, reason, **kwargs):
        roll_calls.append(reason)
        return ([{"role": "user", "content": "fresh epoch"}], {"compacted": False}, {})

    monkeypatch.setattr(runner, "_roll_autonomous_campaign_epoch", fake_roll)
    history, compaction, checkpoint, rolled = runner._roll_pending_startup_scope_epoch(
        object(), [], {}, {}, state, {"target_symbol": "demo"}
    )

    assert rolled is True
    assert roll_calls == ["route-no-graph-progress"]
    assert history == [{"role": "user", "content": "fresh epoch"}]
    assert compaction == {"compacted": False}
    assert checkpoint == {}
    assert "campaign_epoch_requested" not in state


def test_fourth_applied_scope_route_runs_before_startup_epoch_rollover(monkeypatch, tmp_path):
    """Do not count a reserved fourth strategy as attempted before it runs."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "campaign-applied-scope-boundary")
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_enabled", lambda: True)
    monkeypatch.setattr(runner.scope_entry_admission, "arm", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_sync_plan_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_statement_fidelity_audit", lambda *args, **kwargs: "pass")
    monkeypatch.setattr(runner, "_maintain_research_portfolio", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_take_research_findings_prompt", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        runner,
        "_recheck_pending_research_helper_if_due",
        lambda *args, **kwargs: "",
    )
    monkeypatch.setattr(
        runner.research_helper_candidate_priority,
        "matching",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(runner, "_pending_checked_target_replacement", lambda *args: False)
    monkeypatch.setattr(runner, "_reconcile_legacy_epoch_route_completion", lambda *args: None)

    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        }
    }
    for route_name in ("direct-prove", "plan", "negate"):
        runner.campaign_epoch.record_route_decision(state, route=route_name, limit=99)

    selected = orchestrator.OrchestratorRoute(
        route="decompose",
        reason="structurally recover after repeated verification limits",
    )

    def fourth_consult(*args, **kwargs):
        runner.campaign_epoch.record_route_decision(
            state,
            route="decompose",
            target_symbol="demo",
            active_file=str(active),
            trigger="scope-entry",
            reserve_inflight=True,
        )
        return selected

    applied: list[str] = []

    def apply(route, *_args, **_kwargs):
        applied.append(route.route)
        return "continue"

    monkeypatch.setattr(runner, "_orchestrator_consult", fourth_consult)
    monkeypatch.setattr(runner, "_apply_orchestrator_route_with_completion", apply)

    prompt = runner._research_scope_entry_setup(
        "start",
        state,
        {"target_symbol": "demo", "active_file": str(active)},
        agent=object(),
        apply_route=True,
    )

    assert applied == ["decompose"]
    assert "[ORCHESTRATOR SCOPE-ENTRY ROUTE]" in prompt
    assert state["campaign_epoch_requested"] == "route-no-graph-progress"
    scope = runner._orchestrator_event_scope(state)
    assert runner.orchestrator_event_watermark.foreground_grace_active(state, scope=scope)

    history, compaction, checkpoint, rolled = runner._roll_pending_startup_scope_epoch(
        object(), [], {}, {}, state, {"target_symbol": "demo", "active_file": str(active)}
    )

    assert rolled is False
    assert history == []
    assert compaction == {}
    assert checkpoint == {}
    assert state["campaign_epoch_requested"] == "route-no-graph-progress"


def test_shutdown_kills_every_open_portfolio_job(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    launched = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=2,
        workers=2,
    )["launched"]

    killed = research_portfolio.shutdown_portfolio(
        campaign_id="campaign-demo",
        reason="verified completion",
    )

    assert killed == launched
    states = {
        entry.spec.job_id: entry.state
        for entry in dispatch_service.DispatchService(root_job_id="campaign-demo").entries()
    }
    assert {states[job_id] for job_id in launched} == {"killed"}


def test_epoch_refresh_retires_open_workers_and_refills_distinct_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    campaign_id = "campaign-epoch-refresh"
    active_file = str(tmp_path / "Main.lean")
    first = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="hard_goal",
        active_file=active_file,
        attempt_count=3,
        workers=2,
    )
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    first_signatures = {
        service._entry(job_id).spec.inputs["route_signature"] for job_id in first["launched"]
    }

    killed = research_portfolio.refresh_portfolio_for_epoch(
        campaign_id=campaign_id,
        target_symbol="hard_goal",
        active_file=active_file,
        previous_epoch=8,
        new_epoch=9,
        reason="route-no-graph-progress",
    )
    assert set(killed) == set(first["launched"])
    assert all(service._entry(job_id).state == "killed" for job_id in killed)

    refreshed = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="hard_goal",
        active_file=active_file,
        attempt_count=3,
        workers=2,
    )
    refreshed_signatures = {
        service._entry(job_id).spec.inputs["route_signature"] for job_id in refreshed["launched"]
    }
    assert len(refreshed["launched"]) == 2
    assert refreshed_signatures.isdisjoint(first_signatures)


def test_semantic_epoch_refresh_carries_current_epoch_worker_once(monkeypatch, tmp_path):
    """Do not kill freshly launched research at a short semantic rollover."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda event, _message, **details: events.append((event, details)),
    )
    campaign_id = "campaign-semantic-carry"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    spec = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol="hard_goal",
        active_file=active_file,
        attempt_count=3,
        campaign_epoch_number=15,
    )
    service.propose(spec)
    service._transition(spec.job_id, "deployed")
    service._transition(
        spec.job_id,
        "running",
        started_at=dispatch_service._now_iso(),
    )

    killed = research_portfolio.refresh_portfolio_for_epoch(
        campaign_id=campaign_id,
        target_symbol="hard_goal",
        active_file=active_file,
        previous_epoch=15,
        new_epoch=16,
        reason=campaign_epoch.SEMANTIC_PORTFOLIO_ROLLOVER_REASON,
        refill=False,
    )

    assert killed == []
    assert service._entry(spec.job_id).state == "running"
    refresh_events = [
        details for event, details in events if event == "research-portfolio-epoch-refresh"
    ]
    assert refresh_events[-1]["carried"] == [spec.job_id]

    killed_next = research_portfolio.refresh_portfolio_for_epoch(
        campaign_id=campaign_id,
        target_symbol="hard_goal",
        active_file=active_file,
        previous_epoch=16,
        new_epoch=17,
        reason=campaign_epoch.SEMANTIC_PORTFOLIO_ROLLOVER_REASON,
        refill=False,
    )

    assert killed_next == [spec.job_id]
    assert service._entry(spec.job_id).state == "killed"


def test_fresh_epoch_relaxes_only_older_semantic_cooldowns_to_refill_distinct_routes(
    monkeypatch, tmp_path
):
    """An all-lane epoch-N cooldown cannot starve the epoch-N+1 portfolio."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "campaign-semantic-epoch-refresh")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    monkeypatch.setattr(
        research_findings,
        "foreground_use_reason",
        lambda _finding: "partial_coverage_without_completion",
    )
    state: dict = {}
    campaign = runner.campaign_epoch.ensure_campaign(state)
    campaign_id = str(campaign["campaign_id"])
    target_symbol = "hard_goal"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    cooled = _seed_epoch_cooled_lanes(
        service=service,
        target_symbol=target_symbol,
        active_file=active_file,
        campaign_epoch=1,
    )
    prior_signatures = {str(entry.spec.inputs["route_signature"]) for entry in cooled}
    prior_objectives = {
        research_portfolio._normalized_route_objective(entry.spec.objective) for entry in cooled
    }
    runner.campaign_epoch.roll_epoch(
        state,
        reason="route-no-graph-progress",
        cycle=4,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda event_type, _message, **details: events.append((event_type, details)),
    )

    refreshed = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )

    assert len(refreshed["launched"]) == 2
    launched = [service._entry(job_id) for job_id in refreshed["launched"]]
    assert {entry.spec.archetype for entry in launched} == {"deep_search", "empirical"}
    assert {entry.spec.inputs["campaign_epoch"] for entry in launched} == {2}
    assert {str(entry.spec.inputs["route_signature"]) for entry in launched}.isdisjoint(
        prior_signatures
    )
    assert {
        research_portfolio._normalized_route_objective(entry.spec.objective) for entry in launched
    }.isdisjoint(prior_objectives)
    relaxations = refreshed["semantic_lane_cooldown_relaxations"]
    assert {item["archetype"] for item in relaxations} == {"deep_search", "empirical"}
    assert {item["producing_epoch"] for item in relaxations} == {1}
    assert {item["current_epoch"] for item in relaxations} == {2}
    portfolio_event = next(details for kind, details in events if kind == "research-portfolio")
    assert portfolio_event["semantic_lane_cooldown_relaxations"] == relaxations


def test_same_epoch_semantic_cooldowns_are_never_relaxed(monkeypatch, tmp_path):
    """Current-epoch saturation remains authoritative even when every lane is cooled."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "campaign-semantic-same-epoch")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    monkeypatch.setattr(
        research_findings,
        "foreground_use_reason",
        lambda _finding: "partial_coverage_without_completion",
    )
    state: dict = {}
    campaign = runner.campaign_epoch.ensure_campaign(state)
    campaign_id = str(campaign["campaign_id"])
    target_symbol = "hard_goal"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    _seed_epoch_cooled_lanes(
        service=service,
        target_symbol=target_symbol,
        active_file=active_file,
        campaign_epoch=1,
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )

    assert status["active_jobs"] == []
    assert status["launched"] == []
    assert set(status["semantic_lane_cooldowns"]) == set(research_portfolio._ROUTE_FOCUSES)
    assert "semantic_lane_cooldown_relaxations" not in status


def test_attempt_zero_rotates_completed_partial_deep_search_without_overfilling(
    monkeypatch, tmp_path
):
    """A partial scope-entry search rotates lanes without waiting for prover rejection."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "campaign-attempt-zero-rotation")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    monkeypatch.setattr(
        research_findings,
        "foreground_use_reason",
        lambda _finding: "partial_coverage_without_completion",
    )
    campaign_id = "campaign-attempt-zero-rotation"
    target_symbol = "hard_goal"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)

    initial = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=0,
        workers=2,
    )
    completed_job_id = initial["launched"][0]
    assert service._entry(completed_job_id).spec.archetype == "deep_search"
    service._transition(
        completed_job_id,
        "done",
        finished_at=dispatch_service._now_iso(),
        result={
            "status": "done",
            "deliverable": {"summary": "useful partial route that does not close the target"},
        },
    )

    replacement = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=0,
        workers=2,
    )

    assert replacement["consumed"] == [completed_job_id]
    assert len(replacement["launched"]) == 1
    replacement_job_id = replacement["launched"][0]
    assert service._entry(replacement_job_id).spec.archetype == "empirical"
    assert replacement["active"] == 1
    assert replacement["active_jobs"] == [replacement_job_id]
    assert "replacement_pending" not in replacement
    persisted = dispatch_service.read_json_file(service._summary_path())
    assert research_portfolio.PENDING_REPLACEMENT_STATE_KEY not in persisted

    heartbeat = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=0,
        workers=2,
    )

    assert heartbeat["launched"] == []
    assert heartbeat["active"] == 1
    assert heartbeat["active_jobs"] == [replacement_job_id]


def test_uncooled_lanes_fill_capacity_before_an_older_cooldown_is_relaxed(monkeypatch, tmp_path):
    """Older-epoch relaxation is a vacancy fallback, not ordinary lane preference."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "campaign-semantic-uncooled-first")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    monkeypatch.setattr(
        research_findings,
        "foreground_use_reason",
        lambda _finding: "partial_coverage_without_completion",
    )
    state: dict = {}
    campaign = runner.campaign_epoch.ensure_campaign(state)
    campaign_id = str(campaign["campaign_id"])
    target_symbol = "hard_goal"
    active_file = str(tmp_path / "Main.lean")
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    _seed_epoch_cooled_lanes(
        service=service,
        target_symbol=target_symbol,
        active_file=active_file,
        campaign_epoch=1,
        archetypes=("empirical",),
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="route-no-graph-progress",
        cycle=4,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )

    assert {service._entry(job_id).spec.archetype for job_id in status["launched"]} == {
        "deep_search",
        "negation_probe",
    }
    assert "semantic_lane_cooldown_relaxations" not in status


def test_restart_after_epoch_commit_replays_worker_refresh_without_result_loss(
    monkeypatch, tmp_path
):
    """The durable refresh token closes the roll-commit/worker-kill crash gap."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "campaign-refresh-replay")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    state: dict = {}
    campaign = runner.campaign_epoch.ensure_campaign(state)
    campaign_id = str(campaign["campaign_id"])
    active_file = str(tmp_path / "Main.lean")
    first = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="hard_goal",
        active_file=active_file,
        attempt_count=3,
        workers=2,
    )
    service = dispatch_service.DispatchService(root_job_id=campaign_id)
    completed_job, open_job = first["launched"]
    first_signatures = {
        service._entry(job_id).spec.inputs["route_signature"] for job_id in first["launched"]
    }
    service._transition(
        completed_job,
        "done",
        finished_at=dispatch_service._now_iso(),
        result={
            "status": "done",
            "deliverable": {"summary": "preserved boundary finding"},
        },
    )

    # Simulate a crash immediately after the atomic campaign roll, before the
    # runner's best-effort eager worker-refresh call.
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="hard_goal",
        active_file=active_file,
    )
    resumed: dict = {}
    runner.campaign_epoch.ensure_campaign(resumed)
    assert resumed[runner.campaign_epoch.EPOCH_WORKER_REFRESH_STATE_KEY]["pending"] is True

    refreshed = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="hard_goal",
        active_file=active_file,
        attempt_count=3,
        workers=2,
    )

    assert completed_job in refreshed["consumed"]
    assert open_job in refreshed["epoch_refresh_killed"]
    assert service._entry(completed_job).consumed is True
    assert service._entry(open_job).state == "killed"
    assert runner.campaign_epoch.pending_worker_refresh(campaign_id=campaign_id) == {}
    refreshed_signatures = {
        service._entry(job_id).spec.inputs["route_signature"] for job_id in refreshed["launched"]
    }
    assert len(refreshed["launched"]) == 2
    assert refreshed_signatures.isdisjoint(first_signatures)
    assert {
        service._entry(job_id).spec.inputs["campaign_epoch"] for job_id in refreshed["launched"]
    } == {2}
    assert service._entry(completed_job).result["deliverable"]["summary"] == (
        "preserved boundary finding"
    )
    archive = dict(
        workflow_json_io.read_json_file(research_findings._summary_path()).get(
            research_findings.FINDING_MIGRATION_KEY
        )
        or {}
    )
    assert completed_job in dict(archive.get("records") or {})


def test_epoch_worker_refresh_stays_pending_when_retirement_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "campaign-refresh-retry")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )
    state: dict = {}
    campaign_id = str(runner.campaign_epoch.ensure_campaign(state)["campaign_id"])
    active_file = str(tmp_path / "Main.lean")
    first = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="hard_goal",
        active_file=active_file,
        attempt_count=0,
        workers=1,
    )
    old_job = first["launched"][0]
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="hard_goal",
        active_file=active_file,
    )
    original_kill = dispatch_service.DispatchService.kill

    def fail_old_worker(self, job_id, *, requester_job_id):
        if job_id == old_job:
            raise RuntimeError("transient process-reaper failure")
        return original_kill(self, job_id, requester_job_id=requester_job_id)

    monkeypatch.setattr(dispatch_service.DispatchService, "kill", fail_old_worker)

    status = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="hard_goal",
        active_file=active_file,
        attempt_count=0,
        workers=1,
    )

    assert status["epoch_refresh_pending"] is True
    assert status["launched"] == []
    assert status["active_jobs"] == [old_job]
    assert runner.campaign_epoch.pending_worker_refresh(campaign_id=campaign_id)["pending"] is True


def test_shutdown_counts_worker_already_normalized_as_killed(monkeypatch, tmp_path):
    """A concurrent interruption verdict still counts as a stopped worker."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    events: list[tuple[tuple, dict]] = []

    class _FakeService:
        root_job_id = "campaign-demo"

        def __init__(self, *, root_job_id):
            assert root_job_id == "campaign-demo"

        def open_jobs(self):
            return [
                type("Entry", (), {"spec": type("Spec", (), {"job_id": "job-a"})()})(),
                type("Entry", (), {"spec": type("Spec", (), {"job_id": "job-b"})()})(),
            ]

        def kill(self, job_id, *, requester_job_id):
            assert requester_job_id == "campaign-demo"
            return {
                "job_id": job_id,
                "state": "killed",
                "killed": job_id == "job-b",
            }

    monkeypatch.setattr(research_portfolio, "DispatchService", _FakeService)
    monkeypatch.setattr(
        research_portfolio,
        "append_workflow_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    stopped = research_portfolio._shutdown_portfolio_once(
        campaign_id="campaign-demo",
        reason="signal interrupt",
    )

    assert stopped == ["job-a", "job-b"]
    assert events[0][1]["killed"] == ["job-a", "job-b"]
    assert events[0][0][1] == "Stopped 2 research worker(s): signal interrupt"


def test_shutdown_suppresses_late_parent_heartbeat_refill(monkeypatch, tmp_path):
    """A callback finishing after process shutdown cannot launch replacement work."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-late-heartbeat"
    research_portfolio.shutdown_portfolio(
        campaign_id=campaign_id,
        reason="signal interrupt",
    )
    monkeypatch.setattr(
        research_portfolio,
        "_maintain_portfolio_once",
        lambda **_kwargs: pytest.fail("late heartbeat refilled a closed portfolio"),
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=3,
        workers=2,
    )

    assert status == {
        "active": 0,
        "active_jobs": [],
        "launched": [],
        "consumed": [],
        "shutdown": True,
    }


def test_portfolio_tick_resumes_durable_launch_before_refill(monkeypatch, tmp_path):
    """A restarted portfolio recovers a reserved lane instead of duplicating it."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    campaign_id = "campaign-launch-resume"
    service = dispatch_service.DispatchService(root_job_id=campaign_id, cap=1)
    spec = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    service.propose(spec)

    class SimulatedCrash(BaseException):
        pass

    monkeypatch.setattr(
        service,
        "_write_async_launch_spec",
        lambda _entry: (_ for _ in ()).throw(SimulatedCrash()),
    )
    with pytest.raises(SimulatedCrash):
        service.deploy_async(spec.job_id)

    class Process:
        pid = 7373

    launches: list[int] = []
    monkeypatch.setattr(dispatch_service, "ASYNC_LAUNCH_HANDSHAKE_GRACE_S", 0.0)
    monkeypatch.setattr(
        dispatch_service.subprocess,
        "Popen",
        lambda *args, **kwargs: (launches.append(1) or Process()),
    )
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_is_live",
        lambda _entry: True,
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id=campaign_id,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
        workers=1,
    )

    running = dispatch_service.DispatchService(root_job_id=campaign_id)._entry(spec.job_id)
    assert running.state == "running"
    assert running.launch_attempt == 2
    assert status["active_jobs"] == [spec.job_id]
    assert status["launched"] == []
    assert launches == [1]


def test_zero_worker_portfolio_reconciles_stale_jobs_without_refill(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    spec = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    service.propose(spec)
    service._transition(spec.job_id, "deployed")
    service._transition(
        spec.job_id,
        "running",
        process_id=999_999,
        started_at=dispatch_service._now_iso(),
    )
    monkeypatch.setattr(dispatch_service, "_process_seems_alive", lambda _pid: False)

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
        workers=0,
    )

    assert status == {"active": 0, "active_jobs": [], "launched": [], "consumed": []}
    assert service._entry(spec.job_id).state == "failed"
    assert service._entry(spec.job_id).notes == "agent process died"


def test_zero_worker_portfolio_reports_cleanup_pending_for_live_exact_process(
    monkeypatch, tmp_path
):
    """Zero capacity remains occupied when exact process exit cannot be proven."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")
    spec = research_portfolio._job_spec(
        service,
        archetype="deep_search",
        generation=1,
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
    )
    service.propose(spec)
    service._transition(spec.job_id, "deployed")
    service._transition(
        spec.job_id,
        "running",
        process_id=4242,
        process_group_id=4242,
        process_session_id=4242,
        process_token_sha256="a" * 64,
        started_at=dispatch_service._now_iso(),
    )
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_is_live",
        lambda _entry: True,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_dispatch_process_identity_has_exited",
        lambda _entry: False,
    )
    monkeypatch.setattr(
        dispatch_service,
        "_terminate_dispatch_process_and_wait",
        lambda _entry: False,
    )

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file="Main.lean",
        attempt_count=0,
        workers=0,
    )

    assert status == {
        "active": 1,
        "active_jobs": [spec.job_id],
        "launched": [],
        "consumed": [],
        "cleanup_pending": True,
        "still_active": [spec.job_id],
    }
    assert service._entry(spec.job_id).state == "running"


def test_zero_worker_runner_still_maintains_persisted_portfolio(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.research_mode, "research_worker_count", lambda: 0)
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda _state: False)
    monkeypatch.setattr(
        runner.campaign_epoch,
        "ensure_campaign",
        lambda _state: {"campaign_id": "campaign-demo"},
    )
    monkeypatch.setattr(
        runner.research_portfolio,
        "maintain_portfolio",
        lambda **kwargs: calls.append(kwargs)
        or {"active": 0, "active_jobs": [], "launched": [], "consumed": []},
    )

    runner._maintain_research_portfolio({}, {"target_symbol": "demo"})

    assert calls and calls[0]["workers"] == 0


def test_verified_state_does_not_refill_research_portfolio(monkeypatch):
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda live_state: True)
    monkeypatch.setattr(
        runner.research_portfolio,
        "maintain_portfolio",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not refill")),
    )

    runner._maintain_research_portfolio({}, {"verified": True})


def test_read_only_tool_boundary_polls_portfolio_during_foreground_turn(monkeypatch):
    calls: list[tuple[dict, dict]] = []
    staged: list[str] = []
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_maintain_research_portfolio",
        lambda state, live: calls.append((state, dict(live or {}))),
    )
    monkeypatch.setattr(
        runner,
        "_take_research_findings_prompt",
        lambda state, live: "[fresh research finding]",
    )

    class _Agent:
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
            }
        }

        def is_interrupted(self):
            return False

        def stage_tool_result_appendix(self, text):
            staged.append(text)

    runner._handle_managed_tool_result(
        _Agent(),
        "lean_reasoning_help",
        {},
        '{"success": true}',
    )

    assert len(calls) == 1
    assert calls[0][1] == {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
    }
    assert staged == ["[fresh research finding]"]


@pytest.mark.parametrize(
    "function_name",
    [
        "apply_verified_patch",
        "lean_incremental_check",
        "lean_verify",
        "patch",
        "terminal",
        "write_file",
    ],
)
def test_queue_authority_boundary_defers_portfolio_poll_to_queue_gate(monkeypatch, function_name):
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_maintain_research_portfolio",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must defer")),
    )

    class _Agent:
        _managed_autonomy_state = {}

    runner._poll_research_portfolio_after_tool_result(_Agent(), function_name)


def test_findings_are_target_scoped_and_delivered_to_foreground_once(monkeypatch):
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    summary = {
        "dispatch_ledger": [
            {
                "spec": {
                    "job_id": "campaign.ds-001",
                    "inputs": {"target_symbol": "demo", "active_file": "/tmp/Demo.lean"},
                }
            },
            {
                "spec": {
                    "job_id": "campaign.ds-002",
                    "inputs": {"target_symbol": "other", "active_file": "/tmp/Demo.lean"},
                }
            },
        ],
        "research_findings": [
            {
                "job_id": "campaign.ds-001",
                "archetype": "deep_search",
                "deliverable": {"summary": "use invariant h"},
            },
            {
                "job_id": "campaign.ds-002",
                "archetype": "deep_search",
                "deliverable": {"summary": "wrong target"},
            },
        ],
    }
    monkeypatch.setattr(runner.plan_state, "load_summary", lambda: summary)
    state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "/tmp/Demo.lean",
        }
    }

    selected = research_findings.relevant_findings(
        summary,
        target_symbol="demo",
        active_file="/private/tmp/Demo.lean",
    )
    assert [finding["job_id"] for finding in selected] == ["campaign.ds-001"]

    first = runner._take_research_findings_prompt(state, {"target_symbol": "demo"})
    second = runner._take_research_findings_prompt(state, {"target_symbol": "demo"})

    assert "use invariant h" in first
    assert "wrong target" not in first
    assert second == ""


def test_findings_prompt_prioritizes_exact_replacement_without_cutting_code():
    replacement = "by\n  " + "exact True.intro\n  " * 500
    prompt = research_findings.prompt_payload(
        [
            {
                "job_id": "campaign.ds-checked",
                "archetype": "deep_search",
                "objective": "Find a checked proof of demo",
                "deliverable": {
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
                    "large_advisory_note": "noise" * 10_000,
                },
            }
        ],
        max_chars=1000,
    )

    payload = json.loads(prompt)
    assert payload[0]["checked_replacements"][0]["replacement"] == replacement
    assert payload[0]["prompt_cap_exceeded_for_exact_replacements"] is True
    assert "parent must rerun lean_incremental_check" in payload[0]["checked_replacement_policy"]


def test_subsumed_finding_is_delivered_as_evidence_not_an_actionable_candidate():
    """Keep route exclusions while suppressing candidates already judged ineligible."""
    replacement = "by\n  exact previously_known_partial_branch"
    stale_route = "by_cases h29 : q % 29 = 28"
    prompt = research_findings.prompt_payload(
        [
            {
                "job_id": "campaign.em-subsumed",
                "objective": f"Implement the stale route now: {stale_route}",
                "plan_delta": [{"next_action": "insert h29"}],
                "semantic_novelty": {
                    "classification": "subsumed",
                    "progress_anchor_eligible": False,
                    "progress_anchor_reason": "subsumed_mathematical_semantics",
                },
                "deliverable": {
                    "status": "new checked helper route; no proof-completion claim",
                    "issues": ["The helper leaves the universal complement unresolved."],
                    "unresolved_dependency": "A genuinely exhaustive construction is still needed.",
                    "new_proof_shape": stale_route,
                    "checked_proof_delta": {"candidate": "insert h29"},
                    "earliest_unresolved_graph_dependency": {
                        "target_delta": "add a modulus-29 helper",
                        "name": "stale_modulus_twenty_nine_helper",
                        "statement": "For q % 29 = 28, prove the partial branch.",
                    },
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
                },
            }
        ]
    )

    payload = json.loads(prompt)
    finding = payload[0]
    assert finding["foreground_use_role"] == "evidence_only"
    assert "do not implement" in finding["foreground_use_policy"].lower()
    assert finding["checked_replacements"] == []
    assert finding["suppressed_candidate_count"] == 1
    assert len(finding["suppressed_deliverable_sha256"]) == 64
    assert len(finding["suppressed_objective_sha256"]) == 64
    assert replacement not in prompt
    assert stale_route not in prompt
    assert "insert h29" not in prompt
    assert "modulus-29 helper" not in prompt
    assert "stale_modulus_twenty_nine_helper" not in prompt
    assert "prove the partial branch" not in prompt
    assert "universal complement unresolved" in prompt
    assert "genuinely exhaustive construction" in prompt


def test_evidence_only_finding_retains_explicit_dead_end_route_and_reason():
    """Carry safe route exclusions forward without reviving embedded proof code."""
    prompt = research_findings.prompt_payload(
        [
            {
                "job_id": "campaign.ds-dead-end",
                "semantic_novelty": {
                    "classification": "subsumed",
                    "progress_anchor_eligible": False,
                },
                "deliverable": {
                    "dead_ends": [
                        {
                            "route": "fixed coloring (fun _ => True)",
                            "reason": (
                                "The safe branch depends on the current triangle and move, so "
                                "one constant coloring cannot select it parametrically."
                            ),
                        },
                        {
                            "route": "by_cases h29 : q % 29 = 28",
                            "reason": "This finite residue split leaves the universal tail open.",
                        },
                    ]
                },
            }
        ]
    )

    assert "fixed coloring (fun _ => True)" in prompt
    assert "one constant coloring cannot select it parametrically" in prompt
    assert "finite residue split leaves the universal tail open" in prompt
    assert "by_cases h29" not in prompt


def test_research_handoff_persists_dead_ends_for_exact_assignment(monkeypatch):
    """Make delivered research exclusions survive later context compression."""
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    active_file = "/tmp/P4.lean"
    finding = {
        "job_id": "campaign.ds-dead-end",
        "target_symbol": "result",
        "active_file": active_file,
        "consumed_at": "2026-08-05T12:00:00+00:00",
        "deliverable": {
            "dead_ends": [
                {
                    "route": "constant coloring",
                    "reason": "The branch choice must depend on the live move.",
                }
            ]
        },
    }
    monkeypatch.setattr(
        runner,
        "_migrate_research_findings_for_assignment",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        runner.plan_state,
        "load_summary",
        lambda: {"research_findings": [finding]},
    )
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    recorded = []

    def record_checkpoint_advisory(**kwargs):
        recorded.append(kwargs)
        return True

    monkeypatch.setattr(
        runner.plan_state,
        "record_checkpoint_advisory",
        record_checkpoint_advisory,
    )
    state = {
        "campaign_id": "campaign",
        "current_queue_assignment": {
            "target_symbol": "result",
            "active_file": active_file,
        },
    }

    prompt = runner._take_research_findings_prompt(state, None)

    assert "constant coloring" in prompt
    assert len(recorded) == 1
    assert recorded[0]["target_symbol"] == "result"
    assert recorded[0]["active_file"] == active_file
    assert recorded[0]["created_at"] == "2026-08-05T12:00:00+00:00"
    assert recorded[0]["negative_evidence"] == [
        "constant coloring: The branch choice must depend on the live move."
    ]


def test_research_handoff_backfills_dead_ends_from_delivered_findings(monkeypatch):
    """Repair durable route memory when resuming a campaign from an older build."""
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    active_file = "/tmp/P4.lean"
    finding = {
        "job_id": "campaign.ds-already-delivered",
        "target_symbol": "result",
        "active_file": active_file,
        "deliverable": {
            "dead_ends": [
                {
                    "route": "fixed initial state",
                    "reason": "The invariant requires a state-dependent witness.",
                }
            ]
        },
    }
    summary = {"research_findings": [finding]}
    monkeypatch.setattr(
        runner,
        "_migrate_research_findings_for_assignment",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(runner.plan_state, "load_summary", lambda: summary)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    recorded = []
    monkeypatch.setattr(
        runner.plan_state,
        "record_checkpoint_advisory",
        lambda **kwargs: recorded.append(kwargs) or True,
    )
    marker = research_findings.delivery_key(finding["job_id"], "result")
    state = {
        "campaign_id": "campaign",
        "research_findings_delivered": [marker],
        "current_queue_assignment": {
            "target_symbol": "result",
            "active_file": active_file,
        },
    }

    prompt = runner._take_research_findings_prompt(state, None)

    assert prompt == ""
    assert [record["negative_evidence"] for record in recorded] == [
        ["fixed initial state: The invariant requires a state-dependent witness."]
    ]


def _partial_congruence_finding(*, failed_shape_count: int = 2, checked: bool = False):
    """Return an em-329-shaped finding with an explicitly incomplete residue leaf."""
    return {
        "job_id": "campaign.em-partial-congruence",
        "target_symbol": "demo",
        "semantic_novelty": {
            "classification": "novel",
            "fingerprints": ["congruence:t%23=19", "proof-shape:partial23"],
            "has_checked_helper": checked,
            "progress_anchor_eligible": True,
            "progress_anchor_reason": "novel_mathematical_semantics",
        },
        "deliverable": {
            "checked_proof_delta": {
                "helper_name": "demo_of_mod_twenty_three_eq_nineteen",
                "statement": "t % 23 = 19 -> Demo t",
            },
            "limitations": (
                "This proves only the new mod-23 residue branch; it does not establish "
                "coverage of the remaining terminal branch and is not a proof completion claim."
            ),
            "new_proof_shape": "by_cases h23 : t % 23 = 19",
            research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY: {
                "recent_failed_proof_shapes": [
                    {
                        "attempt": index + 1,
                        "proof_shape": f"prior residue leaf {index + 1}",
                        "reason": "assigned declaration still has sorry",
                    }
                    for index in range(failed_shape_count)
                ]
            },
        },
    }


def _canonical_checked_helper_finding(*, declaration: str | None = None):
    """Return a parent-captured checked helper with an incomplete final report."""
    exact_declaration = declaration or (
        "private lemma demo_of_mod_eighty_seven_eq_twenty "
        "(t : Nat) (h : t % 87 = 20) : True := by\n  trivial"
    )
    return {
        "job_id": "campaign.orchestrator.em-438",
        "objective": "Check one distinct factor-pair helper.",
        "target_symbol": "demo",
        "active_file": "Main.lean",
        "semantic_novelty": {
            "classification": "novel",
            "fingerprints": ["helper-statement:mod87", "proof-shape:factor-pair-mod87"],
            "has_checked_helper": True,
            "progress_anchor_eligible": True,
            "progress_anchor_reason": "new_checked_helper_semantics",
        },
        "deliverable": {
            "status": "incomplete_unverified",
            "summary": "The helper checks, but the residual target is still open.",
            "limitations": "This is a partial helper and not a target-closing proof.",
            "checked_helper_status": dispatch_service.CHECKED_HELPER_STATUS,
            "parent_recheck_required": True,
            "checked_helpers": [
                {
                    "anchor_target_symbol": "demo",
                    "active_file": "Main.lean",
                    "declaration": exact_declaration,
                    "declaration_sha256": sha256(exact_declaration.encode()).hexdigest(),
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "action": "check_helper",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "verification_scope": "helper_candidate",
                        "replacement_matches_target": False,
                        "replacement_declarations": ["demo_of_mod_eighty_seven_eq_twenty"],
                    },
                    "parent_recheck_required": True,
                }
            ],
            research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY: {
                "recent_failed_proof_shapes": [
                    {
                        "attempt": index + 1,
                        "proof_shape": f"prior residue leaf {index + 1}",
                        "reason": "assigned declaration still has sorry",
                    }
                    for index in range(4)
                ]
            },
        },
    }


def _status_only_partial_finding():
    """Return the em-366 shape that exposed partial candidate code to prompts."""
    return {
        "job_id": "campaign.orchestrator.em-366",
        "objective": "Integrate the em-366 residue route into the target.",
        "target_symbol": "demo",
        "active_file": "Main.lean",
        "semantic_novelty": {
            "classification": "novel",
            "fingerprints": ["proof-shape:em366-direct-identity"],
            "has_checked_helper": False,
            "progress_anchor_eligible": True,
            "progress_anchor_reason": "new_mathematical_semantics",
        },
        "deliverable": {
            "status": "new_checked_partial_route",
            "checked_delta": {
                "candidate_code": (
                    "private lemma em_366_residue_helper (s : Nat) "
                    "(hs : s % 11 = 8) : Demo s := by exact em_366_candidate"
                ),
                "result": "ok=true; valid_without_sorry=true",
            },
            "integration": (
                "Insert em_366_residue_helper before the target and dispatch s % 11 = 8."
            ),
            "issues": [
                "The discarded universal split relied on an invalid divisibility assumption."
            ],
            research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY: {
                "recent_failed_proof_shapes": [
                    {
                        "attempt": index + 1,
                        "proof_shape": f"prior rejected shape {index + 1}",
                        "reason": "target still has sorry",
                    }
                    for index in range(4)
                ]
            },
        },
    }


def test_explicit_partial_status_alone_suppresses_em_366_action_content():
    """Treat the worker's partial verdict as authoritative for prompt containment."""
    finding = _status_only_partial_finding()
    archived = json.dumps(finding, ensure_ascii=False, sort_keys=True)

    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]

    assert research_findings.foreground_use_role(finding) == "evidence_only"
    assert payload["foreground_use_role"] == "evidence_only"
    assert payload["foreground_use_reason"] == "partial_coverage_without_completion"
    assert "em_366_residue_helper" not in prompt
    assert "em_366_candidate" not in prompt
    assert "s % 11 = 8" not in prompt
    assert "Integrate the em-366 residue route" not in prompt
    assert "invalid divisibility assumption" in prompt
    assert json.dumps(finding, ensure_ascii=False, sort_keys=True) == archived


@pytest.mark.parametrize(
    "status",
    [
        "research_only_not_completion",
        "research_complete_not_proof_complete",
        "delta_checked_not_proof_complete",
        "researched_not_completed",
    ],
)
def test_nested_noncompletion_status_variants_are_evidence_only(status):
    """Recognize noncompletion verdicts serialized inside a findings report."""
    candidate = "private lemma ds_365_mod_forty_one := by exact ds_365_candidate"
    finding = _status_only_partial_finding()
    finding["deliverable"] = {
        "summary": json.dumps(
            {
                "findings_report": {
                    "status": status,
                    "helper_candidate": candidate,
                    "integration_delta": "Dispatch s % 41 = 37 in the target.",
                }
            }
        )
    }

    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]

    assert payload["foreground_use_role"] == "evidence_only"
    assert payload["foreground_use_reason"] == "partial_coverage_without_completion"
    assert candidate not in prompt
    assert "s % 41 = 37" not in prompt


def test_novel_partial_congruence_after_repeated_failures_is_evidence_only():
    """Do not let a fresh residue number masquerade as a new completion route."""
    prompt = research_findings.prompt_payload([_partial_congruence_finding()])

    payload = json.loads(prompt)
    finding = payload[0]
    assert finding["foreground_use_role"] == "evidence_only"
    assert finding["foreground_use_reason"] == "partial_coverage_without_completion"
    assert "demo_of_mod_twenty_three_eq_nineteen" not in prompt
    assert "t % 23 = 19" not in prompt
    assert "remaining terminal branch" in prompt


def test_novel_parent_captured_checked_helper_is_actionable_despite_partial_status():
    """Deliver em-438's exact helper for parent recheck without claiming closure."""
    finding = _canonical_checked_helper_finding()
    declaration = finding["deliverable"]["checked_helpers"][0]["declaration"]

    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]

    assert research_findings.foreground_use_role(finding) == "actionable"
    assert payload["foreground_use_role"] == "actionable"
    assert payload["checked_helpers"][0]["declaration"] == declaration
    assert "PARTIAL HELPER, NOT TARGET CLOSURE" in payload["checked_helper_policy"]
    assert "action=check_helper" in payload["checked_helper_policy"]
    assert "continue proving the unresolved target" in payload["checked_helper_policy"]
    assert payload["deliverable"]["status"] == "incomplete_unverified"


def test_mechanism_repeat_checked_helper_remains_evidence_only():
    """Novelty ineligibility still suppresses exact repeated helper code."""
    finding = _canonical_checked_helper_finding()
    declaration = finding["deliverable"]["checked_helpers"][0]["declaration"]
    finding["semantic_novelty"].update(
        {
            "classification": "mechanism_repeat",
            "progress_anchor_eligible": False,
            "progress_anchor_reason": "checked_helper_mechanism_already_known",
        }
    )

    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]

    assert research_findings.foreground_use_role(finding) == "evidence_only"
    assert payload["foreground_use_role"] == "evidence_only"
    assert payload["foreground_use_reason"] == "checked_helper_mechanism_already_known"
    assert payload["checked_helpers"] == []
    assert payload["suppressed_candidate_count"] == 1
    assert declaration not in prompt
    assert "not a target-closing proof" in prompt


def test_unhashed_checked_helper_key_cannot_bypass_partial_quarantine():
    """The reserved key alone is not enough to acquire foreground authority."""
    finding = _canonical_checked_helper_finding()
    declaration = finding["deliverable"]["checked_helpers"][0]["declaration"]
    finding["deliverable"]["checked_helpers"][0]["declaration_sha256"] = "spoofed"

    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]

    assert research_findings.foreground_use_role(finding) == "evidence_only"
    assert payload["foreground_use_role"] == "evidence_only"
    assert payload["foreground_use_reason"] == "partial_coverage_without_completion"
    assert declaration not in prompt


def test_checked_helper_exact_source_exceeds_prompt_cap_without_truncation():
    """Treat helper declarations as exact code, not a generic prose summary."""
    declaration = "private lemma huge_helper : True := by\n" + "  exact True.intro\n" * 400
    finding = _canonical_checked_helper_finding(declaration=declaration)

    prompt = research_findings.prompt_payload([finding], max_chars=1000)
    payload = json.loads(prompt)[0]

    assert payload["checked_helpers"][0]["declaration"] == declaration
    assert payload["prompt_cap_exceeded_for_exact_checked_helpers"] is True


def test_checked_helper_travels_alone_in_foreground_batch():
    """Do not crowd exact helper code with neighboring generic findings."""
    finding = _canonical_checked_helper_finding()
    later = {
        "job_id": "campaign.ds-later",
        "target_symbol": "demo",
        "deliverable": {"summary": "later generic route"},
    }

    batch, rendered = research_findings.foreground_delivery_batch(
        [finding, later],
        target_symbol="demo",
    )

    assert batch == (finding,)
    assert "demo_of_mod_eighty_seven_eq_twenty" in rendered
    assert "later generic route" not in rendered


def test_checked_helper_overtakes_earlier_generic_finding_in_foreground_batch():
    """Live em-702/em-709 regression: checked work cannot hide behind FIFO."""
    earlier = {
        "job_id": "campaign.orchestrator.em-702",
        "target_symbol": "demo",
        "active_file": "Main.lean",
        "deliverable": {"summary": "earlier generic denominator experiment"},
    }
    checked = _canonical_checked_helper_finding(
        declaration="private lemma denominator_scale_certificate : True := by\n  trivial"
    )
    checked["job_id"] = "campaign.orchestrator.em-709"
    checked["deliverable"]["checked_helpers"][0]["worker_check"]["replacement_declarations"] = [
        "denominator_scale_certificate"
    ]

    batch, rendered = research_findings.foreground_delivery_batch(
        [earlier, checked],
        target_symbol="demo",
    )

    assert batch == (checked,)
    assert "campaign.orchestrator.em-709" in rendered
    assert "denominator_scale_certificate" in rendered
    assert "earlier generic denominator experiment" not in rendered


@pytest.mark.parametrize(
    ("fingerprints", "partial_fields"),
    [
        (
            ["proof-shape:factor-pair-leaf"],
            {
                "route": {"branch_hypothesis": "t % 47 = 40"},
                "evidence": {
                    "scope_limit": (
                        "This helper covers only the infinite class t ≡ 40 (mod 47); "
                        "inserting a by_cases branch advances the sieve but does not close "
                        "the residual complement."
                    )
                },
            },
        ),
        (
            ["congruence:t%43=11", "proof-shape:factor-leaf"],
            {
                "integration_note": (
                    "Add by_cases h43 : t % 43 = 11. This does not claim that the "
                    "remaining dispatch is exhaustive."
                ),
                "issues": ["The global complement/coverage implication remains unresolved."],
            },
        ),
    ],
)
def test_partial_finite_leaf_wording_variants_are_evidence_only(fingerprints, partial_fields):
    """Cover the deep-search and empirical phrasings observed in the live campaign."""
    finding = _partial_congruence_finding()
    finding["semantic_novelty"]["fingerprints"] = fingerprints
    parent_context = finding["deliverable"][
        research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY
    ]
    finding["deliverable"] = {
        **partial_fields,
        research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY: parent_context,
    }

    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]
    assert payload["foreground_use_role"] == "evidence_only"
    assert payload["foreground_use_reason"] == "partial_coverage_without_completion"
    assert "by_cases" not in prompt
    assert "t % 43 = 11" not in prompt
    assert "t % 47 = 40" not in prompt


def test_checked_partial_route_status_without_congruence_fingerprint_is_evidence_only():
    """Recognize the live empirical-worker wording for one checked residue leaf."""
    finding = _partial_congruence_finding()
    finding["semantic_novelty"]["fingerprints"] = [
        "obstruction:live-mod-thirty-seven",
        "proof-shape:nonresidual-factor-leaf",
    ]
    parent_context = finding["deliverable"][
        research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY
    ]
    finding["deliverable"] = {
        "status": "partial_new_route_checked",
        "new_route": {
            "branch": "s % 37 = 11",
            "construction": "Factor the denominator by 37 and apply the helper.",
        },
        "issues": [
            "No completion claimed: the checked branch adds coverage but does not solve "
            "the final sieve-complement dependency."
        ],
        research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY: parent_context,
    }

    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]
    assert payload["foreground_use_role"] == "evidence_only"
    assert payload["foreground_use_reason"] == "partial_coverage_without_completion"
    assert "s % 37 = 11" not in prompt
    assert "Factor the denominator" not in prompt
    assert "final sieve-complement dependency" in prompt


def test_evidence_only_counterexample_wrapper_cannot_leak_a_new_modulus_test():
    """Sanitize action text nested below an otherwise safe negative-evidence key."""
    finding = _partial_congruence_finding()
    finding["semantic_novelty"].update(
        {
            "classification": "subsumed",
            "progress_anchor_eligible": False,
            "progress_anchor_reason": "subsumed_mathematical_semantics",
        }
    )
    finding["deliverable"]["counterexample_evidence"] = {
        "avoids_existing_tests": {"s_mod_11": [4, "not 0 or 2"]},
        "new_test": "4 % 59 = 4",
        "s": 4,
        "t": 20,
    }

    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]
    evidence = payload["deliverable"]["negative_evidence"]["counterexample_evidence"]
    assert payload["foreground_use_role"] == "evidence_only"
    assert "4 % 59 = 4" not in prompt
    assert "not 0 or 2" in prompt
    assert evidence["s"] == 4
    assert evidence["t"] == 20


def test_first_partial_congruence_can_still_define_an_early_decomposition():
    """Permit an initial bounded split before repeated failures demand diversity."""
    finding = _partial_congruence_finding(failed_shape_count=1)

    assert research_findings.foreground_use_role(finding) == "actionable"


def test_checked_non_target_partial_congruence_is_evidence_only_after_failures():
    """A checked helper alone cannot masquerade as target-closing progress."""
    finding = _partial_congruence_finding(checked=True)
    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]

    assert research_findings.foreground_use_role(finding) == "evidence_only"
    assert payload["foreground_use_role"] == "evidence_only"
    assert "demo_of_mod_twenty_three_eq_nineteen" not in prompt
    assert "t % 23 = 19" not in prompt


def test_formally_checked_helper_replacement_cannot_exempt_partial_coverage():
    """Match em-291: checked helper code is not a checked target replacement."""
    finding = _partial_congruence_finding(checked=True)
    finding["deliverable"].update(
        {
            "status": "partial_new_route_checked",
            "checked_replacements": [
                {
                    "target_symbol": "demo_of_mod_twenty_three_eq_nineteen",
                    "replacement": "by\n  exact hidden_partial_candidate",
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "replacement_matches_target": False,
                    },
                }
            ],
            "issues": [
                "The helper elaborates, but target integration and residual coverage remain open."
            ],
        }
    )

    normalized = dispatch_service.enforce_checked_replacement_contract(
        finding["deliverable"],
        expected_target_symbol="demo",
    )
    prompt = research_findings.prompt_payload([finding])
    payload = json.loads(prompt)[0]

    assert normalized["checked_replacements"] == []
    assert (
        normalized["unchecked_replacements"][0]["worker_check"]["replacement_matches_target"]
        is False
    )
    assert research_findings.foreground_use_role(finding) == "evidence_only"
    assert payload["foreground_use_role"] == "evidence_only"
    assert payload["foreground_use_reason"] == "partial_coverage_without_completion"
    assert "hidden_partial_candidate" not in prompt
    assert "demo_of_mod_twenty_three_eq_nineteen" not in prompt
    assert "t % 23 = 19" not in prompt


def test_checked_target_closing_congruence_remains_actionable_after_failures():
    """Never quarantine an exact contract-valid target completion candidate."""
    finding = _partial_congruence_finding(checked=True)
    finding["deliverable"]["checked_replacements"] = [
        {
            "target_symbol": "demo",
            "replacement": "by\n  exact demo_complete",
            "worker_check": {
                "tool": "lean_incremental_check",
                "valid_without_sorry": True,
                "has_errors": False,
                "has_sorry": False,
                "replacement_matches_target": True,
            },
        }
    ]

    assert research_findings.foreground_use_role(finding) == "actionable"


def test_partial_congruence_cannot_seed_recursive_portfolio_refresh():
    """Keep a non-closing residue leaf as knowledge without spawning more sieve work."""
    finding = _partial_congruence_finding()
    entry = LedgerEntry(
        spec=JobSpec(
            job_id="campaign.orchestrator.em-329",
            archetype="empirical",
            requester_role="orchestrator",
            objective="Find one new route for demo.",
            budget=JobBudget(api_steps=2, wall_clock_s=30),
            deliverable="empirical_report",
            inputs={"target_symbol": "demo", "active_file": "Main.lean"},
            parent_job_id="campaign.orchestrator",
        ),
        state="done",
        result={"status": "done", "deliverable": finding["deliverable"]},
    )

    assert (
        research_portfolio._is_progress_route_evidence(
            entry,
            semantic_entries=(entry,),
        )
        is False
    )


def test_status_only_partial_result_cannot_seed_recursive_portfolio_refresh():
    """Do not turn em-366 candidate code into an evidence-to-helper worker."""
    finding = _status_only_partial_finding()
    entry = LedgerEntry(
        spec=JobSpec(
            job_id=finding["job_id"],
            archetype="empirical",
            requester_role="orchestrator",
            objective=finding["objective"],
            budget=JobBudget(api_steps=2, wall_clock_s=30),
            deliverable="empirical_report",
            inputs={"target_symbol": "demo", "active_file": "Main.lean"},
            parent_job_id="campaign.orchestrator",
        ),
        state="done",
        result={"status": "done", "deliverable": finding["deliverable"]},
    )

    assert not research_portfolio._is_progress_route_evidence(
        entry,
        semantic_entries=(entry,),
    )


def test_research_handoff_forbids_acting_on_evidence_only_findings(monkeypatch):
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    active_file = "/tmp/Erdos242.lean"
    summary = {
        "research_findings": [
            {
                "job_id": "campaign.em-subsumed",
                "target_symbol": "erdos_242",
                "active_file": active_file,
                "semantic_novelty": {
                    "classification": "subsumed",
                    "progress_anchor_eligible": False,
                },
                "deliverable": {"noncoverage_summary": "finite branch leaves a surviving input"},
            }
        ]
    }
    monkeypatch.setattr(runner.plan_state, "load_summary", lambda: summary)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    state = {
        "current_queue_assignment": {
            "target_symbol": "erdos_242",
            "active_file": active_file,
        }
    }

    prompt = runner._take_research_findings_prompt(state, None)

    assert "finite branch leaves a surviving input" in prompt
    assert "EVIDENCE_ONLY" in prompt
    assert "must not be implemented or retried" in prompt


def test_research_handoff_suppresses_status_only_partial_candidate(monkeypatch, tmp_path):
    """Apply the same containment policy to the foreground prover handoff."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    finding = _status_only_partial_finding()
    active_file = str(tmp_path / "Main.lean")
    finding["active_file"] = active_file
    monkeypatch.setattr(
        runner,
        "_migrate_research_findings_for_assignment",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        runner.plan_state,
        "load_summary",
        lambda: {"research_findings": [finding]},
    )
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": active_file,
        }
    }

    prompt = runner._take_research_findings_prompt(state, None)

    assert "EVIDENCE_ONLY" in prompt
    assert "em_366_residue_helper" not in prompt
    assert "em_366_candidate" not in prompt
    assert "s % 11 = 8" not in prompt
    assert "Integrate the em-366 residue route" not in prompt


def test_evidence_only_truncation_retains_obstruction_not_candidate_code():
    stale_route = "by_cases h29 : q % 29 = 28"
    prompt = research_findings.prompt_payload(
        [
            {
                "job_id": "campaign.em-bounded",
                "objective": stale_route + " route context " * 500,
                "semantic_novelty": {
                    "classification": "subsumed",
                    "progress_anchor_eligible": False,
                },
                "deliverable": {
                    "checked_proof_delta": {"candidate": stale_route + "\n" + "code " * 500},
                    "issues": ["finite dispatch is non-exhaustive"],
                    "unresolved_dependency": "universal residual-prime certificate",
                },
            }
        ],
        max_chars=1000,
    )

    assert stale_route not in prompt
    assert "finite dispatch is non-exhaustive" in prompt
    assert "universal residual-prime certificate" in prompt
    assert "EVIDENCE_ONLY" in prompt


def test_ineligible_findings_cannot_raise_queue_research_priority():
    ineligible = {
        "target_symbol": "demo_target",
        "semantic_novelty": {
            "classification": "subsumed",
            "progress_anchor_eligible": False,
        },
        "deliverable": {"verified_helper": "demo_target"},
    }
    legacy_actionable = {
        "target_symbol": "demo_target",
        "deliverable": {"verified_helper": "demo_target"},
    }

    assert (
        research_finding_priority._finding_priority(
            ineligible,
            target_symbol="demo_target",
        )
        == research_finding_priority.NEUTRAL_PRIORITY
    )
    assert (
        research_finding_priority._finding_priority(
            legacy_actionable,
            target_symbol="demo_target",
        )
        == research_finding_priority.EXACT_TARGET_PRIORITY
    )


def test_foreground_batch_never_acknowledges_evidence_omitted_by_exact_code(monkeypatch):
    """A large checked candidate cannot crowd a neighboring finding out."""
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    active_file = "/tmp/Erdos242.lean"
    replacement = "by\n  " + "exact True.intro\n  " * 1800
    summary = {
        "research_findings": [
            {
                "job_id": "campaign.ds-checked",
                "archetype": "deep_search",
                "target_symbol": "erdos_242",
                "active_file": active_file,
                "deliverable": {
                    "status": "candidate_verified",
                    "checked_replacements": [
                        {
                            "target_symbol": "erdos_242",
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
                },
            },
            {
                "job_id": "campaign.em-next",
                "archetype": "empirical",
                "target_symbol": "erdos_242",
                "active_file": active_file,
                "deliverable": {"summary": "new residue-class obstruction"},
            },
        ]
    }
    monkeypatch.setattr(runner.plan_state, "load_summary", lambda: summary)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    state = {
        "current_queue_assignment": {
            "target_symbol": "erdos_242",
            "active_file": active_file,
        }
    }

    checked_prompt = runner._take_research_findings_prompt(state, None)

    assert checked_prompt.count("exact True.intro") == 1800
    assert "new residue-class obstruction" not in checked_prompt
    assert research_findings.pending_foreground_markers(
        state,
        target_symbol="erdos_242",
    ) == {research_findings.delivery_key("campaign.ds-checked", "erdos_242")}
    assert research_findings.pending_checked_target_replacement(
        state,
        summary,
        target_symbol="erdos_242",
        active_file=active_file,
        blueprint=None,
    )

    research_findings.acknowledge_foreground_deliveries(
        state,
        [
            {"role": "user", "content": checked_prompt},
            {"role": "assistant", "content": "Rechecking the exact candidate."},
        ],
    )
    assert not research_findings.pending_checked_target_replacement(
        state,
        summary,
        target_symbol="erdos_242",
        active_file=active_file,
        blueprint=None,
    )
    empirical_prompt = runner._take_research_findings_prompt(state, None)
    assert "new residue-class obstruction" in empirical_prompt


def test_oversized_exact_finding_stays_durable_without_starving_bounded_evidence(monkeypatch):
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    active_file = "/tmp/Erdos242.lean"
    oversized = "by\n  " + "exact True.intro\n  " * 4000
    summary = {
        "research_findings": [
            {
                "job_id": "campaign.ds-oversized",
                "target_symbol": "erdos_242",
                "active_file": active_file,
                "deliverable": {
                    "status": "candidate_verified",
                    "checked_replacements": [
                        {
                            "target_symbol": "erdos_242",
                            "replacement": oversized,
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
            },
            {
                "job_id": "campaign.em-bounded",
                "target_symbol": "erdos_242",
                "active_file": active_file,
                "deliverable": {"summary": "bounded evidence still arrives"},
            },
        ]
    }
    monkeypatch.setattr(runner.plan_state, "load_summary", lambda: summary)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    state = {
        "current_queue_assignment": {
            "target_symbol": "erdos_242",
            "active_file": active_file,
        }
    }

    prompt = runner._take_research_findings_prompt(state, None)

    assert "bounded evidence still arrives" in prompt
    pending = research_findings.pending_foreground_markers(
        state,
        target_symbol="erdos_242",
    )
    assert pending == {research_findings.delivery_key("campaign.em-bounded", "erdos_242")}
    assert research_findings.delivery_key("campaign.ds-oversized", "erdos_242") not in pending

    research_findings.acknowledge_foreground_deliveries(
        state,
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "Using the bounded evidence first."},
        ],
    )
    oversized_prompt = runner._take_research_findings_prompt(state, None)
    assert "leanflow-research-finding-chunks-v1" in oversized_prompt
    assert len(oversized_prompt) <= research_findings.FOREGROUND_PROMPT_HARD_CAP


def test_oversized_finding_reassembles_exactly_and_resumes_after_restart(
    monkeypatch,
    tmp_path,
):
    """Persist prefix receipts and mark delivery only after the final chunk."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    marker = research_findings.delivery_key("campaign.ds-huge", "erdos_242")
    full_prompt = "BEGIN\n" + ('by\n  exact \\"quoted\\" \\\\ path -- λ\n' * 7000) + "END\n"
    state = {"campaign_id": "campaign-chunks"}
    segments: list[str] = []

    first = research_findings.stage_foreground_delivery(
        state,
        target_symbol="erdos_242",
        markers=[marker],
        prompt=full_prompt,
    )
    first_body = json.loads(first.split("\n", 1)[1])
    transfer_id = first_body["transfer_id"]
    stale_transfer = dict(state[research_findings.CHUNK_TRANSFERS_KEY][transfer_id])
    assert len(first) <= research_findings.FOREGROUND_PROMPT_HARD_CAP
    assert first_body["chunk_index"] == 0
    assert first_body["chunk_count"] > 1
    segments.append(first_body["segment"])
    assert (
        research_findings.acknowledge_foreground_deliveries(
            state,
            [{"role": "user", "content": first}],
        )
        == ()
    )
    assert (
        research_findings.stage_foreground_delivery(
            state,
            target_symbol="erdos_242",
            markers=[marker],
            prompt=full_prompt,
        )
        == ""
    )
    assert (
        research_findings.acknowledge_foreground_deliveries(
            state,
            [
                {"role": "user", "content": first},
                {"role": "assistant", "content": "Retained chunk zero exactly."},
            ],
        )
        == ()
    )
    assert "research_findings_delivered" not in state

    restarted = {"campaign_id": "campaign-chunks"}
    assert research_findings.hydrate_delivery_markers(restarted)
    next_index = 1
    while True:
        chunk = research_findings.stage_foreground_delivery(
            restarted,
            target_symbol="erdos_242",
            markers=[marker],
            prompt=full_prompt,
        )
        body = json.loads(chunk.split("\n", 1)[1])
        assert len(chunk) <= research_findings.FOREGROUND_PROMPT_HARD_CAP
        assert body["chunk_index"] == next_index
        assert body["payload_sha256"] == sha256(full_prompt.encode("utf-8")).hexdigest()
        assert body["segment_sha256"] == sha256(body["segment"].encode("utf-8")).hexdigest()
        segments.append(body["segment"])
        acknowledged = research_findings.acknowledge_foreground_deliveries(
            restarted,
            [
                {"role": "user", "content": chunk},
                {"role": "assistant", "content": f"Retained chunk {next_index}."},
            ],
        )
        next_index += 1
        if next_index == body["chunk_count"]:
            assert acknowledged == (marker,)
            break
        assert acknowledged == ()
        assert marker not in set(restarted.get("research_findings_delivered") or [])

    assert "".join(segments) == full_prompt
    assert restarted["research_findings_delivered"] == [marker]

    receipt = workflow_json_io.read_json_file(
        research_findings._delivery_receipts_path("campaign-chunks")
    )
    assert marker in receipt[research_findings.DELIVERY_RECEIPT_ARCHIVE_KEY]

    # Reproduce a stale shared-summary writer after final-chunk completion.
    # The isolated receipt must restore the pair and remove the obsolete
    # prefix receipt instead of restarting or accumulating the transfer.
    summary = workflow_json_io.read_json_file(research_findings._summary_path())
    summary[research_findings.DELIVERY_STATE_KEY] = {
        "campaign_id": "campaign-chunks",
        research_findings.CHUNK_TRANSFERS_KEY: {transfer_id: stale_transfer},
    }
    workflow_json_io.write_json_file(research_findings._summary_path(), summary)
    final_restart = {"campaign_id": "campaign-chunks"}

    assert research_findings.hydrate_delivery_markers(final_restart)
    assert final_restart["research_findings_delivered"] == [marker]
    assert research_findings.CHUNK_TRANSFERS_KEY not in final_restart
    repaired = workflow_json_io.read_json_file(research_findings._summary_path())
    repaired_delivery = repaired[research_findings.DELIVERY_STATE_KEY]
    assert repaired_delivery["research_findings_delivered"] == [marker]
    assert research_findings.CHUNK_TRANSFERS_KEY not in repaired_delivery


def test_delivery_marker_cap_retains_new_ack_and_cold_archive_prevents_replay(
    monkeypatch,
    tmp_path,
):
    """Treat bounded marker lists as hot caches, never the lossless authority."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(research_findings, "DELIVERY_MARKER_CAP", 1)
    campaign_id = "campaign-marker-cap"
    target_symbol = "demo"
    active_file = str(tmp_path / "Main.lean")
    old_entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="zz-old",
        target_symbol=target_symbol,
        active_file=active_file,
        summary="old delivered evidence",
    )
    new_entry = _consumed_research_entry(
        campaign_id=campaign_id,
        suffix="aa-new",
        target_symbol=target_symbol,
        active_file=active_file,
        summary="new delivered evidence",
    )
    old_marker = research_findings.delivery_key(old_entry.spec.job_id, target_symbol)
    new_marker = research_findings.delivery_key(new_entry.spec.job_id, target_symbol)
    assert old_marker > new_marker
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "dispatch_ledger": [old_entry.to_mapping(), new_entry.to_mapping()],
            "research_findings": [],
            research_findings.DELIVERY_STATE_KEY: {"campaign_id": campaign_id},
        },
    )
    state = {"campaign_id": campaign_id}

    research_findings.persist_delivery_markers(
        state,
        key="research_findings_delivered",
        markers=[old_marker],
    )
    research_findings.persist_delivery_markers(
        state,
        key="research_findings_delivered",
        markers=[new_marker],
    )

    assert state["research_findings_delivered"] == [new_marker]
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    assert persisted[research_findings.DELIVERY_STATE_KEY]["research_findings_delivered"] == [
        new_marker
    ]
    receipt = workflow_json_io.read_json_file(
        research_findings._delivery_receipts_path(campaign_id)
    )
    assert receipt["research_findings_delivered"] == [new_marker]
    assert receipt[research_findings.DELIVERY_RECEIPT_ARCHIVE_KEY] == [
        old_marker,
        new_marker,
    ]
    assert research_findings.durable_delivery_markers(persisted) == {
        old_marker,
        new_marker,
    }

    restarted = {"campaign_id": campaign_id}
    assert research_findings.hydrate_delivery_markers(restarted)
    assert restarted["research_findings_delivered"] == [new_marker]
    report = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    assert report["materialized"] == 0
    assert report["already_delivered"] == 2
    assert (
        research_findings.delivery_backlog_count(
            workflow_json_io.read_json_file(research_findings._summary_path()),
            target_symbol=target_symbol,
            active_file=active_file,
        )
        == 0
    )


def test_first_v2_ack_archives_same_campaign_summary_hot_before_eviction(
    monkeypatch,
    tmp_path,
):
    """Characterize the live upgrade from summary-only receipts at the hot cap."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(research_findings, "DELIVERY_MARKER_CAP", 1)
    campaign_id = "campaign-summary-upgrade"
    old_marker = research_findings.delivery_key("campaign.ds-zz-old", "demo")
    new_marker = research_findings.delivery_key("campaign.ds-aa-new", "demo")
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "campaign": {"campaign_id": campaign_id, "status": "running"},
            "research_findings": [],
            research_findings.DELIVERY_STATE_KEY: {
                "campaign_id": campaign_id,
                "research_findings_delivered": [old_marker],
            },
        },
    )
    state = {"campaign_id": campaign_id}

    research_findings.persist_delivery_markers(
        state,
        key="research_findings_delivered",
        markers=[new_marker],
    )

    receipt = workflow_json_io.read_json_file(
        research_findings._delivery_receipts_path(campaign_id)
    )
    assert receipt["research_findings_delivered"] == [new_marker]
    assert receipt[research_findings.DELIVERY_RECEIPT_ARCHIVE_KEY] == [
        old_marker,
        new_marker,
    ]
    summary = workflow_json_io.read_json_file(research_findings._summary_path())
    assert summary[research_findings.DELIVERY_STATE_KEY]["research_findings_delivered"] == [
        new_marker
    ]
    assert research_findings.durable_delivery_markers(summary) == {
        old_marker,
        new_marker,
    }


def test_hydration_migrates_same_campaign_summary_receipts_into_cold_archive(
    monkeypatch,
    tmp_path,
):
    """Backfill a partial v2 sidecar from the larger same-campaign summary window."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_id = "campaign-hydration-upgrade"
    markers = [
        research_findings.delivery_key(f"campaign.ds-{index:03d}", "demo") for index in range(3)
    ]
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "campaign": {"campaign_id": campaign_id, "status": "running"},
            "research_findings": [],
            research_findings.DELIVERY_STATE_KEY: {
                "campaign_id": campaign_id,
                "research_findings_delivered": markers,
            },
        },
    )
    research_findings._persist_delivery_receipts(
        campaign_id=campaign_id,
        markers=[markers[-1]],
    )
    state = {"campaign_id": campaign_id}

    assert research_findings.hydrate_delivery_markers(state)

    receipt = workflow_json_io.read_json_file(
        research_findings._delivery_receipts_path(campaign_id)
    )
    assert receipt["research_findings_delivered"] == markers
    assert receipt[research_findings.DELIVERY_RECEIPT_ARCHIVE_KEY] == markers
    assert state["research_findings_delivered"] == markers


def test_overlapping_campaign_receipts_cannot_clear_or_replace_durable_owner(
    monkeypatch,
    tmp_path,
):
    """Isolate receipt files and reject a stale campaign's summary mirror write."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    campaign_a = "campaign-overlap-a"
    campaign_b = "campaign-overlap-b"
    marker_a = research_findings.delivery_key("campaign-a.ds-001", "demo")
    marker_b = research_findings.delivery_key("campaign-b.ds-001", "demo")
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "campaign": {"campaign_id": campaign_a, "status": "running"},
            "research_findings": [],
        },
    )

    state_a = {"campaign_id": campaign_a}
    research_findings.persist_delivery_markers(
        state_a,
        key="research_findings_delivered",
        markers=[marker_a],
    )
    owned_summary = workflow_json_io.read_json_file(research_findings._summary_path())
    state_b = {"campaign_id": campaign_b}
    research_findings.persist_delivery_markers(
        state_b,
        key="research_findings_delivered",
        markers=[marker_b],
    )

    assert research_findings._delivery_receipts_path(
        campaign_a
    ) != research_findings._delivery_receipts_path(campaign_b)
    receipt_a = workflow_json_io.read_json_file(
        research_findings._delivery_receipts_path(campaign_a)
    )
    receipt_b = workflow_json_io.read_json_file(
        research_findings._delivery_receipts_path(campaign_b)
    )
    assert receipt_a[research_findings.DELIVERY_RECEIPT_ARCHIVE_KEY] == [marker_a]
    assert receipt_b[research_findings.DELIVERY_RECEIPT_ARCHIVE_KEY] == [marker_b]
    assert workflow_json_io.read_json_file(research_findings._summary_path()) == owned_summary
    assert research_findings.durable_delivery_markers(owned_summary) == {marker_a}

    restarted_b = {"campaign_id": campaign_b}
    assert research_findings.hydrate_delivery_markers(restarted_b, owned_summary)
    assert restarted_b["research_findings_delivered"] == [marker_b]
    receipt_b_after = workflow_json_io.read_json_file(
        research_findings._delivery_receipts_path(campaign_b)
    )
    assert receipt_b_after[research_findings.DELIVERY_RECEIPT_ARCHIVE_KEY] == [marker_b]
    assert workflow_json_io.read_json_file(research_findings._summary_path()) == owned_summary


def test_legacy_receipt_upgrade_cold_archives_existing_hot_window(monkeypatch, tmp_path):
    """Move every v1 hot marker into cold storage before the first v2 eviction."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(research_findings, "DELIVERY_MARKER_CAP", 1)
    campaign_id = "campaign-legacy-receipt"
    old_marker = research_findings.delivery_key("campaign.ds-zz-old", "demo")
    new_marker = research_findings.delivery_key("campaign.ds-aa-new", "demo")
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {
            "campaign": {"campaign_id": campaign_id, "status": "running"},
            "research_findings": [],
        },
    )
    workflow_json_io.write_json_file(
        research_findings._delivery_receipts_path(),
        {
            "version": 1,
            "campaign_id": campaign_id,
            "research_findings_delivered": [old_marker],
        },
    )

    state = {"campaign_id": campaign_id}
    research_findings.persist_delivery_markers(
        state,
        key="research_findings_delivered",
        markers=[new_marker],
    )

    upgraded = workflow_json_io.read_json_file(
        research_findings._delivery_receipts_path(campaign_id)
    )
    assert upgraded["version"] == research_findings.DELIVERY_RECEIPTS_VERSION
    assert upgraded["research_findings_delivered"] == [new_marker]
    assert upgraded[research_findings.DELIVERY_RECEIPT_ARCHIVE_KEY] == [
        old_marker,
        new_marker,
    ]
    assert research_findings.durable_delivery_markers(
        workflow_json_io.read_json_file(research_findings._summary_path())
    ) == {old_marker, new_marker}


def test_in_progress_oversized_transfer_yields_to_new_later_finding():
    """A multi-turn transfer cannot monopolize every subsequent batch."""
    target = "erdos_242"
    oversized = {
        "job_id": "campaign.ds-huge",
        "target_symbol": target,
        "deliverable": {"summary": "x" * (research_findings.FOREGROUND_PAYLOAD_CAP * 2)},
    }
    later = {
        "job_id": "campaign.em-later",
        "target_symbol": target,
        "deliverable": {"summary": "new bounded obstruction"},
    }
    marker = research_findings.delivery_key("campaign.ds-huge", target)
    state: dict = {}
    batch, rendered = research_findings.foreground_delivery_batch(
        [oversized],
        autonomy_state=state,
        target_symbol=target,
    )
    assert batch == (oversized,)
    first_chunk = research_findings.stage_foreground_delivery(
        state,
        target_symbol=target,
        markers=[marker],
        prompt=rendered,
    )
    assert (
        research_findings.acknowledge_foreground_deliveries(
            state,
            [
                {"role": "user", "content": first_chunk},
                {"role": "assistant", "content": "Retained the first chunk."},
            ],
        )
        == ()
    )

    yielded_batch, yielded_rendered = research_findings.foreground_delivery_batch(
        [oversized, later],
        autonomy_state=state,
        target_symbol=target,
    )

    assert yielded_batch == (later,)
    assert "new bounded obstruction" in yielded_rendered


def test_findings_prompt_labels_legacy_verified_prose_unverified():
    prompt = research_findings.prompt_payload(
        [
            {
                "job_id": "campaign.ds-legacy",
                "deliverable": {
                    "status": "verified",
                    "summary": "verified construction, exact code omitted",
                },
            }
        ]
    )

    payload = json.loads(prompt)
    assert payload[0]["checked_replacements"] == []
    assert payload[0]["deliverable"]["status"] == "incomplete_unverified"
    assert "Do not treat this candidate as verified" in payload[0]["checked_replacement_policy"]


def test_findings_prompt_keeps_new_evidence_ahead_of_parent_route_context():
    """Novelty audit metadata remains durable without crowding out the finding."""
    parent_context = research_route_context.attach_parent_route_context(
        {},
        {
            "assignment": {"target_symbol": "demo", "active_file": "/tmp/Demo.lean"},
            "recent_failed_proof_shapes": [
                {
                    "attempt": 4,
                    "proof_shape": "old fixed witness " + "noise " * 500,
                    "reason": "kernel rejected",
                }
            ],
        },
    )[research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY]
    prompt = research_findings.prompt_payload(
        [
            {
                "job_id": "campaign.ds-context",
                "deliverable": {
                    "summary": "new factor-pair dependency isolated",
                    research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY: parent_context,
                },
            }
        ],
        max_chars=1000,
    )

    payload = json.loads(prompt)
    assert payload[0]["deliverable"]["summary"] == ("new factor-pair dependency isolated")
    assert payload[0]["parent_route_context_sha256"] == parent_context["sha256"]
    assert (
        research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY not in payload[0]["deliverable"]
    )


def test_split_descendant_inherits_ancestor_findings_but_not_unrelated_theorem(monkeypatch):
    """Keep decomposition from hiding useful research on its parent theorem."""
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    active_file = "/tmp/Erdos242.lean"
    parent_id = plan_state.node_id_for("erdos_242", active_file)
    child_id = plan_state.node_id_for("erdos_242_residual_mod_seven_eq_two", active_file)
    unrelated_id = plan_state.node_id_for("other_theorem", active_file)
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(id=parent_id, name="erdos_242", file=active_file),
            plan_state.GraphNode(
                id=child_id,
                name="erdos_242_residual_mod_seven_eq_two",
                file=active_file,
            ),
            plan_state.GraphNode(id=unrelated_id, name="other_theorem", file=active_file),
        ),
        edges=(plan_state.GraphEdge(source=child_id, target=parent_id, kind="split_of"),),
    )
    summary = {
        "dispatch_ledger": [
            {
                "spec": {
                    "job_id": "campaign.ds-parent",
                    "inputs": {"target_symbol": "erdos_242", "active_file": active_file},
                }
            },
            {
                "spec": {
                    "job_id": "campaign.ds-unrelated",
                    "inputs": {"target_symbol": "other_theorem", "active_file": active_file},
                }
            },
        ],
        "research_findings": [
            {
                "job_id": "campaign.ds-parent",
                "deliverable": {"summary": "verified construction for k % 7 = 2"},
            },
            {
                "job_id": "campaign.ds-unrelated",
                "deliverable": {"summary": "same file, wrong theorem"},
            },
        ],
    }
    target = "erdos_242_residual_mod_seven_eq_two"

    selected = research_findings.relevant_findings(
        summary,
        target_symbol=target,
        active_file=active_file,
        blueprint=blueprint,
    )

    assert [finding["job_id"] for finding in selected] == ["campaign.ds-parent"]

    ctx = orchestrator.build_route_context(
        trigger="event",
        live_state={"target_symbol": target, "active_file": active_file},
        blueprint=blueprint,
        summary=summary,
    )
    assert [finding["job_id"] for finding in ctx.research_findings] == ["campaign.ds-parent"]

    monkeypatch.setattr(runner.plan_state, "load_summary", lambda: summary)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: blueprint)
    state = {
        "current_queue_assignment": {
            "target_symbol": target,
            "active_file": active_file,
        },
        # A legacy parent-target event must not suppress the descendant event.
        "orchestrator_jobs_seen": ["campaign.ds-parent"],
    }
    assert runner._orchestrator_event_due(state, 1) == "event"
    assert runner._orchestrator_event_due(state, 1) == ""
    prompt = runner._take_research_findings_prompt(state, None)
    assert "verified construction for k % 7 = 2" in prompt
    assert "same file, wrong theorem" not in prompt


def test_finding_delivery_is_scoped_to_current_target_and_honors_legacy_exact_target(
    monkeypatch,
):
    """Deliver a parent finding once again after the queue creates a descendant."""
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    active_file = "/tmp/Erdos242.lean"
    parent = "erdos_242"
    child = "erdos_242_residual_mod_seven_eq_two"
    parent_id = plan_state.node_id_for(parent, active_file)
    child_id = plan_state.node_id_for(child, active_file)
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(id=parent_id, name=parent, file=active_file),
            plan_state.GraphNode(id=child_id, name=child, file=active_file),
        ),
        edges=(plan_state.GraphEdge(source=child_id, target=parent_id, kind="split_of"),),
    )
    summary = {
        "dispatch_ledger": [
            {
                "spec": {
                    "job_id": "campaign.ds-096",
                    "inputs": {"target_symbol": parent, "active_file": active_file},
                }
            }
        ],
        "research_findings": [
            {
                "job_id": "campaign.ds-096",
                "deliverable": {"summary": "promote Research242 construction"},
            }
        ],
    }
    monkeypatch.setattr(runner.plan_state, "load_summary", lambda: summary)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: blueprint)

    state = {
        "current_queue_assignment": {"target_symbol": parent, "active_file": active_file},
        # Persisted by the old globally job-keyed implementation.
        "research_findings_delivered": ["campaign.ds-096"],
    }
    assert runner._take_research_findings_prompt(state, None) == ""

    state["current_queue_assignment"]["target_symbol"] = child
    first_child = runner._take_research_findings_prompt(state, None)
    second_child = runner._take_research_findings_prompt(state, None)

    assert "promote Research242 construction" in first_child
    assert second_child == ""
    research_findings.acknowledge_foreground_deliveries(
        state,
        [
            {"role": "user", "content": first_child},
            {"role": "assistant", "content": "I will use the inherited construction."},
        ],
    )
    assert "campaign.ds-096" in state["research_findings_delivered"]
    assert any(
        "campaign.ds-096" in key and child in key for key in state["research_findings_delivered"]
    )


def test_delivery_markers_survive_restart_and_remain_target_scoped(monkeypatch, tmp_path):
    """Only an acknowledged turn suppresses restart delivery for its target."""
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    active_file = str(tmp_path / "Erdos242.lean")
    parent = "erdos_242"
    child = "erdos_242_residual_mod_seven_eq_two"
    parent_id = plan_state.node_id_for(parent, active_file)
    child_id = plan_state.node_id_for(child, active_file)
    blueprint = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(id=parent_id, name=parent, file=active_file),
            plan_state.GraphNode(id=child_id, name=child, file=active_file),
        ),
        edges=(plan_state.GraphEdge(source=child_id, target=parent_id, kind="split_of"),),
    )
    summary = {
        "research_findings": [
            {
                "job_id": "campaign.ds-096",
                "target_symbol": parent,
                "active_file": active_file,
                "deliverable": {"summary": "verified child construction"},
            }
        ]
    }
    monkeypatch.setattr(runner.plan_state, "load_summary", lambda: summary)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: blueprint)

    parent_state = {
        "campaign_id": "campaign-demo",
        "current_queue_assignment": {"target_symbol": parent, "active_file": active_file},
    }
    parent_prompt = runner._take_research_findings_prompt(parent_state, None)
    assert "verified child construction" in parent_prompt

    # Crash before a foreground provider response: no durable marker exists,
    # so the same assignment must receive the finding again after restart.
    unacknowledged_restart = {
        "campaign_id": "campaign-demo",
        "current_queue_assignment": {"target_symbol": parent, "active_file": active_file},
    }
    assert not research_findings.hydrate_delivery_markers(unacknowledged_restart)
    assert "verified child construction" in runner._take_research_findings_prompt(
        unacknowledged_restart, None
    )

    acknowledged = research_findings.acknowledge_foreground_deliveries(
        parent_state,
        [
            {"role": "user", "content": parent_prompt},
            {"role": "assistant", "content": "Using the completed finding now."},
        ],
    )
    assert acknowledged == (research_findings.delivery_key("campaign.ds-096", parent),)

    same_parent_restart = {
        "campaign_id": "campaign-demo",
        "current_queue_assignment": {"target_symbol": parent, "active_file": active_file},
    }
    assert research_findings.hydrate_delivery_markers(same_parent_restart)
    assert runner._take_research_findings_prompt(same_parent_restart, None) == ""

    # The marker is still target-scoped: a generated child inherits the
    # parent's mathematical evidence and gets its own acknowledgement.
    child_restart = {
        "campaign_id": "campaign-demo",
        "current_queue_assignment": {"target_symbol": child, "active_file": active_file},
    }
    assert research_findings.hydrate_delivery_markers(child_restart)
    assert "verified child construction" in runner._take_research_findings_prompt(
        child_restart, None
    )


def test_staged_finding_requires_assistant_response_before_acknowledgement():
    state = {"campaign_id": "campaign-demo"}
    marker = research_findings.delivery_key("campaign.ds-288", "erdos_242")
    prompt = research_findings.stage_foreground_delivery(
        state,
        target_symbol="erdos_242",
        markers=[marker],
        prompt="[completed ds-288 finding]",
    )

    assert (
        research_findings.acknowledge_foreground_deliveries(
            state,
            [{"role": "user", "content": prompt}],
        )
        == ()
    )
    assert "research_findings_delivered" not in state

    acknowledged = research_findings.acknowledge_foreground_deliveries(
        state,
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "I received ds-288."},
        ],
    )

    assert acknowledged == (marker,)
    assert state["research_findings_delivered"] == [marker]


def test_acknowledged_finding_survives_summary_and_process_state_regression(
    monkeypatch,
    tmp_path,
):
    """Recover an acknowledged pair from its isolated write-ahead receipt."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    campaign_id = "campaign-durable-ack"
    target_symbol = "erdos_242"
    active_file = str(tmp_path / "Erdos242.lean")
    job_ids = ("campaign.ds-290", "campaign.em-291", "campaign.ds-332")
    markers = tuple(
        sorted(research_findings.delivery_key(job_id, target_symbol) for job_id in job_ids)
    )
    summary_path = research_findings._summary_path()
    findings = [
        {
            "job_id": job_id,
            "target_symbol": target_symbol,
            "active_file": active_file,
            "deliverable": {"summary": f"one-shot completed finding {job_id}"},
        }
        for job_id in job_ids
    ]
    workflow_json_io.write_json_file(
        summary_path,
        {
            "research_findings": findings,
            "research_delivery_state": {"campaign_id": campaign_id},
        },
    )
    monkeypatch.setattr(
        runner.plan_state,
        "load_summary",
        lambda: workflow_json_io.read_json_file(summary_path),
    )
    state = {
        "campaign_id": campaign_id,
        "current_queue_assignment": {
            "target_symbol": target_symbol,
            "active_file": active_file,
        },
    }
    prompt = runner._take_research_findings_prompt(state, None)

    assert (
        research_findings.acknowledge_foreground_deliveries(
            state,
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "Consumed the completed finding."},
            ],
        )
        == markers
    )

    # Reproduce the live failure: both the in-process dict and the shared
    # summary mirror regress after the acknowledgement event.
    regressed = workflow_json_io.read_json_file(summary_path)
    regressed["research_delivery_state"] = {"campaign_id": campaign_id}
    workflow_json_io.write_json_file(summary_path, regressed)
    restarted = {
        "campaign_id": campaign_id,
        "current_queue_assignment": {
            "target_symbol": target_symbol,
            "active_file": active_file,
        },
    }

    assert runner._take_research_findings_prompt(restarted, None) == ""
    assert restarted["research_findings_delivered"] == list(markers)
    repaired = workflow_json_io.read_json_file(summary_path)
    assert set(markers) <= set(repaired["research_delivery_state"]["research_findings_delivered"])


def test_delivery_token_first_seen_in_assistant_requires_a_later_assistant():
    """An assistant cannot acknowledge a token introduced in its own message."""
    state = {"campaign_id": ""}
    marker = research_findings.delivery_key("campaign.ds-self", "demo")
    prompt = research_findings.stage_foreground_delivery(
        state,
        target_symbol="demo",
        markers=[marker],
        prompt="self-seen finding",
    )

    assert (
        research_findings.acknowledge_foreground_deliveries(
            state,
            [{"role": "assistant", "content": prompt}],
        )
        == ()
    )
    assert research_findings.acknowledge_foreground_deliveries(
        state,
        [
            {"role": "assistant", "content": prompt},
            {"role": "assistant", "content": "Now I consumed the prior context."},
        ],
    ) == (marker,)


def test_pending_foreground_admission_never_evicts_over_sixty_four_records():
    """A full process-local queue rejects new staging without dropping old tags."""
    state: dict = {"campaign_id": "campaign-staging-cap"}
    admitted: list[str] = []
    refused: list[str] = []
    for index in range(research_findings.PENDING_FOREGROUND_CAP + 7):
        marker = research_findings.delivery_key(f"campaign.ds-{index:03d}", f"target_{index}")
        prompt = research_findings.stage_foreground_delivery(
            state,
            target_symbol=f"target_{index}",
            markers=[marker],
            prompt=f"finding {index}",
        )
        (admitted if prompt else refused).append(marker)

    records = research_findings._pending_foreground_records(state)
    retained = {marker for record in records for marker in record["markers"]}
    assert len(records) == research_findings.PENDING_FOREGROUND_CAP
    assert retained == set(admitted)
    assert set(refused).isdisjoint(retained)


def test_more_than_one_hundred_undelivered_findings_survive_restart(monkeypatch, tmp_path):
    """Drain a durable FIFO across both historical in-memory cap boundaries."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: None)
    active_file = str(tmp_path / "Erdos242.lean")
    target_symbol = "erdos_242"
    findings = [
        {
            "job_id": f"campaign.ds-{index:03d}",
            "archetype": "deep_search",
            "target_symbol": target_symbol,
            "active_file": active_file,
            "deliverable": {"summary": f"durable finding {index}"},
        }
        for index in range(130)
    ]
    workflow_json_io.write_json_file(
        research_findings._summary_path(),
        {"research_findings": findings},
    )
    monkeypatch.setattr(
        runner.plan_state,
        "load_summary",
        lambda: workflow_json_io.read_json_file(research_findings._summary_path()),
    )

    state = {
        "campaign_id": "campaign-restart-stress",
        "current_queue_assignment": {
            "target_symbol": target_symbol,
            "active_file": active_file,
        },
    }
    acknowledged_count = 0
    while acknowledged_count < 66:
        prompt = runner._take_research_findings_prompt(state, None)
        assert prompt
        acknowledged = research_findings.acknowledge_foreground_deliveries(
            state,
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "Consumed this complete batch."},
            ],
        )
        acknowledged_count += len(acknowledged)

    restarted = {
        "campaign_id": "campaign-restart-stress",
        "current_queue_assignment": {
            "target_symbol": target_symbol,
            "active_file": active_file,
        },
    }
    assert research_findings.hydrate_delivery_markers(restarted)
    while True:
        prompt = runner._take_research_findings_prompt(restarted, None)
        if not prompt:
            break
        research_findings.acknowledge_foreground_deliveries(
            restarted,
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "Consumed after restart."},
            ],
        )

    delivered = set(restarted["research_findings_delivered"])
    expected = {
        research_findings.delivery_key(f"campaign.ds-{index:03d}", target_symbol)
        for index in range(130)
    }
    assert expected <= delivered
    persisted = workflow_json_io.read_json_file(research_findings._summary_path())
    assert len(persisted["research_findings"]) <= research_portfolio.FINDINGS_CAP


def test_pending_finding_is_reattached_after_context_reset():
    state = {"campaign_id": "campaign-demo"}
    marker = research_findings.delivery_key("campaign.em-289", "erdos_242")
    prompt = research_findings.stage_foreground_delivery(
        state,
        target_symbol="erdos_242",
        markers=[marker],
        prompt="[completed em-289 finding]",
    )

    unchanged = research_findings.attach_pending_foreground_prompts(
        state,
        target_symbol="erdos_242",
        user_message="continue",
        conversation_history=[{"role": "user", "content": prompt}],
    )
    reattached = research_findings.attach_pending_foreground_prompts(
        state,
        target_symbol="erdos_242",
        user_message="fresh epoch",
        conversation_history=[],
    )

    assert unchanged == "continue"
    assert "fresh epoch" in reattached
    assert "completed em-289 finding" in reattached


def test_assignment_change_destages_old_target_without_acknowledging_it():
    state = {"campaign_id": "campaign-demo"}
    marker = research_findings.delivery_key("campaign.ds-old", "old_target")
    old_prompt = research_findings.stage_foreground_delivery(
        state,
        target_symbol="old_target",
        markers=[marker],
        prompt="old target evidence",
    )

    new_message = research_findings.attach_pending_foreground_prompts(
        state,
        target_symbol="new_target",
        user_message="prove the new target",
        conversation_history=[],
    )

    assert new_message == "prove the new target"
    assert "research_findings_delivered" not in state
    assert (
        research_findings.pending_foreground_markers(
            state,
            target_symbol="old_target",
        )
        == set()
    )
    assert (
        research_findings.stage_foreground_delivery(
            state,
            target_symbol="old_target",
            markers=[marker],
            prompt="old target evidence",
        )
        == old_prompt
    )


def test_orchestrator_seen_markers_are_campaign_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state = {"campaign_id": "campaign-one"}
    marker = research_findings.delivery_key("campaign.ds-001", "demo")
    research_findings.persist_delivery_markers(
        state,
        key="orchestrator_jobs_seen",
        markers=[marker],
    )

    resumed = {"campaign_id": "campaign-one"}
    assert research_findings.hydrate_delivery_markers(resumed)
    assert resumed["orchestrator_jobs_seen"] == [marker]

    fresh_campaign = {"campaign_id": "campaign-two"}
    assert not research_findings.hydrate_delivery_markers(fresh_campaign)
    assert "orchestrator_jobs_seen" not in fresh_campaign


def test_campaign_exit_shuts_down_research_workers(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_workflow_kind", lambda: "prove")
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(
        runner.campaign_epoch,
        "ensure_campaign",
        lambda state: {"campaign_id": "campaign-demo"},
    )
    monkeypatch.setattr(
        runner.research_portfolio,
        "shutdown_portfolio",
        lambda *, campaign_id, reason: calls.append((campaign_id, reason)) or [],
    )
    monkeypatch.setattr(runner.campaign_epoch, "record_process_exit", lambda *a, **k: None)

    assert runner._record_campaign_exit(0, {}, {"verified": True}, reason="verified") == 0
    assert calls == [("campaign-demo", "verified")]


def test_finalizer_still_records_after_interrupted_worker_shutdown(monkeypatch):
    recorded: list[tuple[int, str]] = []
    shutdown_calls: list[str] = []
    autonomy_state: dict[str, object] = {}
    monkeypatch.setattr(runner, "_workflow_kind", lambda: "prove")
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(
        runner.campaign_epoch,
        "ensure_campaign",
        lambda state: {"campaign_id": "campaign-demo"},
    )

    def shutdown_portfolio(**kwargs):
        shutdown_calls.append(str(kwargs["reason"]))
        if len(shutdown_calls) == 1:
            raise KeyboardInterrupt
        return []

    monkeypatch.setattr(runner.research_portfolio, "shutdown_portfolio", shutdown_portfolio)
    monkeypatch.setattr(
        runner.campaign_epoch,
        "record_process_exit",
        lambda state, code, **kwargs: recorded.append((code, kwargs["reason"])),
    )
    monkeypatch.setattr(runner, "_quiesce_native_writer_threads", lambda _agent: None)
    monkeypatch.setattr(runner, "shutdown_native_runtime_services", lambda _agent: ())
    monkeypatch.setattr(
        runner,
        "_write_signal_interruption_checkpoint",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(runner, "_release_native_runner_locks", lambda _agent: None)
    monkeypatch.setattr(runner, "_persist_live_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_record_agent_activity", lambda *args, **kwargs: None)

    result = runner._finalize_native_run(
        runner.NativeRunFinalizer(),
        runner.EXIT_INTERRUPTED,
        agent=None,
        history=[],
        compaction_state={},
        checkpoint_state={},
        autonomy_state=autonomy_state,
        live_state={"verified": False},
        reason="signal interrupt",
    )

    assert result == runner.EXIT_INTERRUPTED
    assert shutdown_calls == ["signal interrupt", "signal interrupt"]
    assert recorded == [(runner.EXIT_INTERRUPTED, "signal interrupt")]
