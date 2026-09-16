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
from leanflow_cli.workflows.prover.session_response_guard import (
    RESPONSE_FAILURE,
    ProverResponseGuard,
)
from tests.leanflow.test_prover_sessions import run_fake


def guard(tmp_path: Path, role: str = "prover") -> ProverResponseGuard:
    """Create or reopen the same job's durable response guard."""
    return ProverResponseGuard(tmp_path / ".runtime/job", role=role)


@pytest.mark.parametrize("role", ["prover", "negation"])
@pytest.mark.parametrize("finish_reason", ["stop", "length", "incomplete"])
def test_empty_response_recovers_once_then_stops(
    tmp_path: Path, role: str, finish_reason: str
) -> None:
    current = guard(tmp_path, role)
    response = {"content": "", "finish_reason": finish_reason, "reasoning": "private derivation"}
    first = current.observe(response, candidate_ready=False, used=89, limit=150)
    second = current.observe(response, candidate_ready=False, used=90, limit=150)
    assert first is not None and first.action == "recovery"
    assert second is not None and second.action == "stop"
    assert second.fingerprint == first.fingerprint
    assert current.stopped
    assert current.stop_reason["code"] == "response_stalled"
    assert current.stop_reason["scope"] == "job"
    assert current.stop_reason["unusable_responses"] == 2
    for text in (current.path.read_text(), first.message, second.message):
        assert "private derivation" not in text


def test_different_truncated_responses_do_not_refresh_recovery(tmp_path: Path) -> None:
    current = guard(tmp_path)
    first = current.observe(
        {"content": "partial attempt A", "finish_reason": "length"},
        candidate_ready=False,
        used=1,
        limit=150,
    )
    second = current.observe(
        {"content": "partial attempt B", "finish_reason": "length"},
        candidate_ready=False,
        used=2,
        limit=150,
    )
    assert first is not None and first.action == "recovery"
    assert second is not None and second.action == "stop"
    assert first.fingerprint != second.fingerprint


def test_repeated_complete_non_candidate_is_bounded(tmp_path: Path) -> None:
    current = guard(tmp_path)
    response = {"content": "I will keep considering the same idea.", "finish_reason": "stop"}
    assert current.observe(response, candidate_ready=False, used=1, limit=150) is None
    first = current.observe({**response, "id": "new-id"}, candidate_ready=False, used=2, limit=150)
    second = current.observe(response, candidate_ready=False, used=3, limit=150)
    assert first is not None and first.action == "recovery" and first.reason == "duplicate"
    assert second is not None and second.action == "stop"


def test_changed_complete_reasoning_is_not_declared_stalled(tmp_path: Path) -> None:
    current = guard(tmp_path)
    for attempt in range(4):
        assert (
            current.observe(
                {"content": f"Different finding {attempt}", "finish_reason": "stop"},
                candidate_ready=False,
                used=attempt + 1,
                limit=150,
            )
            is None
        )
    assert not current.stopped


@pytest.mark.parametrize("candidate", [False, True])
def test_actual_action_or_candidate_clears_interruption(tmp_path: Path, candidate: bool) -> None:
    current = guard(tmp_path)
    assert current.observe({}, candidate_ready=False, used=1, limit=150) is not None
    action = {"finish_reason": "length", "tool_calls": [] if candidate else [{"id": "tool1"}]}
    assert (
        current.observe(
            action, candidate_ready=candidate, tool_executed=not candidate, used=2, limit=150
        )
        is None
    )
    reopened = guard(tmp_path)
    assert not reopened.stopped
    notice = reopened.observe({}, candidate_ready=False, used=3, limit=150)
    assert notice is not None and notice.action == "recovery"


def test_resume_does_not_buy_another_recovery(tmp_path: Path) -> None:
    current = guard(tmp_path)
    current.observe({}, candidate_ready=False, used=1, limit=150)
    for _ in range(3):
        reopened = guard(tmp_path)
        assert reopened.stopped
        notice = reopened.observe({}, candidate_ready=False, used=2, limit=150)
        assert notice is not None and notice.action == "stop"


def test_resume_retains_complete_response_fingerprints(tmp_path: Path) -> None:
    response = {"content": "The same non-candidate."}
    assert guard(tmp_path).observe(response, candidate_ready=False, used=1, limit=150) is None
    notice = guard(tmp_path).observe(response, candidate_ready=False, used=2, limit=150)
    assert notice is not None and notice.action == "recovery" and notice.reason == "duplicate"


def test_no_recovery_beyond_original_allocation(tmp_path: Path) -> None:
    current = guard(tmp_path)
    notice = current.observe({}, candidate_ready=False, used=150, limit=150)
    assert notice is not None and notice.action == "stop"


@pytest.mark.parametrize("saved", ["broken", "{}", '"not a ledger"', " " * 8193])
def test_damaged_guard_ledger_fails_closed(tmp_path: Path, saved: str) -> None:
    current = guard(tmp_path)
    current.path.parent.mkdir(parents=True)
    current.path.write_text(saved)
    reopened = guard(tmp_path)
    assert reopened.stopped
    assert reopened.stop_reason["response_kind"] == "invalid_recovery_state"


