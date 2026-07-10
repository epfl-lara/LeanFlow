"""Planner phase — research fan-out + synthesis + graph merge (Phase 5 §5.5).

The ``plan`` orchestrator route's mechanical arm (dark behind
``LEANFLOW_PLANNER_ENABLED``): fan out up to three research sub-agents
(web/literature, mathlib, empirical) via ``delegate_task`` with isolated
budgets, synthesize their JSON deliverables into a plan with one
``planner_synthesis`` model turn, merge the result through
``plan_state.apply_delta`` (the planner's only door into the graph), and
state validated stubs through ``decomposer.place_helpers`` — every guard
(stub shape, forbidden axioms, in-place validation, all-or-nothing revert)
applies to planner stubs exactly as to decomposer stubs.

N1: no lane result is ever lost — every lane lands in the outcome payload
and the journal, parse failures included. Kernel truth: nothing here can
mark a node proved/false; apply_delta derives statuses and the queue gate
is untouched. Premise retrieval intentionally has no wiring here: it rides
the Phase 1 assignment-time mechanism (``LEANFLOW_PREMISE_RETRIEVAL``).
Queue pickup is the runner's loop-bottom rescan: placed stubs precede the
target in file order and carry sorries, so they become the next
assignments without a separate seeding path.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from leanflow_cli.workflows import decomposer, plan_state
from leanflow_cli.workflows.verification_providers import run_model_verification_review
from tools.implementations.delegate_tool import delegate_task

logger = logging.getLogger(__name__)

PLANNER_SYNTHESIS_TASK = "planner_synthesis"

#: Per-lane child budget (delegate max_iterations) — bounded research, not a prover run.
LANE_MAX_ITERATIONS = 24

_SYNTH_SYSTEM_PROMPT = (
    "You are the planning synthesizer of an autonomous Lean 4 proving harness. "
    "The Lean kernel gate is the sole authority on truth; your plan is advisory "
    "strategy. Turn the research deliverables into (a) grounding facts worth "
    "remembering, (b) a strategy, and (c) concrete graph nodes: helper lemmas "
    "with COMPLETE sorry-bodied Lean statements when you are confident of the "
    "formal statement, name-only conjectures otherwise. Never claim anything "
    "is proved or false. Answer with ONE JSON object only."
)


def planner_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_PLANNER_ENABLED", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def planner_max_subagents() -> int:
    try:
        value = int(os.getenv("LEANFLOW_PLANNER_MAX_SUBAGENTS", "3") or "3")
    except ValueError:
        value = 3
    return max(1, min(3, value))  # delegate_task's concurrent-batch cap is 3


@dataclass(frozen=True)
class PlannerOutcome:
    ok: bool
    reason: str = ""
    lanes: tuple[dict[str, Any], ...] = ()
    nodes_added: int = 0
    stubs_placed: tuple[str, ...] = ()
    grounding_count: int = 0
    strategy_count: int = 0
    synthesis_status: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "lanes": [dict(lane) for lane in self.lanes],
            "nodes_added": self.nodes_added,
            "stubs_placed": list(self.stubs_placed),
            "grounding_count": self.grounding_count,
            "strategy_count": self.strategy_count,
            "synthesis_status": self.synthesis_status,
        }


@dataclass(frozen=True)
class _Lane:
    key: str
    toolsets: tuple[str, ...]
    goal_template: str
    deliverable_hint: str


_LANES: tuple[_Lane, ...] = (
    _Lane(
        key="web",
        toolsets=("web",),
        goal_template=(
            "Research the mathematical literature and the web for prior art, "
            "known results, and proof strategies relevant to this Lean 4 goal: "
            "{goal}. Clone promising proof developments with repo_clone when "
            "concrete."
        ),
        deliverable_hint=(
            '{"findings": [{"claim": "...", "source": "url or path", '
            '"relevance": "...", "candidate_lemmas": ["..."]}], '
            '"providers_tried": ["web_search", "unavailable:lean_search"], '
            '"exhausted": false}'
        ),
    ),
    _Lane(
        key="mathlib",
        toolsets=("lean",),
        goal_template=(
            "Search mathlib and the local Lean project for lemmas, definitions "
            "and instances that could discharge or decompose this goal: {goal}. "
            "Use lean_search / lean_lemma_suggest / lean_proof_context."
        ),
        deliverable_hint=(
            '{"findings": [{"claim": "what the lemma gives you", '
            '"source": "Mathlib.Module.Path", "relevance": "...", '
            '"candidate_lemmas": ["Fully.Qualified.Name"]}], '
            '"providers_tried": ["lean_search:local", "lean_search:semantic"], '
            '"exhausted": false}'
        ),
    ),
    _Lane(
        key="empirical",
        toolsets=("terminal", "lean"),
        goal_template=(
            "Empirically probe this Lean 4 goal before anyone spends prover "
            "budget on it: {goal}. Test small cases numerically (python via "
            "terminal) and/or with lean_multi_attempt; look for counterexamples "
            "and for the pattern a proof would need."
        ),
        deliverable_hint=(
            '{"hypothesis": "...", "method": "...", '
            '"result": "supports|refutes|inconclusive", "evidence": "...", '
            '"counterexample": null}'
        ),
    ),
)

#: Probe-archetype / synonym -> lane key (orchestrator-LLM probes select lanes).
_LANE_ALIASES = {
    "web": "web",
    "literature": "web",
    "deep-search": "web",
    "deep_search": "web",
    "mathlib": "mathlib",
    "empirical": "empirical",
}

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Fence-tolerant dict extraction (orchestrator_llm parser pattern)."""
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [match.group(1) for match in _JSON_FENCE_RE.finditer(raw)]
    if not candidates:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            candidates = [raw[start : end + 1]]
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _phase_fragment(spec_id: str, *, include_schema: bool = True) -> str:
    """Phase-fragment text via the shared spec helper; fail-open ''."""
    try:
        from leanflow_cli.lean.lean_workflow_specs import phase_fragment_text

        return phase_fragment_text(spec_id, include_schema=include_schema)
    except Exception:
        logger.debug("phase fragment %s unavailable", spec_id, exc_info=True)
        return ""


