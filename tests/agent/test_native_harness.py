"""Exercise native invocation lifecycle, authentic configuration, and usage reporting."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime.harness import (
    BackendError,
    HarnessRequest,
    IsolationPolicy,
    build_command,
    child_environment,
    run_harness,
)
from agent.runtime.harness.events import (
    EventState,
    normalize_claude_model_usage,
    normalize_usage,
    usage_delta,
)


@pytest.fixture
def fake_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run real fake processes while testing OS isolation independently."""
    from agent.runtime.harness import runner

    monkeypatch.setattr(runner, "sandbox_command", lambda argv, policy, **kwargs: argv)

    def factory(body: str, backend: str = "lemex") -> HarnessRequest:
        executable = tmp_path / "native-cli"
        executable.write_text(
            f"#!{sys.executable}\nimport sys\nif sys.argv[1:3] == ['mcp', 'list']:\n    print('[]')\n    sys.exit(0)\n"
            + body
        )
        executable.chmod(0o700)
        return HarnessRequest(
            backend,
            "a complete task",
            tmp_path,
            executable=str(executable),
            isolation=IsolationPolicy(tmp_path),
        )

    return factory


@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_commands_preserve_model_and_sampling_defaults(tmp_path: Path, backend: str):
    command = build_command(HarnessRequest(backend, "hello", tmp_path))
    rendered = " ".join(command)
    for name in (
        "temperature",
        "top_p",
        "seed",
        "max-turns",
        "timeout",
        "ignore-user-config",
        "bypass",
        "--bare",
    ):
        assert name not in rendered
    assert "--model" not in command
    assert "--effort" not in command


@pytest.mark.parametrize("backend", ["codex", "lemex"])
def test_explicit_overrides_and_resume(tmp_path: Path, backend: str):
    command = build_command(
        HarnessRequest(backend, "hello", tmp_path, model="m", effort="high", session_id="abc")
    )
    assert command[-3:] == ["resume", "abc", "-"]
    assert command[command.index("--model") + 1] == "m"
    assert 'model_reasoning_effort="high"' in command


@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_native_continuation_preserves_session_and_compaction_defaults(
    tmp_path: Path, backend: str
):
    command = build_command(
        HarnessRequest(backend, "continue", tmp_path, session_id="saved-session")
    )
    if backend == "claude":
        assert command[command.index("--resume") + 1] == "saved-session"
    else:
        assert command[-3:] == ["resume", "saved-session", "-"]
    for override in (
        "--ephemeral",
        "--no-session-persistence",
        "--fork-session",
        "--last",
        "--autocompact",
        "prompt_cache_key",
        "model_context_window",
        "model_auto_compact_token_limit",
    ):
        assert override not in " ".join(command)


def test_inner_sandbox_changes_require_outer_policy_and_native_tools(tmp_path: Path):
    request = HarnessRequest("codex", "hello", tmp_path)
    assert 'sandbox_mode="workspace-write"' in build_command(request)
    with pytest.raises(BackendError, match="outer isolation"):
        build_command(request, externally_isolated=True)
    isolated = replace(request, isolation=IsolationPolicy(tmp_path))
    assert 'sandbox_mode="danger-full-access"' in build_command(isolated, externally_isolated=True)
    assert 'sandbox_mode="read-only"' in build_command(
        replace(isolated, tools=False), externally_isolated=True
    )


def test_auth_is_inherited_and_openai_mapping_is_child_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "explicit-test-key")
    monkeypatch.setenv("CODEX_API_KEY", "other-key")
    monkeypatch.setenv("LEMEX_BASE_URL", "https://example.invalid/v1")
    request = HarnessRequest("codex", "hello", tmp_path)
    assert child_environment(request)["CODEX_API_KEY"] == "explicit-test-key"
    assert os.environ["CODEX_API_KEY"] == "other-key"
    assert (
        child_environment(replace(request, backend="lemex"))["LEMEX_BASE_URL"]
        == "https://example.invalid/v1"
    )


