"""Run research fan-out, synthesis, and graph updates for the planner route.

The ``plan`` orchestrator route's mechanical arm (opt-in through
``LEANFLOW_PLANNER_ENABLED``): run up to three research sub-agents
(web/literature, mathlib, empirical) via capacity-bounded ``delegate_task``
waves with isolated budgets, synthesize their JSON deliverables into a plan with one
``planner_synthesis`` model turn, merge the result through
``plan_state.apply_delta`` (the planner's only door into the graph), and
state validated stubs through ``decomposer.place_helpers`` — every guard
(stub shape, forbidden axioms, in-place validation, all-or-nothing revert)
applies to planner stubs exactly as to decomposer stubs.

Every lane result lands in the outcome payload
and the journal, parse failures included. Kernel truth: nothing here can
mark a node proved/false; apply_delta derives statuses and the queue gate
is untouched. Premise retrieval intentionally has no wiring here: it rides
the assignment-time mechanism (``LEANFLOW_PREMISE_RETRIEVAL``).
Queue pickup is the runner's loop-bottom rescan: placed stubs precede the
target in file order and carry sorries, so they become the next
assignments without a separate seeding path.

Research-mode planner actors share ``--research-workers N`` capacity with
process jobs. A saturated lane returns ``capacity-deferred`` after a short
bounded wait. Once its wave joins, the planner retries only deferred lanes
once; a second miss is journaled and retried by the runner at the next safe
orchestration boundary rather than blocking the foreground for a whole job.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from leanflow_cli.workflows import (
    decomposer,
    empirical_pilot,
    orchestrator_arithmetic_preflight,
    plan_state,
    planner_candidate_admission,
    planner_evidence,
    research_mode,
)
from leanflow_cli.workflows.verification_providers import run_model_verification_review
from tools.implementations.delegate_tool import delegate_task
from tools.utilities.interrupt import CooperativeInterrupt, raise_if_interrupted
from tools.utilities.repository_research_policy import (
    clean_room_task_labels,
    repository_research_disabled,
    solution_research_disabled,
)

logger = logging.getLogger(__name__)

PLANNER_SYNTHESIS_TASK = "planner_synthesis"
PLANNER_SYNTHESIS_TIMEOUT_S = 300
PLANNER_ARITHMETIC_REJECTION_STATUS = "arithmetic-preflight-rejected"
PLANNER_EVIDENCE_INTERRUPTED_STATUS = "evidence-interrupted"

#: Per-lane child budget (delegate max_iterations) — bounded research, not a prover run.
LANE_MAX_ITERATIONS = 24
PLANNER_CAPACITY_WAIT_DEFAULT_S = 1.0

_EQUALITY_MARKER_RE = re.compile(r"(?<![:<>!=%])=(?!=)")
_ASSERTION_CUE_RE = re.compile(
    r"\b(?:then|therefore|hence|identity|claim|prove|proves|derive|use)\b",
    re.IGNORECASE,
)
_NON_ASSERTION_PROSE_RE = re.compile(
    r"\b(?:test(?:ed|ing)?|examples?|observed|respectively|inventory|json|payload|"
    r"if|when|from|assuming|under|provided|hypothesis)\b",
    re.IGNORECASE,
)

# Full structured lane payloads live in the journal/outcome.  The summary is
# prompt-facing, so keep each deterministic fallback line compact while
# retaining the authoritative payload elsewhere.
_UNSYNTHESIZED_GROUNDING_MAX_CHARS = 3000

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


def planner_synthesis_timeout_s() -> int:
    """Return the bounded planner synthesis deadline.

    Planner lane evidence is durable before synthesis starts. A silent
    control-plane call must fall back promptly instead of freezing the active
    proof for fifteen minutes.
    """
    try:
        requested = int(
            str(
                os.getenv(
                    "LEANFLOW_PLANNER_SYNTHESIS_TIMEOUT_S",
                    PLANNER_SYNTHESIS_TIMEOUT_S,
                )
                or PLANNER_SYNTHESIS_TIMEOUT_S
            )
        )
    except (TypeError, ValueError):
        requested = PLANNER_SYNTHESIS_TIMEOUT_S
    return max(30, min(requested, 600))


def planner_max_subagents() -> int:
    try:
        value = int(os.getenv("LEANFLOW_PLANNER_MAX_SUBAGENTS", "3") or "3")
    except ValueError:
        value = 3
    return max(1, min(3, value))  # delegate_task's concurrent-batch cap is 3


def planner_capacity_wait_s() -> float:
    """Return the short wait before a busy planner lane is deferred."""
    try:
        value = float(
            os.getenv(
                "LEANFLOW_PLANNER_CAPACITY_WAIT_S",
                str(PLANNER_CAPACITY_WAIT_DEFAULT_S),
            )
            or PLANNER_CAPACITY_WAIT_DEFAULT_S
        )
    except ValueError:
        value = PLANNER_CAPACITY_WAIT_DEFAULT_S
    return max(0.0, min(10.0, value))


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
            "{goal}. Start with a deep web_search portfolio containing materially "
            "different formulations. Treat snippets only as discovery: inspect "
            "promising primary sources with web_fetch, and clone promising proof "
            "developments with repo_clone when concrete. If a provider fails or a "
            "query is empty, continue through surviving providers and reformulations. "
            "Record rejected sources and dead branches; set exhausted=true only after "
            "the query portfolio and source reads are genuinely exhausted."
        ),
        deliverable_hint=(
            '{"findings": [{"claim": "...", "source": "url or path", '
            '"relevance": "...", "candidate_lemmas": ["..."]}], '
            '"queries_tried": ["..."], "providers_tried": ["..."], '
            '"sources_read": ["url or path"], '
            '"dead_ends": [{"route": "...", "reason": "..."}], "exhausted": false}'
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
            "and for the pattern a proof would need. " + empirical_pilot.prompt_contract()
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


def _bounded_text(value: str, limit: int) -> str:
    """Return a prompt-safe bounded string without changing its semantics."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 18)] + "\n...[truncated]"


