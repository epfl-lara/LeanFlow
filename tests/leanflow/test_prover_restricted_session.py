"""Exercise local-only native sessions, policy transitions, and command network confinement."""

from __future__ import annotations

import json
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import HarnessResult
from leanflow_cli.workflows.prover.agent_session import run_session
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.native_bridge import NativeBridge
from leanflow_cli.workflows.prover.native_command import run_command
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.session_tools import SessionTools
from tests.leanflow.test_prover_runtime import Verifier, project


def _session(root: Path, **config: Any) -> dict[str, Any]:
    """Run one deterministic test job without invoking a real model."""
    return run_session(
        role="research",
        prompt="Explore the supplied problem using local tools.",
        project_root=root,
        workspace=root / ".leanflow/jobs/job",
        config=config,
        api_budget=1,
        log_path=root / "events.jsonl",
        context={},
    )


@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
@pytest.mark.parametrize("restriction", ["clean-room", "repository", "offline"])
def test_restrictions_keep_native_loop_with_only_local_bridge_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, restriction: str
) -> None:
    project(tmp_path)
    registry: dict[str, NativeBridge] = {}
    if restriction != "offline":
        monkeypatch.setenv(
            (
                "LEANFLOW_DISABLE_SOLUTION_RESEARCH"
                if restriction == "clean-room"
                else "LEANFLOW_DISABLE_REPOSITORY_RESEARCH"
            ),
            "true",
        )

    def harness(request: Any, **kwargs: Any) -> HarnessResult:
        assert request.backend == backend
        assert request.restricted_tools
        assert request.deadline is None
        assert "local-only research session" in request.prompt
        bridge = next(iter(registry.values()))
        assert not bridge.toolset.allow_internet
        names = {item["name"] for item in bridge.schemas()}
        assert {"start_command", "start_read_file", "start_compute"} <= names
        assert names.isdisjoint({"start_web_search", "start_fetch_resource"})
        request.on_admitting()
        request.on_started()
        return HarnessResult("completed", "{}", session_id="restricted-session")

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    try:
        result = _session(
            tmp_path,
            provider=backend,
            allow_internet=restriction != "offline",
            _bridge_registry=registry,
        )
        assert result["status"] == "completed"
        assert result["agent_calls"] == 1
        ledger = json.loads(
            (tmp_path / ".leanflow/jobs/.runtime/job/request-count.json").read_text()
        )
        assert ledger["research_policy"] == "restricted-v1"
    finally:
        for bridge in registry.values():
            bridge.close()


@pytest.mark.parametrize("previous_policy", [None, "online-v1", "restricted-v1"])
@pytest.mark.parametrize("restricted", [False, True])
def test_native_history_and_usage_baseline_are_bound_to_research_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_policy: str | None,
    restricted: bool,
) -> None:
    project(tmp_path)
    ledger = tmp_path / ".leanflow/jobs/.runtime/job/request-count.json"
    ledger.parent.mkdir(parents=True)
    prior = {
        "backend": "codex",
        "used": 3,
        "session_id": "earlier-session",
        "usage_total": {"input_tokens": 7, "output_tokens": 2},
        "research_policy": previous_policy,
    }
    ledger.write_text(json.dumps(prior))
    expected_policy = "restricted-v1" if restricted else "online-v1"

    def harness(request: Any, **kwargs: Any) -> HarnessResult:
        resumable = previous_policy == expected_policy and not restricted
        assert request.session_id == ("earlier-session" if resumable else "")
        assert request.previous_usage == (prior["usage_total"] if resumable else None)
        request.on_admitting()
        assert json.loads(ledger.read_text())["research_policy"] == expected_policy
        request.on_started()
        return HarnessResult("completed", "{}", session_id="current-session")

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    result = _session(tmp_path, provider="codex", allow_internet=not restricted)
    assert result["agent_calls"] == 4
    assert json.loads(ledger.read_text())["research_policy"] == expected_policy


@pytest.mark.parametrize("previous_policy", [None, "online-v1"])
def test_restricted_run_cannot_reuse_old_research_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous_policy: str | None
) -> None:
    project(tmp_path)
    workspace = tmp_path / ".leanflow/jobs/job"
    snapshot = workspace / "project"
    snapshot.mkdir(parents=True)
    prior_research = snapshot / "solution.md"
    prior_research.write_text("Earlier external research")
    ledger = workspace.parent / ".runtime/job/request-count.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"used": 3, "research_policy": previous_policy}))
    original_ledger = ledger.read_bytes()
    old_bridge = NativeBridge(
        toolset=SessionTools(
            role="research", project_root=snapshot, workspace=snapshot, context={}
        ),
        context={},
        callbacks={},
        directory=ledger.parent / "operations",
        persistent=True,
    )
    registry = {str(workspace): old_bridge}
    monkeypatch.setattr(
        "agent.runtime.harness.run_harness",
        lambda *args, **kwargs: pytest.fail("Contaminated workspace must not be reused"),
    )
    result = _session(tmp_path, allow_internet=False, _bridge_registry=registry)
    assert result["status"] == "environment_error"
    assert "Start a new run" in result["error"]
    assert result["agent_calls"] == 3
    assert ledger.read_bytes() == original_ledger
    assert prior_research.read_text() == "Earlier external research"
    assert old_bridge.closed and registry == {}


