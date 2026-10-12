"""Verify native settings provenance, invocation history, privacy, and command invariants."""

import json
from dataclasses import replace

import pytest

from agent.runtime.harness import HarnessRequest, run_harness
from agent.runtime.harness.commands import build_command, child_environment
from agent.runtime.harness.settings import (
    SettingsRecorder,
    native_input,
    private_control_response,
    startup_settings,
)
from tests.agent import test_native_harness as harness_tests

fake_backend = harness_tests.fake_backend


def diagnostic(backend: str, effort: str = "Some(XHigh)") -> str:
    """Reproduce the installed native exec bootstrap envelope."""
    name = "Lemex" if backend == "lemex" else "Codex"
    return (
        f"2026-10-12 INFO codex_exec: {name} initialized with event: SessionConfiguredEvent {{ "
        'thread_id: ThreadId { uuid: session-1 }, model: "actual-model", '
        f'model_provider_id: "provider", reasoning_effort: {effort}, rollout_path: None }}'
    )


@pytest.mark.parametrize("backend", ["codex", "lemex"])
@pytest.mark.parametrize(
    "effort,value",
    [
        ("Some(XHigh)", "xhigh"),
        ('Some(Custom("adaptive"))', "adaptive"),
        ("Some(None)", "none"),
        ("None", ""),
    ],
)
def test_startup_settings_come_from_native_process(backend, effort, value):
    result = startup_settings(backend, diagnostic(backend, effort))
    assert result["model"] == "actual-model"
    assert result["model_source"] == "native_session"
    assert result.get("reasoning_effort", "") == value
    assert result["session_id"] == "session-1"
    assert startup_settings(backend, 'tool said model: "guessed-model"') is None
    assert startup_settings(backend, diagnostic(backend).replace('"actual-model"', '"bad')) is None


def test_claude_input_has_metadata_requests_and_exactly_one_user_prompt(tmp_path):
    request = HarnessRequest("claude", 'Proof with "quotes"\nand unicode ∀', tmp_path)
    messages = [json.loads(line) for line in native_input(request).splitlines()]
    assert [m["type"] for m in messages] == ["control_request", "control_request", "user"]
    assert messages[-1]["message"]["content"] == request.prompt
    assert [m["request"]["subtype"] for m in messages[:-1]] == ["initialize", "get_settings"]
    assert "--input-format" in build_command(request)
    assert "--model" not in build_command(request)
    assert "--effort" not in build_command(request)
    assert native_input(replace(request, backend="codex")) == request.prompt.encode()


def control(**applied):
    """Include sensitive sibling settings to check allowlist extraction."""
    return {
        "type": "control_response",
        "response": {
            "request_id": "leanflow-settings-read",
            "subtype": "success",
            "response": {
                "applied": applied,
                "effective": {"env": {"API_KEY": "private-key"}},
            },
        },
    }


def test_settings_preserve_changes_and_resumes_without_confusing_overrides(tmp_path):
    request = HarnessRequest("claude", "proof", tmp_path, model="opus", effort="high")
    recorder = SettingsRecorder(request, ("private-key",))
    initial = recorder.update({})
    assert initial["requested_model"] == "opus"
    assert initial["model"] == "" and initial["reasoning_effort"] == ""
    actual = recorder.consume(control(model="claude-actual", effort="max"))
    assert actual["model"] == "claude-actual" and actual["reasoning_effort"] == "max"
    assert actual["model_source"] == "native_settings"
    assert "private-key" not in json.dumps(recorder.history)
    assert recorder.consume(control(model="claude-actual", effort="max")) is None
    changed = recorder.consume(
        {"type": "assistant", "message": {"model": "fallback-model", "content": []}}
    )
    assert changed["model_source"] == "native_response"
    assert (
        changed["reasoning_effort"] == ""
    ), "old model's effort must not be attributed to a fallback"
    assert recorder.history[1]["model"] == "claude-actual"
    resumed = SettingsRecorder(replace(request, session_id="same-session"), ())
    assert resumed.current["invocation_id"] != recorder.current["invocation_id"]
    assert resumed.current["model"] == ""
    assert private_control_response(control())
    assert recorder.consume({"type": "assistant", "message": {"model": "<synthetic>"}}) is None
    assert recorder.consume({"type": "user", "message": {"model": "tool-result-model"}}) is None
    assert (
        recorder.consume(
            {"type": "assistant", "parent_tool_use_id": "subagent", "message": {"model": "child"}}
        )
        is None
    )


