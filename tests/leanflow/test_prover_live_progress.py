"""Exercise live publication while a source transaction owns the controller lock."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import agent_session
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.job_controller import session_event
from leanflow_cli.workflows.prover.live_progress import _snapshot
from leanflow_cli.workflows.prover.runtime import (
    BudgetExhausted,
    InfrastructureFailure,
    ProverRuntime,
)
from leanflow_cli.workflows.prover.session_tools import SessionTools
from leanflow_cli.workflows.prover.stop_reason import campaign_stop_reason


def make_runtime(root: Path) -> ProverRuntime:
    """Create a protected goal without starting Lean or a model."""
    target = root / "Main.lean"
    target.write_text("theorem goal : True := by sorry\n")
    (root / "lakefile.toml").write_text('name = "Demo"\n')
    return ProverRuntime(
        root=root,
        targets=[target],
        config=ProverConfig(total_api_calls=100, job_api_calls=20),
        session=lambda **_: {},
        verifier=object(),
    )


def test_committed_snapshot_preserves_source_checkpoint(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    state = json.loads((runtime.store.directory / "state.json").read_text())
    checkpoint = json.loads((runtime.store.directory / state["source_checkpoint"]).read_text())
    assert checkpoint["Main.lean"]["replacements"] == {}
    assert state["dag"]["nodes"][0]["status"] == "pending"


def test_cancelled_final_event_preserves_invoke_outcome_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    runtime = make_runtime(tmp_path)
    runtime.session = agent_session.run_session
    monkeypatch.setattr(agent_session, "build_transport", lambda *_: SimpleNamespace(model="test"))
    monkeypatch.setattr(agent_session, "close_transport", lambda *_: None)
    monkeypatch.setattr(
        agent_session,
        "request_once",
        lambda *_: (
            {"role": "assistant", "content": '{"proof":"trivial"}'},
            {"input_tokens": 17, "output_tokens": 5},
        ),
    )

    def timed_out(*_: Any) -> dict[str, Any]:
        runtime.cancelled.set()
        raise BudgetExhausted("campaign wall-clock budget exhausted", code="campaign_wall_time")

    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.job_controller.candidate_feedback", timed_out
    )
    job, context = runtime._new_job("prover", node=runtime.dag.nodes[0])
    result = runtime._invoke(job, context, "Prove the goal")
    assert result["status"] == "timeout", result
    assert result["stop_reason"]["code"] == "campaign_wall_time"
    assert result["api_calls"] == 1
    assert result["input_tokens"] == 17 and result["output_tokens"] == 5
    assert job["input_tokens"] == 17 and job["output_tokens"] == 5


def test_worker_reports_during_source_lock_without_publishing_tentative_source(
    tmp_path: Path,
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "running"
    job, _ = runtime._new_job("prover", node=node)
    before = json.loads((runtime.store.directory / "state.json").read_text())
    done = threading.Event()
    errors: list[BaseException] = []

    def update() -> None:
        try:
            session_event(runtime, job, "api-response", {"api_calls": 3, "input_tokens": 27})
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    with runtime.lock:
        # Integration mutates an in-memory document before Lean accepts the new module.
        runtime.documents["Main.lean"].replacements["0"] = "exact True.intro"
        worker = threading.Thread(target=update)
        worker.start()
        timely = done.wait(1)
        snapshot = json.loads((runtime.store.directory / "state.json").read_text())
    worker.join(timeout=2)
    assert timely, "job progress was blocked by a long source transaction"
    assert not errors
    assert snapshot["source_checkpoint"] == before["source_checkpoint"]
    assert snapshot["dag"]["nodes"][0]["status"] == "running"
    assert snapshot["metrics"]["api_calls"] == 3
    assert snapshot["metrics"]["available_api_calls"] == 80
    assert snapshot["metrics"]["remaining_reserved_api_calls"] == 17
    assert snapshot["updated_at"] > before["updated_at"]


def test_operation_is_visible_and_never_marks_unintegrated_node_proved(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "running"
    runtime._persist()
    observed: list[dict[str, Any]] = []

    def check() -> dict[str, Any]:
        observed.append(json.loads((runtime.store.directory / "state.json").read_text()))
        return {"accepted": False, "error": "fixture rejection"}

    runtime.progress.call("submission_check", "Checking submitted proof", check, node_id=node.id)
    assert observed[0]["dag"]["nodes"][0]["status"] == "verifying"
    assert observed[0]["operations"][-1]["status"] == "running"
    after = json.loads((runtime.store.directory / "state.json").read_text())
    assert after["dag"]["nodes"][0]["status"] == "running"
    assert after["operations"][-1]["status"] == "failed"
    assert node.status == "running"


def test_local_call_exhaustion_does_not_claim_total_budget_exhaustion(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    runtime._finish_job(job, {"status": "budget_exhausted", "api_calls": 20})
    assert job["stop_reason"]["scope"] == "job"
    reason = campaign_stop_reason(runtime, "budget_exhausted")
    assert reason is not None and reason["scope"] == "scheduler"
    assert reason["remaining_api_calls"] == 80
    runtime.state["metrics"]["api_calls"] = 100
    reason = campaign_stop_reason(runtime, "budget_exhausted")
    assert reason is not None and reason["code"] == "campaign_api_calls"


def test_scratch_timeout_uses_config_and_remaining_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import check_process

    workspace = tmp_path / "job"
    workspace.mkdir()
    scratch = workspace / "Scratch.lean"
    scratch.write_text("example : True := by trivial\n")
    captured: dict[str, Any] = {}

    def check(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(check_process, "check_scratch", check)
    tools = SessionTools(
        role="prover",
        project_root=tmp_path,
        workspace=workspace,
        context={"scratch_file": str(scratch)},
        timeout_s=1200,
        deadline=time.monotonic() + 1300,
    )
    tools.invoke("lean_check", {})
    assert captured["timeout_s"] == 1200
    tools.deadline = time.monotonic() + 25
    tools.invoke("lean_check", {})
    assert 24 < captured["timeout_s"] <= 25


@pytest.mark.parametrize("accepted", [True, False])
def test_integration_publishes_staged_diff_and_keeps_transaction_recoverable(
    tmp_path: Path, accepted: bool
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "running"
    runtime._persist()
    checkpoint = runtime.state["source_checkpoint"]

    class Verifier:
        def check(self, *args: Any) -> dict[str, Any]:
            return {"accepted": True}

        def compile_module(self, relative: str) -> dict[str, Any]:
            snapshot = json.loads((runtime.store.directory / "state.json").read_text())
            assert snapshot["source_checkpoint"] == checkpoint
            assert snapshot["dag"]["nodes"][0]["status"] == "integrating"
            assert snapshot["changes"][-1]["pending"] is True
            assert snapshot["changes"][-1]["status"] == "staged"
            return {"accepted": accepted}

    runtime.verifier = Verifier()
    if accepted:
        assert runtime._accept(node, ["exact True.intro"], "fixture")
        assert node.status == "proved"
        assert runtime.state["changes"][-1]["pending"] is False
    else:
        with pytest.raises(InfrastructureFailure, match="could not be compiled"):
            runtime._accept(node, ["exact True.intro"], "fixture")
        assert node.status == "candidate"
        assert runtime.state["changes"][-1]["status"] == "rolled_back"
        assert runtime.state["changes"][-1]["pending"] is False
        assert (tmp_path / "Main.lean").read_text() == "theorem goal : True := by sorry\n"
    runtime._assert_sources()


def test_metadata_snapshot_survives_concurrent_dictionary_resize(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.state["metadata_fixture"] = {str(i): i for i in range(1000)}
    stop = threading.Event()

    def mutate() -> None:
        while not stop.is_set():
            runtime.state["temporary"] = {"value": 1}
            runtime.state["metadata_fixture"]["extra"] = 2
            runtime.state.pop("temporary", None)
            runtime.state["metadata_fixture"].pop("extra", None)

    thread = threading.Thread(target=mutate)
    thread.start()
    try:
        for _ in range(50):
            copied = _snapshot(runtime.state)
            assert copied["metadata_fixture"]["500"] == 500
        runtime.progress.publish()
    finally:
        stop.set()
        thread.join(timeout=2)


def test_resume_keeps_candidate_under_verification_overlay(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status, node.candidate, node.attempts = "candidate", ["trivial"], 50
    runtime._persist()
    runtime.progress.begin("submission_check", "Checking candidate", node_id=node.id)
    snapshot = json.loads((runtime.store.directory / "state.json").read_text())
    assert snapshot["dag"]["nodes"][0]["status"] == "verifying"
    restored = ProverRuntime(
        root=tmp_path,
        targets=[tmp_path / "Main.lean"],
        config=runtime.config,
        verifier=object(),
        session=lambda **_: {},
        run_id=runtime.run_id,
        resume=True,
    )
    assert restored.dag.nodes[0].status == "candidate"
    assert restored.dag.nodes[0].candidate == ["trivial"]


def test_failed_later_proof_keeps_prior_committed_change(tmp_path: Path) -> None:
    target = tmp_path / "Main.lean"
    target.write_text("theorem a : True := by sorry\ntheorem b : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[target],
        config=ProverConfig(),
        session=lambda **_: {},
        verifier=object(),
    )

    class Verifier:
        accepted = True

        def check(self, *args: Any) -> dict[str, Any]:
            return {"accepted": True}

        def compile_module(self, relative: str) -> dict[str, Any]:
            return {"accepted": self.accepted}

    runtime.verifier = Verifier()
    assert runtime._accept(runtime.dag.nodes[0], ["trivial"], "first")
    runtime.verifier.accepted = False
    with pytest.raises(InfrastructureFailure):
        runtime._accept(runtime.dag.nodes[1], ["trivial"], "second")
    assert len(runtime.state["changes"]) == 1
    change = runtime.state["changes"][0]
    assert change["status"] == "modified" and change["agent_id"] == "first"
    assert change["last_attempt"]["status"] == "rolled_back"
    assert target.read_text() == "theorem a : True := by trivial\ntheorem b : True := by sorry\n"


@pytest.mark.parametrize("status", ["verification_failed", "source_conflict", "disproved"])
def test_budget_totals_do_not_hide_terminal_cause(tmp_path: Path, status: str) -> None:
    runtime = make_runtime(tmp_path)
    runtime.state["metrics"]["api_calls"] = runtime.config.total_api_calls
    assert campaign_stop_reason(runtime, status)["code"] == status
