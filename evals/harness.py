"""Score persisted proof-workflow artifacts against release criteria.

Scores what the runs already persist — the dependency graph, summary,
journal, and decision packets — against concrete-result and kernel-truth
invariants. The scorer is model-free: capability suites reuse these primitives
over completed run outputs, and this module never launches runs itself. See
``evals/README.md`` for the suite and promotion criteria.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.manager_nudge import contains_surrender_language
from leanflow_cli.workflows.plan_state import (
    FINAL_REPORT_STATUSES,
    Blueprint,
    DeclTruth,
    reconcile,
    status_counters,
)
from leanflow_cli.workflows.workflow_json_io import read_json_file

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = Path(__file__).resolve().parent / "results.jsonl"
CORPUS_MANIFEST_PATH = Path(__file__).resolve().parent / "corpus_manifest.json"

#: Graph statuses that count as verified progress (T3 metric).
VERIFIED_PROGRESS_STATUSES = frozenset({"proved"})
EXPECTED_SUITE_COUNTS = {"t2": 40, "t3": 10, "adversarial": 4}


def load_corpus_manifest(path: Path | str = CORPUS_MANIFEST_PATH) -> dict[str, Any]:
    """Load the frozen capability, research, and adversarial inventories."""
    return dict(read_json_file(Path(path)))


def suite_cases(suite: str, path: Path | str = CORPUS_MANIFEST_PATH) -> tuple[dict[str, Any], ...]:
    """Return one suite's frozen case records."""
    manifest = load_corpus_manifest(path)
    raw_suite = dict(dict(manifest.get("suites") or {}).get(suite) or {})
    return tuple(dict(case) for case in raw_suite.get("cases") or [] if isinstance(case, Mapping))


def validate_corpus_manifest(path: Path | str = CORPUS_MANIFEST_PATH) -> list[str]:
    """Return corpus integrity problems without fetching external repositories."""
    manifest = load_corpus_manifest(path)
    suites = dict(manifest.get("suites") or {})
    sources = dict(manifest.get("sources") or {})
    problems: list[str] = []
    all_ids: set[str] = set()
    for suite, expected in EXPECTED_SUITE_COUNTS.items():
        cases = [case for case in dict(suites.get(suite) or {}).get("cases") or []]
        if len(cases) != expected:
            problems.append(f"{suite} has {len(cases)} cases; expected {expected}")
        for raw in cases:
            if not isinstance(raw, Mapping):
                problems.append(f"{suite} contains a non-object case")
                continue
            case = dict(raw)
            case_id = str(case.get("id", "") or "")
            source = str(case.get("source", "") or "")
            file_name = str(case.get("file", "") or "")
            declaration = str(case.get("declaration", "") or "")
            if not case_id or case_id in all_ids:
                problems.append(f"{suite} has a missing or duplicate case id {case_id!r}")
            all_ids.add(case_id)
            if source not in sources:
                problems.append(f"{case_id} names unknown source {source!r}")
            if not file_name or not declaration:
                problems.append(f"{case_id} is missing file or declaration")
            if source == "LeanFlow" and not (REPO_ROOT / file_name).is_file():
                problems.append(f"{case_id} local file does not exist: {file_name}")
    for source, raw in sources.items():
        revision = str(dict(raw or {}).get("revision", "") or "")
        if source != "LeanFlow" and (len(revision) != 40 or not revision.isalnum()):
            problems.append(f"{source} is not pinned to a 40-character revision")
    return problems


def t1_fixture_projects() -> tuple[Path, ...]:
    """The frozen T1 regression project inventory (must stay green)."""
    root = REPO_ROOT / "testdata" / "workflow_projects"
    return tuple(
        path for path in (root / "ProveDemo", root / "DocFormalizationDemo") if path.is_dir()
    )