def test_restricted_launch_failure_keeps_safe_snapshot_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project(tmp_path)
    calls = []

    def harness(request: Any, **kwargs: Any) -> HarnessResult:
        calls.append(request)
        return HarnessResult("startup_error", error="Runtime binary missing")

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    for _ in range(2):
        result = _session(tmp_path, allow_internet=False)
        assert result["status"] == "provider_error"
        assert result["agent_calls"] == 0
    assert len(calls) == 2


@pytest.mark.parametrize("previous_policy", [None, "online-v1"])
@pytest.mark.parametrize("restriction", ["clean-room", "offline"])
def test_controller_rejects_prior_research_before_restoring_graph_or_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_policy: str | None,
    restriction: str,
) -> None:
    source = project(tmp_path)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[source],
        config=ProverConfig(),
        run_id="old-run",
        verifier=Verifier(),
    )
    state_file = runtime.store.directory / "state.json"
    state = json.loads(state_file.read_text())
    state.update(research_policy=previous_policy, dag=None, plan_markdown="Earlier solution")
    state_file.write_text(json.dumps(state))
    original = state_file.read_bytes()
    if restriction == "clean-room":
        monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "true")
    with pytest.raises(ValueError, match="Start a new run"):
        ProverRuntime(
            root=tmp_path,
            targets=[source],
            config=ProverConfig(allow_internet=restriction != "offline"),
            run_id="old-run",
            resume=True,
            verifier=Verifier(),
        )
    assert state_file.read_bytes() == original


def test_restricted_controller_keeps_policy_across_restart_without_environment_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = project(tmp_path)
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "true")
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[source],
        config=ProverConfig(),
        run_id="clean-run",
        verifier=Verifier(),
    )
    assert not runtime.config.allow_internet
    assert runtime.state["research_policy"] == "restricted-v1"
    monkeypatch.delenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH")
    resumed = ProverRuntime(
        root=tmp_path,
        targets=[source],
        config=ProverConfig(),
        run_id="clean-run",
        resume=True,
        verifier=Verifier(),
    )
    assert not resumed.config.allow_internet
    assert resumed.state["research_policy"] == "restricted-v1"


@pytest.mark.parametrize("allow_internet", [False, True])
def test_bridge_forwards_network_policy_to_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_internet: bool
) -> None:
    received = []

    def command(argv: list[str], **kwargs: Any) -> dict[str, Any]:
        received.append(kwargs["network_allowed"])
        return {"success": True}

    monkeypatch.setattr("leanflow_cli.workflows.prover.native_command.run_command", command)
    with NativeBridge(
        toolset=SessionTools(
            role="prover",
            project_root=tmp_path,
            workspace=tmp_path,
            context={},
            allow_internet=allow_internet,
        ),
        context={},
        callbacks={},
        directory=tmp_path / "operations",
    ) as bridge:
        bridge.call("start_command", {"argv": ["/bin/echo", "local"]})
        bridge.drain()
    assert received == [allow_internet]


def test_restricted_command_can_work_locally_but_cannot_reach_network(tmp_path: Path) -> None:
    if sys.platform == "darwin" and not shutil.which("sandbox-exec"):
        pytest.skip("macOS sandbox-exec unavailable")
    if sys.platform == "linux" and not shutil.which("bwrap"):
        pytest.skip("Bubblewrap unavailable")
    if sys.platform not in {"darwin", "linux"}:
        pytest.skip("No supported operating system sandbox")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        baseline = run_command(
            [
                sys.executable,
                "-c",
                f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=2).close()",
            ],
            snapshot=tmp_path,
            directory=tmp_path / "home",
        )
        assert baseline["success"], baseline
        script = f"""
import json, pathlib, socket
pathlib.Path('local.txt').write_text('local work succeeded')
result = {{'file': pathlib.Path('local.txt').read_text(), 'network': []}}
for target in [('127.0.0.1', {port}), ('1.1.1.1', 443)]:
    try:
        with socket.create_connection(target, timeout=2):
            result['network'].append('connected')
    except OSError as error:
        result['network'].append(error.errno)
print(json.dumps(result))
"""
        result = run_command(
            [sys.executable, "-c", script],
            snapshot=tmp_path,
            directory=tmp_path / "home",
            network_allowed=False,
        )
    assert result["success"], result
    observed = json.loads(result["stdout"])
    assert observed["file"] == "local work succeeded"
    # Seatbelt denies sockets; a separate Linux network namespace has no host routes.
    assert all(error in {1, 13, 101, 111} for error in observed["network"]), observed
