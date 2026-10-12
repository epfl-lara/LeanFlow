"""Keep empty-response recovery finite and isolated from healthy proof work."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.runtime import ProverRuntime

RESPONSE_FAILURE = "Native conversation ended without a usable response."


class Verifier:
    """Record the independent candidate gate without invoking Lean or a provider."""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.checked: list[str] = []

    def check(self, node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        self.checked.append(node.id)
        return {"accepted": self.accepted}

    def final(self, files: list[Path]) -> dict[str, Any]:
        return {"accepted": self.accepted}

    def compile_module(self, relative: str) -> dict[str, Any]:
        return {"accepted": True}


def make_runtime(tmp_path: Path, *, accepted: bool = True) -> ProverRuntime:
    """Prepare independent goals with every model call replaced by a test function."""
    target = tmp_path / "Main.lean"
    target.write_text("theorem stalled : True := by sorry\ntheorem healthy : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[target],
        config=ProverConfig(mode="research", parallelism=2, job_api_calls=5, total_api_calls=100),
        session=lambda **kwargs: {
            "status": "complete",
            "api_calls": 1,
            "final_response": '{"action":"retry","rationale":"Test-selected retry"}',
        },
        verifier=Verifier(accepted),
    )
    runtime.state["design_plan_complete"] = True
    runtime._persist()
    return runtime


def stalled_result(proof: str = "") -> dict[str, Any]:
    return {
        "status": "response_stalled",
        "api_calls": 1,
        "error": RESPONSE_FAILURE,
        "final_response": json.dumps({"proof": proof}) if proof else "",
        "stop_reason": {"code": "response_stalled", "scope": "job", "message": RESPONSE_FAILURE},
    }


def test_stalled_job_consults_orchestrator_while_parallel_job_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = make_runtime(tmp_path)
    healthy_started, stalled_handled = threading.Event(), threading.Event()
    calls: list[str] = []
    original_handle = runtime._handle_result

    def handle(job: dict[str, Any], result: dict[str, Any]) -> None:
        original_handle(job, result)
        if result["status"] == "response_stalled":
            stalled_handled.set()

    def session(**kwargs: Any) -> dict[str, Any]:
        name = kwargs["context"]["assignment"]["name"]
        if kwargs["role"] == "orchestrator":
            calls.append("orchestrator")
            return {
                "status": "complete",
                "api_calls": 1,
                "final_response": '{"action":"continue","instructions":"Try exact True.intro."}',
            }
        calls.append(name)
        if name == "stalled" and calls.count(name) > 1:
            return {"status": "completed", "api_calls": 1, "final_response": '{"proof":"trivial"}'}
        if name == "stalled":
            assert healthy_started.wait(5)
            return stalled_result()
        assert name == "healthy"
        healthy_started.set()
        assert stalled_handled.wait(5)
        assert not runtime.cancelled.is_set()
        return {"status": "completed", "api_calls": 1, "final_response": '{"proof":"trivial"}'}

    monkeypatch.setattr(runtime, "session", session)
    monkeypatch.setattr(runtime, "_handle_result", handle)
    state = runtime.run()
    nodes = {node.name: node for node in runtime.dag.nodes}
    assert nodes["stalled"].status == "proved"
    assert nodes["healthy"].status == "proved"
    assert sorted(calls) == ["healthy", "orchestrator", "stalled", "stalled"]
    assert state["status"] == "completed"
    assert state["metrics"]["api_calls"] == 4
    assert state["metrics"]["decompositions"] == 1
    assert state["stop_reason"] is None
    assert not state.get("recovery_in_flight")
    assert (
        next(job for job in state["jobs"] if job["status"] == "response_stalled")["stop_reason"][
            "code"
        ]
        == "response_stalled"
    )


@pytest.mark.parametrize("accepted", [False, True])
def test_saved_candidate_is_checked_before_stall_blocks(tmp_path: Path, accepted: bool) -> None:
    runtime = make_runtime(tmp_path, accepted=accepted)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    node.status = "running"
    runtime._handle_result(job, stalled_result("trivial"))
    assert runtime.verifier.checked == [node.id]
    assert node.status == ("proved" if accepted else "retry")
    assert not runtime.state.get("recovery_in_flight")
    assert not runtime.cancelled.is_set()


def test_crash_after_accounting_enqueues_orchestrator_without_reopening_stalled_job(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    node.status = "running"
    runtime._finish_job(job, stalled_result())
    resumed = ProverRuntime(
        root=runtime.root,
        targets=runtime.targets,
        config=runtime.config,
        run_id=runtime.run_id,
        resume=True,
        session=lambda **kwargs: pytest.fail("A stalled job must not send a model request"),
        verifier=Verifier(),
    )
    assert resumed.dag.by_id()[node.id].status == "blocked"
    assert node.id not in resumed.resume_jobs
    assert resumed.state["recovery_in_flight"][node.id]["stage"] == "decide"
    assert resumed.state["jobs"][0]["response_recovery_enqueued"] is True
    assert resumed.state["jobs"][0]["result_processed"] is True


def test_deferred_candidate_rejection_calls_orchestrator_for_stalled_response(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path, accepted=False)
    node, prerequisite = runtime.dag.nodes
    node.dependencies = [prerequisite.id]
    job, _ = runtime._new_job("prover", node=node)
    node.status = "running"
    runtime._handle_result(job, stalled_result("trivial"))
    assert node.status == "candidate"
    assert runtime.verifier.checked == []
    prerequisite.status = "proved"
    runtime._promote_candidates()
    assert runtime.verifier.checked == [node.id]
    assert node.status == "retry"
    assert not runtime.state.get("recovery_in_flight")
    assert runtime.state["metrics"]["decompositions"] == 1


def test_negation_response_failure_returns_to_orchestrator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import negation_job

    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    runtime.state["recovery_in_flight"] = {node.id: {"stage": "negate", "screen": {}}}

    def negation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        job, _ = runtime._new_job("negation", node=node)
        runtime._finish_job(job, stalled_result())
        return {"certified": False, "notes": "No usable response"}

    monkeypatch.setattr(negation_job, "attempt_negation", negation)
    runtime._recover(node)
    assert node.status == "retry"
    assert not runtime.state.get("recovery_in_flight")
    assert not runtime.cancelled.is_set()
    assert len(runtime.state["jobs"]) == 2
    assert runtime.state["metrics"]["decompositions"] == 1


def test_stalled_negation_keeps_cached_proof_for_independent_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import negation_job

    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("negation", node=node)
    runtime._finish_job(job, stalled_result())
    runtime.state["negation_proofs"] = {node.id: "exact a_saved_proof"}
    runtime.state["recovery_in_flight"] = {node.id: {"stage": "negate", "screen": {}}}
    checked = []

    def check_cached(*args: Any, **kwargs: Any) -> dict[str, Any]:
        checked.append(runtime.state["negation_proofs"][node.id])
        return {"certified": False, "notes": "Saved proof was checked and rejected"}

    monkeypatch.setattr(negation_job, "attempt_negation", check_cached)
    runtime._recover(node)
    assert checked == ["exact a_saved_proof"]
    assert node.status == "retry"
    assert node.id not in runtime.state["negation_proofs"]
    assert runtime.state["recovery_decisions"][0]["action"] == "retry"
