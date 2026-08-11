from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from leanflow_cli.workflows import dispatch_ledger_compaction, research_findings, target_handoff
from leanflow_cli.workflows.dispatch_ledger_compaction import (
    DISPATCH_ARCHIVE_KEY,
    DispatchLedgerArchiveError,
    compact_consumed_dispatch_records,
    compact_terminal_dispatch_records,
    hydrate_dispatch_record,
)
from leanflow_cli.workflows.dispatch_models import JobBudget, JobSpec, LedgerEntry
from leanflow_cli.workflows.dispatch_service import DispatchService
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

_ROUTE_CONTEXT_MARKER = "[LEANFLOW BOUNDED PARENT ROUTE CONTEXT]"


def _payload_sha256(value) -> str:
    """Return the compactor's exact JSON-payload digest contract."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record(*, state: str) -> dict:
    exact_helper = "private lemma large_helper : True := by\n  exact True.intro\n" + (
        "-- exact proof payload\n" * 300
    )
    return {
        "state": state,
        "spec": {
            "job_id": "run.orchestrator.ds-001",
            "objective": "Find a distinct proof route.",
            "inputs": {
                "recent_route_context": {
                    "assignment": {"target_symbol": "demo"},
                    "sha256": "a" * 64,
                },
                "recent_route_context_sha256": "a" * 64,
            },
        },
        "result": {
            "deliverable": {
                "summary": "verified partial helper",
                "checked_helpers": [
                    {
                        "declaration": exact_helper,
                        "worker_check": {"valid_without_sorry": True},
                    }
                ],
                "parent_route_context": {
                    "recent_research_routes": [{"job_id": "older"}],
                    "sha256": "b" * 64,
                },
            }
        },
    }


def _consumed_record(*, process_kind: str = "sync") -> dict:
    """Return one large, fully consumed row with exact checked Lean evidence."""
    exact_helper = "private lemma archive_helper : True := by\n  exact True.intro\n" + (
        "-- exact checked source\n" * 500
    )
    entry = LedgerEntry(
        spec=JobSpec(
            job_id="campaign.orchestrator.ds-659",
            archetype="deep_search",
            requester_role="orchestrator",
            objective="Audit a distinct source-backed proof route.",
            budget=JobBudget(api_steps=8, wall_clock_s=120),
            deliverable="findings_report",
            inputs={
                "campaign_id": "campaign",
                "campaign_epoch": 41,
                "target_symbol": "Erdos242.erdos_242",
                "active_file": "ErdosProblems/242.lean",
                "assignment_statement_sha256": "a" * 64,
                "route_key": "source-backed-audit",
                "route_signature": "b" * 64,
            },
            toolsets=("lean", "web"),
            scope={"scratch_only": True},
            parent_job_id="campaign.orchestrator",
        ),
        state="done",
        created_at="2026-07-19T01:00:00+00:00",
        started_at="2026-07-19T01:01:00+00:00",
        finished_at="2026-07-19T01:12:12+00:00",
        result={
            "status": "done",
            "deliverable": {
                "summary": "A source-backed helper was checked. " + ("evidence " * 1_000),
                "checked_helper_status": "worker_checked_parent_recheck_required",
                "parent_recheck_required": True,
                "checked_helpers": [
                    {
                        "anchor_target_symbol": "Erdos242.erdos_242",
                        "active_file": "ErdosProblems/242.lean",
                        "declaration": exact_helper,
                        "declaration_sha256": hashlib.sha256(
                            exact_helper.encode("utf-8")
                        ).hexdigest(),
                        "worker_check": {
                            "valid_without_sorry": True,
                            "has_errors": False,
                            "has_sorry": False,
                            "verification_scope": "helper_candidate",
                        },
                        "parent_recheck_required": True,
                    }
                ],
            },
            "artifact_paths": [".leanflow/workflow-state/scratch/helper.lean"],
            "plan_delta": [{"kind": "candidate", "payload": "x" * 4_000}],
        },
        consumed=True,
    ).to_mapping()
    if process_kind == "modern":
        entry.update(
            {
                "launch_nonce": "modern-launch-nonce",
                "process_id": 43210,
                "process_group_id": 43210,
                "process_session_id": 43210,
                "process_token_sha256": "c" * 64,
            }
        )
    elif process_kind == "legacy":
        entry.update(
            {
                "process_id": 43210,
                "process_group_id": 43210,
                "process_session_id": 43210,
            }
        )
    return entry


def test_terminal_compaction_removes_only_parent_owned_context() -> None:
    record = _record(state="done")
    expected_context_hash = _payload_sha256(record["spec"]["inputs"]["recent_route_context"])
    expected_helper = deepcopy(record["result"]["deliverable"]["checked_helpers"])
    expected_result = deepcopy(record["result"])

    removed = compact_terminal_dispatch_records([record])

    assert removed == 1
    assert "recent_route_context" not in record["spec"]["inputs"]
    assert record["spec"]["inputs"]["recent_route_context_sha256"] == expected_context_hash
    assert record["result"]["deliverable"]["parent_route_context"]["sha256"] == "b" * 64
    assert record["result"]["deliverable"]["checked_helpers"] == expected_helper
    assert record["result"] == expected_result
    assert record["spec"]["objective"] == "Find a distinct proof route."


def test_terminal_compaction_strips_rendered_objective_context_and_keeps_evidence() -> None:
    record = _record(state="done")
    expected_context_hash = _payload_sha256(record["spec"]["inputs"]["recent_route_context"])
    semantic_objective = "Find a distinct proof route."
    rendered_context = "Prior rejected route: fixed witness.\n" + ("x" * 6_000)
    full_objective = f"{semantic_objective}\n\n{_ROUTE_CONTEXT_MARKER}\n{rendered_context}"
    record["spec"]["objective"] = full_objective
    expected_result = deepcopy(record["result"])
    before_size = len(json.dumps(record, sort_keys=True))

    removed = compact_terminal_dispatch_records([record])

    assert removed == 2
    assert record["spec"]["objective"] == semantic_objective
    assert (
        record["spec"]["inputs"]["objective_sha256"]
        == hashlib.sha256(full_objective.encode("utf-8")).hexdigest()
    )
    assert record["spec"]["inputs"]["recent_route_context_sha256"] == expected_context_hash
    assert record["result"] == expected_result
    assert len(json.dumps(record, sort_keys=True)) < before_size - len(rendered_context)


@pytest.mark.parametrize("state", ["proposed", "deployed", "running"])
def test_live_dispatch_record_keeps_recovery_context_byte_identical(state: str) -> None:
    record = _record(state=state)
    record["spec"][
        "objective"
    ] += f"\n\n{_ROUTE_CONTEXT_MARKER}\nPrior rejected route: fixed witness."
    original = deepcopy(record)
    serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    terminal = _record(state="done")

    assert compact_terminal_dispatch_records([terminal, record]) == 1
    assert record == original
    assert json.dumps(record, ensure_ascii=False, separators=(",", ":")) == serialized


def test_terminal_compaction_replaces_stale_context_hash_from_exact_payload() -> None:
    record = _record(state="done")
    context = deepcopy(record["spec"]["inputs"]["recent_route_context"])
    record["spec"]["inputs"]["recent_route_context_sha256"] = "stale-digest"

    assert compact_terminal_dispatch_records([record]) == 1
    assert record["spec"]["inputs"]["recent_route_context_sha256"] == _payload_sha256(context)


def test_terminal_compaction_keeps_objective_when_digest_storage_is_malformed() -> None:
    record = _record(state="done")
    record["spec"][
        "objective"
    ] += f"\n\n{_ROUTE_CONTEXT_MARKER}\nPrior rejected route: fixed witness."
    record["spec"]["inputs"] = "malformed"
    original = deepcopy(record)

    assert compact_terminal_dispatch_records([record]) == 0
    assert record == original


def test_terminal_compaction_is_idempotent_and_hashes_legacy_context() -> None:
    record = _record(state="killed")
    record["spec"][
        "objective"
    ] += f"\n\n{_ROUTE_CONTEXT_MARKER}\nPrior rejected route: fixed witness."
    record["spec"]["inputs"].pop("recent_route_context_sha256")
    record["spec"]["inputs"]["recent_route_context"].pop("sha256")

    assert compact_terminal_dispatch_records([record]) == 2
    first = deepcopy(record)
    assert len(record["spec"]["inputs"]["recent_route_context_sha256"]) == 64
    assert len(record["spec"]["inputs"]["objective_sha256"]) == 64
    assert compact_terminal_dispatch_records([record]) == 0
    assert record == first


def test_compacted_job_spec_round_trips_without_mutating_source_value() -> None:
    semantic_objective = "Find a distinct proof route."
    full_objective = (
        f"{semantic_objective}\n\n{_ROUTE_CONTEXT_MARKER}\n" "Prior rejected route: fixed witness."
    )
    route_context = {
        "assignment": {"target_symbol": "demo", "active_file": "Demo.lean"},
        "recent_research_routes": [{"job_id": "run.orchestrator.ds-000"}],
    }
    spec = JobSpec(
        job_id="run.orchestrator.ds-001",
        archetype="deep_search",
        requester_role="orchestrator",
        objective=full_objective,
        budget=JobBudget(api_steps=8, wall_clock_s=120),
        deliverable="findings_report",
        inputs={
            "target_symbol": "demo",
            "active_file": "Demo.lean",
            "recent_route_context": route_context,
            "recent_route_context_sha256": "stale-digest",
        },
        toolsets=("web", "lean"),
        scope={"scratch_only": True},
        parent_job_id="run.orchestrator",
    )
    source_mapping = deepcopy(spec.to_mapping())
    record = LedgerEntry(spec=spec, state="done", result={"status": "done"}).to_mapping()

    assert compact_terminal_dispatch_records([record]) == 2
    restored = LedgerEntry.from_mapping(record)

    assert restored.spec.validate() == []
    assert restored.spec.objective == semantic_objective
    assert (
        restored.spec.inputs["objective_sha256"]
        == hashlib.sha256(full_objective.encode("utf-8")).hexdigest()
    )
    assert restored.spec.inputs["recent_route_context_sha256"] == _payload_sha256(route_context)
    assert restored.to_mapping() == record
    assert spec.to_mapping() == source_mapping


def test_dispatch_transaction_compacts_existing_terminal_history(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state_root = workflow_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    summary_path = state_root / "summary.json"
    record = _record(state="done")
    record["spec"][
        "objective"
    ] += f"\n\n{_ROUTE_CONTEXT_MARKER}\nPrior rejected route: fixed witness."
    live_record = _record(state="running")
    live_record["spec"]["objective"] += f"\n\n{_ROUTE_CONTEXT_MARKER}\nStill needed for recovery."
    expected_live_bytes = json.dumps(
        live_record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    summary_path.write_text(
        json.dumps({"dispatch_ledger": [record, live_record]}),
        encoding="utf-8",
    )

    service = DispatchService(root_job_id="run")
    service._transaction(lambda ledger: (None, []))

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    persisted = summary["dispatch_ledger"][0]
    assert "recent_route_context" not in persisted["spec"]["inputs"]
    assert persisted["spec"]["objective"] == "Find a distinct proof route."
    assert len(persisted["spec"]["inputs"]["objective_sha256"]) == 64
    assert "parent_route_context" in persisted["result"]["deliverable"]
    assert persisted["result"]["deliverable"]["checked_helpers"]
    persisted_live_bytes = json.dumps(
        summary["dispatch_ledger"][1],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert persisted_live_bytes == expected_live_bytes
    assert summary["dispatch_ledger_compaction"]["fields_removed"] == 2


def test_failed_dispatch_transaction_does_not_partially_compact(monkeypatch, tmp_path) -> None:
    """A failed ledger mutation cannot commit compaction as a side effect."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state_root = workflow_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    summary_path = state_root / "summary.json"
    record = _record(state="done")
    record["spec"][
        "objective"
    ] += f"\n\n{_ROUTE_CONTEXT_MARKER}\nPrior rejected route: fixed witness."
    summary_path.write_text(json.dumps({"dispatch_ledger": [record]}), encoding="utf-8")
    before = summary_path.read_bytes()
    service = DispatchService(root_job_id="run")

    def fail_after_mutation(ledger):
        ledger[0]["notes"] = "must not persist"
        raise RuntimeError("abort transaction")

    with pytest.raises(RuntimeError, match="abort transaction"):
        service._transaction(fail_after_mutation)

    assert summary_path.read_bytes() == before


