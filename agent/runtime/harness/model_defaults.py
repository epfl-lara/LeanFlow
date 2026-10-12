"""Read a native model's advertised effort only when session telemetry leaves it unset."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .isolation import IsolationPolicy, IsolationUnavailable, sandbox_command
from .metadata import MetadataSession, MetadataUnavailable
from .models import HarnessRequest


def reasoning_default(
    request: HarnessRequest,
    executable: str,
    environment: Mapping[str, str],
    policy: IsolationPolicy,
    model: str,
) -> str:
    """Read model/list in the invocation's environment without starting a model turn.

    The returned value is labelled as a native model default, not per-response
    inference telemetry. Discovery failure cannot change or fail the agent call.
    """
    timeout = min(3.0, request.deadline - time.monotonic()) if request.deadline else 3.0
    if timeout <= 0 or (request.cancelled and request.cancelled()):
        return ""
    try:
        command = sandbox_command(
            [
                executable,
                "app-server",
                "-c",
                "features.hooks=false",
                "-c",
                "features.plugins=false",
            ],
            policy,
            writable=False,
        )
        with MetadataSession(
            command, timeout=timeout, env=environment, cwd=str(request.cwd)
        ) as session:

            def rpc(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
                """Ignore unrelated notifications and discard private error diagnostics."""
                session.send({"id": identifier, "method": method, "params": params})
                while True:
                    message = session.receive()
                    if message.get("id") == identifier:
                        if "error" in message or not isinstance(message.get("result"), dict):
                            raise MetadataUnavailable("Model metadata unavailable")
                        return message["result"]

            rpc(1, "initialize", {"clientInfo": {"name": "leanflow-settings", "version": "1"}})
            session.send({"method": "initialized"})
            cursor: str | None = None
            seen: set[str] = set()
            while True:
                response = rpc(2, "model/list", {"limit": 100, "cursor": cursor})
                for row in response.get("data", []):
                    if isinstance(row, dict) and (row.get("model") or row.get("id")) == model:
                        effort = row.get("defaultReasoningEffort")
                        return effort if isinstance(effort, str) and 0 < len(effort) < 100 else ""
                cursor = response.get("nextCursor")
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    return ""
                seen.add(cursor)
    except (MetadataUnavailable, IsolationUnavailable, OSError, TypeError, ValueError):
        return ""
