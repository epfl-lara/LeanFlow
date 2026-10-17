"""Discover native MCP configuration without loading or copying authentication files."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections.abc import Mapping, Sequence

from .models import BackendError, HarnessRequest, NativeCleanupUnverified
from .processes import ProcessTree, cleanup_group, process_exited, require_output_eof
from .resources import SAMPLE_INTERVAL_S, NativeResourceSampler, native_memory_limit_bytes

DISCOVERY_TIMEOUT_S = 30.0
MAX_OUTPUT_BYTES = 1024**2


def discover_mcp_servers(
    request: HarnessRequest, command: Sequence[str], env: Mapping[str, str]
) -> dict[str, dict[str, str | bool]]:
    """Ask the native CLI for effective names and discard all transport credentials.

    The caller supplies the same OS sandbox as the invocation. This metadata process
    does not start an agent conversation and is never charged as an agent call.
    """
    memory_limit = native_memory_limit_bytes(env)
    deadline = min(
        request.deadline if request.deadline is not None else float("inf"),
        time.monotonic() + DISCOVERY_TIMEOUT_S,
    )
    process = subprocess.Popen(
        command,
        cwd=request.cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    tree = ProcessTree(process.pid, process=process)
    sampler = NativeResourceSampler(process, tree, memory_limit)
    outputs_closed = False
    cleaned = False
    drain_deadline: float | None = None
    sample_at = 0.0
    stdout = bytearray()
    output_bytes = 0
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map() or not process_exited(process):
                now = time.monotonic()
                if not cleaned:
                    if now >= sample_at:
                        sampler.sample()
                        sample_at = now + SAMPLE_INTERVAL_S
                    if request.cancelled is not None and request.cancelled():
                        raise BackendError(
                            "Native configuration discovery was cancelled by the campaign."
                        )
                    if now >= deadline:
                        raise BackendError("Native configuration discovery exceeded its deadline.")
                    if process_exited(process):
                        # Keep the original PID reserved until its owned group is stopped.
                        cleanup_group(process, tree)
                        cleaned = True
                        drain_deadline = time.monotonic() + 2
                for key, _ in selector.select(timeout=0.1):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    output_bytes += len(data)
                    if output_bytes > MAX_OUTPUT_BYTES:
                        raise BackendError(
                            "Native configuration discovery exceeded its output limit."
                        )
                    if key.fileobj is process.stdout:
                        stdout.extend(data)
                if (
                    drain_deadline is not None
                    and time.monotonic() >= drain_deadline
                    and selector.get_map()
                ):
                    raise NativeCleanupUnverified(
                        "Native configuration descendants did not close output channels; "
                        "retain invocation inputs"
                    )
            outputs_closed = not bool(selector.get_map())
        if not cleaned:
            cleanup_group(process, tree)
            cleaned = True
        if process.returncode != 0:
            raise BackendError(
                "Native MCP configuration discovery failed; refusing to inherit unverified external tools."
            )
        try:
            entries = json.loads(stdout)
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                for entry in entries
            ):
                raise ValueError("Expected a native MCP server list")
            disabled: dict[str, dict[str, str | bool]] = {}
            for entry in entries:
                name = entry["name"]
                if not name or name in disabled:
                    raise ValueError("Native MCP server names must be nonempty and unique")
                transport = entry.get("transport", {})
                if not isinstance(transport, dict):
                    raise ValueError("Expected a native MCP transport object")
                if transport.get("type") == "stdio":
                    disabled[name] = {"enabled": False, "command": "/usr/bin/false"}
                elif transport.get("type") == "streamable_http":
                    disabled[name] = {"enabled": False, "url": "http://127.0.0.1"}
                else:
                    raise ValueError("Unknown native MCP transport")
            return disabled
        except (ValueError, TypeError) as exc:
            raise BackendError(
                "Native MCP configuration discovery returned an unsupported response."
            ) from exc
    finally:
        try:
            if not cleaned:
                cleanup_group(process, tree)
            if not outputs_closed:
                try:
                    require_output_eof(process, timeout_s=2, description="Native configuration")
                except NativeCleanupUnverified as error:
                    raise NativeCleanupUnverified(
                        "Native configuration descendants did not close output channels; "
                        "retain invocation inputs"
                    ) from error
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
