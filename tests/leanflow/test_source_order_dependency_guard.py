"""Tests for source-order dependency warnings."""

from leanflow_cli.native import source_order_dependency_guard as guard
from leanflow_cli.workflows.plan_state import Blueprint, GraphEdge, GraphNode


def _blueprint(active_file: str) -> Blueprint:
    return Blueprint(
        nodes=(
            GraphNode(id="target", name="result", file=active_file, status="proving"),
            GraphNode(id="helper", name="late_helper", file=active_file, status="proved"),
        ),
        edges=(GraphEdge(source="target", target="helper", kind="depends_on"),),
    )


def test_reports_proved_dependency_declared_after_target(tmp_path):
    active = tmp_path / "Demo.lean"
    source = (
        "theorem result : True := by\n"
        "  sorry\n\n"
        "private lemma late_helper : True := by\n"
        "  trivial\n"
    )

    assert guard.later_dependency_names(
        _blueprint(str(active)),
        target_symbol="result",
        active_file=str(active),
        source=source,
    ) == ("late_helper",)


def test_advice_is_empty_when_dependency_precedes_target(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "private lemma late_helper : True := by\n"
        "  trivial\n\n"
        "theorem result : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )

    assert (
        guard.source_order_dependency_advice(
            _blueprint(str(active)),
            target_symbol="result",
            active_file=str(active),
        )
        == ""
    )
