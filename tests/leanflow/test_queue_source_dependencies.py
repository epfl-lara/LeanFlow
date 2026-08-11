"""Tests for pending source-reference dependency inference."""

from leanflow_cli.native.queue_source_dependencies import pending_source_dependencies


def test_result_depends_on_earlier_unresolved_answer(tmp_path):
    active = tmp_path / "P4.lean"
    active.write_text(
        "def answer : Set Nat := sorry\n\n"
        "theorem result : {n : Nat | n > 0} = answer := by\n"
        "  sorry\n",
        encoding="utf-8",
    )

    assert pending_source_dependencies(
        str(active),
        ("answer", "result"),
    ) == {"result": ("answer",)}


def test_comments_and_later_declarations_do_not_create_dependencies(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem first : True := by\n"
        "  sorry\n\n"
        "-- first is not a dependency of second\n"
        "theorem second : True := by\n"
        "  sorry\n\n"
        "theorem later : second = second := by\n"
        "  sorry\n",
        encoding="utf-8",
    )

    assert pending_source_dependencies(
        str(active),
        ("first", "second", "later"),
    ) == {"later": ("second",)}