@pytest.mark.parametrize("backend", ["codex", "lemex"])
def test_real_process_startup_metadata_survives_ephemeral_mode(fake_backend, tmp_path, backend):
    request = fake_backend(
        "import json, sys\nsys.stdin.read()\n"
        + f"print({diagnostic(backend)!r}, file=sys.stderr, flush=True)\n"
        + 'print(json.dumps({"type":"thread.started","thread_id":"session-1"}), flush=True)\n'
        + 'print(json.dumps({"type":"turn.completed"}), flush=True)\n',
        backend,
    )
    events = []
    result = run_harness(
        replace(request, restricted_tools=True, log_path=tmp_path / "events.jsonl"), events.append
    )
    assert result.status == "completed"
    assert result.model_settings[-1]["model"] == "actual-model"
    assert result.model_settings[-1]["reasoning_effort"] == "xhigh"
    assert any(e["type"] == "harness.settings" for e in events)
    assert "SessionConfiguredEvent" not in (tmp_path / "events.jsonl").read_text()
    assert ",codex_exec=info" in child_environment(request)["RUST_LOG"]


def test_real_claude_stream_records_metadata_but_discards_private_control_payloads(
    fake_backend, tmp_path
):
    payload = control(model="claude-actual", effort="max")
    request = fake_backend(
        "import json, sys\nmessages=[json.loads(line) for line in sys.stdin]\n"
        + 'assert len([m for m in messages if m["type"]=="user"])==1\n'
        + f"print(json.dumps({payload!r}), flush=True)\n"
        + 'print(json.dumps({"type":"result","subtype":"success","result":"done"}), flush=True)\n',
        "claude",
    )
    events = []
    result = run_harness(replace(request, log_path=tmp_path / "events.jsonl"), events.append)
    assert result.status == "completed"
    assert result.model_settings[-1]["model"] == "claude-actual"
    assert result.model_settings[-1]["reasoning_effort"] == "max"
    assert "private-key" not in (tmp_path / "events.jsonl").read_text()
    assert all(e["type"] != "control_response" for e in events)


def test_claude_response_identity_wins_over_late_settings_and_keeps_context_effort(tmp_path):
    recorder = SettingsRecorder(HarnessRequest("claude", "", tmp_path), ())
    recorder.consume(control(model="claude-model[1m]", effort="max"))
    actual = recorder.consume({"type": "assistant", "message": {"model": "claude-model"}})
    assert actual["reasoning_effort"] == "max"
    recorder.consume({"type": "assistant", "message": {"model": "fallback"}})
    assert recorder.consume(control(model="claude-model[1m]", effort="max")) is None
    assert recorder.current["model"] == "fallback"
    assert recorder.current["reasoning_effort"] == ""


def test_unset_session_effort_uses_labelled_native_model_default(fake_backend, monkeypatch):
    request = fake_backend(
        "import json, sys\nsys.stdin.read()\n"
        + f"print({diagnostic('codex', 'None')!r}, file=sys.stderr, flush=True)\n"
        + 'print(json.dumps({"type":"turn.completed"}), flush=True)\n',
        "codex",
    )
    calls = []

    def lookup(request, executable, environment, policy, model):
        calls.append(model)
        return "medium"

    monkeypatch.setattr("agent.runtime.harness.runner.reasoning_default", lookup)
    result = run_harness(request)
    assert calls == ["actual-model"]
    assert result.model_settings[-1]["reasoning_effort"] == "medium"
    assert result.model_settings[-1]["reasoning_source"] == "native_model_default"