def test_native_json_and_stderr_are_independent(fake_backend, tmp_path: Path):
    request = fake_backend("""import json, sys
assert sys.stdin.read() == 'a complete task'
print('a warning without JSON', file=sys.stderr)
for event in [{'type':'thread.started','thread_id':'thread-1'},
              {'type':'item.completed','item':{'type':'agent_message','text':'proved'}},
              {'type':'turn.completed','usage':{'input_tokens':15,'output_tokens':4}}]:
    print(json.dumps(event))
""")
    observed = []
    starts = []
    result = run_harness(
        replace(request, log_path=tmp_path / "out.jsonl", on_started=lambda: starts.append(True)),
        observed.append,
    )
    assert result.status == "completed"
    assert result.final_response == "proved"
    assert result.session_id == "thread-1"
    assert result.usage == {"input_tokens": 15, "output_tokens": 4, "total_tokens": 19}
    assert starts == [True]
    assert any(event["type"] == "harness.stderr" for event in observed)
    assert all(
        isinstance(json.loads(line), dict)
        for line in (tmp_path / "out.jsonl").read_text().splitlines()
    )


def test_native_executable_symlink_works_with_real_isolation(tmp_path: Path, kernel_sandbox):
    """Launch a symlinked CLI without granting its unrelated PATH directory."""
    from agent.runtime.harness import build_runtime_isolation

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    executable = tmp_path / "native-cli"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sqlite3, sys\n"
        "if sys.platform == 'linux':\n"
        "    assert pathlib.Path('/etc/resolv.conf').read_text()\n"
        "if sys.argv[1:3] == ['mcp', 'list']:\n"
        "    print('[]')\n"
        "    sys.exit(0)\n"
        "if sys.platform == 'linux':\n"
        "    setting=next(arg for arg in sys.argv if arg.startswith('sqlite_home='))\n"
        "    database=pathlib.Path(json.loads(setting.split('=',1)[1]))/'history.sqlite'\n"
        "    connection=sqlite3.connect(database)\n"
        "    assert connection.execute('pragma journal_mode=wal').fetchone()==('wal',)\n"
        "    connection.execute('create table calls (value text)')\n"
        "    connection.execute(\"insert into calls values ('persisted')\")\n"
        "    connection.commit();connection.close()\n"
        "assert sys.stdin.read() == 'a complete task'\n"
        "print(json.dumps({'type':'thread.started','thread_id':'symlink-session'}))\n"
        "print(json.dumps({'type':'item.completed','item':"
        "{'type':'agent_message','text':'isolated invocation'}}))\n"
        "print(json.dumps({'type':'turn.completed','usage':"
        "{'input_tokens':1,'output_tokens':1}}))\n"
    )
    executable.chmod(0o700)
    aliases = tmp_path / "bin"
    aliases.mkdir()
    alias = aliases / "codex"
    alias.symlink_to(executable)
    env = {"HOME": str(tmp_path / "home"), "CODEX_HOME": str(tmp_path / "native-home")}
    request = HarnessRequest(
        "codex",
        "a complete task",
        snapshot,
        env=env,
        executable=str(alias),
        isolation=build_runtime_isolation("codex", snapshot, env=env),
    )
    starts = []
    result = run_harness(replace(request, on_started=lambda: starts.append(True)))
    assert result.status == "completed", result
    assert result.session_id == "symlink-session"
    assert result.final_response == "isolated invocation"
    assert starts == [True]
    if sys.platform == "linux":
        assert request.isolation is not None
        assert request.isolation.native_database_root is not None
        import sqlite3

        with sqlite3.connect(request.isolation.native_database_root / "history.sqlite") as db:
            assert db.execute("select value from calls").fetchall() == [("persisted",)]


