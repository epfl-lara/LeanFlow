"""Read provider-owned model choices, resolved defaults and reasoning capabilities."""

from __future__ import annotations

import os
import tempfile
from typing import Any

from agent.runtime.harness.redaction import environment_secrets, redact_native_value
from leanflow_cli.runtime.provider_protocol import MetadataSession, MetadataUnavailable


def _text(value: Any) -> str:
    """Keep only bounded strings from native responses."""
    return value.strip()[:512] if isinstance(value, str) else ""


def _efforts(value: Any, key: str = "") -> list[str]:
    """Preserve native effort names without inventing unsupported levels."""
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            text
            for item in value
            if (text := _text(item.get(key) if key and isinstance(item, dict) else item))
        )
    )


def _catalog(source: str, *, status: str = "ready", error: str = "") -> dict[str, Any]:
    """Build the stable editor catalog shape, including unavailable results."""
    return {
        "status": status,
        "source": source,
        "default_model": "",
        "default_reasoning_effort": "",
        "configured_reasoning_effort": "",
        "models": [],
        "error": error,
    }


def _rpc(session: MetadataSession, request_id: int, method: str, params: dict[str, Any]) -> Any:
    """Read one app-server response while ignoring unrelated notifications."""
    session.send({"id": request_id, "method": method, "params": params})
    while True:
        response = session.receive()
        if response.get("id") == request_id:
            if "error" in response:
                raise MetadataUnavailable(f"The native runtime could not read {method} metadata.")
            return response.get("result", {})


def _codex_catalog(executable: str, backend: str, cwd: str) -> dict[str, Any]:
    """Use the same native catalog and layered configuration as Codex or Lemex."""
    result = _catalog("Native app-server model/list and config/read")
    environment = dict(os.environ)
    if backend == "codex" and environment.get("OPENAI_API_KEY"):
        # Match the native harness's explicit-key precedence over saved login.
        environment["CODEX_API_KEY"] = environment["OPENAI_API_KEY"]
    with MetadataSession([executable, "app-server"], env=environment, cwd=cwd) as session:
        _rpc(
            session,
            1,
            "initialize",
            {"clientInfo": {"name": "leanflow-models", "version": "1"}},
        )
        session.send({"method": "initialized"})
        configuration = _rpc(session, 2, "config/read", {"includeLayers": False, "cwd": cwd})
        config = configuration.get("config", {})
        result["default_model"] = _text(config.get("model"))
        result["default_reasoning_effort"] = _text(config.get("model_reasoning_effort"))
        result["configured_reasoning_effort"] = result["default_reasoning_effort"]
        cursor: str | None = None
        cursors: set[str] = set()
        seen: set[str] = set()
        while True:
            response = _rpc(session, 3, "model/list", {"limit": 100, "cursor": cursor})
            for row in response.get("data", []):
                if not isinstance(row, dict) or row.get("hidden") is True:
                    continue
                model = _text(row.get("model")) or _text(row.get("id"))
                if not model or model in seen:
                    continue
                seen.add(model)
                result["models"].append(
                    {
                        "id": model,
                        "label": _text(row.get("displayName")) or model,
                        "reasoning_efforts": _efforts(
                            row.get("supportedReasoningEfforts"), "reasoningEffort"
                        ),
                        "reasoning_known": isinstance(row.get("supportedReasoningEfforts"), list),
                        "default_reasoning_effort": _text(row.get("defaultReasoningEffort")),
                    }
                )
                if not result["default_model"] and row.get("isDefault") is True:
                    result["default_model"] = model
            cursor = _text(response.get("nextCursor"))
            if not cursor:
                break
            if cursor in cursors:
                raise MetadataUnavailable("The native model catalog returned a repeated page.")
            cursors.add(cursor)
        if not result["default_reasoning_effort"]:
            default: dict[str, Any] = next(
                (m for m in result["models"] if m["id"] == result["default_model"]), {}
            )
            result["default_reasoning_effort"] = default.get("default_reasoning_effort", "")
    return result


def _control(session: MetadataSession, request_id: str, subtype: str) -> dict[str, Any]:
    """Read Claude's SDK control response without sending any user message."""
    session.send(
        {"type": "control_request", "request_id": request_id, "request": {"subtype": subtype}}
    )
    while True:
        message = session.receive()
        response = message.get("response", {})
        if message.get("type") == "control_response" and response.get("request_id") == request_id:
            if response.get("subtype") != "success":
                raise MetadataUnavailable("Claude could not report its native model settings.")
            value = response.get("response", {})
            return value if isinstance(value, dict) else {}


def _claude_catalog(executable: str, cwd: str) -> dict[str, Any]:
    """Read Claude's account-aware SDK model catalog and effective model settings."""
    result = _catalog("Native Claude initialize and get_settings")
    command = [
        executable,
        "--print",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        '{"disableAllHooks":true}',
    ]
    with MetadataSession(command, cwd=cwd) as session:
        initialized = _control(session, "models", "initialize")
        seen: set[str] = set()
        for row in initialized.get("models", []):
            if not isinstance(row, dict):
                continue
            model = _text(row.get("value"))
            # The native default alias is resolved by get_settings below; it
            # must not become a second, ambiguous default choice in the picker.
            if not model or model == "default" or model in seen:
                continue
            seen.add(model)
            result["models"].append(
                {
                    "id": model,
                    "label": _text(row.get("displayName")) or model,
                    "resolved_model": _text(row.get("resolvedModel")),
                    "reasoning_efforts": _efforts(row.get("supportedEffortLevels")),
                    "reasoning_known": (
                        isinstance(row.get("supportedEffortLevels"), list)
                        or row.get("supportsEffort") is False
                    ),
                    "default_reasoning_effort": _text(row.get("defaultEffortLevel")),
                }
            )
        try:
            settings = _control(session, "settings", "get_settings")
            applied = settings.get("applied", {})
            result["default_model"] = _text(applied.get("model"))
            result["default_reasoning_effort"] = _text(applied.get("effort"))
        except MetadataUnavailable:
            result["default_model"] = _text(initialized.get("current_model"))
            result["error"] = "This Claude version does not report effective model settings."
    return result


def model_catalog(backend: str, executable: str, *, check: bool) -> dict[str, Any]:
    """Expose only model metadata, never account details, native settings or errors."""
    if not check:
        return _catalog("", status="not_checked")
    if not executable:
        return _catalog(
            "", status="unavailable", error="Install the native runtime to list models."
        )
    try:
        # Native jobs run in filtered snapshots without project runtime config.
        # Inspect the same user/environment defaults, not canonical workspace
        # settings that the actual sandboxed agent cannot read.
        with tempfile.TemporaryDirectory(prefix="leanflow-models-") as directory:
            result = (
                _claude_catalog(executable, directory)
                if backend == "claude"
                else _codex_catalog(executable, backend, directory)
            )
        if not result["models"]:
            result["status"] = "unavailable"
            result["error"] = (
                "The native runtime returned no available models. Check its login and configuration."
            )
        return dict(redact_native_value(result, environment_secrets(os.environ)))
    except MetadataUnavailable as error:
        return _catalog("", status="unavailable", error=str(error))
    except (OSError, TypeError, ValueError, AttributeError):
        return _catalog(
            "", status="unavailable", error="The native model metadata could not be read."
        )
