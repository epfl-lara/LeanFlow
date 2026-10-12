"""Record bounded model settings emitted by the native invocation, never user defaults."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .models import HarnessRequest
from .redaction import redact_native_value

_INIT = "leanflow-settings-init"
_SETTINGS = "leanflow-settings-read"
_QUOTED = r'"(?:[^"\\]|\\.)*"'


def native_input(request: HarnessRequest) -> bytes:
    """Ask Claude for settings in the same process before submitting its one user prompt."""
    if request.backend != "claude":
        return request.prompt.encode()
    messages = [
        {"type": "control_request", "request_id": _INIT, "request": {"subtype": "initialize"}},
        {
            "type": "control_request",
            "request_id": _SETTINGS,
            "request": {"subtype": "get_settings"},
        },
        {"type": "user", "message": {"role": "user", "content": request.prompt}},
    ]
    return ("\n".join(json.dumps(message) for message in messages) + "\n").encode()


def _text(value: Any) -> str:
    """Accept short scalar metadata, excluding unbounded or multiline diagnostics."""
    return value if isinstance(value, str) and 0 < len(value) <= 512 and "\n" not in value else ""


def _model(value: Any) -> str:
    """Reject synthetic error-message models that did not perform inference."""
    value = _text(value)
    return value if not value.startswith("<") else ""


def startup_settings(backend: str, line: str) -> dict[str, str] | None:
    """Read Codex-compatible startup telemetry without retaining the full configuration.

    Exec's JSON stream omits SessionConfigured. Its narrowly enabled codex_exec
    INFO diagnostic reports the same native start/resume response, even for
    ephemeral clean-room sessions. Unknown diagnostic formats stay unreported.
    """
    name = "Lemex" if backend == "lemex" else "Codex"
    marker = f"codex_exec: {name} initialized with event: SessionConfiguredEvent {{"
    if backend == "claude" or marker not in line:
        return None
    payload = line.split(marker, 1)[1]
    model = re.search(r", model: (" + _QUOTED + r"), model_provider_id:", payload)
    if not model:
        return None
    try:
        value = _text(json.loads(model[1]))
    except ValueError:
        return None
    if not value:
        return None
    result = {"model": value, "model_source": "native_session"}
    effort = re.search(r", reasoning_effort: Some\((Custom\(" + _QUOTED + r"\)|\w+)\)", payload)
    if effort:
        raw = effort[1]
        try:
            value = json.loads(raw[7:-1]) if raw.startswith("Custom(") else raw.lower()
        except ValueError:
            value = ""
        if _text(value):
            result.update(reasoning_effort=value, reasoning_source="native_session")
    session = re.search(r"thread_id: ThreadId \{ uuid: ([\w-]+) \}", payload)
    if session:
        result["session_id"] = session[1]
    return result


class SettingsRecorder:
    """Keep per-invocation settings changes separate from requested overrides and usage."""

    def __init__(self, request: HarnessRequest, secrets: tuple[str, ...]) -> None:
        self.secrets = secrets
        self.current: dict[str, Any] = {
            "invocation_id": uuid.uuid4().hex,
            "backend": request.backend,
            "session_id": request.session_id if not request.restricted_tools else "",
            "requested_model": request.model,
            "requested_reasoning_effort": request.effort,
            "model": "",
            "model_source": "unreported",
            "reasoning_effort": "",
            "reasoning_source": "unreported",
        }
        self.history: list[dict[str, Any]] = []

    def update(self, fields: dict[str, str]) -> dict[str, Any] | None:
        """Publish changed settings, clearing stale effort when the runtime switches models."""
        fields = dict(fields)
        # Claude's context-window suffix may disappear in response model IDs.
        # It is not a switch to another model or another effort policy.
        same_model = fields.get("model", "").removesuffix("[1m]") == str(
            self.current["model"]
        ).removesuffix("[1m]")
        if self.current["model_source"] == "native_response" and fields.get("model_source") in {
            "native_session",
            "native_settings",
        }:
            if not same_model:
                fields.pop("reasoning_effort", None)
                fields.pop("reasoning_source", None)
            fields.pop("model", None)
            fields.pop("model_source", None)
        updated = {**self.current, **fields}
        if fields.get("model") and not same_model:
            updated["reasoning_effort"] = fields.get("reasoning_effort", "")
            updated["reasoning_source"] = fields.get("reasoning_source", "unreported")
        updated = redact_native_value(updated, self.secrets)
        if self.history and updated == self.current:
            return None
        self.current = updated
        record = {**updated, "observed_at": datetime.now(UTC).isoformat()}
        self.history.append(record)
        return {"type": "harness.settings", **record}

    def consume(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Extract settings from native protocol envelopes, never tool or assistant text."""
        kind = event.get("type")
        fields: dict[str, str] = {}
        session = _text(
            event.get("thread_id") if kind == "thread.started" else event.get("session_id")
        )
        if session:
            fields["session_id"] = session
        if self.current["backend"] == "claude":
            data: dict[str, Any] = {}
            source = ""
            response = event.get("response")
            if kind == "control_response" and isinstance(response, dict):
                if response.get("request_id") == _SETTINGS and response.get("subtype") == "success":
                    settings = response.get("response")
                    applied = settings.get("applied") if isinstance(settings, dict) else None
                    if isinstance(applied, dict):
                        data, source = applied, "native_settings"
            elif kind == "system" and event.get("subtype") == "init":
                data, source = event, "native_session"
            elif (
                kind == "assistant"
                and not event.get("parent_tool_use_id")
                and not event.get("isApiErrorMessage")
            ):
                message = event.get("message")
                if isinstance(message, dict):
                    data, source = message, "native_response"
            if _model(data.get("model")):
                fields.update(model=data["model"], model_source=source)
            effort = _text(data.get("effort", data.get("reasoning_effort")))
            if effort:
                fields.update(reasoning_effort=effort, reasoning_source=source)
        elif kind in {"thread.started", "session.configured", "session_configured"}:
            if _model(event.get("model")):
                fields.update(model=event["model"], model_source="native_session")
            if effort := _text(event.get("reasoning_effort")):
                fields.update(reasoning_effort=effort, reasoning_source="native_session")
        return self.update(fields) if fields else None


def private_control_response(event: dict[str, Any]) -> bool:
    """Exclude metadata control payloads, which can include account details and secrets."""
    response = event.get("response")
    return (
        event.get("type") == "control_response"
        and isinstance(response, dict)
        and response.get("request_id") in {_INIT, _SETTINGS}
    )
