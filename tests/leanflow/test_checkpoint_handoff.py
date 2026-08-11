"""Tests for authoritative checkpoint handoff status."""

from leanflow_cli.native.checkpoint_handoff import (
    checkpoint_advisory_records,
    checkpoint_success_state,
    extract_negative_evidence,
)


def test_signal_interruption_remains_in_progress_despite_concrete_blocker():
    assert (
        checkpoint_success_state(
            {"exit_code": 130, "interrupt_source": "signal"},
            verified=False,
            blocker_summary="target still contains sorry",
        )
        == "in-progress"
    )


def test_non_signal_blocker_and_verified_state_keep_existing_meanings():
    assert (
        checkpoint_success_state({}, verified=False, blocker_summary="target still contains sorry")
        == "blocked"
    )
    assert (
        checkpoint_success_state(
            {"exit_code": 130},
            verified=True,
            blocker_summary="stale blocker",
        )
        == "verified"
    )


def test_extract_negative_evidence_keeps_only_explicit_nested_dead_branches():
    summary = """## Workflow
- Negative evidence to preserve:
  - The predecessor map reaches the forbidden boundary value.
  - Repeating the same search query produced no new evidence.
- A later workflow fact that is not negative evidence.

## Next steps
1. Inspect the current source.
"""

    assert extract_negative_evidence(summary) == (
        "The predecessor map reaches the forbidden boundary value.",
        "Repeating the same search query produced no new evidence.",
    )


def test_extract_negative_evidence_accepts_prompted_top_level_section():
    summary = """## Blockers
- The target still contains sorry.

Negative evidence:
- The empty-carrier route cannot work because the torsor requires nonemptiness.
- Repeating broad simplification was rejected by the kernel.

## Next steps
1. Develop the recursive invariant.
"""

    assert extract_negative_evidence(summary) == (
        "The empty-carrier route cannot work because the torsor requires nonemptiness.",
        "Repeating broad simplification was rejected by the kernel.",
    )


def test_extract_negative_evidence_recovers_legacy_dead_branch_blockers():
    summary = """## Blockers
- The target still contains sorry.
- `emptyMetric` cannot make the goal vacuous because `AddTorsor` requires `Nonempty P`.
- Direct `simp` and `rfl` were rejected and must not be repeated unchanged.
- The forward implication remains unproved.

## Next steps
- Prove a new invariant.
"""

    assert extract_negative_evidence(summary) == (
        "`emptyMetric` cannot make the goal vacuous because `AddTorsor` requires `Nonempty P`.",
        "Direct `simp` and `rfl` were rejected and must not be repeated unchanged.",
    )


def test_extract_negative_evidence_does_not_promote_unlabeled_checkpoint_prose():
    assert (
        extract_negative_evidence(
            "## Lean findings\n- Use a claimed theorem.\n## Blockers\n- Current target has sorry."
        )
        == ()
    )


def test_checkpoint_advisory_records_recovers_only_exact_assignment(tmp_path):
    active = tmp_path / "Main.lean"
    other = tmp_path / "Other.lean"
    entries = [
        {
            "checkpoint_id": "old",
            "created_at": "2026-08-05T00:00:00+00:00",
            "target_symbol": "result",
            "active_files": [active.name],
            "summary_text": "## Blockers\n- The empty route cannot work.",
        },
        {
            "checkpoint_id": "wrong-file",
            "target_symbol": "result",
            "active_files": [str(other)],
            "summary_text": "## Blockers\n- A different route cannot work.",
        },
        {
            "checkpoint_id": "new",
            "created_at": "2026-08-05T00:01:00+00:00",
            "target_symbol": "result",
            "active_files": [str(active)],
            "summary_text": "Negative evidence:\n- Broad simplification was rejected.",
        },
    ]

    records = checkpoint_advisory_records(
        entries,
        target_symbol="result",
        active_file=str(active),
    )

    assert [record["checkpoint_id"] for record in records] == ["old", "new"]
    assert records[0]["negative_evidence"] == ("The empty route cannot work.",)
    assert records[1]["negative_evidence"] == ("Broad simplification was rejected.",)