def test_consumed_terminal_compaction_archives_exact_result_and_shrinks_summary(tmp_path) -> None:
    record = _consumed_record()
    original = deepcopy(record)
    before_size = len(json.dumps(record, sort_keys=True))

    assert compact_consumed_dispatch_records([record], state_root=tmp_path) == 1

    metadata = record[DISPATCH_ARCHIVE_KEY]
    archive_path = tmp_path / metadata["path"]
    compact_helper = record["result"]["deliverable"]["checked_helpers"][0]
    assert archive_path.is_file()
    assert metadata["result_sha256"] == _payload_sha256(original["result"])
    assert (
        compact_helper["declaration_sha256"]
        == original["result"]["deliverable"]["checked_helpers"][0]["declaration_sha256"]
    )
    assert compact_helper["worker_check"]["valid_without_sorry"] is True
    assert "declaration" not in compact_helper
    assert len(json.dumps(record, sort_keys=True)) < before_size // 3

    restored = hydrate_dispatch_record(record, state_root=tmp_path)
    assert restored["spec"] == original["spec"]
    assert restored["result"] == original["result"]
    assert restored["finished_at"] == original["finished_at"]


@pytest.mark.parametrize(
    ("mutation", "process_kind"),
    [
        ({"state": "running"}, "sync"),
        ({"consumed": False}, "sync"),
        ({"finished_at": ""}, "sync"),
        ({}, "legacy"),
        (
            {
                "process_released_at": "2026-07-19T01:13:00+00:00",
                "process_release_report_key": "release:pending",
                "process_release_reported_at": "",
            },
            "legacy",
        ),
    ],
)
def test_second_stage_never_compacts_mutable_or_unreleased_rows(
    tmp_path, mutation: dict, process_kind: str
) -> None:
    record = _consumed_record(process_kind=process_kind)
    record.update(mutation)
    original = deepcopy(record)

    assert compact_consumed_dispatch_records([record], state_root=tmp_path) == 0
    assert record == original
    assert not (tmp_path / "dispatch-archives").exists()


