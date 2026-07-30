"""Render a complete end-of-scope report for managed workflows.

Every non-verified exit from the autonomous loop must leave a rigorous,
machine-written account: what was proved, what was tried, what was learned,
which jobs are still open (loudly), and the concrete next attack. The
generator is a pure renderer over artifacts the run already produced — the
queue outcomes and attempts, the nudge log, the dispatch ledger, and the
negation probes — so a silent give-up is structurally impossible: the only
non-verified exits are the instrumented returns.

Default ON (``LEANFLOW_FINAL_REPORT`` is opt-out); generation is fail-open —
it can never turn a clean stop into a crash.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.dispatch_models import TERMINAL_STATES
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

OUTCOME_KINDS = ("proved", "disproved", "report")  # the N1 trichotomy


@dataclass(frozen=True)
class ScopeOutcome:
    kind: str
    detail: str


def final_report_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_FINAL_REPORT", "") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def classify_scope_outcome(
    autonomy_state: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
    summary: Mapping[str, Any] | None = None,
) -> ScopeOutcome:
    """proved | disproved | report — the concrete-result trichotomy.

    ``proved`` is the caller's determination (a verified exit skips the
    generator entirely); ``disproved`` requires this run's revalidated,
    ambiguity-free requested-root promotion payload. Raw summary rows and the
    mutable current assignment are never mathematical verdict authority.
    """
    from leanflow_cli.workflows import negation_promotion

    promotion = negation_promotion.authoritative_runtime_main_promotion(
        autonomy_state,
        summary=summary,
        cwd=str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or ""),
    )
    if promotion is not None:
        theorem = str(promotion.get("theorem", "") or "the requested root")
        return ScopeOutcome(
            kind="disproved",
            detail=f"negation of `{theorem}` passed authoritative promotion",
        )
    return ScopeOutcome(kind="report", detail="documented account of the attempt")


def _cell(text: str, limit: int) -> str:
    """One-line, pipe-free prose slice — table/section structure stays intact."""
    collapsed = " ".join(str(text or "").split())
    return collapsed.replace("|", "/")[:limit]


def _theorem_ledger_lines(autonomy_state: Mapping[str, Any]) -> list[str]:
    outcomes = dict(autonomy_state.get("theorem_outcomes") or {})
    attempts = [
        dict(entry)
        for entry in (autonomy_state.get("failed_attempts") or [])
        if isinstance(entry, Mapping)
    ]
    attempt_counts: dict[str, int] = {}
    last_reason: dict[str, str] = {}
    for entry in attempts:
        key = f"{entry.get('active_file', '')}::{entry.get('target_symbol', '')}"
        attempt_counts[key] = attempt_counts.get(key, 0) + 1
        last_reason[key] = str(entry.get("reason", "") or "")
    lines = ["| theorem | status | attempts | last blocker |", "| --- | --- | --- | --- |"]
    seen = set()
    for key, outcome in outcomes.items():
        data = dict(outcome or {})
        symbol = str(data.get("target_symbol", "") or key)
        lines.append(
            f"| `{symbol}` | {data.get('status', '?')} | "
            f"{attempt_counts.get(str(key), 0)} | {_cell(last_reason.get(str(key), ''), 120)} |"
        )
        seen.add(str(key))
    for key, count in attempt_counts.items():
        if key not in seen:
            symbol = key.rpartition("::")[2]
            lines.append(
                f"| `{symbol}` | unresolved | {count} | {_cell(last_reason.get(key, ''), 120)} |"
            )
    if len(lines) == 2:
        lines.append("| [none] | - | - | - |")
    return lines


def _graph_sections() -> tuple[list[str], list[str]]:
    """(graph inventory, ranked open subgoals) from the dependency graph.

    Ranked = frontier-ready first (stated/audited, dependencies proved),
    then waiting-on-dependencies, then blocked/parked with their notes.
    Empty lists when plan-state is off or the graph is empty.
    """
    from leanflow_cli.workflows import plan_state

    if not plan_state.plan_state_enabled():
        return [], []
    try:
        bp = plan_state.load_blueprint()
    except Exception:
        return [], []
    if not bp.nodes:
        return [], []
    inventory: list[str] = []
    for status in ("proved", "false", "parked"):
        names = [f"`{n.name}` ({n.file})" for n in bp.nodes if n.status == status and n.name]
        if names:
            inventory.append(f"- {status}: " + ", ".join(names))
    frontier_ids = {node.id for node in bp.frontier()}
    ranked: list[tuple[int, str]] = []
    for node in bp.nodes:
        if not node.name:
            continue
        if node.id in frontier_ids:
            ranked.append((0, f"- READY `{node.name}` ({node.file})"))
        elif node.status in {"stated", "audited", "conjectured", "proving"}:
            ranked.append((1, f"- waiting `{node.name}` [{node.status}] ({node.file})"))
        elif node.status in {"blocked", "parked"}:
            note = f" — {node.notes}" if node.notes else ""
            ranked.append((2, f"- {node.status} `{node.name}` ({node.file}){note}"))
    ranked.sort(key=lambda pair: pair[0])
    return inventory, [line for _rank, line in ranked[:25]]


def _route_history_lines(limit: int = 20) -> list[str]:
    """Orchestrator route history from journal.jsonl (the lab notebook)."""
    from leanflow_cli.workflows import plan_state

    if not plan_state.plan_state_enabled():
        return []
    try:
        journal = plan_state.plan_state_paths().journal_jsonl
        if not journal.is_file():
            return []
        entries: list[str] = []
        for line in journal.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "orchestrator-route":
                entries.append(
                    f"- {event.get('ts', '?')} [{event.get('trigger', '?')}] "
                    f"{event.get('route', '?')} ({event.get('source', '?')}): "
                    f"{_cell(event.get('reason', ''), 140)}"
                )
        return entries[-limit:]
    except Exception:
        return []


def generate_final_report(
    *,
    stop_reason: str,
    autonomy_state: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
    run_id: str = "",
) -> Path:
    """Render the never-silent end-of-scope artifact and mirror the summary."""
    root = workflow_state_root()
    summary = read_json_file(root / "summary.json")
    outcome = classify_scope_outcome(autonomy_state, live_state, summary)

    nudges = [n for n in (summary.get("manager_nudges") or []) if isinstance(n, Mapping)]
    probes = [p for p in (summary.get("negation_probes") or []) if isinstance(p, Mapping)]
    ledger = [e for e in (summary.get("dispatch_ledger") or []) if isinstance(e, Mapping)]
    open_jobs = [
        entry for entry in ledger if str(entry.get("state", "") or "") not in TERMINAL_STATES
    ]
    packets = [p for p in (summary.get("decision_packets") or []) if isinstance(p, Mapping)]
    outcomes = dict(autonomy_state.get("theorem_outcomes") or {})
    counts = {
        "proved": sum(1 for o in outcomes.values() if dict(o or {}).get("status") == "solved"),
        "blocked": sum(1 for o in outcomes.values() if dict(o or {}).get("status") == "blocked"),
        "unresolved": max(0, len(outcomes))
        - sum(1 for o in outcomes.values() if dict(o or {}).get("status") in {"solved", "blocked"}),
    }

    lines = [
        f"# Final Report — {run_id or 'run'}",
        "",
        f"- stop reason: **{stop_reason}**",
        f"- outcome: **{outcome.kind}** — {outcome.detail}",
        f"- generated: {_now_iso()}",
        "",
        "## Theorem ledger",
        "",
        *_theorem_ledger_lines(autonomy_state),
        "",
        "## What was tried",
        "",
    ]
    attempts = [
        dict(entry)
        for entry in (autonomy_state.get("failed_attempts") or [])
        if isinstance(entry, Mapping)
    ][-10:]
    lines.extend(
        f"- attempt {entry.get('attempt', '?')} on `{entry.get('target_symbol', '?')}`: "
        f"{_cell(entry.get('reason', ''), 160)}"
        for entry in attempts
    )
    if not attempts:
        lines.append("- [no failed attempts recorded]")
    lines.extend(["", "## What was learned", ""])
    learned = False
    for nudge in nudges[-5:]:
        payload = dict(nudge.get("nudge") or {})
        if payload.get("rationale"):
            lines.append(f"- manager: {_cell(payload['rationale'], 160)}")
            learned = True
    for probe in probes[-5:]:
        plausible = dict(probe.get("plausible") or {})
        if plausible.get("counterexample_text"):
            lines.append(
                f"- counterexample for `{probe.get('theorem', '?')}`: "
                f"{_cell(plausible['counterexample_text'], 160)}"
            )
            learned = True
        negation = dict(probe.get("negation") or {})
        if negation.get("verdict"):
            lines.append(f"- negation probe `{probe.get('theorem', '?')}`: {negation['verdict']}")
            learned = True
    if not learned:
        lines.append("- [no grounding findings recorded]")
    lines.extend(["", "## Open jobs", ""])
    if open_jobs:
        # LOUD: a job left non-terminal is an audit finding, never silence.
        lines.extend(
            f"- **OPEN** {dict(job.get('spec') or {}).get('job_id', '?')} "
            f"[{job.get('state', '?')}] — {_cell(job.get('notes', ''), 120)}"
            for job in open_jobs
        )
    else:
        lines.append("- none — every dispatched job reached a terminal state")
    # Include graph inventory, route history, and the ranked open frontier.
    graph_lines, subgoal_lines = _graph_sections()
    if graph_lines:
        lines.extend(["", "## Graph inventory", "", *graph_lines])
    route_lines = _route_history_lines()
    if route_lines:
        lines.extend(["", "## Route history", "", *route_lines])
    if subgoal_lines:
        lines.extend(["", "## Open subgoals (ranked)", "", *subgoal_lines])
    lines.extend(["", "## Recommended next actions", ""])
    recommendations = []
    if counts["blocked"]:
        recommendations.append(
            f"- {counts['blocked']} blocked theorem(s): split into helper lemmas or run a "
            "feasibility probe (`negate`)."
        )
    if outcome.kind == "disproved":
        recommendations.append(
            "- promote the scratch disproof through the authoritative gate, then re-state "
            "or retire the affected statement."
        )
    if stop_reason == "budget-breakpoint":
        recommendations.append(
            "- decide the open packet: split / plan / negate / park / re-state "
            "(raise LEANFLOW_THEOREM_BUDGET_STEPS to keep grinding)."
        )
    if packets and not any(p.get("decision") for p in packets):
        recommendations.append(f"- {len(packets)} undecided decision packet(s) await a route.")
    if not recommendations:
        recommendations.append("- resume from the plan artifacts and continue the frontier.")
    lines.extend(recommendations)
    lines.append("")

    report_path = root / f"final-report-{run_id or 'run'}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")

    status = {"proved": "proved", "disproved": "disproved"}.get(outcome.kind, "documented")

    def mutate(payload: dict[str, Any]) -> None:
        payload["final_report"] = {
            "run_id": run_id,
            "stop_reason": stop_reason,
            "outcome_kind": outcome.kind,
            "status": status,
            "path": str(report_path),
            "generated_at": _now_iso(),
            "theorem_counts": counts,
            "open_jobs": [
                dict(dict(job.get("spec") or {}), state=job.get("state", "")) for job in open_jobs
            ],
        }

    update_json_file(root / "summary.json", mutate)
    return report_path
