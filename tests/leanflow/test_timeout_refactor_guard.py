"""Tests for timeout-driven structural-refactor edit classification."""

from leanflow_cli.native.timeout_refactor_guard import (
    is_heartbeat_only_change,
    is_same_tactic_with_budget_wrapper,
)


def test_added_heartbeat_wrapper_is_only_change() -> None:
    before = """theorem demo : True := by
  aesop
"""
    after = """theorem demo : True := by
  set_option maxHeartbeats 2_000_000 in
    aesop
"""

    assert is_heartbeat_only_change(before, after)


def test_changed_heartbeat_budget_is_only_change() -> None:
    before = """theorem demo : True := by
  set_option maxHeartbeats 200000 in
    aesop
"""
    after = """theorem demo : True := by
  set_option maxHeartbeats 2000000 in
    aesop
"""

    assert is_heartbeat_only_change(before, after)


def test_substantive_change_with_heartbeat_wrapper_is_admitted() -> None:
    before = """theorem demo : True := by
  aesop
"""
    after = """theorem demo : True := by
  set_option maxHeartbeats 2_000_000 in
    exact True.intro
"""

    assert not is_heartbeat_only_change(before, after)


def test_local_budget_wrapper_preserves_same_tactic() -> None:
    assert is_same_tactic_with_budget_wrapper(
        "aesop",
        "set_option maxHeartbeats 1_000_000 in aesop",
    )


def test_local_exact_by_classical_wrapper_preserves_same_tactic() -> None:
    assert is_same_tactic_with_budget_wrapper("aesop", "exact by classical aesop")


def test_local_material_prefix_is_not_the_same_tactic() -> None:
    assert not is_same_tactic_with_budget_wrapper(
        "aesop",
        "obtain ⟨m⟩ := move_exists t; aesop",
    )
