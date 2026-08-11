"""Warning-cleanup source-change guard tests."""

from leanflow_cli.native import warning_cleanup_guard


def test_unchanged_recheck_matches_granted_declaration(tmp_path):
    active = tmp_path / "Main.lean"
    state: dict[str, object] = {}
    declaration = "theorem demo : True := by\n  trivial"

    warning_cleanup_guard.remember_grant(
        state,
        target_symbol="demo",
        active_file=str(active),
        declaration=declaration,
    )

    assert warning_cleanup_guard.unchanged_since_grant(
        state,
        target_symbol="demo",
        active_file=str(active),
        declaration=declaration + "\n",
    )


def test_meaningful_edit_releases_unchanged_guard(tmp_path):
    active = tmp_path / "Main.lean"
    state: dict[str, object] = {}
    warning_cleanup_guard.remember_grant(
        state,
        target_symbol="demo",
        active_file=str(active),
        declaration="theorem demo : True := by\n  all_goals trivial",
    )

    assert not warning_cleanup_guard.unchanged_since_grant(
        state,
        target_symbol="demo",
        active_file=str(active),
        declaration="theorem demo : True := by\n  trivial",
    )


def test_clear_grant_is_scoped(tmp_path):
    state: dict[str, object] = {}
    for target in ("one", "two"):
        warning_cleanup_guard.remember_grant(
            state,
            target_symbol=target,
            active_file=str(tmp_path / "Main.lean"),
            declaration=f"theorem {target} : True := by\n  trivial",
        )

    warning_cleanup_guard.clear_grant(
        state,
        target_symbol="one",
        active_file=str(tmp_path / "Main.lean"),
    )

    assert not warning_cleanup_guard.unchanged_since_grant(
        state,
        target_symbol="one",
        active_file=str(tmp_path / "Main.lean"),
        declaration="theorem one : True := by\n  trivial",
    )
    assert warning_cleanup_guard.unchanged_since_grant(
        state,
        target_symbol="two",
        active_file=str(tmp_path / "Main.lean"),
        declaration="theorem two : True := by\n  trivial",
    )
