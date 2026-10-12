"""Verify native-loop delegation, job authority, and global-only invocation accounting."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import HarnessResult
from leanflow_cli.workflows.prover.agent_session import _map_paths, run_session
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.native_bridge import NativeBridge
from leanflow_cli.workflows.prover.native_snapshot import create_snapshot, remap_project_text
from leanflow_cli.workflows.prover.runtime import (
    BudgetExhausted,
    InfrastructureFailure,
    ProverRuntime,
)
from leanflow_cli.workflows.prover.session_tools import SessionTools
from tests.leanflow.test_prover_runtime import Verifier, project


def test_native_defaults_leave_global_limits_and_model_settings_unset() -> None:
    config = ProverConfig.from_env({"LEANFLOW_NATIVE_PROVIDER": "lemex"})
    assert (config.agent_calls, config.tokens, config.wall_time_s, config.timeout_s) == (None,) * 4
    assert (config.model, config.reasoning_effort, config.provider) == ("", "", "lemex")
    assert ProverConfig.from_env({"LEANFLOW_PROVER_AGENT_CALLS": "0"}).agent_calls == 0


@pytest.mark.parametrize("launch_provider", ["", "codex", "claude", "lemex"])
@pytest.mark.parametrize("prover_provider", ["", "codex", "claude", "lemex"])
@pytest.mark.parametrize("explicit", [False, True])
def test_prover_only_inherits_launch_model_choices_within_the_same_backend(
    launch_provider: str, prover_provider: str, explicit: bool
) -> None:
    config = ProverConfig.from_env(
        {
            "LEANFLOW_NATIVE_PROVIDER": launch_provider,
            "LEANFLOW_PROVER_PROVIDER": prover_provider,
            "LEANFLOW_NATIVE_MODEL": "launch-model",
            "LEANFLOW_NATIVE_REASONING_EFFORT": "high",
            "LEANFLOW_PROVER_MODEL": "prover-model" if explicit else "",
            "LEANFLOW_PROVER_REASONING_EFFORT": "medium" if explicit else "",
        }
    )
    same_backend = not prover_provider or prover_provider == (launch_provider or "codex")
    assert config.provider == (prover_provider or launch_provider or "codex")
    assert config.model == ("prover-model" if explicit else "launch-model" if same_backend else "")
    assert config.reasoning_effort == ("medium" if explicit else "high" if same_backend else "")


@pytest.mark.parametrize("model", ["", "launch-model", "different-model"])
@pytest.mark.parametrize("effort", ["", "medium"])
def test_new_role_model_uses_native_effort_unless_explicitly_overridden(
    model: str, effort: str
) -> None:
    config = ProverConfig.from_env(
        {
            "LEANFLOW_NATIVE_PROVIDER": "lemex",
            "LEANFLOW_NATIVE_MODEL": "launch-model",
            "LEANFLOW_NATIVE_REASONING_EFFORT": "xhigh",
            "LEANFLOW_PROVER_MODEL": model,
            "LEANFLOW_PROVER_REASONING_EFFORT": effort,
        }
    )
    inherited_effort = "xhigh" if model in {"", "launch-model"} else ""
    assert config.reasoning_effort == (effort or inherited_effort)
    orchestrator = ProverConfig(
        provider="lemex",
        model="launch-model",
        reasoning_effort="xhigh",
        orchestrator_model=model,
        orchestrator_reasoning_effort=effort,
    )
    for role in ("orchestrator", "review", "research"):
        assert orchestrator.to_mapping(role)["reasoning_effort"] == (effort or inherited_effort)


@pytest.mark.parametrize("provider", ["lemex", "codex", ""])
@pytest.mark.parametrize("orchestrator", ["codex", "claude", "lemex", ""])
@pytest.mark.parametrize("explicit", [False, True])
def test_orchestrator_only_inherits_model_choices_within_the_same_backend(
    monkeypatch: pytest.MonkeyPatch, provider: str, orchestrator: str, explicit: bool
) -> None:
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "lemex")
    config = ProverConfig(
        provider=provider,
        orchestrator_provider=orchestrator,
        model="rcp-model",
        reasoning_effort="high",
        orchestrator_model="native-review-model" if explicit else "",
        orchestrator_reasoning_effort="medium" if explicit else "",
    )
    effective_provider = provider or "lemex"
    same_backend = (orchestrator or effective_provider) == effective_provider
    for role in ("orchestrator", "review", "research"):
        selected = config.to_mapping(role)
        assert selected["model"] == (
            "native-review-model" if explicit else "rcp-model" if same_backend else ""
        )
        assert selected["reasoning_effort"] == (
            "medium" if explicit else "high" if same_backend else ""
        )
        assert selected["provider"] == (orchestrator or provider)
    assert config.to_mapping("prover")["model"] == "rcp-model"
    assert config.to_mapping("prover")["reasoning_effort"] == "high"


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_invalid_global_limits_are_rejected(value: Any) -> None:
    with pytest.raises(ValueError):
        ProverConfig(wall_time_s=value)


def test_nested_snapshot_paths_are_mapped_once_and_remain_stable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    workspace = root / ".leanflow/workflow-state/prover/run/jobs/prover_00001"
    snapshot = workspace / "project"
    original = {
        "project_root": str(root),
        "scratch": str(workspace / "Scratch.lean"),
        "prompt": f"Read {workspace / 'Scratch.lean'} and {root / 'Main.lean'}.",
        "already_private": str(snapshot / ".leanflow-job/Scratch.lean"),
    }
    mapped = _map_paths(original, root, workspace, snapshot)
    assert mapped["project_root"] == str(snapshot)
    assert mapped["scratch"] == str(snapshot / ".leanflow-job/Scratch.lean")
    assert mapped["prompt"] == (
        f"Read {snapshot / '.leanflow-job/Scratch.lean'} and {snapshot / 'Main.lean'}."
    )
    assert mapped["already_private"] == mapped["scratch"]
    assert _map_paths(mapped, root, workspace, snapshot) == mapped
    sibling = str(root) + "2/Main.lean"
    shared = remap_project_text(f"{root}/Main.lean {snapshot}/Main.lean {sibling}", root, snapshot)
    assert shared == f"{snapshot}/Main.lean {snapshot}/Main.lean {sibling}"
    assert remap_project_text(shared, root, snapshot) == shared


def test_snapshot_has_independent_files_and_no_private_or_compiled_target_bytes(
    tmp_path: Path,
) -> None:
    original = tmp_path / "canonical"
    original.mkdir()
    (original / "Main.lean").write_text("theorem t : True := by sorry\n")
    private = original / ".leanflow"
    private.mkdir()
    (private / "state.json").write_text("controller secret")
    (original / ".mcp.json").write_text('{"mcpServers":{"external":{}}}')
    (original / ".codex").mkdir()
    (original / ".codex/config.toml").write_text("unsafe = true")
    build = original / ".lake/build/lib/lean"
    build.mkdir(parents=True)
    (build / "Main.olean").write_bytes(b"compiled target")
    (original / "escape").symlink_to(tmp_path)
    snapshot = create_snapshot(original, tmp_path / "snapshot")
    assert not (snapshot / ".leanflow").exists()
    assert not (snapshot / ".mcp.json").exists()
    assert not (snapshot / ".codex").exists()
    assert not (snapshot / ".lake/build").exists()
    assert not (snapshot / "escape").exists()
    (snapshot / "Main.lean").write_text("changed")
    assert "sorry" in (original / "Main.lean").read_text()


def test_snapshot_copy_failure_never_publishes_a_partial_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import native_snapshot

    original = tmp_path / "canonical"
    original.mkdir()
    (original / "Main.lean").write_text("theorem t : True := by sorry\n")

    def fail(*_: object) -> str:
        raise OSError("No space left on device")

    monkeypatch.setattr(native_snapshot, "copy_file", fail)
    with pytest.raises(OSError, match="No space left"):
        create_snapshot(original, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()
    assert not list(tmp_path.glob(".snapshot-*"))


def test_snapshot_nested_in_project_excludes_its_own_staging_tree(tmp_path: Path) -> None:
    (tmp_path / "Main.lean").write_text("theorem t : True := by sorry\n")
    snapshot = create_snapshot(tmp_path, tmp_path / "private")
    assert (snapshot / "Main.lean").is_file()
    assert not (snapshot / "private").exists()
    assert not list(snapshot.glob(".snapshot-*"))


@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_one_native_invocation_owns_the_full_conversation_without_default_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project(root)
    workspace = root / ".leanflow/jobs/job"
    workspace.mkdir(parents=True)
    calls = []

    def harness(request: Any, **kwargs: Any) -> HarnessResult:
        calls.append(request)
        assert request.backend == backend
        assert request.model == request.effort == ""
        assert request.deadline is None
        assert request.on_started is not None
        request.on_started()
        for number in range(5):
            event = (
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"Reply {number}"}]},
                }
                if backend == "claude"
                else {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": f"Reply {number}"},
                }
            )
            kwargs["on_event"](event)
        mcp = json.loads(request.mcp_config.read_text())
        assert next(iter(mcp["mcpServers"])).startswith("leanflow_job_")
        return HarnessResult(
            "completed",
            '{"proof":"trivial"}',
            session_id="native-session",
            usage={"input_tokens": 10, "output_tokens": 2},
            usage_total={"input_tokens": 10, "output_tokens": 2},
            returncode=0,
        )

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    events = []
    result = run_session(
        role="prover",
        prompt="Prove the theorem",
        project_root=root,
        workspace=workspace,
        config={"provider": backend},
        api_budget=1,
        log_path=workspace / "events.jsonl",
        context={},
        on_event=lambda kind, detail: events.append((kind, detail)),
    )
    assert len(calls) == 1
    assert result["api_calls"] == 1
    assert result["usage_known"] is True
    assert result["session_id"] == "native-session"
    assert [kind for kind, _ in events] == [
        "api-request",
        *(["agent-output"] * 5),
        "job-session-end",
    ]
    resumed = run_session(
        role="prover",
        prompt="Continue the proof",
        project_root=root,
        workspace=workspace,
        config={"provider": backend},
        api_budget=2,
        log_path=workspace / "events.jsonl",
        context={},
        on_event=lambda kind, detail: events.append((kind, detail)),
    )
    assert len(calls) == 2
    assert calls[1].session_id == "native-session"
    assert resumed["agent_calls"] == resumed["api_calls"] == 2
    assert [detail["agent_calls"] for kind, detail in events if kind == "api-request"] == [1, 2]
    assert len([kind for kind, _ in events if kind == "agent-output"]) == 10


def test_startup_failure_is_not_an_agent_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project(root)
    workspace = root / ".leanflow/jobs/job"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(
        "agent.runtime.harness.run_harness",
        lambda request, **kwargs: HarnessResult("startup_error", error="missing binary"),
    )
    result = run_session(
        role="prover",
        prompt="prove",
        project_root=root,
        workspace=workspace,
        config={"provider": "codex"},
        api_budget=1,
        log_path=workspace / "events.jsonl",
        context={},
    )
    assert result["api_calls"] == 0
    assert result["usage_known"] is False
    assert result["input_tokens"] is None


def test_global_call_admission_is_serialized_and_zero_is_exhausted(tmp_path: Path) -> None:
    path = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(agent_calls=1), verifier=Verifier()
    )
    successes = []
    rejected = []

    def reserve() -> None:
        try:
            successes.append(runtime._reserve(1))
        except BudgetExhausted:
            rejected.append(True)

    threads = [threading.Thread(target=reserve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert successes == [1]
    assert len(rejected) == 7
    runtime.config = ProverConfig(agent_calls=0)
    with pytest.raises(BudgetExhausted):
        runtime._reserve(1)


def test_silent_session_has_no_three_or_twenty_minute_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), verifier=Verifier()
    )
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.runtime.time.monotonic", lambda: runtime.started + 3600
    )
    runtime._ensure_active()
    assert runtime._remaining_verification_time() is None
    runtime._persist()
    saved = json.loads((runtime.store.directory / "state.json").read_text())
    assert saved["metrics"]["elapsed_s"] >= 3600
    assert saved["metrics"]["agent_call_limit"] is None


def test_bridge_polling_preserves_work_and_assignment_is_immutable(tmp_path: Path) -> None:
    tools = SessionTools(role="review", project_root=tmp_path, workspace=tmp_path, context={})
    started, finish = threading.Event(), threading.Event()
    context = {"node": {"revision": 3}}

    def submit(response: str) -> dict[str, Any]:
        started.set()
        finish.wait()
        return {"accepted": True, "response": response}

    with NativeBridge(
        toolset=tools,
        context=context,
        callbacks={"submit_report": submit},
        directory=tmp_path / "receipts",
    ) as bridge:
        context["node"]["revision"] = 9
        assert bridge.call("get_assignment", {})["node"]["revision"] == 3
        receipt = bridge.call("submit_report", {"response": "{}"})
        poll = {"operation_id": receipt["operation_id"]}
        assert started.wait(1)
        assert bridge.call("poll_operation", poll)["status"] == "running"
        assert bridge.call("poll_operation", poll)["cancel_requested"] is False
        finish.set()
        bridge.threads[-1].join()
        assert bridge.call("poll_operation", poll)["result"]["accepted"] is True
        assert bridge.submitted_response == "{}"


def test_bridge_drain_preserves_pending_submission_and_close_rejects_new_work(
    tmp_path: Path,
) -> None:
    tools = SessionTools(role="review", project_root=tmp_path, workspace=tmp_path, context={})
    started, finish, drained = threading.Event(), threading.Event(), threading.Event()

    def submit(response: str) -> dict[str, Any]:
        started.set()
        finish.wait()
        return {"accepted": True}

    bridge = NativeBridge(
        toolset=tools,
        context={},
        callbacks={"submit_report": submit},
        directory=tmp_path / "receipts",
    )
    receipt = bridge.call("submit_report", {"response": "accepted report"})
    assert started.wait(1)

    def drain() -> None:
        bridge.drain()
        drained.set()

    waiter = threading.Thread(target=drain)
    waiter.start()
    assert not drained.wait(0.05)
    assert not bridge.stop_events[receipt["operation_id"]].is_set()
    finish.set()
    waiter.join(1)
    assert drained.is_set()
    assert bridge.submitted_response == "accepted report"
    bridge.close()
    with pytest.raises(ValueError, match="closed"):
        bridge.call("submit_report", {"response": "late"})


def test_unknown_usage_stops_further_token_capped_admission(tmp_path: Path) -> None:
    path = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(tokens=100), verifier=Verifier()
    )
    runtime.state["jobs"].append({"api_calls": 1, "accounted": True, "usage_known": False})
    with pytest.raises(BudgetExhausted, match="unavailable"):
        runtime._reserve(1)


def test_cancellation_during_independent_check_prevents_canonical_promotion(tmp_path: Path) -> None:
    path = project(tmp_path)
    before = path.read_bytes()

    class CancellingVerifier(Verifier):
        def check(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            runtime.cancelled.set()
            return {"accepted": True}

    runtime = ProverRuntime(
        root=tmp_path, targets=[path], config=ProverConfig(), verifier=CancellingVerifier()
    )
    with pytest.raises(InfrastructureFailure) as stopped:
        runtime._accept(runtime.dag.nodes[0], ["trivial"], "job")
    assert stopped.value.status == "interrupted"
    assert path.read_bytes() == before


def test_native_rejected_candidate_repairs_in_same_process_and_receipt_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.leanflow.native_harness_helpers import call_tool, finish_operation

    path = project(tmp_path)
    original = path.read_bytes()

    class CandidateVerifier(Verifier):
        def check(self, node: Any, file: Path, **kwargs: Any) -> dict[str, Any]:
            accepted = "by trivial" in file.read_text()
            return {
                "accepted": accepted,
                "error": (
                    ("unknown identifier bad " + "x" * 20000 + " tail evidence")
                    if not accepted
                    else ""
                ),
            }

    def harness(request: Any, **kwargs: Any) -> HarnessResult:
        request.on_admitting()
        request.on_started()
        rejected = finish_operation(
            request, call_tool(request, "submit_candidate", {"response": '{"proof":"bad"}'})
        )
        assert rejected["accepted"] is False
        assert json.loads(rejected["feedback"])["error"].endswith("tail evidence")
        accepted = finish_operation(
            request, call_tool(request, "submit_candidate", {"response": '{"proof":"trivial"}'})
        )
        assert accepted["accepted"] is True
        assert accepted["receipt"]["revision"] == 0
        assert len(accepted["receipt"]["input_hash"]) == 64
        assert path.read_bytes() == original
        return HarnessResult("completed", '{"proof":"trivial"}', returncode=0)

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    state = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(mode="standard"),
        verifier=CandidateVerifier(),
    ).run()
    assert state["status"] == "completed", state.get("error")
    assert state["metrics"]["agent_calls"] == 1
    assert path.read_text().endswith("by trivial\n")
    events = [
        json.loads(line) for line in Path(state["jobs"][0]["log_path"]).read_text().splitlines()
    ]
    assert sum(event["type"] == "tool-result" for event in events) == 2
    assert all(event["evidence_id"] for event in events)


def test_long_command_bridge_runs_with_no_timeout_and_cancels_process_tree(tmp_path: Path) -> None:
    import shutil
    import sys

    from leanflow_cli.workflows.prover.native_command import run_command

    if sys.platform == "darwin" and not shutil.which("sandbox-exec"):
        pytest.skip("macOS sandbox-exec unavailable")
    if sys.platform == "linux" and not shutil.which("bwrap"):
        pytest.skip("Bubblewrap unavailable")
    result = run_command(
        ["/bin/sh", "-c", "printf long-command-ok"], snapshot=tmp_path, directory=tmp_path / "home"
    )
    assert result["success"], result
    assert result["stdout"] == "long-command-ok"
    cancelled = threading.Event()
    timer = threading.Timer(0.1, cancelled.set)
    timer.start()
    try:
        result = run_command(
            ["/bin/sleep", "60"],
            snapshot=tmp_path,
            directory=tmp_path / "home",
            cancelled=cancelled.is_set,
        )
    finally:
        timer.join()
    assert result["status"] == "cancelled"
    assert result["elapsed_s"] < 5


@pytest.mark.parametrize("explicit_elan_home", [False, True])
def test_native_commands_keep_installed_lean_when_home_moves_to_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_elan_home: bool
) -> None:
    from leanflow_cli.workflows.prover import native_command

    original_home = tmp_path / "original-home"
    elan = tmp_path / "custom-elan" if explicit_elan_home else original_home / ".elan"
    elan.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(original_home))
    monkeypatch.setenv("ELAN_TOOLCHAIN", "leanprover/lean4:v4.33.1")
    if explicit_elan_home:
        monkeypatch.setenv("ELAN_HOME", str(elan))
    else:
        monkeypatch.delenv("ELAN_HOME", raising=False)
    policies = []

    def sandbox(argv, policy):
        policies.append(policy)
        return argv

    monkeypatch.setattr(native_command, "sandbox_command", sandbox)
    directory = tmp_path / "scratch"
    result = native_command.run_command(
        ["/bin/sh", "-c", 'printf "%s\\n%s\\n%s" "$HOME" "$ELAN_HOME" "$ELAN_TOOLCHAIN"'],
        snapshot=tmp_path,
        directory=directory,
    )
    assert result["success"]
    assert result["stdout"].splitlines() == [
        str(directory),
        str(elan.resolve()),
        "leanprover/lean4:v4.33.1",
    ]
    assert elan.resolve() in policies[0].readable_roots
    assert elan.resolve() not in policies[0].writable_roots
