"""Exercise native model discovery without authentication or inference requests."""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import pytest

from leanflow_cli.runtime import provider_models as models
from leanflow_cli.runtime.provider_protocol import MetadataSession, MetadataUnavailable


class FakeSession:
    """Record exact native wire requests and return fixture responses."""

    def __init__(self, responses):
        self.responses = deque(responses)
        self.requests = []
        self.command = []
        self.environment = None
        self.cwd = None
        self.closed = False

    def __call__(self, command, **kwargs):
        self.command = command
        self.environment = kwargs.get("env")
        self.cwd = kwargs.get("cwd")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def send(self, request):
        self.requests.append(request)

    def receive(self):
        return self.responses.popleft()


def rpc(request_id, data):
    return {"id": request_id, "result": data}


def control(request_id, data):
    return {
        "type": "control_response",
        "response": {"request_id": request_id, "subtype": "success", "response": data},
    }


def native_row(model="model-one", **kwargs):
    return {
        "id": model,
        "model": model,
        "displayName": model.title(),
        "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "max"}],
        "defaultReasoningEffort": "low",
        "isDefault": True,
        **kwargs,
    }


@pytest.mark.parametrize("backend", ["codex", "lemex"])
def test_app_server_reads_native_defaults_and_all_pages(monkeypatch, backend):
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    session = FakeSession(
        [
            {"method": "unrelated/notification"},
            rpc(1, {}),
            rpc(2, {"config": {"model": "configured-model", "model_reasoning_effort": "max"}}),
            rpc(3, {"data": [native_row()], "nextCursor": "next"}),
            rpc(
                3,
                {
                    "data": [
                        native_row(),
                        native_row("configured-model"),
                        native_row("hidden", hidden=True),
                    ]
                },
            ),
        ]
    )
    monkeypatch.setattr(models, "MetadataSession", session)
    result = models.model_catalog(backend, backend, check=True)
    assert result["status"] == "ready"
    assert result["default_model"] == "configured-model"
    assert result["default_reasoning_effort"] == "max"
    assert result["configured_reasoning_effort"] == "max"
    assert [m["id"] for m in result["models"]] == ["model-one", "configured-model"]
    assert result["models"][0]["reasoning_efforts"] == ["low", "max"]
    assert result["models"][0]["reasoning_known"] is True
    assert [r["method"] for r in session.requests] == [
        "initialize",
        "initialized",
        "config/read",
        "model/list",
        "model/list",
    ]
    assert session.requests[-1]["params"]["cursor"] == "next"
    assert session.command == [backend, "app-server"]
    if backend == "codex":
        assert session.environment["CODEX_API_KEY"] == "private-key"
    assert "private-key" not in json.dumps(result)
    assert session.closed


def test_native_default_and_unknown_reasoning_are_not_invented(monkeypatch):
    session = FakeSession(
        [
            rpc(1, {}),
            rpc(2, {"config": {}}),
            rpc(
                3,
                {
                    "data": [
                        native_row(),
                        {"model": "unknown"},
                        native_row("no-effort", supportedReasoningEfforts=[]),
                    ]
                },
            ),
        ]
    )
    monkeypatch.setattr(models, "MetadataSession", session)
    result = models.model_catalog("codex", "codex", check=True)
    assert result["default_model"] == "model-one"
    assert result["default_reasoning_effort"] == "low"
    assert result["configured_reasoning_effort"] == ""
    assert result["models"][1]["reasoning_known"] is False
    assert result["models"][2]["reasoning_known"] is True
    assert result["models"][2]["reasoning_efforts"] == []


def test_claude_uses_control_requests_and_effective_settings(monkeypatch):
    session = FakeSession(
        [
            control(
                "models",
                {
                    "account": {"email": "private@example.com"},
                    "models": [
                        {"value": "default", "resolvedModel": "canonical"},
                        {
                            "value": "opus",
                            "resolvedModel": "canonical",
                            "displayName": "Opus",
                            "supportedEffortLevels": ["high", "max"],
                        },
                        {"value": "haiku", "supportsEffort": False},
                        {"value": "old-metadata"},
                    ],
                },
            ),
            control(
                "settings",
                {
                    "applied": {"model": "canonical", "effort": "max"},
                    "effective": {"env": {"API_KEY": "secret"}},
                },
            ),
        ]
    )
    monkeypatch.setattr(models, "MetadataSession", session)
    result = models.model_catalog("claude", "claude", check=True)
    assert result["default_model"] == "canonical"
    assert result["default_reasoning_effort"] == "max"
    assert [m["id"] for m in result["models"]] == ["opus", "haiku", "old-metadata"]
    assert result["models"][0]["resolved_model"] == "canonical"
    assert result["models"][0]["reasoning_efforts"] == ["high", "max"]
    assert result["models"][1]["reasoning_known"] is True
    assert result["models"][2]["reasoning_known"] is False
    assert [r["request"]["subtype"] for r in session.requests] == ["initialize", "get_settings"]
    assert all(r["type"] == "control_request" for r in session.requests)
    assert "--no-session-persistence" in session.command
    assert '{"disableAllHooks":true}' in session.command
    assert '{"mcpServers":{}}' in session.command
    assert "private@example.com" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_older_claude_reports_unknown_default_instead_of_recommended_alias(monkeypatch):
    session = FakeSession(
        [
            control(
                "models",
                {
                    "models": [
                        {"value": "default", "resolvedModel": "recommended"},
                        {"value": "sonnet"},
                    ]
                },
            ),
            {
                "type": "control_response",
                "response": {
                    "request_id": "settings",
                    "subtype": "error",
                    "error": "private diagnostic",
                },
            },
        ]
    )
    monkeypatch.setattr(models, "MetadataSession", session)
    result = models.model_catalog("claude", "claude", check=True)
    assert result["status"] == "ready"
    assert result["default_model"] == ""
    assert result["error"]
    assert "private diagnostic" not in json.dumps(result)


