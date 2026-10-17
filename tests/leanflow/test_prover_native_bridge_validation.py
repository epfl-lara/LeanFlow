"""Check stable native tool identities and enforce the bridge's advertised schemas."""

from __future__ import annotations

import json
import shutil
import socket
import threading
from pathlib import Path

import pytest

from leanflow_cli.workflows.prover.native_bridge import NativeBridge
from leanflow_cli.workflows.prover.session_tools import SessionTools


@pytest.fixture
def bridge(tmp_path: Path):
    tools = SessionTools(role="research", project_root=tmp_path, workspace=tmp_path, context={})
    calls = []

    def submit(response):
        calls.append(response)
        return {"accepted": True}

    instance = NativeBridge(
        toolset=tools,
        context={},
        callbacks={"submit_report": submit},
        directory=tmp_path / "bridge",
    )
    try:
        yield instance, calls
    finally:
        instance.close()


def test_native_bridge_name_and_schema_order_survive_restart_and_resume_copy(bridge, tmp_path):
    first, _ = bridge
    original_name = first.server_name
    original_token = first.token
    schemas = first.schemas()
    assert [item["name"] for item in schemas] == sorted(item["name"] for item in schemas)
    first.close()
    copied = tmp_path / "resumed-bridge"
    shutil.copytree(first.directory, copied)
    for directory in (first.directory, copied):
        other = NativeBridge(
            toolset=first.toolset, context={}, callbacks=first.callbacks, directory=directory
        )
        try:
            assert other.server_name == original_name
            assert other.token != original_token
            assert other.schemas() == schemas
        finally:
            other.close()
    assert str(tmp_path) not in original_name


@pytest.mark.parametrize("drip", [False, True])
def test_bridge_shutdown_is_bounded_when_an_authenticated_client_stalls_mid_body(
    bridge, monkeypatch, drip
):
    instance, _ = bridge
    entered = threading.Event()
    handler_finished = threading.Event()
    finished = threading.Event()
    stop_drip = threading.Event()
    original = instance.server.RequestHandlerClass.do_POST

    def observe(handler):
        entered.set()
        try:
            original(handler)
        finally:
            handler_finished.set()

    monkeypatch.setattr(instance.server.RequestHandlerClass, "do_POST", observe)
    instance.__enter__()
    client = socket.create_connection(instance.server.server_address, timeout=1)
    worker = threading.Thread(target=lambda: (instance.close(), finished.set()), daemon=True)

    def feed() -> None:
        while not stop_drip.wait(0.1):
            try:
                client.sendall(b" ")
            except OSError:
                return

    feeder = threading.Thread(target=feed, daemon=True)
    try:
        client.sendall(
            (
                f"POST / HTTP/1.0\r\nAuthorization: Bearer {instance.token}\r\nContent-Length: 64\r\n\r\n"
                + "{"
            ).encode()
        )
        assert entered.wait(1)
        if drip:
            feeder.start()
        worker.start()
        assert finished.wait(3), "A partial HTTP body must not deadlock bridge shutdown"
        assert handler_finished.is_set(), "Closed bridge must not leave an HTTP reader alive"
        assert instance.closed and instance.operations == {}
        assert instance._connections == set()
    finally:
        stop_drip.set()
        client.close()
        if feeder.ident is not None:
            feeder.join(timeout=1)
        assert not feeder.is_alive()
        if worker.ident is not None:
            worker.join(timeout=3)
        assert not worker.is_alive()


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("submit_report", {"response": {"accepted": True}}),
        ("submit_report", {"response": "{}", "job_id": "another-job"}),
        ("submit_report", {}),
        ("submit_report", None),
        ("submit_report", []),
        ("start_read_file", {"path": "Main.lean", "offset": True}),
        ("start_read_file", {"path": 5}),
        ("start_command", {"argv": []}),
        ("start_command", {"argv": ["echo", None]}),
        ("start_fetch_resource", {"url": "https://example.invalid", "filename": "../outside"}),
        ("get_assignment", {"unexpected": "value"}),
        ("not_a_tool", {}),
    ],
)
def test_invalid_tool_arguments_cannot_start_operations_or_call_controller(bridge, tool, arguments):
    instance, calls = bridge
    result = instance.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    assert result["result"]["isError"] is True
    assert calls == [] and instance.operations == {} and instance.threads == []


@pytest.mark.parametrize(
    "payload,code",
    [
        (None, -32600),
        ([], -32600),
        ({"jsonrpc": "1.0", "method": "tools/list", "id": 1}, -32600),
        ({"jsonrpc": "2.0", "method": [], "id": 1}, -32600),
        ({"jsonrpc": "2.0", "method": "tools/list", "id": True}, -32600),
        ({"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": []}, -32602),
        (
            {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {"protocolVersion": []}},
            -32602,
        ),
        ({"jsonrpc": "2.0", "method": "unknown", "id": 1}, -32601),
    ],
)
def test_malformed_json_rpc_returns_structured_error(bridge, payload, code):
    instance, calls = bridge
    result = instance.dispatch(payload)
    assert result["jsonrpc"] == "2.0"
    assert result["error"]["code"] == code
    assert calls == []


def test_advertised_valid_callback_and_poll_are_dispatched_exactly_once(bridge):
    instance, calls = bridge
    result = instance.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "start",
            "method": "tools/call",
            "params": {"name": "submit_report", "arguments": {"response": '{"proof":"trivial"}'}},
        }
    )
    assert result["result"]["isError"] is False
    receipt = json.loads(result["result"]["content"][0]["text"])
    instance.drain()
    polled = instance.call("poll_operation", {"operation_id": receipt["operation_id"]})
    assert polled["status"] == "completed" and polled["result"]["accepted"]
    assert calls == ['{"proof":"trivial"}']


@pytest.mark.parametrize("message", ["", "PRIVATE checker exception"])
def test_failed_check_exception_emits_an_explicit_failure_receipt(bridge, monkeypatch, message):
    instance, _ = bridge
    instance.toolset.role = "prover"
    events = []
    instance.on_event = lambda kind, detail: events.append((kind, detail))

    def fail(name, arguments):
        raise RuntimeError(message)

    monkeypatch.setattr(instance.toolset, "invoke", fail)
    receipt = instance.call("start_lean_check", {})
    instance.drain()
    operation = instance.call("poll_operation", {"operation_id": receipt["operation_id"]})
    assert operation["status"] == "failed" and operation["error"] == message
    assert "result" not in operation
    assert [kind for kind, _ in events] == ["tool-start", "tool-result"]
    detail = events[-1][1]
    assert detail["operation_id"] == receipt["operation_id"]
    assert detail["result"] == {"success": False, "error": message}


@pytest.mark.parametrize(
    "offset,accepted",
    [
        (1, True),
        (1.0, True),
        (0.0, True),
        (True, False),
        (1.5, False),
        (float("inf"), False),
        (float("-inf"), False),
        (float("nan"), False),
    ],
)
def test_integer_schema_accepts_integral_json_numbers_only(bridge, tmp_path, offset, accepted):
    instance, _ = bridge
    (tmp_path / "source.txt").write_text("abc")
    result = instance.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "start_read_file",
                "arguments": {"path": "source.txt", "offset": offset},
            },
        }
    )
    assert result["result"]["isError"] is not accepted
    if accepted:
        receipt = json.loads(result["result"]["content"][0]["text"])
        instance.drain()
        operation = instance.call("poll_operation", {"operation_id": receipt["operation_id"]})
        assert operation["status"] == "completed" and operation["result"]["success"]
        assert operation["result"]["content"] == "abc"[int(offset) :]
    else:
        assert instance.operations == {}
