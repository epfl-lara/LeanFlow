"""The declared registry of LeanFlow runtime knobs.

Entries are grouped by the subsystem that reads them. ``ablatable=True`` marks a
knob whose two settings are a meaningful experimental comparison — those are the
ones an ablation matrix should offer first.

Adding a knob to the runtime means adding it here too; the drift test in
``tests/leanflow/test_flag_catalog.py`` fails on a catalogued name that no
non-test module reads.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from leanflow_cli.flags.prover_catalog import PROVER_FLAGS
from leanflow_cli.flags.spec import FlagSpec

_RESEARCH = "Research campaign"
_ORCH = "Orchestration"
_PLANNER = "Planner"
_DISPATCH = "Dispatch"
_QUEUE = "Queue and verification"
_LEAN = "Lean backend"
_SEARCH = "Search and retrieval"
_PROVIDER = "Provider and model"
_CONTEXT = "Context and prompting"
_SAFETY = "Research boundaries"
_OPS = "Operational"
_PLUMBING = "Launcher plumbing"


FLAG_CATALOG: tuple[FlagSpec, ...] = (
    *PROVER_FLAGS,
    # ---------------------------------------------------------------- research
    FlagSpec(
        name="LEANFLOW_RESEARCH_MODE",
        kind="feature",
        value_type="bool",
        default="0",
        group=_RESEARCH,
        summary=(
            "Master switch for the relentless-research profile. Turns difficulty into a "
            "route or epoch transition rather than a terminal result, and switches on the "
            "plan, retrieval, orchestration, dispatch, feasibility, reporting, and "
            "learning surfaces as one coherent set."
        ),
        read_in=("leanflow_cli/workflows/research_mode.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_RESEARCH_WORKERS",
        kind="tuning",
        value_type="int",
        default="2",
        group=_RESEARCH,
        summary=(
            "Number of background research workers a campaign may run alongside the "
            "foreground prover. 0 keeps the run single-lane."
        ),
        read_in=("leanflow_cli/workflows/research_mode.py",),
        minimum=0,
        maximum=16,
    ),
    FlagSpec(
        name="LEANFLOW_RESEARCH_LOCAL_LOOGLE",
        kind="feature",
        value_type="bool",
        default="0",
        group=_RESEARCH,
        summary=(
            "Let a research campaign start its own resident local Loogle index. Off by "
            "default in research mode because the index costs multiple gigabytes on top of "
            "the foreground Lean language server; non-research runs use local Loogle anyway."
        ),
        read_in=("leanflow_cli/workflows/research_mode.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_RESEARCH_EVENT_PREEMPTION",
        kind="feature",
        value_type="bool",
        default="0",
        group=_RESEARCH,
        summary=(
            "Allow a research finding to preempt the foreground prover's current turn "
            "instead of waiting for the next safe boundary."
        ),
        read_in=("leanflow_cli/native/native_runner.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_RESEARCH_RECYCLE_MULTI_ATTEMPT_MCP",
        kind="runtime",
        value_type="bool",
        default="0",
        group=_RESEARCH,
        summary=(
            "Reclaim the MCP process after each bounded multi-attempt tactic screen, "
            "trading warm-start latency for resident memory."
        ),
        read_in=("tools/mcp/mcp_reclaim.py",),
    ),
    FlagSpec(
        name="LEANFLOW_RESEARCH_RECYCLE_STATEFUL_LEAN_LSP_MCP",
        kind="runtime",
        value_type="bool",
        default="0",
        group=_RESEARCH,
        summary=(
            "Reclaim the stateful lean-lsp MCP process after each call. Lowers peak memory "
            "during a long campaign at the cost of repeated language-server startup."
        ),
        read_in=("tools/mcp/mcp_reclaim.py",),
    ),
    FlagSpec(
        name="LEANFLOW_LEARNINGS",
        kind="feature",
        value_type="bool",
        default="0",
        group=_RESEARCH,
        summary=(
            "Append one compact machine-written entry to the project's learnings.md on "
            "every terminal scope exit, so later runs start with what earlier ones found."
        ),
        read_in=("leanflow_cli/workflows/learnings.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_FINAL_REPORT",
        kind="feature",
        value_type="bool",
        default="0",
        group=_RESEARCH,
        summary=(
            "Require a rigorous end-of-scope report on every non-verified exit: what was "
            "proved, what was tried, what was learned, and which jobs remain open."
        ),
        read_in=("leanflow_cli/workflows/final_report.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_CURRICULUM_ORDERING",
        kind="feature",
        value_type="bool",
        default="0",
        group=_RESEARCH,
        summary=(
            "Order the declaration queue by a curriculum heuristic — shorter stated goals "
            "first — instead of plain dependency order."
        ),
        read_in=("leanflow_cli/native/native_runner.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_BUDGET_BREAKPOINT",
        kind="feature",
        value_type="bool",
        default="0",
        group=_RESEARCH,
        summary=(
            "Turn budget exhaustion into a routable breakpoint the orchestrator can act on "
            "rather than a terminal stop."
        ),
        read_in=("leanflow_cli/native/native_runner.py",),
        ablatable=True,
    ),
    # ----------------------------------------------------------- orchestration
    FlagSpec(
        name="LEANFLOW_ORCHESTRATOR_ENABLED",
        kind="feature",
        value_type="bool",
        default="0",
        group=_ORCH,
        summary=(
            "Apply the deterministic route table to each cycle, so stalls, budget "
            "breakpoints, and retry exhaustion become explicit route decisions."
        ),
        read_in=("leanflow_cli/workflows/orchestrator.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_ORCHESTRATOR_LLM_ENABLED",
        kind="feature",
        value_type="bool",
        default="0",
        group=_ORCH,
        summary=(
            "Let a bounded LLM call refine the deterministic route. Upgrade-only: the "
            "model may sharpen a route but never downgrade a working one to park or "
            "escalate."
        ),
        read_in=("leanflow_cli/workflows/orchestrator_llm.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_ORCHESTRATOR_CADENCE_CYCLES",
        kind="tuning",
        value_type="int",
        default="8",
        group=_ORCH,
        summary=(
            "How many cycles pass between orchestrator route evaluations. The research "
            "profile tightens this to 4."
        ),
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_ORCHESTRATOR_MAX_ROUTES",
        kind="tuning",
        value_type="int",
        default="4",
        group=_ORCH,
        summary="Upper bound on distinct routes the orchestrator may hold open at once.",
        read_in=("leanflow_cli/workflows/orchestrator.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_ORCHESTRATOR_LLM_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="75",
        group=_ORCH,
        summary="Wall-clock budget for one orchestrator route-refinement call.",
        read_in=("leanflow_cli/workflows/orchestrator_llm.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_MANAGER_LLM_ENABLED",
        kind="feature",
        value_type="bool",
        default="0",
        group=_ORCH,
        summary=(
            "Legacy alias that forces the persistence coach to 'live'. Prefer "
            "LEANFLOW_MANAGER_LLM_MODE, which this only supplies a fallback for."
        ),
        read_in=("leanflow_cli/workflows/manager_nudge.py",),
    ),
    FlagSpec(
        name="LEANFLOW_MANAGER_LLM_MODE",
        kind="feature",
        value_type="enum",
        default="",
        group=_ORCH,
        summary=(
            "Persistence-coach mode after a rejected proof turn: 'live' issues the model "
            "call, 'dark' computes it without showing it to the prover, 'off' uses "
            "deterministic text. Unset resolves to 'live' for prove and autoprove, 'off' "
            "everywhere else."
        ),
        choices=("", "off", "dark", "live"),
        read_in=("leanflow_cli/workflows/manager_nudge.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_MANAGER_NUDGE_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="60",
        group=_ORCH,
        summary="Wall-clock budget for one persistence-coach model call.",
        read_in=("leanflow_cli/workflows/manager_nudge.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_GRAPH_FRONTIER_SELECTION",
        kind="feature",
        value_type="bool",
        default="0",
        group=_ORCH,
        summary=(
            "Pick the next target from the dependency-graph frontier rather than walking "
            "the queue in recorded order."
        ),
        read_in=("leanflow_cli/native/native_runner.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_FIDELITY_AUDIT",
        kind="feature",
        value_type="bool",
        default="0",
        group=_ORCH,
        summary=(
            "Audit each assigned declaration against the informal claim it came from "
            "before proving, and record a pass/fail verdict in the journal."
        ),
        read_in=("leanflow_cli/native/native_runner.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_MAX_PROVE_DIRECTIONS",
        kind="tuning",
        value_type="int",
        default="3",
        group=_ORCH,
        summary=(
            "How many rival attack directions may be opened for one goal. Each direction "
            "is a stub file discharged by its own nested prover job. Clamped to 1..3."
        ),
        read_in=("leanflow_cli/workflows/multi_direction.py",),
        minimum=1,
        maximum=3,
    ),
    # ---------------------------------------------------------------- planner
    FlagSpec(
        name="LEANFLOW_PLANNER_ENABLED",
        kind="feature",
        value_type="bool",
        default="0",
        group=_PLANNER,
        summary=(
            "Enable the plan route's mechanical arm: capacity-bounded web/literature, "
            "mathlib, and empirical research sub-agents feeding a synthesis step."
        ),
        read_in=("leanflow_cli/workflows/planner_phase.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_PLANNER_MAX_SUBAGENTS",
        kind="tuning",
        value_type="int",
        default="3",
        group=_PLANNER,
        summary="Maximum research sub-agents the planner fan-out may run concurrently.",
        read_in=("leanflow_cli/workflows/planner_phase.py",),
        minimum=1,
        maximum=8,
    ),
    FlagSpec(
        name="LEANFLOW_PLANNER_LANE_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="600",
        group=_PLANNER,
        summary="Wall-clock budget for one planner research lane.",
        read_in=("leanflow_cli/workflows/planner_phase.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_PLANNER_SYNTHESIS_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="300",
        group=_PLANNER,
        summary="Wall-clock budget for the planner's synthesis step.",
        read_in=("leanflow_cli/workflows/planner_phase.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_PLANNER_CAPACITY_WAIT_S",
        kind="tuning",
        value_type="float",
        default="1.0",
        group=_PLANNER,
        summary="How long a planner delegate waits for a free actor slot before giving up.",
        read_in=("leanflow_cli/workflows/planner_phase.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_PLAN_STATE",
        kind="feature",
        value_type="bool",
        default="0",
        group=_PLANNER,
        summary=(
            "Persist the proof plan, dependency graph, and journal — blueprint.json, "
            "summary.json, plan.md — so plans survive restarts and are inspectable."
        ),
        read_in=("leanflow_cli/workflows/plan_state.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_PLAN_STATE_DIR",
        kind="runtime",
        value_type="path",
        default="",
        group=_PLANNER,
        summary=(
            "Override the directory holding plan artifacts. Empty means the project's "
            ".leanflow/workflow-state."
        ),
        read_in=("leanflow_cli/workflows/plan_state.py",),
        sensitive=True,
    ),
    FlagSpec(
        name="LEANFLOW_PREMISE_RETRIEVAL",
        kind="feature",
        value_type="bool",
        default="0",
        group=_PLANNER,
        summary=(
            "Retrieve candidate premises for a declaration at assignment time and put "
            "them in front of the prover before its first attempt."
        ),
        read_in=("leanflow_cli/native/native_runner.py",),
        ablatable=True,
    ),
    # --------------------------------------------------------------- dispatch
    FlagSpec(
        name="LEANFLOW_DISPATCH_ENABLED",
        kind="feature",
        value_type="bool",
        default="0",
        group=_DISPATCH,
        summary=(
            "Enable tracked, lineage-addressed job dispatch: propose → deploy → running → "
            "done/failed/stuck/killed, persisted in the summary's dispatch ledger."
        ),
        read_in=("leanflow_cli/workflows/dispatch_service.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_DISPATCH_MAX_CONCURRENT",
        kind="tuning",
        value_type="int",
        default="3",
        group=_DISPATCH,
        summary=(
            "Maximum dispatch jobs running at once across the campaign. The research "
            "profile lowers this to the requested worker count."
        ),
        read_in=("leanflow_cli/workflows/dispatch_service.py",),
        minimum=1,
        maximum=16,
    ),
    FlagSpec(
        name="LEANFLOW_BACKGROUND_PROVIDER_CAPACITY",
        kind="tuning",
        value_type="int",
        default="2",
        group=_DISPATCH,
        summary=(
            "Shared cap on live background research actors — dispatch workers and planner "
            "delegates draw from the same pool."
        ),
        read_in=("core/provider_capacity.py",),
        minimum=0,
        maximum=16,
    ),
    FlagSpec(
        name="LEANFLOW_PROJECT_LEAN_ADMISSION",
        kind="feature",
        value_type="bool",
        default="0",
        group=_DISPATCH,
        summary=(
            "Arbitrate memory-heavy Lean subprocesses across the project's process tree, "
            "reclaiming background-worker state while the foreground cache stays warm."
        ),
        read_in=("core/project_resource_admission.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_JOB_WALL_CLOCK_S",
        kind="tuning",
        value_type="int",
        default="0",
        group=_DISPATCH,
        summary="Wall-clock budget for one dispatched nested prover job. 0 means unbounded.",
        read_in=("leanflow_cli/workflows/prover_jobs.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_JOB_API_STEPS",
        kind="tuning",
        value_type="int",
        default="0",
        group=_DISPATCH,
        summary="Provider-turn budget for one dispatched prover job. 0 means unbounded.",
        read_in=("leanflow_cli/workflows/multi_direction.py",),
        minimum=0,
    ),
    # ------------------------------------------------- queue and verification
    FlagSpec(
        name="LEANFLOW_NEGATION_PROBE",
        kind="feature",
        value_type="bool",
        default="0",
        group=_QUEUE,
        summary=(
            "Run bounded negation probes on a stuck declaration: mechanically build ¬P and "
            "look for a counterexample before spending more turns on a proof."
        ),
        read_in=("leanflow_cli/lean/negation_probe.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_NEGATION_PROBE_AFTER_FAILURES",
        kind="tuning",
        value_type="int",
        default="2",
        group=_QUEUE,
        summary="How many failed proof attempts must precede the first negation probe.",
        read_in=("leanflow_cli/lean/negation_probe.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_NEGATION_PROBE_BUDGET",
        kind="tuning",
        value_type="int",
        default="1",
        group=_QUEUE,
        summary="How many negation probes one declaration may consume.",
        read_in=("leanflow_cli/lean/negation_probe.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_NEGATION_PROBE_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="120",
        group=_QUEUE,
        summary="Wall-clock budget for one negation probe.",
        read_in=("leanflow_cli/lean/negation_probe.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_NEGATION_RESERVATION_STALE_S",
        kind="tuning",
        value_type="int",
        default="300",
        group=_QUEUE,
        summary="After this long, an unfinished negation reservation is treated as abandoned.",
        read_in=("leanflow_cli/lean/negation_probe.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="300",
        group=_QUEUE,
        summary="Budget for revalidating a negation result against current project source.",
        read_in=("leanflow_cli/workflows/negation_revalidation_policy.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK",
        kind="feature",
        value_type="bool",
        default="0",
        group=_QUEUE,
        summary=(
            "Check the axiom dependency profile of an accepted declaration, so a proof "
            "cannot quietly acquire a nonstandard axiom on the way to green."
        ),
        read_in=("leanflow_cli/native/native_runner.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_ALLOWED_AXIOMS",
        kind="runtime",
        value_type="csv",
        default="",
        group=_QUEUE,
        summary=(
            "Extra axioms a run may depend on beyond the standard three. Set by the "
            "--axioms launch flag."
        ),
        read_in=("leanflow_cli/lean/lean_helper_ephemeral.py",),
    ),
    FlagSpec(
        name="LEANFLOW_QUEUE_INVARIANT_CHECKS",
        kind="feature",
        value_type="bool",
        default="0",
        group=_QUEUE,
        summary=(
            "Assert the theorem queue's invariants after each mutation. Catches state "
            "corruption early at some runtime cost."
        ),
        read_in=("leanflow_cli/workflows/queue_manager_live.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_QUEUE_DECIDE_SHADOW",
        kind="feature",
        value_type="bool",
        default="0",
        group=_QUEUE,
        summary=(
            "Evaluate the queue verdict a second time on a fresh hydration and emit a "
            "structured mismatch when the two disagree. Production state is untouched."
        ),
        read_in=("leanflow_cli/workflows/queue_decide_shadow.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_QUEUE_DECIDE_AUTHORITY",
        kind="feature",
        value_type="bool",
        default="0",
        group=_QUEUE,
        summary="Make the unified verdict policy authoritative rather than advisory.",
        read_in=("leanflow_cli/workflows/queue_decide_shadow.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_QUEUE_BREAKPOINT_CONSECUTIVE",
        kind="tuning",
        value_type="int",
        default="3",
        group=_QUEUE,
        summary="Consecutive unproductive queue verdicts before a breakpoint is raised.",
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS",
        kind="feature",
        value_type="bool",
        default="0",
        group=_QUEUE,
        summary=(
            "Permit edits to declaration statements, not just proof bodies. Off keeps the "
            "guard that stops a run from proving an easier theorem than the one asked for."
        ),
        read_in=("leanflow_cli/lean/lean_statement_guard.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_CONSTRUCTION_NO_PROGRESS_TURN_LIMIT",
        kind="tuning",
        value_type="int",
        default="3",
        group=_QUEUE,
        summary="Provider turns without construction progress before backpressure applies.",
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_CONSTRUCTION_SOURCE_INSPECTION_HARD_LIMIT",
        kind="tuning",
        value_type="int",
        default="6",
        group=_QUEUE,
        summary="Cap on source-inspection steps within one construction cycle.",
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_CONSTRUCTION_SOURCE_INSPECTION_REPEAT_HARD_LIMIT",
        kind="tuning",
        value_type="int",
        default="4",
        group=_QUEUE,
        summary="Cap on repeated inspections of the same source within one cycle.",
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_SEARCH_PROGRESS_HARD_LIMIT",
        kind="tuning",
        value_type="int",
        default="12",
        group=_QUEUE,
        summary="Cap on consecutive search steps before the run must attempt construction.",
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_SEARCH_SYNTHESIS_REJECTION_LIMIT",
        kind="tuning",
        value_type="int",
        default="2",
        group=_QUEUE,
        summary="Rejected bounded-search syntheses tolerated before the route is fenced.",
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_DIAGNOSTIC_FEEDBACK_LIMIT",
        kind="tuning",
        value_type="int",
        default="0",
        group=_QUEUE,
        summary=(
            "Diagnostic-only provider turns allowed before the turn is ended at a safe "
            "boundary. 0 uses the built-in bound."
        ),
        read_in=("leanflow_cli/native/diagnostic_loop_guard.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_THEOREM_BUDGET_STEPS",
        kind="tuning",
        value_type="int",
        default="0",
        group=_QUEUE,
        summary="Provider-turn budget for one theorem before a breakpoint. 0 means unbounded.",
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_HUMAN_REVIEW_ENABLED",
        kind="feature",
        value_type="bool",
        default="0",
        group=_QUEUE,
        summary=(
            "Pause at review points for a human verdict instead of routing autonomously. "
            "Set by the --human-review launch flag."
        ),
        read_in=("leanflow_cli/workflows/orchestrator.py",),
    ),
    # ------------------------------------------------------------ lean backend
    FlagSpec(
        name="LEANFLOW_RCP_PREFIX_CACHE",
        kind="feature",
        value_type="bool",
        default="0",
        group=_PROVIDER,
        summary=(
            "Shape requests so an OpenAI-compatible endpoint can reuse its prefix cache "
            "across turns. Enabled by default in newly installed configs."
        ),
        read_in=("leanflow_cli/native/native_runner.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_LEAN_COMMAND_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="120",
        group=_LEAN,
        summary="Soft wall-clock budget for one Lean command.",
        read_in=("leanflow_cli/lean/lean_command_timeout.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_LEAN_COMMAND_HARD_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="",
        group=_LEAN,
        summary=(
            "Absolute subprocess cap shared by canonical and incremental Lean checks. "
            "Unset means no hard cap beyond the soft timeout."
        ),
        read_in=("leanflow_cli/lean/lean_command_timeout.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_LEAN_INSPECT_WALL_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="0",
        group=_LEAN,
        summary="Wall-clock budget for one Lean inspection tool call. 0 uses the default.",
        read_in=("tools/implementations/lean_tool.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_MANAGER_INCREMENTAL_PREPARE_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="300",
        group=_LEAN,
        summary="Budget for preparing the manager's warm incremental Lean environment.",
        read_in=("leanflow_cli/workflows/manager_verification.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_MANAGER_INCREMENTAL_CHECK_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="60",
        group=_LEAN,
        summary="Budget for one manager-side incremental Lean check.",
        read_in=("leanflow_cli/workflows/manager_verification.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_ADVISORY_VERIFICATION_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="180",
        group=_LEAN,
        summary="Budget for one advisory verification pass. Capped at 300s by the runtime.",
        read_in=("leanflow_cli/workflows/verification_providers.py",),
        minimum=1,
        maximum=300,
    ),
    FlagSpec(
        name="LEANFLOW_INCREMENTAL_FEEDBACK_MAX_CHARS",
        kind="tuning",
        value_type="int",
        default="0",
        group=_LEAN,
        summary="Cap on incremental-check feedback text retained internally. 0 uses the default.",
        read_in=("leanflow_cli/lean/lean_incremental.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_INCREMENTAL_PROVIDER_MAX_CHARS",
        kind="tuning",
        value_type="int",
        default="0",
        group=_LEAN,
        summary="Cap on incremental-check feedback shown to the model. 0 uses the default.",
        read_in=("leanflow_cli/lean/lean_incremental.py",),
        minimum=0,
    ),
    # ------------------------------------------------- search and retrieval
    FlagSpec(
        name="LEANFLOW_LEANEXPLORE_BACKEND",
        kind="feature",
        value_type="enum",
        default="auto",
        group=_SEARCH,
        summary=(
            "LeanExplore semantic-search backend for the foreground prover. 'auto' picks "
            "local when the index is installed and falls back to the API."
        ),
        choices=("auto", "local", "api", "off", "disabled"),
        read_in=("leanflow_cli/lean/lean_search_providers.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_DISPATCH_LEANEXPLORE_BACKEND",
        kind="feature",
        value_type="enum",
        default="off",
        group=_SEARCH,
        summary=(
            "LeanExplore backend for dispatched background workers. Off by default so a "
            "second local index per lane cannot defeat the worker memory bound."
        ),
        choices=("auto", "local", "api", "off", "disabled"),
        read_in=("leanflow_cli/lean/lean_search_providers.py",),
    ),
    FlagSpec(
        name="LEANFLOW_LEANEXPLORE_RERANK_TOP",
        kind="tuning",
        value_type="int",
        default="0",
        group=_SEARCH,
        summary="How many LeanExplore hits to rerank. 0 uses the provider default.",
        read_in=("leanflow_cli/lean/lean_search_providers.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_WEB_SEARCH_CONCURRENCY",
        kind="tuning",
        value_type="int",
        default="8",
        group=_SEARCH,
        summary="Concurrent requests the web-search orchestrator may have in flight.",
        read_in=("tools/implementations/web_search_orchestration.py",),
        minimum=1,
        maximum=32,
    ),
    FlagSpec(
        name="LEANFLOW_REPO_CLONE_MAX_BYTES",
        kind="tuning",
        value_type="int",
        default="0",
        group=_SEARCH,
        summary="Size ceiling for a repository clone during research. 0 uses the default.",
        read_in=("tools/implementations/repo_clone.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_REPO_CLONE_TIMEOUT_SECONDS",
        kind="tuning",
        value_type="int",
        default="0",
        group=_SEARCH,
        summary="Wall-clock budget for one repository clone. 0 uses the default.",
        read_in=("tools/implementations/repo_clone.py",),
        minimum=0,
    ),
    # ------------------------------------------------- research boundaries
    FlagSpec(
        name="LEANFLOW_DISABLE_SOLUTION_RESEARCH",
        kind="feature",
        value_type="bool",
        default="0",
        group=_SAFETY,
        summary=(
            "Clean-room boundary: forbid looking up an existing solution to the target. "
            "Set by --clean-room and required for uncontaminated benchmark runs."
        ),
        read_in=("tools/utilities/repository_research_policy.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_DISABLE_REPOSITORY_RESEARCH",
        kind="feature",
        value_type="bool",
        default="0",
        group=_SAFETY,
        summary="Forbid repository research entirely, including neutral reference reading.",
        read_in=("tools/utilities/repository_research_policy.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_CLEAN_ROOM_TASK_LABELS",
        kind="runtime",
        value_type="string",
        default="",
        group=_SAFETY,
        summary=(
            "Pipe-separated labels the clean-room filter treats as the forbidden target. "
            "Derived from the target path by --clean-room."
        ),
        read_in=("tools/utilities/repository_research_policy.py",),
        sensitive=True,
    ),
    FlagSpec(
        name="LEANFLOW_DIAGNOSTIC_FILE_ACCESS",
        kind="feature",
        value_type="bool",
        default="0",
        group=_SAFETY,
        summary=(
            "Let prover file tools read managed-workflow artifacts. Off keeps campaign "
            "state out of the model's context."
        ),
        read_in=("tools/utilities/workflow_artifact_guard.py",),
    ),
    # ---------------------------------------------------- provider and model
    FlagSpec(
        name="LEANFLOW_INFERENCE_PROVIDER",
        kind="runtime",
        value_type="string",
        default="",
        group=_PROVIDER,
        summary="Provider id used when no explicit --provider is given.",
        read_in=("leanflow_cli/runtime/runtime_provider.py",),
    ),
    FlagSpec(
        name="LEANFLOW_API_TIMEOUT",
        kind="tuning",
        value_type="float",
        default="1200.0",
        group=_PROVIDER,
        summary="Wall-clock budget for one provider API call.",
        read_in=("agent/providers/api_caller.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_PROVIDER_RETRY_BACKOFFS",
        kind="tuning",
        value_type="csv",
        default="5,15,45",
        group=_PROVIDER,
        summary="Comma-separated backoff seconds for successive provider retries.",
        read_in=("leanflow_cli/native/native_runner.py",),
    ),
    FlagSpec(
        name="LEANFLOW_PROVIDER_EXHAUSTION_BACKOFFS",
        kind="tuning",
        value_type="csv",
        default="60",
        group=_PROVIDER,
        summary="Backoff seconds applied after a provider reports quota exhaustion.",
        read_in=("leanflow_cli/native/native_runner.py",),
    ),
    FlagSpec(
        name="LEANFLOW_PROVIDER_RECOVERY_BUDGET_S",
        kind="tuning",
        value_type="float",
        default="180.0",
        group=_PROVIDER,
        summary="Total time spent recovering from transient provider failures before failing.",
        read_in=("agent/providers/api_caller.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_PROVIDER_RESET_MAX_WAIT_SECONDS",
        kind="tuning",
        value_type="int",
        default="900",
        group=_PROVIDER,
        summary="Longest wait for a provider rate-limit window to reset.",
        read_in=("core/provider_availability.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_PROVIDER_WAIT_HEARTBEAT",
        kind="tuning",
        value_type="float",
        default="30.0",
        group=_PROVIDER,
        summary="How often a provider wait emits a heartbeat so the run looks alive.",
        read_in=("run_agent.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_CODEX_MODEL",
        kind="runtime",
        value_type="string",
        default="",
        group=_PROVIDER,
        summary="Model id used for the Codex OAuth provider.",
        read_in=("agent/providers/",),
    ),
    FlagSpec(
        name="LEANFLOW_CODEX_REASONING_EFFORT",
        kind="runtime",
        value_type="enum",
        default="",
        group=_PROVIDER,
        summary="Reasoning effort requested from the Codex provider.",
        choices=("", "low", "medium", "high", "xhigh"),
        read_in=("agent/providers/",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_LEAN_REASONING_HELP_MAX_TOKENS",
        kind="tuning",
        value_type="int",
        default="64000",
        group=_PROVIDER,
        summary="Output-token ceiling for one Lean reasoning-expert consultation.",
        read_in=("tools/implementations/lean_experts.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_LEAN_DECOMPOSE_HELPERS_MAX_TOKENS",
        kind="tuning",
        value_type="int",
        default="64000",
        group=_PROVIDER,
        summary="Output-token ceiling for one helper-decomposition consultation.",
        read_in=("tools/implementations/lean_experts.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_EXPERT_MAX_RESPONSE_CHARS",
        kind="tuning",
        value_type="int",
        default="0",
        group=_PROVIDER,
        summary="Cap on an external expert CLI's response text. 0 uses the default.",
        read_in=("leanflow_cli/cli/expert_help.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_NOUS_TIMEOUT_SECONDS",
        kind="tuning",
        value_type="int",
        default="15",
        group=_PROVIDER,
        summary="Timeout for the Nous credential exchange.",
        read_in=("agent/providers/provider_client.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_NOUS_MIN_KEY_TTL_SECONDS",
        kind="tuning",
        value_type="int",
        default="1800",
        group=_PROVIDER,
        summary="Refresh a Nous key when its remaining lifetime drops below this.",
        read_in=("agent/providers/provider_client.py",),
        minimum=1,
    ),
    # -------------------------------------------------- context and prompting
    FlagSpec(
        name="LEANFLOW_NATIVE_CONTEXT_COMPRESSION_TOKENS",
        kind="tuning",
        value_type="int",
        default="96000",
        group=_CONTEXT,
        summary="Token threshold at which a managed run compresses its conversation.",
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=1000,
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_REPLAY_ALL_REASONING",
        kind="feature",
        value_type="bool",
        default="0",
        group=_CONTEXT,
        summary=(
            "Replay every prior reasoning block to the provider instead of only the "
            "retained window. Much larger requests; useful for reasoning-continuity tests."
        ),
        read_in=("agent/compression/conversation_manager.py",),
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_RUNNER_LEAN_PROMPT",
        kind="feature",
        value_type="bool",
        default="0",
        group=_CONTEXT,
        summary="Use the lean, reduced system prompt for the managed runner.",
        read_in=("leanflow_cli/native/native_runner.py",),
        ablatable=True,
    ),
    # ------------------------------------------------------------ operational
    FlagSpec(
        name="LEANFLOW_LOW_MEMORY",
        kind="runtime",
        value_type="bool",
        default="0",
        group=_OPS,
        summary="Keep heavy optional indexes and warm caches disabled throughout the run.",
        read_in=("core/runtime_modes.py",),
    ),
    FlagSpec(
        name="LEANFLOW_DISABLE_MCP",
        kind="runtime",
        value_type="bool",
        default="0",
        group=_OPS,
        summary="Start with no MCP servers. Lean search and LSP tools become unavailable.",
        read_in=(
            "tools/mcp/mcp_config.py",
            "leanflow_cli/workflows/prover/session_transport.py",
        ),
    ),
    FlagSpec(
        name="LEANFLOW_MCP_STDERR_INHERIT",
        kind="runtime",
        value_type="bool",
        default="0",
        group=_OPS,
        summary="Let MCP server stderr reach the terminal instead of the managed log.",
        read_in=("tools/mcp/mcp_transport.py",),
    ),
    FlagSpec(
        name="LEANFLOW_REDACT_SECRETS",
        kind="runtime",
        value_type="bool",
        default="1",
        group=_OPS,
        summary="Redact credential-shaped strings from logs, activity, and trajectories.",
        read_in=("agent/accounting/redact.py",),
        sensitive=True,
    ),
    FlagSpec(
        name="LEANFLOW_DUMP_REQUESTS",
        kind="runtime",
        value_type="path",
        default="",
        group=_OPS,
        summary="Write each raw provider request to this directory. Debugging only.",
        read_in=("run_agent.py",),
        sensitive=True,
    ),
    FlagSpec(
        name="LEANFLOW_DUMP_REQUEST_STDOUT",
        kind="runtime",
        value_type="bool",
        default="0",
        group=_OPS,
        summary="Echo raw provider requests to stdout. Debugging only; very noisy.",
        read_in=("run_agent.py",),
        sensitive=True,
    ),
    FlagSpec(
        name="LEANFLOW_CHECKPOINT_TIMEOUT",
        kind="tuning",
        value_type="int",
        default="30",
        group=_OPS,
        summary="Wall-clock budget for writing one checkpoint.",
        read_in=("tools/utilities/checkpoint_manager.py",),
        minimum=1,
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_WRITER_JOIN_TIMEOUT_S",
        kind="tuning",
        value_type="int",
        default="2",
        group=_OPS,
        summary="How long shutdown waits for the state writer thread to drain.",
        read_in=("leanflow_cli/native/native_runner.py",),
        minimum=0,
    ),
    FlagSpec(
        name="LEANFLOW_YOLO_MODE",
        kind="runtime",
        value_type="bool",
        default="0",
        group=_OPS,
        summary="Skip command approval prompts. Only safe in a sandbox or throwaway tree.",
        read_in=("tools/utilities/approval.py",),
        sensitive=True,
    ),
    FlagSpec(
        name="LEANFLOW_EXEC_ASK",
        kind="runtime",
        value_type="bool",
        default="0",
        group=_OPS,
        summary="Ask before every command execution, overriding the toolset's policy.",
        read_in=("tools/utilities/approval.py",),
    ),
    FlagSpec(
        name="LEANFLOW_TIMEZONE",
        kind="runtime",
        value_type="string",
        default="",
        group=_OPS,
        summary="IANA timezone for rendered timestamps. Empty uses the system zone.",
        read_in=("core/time.py",),
    ),
    FlagSpec(
        name="LEANFLOW_HOME",
        kind="runtime",
        value_type="path",
        default="~/.leanflow",
        group=_OPS,
        summary="Root for LeanFlow's config, sessions, skills, sandboxes, and state database.",
        read_in=("core/home.py", "leanflow_cli/native/native_config.py"),
        sensitive=True,
    ),
    FlagSpec(
        name="LEANFLOW_SANDBOX_BASE_IMAGE",
        kind="runtime",
        value_type="string",
        default="",
        group=_OPS,
        summary="Base container image used when building the local sandbox image.",
        read_in=("leanflow_cli/runtime/sandbox_runtime.py",),
        sensitive=True,
    ),
    # ------------------------------------------------------ launcher plumbing
    FlagSpec(
        name="LEANFLOW_PROJECT_ROOT",
        kind="internal",
        value_type="path",
        default="",
        group=_PLUMBING,
        summary="Resolved Lean project root for the managed child process.",
        read_in=("leanflow_cli/native/native_config.py",),
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_WORKFLOW_KIND",
        kind="internal",
        value_type="string",
        default="",
        group=_PLUMBING,
        summary="Workflow kind (prove, formalize, review, …) for this managed run.",
        read_in=("leanflow_cli/lean/lean_services.py",),
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_WORKFLOW_COMMAND",
        kind="internal",
        value_type="string",
        default="",
        group=_PLUMBING,
        summary="Backend command string the managed run was launched with.",
        read_in=("leanflow_cli/lean/lean_services.py",),
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_ACTIVE_FILE",
        kind="internal",
        value_type="path",
        default="",
        group=_PLUMBING,
        summary="Project-relative target file for a file-scoped run.",
        read_in=("leanflow_cli/lean/lean_services.py",),
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_ACTIVE_SKILL",
        kind="internal",
        value_type="string",
        default="",
        group=_PLUMBING,
        summary="Skill driving the managed run's prompt assembly.",
        read_in=("leanflow_cli/workflows/workflow_state.py",),
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_MODEL",
        kind="internal",
        value_type="string",
        default="",
        group=_PLUMBING,
        summary="Resolved model id for the managed run's primary lane.",
        read_in=("leanflow_cli/workflow.py",),
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_PROVIDER",
        kind="internal",
        value_type="string",
        default="",
        group=_PLUMBING,
        summary="Resolved provider id for the managed run's primary lane.",
        read_in=("leanflow_cli/workflow.py",),
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_PARALLEL_AGENTS",
        kind="internal",
        value_type="int",
        default="1",
        group=_PLUMBING,
        summary="Swarm width. Above 1 requires explicit user approval at launch.",
        read_in=("leanflow_cli/workflow.py",),
    ),
    FlagSpec(
        name="LEANFLOW_NATIVE_TOOLSET",
        kind="internal",
        value_type="string",
        default="",
        group=_PLUMBING,
        summary="Toolset selected for the managed run.",
        read_in=("leanflow_cli/workflow.py",),
    ),
    FlagSpec(
        name="LEANFLOW_WORKFLOW_RUN_ID",
        kind="internal",
        value_type="string",
        default="",
        group=_PLUMBING,
        summary="Identifier for this run's activity stream and metadata.",
        read_in=("leanflow_cli/native/native_runner.py", "core/provider_capacity.py"),
    ),
    FlagSpec(
        name="LEANFLOW_WORKFLOW_PARENT_RUN_ID",
        kind="internal",
        value_type="string",
        default="",
        group=_PLUMBING,
        summary="Parent run for a dispatched nested job, forming the lineage edge.",
        read_in=("leanflow_cli/workflows/workflow_state.py",),
    ),
    FlagSpec(
        name="LEANFLOW_DISPATCH_WORKER",
        kind="internal",
        value_type="bool",
        default="0",
        group=_PLUMBING,
        summary="Marks this process as an isolated background research worker.",
        read_in=("core/runtime_modes.py",),
    ),
    FlagSpec(
        name="LEANFLOW_DISPATCH_SCRATCH_ONLY",
        kind="internal",
        value_type="bool",
        default="0",
        group=_PLUMBING,
        summary="Marks a dispatch worker as read/check-only — it may not mutate project source.",
        read_in=("core/runtime_modes.py",),
    ),
    FlagSpec(
        name="LEANFLOW_DISPATCH_ARCHETYPE",
        kind="internal",
        value_type="string",
        default="",
        group=_PLUMBING,
        summary="Which research archetype a dispatch worker is serving.",
        read_in=("core/runtime_modes.py",),
    ),
    FlagSpec(
        name="LEANFLOW_PROVE_FILE_SCOPE",
        kind="internal",
        value_type="path",
        default="",
        group=_PLUMBING,
        summary="File scope pinned for a nested file-scoped prove job.",
        read_in=("leanflow_cli/native/native_runner.py",),
    ),
)


_BY_NAME: dict[str, FlagSpec] = {spec.name: spec for spec in FLAG_CATALOG}


def lookup_flag(name: str) -> FlagSpec | None:
    """Return the spec for one knob name, or None when it is not catalogued."""
    return _BY_NAME.get(str(name or "").strip().upper())


def flags_by_kind(kind: str) -> tuple[FlagSpec, ...]:
    """Return every catalogued knob of one kind, in catalog order."""
    return tuple(spec for spec in FLAG_CATALOG if spec.kind == kind)


def flag_groups() -> OrderedDict[str, tuple[FlagSpec, ...]]:
    """Return knobs grouped by subsystem, preserving catalog order within a group."""
    grouped: OrderedDict[str, list[FlagSpec]] = OrderedDict()
    for spec in FLAG_CATALOG:
        grouped.setdefault(spec.group, []).append(spec)
    return OrderedDict((group, tuple(specs)) for group, specs in grouped.items())


def catalog_payload() -> dict[str, Any]:
    """Return the full catalog in the JSON shape the CLI and extension consume."""
    return {
        "version": 1,
        "count": len(FLAG_CATALOG),
        "groups": [
            {"name": group, "flags": [spec.to_payload() for spec in specs]}
            for group, specs in flag_groups().items()
        ],
    }
