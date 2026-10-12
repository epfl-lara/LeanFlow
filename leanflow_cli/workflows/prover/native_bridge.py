"""Expose job-bound controller tools and asynchronous operation receipts over MCP."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.workflows.prover.session_tools import SessionTools


def _validate_arguments(value: Any, schema: Mapping[str, Any], path: str = "arguments") -> None:
    """Enforce the JSON Schema subset advertised by this bridge before dispatch."""
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
        "integer": (
            isinstance(value, int)
            and not isinstance(value, bool)
            or isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
        ),
        "array": isinstance(value, list),
        "boolean": isinstance(value, bool),
    }
    if not isinstance(kind, str) or not valid.get(kind, False):
        raise ValueError(f"{path} must be {kind}")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(schema.get("required", ())) - value.keys():
            raise ValueError(f"{path} is missing required properties")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError(f"{path} contains unrecognized properties")
        for key, item in value.items():
            if key in properties:
                _validate_arguments(item, properties[key], f"{path}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{path} contains too few items")
        for item in value:
            _validate_arguments(item, schema["items"], path + "[]")
    elif kind == "string" and "pattern" in schema:
        if re.search(schema["pattern"], value) is None:
            raise ValueError(f"{path} does not match its required pattern")


class NativeBridge:
    """Keep job authority in the parent while native agents control their own loop."""

    def __init__(
        self,
        *,
        toolset: SessionTools,
        context: Mapping[str, Any],
        callbacks: Mapping[str, Callable[[str], dict[str, Any]]],
        directory: Path,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        persistent: bool = False,
        guidance: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.guidance = guidance
        self.persistent = persistent
        self.started = False
        self.closed = False
        self.stop_events: dict[str, threading.Event] = {}
        self.toolset = toolset
        self.context = copy.deepcopy(dict(context))
        self.callbacks = dict(callbacks)
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.on_event = on_event
        self.token = secrets.token_urlsafe(32)
        identity = self.directory / ".server-id"
        if not identity.exists():
            name = (
                "leanflow_job_" + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
            )
            try:
                with identity.open("x") as handle:
                    handle.write(name)
            except FileExistsError:
                pass
        self.server_name = identity.read_text().strip()
        if not re.fullmatch(r"leanflow_job_[a-f0-9]{24}", self.server_name):
            raise ValueError("Invalid persisted native bridge identity")
        self.lock = threading.RLock()
        self.operations: dict[str, dict[str, Any]] = {}
        for receipt in self.directory.glob("*.json"):
            operation = json.loads(receipt.read_text())
            if operation.get("status") == "running":
                operation.update(
                    status="interrupted",
                    error="The owning controller restarted before the operation completed.",
                )
                atomic_json_write(receipt, operation)
            self.operations[str(operation["id"])] = operation
        self.threads: list[threading.Thread] = []
        self.submitted_response = ""
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                """Authenticate one job and dispatch a bounded protocol request."""
                if not secrets.compare_digest(
                    self.headers.get("Authorization", ""), "Bearer " + bridge.token
                ):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 4 * 1024 * 1024:
                        self.send_error(413)
                        return
                    request = json.loads(self.rfile.read(size))
                    payload = bridge.dispatch(request)
                except (ValueError, TypeError, KeyError) as error:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32602, "message": str(error)},
                    }
                encoded = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: Any) -> None:
                """Keep MCP stdout and controller logs free from HTTP diagnostics."""

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> NativeBridge:
        if not self.started:
            self.thread.start()
            self.started = True
        return self

    def __exit__(self, *args: Any) -> None:
        if not self.persistent:
            self.close()

    def drain(self) -> None:
        """Wait for outstanding operation results without imposing a new work deadline."""
        while True:
            with self.lock:
                pending = [thread for thread in self.threads if thread.is_alive()]
            if not pending:
                return
            for thread in pending:
                thread.join()

    def close(self) -> None:
        """Cancel operations and join their cleanup when the owning controller stops."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for event in self.stop_events.values():
                event.set()
        self.drain()
        from leanflow_cli.workflows.prover.check_process import close_check_workers

        close_check_workers(self.toolset.workspace)
        if self.started:
            self.server.shutdown()
            self.thread.join()
        self.server.server_close()

    def mcp_config(self) -> dict[str, Any]:
        """Return a native stdio server specification with this job's capability."""
        return {
            "mcpServers": {
                self.server_name: {
                    "command": sys.executable,
                    "args": ["-m", "leanflow_cli.workflows.prover.mcp_relay"],
                    "env": {
                        "LEANFLOW_BRIDGE_URL": f"http://127.0.0.1:{self.server.server_port}/",
                        "LEANFLOW_BRIDGE_TOKEN": self.token,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
                    },
                }
            }
        }

    def schemas(self) -> list[dict[str, Any]]:
        """Advertise quick tools plus durable start/poll/cancel operations."""

        def schema(
            name: str, description: str, properties: dict[str, Any], required: list[str]
        ) -> dict[str, Any]:
            return {
                "name": name,
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }

        text = {"type": "string"}
        result = [
            schema("get_assignment", "Recover the immutable assignment after compaction.", {}, []),
            schema(
                "list_operations",
                "Recover durable operation IDs and receipts after compaction or reconnect.",
                {},
                [],
            ),
            schema(
                "poll_operation",
                "Read an operation receipt without stopping ongoing work.",
                {"operation_id": text},
                ["operation_id"],
            ),
            schema(
                "cancel_operation",
                "Request cancellation of a running operation.",
                {"operation_id": text},
                ["operation_id"],
            ),
        ]
        result.append(
            schema(
                "start_command",
                "Run a long command in the private project copy. No per-command timeout; poll_operation returns completion and cancel_operation stops its process tree.",
                {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
                ["argv"],
            )
        )
        if self.guidance is not None:
            result.append(
                schema(
                    "get_guidance",
                    "Read new user guidance addressed to this job and retained instructions.",
                    {},
                    [],
                )
            )
        for item in self.toolset.schemas():
            tool = item["function"]
            result.append(
                {
                    "name": "start_" + tool["name"],
                    "description": tool["description"]
                    + " Returns an operation ID immediately; poll_operation retrieves its result.",
                    "inputSchema": tool["parameters"],
                }
            )
        for name in self.callbacks:
            result.append(
                schema(
                    name,
                    "Start controller validation; poll the returned operation ID. Only an accepted controller receipt establishes acceptance.",
                    {"response": text},
                    ["response"],
                )
            )
        if len({item["name"] for item in result}) != len(result):
            raise ValueError("Native bridge tool names must be unique")
        return sorted(result, key=lambda item: item["name"])

    def dispatch(self, request: Any) -> dict[str, Any]:
        """Implement the small MCP server surface used by supported native runtimes."""
        if (
            not isinstance(request, dict)
            or request.get("jsonrpc") != "2.0"
            or not isinstance(request.get("method"), str)
            or isinstance(request.get("id"), bool)
            or not isinstance(request.get("id"), (str, int, type(None)))
        ):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid JSON-RPC request"},
            }
        params = request.get("params", {})
        if not isinstance(params, dict):
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32602, "message": "MCP parameters must be an object"},
            }
        method = request.get("method")
        if method == "initialize":
            if not isinstance(params.get("protocolVersion", "2024-11-05"), str):
                return {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32602, "message": "MCP protocolVersion must be a string"},
                }
            result: Any = {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "leanflow-job", "version": "2"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.schemas()}
        elif method == "tools/call":
            try:
                if not isinstance(params.get("name"), str):
                    raise ValueError("Tool name must be a string")
                value = self.call(params["name"], params.get("arguments", {}))
                result = {
                    "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                    "isError": False,
                }
            except (ValueError, KeyError, TypeError) as error:
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
        else:
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32601, "message": "Unsupported MCP method"},
            }
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Bind operations to this job rather than accepting a caller-supplied job ID."""
        schema = next((item for item in self.schemas() if item["name"] == name), None)
        if schema is None:
            raise ValueError("Tool is not permitted for this assignment")
        _validate_arguments(arguments, schema["inputSchema"])
        arguments = copy.deepcopy(arguments)
        with self.lock:
            if self.closed:
                raise ValueError("This job bridge is closed")
        if name == "get_assignment":
            return copy.deepcopy(self.context)
        if name == "get_guidance" and self.guidance is not None:
            return self.guidance()
        if name == "list_operations":
            with self.lock:
                return {"operations": copy.deepcopy(list(self.operations.values()))}
        if name in {"poll_operation", "cancel_operation"}:
            with self.lock:
                operation = self.operations[str(arguments["operation_id"])]
                if name == "cancel_operation" and operation["status"] == "running":
                    operation["cancel_requested"] = True
                    self.stop_events[operation["id"]].set()
                    self._save(operation)
                result = copy.deepcopy(operation)
                if self.guidance is not None:
                    result["guidance"] = self.guidance()
                return result
        if name == "start_command":
            from leanflow_cli.workflows.prover.native_command import run_command

            argv = arguments.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(arg, str) for arg in argv)
            ):
                raise ValueError("argv must be a nonempty array of strings")
            action = lambda: run_command(
                argv,
                snapshot=self.toolset.project_root,
                directory=self.toolset.workspace / ".command-home",
                deadline=self.toolset.deadline,
                cancelled=self.toolset.cancelled,
                network_allowed=self.toolset.allow_internet,
            )
        elif name in self.callbacks:
            response = str(arguments["response"])
            action = lambda: self.callbacks[name](response)
        elif name.startswith("start_") and name[6:] in {
            item["function"]["name"] for item in self.toolset.schemas()
        }:
            action = lambda: self.toolset.invoke(name[6:], arguments)
        else:
            raise ValueError("Tool is not permitted for this assignment")
        operation = {
            "id": uuid.uuid4().hex,
            "tool": name,
            "status": "running",
            "cancel_requested": False,
        }

        def run() -> None:
            try:
                if self.on_event:
                    self.on_event("tool-start", {"tool": name, "operation_id": operation["id"]})
                from core.deadlines import operation_cancellation

                with operation_cancellation(self.stop_events[operation["id"]].is_set):
                    result = action()
                with self.lock:
                    operation.update(status="completed", result=result)
                    if (
                        name in {"submit_candidate", "submit_report"}
                        and result.get("accepted") is True
                    ):
                        self.submitted_response = str(arguments["response"])
            except Exception as error:
                with self.lock:
                    operation.update(status="failed", error=str(error))
            finally:
                with self.lock:
                    self._save(operation)
                if self.on_event:
                    self.on_event(
                        "tool-result",
                        {
                            "tool": name,
                            "result": operation.get("result", {"error": operation.get("error")}),
                            "operation_id": operation["id"],
                            "arguments": dict(arguments),
                        },
                    )

        thread = threading.Thread(target=run, daemon=True)
        # Publish and start under the same lock as close/drain so cleanup cannot
        # miss an admitted operation whose worker has not started yet.
        with self.lock:
            if self.closed:
                raise ValueError("This job bridge is closed")
            self.operations[operation["id"]] = operation
            self.stop_events[operation["id"]] = threading.Event()
            self._save(operation)
            self.threads.append(thread)
            thread.start()
        return {"operation_id": operation["id"], "status": "running"}

    def _save(self, operation: dict[str, Any]) -> None:
        """Keep results outside native-agent write authority."""
        atomic_json_write(self.directory / (operation["id"] + ".json"), operation)
