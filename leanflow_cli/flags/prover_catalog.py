"""Describe the small configuration surface of the bounded prover runtime."""

from __future__ import annotations

from leanflow_cli.flags.spec import FlagSpec

_READER = ("leanflow_cli/workflows/prover/config.py",)
_GROUP = "Prover workflow"


def _integer(name: str, default: int, summary: str, minimum: int = 1) -> FlagSpec:
    """Declare one integral job or campaign limit."""
    return FlagSpec(
        name="LEANFLOW_PROVER_" + name,
        kind="tuning",
        value_type="int",
        default=str(default),
        group=_GROUP,
        summary=summary,
        read_in=_READER,
        minimum=minimum,
    )


PROVER_FLAGS: tuple[FlagSpec, ...] = (
    FlagSpec(
        name="LEANFLOW_PROVER_MODE",
        kind="feature",
        value_type="enum",
        default="standard",
        group=_GROUP,
        summary="Standard uses one prover; research adds a planning orchestrator and parallel jobs.",
        choices=("standard", "research"),
        read_in=_READER,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_SEARCH_ORDER",
        kind="tuning",
        value_type="enum",
        default="bottom-up",
        group=_GROUP,
        summary="DFS scheduling order. Top-down candidates remain unverified until dependencies close.",
        choices=("top-down", "bottom-up"),
        read_in=_READER,
        ablatable=True,
    ),
    _integer(
        "JOB_API_CALLS", 300, "Fixed request ceiling per prover pass, including failed requests."
    ),
    _integer(
        "MAX_RESTARTS",
        3,
        "Maximum additional passes per node; each retains its predecessor's progress.",
        0,
    ),
    _integer(
        "PLAN_REFINEMENTS",
        16,
        "Maximum changes of informal proof direction, independent of routine plan notes.",
        0,
    ),
    _integer("PARALLELISM", 2, "Maximum simultaneous jobs in research mode; standard uses one."),
    _integer(
        "TOTAL_API_CALLS",
        10000,
        "Hard total provider-request ceiling across all roles and retries.",
    ),
    _integer("ORCHESTRATOR_API_CALLS", 40, "Call ceiling per planning, research or review job."),
    _integer("MAX_NODES", 128, "Maximum nodes admitted to the theorem DAG."),
    _integer(
        "MAX_DECOMPOSITIONS",
        32,
        "Hard ceiling on structural splitting independent of plan-direction revisions.",
        0,
    ),
    _integer("WALL_TIME_S", 14400, "Maximum campaign runtime in seconds."),
    _integer("TIMEOUT_S", 180, "Maximum seconds per provider request and independent Lean check."),
    _integer("CONTEXT_TOKENS", 64000, "Prover context cap, including space reserved for output."),
    _integer("ORCHESTRATOR_CONTEXT_TOKENS", 64000, "Planning and research context cap."),
    FlagSpec(
        name="LEANFLOW_PROVER_MODEL",
        kind="tuning",
        value_type="string",
        default="",
        group=_GROUP,
        summary="Prover model; empty inherits the launch model.",
        read_in=_READER,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_ORCHESTRATOR_MODEL",
        kind="tuning",
        value_type="string",
        default="",
        group=_GROUP,
        summary="Planning, review and research model; empty inherits the prover model.",
        read_in=_READER,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_COMPRESSION",
        kind="feature",
        value_type="bool",
        default="1",
        group=_GROUP,
        summary="Compact prover history deterministically while keeping its contract and durable notes.",
        read_in=_READER,
        ablatable=True,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_ORCHESTRATOR_COMPRESSION",
        kind="feature",
        value_type="bool",
        default="1",
        group=_GROUP,
        summary="Compact planning and research history deterministically; stages always start with fresh context.",
        read_in=_READER,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_ALLOW_INTERNET",
        kind="feature",
        value_type="bool",
        default="1",
        group=_GROUP,
        summary="Allow web research, downloads and remote Lean search. Off retains local source search and computation; model API access is unaffected.",
        read_in=_READER,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_FILL_DEFINITIONS",
        kind="feature",
        value_type="bool",
        default="0",
        group=_GROUP,
        summary="Explicitly authorize filling sorry holes in definitions as well as theorem proofs.",
        read_in=_READER,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_ALLOWED_AXIOMS",
        kind="tuning",
        value_type="csv",
        default="propext,Classical.choice,Quot.sound",
        group=_GROUP,
        summary="Allowed proof axioms; sorryAx is always rejected. Also configurable through --axioms.",
        read_in=_READER,
        sensitive=True,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_RESUME_RUN_ID",
        kind="runtime",
        value_type="string",
        default="",
        group=_GROUP,
        summary="Resume an existing prover run with its original budgets and saved work.",
        read_in=_READER,
        sensitive=True,
    ),
)