@pytest.mark.parametrize(
    "payload,status",
    [
        ("print('not-json')\nprint('{\"type\":\"turn.completed\"}')", "malformed_output"),
        ('print(\'{"type":"thread.started"}\')', "failed"),
        ('import sys\nprint(\'{"type":"turn.completed"}\')\nsys.exit(3)', "failed"),
    ],
)
def test_bad_native_output_never_claims_completion(fake_backend, payload: str, status: str):
    result = run_harness(fake_backend(payload))
    assert result.status == status
    assert result.usage is None


def test_stream_errors_can_recover(fake_backend):
    result = run_harness(
        fake_backend(
            'print(\'{"type":"error","message":"reconnecting"}\')\nprint(\'{"type":"turn.completed"}\')'
        )
    )
    assert result.status == "completed"
    assert result.diagnostics == ("reconnecting",)


def test_silent_session_has_no_response_timeout(
    fake_backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from agent.runtime.harness import runner

    silent = tmp_path / "silent"
    observed_jumps = []

    def clock():
        # Jump while the child is silent, then keep real time for output cleanup.
        # Call-count clocks can jump across the drain deadline under CPU load.
        jump = 10_000.0 if silent.exists() else 0.0
        observed_jumps.append(jump)
        return time.monotonic() + jump

    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=clock))
    result = run_harness(
        fake_backend(
            'import time\nfrom pathlib import Path\nPath("silent").touch()\n'
            'time.sleep(0.15)\nprint(\'{"type":"turn.completed"}\')'
        )
    )
    assert 10_000.0 in observed_jumps
    assert result.status == "completed", result


def test_deadline_and_cancel_before_launch_do_not_charge(fake_backend):
    starts = []
    request = replace(
        fake_backend("raise AssertionError('should never run')"),
        deadline=time.monotonic() - 1,
        on_started=lambda: starts.append(True),
    )
    result = run_harness(request)
    assert result.status == "deadline_exceeded"
    assert result.returncode is None
    assert starts == []
    assert (
        run_harness(replace(request, deadline=None, cancelled=lambda: True)).status == "cancelled"
    )


@pytest.mark.parametrize("detached", [False, True])
def test_cancellation_terminates_process_group(fake_backend, tmp_path: Path, detached: bool):
    pid_file = tmp_path / "child-pid"
    child_code = f"import os,time,pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    request = fake_backend(
        f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{child_code!r}], start_new_session={detached!r})\ntime.sleep(30)"
    )
    result = run_harness(replace(request, cancelled=lambda: pid_file.exists()))
    assert result.status == "cancelled"
    child_pid = int(pid_file.read_text())
    # Some kernels briefly retain a zombie until the system reaper observes it.
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        os.kill(child_pid, signal.SIGKILL)
        pytest.fail("Native descendant survived cancellation")


def test_no_isolation_is_rejected(fake_backend):
    with pytest.raises(BackendError, match="isolated workspace"):
        run_harness(replace(fake_backend("pass"), isolation=None))


@pytest.mark.parametrize("members", ["900 S\n", "412 Z\n900 S\n"])
def test_group_cleanup_accepts_eperm_only_for_exited_or_zombie_group(monkeypatch, members):
    from agent.runtime.harness import processes

    def denied(*args):
        raise PermissionError("macOS group has no signalable processes")

    monkeypatch.setattr(processes.os, "killpg", denied)
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=members),
    )
    processes.signal_group(SimpleNamespace(pid=412, returncode=-15), signal.SIGKILL)


@pytest.mark.parametrize("members", ["412 S\n", "412 T\n", "unrecognized process table\n"])
def test_group_cleanup_does_not_hide_permission_failure_with_live_or_unknown_members(
    monkeypatch, members
):
    from agent.runtime.harness import processes

    def denied(*args):
        raise PermissionError("real permission failure")

    monkeypatch.setattr(processes.os, "killpg", denied)
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=members),
    )
    with pytest.raises(PermissionError, match="real permission failure"):
        processes.signal_group(SimpleNamespace(pid=412, returncode=-15), signal.SIGKILL)