def test_report_roles_keep_their_separate_report_recovery(tmp_path: Path) -> None:
    current = guard(tmp_path, "orchestrator")
    for call in range(4):
        assert current.observe({}, candidate_ready=False, used=call + 1, limit=150) is None
    assert not current.path.exists()


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
            "final_response": '{"action":"stop","rationale":"Test-selected stop"}',
        },
        verifier=Verifier(accepted),
    )
    runtime.state["design_plan_complete"] = True
    runtime._persist()
    return runtime


def stalled_result(proof: str = "") -> dict[str, Any]:
    return {
        "status": "response_stalled",
        "api_calls": 2,
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
            return {"status": "complete", "api_calls": 1, "final_response": '{"action":"stop"}'}
        calls.append(name)
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
    assert nodes["stalled"].status == "blocked"
    assert nodes["healthy"].status == "proved"
    assert sorted(calls) == ["healthy", "orchestrator", "stalled"]
    assert state["status"] == "blocked"
    assert state["metrics"]["api_calls"] == 4
    assert state["metrics"]["decompositions"] == 1
    assert state["stop_reason"]["code"] == "no_runnable_obligations"
    assert "call budget is not exhausted" in state["stop_reason"]["message"]
    assert "recovery budget spent" not in state["stop_reason"]["message"]
    assert "replanning declined" not in state["stop_reason"]["message"]
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
    assert node.status == ("proved" if accepted else "blocked")
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
    assert node.status == "blocked"
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
    assert node.status == "blocked"
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
    assert node.status == "blocked"
    assert node.id not in runtime.state["negation_proofs"]
    assert runtime.state["recovery_decisions"][0]["action"] == "stop"


def test_real_session_stops_65k_reasoning_only_loop_and_resume_spends_no_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = []
    private_reasoning = "private derivation repeated " * 10_000

    def request(_agent: Any, messages: Any, _timeout: float) -> tuple[dict[str, Any], Any]:
        requests.append(json.loads(json.dumps(messages)))
        return {
            "role": "assistant",
            "content": "",
            "reasoning": private_reasoning,
            "finish_reason": "length",
        }, {"input_tokens": 10, "output_tokens": 65536}

    options = {"api_budget": 150, "config": {"context_tokens": 256000, "max_output_tokens": 65536}}
    result = run_fake(tmp_path, monkeypatch, request, **options)
    assert result["status"] == "response_stalled"
    assert result["api_calls"] == result["new_api_calls"] == len(requests) == 2
    assert result["output_tokens"] == 2 * 65536
    assert result["stop_reason"]["scope"] == "job"
    assert "single recovery request" in json.dumps(requests[1])
    assert "private derivation repeated" not in json.dumps(requests[1])
    assert private_reasoning not in json.dumps(result)
    resumed = run_fake(tmp_path, monkeypatch, request, **options)
    assert resumed["status"] == "response_stalled"
    assert resumed["api_calls"] == 2 and resumed["new_api_calls"] == 0
    assert len(requests) == 2


@pytest.mark.parametrize("use_tool", [False, True])
def test_real_session_recovers_into_an_actual_proof_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_tool: bool
) -> None:
    calls = 0

    def request(*_args: Any) -> tuple[dict[str, Any], Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"role": "assistant", "content": "", "finish_reason": "length"}, {}
        if calls == 2 and use_tool:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "save_findings",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "PLAN_job.md", "content": "Use trivial"}
                            ),
                        },
                    }
                ],
            }, {}
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    result = run_fake(tmp_path, monkeypatch, request, api_budget=150)
    assert result["status"] == "completed"
    assert result["api_calls"] == calls == (3 if use_tool else 2)
    assert not guard(tmp_path).stopped
    if use_tool:
        assert (tmp_path / "job/PLAN_job.md").read_text() == "Use trivial"


def test_real_session_stale_rejected_candidate_does_not_bypass_response_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "job"
    workspace.mkdir()
    (workspace / "candidate.txt").write_text("invalid_proof")
    checks = []
    calls = 0

    def feedback(text: str) -> dict[str, Any]:
        checks.append(text)
        return {"accepted": False, "error": "unknown identifier"}

    def request(*_args: Any) -> tuple[dict[str, Any], Any]:
        nonlocal calls
        calls += 1
        return {"role": "assistant", "content": "", "finish_reason": "length"}, {}

    result = run_fake(
        tmp_path, monkeypatch, request, api_budget=150, config={"_candidate_feedback": feedback}
    )
    assert result["status"] == "response_stalled"
    assert result["api_calls"] == calls == len(checks) == 2
    assert (workspace / "candidate.txt").read_text() == "invalid_proof"


