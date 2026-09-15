"""Reserve report recovery before planning tools consume the stage call ceiling."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import agent_session as session


def tool_response(index: int) -> dict[str, Any]:
    """Return a distinct scratch write so this test measures budgeting, not loop detection."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"save-{index}",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": f"note-{index}.txt", "content": "finding"}),
                },
            }
        ],
    }


@pytest.mark.parametrize("budget", [2, 3, 50])
@pytest.mark.parametrize("role", ["orchestrator", "review", "research"])
@pytest.mark.parametrize("unusable", ["blank", "truncated", "tool_call"])
def test_report_at_budget_boundary_has_one_remaining_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: int, role: str, unusable: str
) -> None:
    """Exercise tool work, a failed final report, and successful recovery within one ledger."""
    calls: list[bool] = []
    final = '{"plan":"Saved findings and uncertainties","nodes":[]}'
    monkeypatch.setattr(session, "build_transport", lambda *_: SimpleNamespace(model="test"))
    monkeypatch.setattr(session, "close_transport", lambda *_: None)

    def request(agent: Any, messages: Any, timeout: Any, **kwargs: Any) -> Any:
        reporting = kwargs.get("final_report_only", False)
        calls.append(reporting)
        if not reporting:
            return tool_response(len(calls)), {}
        if calls.count(True) == 1:
            if unusable == "tool_call":
                return tool_response(len(calls)), {}
            return {
                "role": "assistant",
                "content": " " if unusable == "blank" else '{"plan":',
                "finish_reason": "stop" if unusable == "blank" else "length",
            }, {}
        if unusable == "tool_call":
            assert any(
                m.get("role") == "tool" and m.get("tool_call_id") == f"save-{budget - 1}"
                for m in messages
            )
            assert not (tmp_path / "job" / f"note-{budget - 1}.txt").exists()
        return {"role": "assistant", "content": final, "finish_reason": "stop"}, {}

    monkeypatch.setattr(session, "request_once", request)
    kwargs = dict(
        role=role,
        prompt="Return JSON with plan and nodes.",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 24000},
        api_budget=budget,
        log_path=tmp_path / "events.jsonl",
        context={},
    )
    result = session.run_session(**kwargs)
    assert result["status"] == "completed", result
    assert result["final_response"] == final
    assert calls == [False] * (budget - 2) + [True, True]
    assert result["api_calls"] == budget
    ledger = json.loads((tmp_path / ".runtime/job/request-count.json").read_text())
    assert (ledger["used"], ledger["limit"]) == (budget, budget)
    resumed = session.run_session(**kwargs)
    assert resumed["status"] == "completed" and resumed["new_api_calls"] == 0
    assert len(calls) == budget


def test_completed_stage_reopens_without_spending_reserved_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain an ordinary successful report even when its unused recovery call remains."""
    calls: list[bool] = []
    monkeypatch.setattr(session, "build_transport", lambda *_: SimpleNamespace(model="test"))
    monkeypatch.setattr(session, "close_transport", lambda *_: None)

    def request(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        return {"role": "assistant", "content": '{"plan":"done"}'}, {}

    monkeypatch.setattr(session, "request_once", request)
    kwargs = dict(
        role="orchestrator",
        prompt="Return the plan.",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 16000},
        api_budget=50,
        log_path=tmp_path / "events.jsonl",
        context={},
    )
    first = session.run_session(**kwargs)
    reopened = session.run_session(**kwargs)
    assert first["status"] == reopened["status"] == "completed"
    assert reopened["final_response"] == first["final_response"]
    assert reopened["new_api_calls"] == 0 and len(calls) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_repeated_reads_report_and_recover_through_real_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    """Replay PB028's read loop and blank report through real tool execution and HTTP."""
    source = tmp_path / "Research.txt"
    source.write_text("The same mathematical passage.\n" * 30)
    requests: list[dict[str, Any]] = []
    final = '{"plan":"Use existing evidence; remaining uncertainty recorded","nodes":[]}'
    reporting_at = 2 if batch else 7

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            index = len(requests)
            if index < reporting_at:
                message = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"read-{index}-{item}",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps(
                                    {"path": str(source), "offset": 0, "limit": 4000}
                                ),
                            },
                        }
                        for item in range(13 if batch else 1)
                    ],
                }
                reason = "tool_calls"
            else:
                message = {"role": "assistant", "content": " " if index == reporting_at else final}
                reason = "stop"
            response = json.dumps(
                {
                    "id": f"read-replay-{index}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "moonshotai/Kimi-K2.7-Code",
                    "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "local")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "chat_completions")
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_BASE_URL",
        f"http://127.0.0.1:{server.server_port}/inference.rcp.epfl.ch/v1",
    )
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "local-replay-key")
    try:
        kwargs = dict(
            role="orchestrator",
            prompt="Return JSON with plan and nodes.",
            project_root=tmp_path,
            workspace=tmp_path / "job",
            config={"model": "moonshotai/Kimi-K2.7-Code", "reasoning_effort": "high"},
            api_budget=50,
            log_path=tmp_path / "events.jsonl",
            context={},
        )
        result = session.run_session(**kwargs)
        assert result["status"] == "completed", result
        assert result["final_response"] == final
        assert result["api_calls"] == len(requests) == reporting_at + 1
        assert all(r["tool_choice"] == "none" for r in requests[reporting_at - 1 :])
        events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
        notices = [e["type"] for e in events if e["type"].startswith("tool-progress-")]
        assert notices == ["tool-progress-warn", "tool-progress-report"]
        tool_results = [e["details"]["result"] for e in events if e["type"] == "tool-result"]
        assert sum(r.get("success") is True for r in tool_results) == 6
        assert sum(r.get("status") == "stage_report_required" for r in tool_results) == (
            7 if batch else 0
        )
        # Every requested tool has a reply, including suppressed calls in a batch.
        messages = requests[reporting_at - 1]["messages"]
        call_ids = {c["id"] for m in messages for c in m.get("tool_calls", [])}
        result_ids = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
        assert call_ids == result_ids
        ledger = json.loads((tmp_path / ".runtime/job/request-count.json").read_text())
        assert ledger["limit"] == 50 and ledger["used"] == reporting_at + 1
        reopened = session.run_session(**kwargs)
        assert reopened["new_api_calls"] == 0 and reopened["final_response"] == final
        assert len(requests) == reporting_at + 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
