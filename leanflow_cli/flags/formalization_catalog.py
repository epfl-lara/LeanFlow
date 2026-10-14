"""Declare reviewer controls and immutable handoff launcher fields."""

from leanflow_cli.flags.spec import FlagSpec

FORMALIZATION_FLAGS = (
    FlagSpec(
        name="LEANFLOW_SETUP_MAX_RSS_GIB",
        kind="tuning",
        value_type="int",
        default="16",
        minimum=1,
        maximum=20,
        group="Project setup",
        summary="Sampled aggregate owned-process RSS ceiling in GiB; shared pages count per process.",
        read_in=("leanflow_cli/workflows/project_setup/execution.py",),
    ),
    FlagSpec(
        name="LEANFLOW_FORMALIZATION_REVIEW_PROVIDER",
        kind="runtime",
        value_type="enum",
        default="",
        choices=("", "codex", "claude", "lemex"),
        group="Formalization",
        summary="Independent source reviewer backend; empty inherits the drafting backend.",
        read_in=(
            "leanflow_cli/formalization/launch_options.py",
            "leanflow_cli/formalization/review.py",
        ),
    ),
    FlagSpec(
        name="LEANFLOW_FORMALIZATION_REVIEW_MODEL",
        kind="runtime",
        value_type="string",
        default="",
        group="Formalization",
        summary="Independent reviewer model; a different backend keeps its native default.",
        read_in=(
            "leanflow_cli/formalization/launch_options.py",
            "leanflow_cli/formalization/review.py",
        ),
    ),
    FlagSpec(
        name="LEANFLOW_FORMALIZATION_REVIEW_EFFORT",
        kind="runtime",
        value_type="enum",
        default="",
        choices=("", "auto", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        group="Formalization",
        summary="Reviewer reasoning effort; auto uses its native default.",
        read_in=(
            "leanflow_cli/formalization/launch_options.py",
            "leanflow_cli/formalization/review.py",
        ),
    ),
    FlagSpec(
        name="LEANFLOW_FORMALIZATION_CORRECTION_ROUNDS",
        kind="tuning",
        value_type="int",
        default="2",
        minimum=0,
        maximum=8,
        group="Formalization",
        summary="Maximum correction rounds within the existing campaign budget.",
        read_in=("leanflow_cli/formalization/launch_options.py",),
    ),
    FlagSpec(
        name="LEANFLOW_FORMALIZATION_HANDOFF",
        kind="internal",
        value_type="path",
        default="",
        group="Launcher plumbing",
        summary="Exact published formalization handoff path.",
        read_in=("leanflow_cli/workflows/prover/entrypoint.py",),
    ),
    FlagSpec(
        name="LEANFLOW_FORMALIZATION_REVISION",
        kind="internal",
        value_type="string",
        default="",
        group="Launcher plumbing",
        summary="Selected reviewed revision bound to the prover handoff.",
        read_in=("leanflow_cli/workflows/prover/entrypoint.py",),
    ),
)