def _assignment_scope_block(
    *,
    campaign_goal: str,
    target_symbol: str,
    active_file: str,
    declaration_slice: str,
    lean_goal: str,
    requested_route: str,
    failed_route_signature: str,
    search_signature: str,
) -> str:
    """Render the exact queue assignment shared by every planner actor."""
    lines = [
        "[LEANFLOW EXACT PLANNER ASSIGNMENT]",
        f"Active file: {active_file or '[unspecified]'}",
        f"Target symbol: {target_symbol or '[unspecified]'}",
        f"Requested route: {requested_route or 'plan'}",
        "Do not broaden to the whole file or another unresolved declaration. "
        "Follow dependencies only when they directly support this exact target.",
        "`/prove` in the campaign context is a workflow command, not a filesystem path. "
        "Use Active file above exactly for every file-scoped tool call.",
    ]
    if campaign_goal:
        lines += [
            "",
            "Campaign context (not the research scope):",
            _bounded_text(campaign_goal, 2000),
        ]
    if declaration_slice:
        lines += ["", "Exact declaration slice:", _bounded_text(declaration_slice, 8000)]
    if lean_goal:
        lines += ["", "Current Lean goal / proof state:", _bounded_text(lean_goal, 4000)]
    if failed_route_signature:
        lines += ["", "Prior failed route signature:", _bounded_text(failed_route_signature, 3000)]
    if search_signature:
        lines += ["", "Prior search signature:", _bounded_text(search_signature, 3000)]
    return "\n".join(lines)


