"""Discover native MCP configuration without loading or copying authentication files."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping, Sequence

from .models import BackendError, HarnessRequest
from .processes import cleanup_group


def discover_mcp_servers(
    request: HarnessRequest, command: Sequence[str], env: Mapping[str, str]
) -> dict[str, dict[str, str | bool]]:
    """Ask the native CLI for effective names and discard all transport credentials.

    The caller supplies the same OS sandbox as the invocation. This metadata process
    does not start an agent conversation and is never charged as an agent call.
    """
    process = subprocess.Popen(
        command,
        cwd=request.cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        while True:
            if (request.cancelled is not None and request.cancelled()) or (
                request.deadline is not None and time.monotonic() >= request.deadline
            ):
                raise BackendError("Native configuration discovery was cancelled by the campaign.")
            try:
                stdout, _ = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
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
        cleanup_group(process)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
