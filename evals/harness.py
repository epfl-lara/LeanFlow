"""Phase E scorer over the /prove redesign's plan-state artifacts.

Scores what the runs already persist — the dependency graph, summary,
journal, decision packets — against the concrete-result guarantee (N1) and
the kernel-truth invariants. Model-free by design: capability suites (T2/T3)
reuse these scoring primitives over their run outputs; this module never
launches runs itself. See evals/README.md for the suite/gate map.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

#: Graph statuses that count as verified progress (T3 metric).
VERIFIED_PROGRESS_STATUSES = frozenset({"proved"})


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