def _lane_prompt(lane: _Lane, goal: str) -> str:
    parts = [
        lane.goal_template.format(goal=goal),
        "",
        "Your final response must be ONLY one JSON object shaped like:",
        lane.deliverable_hint,
        "No prose around it. Findings you cannot support, omit.",
    ]
    # The empirical lane hunts plausibility evidence only — the kernel
    # negation probe (phase-negation) is the orchestrator's business.
    if lane.key in {"web", "mathlib"}:
        fragment = _phase_fragment("phase-search")
        if fragment:
            parts += ["", fragment]
    return "\n".join(parts)


def _run_lanes(
    goal: str, lanes: Sequence[_Lane], *, agent: Any
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Fan out one delegate batch; (lane records, parsed deliverables by key).

    Every lane produces a record whatever happens — completed, failed,
    parse-failure — so the outcome payload and journal never lose a lane.
    """
    tasks = [{"goal": _lane_prompt(lane, goal), "toolsets": list(lane.toolsets)} for lane in lanes]
    records: list[dict[str, Any]] = []
    deliverables: dict[str, dict[str, Any]] = {}
    try:
        raw = delegate_task(
            tasks=tasks,
            parent_agent=agent,
            max_iterations=LANE_MAX_ITERATIONS,
            isolate_budget=True,  # research lanes never drain the prover budget
        )
        payload = json.loads(raw)
        payload = payload if isinstance(payload, Mapping) else {}
        results = {
            int(entry.get("task_index", -1)): entry
            for entry in (payload.get("results") or [])
            if isinstance(entry, Mapping)
        }
        if not results and payload.get("error"):
            # delegate_task reports its own guard failures (no parent agent,
            # depth limit, ...) as a top-level error object, not an exception.
            raise RuntimeError(str(payload["error"]))
    except Exception as exc:
        logger.debug("planner lane fan-out failed", exc_info=True)
        return (
            [
                {"lane": lane.key, "status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
                for lane in lanes
            ],
            {},
        )
    for index, lane in enumerate(lanes):
        entry = dict(results.get(index) or {})
        status = str(entry.get("status", "") or "missing")
        summary = str(entry.get("summary", "") or "")
        record: dict[str, Any] = {"lane": lane.key, "status": status}
        if entry.get("error"):
            record["error"] = str(entry["error"])[:300]
        parsed = _extract_json_object(summary) if status == "completed" else None
        if parsed is None:
            if status == "completed":
                record["status"] = "parse-failure"
                record["summary_head"] = summary[:200]
        else:
            record["deliverable_keys"] = sorted(parsed.keys())
            deliverables[lane.key] = parsed
        records.append(record)
    return records, deliverables


def _synthesis_prompt(
    goal: str,
    deliverables: Mapping[str, Mapping[str, Any]],
    *,
    target_symbol: str,
    active_file: str,
    bp: plan_state.Blueprint,
) -> str:
    nodes_digest = [
        f"- `{node.name}` [{node.status}] ({node.file})" for node in bp.nodes[:30] if node.name
    ]
    lines = [
        f"Goal: {goal}",
        f"Target: `{target_symbol}` in {active_file}" if target_symbol else "Target: [none yet]",
        "",
        "Current graph:",
        *(nodes_digest or ["- [empty]"]),
        "",
        "Research deliverables:",
        json.dumps(dict(deliverables), ensure_ascii=False, sort_keys=True)[:12000],
        "",
        "Reply with ONE JSON object:",
        '{"grounding": ["fact worth remembering", ...],',
        ' "strategy": ["ordered strategy step", ...],',
        ' "nodes": [{"name": "...", "file": "' + (active_file or "Project/File.lean") + '",',
        '   "statement": "lemma name ... := by sorry" or omit when not yet formal,',
        '   "depends_on": ["otherNodeName"], "split_of": "parentName", "notes": "..."}]}',
        "Rules: at most 8 nodes; statements must be COMPLETE sorry-bodied",
        "lemma/theorem declarations; never restate the target as a helper;",
        "never claim proved/false status for anything.",
    ]
    # phase-planning's schema IS this reply's contract (grounding/strategy/
    # nodes); phase-draft rides as POLICY only — its stubs deliverable binds
    # drafting actors, and a second schema here would compete with the
    # nodes JSON above.
    planning = _phase_fragment("phase-planning")
    if planning:
        lines += ["", planning]
    draft = _phase_fragment("phase-draft", include_schema=False)
    if draft:
        lines += [
            "",
            draft,
            "",
            "The draft-phase spec above is POLICY for the statements you",
            "emit; your reply contract is ONLY the nodes JSON above.",
        ]
    return "\n".join(lines)


def _validated_nodes(nodes: Sequence[Any], *, active_file: str) -> list[dict[str, Any]]:
    """Draft-phase validation BEFORE the graph merge.

    A statement that fails the stub-shape guard is stripped (the node
    enters as a conjecture — the idea survives, N1 — but never becomes a
    frontier-eligible ``stated`` node for a declaration that can never be
    placed). The same deferral applies to statements aimed at any file
    OTHER than the active one: this phase only places into the active
    file (sibling files belong to multi-direction), so an unplaceable
    statement must not mint a stated node. The parsed declaration name is
    the name of record: a mismatched claim drops the node whole (it must
    reach neither the graph nor placement); a node without a claimed name
    adopts the parsed one so placed stubs are always graph-tracked. All
    rejections are journaled.
    """
    clean: list[dict[str, Any]] = []
    for entry in nodes:
        if not isinstance(entry, Mapping):
            continue
        node = dict(entry)
        statement = decomposer.normalize_statement(str(node.get("statement", "") or ""))
        if statement and str(node.get("file", "") or "").strip() != (active_file or ""):
            # Adopt the parsed name first — a nameless sibling stub must
            # still survive as a NAMED conjecture (the graph door drops
            # nameless nodes).
            if not str(node.get("name", "") or "").strip():
                node["name"] = decomposer._helper_name(statement) or ""
            plan_state.append_journal_event(
                {
                    "event": "planner-stub-deferred",
                    "name": str(node.get("name", "") or ""),
                    "file": str(node.get("file", "") or ""),
                }
            )
            node["statement"] = ""  # conjecture: nothing places sibling files here
            clean.append(node)
            continue
        if statement:
            if not decomposer.stub_shape_ok(statement):
                plan_state.append_journal_event(
                    {
                        "event": "planner-stub-shape-rejected",
                        "name": str(node.get("name", "") or ""),
                    }
                )
                node["statement"] = ""  # conjecture, not a phantom stated node
                clean.append(node)
                continue
            parsed = decomposer._helper_name(statement)
            claimed = str(node.get("name", "") or "").strip()
            if parsed:
                if claimed and claimed != parsed:
                    plan_state.append_journal_event(
                        {
                            "event": "planner-stub-name-mismatch",
                            "claimed": claimed,
                            "parsed": parsed,
                        }
                    )
                    continue
                node["name"] = parsed
        clean.append(node)
    return clean


def run_planner_phase(
    *,
    goal: str = "",
    target_symbol: str = "",
    active_file: str = "",
    agent: Any = None,
    cwd: str = "",
    allowed_axioms: Sequence[str] = ("propext", "Classical.choice", "Quot.sound"),
    lane_keys: Sequence[str] = (),
) -> PlannerOutcome:
    """One full planner phase; never raises.

    ``lane_keys`` narrows the research lanes (the orchestrator LLM's probes
    list selects them in Phase 6); default = every lane up to the
    ``LEANFLOW_PLANNER_MAX_SUBAGENTS`` cap.
    """
    lane_records: list[dict[str, Any]] = []
    try:
        bp = plan_state.load_blueprint()
        goal = " ".join(str(goal or bp.goal or "").split())
        if not goal:
            return PlannerOutcome(ok=False, reason="no goal available to plan against")
        if agent is None:
            return PlannerOutcome(ok=False, reason="no parent agent for the research fan-out")

        wanted = {
            _LANE_ALIASES[normalized]
            for key in lane_keys
            if (normalized := str(key or "").strip().lower()) in _LANE_ALIASES
        }
        # A selection that names no research lane (e.g. probes=[negation],
        # which is the negate route's business) falls back to the full wave.
        lanes = [lane for lane in _LANES if not wanted or lane.key in wanted]
        lanes = lanes[: planner_max_subagents()]

        lane_records, deliverables = _run_lanes(goal, lanes, agent=agent)
        plan_state.append_journal_event(
            {"event": "planner-lanes", "goal": goal[:200], "lanes": lane_records}
        )

        result = run_model_verification_review(
            provider="auto",
            task=PLANNER_SYNTHESIS_TASK,
            prompt=_synthesis_prompt(
                goal,
                deliverables,
                target_symbol=target_symbol,
                active_file=active_file,
                bp=bp,
            ),
            system_prompt=_SYNTH_SYSTEM_PROMPT,
            timeout_s=900,
            max_tokens=8000,
        )
        status = str(getattr(result, "status", "") or "").strip().lower()
        if status and status != "ok":
            return PlannerOutcome(
                ok=False,
                reason=f"synthesizer unavailable ({status})",
                lanes=tuple(lane_records),
                synthesis_status=status,
            )
        synthesis = _extract_json_object(str(getattr(result, "response", "") or ""))
        if synthesis is None:
            return PlannerOutcome(
                ok=False,
                reason="synthesizer reply was not parseable JSON",
                lanes=tuple(lane_records),
                synthesis_status="parse-failure",
            )

        grounding = [str(item) for item in (synthesis.get("grounding") or []) if str(item).strip()]
        strategy = [str(item) for item in (synthesis.get("strategy") or []) if str(item).strip()]
        # Tolerate the draft-phase field name: a compliant `stubs` reply is
        # the same payload under the fragment's key.
        raw_nodes = synthesis.get("nodes") or synthesis.get("stubs") or []
        nodes = _validated_nodes(raw_nodes, active_file=active_file)
        delta = {"goal": goal, "nodes": nodes}
        # Journal AFTER the save succeeds: the notebook must describe the
        # graph that was actually persisted, not a conflicted first attempt.
        merged, changes = plan_state.apply_delta(bp, delta, generated_by="planner", journal=False)
        try:
            plan_state.save_blueprint(merged)
        except plan_state.PlanStateRevisionConflict:
            # Single retry against the fresh disk state (another writer won).
            merged, changes = plan_state.apply_delta(
                plan_state.load_blueprint(), delta, generated_by="planner", journal=False
            )
            plan_state.save_blueprint(merged)
        plan_state.journal_delta_changes(changes, generated_by="planner")
        created_node_ids = frozenset(
            str(change.get("node_id", "") or "")
            for change in changes
            if change.get("event") == "node-created"
        )
        summary = plan_state.merge_planner_findings(
            plan_state.load_summary(), grounding=grounding, strategy=strategy
        )
        plan_state.save_summary(summary)

        stubs_placed: tuple[str, ...] = ()
        if target_symbol and active_file:
            stubs_placed = _place_planner_stubs(
                nodes,
                target_symbol=target_symbol,
                active_file=active_file,
                allowed_axioms=allowed_axioms,
                cwd=cwd,
                agent=agent,
            )
        # A stated node whose stub did NOT land on disk (placement failed,
        # or fell past the per-batch cap) must not stay frontier-eligible:
        # demote it back to a conjecture, journaled after the save.
        if not _demote_unplaced_stubs(
            nodes,
            placed=stubs_placed,
            active_file=active_file,
            created_node_ids=created_node_ids,
        ):
            # The graph may hold stated nodes with no declaration on disk
            # and we could not fix it — fail LOUDLY (N1), do not render a
            # frontier that lies.
            return PlannerOutcome(
                ok=False,
                reason="unplaced-stub demotion failed; graph may be ahead of disk",
                lanes=tuple(lane_records),
                stubs_placed=stubs_placed,
            )
        # Render plan.md LAST: routing must never consume a frontier view
        # that still lists stubs which failed placement.
        plan_state.save_plan_md(plan_state.load_blueprint(), plan_state.load_summary())

        nodes_added = sum(1 for change in changes if change.get("event") == "node-created")
        return PlannerOutcome(
            ok=True,
            reason="planner phase completed",
            lanes=tuple(lane_records),
            nodes_added=nodes_added,
            stubs_placed=stubs_placed,
            grounding_count=len(grounding),
            strategy_count=len(strategy),
            synthesis_status="ok",
        )
    except Exception as exc:
        logger.debug("planner phase failed", exc_info=True)
        # N1: lane work done before the failure stays in the outcome payload.
        return PlannerOutcome(
            ok=False, reason=f"{type(exc).__name__}: {exc}", lanes=tuple(lane_records)
        )


def _demote_unplaced_stubs(
    nodes: Sequence[Mapping[str, Any]],
    *,
    placed: tuple[str, ...],
    active_file: str,
    created_node_ids: frozenset[str],
) -> bool:
    """stated => conjectured for active-file stubs that never reached disk.

    Restricted to nodes CREATED by this run's merge: a re-stated duplicate
    of a declaration that already lives on disk must never be demoted just
    because its (redundant) placement was rejected. Journal-after-save
    discipline; never raises. Nodes for other files were already deferred
    to conjectures before the merge.
    """
    if not plan_state.plan_state_enabled() or not active_file:
        return True
    placed_set = set(placed)
    unplaced = [
        str(node.get("name", "") or "")
        for node in nodes
        if str(node.get("statement", "") or "").strip()
        and str(node.get("file", "") or "").strip() == active_file
        and str(node.get("name", "") or "") not in placed_set
        and plan_state.node_id_for(str(node.get("name", "") or ""), active_file) in created_node_ids
    ]
    if not unplaced:
        return True
    why = "planner stub not placed (placement failed or over the batch cap)"

    def _apply(bp: Any) -> tuple[Any, list[dict[str, str]]]:
        events: list[dict[str, str]] = []
        for name in unplaced:
            node = bp.node_by_id(plan_state.node_id_for(name, active_file))
            if node is None or node.status != "stated":
                continue
            events.append({"node_id": node.id, "name": node.name})
            bp = plan_state.set_node_status(bp, node.id, "conjectured", why=why, journal=False)
        return bp, events

    try:
        bp, events = _apply(plan_state.load_blueprint())
        if not events:
            return True
        try:
            plan_state.save_blueprint(bp)
        except plan_state.PlanStateRevisionConflict:
            bp, events = _apply(plan_state.load_blueprint())
            if not events:
                return True
            plan_state.save_blueprint(bp)
        for event in events:
            plan_state.journal_node_status(
                node_id=event["node_id"],
                name=event["name"],
                from_status="stated",
                to_status="conjectured",
                via_gate=False,
                why=why,
            )
        return True
    except Exception:
        logger.debug("unplaced-stub demotion failed", exc_info=True)
        return False


def _place_planner_stubs(
    nodes: Sequence[Any],
    *,
    target_symbol: str,
    active_file: str,
    allowed_axioms: Sequence[str],
    cwd: str,
    agent: Any,
) -> tuple[str, ...]:
    """State the target-file stubs through the decomposer's guarded door."""
    skeletons: list[str] = []
    for entry in nodes:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("file", "") or "").strip() != active_file:
            continue
        skeleton = decomposer.normalize_statement(str(entry.get("statement", "") or ""))
        if not skeleton or not decomposer.stub_shape_ok(skeleton):
            continue
        # Name binding already ran in _validated_nodes (before the graph
        # merge) — every skeleton here is graph-tracked under its parsed name.
        skeletons.append(skeleton)
    if not skeletons:
        return ()
    outcome = decomposer.place_helpers(
        active_file=active_file,
        target_symbol=target_symbol,
        skeletons=skeletons[:4],
        allowed_axioms=allowed_axioms,
        cwd=cwd,
    )
    if not outcome.ok:
        plan_state.append_journal_event(
            {"event": "planner-stubs-rejected", "reason": outcome.reason}
        )
        return ()
    decomposer.refresh_queue_edit_guard(agent)
    return outcome.placed
