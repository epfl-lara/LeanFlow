"""Pin negation submission authority, accounting, and interrupted-check recovery."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import agent_session, negation_job
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.negation import prepare_negation
from leanflow_cli.workflows.prover.negation_submission import SUBMISSION_CONTRACT
from leanflow_cli.workflows.prover.runtime import InfrastructureFailure, ProverRuntime
from leanflow_cli.workflows.prover.source import SourceDocument


class RecordingVerifier:
    """Record exact sources while returning a controlled kernel-check result."""

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.result: dict[str, Any] = {"accepted": True}

    def check(self, node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        self.sources.append(file.read_text())
        return dict(self.result)


def make_runtime(tmp_path: Path, **overrides: Any) -> tuple[ProverRuntime, RecordingVerifier]:
    """Build a real controller without invoking a model or Lean process."""
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    path = tmp_path / "Main.lean"
    path.write_text("import Std\n\ntheorem goal : False := by sorry\n")
    verifier = RecordingVerifier()
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(**{"mode": "research", "negation_api_calls": 3, **overrides}),
        session=lambda **kwargs: {
            "status": "completed",
            "api_calls": 1,
            "final_response": json.dumps({"proof": "intro h\n  exact h"}),
        },
        verifier=verifier,
    )
    return runtime, verifier


def test_negation_finishes_with_cached_literal_before_final_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the terminal job and its literal replacement in one durable outcome."""
    runtime, verifier = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    task = prepare_negation(tmp_path, node)
    assert task is not None
    proof = "intro h\n  exact h"
    check = verifier.check

    def inspect(node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        persisted = json.loads((runtime.store.directory / "state.json").read_text())
        assert persisted["negation_proofs"][runtime.dag.nodes[0].id] == proof
        assert persisted["jobs"][-1]["status"] == "completed"
        assert persisted["jobs"][-1]["accounted"] is True
        return check(node, file, **kwargs)

    monkeypatch.setattr(verifier, "check", inspect)
    outcome = negation_job.attempt_negation(runtime, node)

    assert outcome["certified"] is True
    assert verifier.sources == [SourceDocument(node.file, task.source).render({0: proof})]
    assert runtime.consumed == 1 and runtime.reserved == 0
    assert (tmp_path / "Main.lean").read_text().endswith("False := by sorry\n")


def test_actual_negation_check_deadline_preserves_proof_for_free_recheck(
    tmp_path: Path,
) -> None:
    """A process deadline cannot spend a second model allocation on resume."""
    runtime, verifier = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    verifier.result = {"accepted": False, "error_code": "check_timeout", "timed_out": True}

    with pytest.raises(InfrastructureFailure):
        negation_job.attempt_negation(runtime, node)

    assert runtime.state["negation_proofs"][node.id] == "intro h\n  exact h"
    verifier.result = {"accepted": True}
    outcome = negation_job.attempt_negation(runtime, node)
    assert outcome["certified"] is True
    assert len(runtime.state["jobs"]) == 1 and runtime.consumed == 1
    assert len(verifier.sources) == 2


def use_session_loop(runtime: ProverRuntime, monkeypatch: pytest.MonkeyPatch, request: Any) -> None:
    """Exercise the real controller/session boundary with a scripted provider."""
    monkeypatch.setattr(
        agent_session, "build_transport", lambda *args: SimpleNamespace(model="test")
    )
    monkeypatch.setattr(agent_session, "close_transport", lambda *args: None)
    monkeypatch.setattr(agent_session, "request_once", request)
    runtime.session = agent_session.run_session


def submission_response(proof: str, channel: str) -> dict[str, Any]:
    """Submit a candidate through the model's JSON or candidate-file interface."""
    if channel == "json":
        return {"role": "assistant", "content": json.dumps({"proof": proof})}
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "submit",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "candidate.txt", "content": proof}),
                },
            }
        ],
    }


