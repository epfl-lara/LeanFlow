"""Verify native clean-room commands and enforce their boundary in the operating system."""

from __future__ import annotations

import json
import platform
import shutil
import socket
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import (
    HarnessRequest,
    build_command,
    build_runtime_isolation,
    child_environment,
    run_harness,
)


def _request(tmp_path: Path, backend: str) -> HarnessRequest:
    """Create isolated fake native homes and a single explicit job bridge."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    environment = {
        "HOME": str(tmp_path / "home"),
        "CLAUDE_CONFIG_DIR" if backend == "claude" else f"{backend.upper()}_HOME": str(native_home),
    }
    config = snapshot / "mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"leanflow_job_test": {"command": "/usr/bin/false"}}})
    )
    return HarnessRequest(
        backend,
        "Use the supplied local tools.",
        snapshot,
        env=environment,
        isolation=build_runtime_isolation(backend, snapshot, env=environment),
        mcp_config=config,
        restricted_tools=True,
        session_id="previous-session",
        previous_usage={"input_tokens": 100, "output_tokens": 30},
    )


@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_restricted_commands_remove_native_retrieval_without_changing_model_defaults(
    tmp_path: Path, backend: str
) -> None:
    request = _request(tmp_path, backend)
    command = build_command(request, externally_isolated=True)
    rendered = " ".join(command)
    assert "previous-session" not in command
    assert "resume" not in command and "--resume" not in command
    assert "--model" not in command and "--effort" not in command
    for unexpected in (
        "temperature",
        "top_p",
        "max-turns",
        "timeout",
        "model_context_window",
        "model_auto_compact_token_limit",
        "prompt_cache_key",
    ):
        assert unexpected not in rendered
    if backend == "claude":
        assert command[command.index("--tools") + 1] == ""
        assert command[command.index("--allowedTools") + 1] == "mcp__leanflow_job_test__*"
        assert {"--strict-mcp-config", "--no-session-persistence", "--no-chrome"} <= set(command)
        assert "--disable-slash-commands" in command
        settings = json.loads(command[command.index("--settings") + 1])
        assert settings["disableAllHooks"]
        assert settings["autoMemoryEnabled"] is False
        assert settings["claudeMdExcludes"] == ["**"]
        env = child_environment(request)
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    else:
        assert "--ephemeral" in command
        assert 'web_search="disabled"' in command
        for feature in ("shell_tool", "js_repl", "multi_agent", "plugins", "hooks", "memories"):
            assert f"features.{feature}=false" in command
        assert "skills.include_instructions=false" in command
        assert "memories.use_memories=false" in command
        assert "project_doc_max_bytes=0" in command
        assert 'developer_instructions=""' in command
        assert request.isolation is not None
        assert "sqlite_home=" + json.dumps(str(request.isolation.temp_root)) in command


@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_restricted_commands_preserve_explicit_native_model_and_effort(
    tmp_path: Path, backend: str
) -> None:
    command = build_command(
        replace(_request(tmp_path, backend), model="native-choice", effort="high")
    )
    assert command[command.index("--model") + 1] == "native-choice"
    if backend == "claude":
        assert command[command.index("--effort") + 1] == "high"
    else:
        assert 'model_reasoning_effort="high"' in command


@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_kernel_restricts_native_workspace_and_history_but_keeps_model_network(
    tmp_path: Path, backend: str, kernel_sandbox
) -> None:
    system = platform.system()
    if system not in {"Darwin", "Linux"}:
        pytest.skip("No supported native kernel sandbox")
    if not shutil.which("sandbox-exec" if system == "Darwin" else "bwrap"):
        pytest.skip("Native kernel sandbox unavailable")
    request = _request(tmp_path, backend)
    native_home = tmp_path / "native-home"
    history = native_home / "sessions"
    history.mkdir()
    (history / "previous.json").write_text("Earlier solution")
    (native_home / "auth.json").write_text("fake native auth")
    (native_home / "config.toml").write_text("# fake native configuration")
    (native_home / "state.sqlite").write_text("Earlier memory")
    source = request.cwd / "Problem.lean"
    source.write_text("original problem")
    executable = tmp_path / "native-cli"
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        endpoint.listen()
        port = endpoint.getsockname()[1]
        # This fake runtime deliberately ignores feature switches. Kernel enforcement
        # must still block its file access if native apply_patch remains registered.
        executable.write_text(f"#!{sys.executable}\n" + f"""
import json, os, pathlib, socket, sys
if sys.argv[1:3] == ['mcp', 'list']:
    print(json.dumps([
        {{'name': 'external_stdio', 'transport': {{'type': 'stdio'}}}},
        {{'name': 'external_http', 'transport': {{'type': 'streamable_http'}}}},
    ]))
    sys.exit(0)
report = {{'argv': sys.argv[1:], 'denied': []}}
home = pathlib.Path({str(native_home)!r})
for name in ['sessions/previous.json', 'state.sqlite']:
    try:
        (home / name).read_text()
    except OSError:
        report['denied'].append(name)
try:
    pathlib.Path('Problem.lean').write_text('uncontrolled overwrite')
except OSError:
    report['denied'].append('source-write')
try:
    (home / 'config.toml').write_text('uncontrolled config overwrite')
except OSError:
    report['denied'].append('config-write')
report['auth_readable'] = (home / 'auth.json').read_text() == 'fake native auth'
scratch = pathlib.Path(os.environ['TMPDIR']) / 'local-cache'
scratch.write_text('scratch allowed')
report['scratch'] = scratch.read_text()
with socket.create_connection(('127.0.0.1', {port}), timeout=2):
    report['model_network'] = True
if {backend!r} == 'claude':
    print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False,
        'result': json.dumps(report), 'session_id': 'fresh-session',
        'modelUsage': {{'fake-model': {{'inputTokens': 7, 'outputTokens': 2,
            'cacheReadInputTokens': 0, 'cacheCreationInputTokens': 0}}}}}}))
else:
    for event in [
        {{'type': 'thread.started', 'thread_id': 'fresh-session'}},
        {{'type': 'item.completed', 'item': {{'type': 'agent_message', 'text': json.dumps(report)}}}},
        {{'type': 'turn.completed', 'usage': {{'input_tokens': 7, 'output_tokens': 2}}}},
    ]:
        print(json.dumps(event))
""")
        executable.chmod(0o700)
        observations: list[dict[str, Any]] = []
        result = run_harness(replace(request, executable=str(executable)), observations.append)
    assert result.status == "completed", (result, observations)
    report = json.loads(result.final_response)
    assert set(report["denied"]) == {
        "sessions/previous.json",
        "state.sqlite",
        "source-write",
        "config-write",
    }
    assert report["auth_readable"] and report["model_network"]
    assert report["scratch"] == "scratch allowed"
    assert source.read_text() == "original problem"
    assert result.usage is not None
    assert result.usage["input_tokens"] == 7 and result.usage["output_tokens"] == 2
    assert "previous-session" not in report["argv"]
    if backend != "claude":
        mcp = next(value for value in report["argv"] if value.startswith("mcp_servers="))
        assert '"external_stdio" = {"command" = "/usr/bin/false", "enabled" = false}' in mcp
        assert '"external_http" = {"enabled" = false, "url" = "http://127.0.0.1"}' in mcp
        assert '"leanflow_job_test"' in mcp
