"""Verify observational events preserve native output without claiming acceptance."""

from __future__ import annotations

import json

import pytest

from agent.runtime.harness.observations import native_observations
from agent.runtime.harness.redaction import environment_secrets, redact_native_value
from agent.runtime.harness.runner import _record
from leanflow_cli.workflows.prover.event_preview import PREVIEW_CHARS, preview_details


@pytest.mark.parametrize("backend", ["codex", "lemex"])
@pytest.mark.parametrize(
    "item_kind,kind", [("agent_message", "agent-output"), ("reasoning", "agent-reasoning")]
)
def test_native_text_is_emitted_once_and_keeps_full_text(backend, item_kind, kind):
    text = "A long public explanation.\n" * 150
    item = {"id": "item_1", "type": item_kind, "text": text}
    assert native_observations(backend, {"type": "item.started", "item": item}) == []
    partial = native_observations(backend, {"type": "item.updated", "item": item})
    assert partial[0][1]["text"] == text
    assert partial[0][1]["partial"] is True
    events = native_observations(backend, {"type": "item.completed", "item": item})
    assert events[0][0] == kind
    assert events[0][1]["text"] == text
    assert len(preview_details(kind, events[0][1])["content_preview"]) <= PREVIEW_CHARS
    assert "accepted" not in events[0][1]


def test_claude_text_thinking_and_tools_exclude_encrypted_thinking_and_user_prompts():
    events = native_observations(
        "claude",
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I will check the lemma."},
                    {
                        "type": "thinking",
                        "thinking": "Public thinking",
                        "signature": "private-signature",
                    },
                    {"type": "redacted_thinking", "data": "encrypted"},
                    {
                        "type": "tool_use",
                        "id": "tool1",
                        "name": "Read",
                        "input": {"file_path": "Main.lean"},
                    },
                ]
            },
        },
    )
    assert [kind for kind, _ in events] == ["agent-output", "agent-reasoning", "agent-tool-start"]
    assert "signature" not in json.dumps(events)
    assert "encrypted" not in json.dumps(events)
    result = native_observations(
        "claude",
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "private user prompt"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool1",
                        "content": "file content",
                        "is_error": True,
                    },
                ]
            },
        },
    )
    assert len(result) == 1
    assert result[0][1]["item_id"] == "tool1"
    assert result[0][1]["status"] == "failed"


def test_native_command_start_finish_and_unknown_events():
    item = {
        "id": "c1",
        "type": "command_execution",
        "command": "lake env lean Main.lean",
        "aggregated_output": "error: failed",
        "exit_code": 1,
        "status": "failed",
    }
    for phase, kind in [("item.started", "agent-tool-start"), ("item.completed", "agent-tool-end")]:
        event = native_observations("lemex", {"type": phase, "item": item})[0]
        assert event[0] == kind
        assert event[1]["exit_code"] == 1
        assert event[1]["result"] == "error: failed"
    assert native_observations("lemex", {"type": "unknown", "item": item}) == []
    assert native_observations("claude", {"type": "assistant", "message": {"content": None}}) == []


def test_native_transcripts_force_redaction_before_logs_and_callbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "false")
    secret = "rcp-key-without-a-known-prefix"
    secrets = environment_secrets({"LEMEX_API_KEY": secret, "PATH": "/usr/bin"})
    event = {
        "type": "item.completed",
        "item": {"text": f"Output {secret}\nnext line", "apiKey": "other-opaque-key"},
    }
    seen = []
    path = tmp_path / "harness.jsonl"
    with path.open("w") as stream:
        _record(stream, event, seen.append, secrets)
    assert secret not in path.read_text()
    assert "other-opaque-key" not in path.read_text()
    assert json.loads(path.read_text()) == seen[0]
    assert seen[0]["item"]["text"].endswith("\nnext line")
    assert event["item"]["text"].startswith(f"Output {secret}")
    assert (
        redact_native_value({"usage": {"input_tokens": 100}}, secrets)["usage"]["input_tokens"]
        == 100
    )