@pytest.mark.parametrize("channel", ["json", "candidate_file"])
def test_negation_repairs_nested_by_within_same_job_and_checks_exact_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    """Feed the real assembled-source error back before allocating another job."""
    runtime, verifier = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    task = prepare_negation(tmp_path, node)
    assert task is not None
    bad = "by\n  intro h\n  exact h"
    good = "intro h\n  exact h"
    diagnostic = "Submission.lean:5:2: error: unexpected token 'by'; expected tactic"
    calls = []

    def request(_agent: Any, messages: list[dict[str, Any]], _timeout: float) -> Any:
        calls.append(messages)
        assert SUBMISSION_CONTRACT in messages[1]["content"]
        if len(calls) == 2:
            assert any(diagnostic in str(message.get("content")) for message in messages)
        return submission_response(bad if len(calls) == 1 else good, channel), {}

    def check(assigned: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        assert assigned.to_dict() == task.node.to_dict()
        assert not kwargs.get("skeleton")
        verifier.sources.append(file.read_text())
        if len(verifier.sources) == 1:
            return {"accepted": False, "kernel_profile": {"compile": {"stdout": diagnostic}}}
        return {"accepted": True, "axiom_profile_checked": True, "axiom_profile_axioms": []}

    use_session_loop(runtime, monkeypatch, request)
    monkeypatch.setattr(verifier, "check", check)
    outcome = negation_job.attempt_negation(runtime, node)

    assert outcome["certified"] is True
    assert len(calls) == 2 and len(runtime.state["jobs"]) == 1
    job = runtime.state["jobs"][0]
    assert job["api_calls"] == runtime.consumed == 2
    ledger_path = Path(job["workspace"]).parent / ".runtime" / job["id"] / "request-count.json"
    ledger = json.loads(ledger_path.read_text())
    assert (ledger["limit"], ledger["used"]) == (3, 2)
    assert runtime.state["negation_proofs"][node.id] == good
    document = SourceDocument(node.file, task.source)
    assert verifier.sources == [document.render({0: proof}) for proof in (bad, good, good)]
    assert (tmp_path / "Main.lean").read_text().endswith("False := by sorry\n")


def test_failed_negation_keeps_checker_diagnostics_without_model_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty final tool response cannot erase the exact-source rejection."""
    runtime, verifier = make_runtime(tmp_path)
    verifier.result = {
        "accepted": False,
        "kernel_profile": {"compile": {"stdout": "error: unexpected token 'by'; expected tactic"}},
    }
    use_session_loop(
        runtime,
        monkeypatch,
        lambda *args: (submission_response("by\n  intro h\n  exact h", "candidate_file"), {}),
    )
    outcome = negation_job.attempt_negation(runtime, runtime.dag.nodes[0])
    assert outcome["certified"] is False
    assert outcome["verification"] == verifier.result
    assert "unexpected token 'by'" in outcome["notes"]
    assert "not evidence that the claim is true" in outcome["notes"]
    assert len(runtime.state["jobs"]) == 1 and runtime.consumed == 3


@pytest.mark.parametrize(
    "proof", ["exact h\ntheorem extra : True := trivial", "native_decide", "sorry"]
)
def test_unsafe_negation_candidate_never_reaches_the_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: str
) -> None:
    """Apply the ordinary candidate guard to every negation submission channel."""
    runtime, verifier = make_runtime(tmp_path)
    use_session_loop(
        runtime,
        monkeypatch,
        lambda *args: (submission_response(proof, "candidate_file"), {}),
    )
    outcome = negation_job.attempt_negation(runtime, runtime.dag.nodes[0])
    assert outcome["certified"] is False
    assert outcome["verification"]["accepted"] is False
    assert verifier.sources == []
    assert runtime.state["negation_proofs"][runtime.dag.nodes[0].id] == ""


@pytest.mark.parametrize("calls", [1, 3])
def test_interrupted_json_submission_rechecks_on_resume_without_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: int
) -> None:
    """Retain a final-call JSON proof in the unfinished job across checker timeout."""
    runtime, verifier = make_runtime(tmp_path, negation_api_calls=calls, total_api_calls=calls)
    node = runtime.dag.nodes[0]
    request_count = 0

    def request(*args: Any) -> Any:
        nonlocal request_count
        request_count += 1
        assert request_count == 1
        return submission_response("intro h\n  exact h", "json"), {}

    use_session_loop(runtime, monkeypatch, request)
    verifier.result = {"accepted": False, "error_code": "check_timeout", "timed_out": True}
    with pytest.raises(InfrastructureFailure):
        negation_job.attempt_negation(runtime, node)
    persisted = json.loads((runtime.store.directory / "state.json").read_text())
    assert node.id not in persisted.get("negation_proofs", {})
    assert persisted["jobs"][0]["status"] == "environment_error"
    assert persisted["jobs"][0]["negation_pending_submission"]["proof"] == "intro h\n  exact h"

    verifier.result = {"accepted": True}
    resumed = ProverRuntime(
        root=runtime.root,
        targets=runtime.targets,
        config=runtime.config,
        run_id=runtime.run_id,
        session=agent_session.run_session,
        verifier=verifier,
        resume=True,
    )
    outcome = negation_job.attempt_negation(resumed, resumed.dag.nodes[0])
    assert outcome["certified"] is True
    assert len(verifier.sources) == 3
    assert request_count == resumed.consumed == 1
    assert len(resumed.state["jobs"]) == 1 and resumed.reserved == 0
    assert "negation_pending_submission" not in resumed.state["jobs"][0]


def test_successful_submission_survives_interrupted_final_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep accepted JSON bytes until terminal accounting durably owns the proof."""
    runtime, verifier = make_runtime(tmp_path, negation_api_calls=1)
    use_session_loop(
        runtime,
        monkeypatch,
        lambda *args: (submission_response("intro h\n  exact h", "json"), {}),
    )
    real_event = runtime._session_event

    def interrupted_event(job: dict[str, Any], kind: str, details: dict[str, Any]) -> None:
        if kind == "job-session-end":
            raise InfrastructureFailure(
                "interrupted while publishing final event", status="interrupted"
            )
        real_event(job, kind, details)

    monkeypatch.setattr(runtime, "_session_event", interrupted_event)
    with pytest.raises(InfrastructureFailure):
        negation_job.attempt_negation(runtime, runtime.dag.nodes[0])
    assert len(verifier.sources) == 1
    assert runtime.state["jobs"][0]["negation_pending_submission"]["proof"] == "intro h\n  exact h"
    resumed = ProverRuntime(
        root=runtime.root,
        targets=runtime.targets,
        config=runtime.config,
        run_id=runtime.run_id,
        session=agent_session.run_session,
        verifier=verifier,
        resume=True,
    )
    outcome = negation_job.attempt_negation(resumed, resumed.dag.nodes[0])
    assert outcome["certified"] is True
    assert len(verifier.sources) == 3 and resumed.consumed == 1


def test_resumed_rejection_restores_json_candidate_before_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Show the retained candidate alongside its new diagnostic without resetting calls."""
    runtime, verifier = make_runtime(tmp_path)
    bad = "by\n  intro h\n  exact h"
    good = "intro h\n  exact h"
    diagnostic = "unexpected token 'by'; expected tactic"
    request_count = 0

    def request(_agent: Any, messages: list[dict[str, Any]], _timeout: float) -> Any:
        nonlocal request_count
        request_count += 1
        if request_count == 2:
            retained = [
                json.loads(message["content"])
                for message in messages
                if message["role"] == "assistant"
            ]
            assert retained == [{"proof": bad, "notes": ""}]
            assert any(diagnostic in str(message.get("content")) for message in messages)
        return submission_response(bad if request_count == 1 else good, "json"), {}

    def check(node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        verifier.sources.append(file.read_text())
        if len(verifier.sources) == 1:
            return {"accepted": False, "error_code": "check_timeout"}
        if len(verifier.sources) == 2:
            return {"accepted": False, "error": diagnostic}
        return {"accepted": True}

    use_session_loop(runtime, monkeypatch, request)
    monkeypatch.setattr(verifier, "check", check)
    with pytest.raises(InfrastructureFailure):
        negation_job.attempt_negation(runtime, runtime.dag.nodes[0])
    resumed = ProverRuntime(
        root=runtime.root,
        targets=runtime.targets,
        config=runtime.config,
        run_id=runtime.run_id,
        session=agent_session.run_session,
        verifier=verifier,
        resume=True,
    )
    outcome = negation_job.attempt_negation(resumed, resumed.dag.nodes[0])
    assert outcome["certified"] is True
    assert len(verifier.sources) == 4
    assert request_count == resumed.consumed == 2
    assert len(resumed.state["jobs"]) == 1
    assert resumed.state["negation_proofs"][resumed.dag.nodes[0].id] == good
    assert "negation_pending_submission" not in resumed.state["jobs"][0]
