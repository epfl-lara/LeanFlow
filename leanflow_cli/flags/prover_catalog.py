"""Describe native-backend prover orchestration and optional global limits."""

from __future__ import annotations

from leanflow_cli.flags.spec import FlagSpec

_READER = ("leanflow_cli/workflows/prover/config.py",)
_GROUP = "Prover workflow"


def _integer(name: str, default: int | None, summary: str, minimum: int = 0) -> FlagSpec:
    """Declare a nonnegative count; an empty default leaves the limit unlimited."""
    return FlagSpec(
        name="LEANFLOW_PROVER_" + name,
        kind="tuning",
        value_type="int",
        default="" if default is None else str(default),
        group=_GROUP,
        summary=summary,
        read_in=_READER,
        minimum=minimum,
    )


PROVER_FLAGS: tuple[FlagSpec, ...] = (
    *(
        FlagSpec(
            name="LEANFLOW_PROVER_" + suffix,
            kind="tuning",
            value_type="enum",
            default="",
            choices=("", "codex", "claude", "lemex"),
            group=_GROUP,
            summary=summary,
            read_in=_READER,
        )
        for suffix, summary in (
            ("PROVIDER", "Native prover backend; empty inherits the launch backend."),
            (
                "ORCHESTRATOR_PROVIDER",
                "Native planning and review backend; empty inherits the prover backend.",
            ),
        )
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_MODE",
        kind="feature",
        value_type="enum",
        default="standard",
        group=_GROUP,
        summary="Standard uses one prover; research adds planning and parallel jobs.",
        choices=("standard", "research"),
        read_in=_READER,
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_SEARCH_ORDER",
        kind="tuning",
        value_type="enum",
        default="top-down",
        group=_GROUP,
        summary="Schedule theorem dependencies top-down or bottom-up.",
        choices=("top-down", "bottom-up"),
        read_in=_READER,
        ablatable=True,
    ),
    _integer("PARALLELISM", 2, "Maximum simultaneous jobs in research mode; standard uses one.", 1),
    _integer(
        "AGENT_CALLS",
        None,
        "Global native CLI invocation limit. Each resume or retry is another call; empty means unlimited.",
    ),
    _integer(
        "TOKENS",
        None,
        "Global reported token limit. Stops future calls; active calls may overshoot and unavailable usage remains unknown. Empty means unlimited.",
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_WALL_TIME_S",
        kind="tuning",
        value_type="float",
        default="",
        group=_GROUP,
        summary="Global active campaign seconds across resumes, including tools and verification. Empty means unlimited.",
        read_in=_READER,
        minimum=0,
    ),
    *(
        FlagSpec(
            name="LEANFLOW_PROVER_" + suffix,
            kind="tuning",
            value_type="string",
            default="",
            group=_GROUP,
            summary=summary,
            read_in=_READER,
        )
        for suffix, summary in (
            (
                "MODEL",
                "Optional prover model override; empty preserves the launch or native runtime default.",
            ),
            (
                "ORCHESTRATOR_MODEL",
                "Optional planning model override; empty inherits the prover selection.",
            ),
            (
                "REASONING_EFFORT",
                "Optional prover reasoning effort; empty preserves the native runtime default.",
            ),
            (
                "ORCHESTRATOR_REASONING_EFFORT",
                "Optional planning reasoning effort; empty inherits the prover selection.",
            ),
        )
    ),
    FlagSpec(
        name="LEANFLOW_PROVER_ALLOW_INTERNET",
        kind="feature",
        value_type="bool",
        default="1",
        group=_GROUP,
        summary="Allow web research, downloads and remote Lean search.",
        read_in=_READER,
        ablatable=True,
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
        summary="Resume a compatible native-backend run with its original global limits and saved work.",
        read_in=_READER,
        sensitive=True,
    ),
)