def test_real_session_accepted_pending_candidate_clears_interrupted_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard(tmp_path).observe({}, candidate_ready=False, used=1, limit=150)
    result = run_fake(
        tmp_path,
        monkeypatch,
        lambda *_: pytest.fail("A pending candidate must be checked without a model call"),
        api_budget=150,
        config={
            "_pending_submission_response": '{"proof":"trivial"}',
            "_candidate_feedback": lambda text: {"accepted": True},
        },
    )
    assert result["status"] == "completed"
    assert result["new_api_calls"] == 0
    assert not guard(tmp_path).stopped


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("write_file", "{"),
        ("nonexistent_tool", "{}"),
        ("write_file", '{"path":"../outside.lean","content":"denied"}'),
        ("write_file", "{}"),
    ],
)
def test_alternating_reasoning_only_and_invalid_tool_cannot_reset_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, arguments: str
) -> None:
    calls = 0

    def request(*_args: Any) -> tuple[dict[str, Any], Any]:
        nonlocal calls
        calls += 1
        if calls % 2:
            return {
                "role": "assistant",
                "content": "",
                "reasoning": "No usable action yet.",
                "finish_reason": "length",
            }, {"output_tokens": 65536}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "bad", "function": {"name": name, "arguments": arguments}}],
        }, {"output_tokens": 10}

    result = run_fake(tmp_path, monkeypatch, request, api_budget=8)
    assert result["status"] == "response_stalled"
    assert result["api_calls"] == calls == 2
    assert result["stop_reason"]["response_kind"] == "invalid_tool_batch"
    assert not (tmp_path / "outside.lean").exists()
    events = [json.loads(line) for line in (tmp_path / "job.jsonl").read_text().splitlines()]
    assert sum(item.get("type") == "tool-result" for item in events) == 1


def test_invalid_tool_batch_replies_are_paired_before_recovery_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def request(_agent: Any, messages: Any, _timeout: float) -> tuple[dict[str, Any], Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "bad_json", "function": {"name": "write_file", "arguments": "{"}},
                    {"id": "unknown", "function": {"name": "unavailable", "arguments": "{}"}},
                ],
            }, {}
        request_index = next(i for i, item in enumerate(messages) if item.get("tool_calls"))
        replies = messages[request_index + 1 : request_index + 3]
        assert [item["role"] for item in replies] == ["tool", "tool"]
        assert [item["tool_call_id"] for item in replies] == ["bad_json", "unknown"]
        assert all(json.loads(item["content"])["success"] is False for item in replies)
        assert "single recovery request" in messages[request_index + 3]["content"]
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    result = run_fake(tmp_path, monkeypatch, request, api_budget=8)
    assert result["status"] == "completed" and calls == 2


def test_executed_lean_rejection_clears_response_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import check_process

    workspace = tmp_path / "job"
    workspace.mkdir()
    (workspace / "Scratch.lean").write_text("theorem goal : True := by exact False.elim\n")
    checks, calls = [], []

    def check(**kwargs: Any) -> dict[str, Any]:
        checks.append(kwargs)
        return {"success": True, "ok": False, "messages": [{"message": "unsolved goals"}]}

    def request(_agent: Any, messages: Any, _timeout: float) -> tuple[dict[str, Any], Any]:
        calls.append(json.loads(json.dumps(messages)))
        if len(calls) in {1, 3}:
            return {"role": "assistant", "content": "", "finish_reason": "length"}, {}
        if len(calls) == 2:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "check_rejected",
                        "function": {"name": "lean_check", "arguments": '{"file":"Scratch.lean"}'},
                    }
                ],
            }, {}
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    monkeypatch.setattr(check_process, "check_scratch", check)
    result = run_fake(tmp_path, monkeypatch, request, api_budget=8)
    assert result["status"] == "completed"
    assert result["api_calls"] == len(calls) == 4
    assert len(checks) == 1
    assert "unsolved goals" in json.dumps(calls[2])
    assert not guard(tmp_path).stopped


@pytest.mark.parametrize(
    ("computation_status", "expected_calls", "expected_status"),
    [
        ("empirical_compute_error", 4, "completed"),
        ("empirical_compute_denied", 2, "response_stalled"),
    ],
)
def test_executed_computation_error_counts_but_capability_denial_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    computation_status: str,
    expected_calls: int,
    expected_status: str,
) -> None:
    from leanflow_cli.workflows.prover import session_tools

    calls = 0

    def request(*_args: Any) -> tuple[dict[str, Any], Any]:
        nonlocal calls
        calls += 1
        if calls in {1, 3}:
            return {"role": "assistant", "content": "", "finish_reason": "length"}, {}
        if calls == 2:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "compute",
                        "function": {"name": "compute", "arguments": '{"program":"print(1 // 0)"}'},
                    }
                ],
            }, {}
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    monkeypatch.setattr(
        session_tools,
        "run_computation",
        lambda *_: {
            "success": False,
            "status": computation_status,
            "error": "Computation feedback",
        },
    )
    result = run_fake(tmp_path, monkeypatch, request, role="negation", api_budget=8)
    assert result["status"] == expected_status
    assert result["api_calls"] == calls == expected_calls
