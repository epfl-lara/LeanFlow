"""Tests for durable checked partial exact-target candidates."""

from __future__ import annotations

from leanflow_cli.workflows import target_candidate_checkpoint as checkpoint


def _enable_plan_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))


def _source(tmp_path, statement: str = "theorem demo : True := by\n  sorry\n"):
    path = tmp_path / "Main.lean"
    path.write_text(statement, encoding="utf-8")
    return path


def _check(active, *, errors: list[tuple[int, str]]) -> dict:
    return {
        "success": True,
        "ok": False,
        "action": "check_target",
        "target": "demo",
        "file": str(active),
        "valid_without_sorry": False,
        "has_errors": True,
        "has_sorry": False,
        "timed_out": False,
        "replacement_matches_target": True,
        "verification_scope": "target_candidate",
        "backend": "lean_interact",
        "messages": [
            {
                "severity": "error",
                "message": message,
                "start": {"line": line, "column": 2},
            }
            for line, message in errors
        ],
    }


def test_partial_candidate_survives_resume_and_renders_verbatim(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    replacement = "theorem demo : True := by\n  exact missing"

    captured = checkpoint.capture_checked_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement=replacement,
        check=_check(active, errors=[(2, "Unknown identifier `missing`")]),
        campaign_id="campaign-1",
    )

    assert captured is not None
    assert captured["error_count"] == 1
    assert captured["first_error_line"] == 2
    resumed = checkpoint.matching_candidate(target_symbol="demo", active_file=str(active))
    assert resumed == captured
    prompt = checkpoint.candidate_prompt(resumed)
    assert "resumable working state, not a verified proof" in prompt
    assert "do not restart from the source `sorry` body" in prompt
    assert replacement in prompt
    assert "Unknown identifier `missing`" in prompt


def test_fewer_errors_replace_older_candidate_and_regressions_do_not(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    first = checkpoint.capture_checked_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  exact first",
        check=_check(active, errors=[(2, "first"), (2, "second")]),
    )
    better = checkpoint.capture_checked_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  exact almost",
        check=_check(active, errors=[(2, "one remaining")]),
    )
    rejected_regression = checkpoint.capture_checked_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  exact regressed",
        check=_check(active, errors=[(2, "first"), (2, "second")]),
    )

    assert first is not None and better is not None
    assert rejected_regression is None
    retained = checkpoint.matching_candidate(target_symbol="demo", active_file=str(active))
    assert retained is not None
    assert retained["replacement"] == "theorem demo : True := by\n  exact almost"


def test_later_first_error_wins_when_error_count_is_equal(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    checkpoint.capture_checked_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  exact early",
        check=_check(active, errors=[(2, "early error")]),
    )
    later = checkpoint.capture_checked_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement=(
            "theorem demo : True := by\n" "  have h : True := True.intro\n" "  exact later"
        ),
        check=_check(active, errors=[(3, "later error")]),
    )

    assert later is not None
    assert checkpoint.matching_candidate(target_symbol="demo", active_file=str(active)) == later


def test_candidate_rejects_placeholders_mismatches_and_timeouts(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    check = _check(active, errors=[(2, "error")])

    assert (
        checkpoint.capture_checked_candidate(
            target_symbol="demo",
            active_file=str(active),
            replacement="theorem demo : True := by\n  sorry",
            check=check,
        )
        is None
    )
    assert (
        checkpoint.capture_checked_candidate(
            target_symbol="demo",
            active_file=str(active),
            replacement="theorem other : True := by\n  exact missing",
            check=check,
        )
        is None
    )
    assert (
        checkpoint.capture_checked_candidate(
            target_symbol="demo",
            active_file=str(active),
            replacement="theorem demo : True := by\n  exact missing",
            check={**check, "timed_out": True},
        )
        is None
    )
    assert checkpoint.raw_summary().get(checkpoint.SUMMARY_KEY) in (None, [])


def test_statement_change_retires_checkpoint_but_proof_body_change_does_not(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    captured = checkpoint.capture_checked_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  exact missing",
        check=_check(active, errors=[(2, "error")]),
    )
    assert captured is not None

    active.write_text("theorem demo : True := by\n  aesop\n", encoding="utf-8")
    assert checkpoint.matching_candidate(target_symbol="demo", active_file=str(active)) is not None

    active.write_text("theorem demo : False := by\n  sorry\n", encoding="utf-8")
    assert checkpoint.matching_candidate(target_symbol="demo", active_file=str(active)) is None


def test_successful_exact_candidate_retires_partial_checkpoint(monkeypatch, tmp_path):
    _enable_plan_state(monkeypatch, tmp_path)
    active = _source(tmp_path)
    checkpoint.capture_checked_candidate(
        target_symbol="demo",
        active_file=str(active),
        replacement="theorem demo : True := by\n  exact missing",
        check=_check(active, errors=[(2, "error")]),
    )
    successful = {
        **_check(active, errors=[]),
        "ok": True,
        "valid_without_sorry": True,
        "has_errors": False,
    }

    assert (
        checkpoint.capture_checked_candidate(
            target_symbol="demo",
            active_file=str(active),
            replacement="theorem demo : True := by\n  trivial",
            check=successful,
        )
        is None
    )
    assert checkpoint.matching_candidate(target_symbol="demo", active_file=str(active)) is None
