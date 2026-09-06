"""Drain conditional proof candidates in one pass through the controller loop."""

import pytest

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.models import Dag, Node
from leanflow_cli.workflows.prover.runtime import ProverRuntime


def controller(
    monkeypatch: pytest.MonkeyPatch, *, fail: str = ""
) -> tuple[ProverRuntime, list[str], list[bool]]:
    """Build only the promotion boundary with root-before-dependency ordering."""
    runtime = object.__new__(ProverRuntime)
    runtime.config = ProverConfig(search_order="top-down")
    runtime.dag = Dag(
        [
            Node(
                "root",
                "root",
                "theorem root : True",
                "Root.lean",
                "Root",
                dependencies=["a"],
                status="candidate",
                candidate=["exact a"],
            ),
            Node(
                "a",
                "a",
                "theorem a : True",
                "A.lean",
                "A",
                dependencies=["b"],
                status="candidate",
                candidate=["exact b"],
            ),
            Node(
                "b",
                "b",
                "theorem b : True",
                "B.lean",
                "B",
                dependencies=["leaf"],
                status="candidate",
                candidate=["exact leaf"],
            ),
            Node("leaf", "leaf", "theorem leaf : True", "Leaf.lean", "Leaf", status="proved"),
        ],
        ["root"],
    )
    accepted: list[str] = []
    persisted: list[bool] = []

    def accept(node: Node, candidate: list[str], job_id: str) -> bool:
        assert candidate and job_id == "controller"
        accepted.append(node.id)
        if node.id == fail:
            return False
        node.status = "proved"
        return True

    monkeypatch.setattr(runtime, "_accept", accept)
    monkeypatch.setattr(runtime, "_persist", lambda: persisted.append(True))
    return runtime, accepted, persisted


def test_promotion_reaches_a_fixed_point_without_another_job_or_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, accepted, persisted = controller(monkeypatch)
    runtime._promote_candidates()
    assert accepted == ["b", "a", "root"]
    assert all(node.status == "proved" for node in runtime.dag.nodes)
    assert persisted == [True]
    runtime._promote_candidates()
    assert persisted == [True], "Idle loop iterations must not rewrite the full durable graph"


def test_failed_candidate_stops_its_ancestors_but_does_not_spin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, accepted, _ = controller(monkeypatch, fail="a")
    runtime._promote_candidates()
    assert accepted == ["b", "a"]
    assert runtime.dag.by_id()["a"].status == "retry"
    assert runtime.dag.by_id()["root"].status == "candidate"