def test_second_stage_accepts_reaped_modern_identity_and_is_idempotent(tmp_path) -> None:
    record = _consumed_record(process_kind="modern")

    assert compact_consumed_dispatch_records([record], state_root=tmp_path) == 1
    first = deepcopy(record)
    assert compact_consumed_dispatch_records([record], state_root=tmp_path) == 0
    assert record == first
    assert hydrate_dispatch_record(record, state_root=tmp_path)["result"]["status"] == "done"


def test_archive_corruption_fails_closed_instead_of_using_projection(tmp_path) -> None:
    record = _consumed_record()
    assert compact_consumed_dispatch_records([record], state_root=tmp_path) == 1
    archive_path = tmp_path / record[DISPATCH_ARCHIVE_KEY]["path"]
    archive_path.write_bytes(b"corrupt")

    with pytest.raises(DispatchLedgerArchiveError, match="digest mismatch"):
        hydrate_dispatch_record(record, state_root=tmp_path)


def test_missing_archive_fails_closed_instead_of_using_projection(tmp_path) -> None:
    record = _consumed_record()
    assert compact_consumed_dispatch_records([record], state_root=tmp_path) == 1
    (tmp_path / record[DISPATCH_ARCHIVE_KEY]["path"]).unlink()

    with pytest.raises(DispatchLedgerArchiveError, match="is missing"):
        hydrate_dispatch_record(record, state_root=tmp_path)