def test_discovery_ignores_project_runtime_config_like_isolated_jobs(monkeypatch, tmp_path):
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex" / "config.toml").write_text('model = "project-only-model"\n')
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "native-home"))
    session = FakeSession([rpc(1, {}), rpc(2, {"config": {}}), rpc(3, {"data": [native_row()]})])
    monkeypatch.setattr(models, "MetadataSession", session)
    result = models.model_catalog("codex", "codex", check=True)
    assert result["status"] == "ready"
    assert session.cwd != str(project)
    assert session.requests[2]["params"]["cwd"] == session.cwd
    assert session.environment["CODEX_HOME"] == str(tmp_path / "native-home")
    assert not Path(session.cwd).exists()
    assert Path.cwd() == project


def test_native_errors_do_not_expose_config_or_credentials(monkeypatch):
    session = FakeSession([rpc(1, {}), {"id": 2, "error": {"message": "key=private-key"}}])
    monkeypatch.setattr(models, "MetadataSession", session)
    result = models.model_catalog("lemex", "lemex", check=True)
    assert result["status"] == "unavailable"
    assert "config/read" in result["error"]
    assert "private-key" not in json.dumps(result)
    assert session.closed


def test_metadata_fields_redact_exact_environment_credentials(monkeypatch):
    monkeypatch.setenv("LEMEX_API_KEY", "opaque-example-value")
    session = FakeSession(
        [
            rpc(1, {}),
            rpc(2, {"config": {}}),
            rpc(3, {"data": [native_row(displayName="opaque-example-value")]}),
        ]
    )
    monkeypatch.setattr(models, "MetadataSession", session)
    assert "opaque-example-value" not in json.dumps(
        models.model_catalog("lemex", "lemex", check=True)
    )


def test_unchecked_and_missing_runtime_never_spawn(monkeypatch):
    monkeypatch.setattr(
        models, "MetadataSession", lambda *a, **kw: pytest.fail("unexpected process")
    )
    assert models.model_catalog("codex", "codex", check=False)["status"] == "not_checked"
    assert models.model_catalog("codex", "", check=True)["status"] == "unavailable"


def test_repeated_native_catalog_cursor_fails_cleanly(monkeypatch):
    page = rpc(3, {"data": [native_row()], "nextCursor": "same"})
    session = FakeSession([rpc(1, {}), rpc(2, {"config": {}}), page, page])
    monkeypatch.setattr(models, "MetadataSession", session)
    result = models.model_catalog("codex", "codex", check=True)
    assert result["status"] == "unavailable"
    assert "repeated page" in result["error"]


def test_metadata_transport_echoes_json_and_reaps_process():
    command = [
        sys.executable,
        "-u",
        "-c",
        "import sys; print(sys.stdin.readline(), end='', flush=True); sys.stdin.read()",
    ]
    with MetadataSession(command) as session:
        session.send({"metadata": "only"})
        assert session.receive() == {"metadata": "only"}
    assert session.process.poll() is not None


def test_metadata_timeout_reaps_process():
    with pytest.raises(MetadataUnavailable, match="timed out"):
        with MetadataSession(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05
        ) as session:
            session.receive()
    assert session.process.poll() is not None


def test_exited_metadata_process_discards_large_sensitive_stderr(monkeypatch, capsys):
    sessions = []

    def exited(*args, **kwargs):
        session = MetadataSession(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('private-key' * 200000); sys.exit(1)",
            ],
            timeout=2,
        )
        sessions.append(session)
        return session

    monkeypatch.setattr(models, "MetadataSession", exited)
    result = models.model_catalog("codex", "codex", check=True)
    assert result["status"] == "unavailable"
    assert "closed" in result["error"]
    assert "private-key" not in json.dumps(result)
    assert "private-key" not in str(capsys.readouterr())
    assert sessions[0].process.poll() == 1


def test_oversized_metadata_record_closes_without_unbounded_read():
    command = [
        sys.executable,
        "-u",
        "-c",
        "import sys; sys.stdout.write('x' * (5 * 1024 * 1024)); sys.stdout.flush(); sys.stdin.read()",
    ]
    with pytest.raises(MetadataUnavailable, match="closed"):
        with MetadataSession(command, timeout=2) as session:
            session.receive()
    assert session.process.poll() is not None