def test_group_cleanup_does_not_hide_permission_failure_for_running_leader(monkeypatch):
    from agent.runtime.harness import processes

    def denied(*args):
        raise PermissionError("running native process")

    monkeypatch.setattr(processes.os, "killpg", denied)
    monkeypatch.setattr(
        processes,
        "_group_has_live_processes",
        lambda *args: pytest.fail("A live leader already establishes unfinished cleanup"),
    )
    monkeypatch.setattr(processes, "process_exited", lambda process: False)
    with pytest.raises(PermissionError, match="running native process"):
        processes.signal_group(SimpleNamespace(pid=412, returncode=None), signal.SIGKILL)


def test_group_cleanup_fails_closed_when_empty_group_cannot_be_verified(monkeypatch):
    from agent.runtime.harness import processes

    def denied(*args):
        raise PermissionError("unverified native process group")

    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("ps", 2)

    monkeypatch.setattr(processes.os, "killpg", denied)
    monkeypatch.setattr(processes.subprocess, "run", unavailable)
    with pytest.raises(subprocess.TimeoutExpired):
        processes.signal_group(SimpleNamespace(pid=412, returncode=-15), signal.SIGKILL)


def test_process_inspection_failure_still_stops_native_group(monkeypatch: pytest.MonkeyPatch):
    from agent.runtime.harness import processes

    def unavailable():
        raise OSError("process metadata unavailable")

    monkeypatch.setattr(processes, "_processes", unavailable)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        with pytest.raises(BackendError, match="descendant cleanup could not be verified"):
            processes.cleanup_group(process, processes.ProcessTree(process.pid))
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


@pytest.mark.parametrize("session_id", ["", "preserved-native-resume"])
def test_missing_executable_is_explicit(tmp_path: Path, session_id: str):
    result = run_harness(
        HarnessRequest(
            "lemex", "hello", tmp_path, executable="/no/such/native/backend", session_id=session_id
        )
    )
    assert result.status == "startup_error"
    assert result.returncode is None and result.cleanup_verified is True
    assert result.session_id == session_id


def test_resume_usage_requires_baseline():
    total = normalize_usage({"input_tokens": 100, "output_tokens": 20})
    assert usage_delta(total, None, resumed=True, cumulative=True) is None
    assert usage_delta(
        total, {"input_tokens": 80, "output_tokens": 15}, resumed=True, cumulative=True
    ) == {
        "input_tokens": 20,
        "output_tokens": 5,
        "total_tokens": 25,
    }
    assert (
        usage_delta(
            total, {"input_tokens": 101, "output_tokens": 15}, resumed=True, cumulative=True
        )
        is None
    )


def test_native_cache_and_reasoning_subtotals_are_preserved_without_double_counting():
    usage = normalize_usage(
        {
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "cache_write_input_tokens": 30,
            "output_tokens": 20,
            "reasoning_output_tokens": 12,
        }
    )
    assert usage == {
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "cache_write_input_tokens": 30,
        "output_tokens": 20,
        "reasoning_output_tokens": 12,
        "total_tokens": 120,
    }
    assert usage_delta(
        usage,
        {
            "input_tokens": 50,
            "cached_input_tokens": 40,
            "cache_write_input_tokens": 5,
            "output_tokens": 9,
            "reasoning_output_tokens": 8,
        },
        resumed=True,
        cumulative=True,
    ) == {
        "input_tokens": 50,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 25,
        "output_tokens": 11,
        "reasoning_output_tokens": 4,
        "total_tokens": 61,
    }


def test_resumed_cache_subtotals_require_their_own_known_baselines():
    usage = normalize_usage(
        {
            "input_tokens": 100,
            "cached_input_tokens": 60,
            "cache_write_input_tokens": 30,
            "output_tokens": 20,
        }
    )
    delta = usage_delta(
        usage,
        {"input_tokens": 80, "output_tokens": 15, "cached_input_tokens": 65},
        resumed=True,
        cumulative=True,
    )
    assert delta == {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}