def _lane_prompt(
    lane: _Lane,
    goal: str,
    *,
    target_symbol: str = "",
    active_file: str = "",
    declaration_slice: str = "",
    lean_goal: str = "",
    requested_route: str = "",
    failed_route_signature: str = "",
    search_signature: str = "",
) -> str:
    subject = (
        f"`{target_symbol}` in `{active_file}`"
        if target_symbol and active_file
        else target_symbol or goal
    )
    lane_goal = lane.goal_template.format(goal=subject)
    if lane.key == "web" and (repository_research_disabled() or solution_research_disabled()):
        restrictions: list[str] = []
        if repository_research_disabled():
            restrictions.append("do not search, fetch, clone, or cite source-code repositories")
        if solution_research_disabled():
            labels = ", ".join(clean_room_task_labels()) or "[active task]"
            restrictions.append(
                "do not search for, fetch, cite, or use any existing or official solution "
                f"to the active task, and never put these labels into a web query: {labels}"
            )
        lane_goal = (
            "Research only general mathematical literature and non-prohibited web sources "
            f"for proof strategies relevant to this Lean 4 goal: {subject}. "
            f"This is a clean-room run: {'; '.join(restrictions)}. "
            "Record only independently useful mathematical guidance."
        )
    parts = [
        lane_goal,
        "",
        _assignment_scope_block(
            campaign_goal=goal,
            target_symbol=target_symbol,
            active_file=active_file,
            declaration_slice=declaration_slice,
            lean_goal=lean_goal,
            requested_route=requested_route,
            failed_route_signature=failed_route_signature,
            search_signature=search_signature,
        ),
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
    goal: str,
    lanes: Sequence[_Lane],
    *,
    agent: Any,
    target_symbol: str = "",
    active_file: str = "",
    declaration_slice: str = "",
    lean_goal: str = "",
    requested_route: str = "",
    failed_route_signature: str = "",
    search_signature: str = "",
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Run capacity-bounded lane waves and return records plus deliverables.

    Every lane produces a record whatever happens — completed, failed,
    parse-failure, or capacity-deferred — so the outcome payload and journal
    never lose a lane. A deferred lane receives one same-wave retry after its
    siblings release their leases.
    """
    tasks: list[dict[str, Any]] = []
    for lane in lanes:
        task: dict[str, Any] = {
            "goal": _lane_prompt(
                lane,
                goal,
                target_symbol=target_symbol,
                active_file=active_file,
                declaration_slice=declaration_slice,
                lean_goal=lean_goal,
                requested_route=requested_route,
                failed_route_signature=failed_route_signature,
                search_signature=search_signature,
            ),
            "toolsets": list(lane.toolsets),
            # Planner routing runs on the foreground control thread. If all
            # actor slots are occupied by long process jobs, return a durable
            # capacity-deferred record instead of freezing that control loop.
            "_background_capacity_timeout_s": planner_capacity_wait_s(),
        }
        if lane.key == "empirical":
            # Internal delegate hook: this callback is installed on only the
            # empirical child and is never part of the public tool schema.
            task["_pre_tool_call_callback"] = empirical_pilot.BoundedTerminalPilot()
        tasks.append(task)
    records: list[dict[str, Any]] = []
    deliverables: dict[str, dict[str, Any]] = {}

    def run_wave(
        wave_tasks: Sequence[dict[str, Any]],
        wave_lanes: Sequence[_Lane],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Run one lane wave and return records in the supplied lane order."""
        wave_records: list[dict[str, Any]] = []
        wave_deliverables: dict[str, dict[str, Any]] = {}
        try:
            raw = delegate_task(
                tasks=list(wave_tasks),
                parent_agent=agent,
                max_iterations=research_mode.scaled_lane_iterations(LANE_MAX_ITERATIONS),
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
                # delegate_task reports guard failures as a top-level object.
                raise RuntimeError(str(payload["error"]))
        except Exception as exc:
            logger.debug("planner lane fan-out failed", exc_info=True)
            return (
                [
                    {
                        "lane": lane.key,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                    for lane in wave_lanes
                ],
                wave_deliverables,
            )

        for index, lane in enumerate(wave_lanes):
            entry = dict(results.get(index) or {})
            status = str(entry.get("status", "") or "missing")
            summary = str(entry.get("summary", "") or "")
            record: dict[str, Any] = {"lane": lane.key, "status": status}
            if entry.get("error"):
                record["error"] = str(entry["error"])[:300]
            parsed = _extract_json_object(summary) if status == "completed" else None
            if parsed is None:
                if summary:
                    record["raw_summary"] = summary
                if status == "completed":
                    record["status"] = "parse-failure"
            else:
                record["deliverable_keys"] = sorted(parsed.keys())
                record["deliverable"] = parsed
                wave_deliverables[lane.key] = parsed
            wave_records.append(record)
        return wave_records, wave_deliverables

    wave_size = research_mode.planner_lane_parallelism(len(tasks))
    for offset in range(0, len(tasks), wave_size):
        raise_if_interrupted("planner lane fan-out interrupted between waves")
        wave_tasks = tasks[offset : offset + wave_size]
        wave_lanes = lanes[offset : offset + wave_size]
        wave_records, wave_deliverables = run_wave(wave_tasks, wave_lanes)
        deferred_positions = [
            index
            for index, record in enumerate(wave_records)
            if record.get("status") == "capacity-deferred"
        ]
        if deferred_positions:
            # delegate_task joins the entire wave. Any successful sibling has
            # therefore released its actor lease before this bounded retry,
            # closing the common one-slot race without waiting indefinitely on
            # genuinely busy process workers.
            retry_records, retry_deliverables = run_wave(
                [wave_tasks[index] for index in deferred_positions],
                [wave_lanes[index] for index in deferred_positions],
            )
            for retry_index, original_index in enumerate(deferred_positions):
                wave_records[original_index] = retry_records[retry_index]
            wave_deliverables.update(retry_deliverables)
        records.extend(wave_records)
        deliverables.update(wave_deliverables)
        raise_if_interrupted("planner lane fan-out interrupted after a completed wave")
    return records, deliverables


def _persist_unsynthesized_deliverables(
    deliverables: Mapping[str, Mapping[str, Any]], *, reason: str
) -> int:
    """Persist lane evidence deterministically when synthesis is unavailable.

    The preceding ``planner-lanes`` event contains each full structured
    deliverable.  This fallback also distills one bounded, stable line per
    lane into the prompt-facing summary so the next prover turn can use the
    research without depending on a successful synthesis call.
    """
    grounding: list[str] = []
    for lane, deliverable in deliverables.items():
        encoded = json.dumps(
            dict(deliverable), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(encoded) > _UNSYNTHESIZED_GROUNDING_MAX_CHARS:
            encoded = (
                encoded[: _UNSYNTHESIZED_GROUNDING_MAX_CHARS - 28] + "...[full payload in journal]"
            )
        grounding.append(f"Unsynthesized planner lane {lane}: {encoded}")

    summary = plan_state.merge_planner_findings(plan_state.load_summary(), grounding=grounding)
    plan_state.save_summary(summary)
    plan_state.save_plan_md(plan_state.load_blueprint(), summary)
    plan_state.append_journal_event(
        {
            "event": "planner-unsynthesized-findings",
            "reason": reason,
            "lanes": list(deliverables),
            "grounding": grounding,
        }
    )
    return len(grounding)


def _synthesis_prompt(
    goal: str,
    deliverables: Mapping[str, Mapping[str, Any]],
    *,
    target_symbol: str,
    active_file: str,
    declaration_slice: str,
    lean_goal: str,
    requested_route: str,
    failed_route_signature: str,
    search_signature: str,
    bp: plan_state.Blueprint,
    prior_evidence: Sequence[Mapping[str, Any]] = (),
) -> str:
    nodes_digest = [
        f"- `{node.name}` [{node.status}] ({node.file})" for node in bp.nodes[:30] if node.name
    ]
    lines = [
        _assignment_scope_block(
            campaign_goal=goal,
            target_symbol=target_symbol,
            active_file=active_file,
            declaration_slice=declaration_slice,
            lean_goal=lean_goal,
            requested_route=requested_route,
            failed_route_signature=failed_route_signature,
            search_signature=search_signature,
        ),
        "",
        "Current graph:",
        *(nodes_digest or ["- [empty]"]),
    ]
    if prior_evidence:
        lines += [
            "",
            "Previously recovered exact-target evidence:",
            planner_evidence.prompt_payload(prior_evidence),
            "This evidence survived an earlier advisor/search turn. Preserve its concrete",
            "construction, failed branches, and helper split unless current Lean evidence",
            "directly refutes them; do not replace it with a vaguer rediscovery plan.",
        ]
    lines += [
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


def _nodes_without_signature_conflicts(
    nodes: Sequence[Mapping[str, Any]],
    changes: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Drop declarations the graph door rejected under an existing identity."""
    conflicted = {
        str(change.get("node_id", "") or "")
        for change in changes
        if change.get("event") == "plan-delta-node-signature-conflict"
    }
    if not conflicted:
        return list(nodes)
    return [
        node
        for node in nodes
        if plan_state.node_id_for(
            str(node.get("name", "") or "").strip(),
            str(node.get("file", "") or "").strip(),
        )
        not in conflicted
    ]


def _synthesis_assertions(
    synthesis: Mapping[str, Any],
) -> tuple[tuple[str, int, str, str], ...]:
    """Return independently checkable assertions from one synthesis payload."""
    assertions: list[tuple[str, int, str, str]] = []
    for section in ("grounding", "strategy"):
        for index, item in enumerate(synthesis.get(section) or []):
            text = str(item or "").strip()
            if text:
                assertions.append((section, index, "", text))

    raw_nodes = synthesis.get("nodes") or synthesis.get("stubs") or []
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, Mapping):
            continue
        for field in ("statement", "notes", "reason", "claim"):
            text = str(item.get(field, "") or "").strip()
            if text:
                assertions.append(("nodes", index, field, text))
    return tuple(assertions)


def _synthesis_arithmetic_rejections(
    synthesis: Mapping[str, Any],
) -> tuple[tuple[dict[str, Any], ...], str]:
    """Return deterministic refutations and the first bounded rejection note."""
    rejections: list[dict[str, Any]] = []
    first_note = ""
    for section, index, field, assertion in _synthesis_assertions(synthesis):
        report = orchestrator_arithmetic_preflight.preflight_route_decision({"reason": assertion})
        evidence = _safe_synthesis_arithmetic_evidence(
            assertion,
            complete_declaration=(section == "nodes" and field == "statement"),
            report=report,
        )
        if not evidence:
            continue
        if not first_note:
            first_note = report.rejection_note()
        rejection: dict[str, Any] = {
            "section": section,
            "index": index,
            "evidence": list(evidence),
        }
        if field:
            rejection["field"] = field
        rejections.append(rejection)
    return tuple(rejections), first_note


def _safe_standalone_prose_assertion(text: str) -> bool:
    """Return whether prose is one bounded assertion rather than historical data."""
    compact = " ".join(str(text or "").split())
    if not compact or len(compact) > 700 or not _ASSERTION_CUE_RE.search(compact):
        return False
    if _NON_ASSERTION_PROSE_RE.search(compact) or any(
        marker in compact for marker in ("{", "}", "[", "]", "∃", "∀", "∧", "∨", "→", "∣", "%")
    ):
        return False
    return len(_EQUALITY_MARKER_RE.findall(compact)) <= 3


def _safe_synthesis_arithmetic_evidence(
    assertion: str,
    *,
    complete_declaration: bool,
    report: orchestrator_arithmetic_preflight.ArithmeticPreflightReport,
) -> tuple[dict[str, str], ...]:
    """Filter general preflight evidence to a sound synthesis assertion shape."""
    evidence = tuple(dict(issue) for issue in report.evidence())
    exact_rational = tuple(
        issue for issue in evidence if issue.get("kind") == "ground-rational-identity"
    )
    if exact_rational:
        return exact_rational
    if complete_declaration:
        # General affine extraction scans binder hypotheses as well as the
        # conclusion. Keep complete declarations on the exact ground-rational
        # path unless/until a hypothesis-aware affine parser is authoritative.
        return ()
    if not _safe_standalone_prose_assertion(assertion):
        return ()
    return tuple(
        issue
        for issue in evidence
        if issue.get("kind") in {"affine-identity", "affine-divisibility"}
    )


def run_planner_phase(
    *,
    goal: str = "",
    target_symbol: str = "",
    active_file: str = "",
    declaration_slice: str = "",
    lean_goal: str = "",
    requested_route: str = "",
    failed_route_signature: str = "",
    search_signature: str = "",
    agent: Any = None,
    cwd: str = "",
    allowed_axioms: Sequence[str] = ("propext", "Classical.choice", "Quot.sound"),
    lane_keys: Sequence[str] = (),
    prior_evidence: Sequence[Mapping[str, Any]] = (),
) -> PlannerOutcome:
    """One full planner phase; never raises.

    ``lane_keys`` narrows the research lanes (the orchestrator LLM's probes
    list selects them in Phase 6); default = every lane up to the
    ``LEANFLOW_PLANNER_MAX_SUBAGENTS`` cap.
    """
    lane_records: list[dict[str, Any]] = []
    try:
        raise_if_interrupted("planner phase interrupted before state loading")
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

        lane_records, deliverables = _run_lanes(
            goal,
            lanes,
            agent=agent,
            target_symbol=target_symbol,
            active_file=active_file,
            declaration_slice=declaration_slice,
            lean_goal=lean_goal,
            requested_route=requested_route,
            failed_route_signature=failed_route_signature,
            search_signature=search_signature,
        )
        raise_if_interrupted("planner phase interrupted after lane fan-out")
        plan_state.append_journal_event(
            {"event": "planner-lanes", "goal": goal[:200], "lanes": lane_records}
        )
        if any(record.get("status") == "capacity-deferred" for record in lane_records):
            reason = "planner background capacity busy; lane wave deferred"
            grounding_count = _persist_unsynthesized_deliverables(deliverables, reason=reason)
            return PlannerOutcome(
                ok=False,
                reason=reason,
                lanes=tuple(lane_records),
                grounding_count=grounding_count,
                synthesis_status="capacity-deferred",
            )

        interrupted_lanes = planner_candidate_admission.interrupted_lane_records(lane_records)
        if interrupted_lanes:
            reason = "planner evidence portfolio interrupted; synthesis deferred"
            grounding_count = _persist_unsynthesized_deliverables(deliverables, reason=reason)
            plan_state.append_journal_event(
                {
                    "event": "planner-synthesis-deferred-incomplete-evidence",
                    "target_symbol": target_symbol,
                    "active_file": active_file,
                    "lanes": list(interrupted_lanes),
                }
            )
            return PlannerOutcome(
                ok=False,
                reason=reason,
                lanes=tuple(lane_records),
                grounding_count=grounding_count,
                synthesis_status=PLANNER_EVIDENCE_INTERRUPTED_STATUS,
            )

        result = run_model_verification_review(
            provider="auto",
            task=PLANNER_SYNTHESIS_TASK,
            prompt=_synthesis_prompt(
                goal,
                deliverables,
                target_symbol=target_symbol,
                active_file=active_file,
                declaration_slice=declaration_slice,
                lean_goal=lean_goal,
                requested_route=requested_route,
                failed_route_signature=failed_route_signature,
                search_signature=search_signature,
                bp=bp,
                prior_evidence=prior_evidence,
            ),
            system_prompt=_SYNTH_SYSTEM_PROMPT,
            timeout_s=planner_synthesis_timeout_s(),
            max_tokens=8000,
        )
        raise_if_interrupted("planner phase interrupted after synthesis review")
        status = str(getattr(result, "status", "") or "").strip().lower()
        if status and status != "ok":
            reason = f"synthesizer unavailable ({status})"
            grounding_count = _persist_unsynthesized_deliverables(deliverables, reason=reason)
            return PlannerOutcome(
                ok=False,
                reason=reason,
                lanes=tuple(lane_records),
                grounding_count=grounding_count,
                synthesis_status=status,
            )
        synthesis = _extract_json_object(str(getattr(result, "response", "") or ""))
        if synthesis is None:
            reason = "synthesizer reply was not parseable JSON"
            grounding_count = _persist_unsynthesized_deliverables(deliverables, reason=reason)
            return PlannerOutcome(
                ok=False,
                reason=reason,
                lanes=tuple(lane_records),
                grounding_count=grounding_count,
                synthesis_status="parse-failure",
            )

        raise_if_interrupted("planner phase interrupted before synthesis validation")
        arithmetic_rejections, rejection_reason = _synthesis_arithmetic_rejections(synthesis)
        if arithmetic_rejections:
            # The lane payload is already durable in ``planner-lanes``. Keep
            # the rejected synthesis out of every graph/summary/stub surface;
            # only the deterministic countercheck becomes planner state.
            plan_state.append_journal_event(
                {
                    "event": "planner-synthesis-arithmetic-rejected",
                    "target_symbol": target_symbol,
                    "active_file": active_file,
                    "rejections": list(arithmetic_rejections),
                }
            )
            return PlannerOutcome(
                ok=False,
                reason=rejection_reason or PLANNER_ARITHMETIC_REJECTION_STATUS,
                lanes=tuple(lane_records),
                synthesis_status=PLANNER_ARITHMETIC_REJECTION_STATUS,
            )

        grounding = [str(item) for item in (synthesis.get("grounding") or []) if str(item).strip()]
        strategy = [str(item) for item in (synthesis.get("strategy") or []) if str(item).strip()]
        # Tolerate the draft-phase field name: a compliant `stubs` reply is
        # the same payload under the fragment's key.
        raw_nodes = synthesis.get("nodes") or synthesis.get("stubs") or []
        admitted_nodes, uncertainty_rejections = (
            planner_candidate_admission.partition_synthesis_nodes(raw_nodes)
        )
        if uncertainty_rejections:
            plan_state.append_journal_event(
                {
                    "event": "planner-synthesis-node-uncertainty-rejected",
                    "target_symbol": target_symbol,
                    "active_file": active_file,
                    "rejections": list(uncertainty_rejections),
                }
            )
        nodes = _validated_nodes(admitted_nodes, active_file=active_file)
        raise_if_interrupted("planner phase interrupted before graph mutation")
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
        placement_nodes = _nodes_without_signature_conflicts(nodes, changes)
        created_node_ids = frozenset(
            str(change.get("node_id", "") or "")
            for change in changes
            if change.get("event") == "node-created"
        )
        summary = plan_state.merge_planner_findings(
            plan_state.load_summary(),
            grounding=grounding,
            strategy=strategy,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        plan_state.save_summary(summary)

        stubs_placed: tuple[str, ...] = ()
        try:
            if target_symbol and active_file:
                stubs_placed = _place_planner_stubs(
                    placement_nodes,
                    target_symbol=target_symbol,
                    active_file=active_file,
                    allowed_axioms=allowed_axioms,
                    cwd=cwd,
                    agent=agent,
                )
        except CooperativeInterrupt:
            # The graph merge precedes source placement. Cancellation at that
            # boundary must not leave newly stated nodes whose declarations
            # never landed. Finish the same demotion transaction used by an
            # ordinary placement rejection before propagating the signal.
            _demote_unplaced_stubs(
                placement_nodes,
                placed=stubs_placed,
                active_file=active_file,
                created_node_ids=created_node_ids,
            )
            plan_state.save_plan_md(plan_state.load_blueprint(), plan_state.load_summary())
            raise
        # A stated node whose stub did NOT land on disk (placement failed,
        # or fell past the per-batch cap) must not stay frontier-eligible:
        # demote it back to a conjecture, journaled after the save.
        if not _demote_unplaced_stubs(
            placement_nodes,
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
        raise_if_interrupted("planner phase interrupted after transactional validation")

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
    raise_if_interrupted("planner helper placement interrupted before validation")
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
