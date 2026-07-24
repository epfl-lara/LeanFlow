"""Performance and authority tests for research-backed queue priority."""

from __future__ import annotations

import pytest

from leanflow_cli.workflows import research_finding_priority, research_findings
from leanflow_cli.workflows.dispatch_ledger_compaction import (
    DISPATCH_ARCHIVE_KEY,
    DispatchLedgerArchiveError,
    compact_consumed_dispatch_records,
)
from leanflow_cli.workflows.dispatch_models import JobBudget, JobSpec, LedgerEntry
from leanflow_cli.workflows.plan_state import Blueprint, GraphEdge, GraphNode, node_id_for
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root


def _consumed_record(
    job_id: str,
    *,
    target_symbol: str,
    active_file: str,
) -> dict:
    """Return one archive-eligible research ledger row."""
    return LedgerEntry(
        spec=JobSpec(
            job_id=job_id,
            archetype="deep_search",
            requester_role="orchestrator",
            objective=f"Research {target_symbol}",
            budget=JobBudget(api_steps=2, wall_clock_s=60),
            deliverable="findings_report",
            inputs={
                "target_symbol": target_symbol,
                "active_file": active_file,
            },
        ),
        state="done",
        created_at="2026-07-19T00:00:00+00:00",
        started_at="2026-07-19T00:00:01+00:00",
        finished_at="2026-07-19T00:00:02+00:00",
        result={
            "status": "done",
            "deliverable": {
                "summary": f"evidence for {target_symbol}: " + ("checked detail " * 400),
            },
        },
        consumed=True,
    ).to_mapping()


def test_priority_prepares_authenticated_dispatch_ledger_once(monkeypatch, tmp_path):
    """Hydrate archive evidence once instead of once per graph target."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    blueprint = Blueprint(
        nodes=tuple(
            GraphNode(
                id=node_id_for(f"helper_{index}", "Main.lean"),
                name=f"helper_{index}",
                file="Main.lean",
                statement=f"lemma helper_{index} : True",
            )
            for index in range(20)
        )
    )
    summary = {"dispatch_ledger": [], "research_findings": []}
    hydrate_calls = 0
    original_hydrate = research_findings.dispatch_ledger_compaction.hydrate_dispatch_ledger

    def tracked_hydrate(raw_ledger, *, state_root):
        nonlocal hydrate_calls
        hydrate_calls += 1
        return original_hydrate(raw_ledger, state_root=state_root)

    monkeypatch.setattr(
        research_findings.dispatch_ledger_compaction,
        "hydrate_dispatch_ledger",
        tracked_hydrate,
    )

    priorities = research_finding_priority.priority_by_target(
        summary,
        blueprint=blueprint,
    )

    assert set(priorities) == {f"helper_{index}" for index in range(20)}
    assert hydrate_calls == 1


def test_priority_propagates_dispatch_archive_authentication_failure(monkeypatch, tmp_path):
    """Keep corrupt durable evidence fail-loud in the prepared-index path."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    blueprint = Blueprint(
        nodes=(
            GraphNode(
                id=node_id_for("target", "Main.lean"),
                name="target",
                file="Main.lean",
            ),
        )
    )

    def reject_archive(_raw_ledger, *, state_root):
        raise DispatchLedgerArchiveError(f"corrupt archive under {state_root}")

    monkeypatch.setattr(
        research_findings.dispatch_ledger_compaction,
        "hydrate_dispatch_ledger",
        reject_archive,
    )

    with pytest.raises(DispatchLedgerArchiveError, match="corrupt archive"):
        research_finding_priority.priority_by_target(
            {"dispatch_ledger": [{}], "research_findings": []},
            blueprint=blueprint,
        )


def test_prepared_index_matches_standalone_for_archived_quarantined_ancestor_evidence(
    monkeypatch,
    tmp_path,
):
    """Preserve exact/inherited ordering and quarantine across archive reuse."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    active_file = str(tmp_path / "Main.lean")
    parent = "parent_target"
    child = "child_target"
    parent_id = node_id_for(parent, active_file)
    child_id = node_id_for(child, active_file)
    blueprint = Blueprint(
        nodes=(
            GraphNode(id=parent_id, name=parent, file=active_file),
            GraphNode(id=child_id, name=child, file=active_file),
        ),
        edges=(GraphEdge(source=child_id, target=parent_id, kind="split_of"),),
    )
    exact_job = "campaign.orchestrator.ds-exact"
    ancestor_job = "campaign.orchestrator.ds-ancestor"
    quarantined_job = "campaign.orchestrator.ds-quarantined"
    ledger = [
        _consumed_record(
            exact_job,
            target_symbol=child,
            active_file=active_file,
        ),
        _consumed_record(
            ancestor_job,
            target_symbol=parent,
            active_file=active_file,
        ),
        _consumed_record(
            quarantined_job,
            target_symbol=child,
            active_file=active_file,
        ),
    ]
    assert (
        compact_consumed_dispatch_records(
            ledger,
            state_root=workflow_state_root(),
        )
        == 3
    )
    assert all(DISPATCH_ARCHIVE_KEY in record for record in ledger)
    summary = {
        "dispatch_ledger": ledger,
        "research_findings": [
            {
                "job_id": exact_job,
                "target_symbol": child,
                "active_file": active_file,
                "deliverable": {"verified_helper": "exact_child_helper"},
            },
            {
                "job_id": ancestor_job,
                "target_symbol": parent,
                "active_file": active_file,
                "deliverable": {"verified_helper": "inherited_parent_helper"},
            },
            {
                "job_id": quarantined_job,
                "target_symbol": child,
                "active_file": active_file,
                "deliverable": {"verified_helper": "unsafe_quarantined_helper"},
            },
        ],
        research_findings.FINDING_MIGRATION_KEY: {
            "records": {
                quarantined_job: {"status": "quarantined_hash_mismatch"},
            }
        },
    }

    standalone = research_findings.relevant_findings(
        summary,
        target_symbol=child,
        active_file=active_file,
        blueprint=blueprint,
        limit=None,
    )
    index = research_findings.build_relevant_findings_index(summary)
    prepared = research_findings.relevant_findings(
        summary,
        target_symbol=child,
        active_file=active_file,
        blueprint=blueprint,
        limit=None,
        index=index,
    )

    assert prepared == standalone
    assert [finding["job_id"] for finding in prepared] == [exact_job, ancestor_job]
