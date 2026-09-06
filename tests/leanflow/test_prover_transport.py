"""Count physical provider requests and protect Codex request preflight ordering."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover.session_transport import (
    build_transport,
    close_transport,
    request_once,
)


def test_rate_limit_does_not_trigger_hidden_sdk_retries(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = b'{"error":{"message":"bounded test limit","type":"rate_limit_error"}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "local")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "chat_completions")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "bounded-test-key")
    agent = build_transport({"model": "bounded-test"}, [])
    try:
        with pytest.raises(Exception, match="bounded test limit"):
            request_once(
                agent,
                [{"role": "system", "content": "contract"}, {"role": "user", "content": "goal"}],
                2,
            )
        assert len(requests) == 1
    finally:
        close_transport(agent)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_codex_timeout_is_added_after_supported_payload_preflight() -> None:
    dispatched = []
    response = SimpleNamespace(usage={"input_tokens": 2}, output=[])

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def get_final_response(self):
            return response

        def __iter__(self):
            yield SimpleNamespace(type="response.output_item.done", item={"content": "done"})

    def stream(**kwargs):
        dispatched.append(kwargs)
        return Stream()

    def preflight(kwargs):
        assert "timeout" not in kwargs
        assert "max_output_tokens" not in kwargs
        assert "temperature" not in kwargs
        return kwargs

    def repair(response, collected):
        assert response.output == []
        response.output = list(collected.values())
        return response

    agent = SimpleNamespace(
        api_mode="codex_responses",
        _build_api_messages_for_turn=lambda messages, system: messages,
        _build_api_kwargs=lambda messages: {
            "model": "codex",
            "input": messages,
            "max_output_tokens": 8192,
            "temperature": 1,
        },
        _create_request_openai_client=lambda **kwargs: SimpleNamespace(
            responses=SimpleNamespace(stream=stream)
        ),
        _close_request_openai_client=lambda *args, **kwargs: None,
        _preflight_codex_api_kwargs=preflight,
        _collect_responses_stream_output_item=lambda event, collected: collected.update(
            {0: event.item}
        ),
        _repair_empty_responses_stream_output=repair,
        _normalize_codex_response=lambda value: (value.output[0], "stop"),
        _build_assistant_message=lambda message, reason: message,
    )
    message, usage = request_once(
        agent, [{"role": "system", "content": "contract"}, {"role": "user", "content": "goal"}], 3
    )
    assert message["content"] == "done"
    assert usage["input_tokens"] == 2
    assert len(dispatched) == 1 and dispatched[0]["timeout"] == 3
