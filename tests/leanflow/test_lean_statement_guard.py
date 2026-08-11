import pytest

from leanflow_cli.lean.lean_statement_guard import (
    ALLOW_STATEMENT_EDITS_ENV,
    should_guard_lean_statement_path,
    validate_lean_statement_edit,
)


@pytest.fixture(autouse=True)
def clear_guard_env(monkeypatch):
    monkeypatch.delenv(ALLOW_STATEMENT_EDITS_ENV, raising=False)


def test_lean_statement_guard_allows_proof_body_edit():
    before = "theorem demo (n : Nat) : n = n := by\n  rfl\n"
    after = "theorem demo (n : Nat) : n = n := by\n  exact rfl\n"

    result = validate_lean_statement_edit(before, after)

    assert result.ok is True


def test_lean_statement_guard_blocks_theorem_statement_change():
    before = "theorem demo (n : Nat) : n = n := by\n  rfl\n"
    after = "theorem demo (n : Nat) : n + 0 = n := by\n  simp\n"

    result = validate_lean_statement_edit(before, after)

    assert result.ok is False
    assert "changed statement of theorem demo" in result.error
    assert ALLOW_STATEMENT_EDITS_ENV not in result.error


def test_lean_statement_guard_blocks_deleting_existing_theorem():
    before = "lemma helper : True := by\n  trivial\n\ntheorem demo : True := by\n  trivial\n"
    after = "lemma helper : True := by\n  trivial\n"

    result = validate_lean_statement_edit(before, after)

    assert result.ok is False
    assert "deleted theorem demo" in result.error


def test_lean_statement_guard_blocks_moving_existing_declaration_order():
    before = "lemma helper : True := by\n  trivial\n\ntheorem demo : True := by\n  trivial\n"
    after = "theorem demo : True := by\n  trivial\n\nlemma helper : True := by\n  trivial\n"

    result = validate_lean_statement_edit(before, after)

    assert result.ok is False
    assert "moved or reordered" in result.error


def test_lean_statement_guard_allows_adding_new_theorem():
    before = "theorem demo : True := by\n  trivial\n"
    after = before + "\ntheorem extra : True := by\n  trivial\n"

    result = validate_lean_statement_edit(before, after)

    assert result.ok is True


def test_lean_statement_guard_blocks_duplicate_existing_declaration():
    """Reject reinserting an exact helper before touching the Lean file."""
    helper = "private lemma checked_map : True := by\n  trivial"
    before = helper + "\n\ntheorem target : True := by\n  trivial\n"
    after = helper + "\n\n" + before

    result = validate_lean_statement_edit(before, after)

    assert result.ok is False
    assert result.violations == ("duplicated existing lemma checked_map",)


def test_lean_statement_guard_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv(ALLOW_STATEMENT_EDITS_ENV, "1")

    assert should_guard_lean_statement_path("Demo.lean") is False
