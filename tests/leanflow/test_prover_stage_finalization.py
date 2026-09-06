"""Reserve the final admitted planning request for the actual structured result."""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import agent_session as session
from leanflow_cli.workflows.prover.session_transport import (
    build_transport,
    close_transport,
    request_once,
)


def _tool_message(path: str, content: str) -> dict[str, Any]:
    """Return a provider tool call that persists a completed stage artifact."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "save",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": path, "content": content}),
                },
            }
        ],
    }


def test_last_planning_call_returns_report_through_real_chat_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []
    final = json.dumps({"plan": "Use the saved research outline", "nodes": []})

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if len(requests) == 2 and (
                not body.get("tools")
                or body.get("tool_choice") != "none"
                or not any(item.get("role") == "tool" for item in body["messages"])
            ):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(
                    b'{"error":{"message":"Final request must retain schemas/history and choose none"}}'
                )
                return
            if body.get("tool_choice") != "none":
                message = _tool_message(
                    "graph_proposal.json" if len(requests) == 1 else "PLAN_job.md", final
                )
            else:
                message = {"role": "assistant", "content": final}
            response = json.dumps(
                {
                    "id": "bounded",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "bounded-test",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "local")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "chat_completions")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "bounded-test-key")
    try:
        kwargs = dict(
            role="orchestrator",
            prompt="Return JSON with a concrete plan and nodes.",
            project_root=tmp_path,
            workspace=tmp_path / "job",
            config={"model": "bounded-test", "context_tokens": 24000},
            api_budget=2,
            log_path=tmp_path / "session.jsonl",
            context={},
        )
        result = session.run_session(**kwargs)
        assert result["status"] == "completed", result
        assert json.loads(result["final_response"]) == json.loads(final)
        assert len(requests) == result["api_calls"] == 2
        assert requests[0]["tools"] == requests[1]["tools"]
        assert requests[1]["tool_choice"] == "none"
        assert "final" in requests[1]["messages"][-1]["content"].lower()
        assert "graph_proposal.json" in json.dumps(requests[1]["messages"])
        assert (tmp_path / "job/graph_proposal.json").read_text() == final
        assert not (tmp_path / "job/PLAN_job.md").exists()
        assert json.loads((tmp_path / ".runtime/job/request-count.json").read_text()) == {
            "limit": 2,
            "used": 2,
        }
        resumed = session.run_session(**kwargs)
        assert resumed["new_api_calls"] == 0 and len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("role", ["orchestrator", "planner", "research", "review"])
def test_single_call_stage_retains_schemas_and_keeps_one_call_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    observed: list[Any] = []

    def build(_config: Any, schemas: Any) -> Any:
        observed.append(schemas)
        return SimpleNamespace(model="test", tools=schemas)

    def request(
        agent: Any, messages: Any, _timeout: Any, *, final_report_only: bool = False
    ) -> Any:
        assert agent.tools and final_report_only
        assert json.loads((tmp_path / ".runtime/job/request-count.json").read_text())["used"] == 1
        return {"role": "assistant", "content": '{"accepted":true,"plan":"done"}'}, {}

    monkeypatch.setattr(session, "build_transport", build)
    monkeypatch.setattr(session, "request_once", request)
    monkeypatch.setattr(session, "close_transport", lambda *_: None)
    result = session.run_session(
        role=role,
        prompt="Return requested JSON.",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 16000},
        api_budget=1,
        log_path=tmp_path / "session.jsonl",
        context={},
    )
    assert len(observed) == 1 and observed[0]
    assert result["status"] == "completed" and result["api_calls"] == 1


def test_prover_keeps_tools_and_candidate_check_on_its_last_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[str] = []
    monkeypatch.setattr(
        session,
        "build_transport",
        lambda config, schemas: SimpleNamespace(model="test", tools=schemas),
    )
    monkeypatch.setattr(session, "close_transport", lambda *_: None)

    def request(agent: Any, _messages: Any, _timeout: Any) -> Any:
        assert agent.tools
        return _tool_message("candidate.txt", "trivial"), {}

    def feedback(text: str) -> dict[str, Any]:
        checked.append(text)
        return {"accepted": True}

    monkeypatch.setattr(session, "request_once", request)
    result = session.run_session(
        role="prover",
        prompt="Prove goal.",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 16000, "_candidate_feedback": feedback},
        api_budget=1,
        log_path=tmp_path / "session.jsonl",
        context={},
    )
    assert result["status"] == "completed" and result["api_calls"] == 1
    assert checked == [""] and (tmp_path / "job/candidate.txt").read_text() == "trivial"


def test_final_stage_cannot_execute_a_provider_tool_call_without_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        session,
        "build_transport",
        lambda config, schemas: SimpleNamespace(model="test", tools=schemas),
    )
    monkeypatch.setattr(session, "close_transport", lambda *_: None)
    calls: list[bool] = []

    def request(
        agent: Any, _messages: Any, _timeout: Any, *, final_report_only: bool = False
    ) -> Any:
        calls.append(True)
        assert agent.tools and final_report_only
        return _tool_message("unrequested.txt", "must not execute"), {}

    monkeypatch.setattr(session, "request_once", request)
    result = session.run_session(
        role="review",
        prompt="Return JSON.",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 16000},
        api_budget=1,
        log_path=tmp_path / "session.jsonl",
        context={},
    )
    assert result["status"] == "budget_exhausted" and result["api_calls"] == len(calls) == 1
    assert "despite disabled tools" in result["error"]
    assert not (tmp_path / "job/unrequested.txt").exists()


@pytest.mark.parametrize("mode", ["chat_completions", "codex_responses", "anthropic_messages"])
def test_final_request_preserves_real_tool_history_and_provider_contract(
    tmp_path, monkeypatch, mode
):
    """Validate actual SDK HTTP payloads, including native Anthropic and Responses conversion."""
    requests = []
    failures = []
    final = '{"plan":"reported","nodes":[]}'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            try:
                if mode == "anthropic_messages":
                    assert self.path == "/v1/messages"
                    assert body["tool_choice"] == {"type": "none"}
                    assert body["tools"][0]["name"] == "write_file"
                    blocks = [
                        block
                        for item in body["messages"]
                        for block in item["content"]
                        if isinstance(block, dict)
                    ]
                    call = next(block for block in blocks if block["type"] == "tool_use")
                    result = next(block for block in blocks if block["type"] == "tool_result")
                    assert call["id"] == result["tool_use_id"] == "save"
                    assert call["name"] == body["tools"][0]["name"]
                    assert body["thinking"]["type"] == "enabled"
                    assert (
                        next(block for block in blocks if block["type"] == "thinking")["signature"]
                        == "preserved-test-signature"
                    )
                elif mode == "codex_responses":
                    assert self.path == "/v1/responses"
                    assert body["tool_choice"] == "none"
                    assert body["tools"][0]["name"] == "write_file"
                    call = next(
                        item for item in body["input"] if item.get("type") == "function_call"
                    )
                    result = next(
                        item for item in body["input"] if item.get("type") == "function_call_output"
                    )
                    assert call["call_id"] == result["call_id"] == "save"
                else:
                    assert self.path == "/v1/chat/completions"
                    assert body["tool_choice"] == "none"
                    assert body["tools"][0]["function"]["name"] == "write_file"
                    call = next(item for item in body["messages"] if item.get("tool_calls"))[
                        "tool_calls"
                    ][0]
                    result = next(item for item in body["messages"] if item["role"] == "tool")
                    assert call["id"] == result["tool_call_id"] == "save"
            except (AssertionError, KeyError, StopIteration) as error:
                failures.append(repr(error))
                self.send_response(400)
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "error": {
                                "message": "Invalid final request payload contract: "
                                + repr(error)
                                + " "
                                + json.dumps(body)
                            }
                        }
                    ).encode()
                )
                return
            if mode == "anthropic_messages":
                payload = {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5",
                    "content": [{"type": "text", "text": final}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 8, "output_tokens": 4},
                }
            elif mode == "codex_responses":
                item = {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": final, "annotations": []}],
                }
                payload = {
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": "test",
                    "output": [item],
                    "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
                }
                events = [
                    {
                        "type": "response.created",
                        "response": {**payload, "status": "in_progress", "output": []},
                    },
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": payload},
                ]
                encoded = "".join(
                    "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                    for event in events
                ).encode()
            else:
                payload = {
                    "id": "test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": final},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                }
            if mode != "codex_responses":
                encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/event-stream" if mode == "codex_responses" else "application/json",
            )
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "local")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "chat_completions")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "test")
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Save notes",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                },
            },
        }
    ]
    agent = build_transport({"model": "claude-sonnet-4-5", "reasoning_effort": "low"}, schemas)
    agent.api_mode = mode
    if mode == "anthropic_messages":
        import anthropic

        agent._anthropic_client = anthropic.Anthropic(
            api_key="test", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0
        )
    call = _tool_message("PLAN_job.md", "complete outline")
    if mode == "anthropic_messages":
        call["content"] = [
            {
                "type": "thinking",
                "thinking": "retained analysis",
                "signature": "preserved-test-signature",
            }
        ]
    messages = [
        {"role": "system", "content": "Stage contract"},
        {"role": "user", "content": "Research then return JSON"},
        call,
        {
            "role": "tool",
            "tool_call_id": "save",
            "name": "write_file",
            "content": '{"success":true}',
        },
        {"role": "user", "content": "Return the final report now."},
    ]
    previous = copy.deepcopy(messages)
    try:
        assistant, _ = request_once(agent, messages, 5, final_report_only=True)
        assert not failures, failures
        assert assistant["content"] == final
        assert len(requests) == 1
        assert messages == previous and agent.tools == schemas
    finally:
        close_transport(agent)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_final_reporting_context_accounts_for_retained_schemas(tmp_path, monkeypatch):
    """Reject an oversized final payload before consuming its only request."""
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "metadata" * 2000,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    monkeypatch.setattr(session.SessionTools, "schemas", lambda self: schemas)
    monkeypatch.setattr(
        session, "build_transport", lambda *args: pytest.fail("oversized request admitted")
    )
    result = session.run_session(
        role="review",
        prompt="Return the report",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 4000},
        api_budget=1,
        log_path=tmp_path / "session.jsonl",
        context={},
    )
    assert result["status"] == "context_limit" and result["api_calls"] == 0
    assert not (tmp_path / ".runtime/job/request-count.json").exists()
