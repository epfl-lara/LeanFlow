"""Keep rejected planning reports inside their original conversation and budget."""

from __future__ import annotations

import json
from typing import Any

import pytest


def test_negative_review_is_a_valid_report() -> None:
    """A reviewer may reject the mathematics; do not pressure it into approval."""
    from leanflow_cli.workflows.prover.models import Dag
    from leanflow_cli.workflows.prover.planning_feedback import report_feedback

    assert (
        report_feedback(
            '{"accepted":false,"critique":"Missing base case"}',
            stage="review",
            dag=Dag([], []),
            max_nodes=10,
        )["accepted"]
        is True
    )


@pytest.mark.parametrize(
    "report", [{"plan": "outline", "change_kind": []}, {"accepted": "true"}, {"accepted": False}]
)
def test_malformed_fields_return_feedback(report: dict[str, Any]) -> None:
    """Bad field types are repairable submissions rather than runtime exceptions."""
    from leanflow_cli.workflows.prover.models import Dag
    from leanflow_cli.workflows.prover.planning_feedback import report_feedback

    feedback = report_feedback(
        json.dumps(report),
        stage="review" if "accepted" in report else "outline",
        dag=Dag([], []),
        max_nodes=10,
    )
    assert feedback["accepted"] is False
    assert feedback["error"]


def test_native_planning_repairs_format_and_graph_inside_one_invocation(
    tmp_path, monkeypatch
) -> None:
    """Preserve the real controller report gate while delegating the whole loop."""
    from agent.runtime.harness import HarnessResult
    from leanflow_cli.workflows.prover import planning_controller
    from leanflow_cli.workflows.prover.config import ProverConfig
    from leanflow_cli.workflows.prover.runtime import ProverRuntime
    from tests.leanflow.native_harness_helpers import call_tool, finish_operation

    source = tmp_path / "Main.lean"
    source.write_text("theorem goal : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    replies = iter(
        [
            ['{"plan":"Initial research"}'],
            [
                'Here is the JSON: {"plan":"Keep this argument", "nodes":[]}',
                '{"plan":"Keep this argument", "nodes":[{"id":"helper","name":"helper","file":"LeanFlowProofs/Helper.lean","statement":"theorem helper : True := by trivial"}]}',
                '{"plan":"Keep this argument", "nodes":[]}',
            ],
            ['{"accepted":"yes"}', '{"accepted":true,"critique":"Sound"}'],
        ]
    )
    accepted = []

    def harness(request, **kwargs):
        request.on_admitting()
        request.on_started()
        assert call_tool(request, "get_assignment", {})["planning_stage"] in {
            "outline",
            "proposal",
            "review",
        }
        responses = next(replies)
        for response in responses:
            receipt = call_tool(request, "submit_report", {"response": response})
            accepted.append(finish_operation(request, receipt)["accepted"])
        return HarnessResult("completed", responses[-1], returncode=0, cleanup_verified=True)

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    monkeypatch.setattr(planning_controller, "materialize", lambda *args: None)
    runtime = ProverRuntime(root=tmp_path, targets=[source], config=ProverConfig(mode="research"))
    try:
        assert runtime._research_plan("Design a proof")
        assert accepted == [True, False, False, True, False, True]
        assert [(job["role"], job["api_calls"]) for job in runtime.state["jobs"]] == [
            ("orchestrator", 1),
            ("orchestrator", 1),
            ("review", 1),
        ]
        assert runtime.state["metrics"]["agent_calls"] == 3
        assert source.read_text().endswith("by sorry\n")
    finally:
        runtime.cancelled.set()
        for bridge in runtime.native_bridges.values():
            bridge.close()