def test_native_mcp_servers_are_disabled_without_reusing_credentials(tmp_path: Path):
    command = build_command(
        HarnessRequest("lemex", "hello", tmp_path),
        native_mcp_servers={
            "user_http": {"enabled": False, "url": "http://127.0.0.1"},
            "user_stdio": {"enabled": False, "command": "/usr/bin/false"},
        },
    )
    import tomllib

    override = next(arg for arg in command if arg.startswith("mcp_servers="))
    configured = tomllib.loads(override)["mcp_servers"]
    assert configured["user_http"]["enabled"] is False
    assert configured["user_stdio"]["enabled"] is False


@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_equivalent_mcp_configuration_has_stable_command_order(tmp_path: Path, backend: str):
    configuration = tmp_path / "mcp.json"
    servers = {
        "leanflow_b": {"env": {"Z": "last", "A": "first"}, "command": "relay"},
        "leanflow_a": {"command": "relay", "args": ["-m", "module"]},
    }
    configuration.write_text(json.dumps({"mcpServers": servers}))
    request = HarnessRequest(backend, "hello", tmp_path, mcp_config=configuration)
    first = build_command(request)
    configuration.write_text(json.dumps({"mcpServers": dict(reversed(list(servers.items())))}))
    assert build_command(request) == first


@pytest.mark.parametrize(
    "metadata",
    [
        [{"name": "server", "transport": None}],
        [{"name": "", "transport": {"type": "stdio"}}],
        [{"name": "server", "transport": {"type": "stdio"}}] * 2,
        [{"name": "server", "transport": {"type": "unsupported"}}],
    ],
)
def test_malformed_native_tool_metadata_fails_closed_without_raw_details(tmp_path: Path, metadata):
    from agent.runtime.harness.discovery import discover_mcp_servers

    request = HarnessRequest("codex", "hello", tmp_path)
    command = [sys.executable, "-c", f"print({json.dumps(metadata)!r})"]
    with pytest.raises(BackendError, match="unsupported response"):
        discover_mcp_servers(request, command, os.environ)


def test_job_bridge_cannot_merge_with_existing_external_server(tmp_path: Path):
    configuration = tmp_path / "mcp.json"
    configuration.write_text(json.dumps({"mcpServers": {"leanflow": {"command": "relay"}}}))
    with pytest.raises(BackendError, match="conflicts"):
        build_command(
            HarnessRequest("codex", "hello", tmp_path, mcp_config=configuration),
            native_mcp_servers={"leanflow": {"enabled": False, "url": "http://127.0.0.1"}},
        )


def test_claude_usage_includes_cached_input_and_final_errors():
    state = EventState("claude")
    state.consume(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "claude-1",
            "result": "answer",
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
            },
            "modelUsage": {
                "main": {
                    "inputTokens": 2,
                    "outputTokens": 3,
                    "cacheReadInputTokens": 10,
                    "cacheCreationInputTokens": 5,
                },
            },
        }
    )
    assert state.completed
    assert state.usage_total["total_tokens"] == 20
    state.consume(
        {"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": ["bad"]}
    )
    assert not state.completed
    assert "bad" in state.error


def test_claude_all_model_usage_includes_helpers_and_requires_complete_totals():
    models = {
        "main": {
            "inputTokens": 2,
            "outputTokens": 3,
            "cacheReadInputTokens": 10,
            "cacheCreationInputTokens": 5,
            "thinkingTokens": 2,
        },
        "helper": {
            "inputTokens": 4,
            "outputTokens": 6,
            "cacheReadInputTokens": 20,
            "cacheCreationInputTokens": 7,
            "thinkingTokens": 4,
        },
    }
    assert normalize_claude_model_usage(models) == {
        "input_tokens": 48,
        "output_tokens": 9,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 12,
        "reasoning_output_tokens": 6,
        "total_tokens": 57,
    }
    del models["helper"]["cacheReadInputTokens"]
    assert normalize_claude_model_usage(models) is None
    state = EventState("claude")
    state.consume(
        {"type": "result", "result": "done", "usage": {"input_tokens": 2, "output_tokens": 3}}
    )
    assert state.completed and state.usage_total is None