def test_dispatch_transaction_compacts_consumed_payload_but_entries_hydrate_it(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state_root = workflow_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    summary_path = state_root / "summary.json"
    original = _consumed_record()
    summary_path.write_text(json.dumps({"dispatch_ledger": [original]}), encoding="utf-8")

    service = DispatchService(root_job_id="campaign")
    service._transaction(lambda ledger: (None, []))

    persisted = json.loads(summary_path.read_text(encoding="utf-8"))
    assert DISPATCH_ARCHIVE_KEY in persisted["dispatch_ledger"][0]
    assert persisted["dispatch_ledger_compaction"]["version"] == 2
    assert persisted["dispatch_ledger_compaction"]["records_archived"] == 1
    restored = service.entries()[0]
    assert restored.result == original["result"]
    assert restored.spec.to_mapping() == original["spec"]


def test_entry_hydrates_only_first_exact_archived_row_among_large_cold_history(
    monkeypatch, tmp_path
) -> None:
    """A point lookup must remain O(1) in hydrated archive payloads."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state_root = workflow_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    target = _consumed_record()
    target["notes"] = "first exact row"
    original = deepcopy(target)
    assert compact_consumed_dispatch_records([target], state_root=state_root) == 1

    # These rows have the shape and volume of cold first-stage-compacted
    # terminal history; only the selected target keeps a cold archive.
    cold_history = []
    for index in range(725):
        decoy = deepcopy(target)
        decoy.pop(DISPATCH_ARCHIVE_KEY)
        decoy["spec"]["job_id"] = f"campaign.orchestrator.history-{index:03d}"
        cold_history.append(decoy)
    target_index = 511
    cold_history[target_index] = target
    later_duplicate = deepcopy(target)
    later_duplicate["notes"] = "later duplicate"
    cold_history.append(later_duplicate)
    service = DispatchService(root_job_id="campaign")
    service._summary_path().write_text(
        json.dumps({"dispatch_ledger": cold_history}),
        encoding="utf-8",
    )

    hydrated_job_ids: list[str] = []
    original_hydrate = dispatch_ledger_compaction.hydrate_dispatch_record

    def count_hydration(raw, *, state_root):
        hydrated_job_ids.append(str(dict(raw.get("spec") or {}).get("job_id", "")))
        return original_hydrate(raw, state_root=state_root)

    monkeypatch.setattr(
        dispatch_ledger_compaction,
        "hydrate_dispatch_record",
        count_hydration,
    )

    baseline = next(
        entry
        for entry in service._load_ledger()
        if entry.spec.job_id == "campaign.orchestrator.ds-659"
    )
    assert len(hydrated_job_ids) == 726
    hydrated_job_ids.clear()
    restored = service._entry("campaign.orchestrator.ds-659")

    assert hydrated_job_ids == ["campaign.orchestrator.ds-659"]
    assert restored == baseline
    assert restored.notes == "first exact row"
    assert restored.result == original["result"]
    assert restored.spec.to_mapping() == original["spec"]
    with pytest.raises(KeyError, match="unknown dispatch job"):
        service._entry("campaign.orchestrator.ds-missing")
    assert hydrated_job_ids == ["campaign.orchestrator.ds-659"]


def test_entry_exact_archived_corruption_still_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state_root = workflow_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    target = _consumed_record()
    assert compact_consumed_dispatch_records([target], state_root=state_root) == 1
    (state_root / target[DISPATCH_ARCHIVE_KEY]["path"]).write_bytes(b"corrupt")
    service = DispatchService(root_job_id="campaign")
    service._summary_path().write_text(
        json.dumps({"dispatch_ledger": [target]}),
        encoding="utf-8",
    )

    with pytest.raises(DispatchLedgerArchiveError, match="digest mismatch"):
        service._entry("campaign.orchestrator.ds-659")


def test_open_jobs_skips_terminal_archives_and_hydrates_every_recovery_row(
    monkeypatch, tmp_path
) -> None:
    """Terminal history stays cold while malformed states remain recoverable."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state_root = workflow_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    terminal = _consumed_record()
    assert compact_consumed_dispatch_records([terminal], state_root=state_root) == 1

    terminal_history = [terminal]
    terminal_states = ("done", "failed", "stuck", "killed")
    for index in range(724):
        compact_terminal = deepcopy(terminal)
        compact_terminal.pop(DISPATCH_ARCHIVE_KEY)
        compact_terminal["spec"]["job_id"] = f"campaign.orchestrator.history-{index:03d}"
        compact_terminal["state"] = terminal_states[index % len(terminal_states)]
        terminal_history.append(compact_terminal)

    live = deepcopy(_consumed_record())
    live.update(
        {
            "state": "running",
            "consumed": False,
            "finished_at": "",
            "result": {},
        }
    )
    live["spec"]["job_id"] = "campaign.orchestrator.ds-live"
    malformed_state = deepcopy(live)
    malformed_state["spec"]["job_id"] = "campaign.orchestrator.ds-malformed"
    malformed_state["state"] = ["done"]
    ledger = [*terminal_history, live, malformed_state]
    service = DispatchService(root_job_id="campaign")
    service._summary_path().write_text(
        json.dumps({"dispatch_ledger": ledger}),
        encoding="utf-8",
    )
    expected = [entry for entry in service.entries() if not entry.is_terminal()]

    hydrated_job_ids: list[str] = []
    original_hydrate = dispatch_ledger_compaction.hydrate_dispatch_record

    def count_hydration(raw, *, state_root):
        hydrated_job_ids.append(str(dict(raw.get("spec") or {}).get("job_id", "")))
        return original_hydrate(raw, state_root=state_root)

    monkeypatch.setattr(
        dispatch_ledger_compaction,
        "hydrate_dispatch_record",
        count_hydration,
    )

    actual = service.open_jobs()

    assert actual == expected
    assert [entry.spec.job_id for entry in actual] == [
        "campaign.orchestrator.ds-live",
        "campaign.orchestrator.ds-malformed",
    ]
    assert hydrated_job_ids == [
        "campaign.orchestrator.ds-live",
        "campaign.orchestrator.ds-malformed",
    ]


def test_open_jobs_unknown_archived_state_hydrates_and_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state_root = workflow_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    malformed = _consumed_record()
    assert compact_consumed_dispatch_records([malformed], state_root=state_root) == 1
    malformed["state"] = "unknown-future-state"
    malformed[DISPATCH_ARCHIVE_KEY] = "malformed"
    service = DispatchService(root_job_id="campaign")
    service._summary_path().write_text(
        json.dumps({"dispatch_ledger": [malformed]}),
        encoding="utf-8",
    )

    with pytest.raises(DispatchLedgerArchiveError, match="metadata is malformed"):
        service.open_jobs()


def test_consume_returns_exact_result_before_persisting_archive(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state_root = workflow_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    summary_path = state_root / "summary.json"
    original = _consumed_record()
    original["consumed"] = False
    summary_path.write_text(json.dumps({"dispatch_ledger": [original]}), encoding="utf-8")

    service = DispatchService(root_job_id="campaign")
    result = service.consume("campaign.orchestrator.ds-659")

    assert result["deliverable"] == original["result"]["deliverable"]
    assert result["artifact_paths"] == original["result"]["artifact_paths"]
    assert result["plan_delta"] == original["result"]["plan_delta"]
    persisted = json.loads(summary_path.read_text(encoding="utf-8"))["dispatch_ledger"][0]
    assert persisted["consumed"] is True
    assert DISPATCH_ARCHIVE_KEY in persisted
    assert service.entries()[0].result == original["result"]


def test_research_migration_and_target_handoff_hydrate_compacted_evidence(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state_root = workflow_state_root()
    state_root.mkdir(parents=True, exist_ok=True)
    summary_path = state_root / "summary.json"
    original = _consumed_record()
    summary_path.write_text(
        json.dumps(
            {
                "campaign": {"campaign_id": "campaign"},
                "dispatch_ledger": [original],
            }
        ),
        encoding="utf-8",
    )
    service = DispatchService(root_job_id="campaign")
    service._transaction(lambda ledger: (None, []))

    report = research_findings.migrate_consumed_findings_for_assignment(
        campaign_id="campaign",
        target_symbol="Erdos242.erdos_242",
        active_file="ErdosProblems/242.lean",
    )

    assert report["materialized_job_ids"] == ["campaign.orchestrator.ds-659"]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    materialized = summary["research_findings"][0]
    assert (
        materialized["deliverable"]["checked_helpers"][0]["declaration"]
        == original["result"]["deliverable"]["checked_helpers"][0]["declaration"]
    )
    findings = target_handoff._consumed_target_findings(
        summary,
        target_symbol="Erdos242.erdos_242",
        active_file="ErdosProblems/242.lean",
        assignment_revision="a" * 64,
    )
    assert (
        findings[0]["deliverable"]["checked_helpers"][0]["declaration"]
        == original["result"]["deliverable"]["checked_helpers"][0]["declaration"]
    )
