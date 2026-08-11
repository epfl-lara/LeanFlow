"""Tests for bounded foreground exact-candidate persistence and replay state."""

from __future__ import annotations

from leanflow_cli.workflows import verification_candidate_replay as replay
from leanflow_cli.workflows.workflow_json_io import update_json_file


def _enable_plan_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))


def _source(tmp_path, statement: str = "theorem demo : True := by\n  sorry\n"):
    path = tmp_path / "Main.lean"
    path.write_text(statement, encoding="utf-8")
    return path


def test_operational_candidate_survives_resume_and_revalidates_verbatim(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    replacement = "theorem demo : True := by\n  trivial"

    captured = replay.capture_operational_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement=replacement,
        campaign_id="campaign-1",
        process_id=101,
    )

    assert captured is not None
    assert captured["schema_version"] == replay.SCHEMA_VERSION
    assert captured["state"] == "awaiting_axiom_profile"
    assert replay.replay_due(captured, process_id=101) is False
    assert replay.replay_due(captured, process_id=202) is True
    assert (
        replay.replay_due(
            captured,
            process_id=101,
            verifier_contract_version="exact-target-inline-axiom-v2",
        )
        is True
    )

    ready = replay.mark_replay(
        captured["candidate_id"],
        status="ready_to_commit",
        process_id=202,
        detail="current kernel and axiom gates passed",
    )

    assert ready is not None
    prompt = replay.ready_candidate_prompt(ready)
    assert "kernel check and candidate-bound axiom allowlist" in prompt
    assert replacement in prompt
    assert replay.matching_candidate(target_symbol="demo", active_file=str(active)) == ready


def test_ready_candidate_requires_replay_after_new_process_launch(monkeypatch, tmp_path):
    """Do not expose a persisted ready verdict as current after verifier restart."""
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    fingerprint_one = "1" * 64
    fingerprint_two = "2" * 64
    captured = replay.capture_operational_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  trivial",
        process_id=101,
        process_fingerprint=fingerprint_one,
    )
    assert captured is not None
    ready = replay.mark_replay(
        captured["candidate_id"],
        status="ready_to_commit",
        process_id=101,
        process_fingerprint=fingerprint_one,
    )
    assert ready is not None

    assert (
        replay.replay_due(
            ready,
            process_id=101,
            process_fingerprint=fingerprint_one,
        )
        is False
    )
    assert (
        replay.replay_due(
            ready,
            process_id=101,
            process_fingerprint=fingerprint_two,
        )
        is True
    )


def test_launch_fingerprint_prevents_pid_reuse_from_suppressing_replay(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    captured = replay.capture_operational_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  trivial",
        process_id=404,
        process_fingerprint="a" * 64,
    )
    assert captured is not None

    assert (
        replay.replay_due(
            captured,
            process_id=404,
            process_fingerprint="b" * 64,
        )
        is True
    )


def test_malformed_non_list_store_fails_closed_and_recovers(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    update_json_file(
        replay.plan_state.plan_state_paths().summary_json,
        lambda summary: summary.update({replay.SUMMARY_KEY: 17}),
    )

    assert replay.matching_candidate(target_symbol="demo", active_file=str(active)) is None
    assert replay.SUMMARY_KEY not in replay.raw_summary()

    captured = replay.capture_operational_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  trivial",
    )
    assert captured is not None
    assert replay.raw_summary()[replay.SUMMARY_KEY] == [captured]


def test_candidate_identity_ignores_proof_body_but_retires_on_statement_change(
    monkeypatch, tmp_path
):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    replacement = "theorem demo : True := by\n  trivial"
    captured = replay.capture_operational_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement=replacement,
        process_id=10,
    )
    assert captured is not None

    active.write_text("theorem demo : True := by\n  aesop\n", encoding="utf-8")
    assert replay.matching_candidate(target_symbol="demo", active_file=str(active)) is not None

    active.write_text("theorem demo : False := by\n  sorry\n", encoding="utf-8")
    assert replay.matching_candidate(target_symbol="demo", active_file=str(active)) is None
    assert replay.SUMMARY_KEY not in replay.raw_summary()


def test_capture_rejects_placeholders_and_oversized_candidates(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)

    assert (
        replay.capture_operational_candidate(
            target_symbol="demo",
            active_file=str(active),
            replacement="theorem demo : True := by\n  sorry",
        )
        is None
    )
    assert (
        replay.capture_operational_candidate(
            target_symbol="demo",
            active_file=str(active),
            replacement="theorem demo : True := by\n  "
            + " " * replay.MAX_CANDIDATE_CHARS
            + "trivial",
        )
        is None
    )
    assert replay.raw_summary().get(replay.SUMMARY_KEY) in (None, [])


def test_capture_keeps_only_the_latest_bounded_candidate_per_target(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    first = replay.capture_operational_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  trivial",
    )
    second = replay.capture_operational_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  exact True.intro",
    )

    records = replay.raw_summary()[replay.SUMMARY_KEY]
    assert first is not None and second is not None
    assert len(records) == 1
    assert records[0]["candidate_id"] == second["candidate_id"]
    assert records[0]["replacement"] == "theorem demo : True := by\n  exact True.intro"


def test_mathematical_rejection_retires_only_the_matching_candidate(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    first = _source(tmp_path)
    second = tmp_path / "Other.lean"
    second.write_text("theorem other : True := by\n  sorry\n", encoding="utf-8")
    first_record = replay.capture_operational_candidate(
        target_symbol="demo",
        active_file=str(first),
        replacement="theorem demo : True := by\n  trivial",
    )
    second_record = replay.capture_operational_candidate(
        target_symbol="other",
        active_file=str(second),
        replacement="theorem other : True := by\n  trivial",
    )
    assert first_record is not None and second_record is not None

    replay.mark_replay(
        first_record["candidate_id"],
        status="mathematically_rejected",
        process_id=22,
        detail="type mismatch",
    )

    assert replay.matching_candidate(target_symbol="demo", active_file=str(first)) is None
    assert (
        replay.matching_candidate(target_symbol="other", active_file=str(second))["candidate_id"]
        == second_record["candidate_id"]
    )