def test_claude_resume_subtracts_saved_model_totals(fake_backend):
    payload = {
        "type": "result",
        "result": "continued",
        "session_id": "saved-session",
        "modelUsage": {
            "main": {
                "inputTokens": 20,
                "outputTokens": 30,
                "cacheReadInputTokens": 100,
                "cacheCreationInputTokens": 50,
            }
        },
    }
    request = replace(
        fake_backend(f"print({json.dumps(payload)!r})", backend="claude"),
        session_id="saved-session",
        previous_usage={
            "input_tokens": 85,
            "output_tokens": 15,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 25,
        },
    )
    result = run_harness(request)
    assert result.usage == {
        "input_tokens": 85,
        "output_tokens": 15,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 25,
        "total_tokens": 100,
    }
    assert run_harness(replace(request, previous_usage=None)).usage is None


def test_additive_native_credentials_are_redacted_before_logs_and_callbacks(
    fake_backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Keep native verdict inputs original while publishing only credential-safe fragments."""
    secret = "opaque-test-credential"
    monkeypatch.setenv("TEST_NATIVE_API_KEY", secret)
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "false")
    events = []
    for item_id, fragments in (
        ("known", ("Working opaque-test-", "credential", "\n")),
        ("assignment", ("API_KE", "Y=unstored-", "credential\nContinuing. ")),
        ("header", ("Authorization: Bea", "rer uncached-", "credential\nNext step. ")),
    ):
        for fragment in fragments:
            events.append(
                {
                    "type": "item.delta",
                    "item_id": item_id,
                    "item_type": "reasoning",
                    "field": "raw_text",
                    "reasoning_kind": "trace",
                    "delta": fragment,
                }
            )
    events += [
        {
            "type": "item.delta",
            "item_id": [],
            "item_type": "reasoning",
            "field": "raw_text",
            "delta": "unscoped-private-fragment",
        },
        {
            "type": "item.delta",
            "item_id": "tail",
            "item_type": "agent_message",
            "field": "text",
            "delta": "Final ordinaryword",
        },
        {
            "type": "item.completed",
            "item": {
                "id": "answer",
                "type": "agent_message",
                "text": "Native state retains " + secret,
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}},
    ]
    request = fake_backend(
        "import json\nfor event in "
        + repr(events)
        + ":\n    print(json.dumps(event), flush=True)\n"
    )
    log = tmp_path / "harness.jsonl"
    seen = []
    result = run_harness(replace(request, log_path=log), seen.append)
    persisted = [json.loads(line) for line in log.read_text().splitlines()]
    assert persisted == seen
    assert result.status == "completed"
    assert result.final_response.endswith(secret)
    assert result.usage == {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
    public = json.dumps(seen)
    for private in ("opaque-test-", "unstored-", "uncached-", "unscoped-private-fragment"):
        assert private not in public
    assert "[REDACTED]" in public
    assert "Continuing." in public and "Next step." in public
    assert any(event.get("delta") == "ordinaryword" for event in seen)


def test_malformed_native_delta_diagnostics_omit_unscoped_raw_fragments(
    fake_backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Keep parse diagnostics without bypassing credential buffering through invalid JSON."""
    monkeypatch.setenv("TEST_NATIVE_API_KEY", "opaque-test-credential")
    malformed = '{"type":"item.delta","item_id":"item_0","delta":"opaque-test-'
    terminal = json.dumps({"type": "turn.completed"})
    request = fake_backend(f"print({malformed!r})\nprint({terminal!r})\n")
    seen = []
    log = tmp_path / "harness.jsonl"
    result = run_harness(replace(request, log_path=log), seen.append)
    assert result.status == "malformed_output"
    assert "opaque-test-" not in log.read_text()
    assert "opaque-test-" not in json.dumps(seen)
    diagnostic = next(event for event in seen if event["type"] == "harness.invalid_output")
    assert "Raw record omitted" in diagnostic["text"]
    assert result.diagnostics