def score_terminal_artifacts(state_root: Path | str) -> dict[str, Any]:
    """Score one run's plan-state artifacts for N1 + kernel-truth compliance.

    Returns a report with ``violations`` (empty = compliant): a missing or
    non-terminal final report, counters diverging from the graph, malformed
    decision packets, or an unreadable journal all fail the run. Corrupted
    JSON raises (WorkflowStateCorruptionError) — that is itself a finding.
    """
    root = Path(state_root)
    blueprint_payload = read_json_file(root / "blueprint.json")
    summary = read_json_file(root / "summary.json")
    bp = Blueprint.from_mapping(blueprint_payload)
    violations: list[str] = []

    final_report = dict(summary.get("final_report") or {})
    status = str(final_report.get("status", "") or "")
    if not final_report:
        violations.append("missing final_report (N1: every scope ends in a concrete result)")
    elif status not in FINAL_REPORT_STATUSES:
        violations.append(f"final_report status {status!r} is not terminal (N1 vocabulary)")

    counters = dict(summary.get("counters") or {})
    graph_counters = status_counters(bp)
    if bp.nodes and not counters:
        violations.append("summary is missing counters for a non-empty graph")
    elif counters and counters != graph_counters:
        violations.append(f"summary counters {counters} diverge from the graph {graph_counters}")

    for packet in summary.get("decision_packets") or []:
        if not isinstance(packet, Mapping) or not str(packet.get("packet_id", "") or ""):
            violations.append("malformed decision packet (missing packet_id)")
            continue
        node_id = str(packet.get("node_id", "") or "")
        if node_id and bp.node_by_id(node_id) is None:
            violations.append(f"decision packet {packet['packet_id']} links unknown node {node_id}")

    journal_events = 0
    journal_path = root / "journal.jsonl"
    if journal_path.is_file():
        for line in journal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                violations.append("journal.jsonl contains an unparseable line")
                break
            journal_events += 1

    return {
        "state_root": str(root),
        "final_report_status": status or "missing",
        "counters": graph_counters,
        "verified_progress": sum(
            1 for node in bp.nodes if node.status in VERIFIED_PROGRESS_STATUSES
        ),
        "decision_packets": len(list(summary.get("decision_packets") or [])),
        "journal_events": journal_events,
        "violations": violations,
        "compliant": not violations,
    }


def _journal_records(root: Path) -> list[dict[str, Any]]:
    """Return parseable lab-notebook records for campaign scoring."""
    path = root / "journal.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            records.append(dict(payload))
    return records


def score_campaign_metrics(state_root: Path | str) -> dict[str, Any]:
    """Score the relentless-prover acceptance metrics for one campaign."""
    root = Path(state_root)
    summary = read_json_file(root / "summary.json")
    blueprint_payload = read_json_file(root / "blueprint.json")
    bp = Blueprint.from_mapping(blueprint_payload)
    campaign = dict(summary.get("campaign") or {})
    stored_metrics = dict(summary.get("campaign_metrics") or {})
    journal = _journal_records(root)
    nudges = [entry for entry in summary.get("manager_nudges") or [] if isinstance(entry, Mapping)]
    ledger = [entry for entry in summary.get("dispatch_ledger") or [] if isinstance(entry, Mapping)]

    rejected_turns = int(stored_metrics.get("rejected_turns", len(nudges)) or 0)
    coach_messages = int(
        stored_metrics.get(
            "coach_messages",
            sum(1 for entry in nudges if bool(entry.get("coach_applied", True))),
        )
        or 0
    )
    coach_coverage = coach_messages / rejected_turns if rejected_turns else 1.0
    routes = sorted(
        {
            str(event.get("route", "") or "")
            for event in journal
            if event.get("event") == "orchestrator-route" and str(event.get("route", "") or "")
        }
    )
    proof_shapes = sorted(
        {
            str(event.get("proof_shape", "") or "")
            for event in journal
            if event.get("event") == "proof-attempt-rejected"
            and str(event.get("proof_shape", "") or "")
        }
    )
    jobs_launched = sum(1 for entry in ledger if str(entry.get("started_at", "") or ""))
    jobs_consumed = sum(1 for entry in ledger if bool(entry.get("consumed", False)))
    jobs_replaced = sum(
        1
        for entry in ledger
        if int(dict(dict(entry.get("spec") or {}).get("inputs") or {}).get("generation", 1) or 1)
        > 1
    )
    exit_code_raw = campaign.get("last_exit_code")
    exit_code = int(exit_code_raw) if exit_code_raw is not None else None
    exit_reason = str(campaign.get("last_exit_reason", "") or "")
    voluntary_give_up = bool(
        exit_code is not None
        and exit_code not in {0, 3, 130}
        and contains_surrender_language(exit_reason)
    )
    unresolved_success = bool(exit_code == 0 and not campaign.get("last_exit_verified", False))

    return {
        "state_root": str(root),
        "campaign_id": str(campaign.get("campaign_id", "") or ""),
        "process_exit_code": exit_code,
        "voluntary_give_up_termination": voluntary_give_up,
        "unresolved_success_exit": unresolved_success,
        "rejected_turns": rejected_turns,
        "coach_messages": coach_messages,
        "coach_coverage": coach_coverage,
        "coach_fallbacks": int(stored_metrics.get("coach_fallbacks", 0) or 0),
        "routes": routes,
        "route_diversity": len(routes),
        "proof_shapes": proof_shapes,
        "proof_shape_diversity": len(proof_shapes),
        "jobs_launched": jobs_launched,
        "jobs_consumed": jobs_consumed,
        "jobs_replaced": jobs_replaced,
        "verified_graph_progress": sum(
            1 for node in bp.nodes if node.status in VERIFIED_PROGRESS_STATUSES
        ),
        "epoch_rollovers": len(list(campaign.get("epoch_history") or [])),
        "acceptance": {
            "voluntary_give_up_rate_zero": not voluntary_give_up,
            "unresolved_success_rate_zero": not unresolved_success,
            "coach_coverage_complete": coach_coverage == 1.0,
        },
    }


