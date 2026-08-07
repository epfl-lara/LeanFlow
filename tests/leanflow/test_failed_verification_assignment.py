"""Tests for restoring sorry-free assignments after failed verification."""

from leanflow_cli.native.failed_verification_assignment import restore_failed_assignment


def test_restore_failed_assignment_reopens_empty_file_queue(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem result : True := by\n  trivial\n", encoding="utf-8")
    failure = "maximum heartbeats exceeded"

    restored = restore_failed_assignment(
        {
            "active_file": str(source),
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "declaration_queue": [],
            "current_queue_item": {},
            "queue_needs_final_file_sweep": True,
        },
        {"target_symbol": "result", "active_file": str(source)},
        queue_item={"label": "result", "kind": "theorem", "line": 1, "end_line": 2},
        declaration_prefix="",
        declaration_slice="theorem result : True := by\n  trivial",
        failure=failure,
    )

    assert restored["target_symbol"] == "result"
    assert restored["current_queue_item"]["label"] == "result"
    assert restored["declaration_queue_total"] == 1
    assert restored["current_queue_item_slice"].startswith("theorem result")
    assert restored["current_blocker"] == failure
    assert restored["queue_needs_final_file_sweep"] is False


def test_restore_failed_assignment_does_not_override_an_existing_queue_item(tmp_path):
    source = tmp_path / "Main.lean"
    source.write_text("theorem result : True := by\n  trivial\n", encoding="utf-8")
    state = {
        "active_file": str(source),
        "declaration_scope": "file",
        "declaration_queue_total": 1,
        "current_queue_item": {"label": "helper"},
    }

    restored = restore_failed_assignment(
        state,
        {"target_symbol": "result", "active_file": str(source)},
        queue_item={"label": "result"},
        declaration_prefix="",
        declaration_slice="theorem result : True := by trivial",
        failure="failed",
    )

    assert restored == state