def aggregate_campaign_metrics(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate acceptance metrics across a frozen evaluation suite."""
    records = [dict(report) for report in reports]
    run_count = len(records)
    rejected = sum(int(record.get("rejected_turns", 0) or 0) for record in records)
    coached = sum(int(record.get("coach_messages", 0) or 0) for record in records)
    give_ups = sum(bool(record.get("voluntary_give_up_termination", False)) for record in records)
    false_successes = sum(bool(record.get("unresolved_success_exit", False)) for record in records)
    routes = sorted({route for record in records for route in record.get("routes", []) or []})
    shapes = sorted({shape for record in records for shape in record.get("proof_shapes", []) or []})
    return {
        "runs": run_count,
        "voluntary_give_up_termination_rate": give_ups / run_count if run_count else 0.0,
        "unresolved_success_exit_rate": false_successes / run_count if run_count else 0.0,
        "coach_coverage": coached / rejected if rejected else 1.0,
        "route_diversity": len(routes),
        "proof_shape_diversity": len(shapes),
        "routes": routes,
        "proof_shapes": shapes,
        "jobs_launched": sum(int(record.get("jobs_launched", 0) or 0) for record in records),
        "jobs_consumed": sum(int(record.get("jobs_consumed", 0) or 0) for record in records),
        "jobs_replaced": sum(int(record.get("jobs_replaced", 0) or 0) for record in records),
        "verified_graph_progress": sum(
            int(record.get("verified_graph_progress", 0) or 0) for record in records
        ),
        "epoch_rollovers": sum(int(record.get("epoch_rollovers", 0) or 0) for record in records),
    }


def reconcile_drill(bp: Blueprint, truth: Mapping[tuple[str, str], DeclTruth]) -> dict[str, Any]:
    """The P1 resume-drill core: reconcile must lose zero verified work.

    "Verified work" = proved nodes whose declarations are still clean on
    disk; the drill fails if reconcile downgrades any of them, and reports
    (as expected changes) the ones whose declarations genuinely regressed.
    """
    clean_proved = {
        node.id
        for node in bp.nodes
        if node.status == "proved"
        and (decl := truth.get((node.file, node.name))) is not None
        and decl.present
        and not decl.has_sorry
        and not decl.has_error_diag
    }
    reconciled, changes = reconcile(bp, truth)
    # Judge against the AFTER graph directly — a silent downgrade or dropped
    # node must fail even if no change event was emitted; changes only explain.
    lost = [
        node_id
        for node_id in clean_proved
        if (after := reconciled.node_by_id(node_id)) is None or after.status != "proved"
    ]
    return {
        "changes": changes,
        "lost_verified_work": lost,
        "ok": not lost,
        "proved_after": sum(1 for node in reconciled.nodes if node.status == "proved"),
    }


def append_result(record: Mapping[str, Any], path: Path | str = RESULTS_PATH) -> None:
    """Append one suite result to the tracked results log (one JSON per line)."""
    payload = {
        "ts": datetime.now(UTC).replace(microsecond=0).isoformat(),
        **dict(record),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
