from __future__ import annotations

from epflemma_cli.doctor import run_doctor


def test_run_doctor_json_is_structured_in_degraded_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "epflemma_cli.doctor.resolve_runtime_provider",
        lambda: {
            "provider": "custom",
            "base_url": "https://example.test/v1",
            "api_mode": "chat",
            "model": "demo-model",
        },
    )
    monkeypatch.setattr("epflemma_cli.doctor.get_mcp_status", lambda: [])

    issues, payload = run_doctor(tmp_path, json_output=True)

    assert isinstance(payload, dict)
    assert payload["mode"] == "all"
    assert "capability_report" in payload
    assert payload["capability_report"]["project_valid"] is False
    assert "issues" in payload
    assert issues


def test_run_doctor_supports_mcp_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "epflemma_cli.doctor.resolve_runtime_provider",
        lambda: {
            "provider": "custom",
            "base_url": "https://example.test/v1",
            "api_mode": "chat",
            "model": "demo-model",
        },
    )
    monkeypatch.setattr(
        "epflemma_cli.doctor.get_mcp_status",
        lambda: [{"name": "lean-lsp", "transport": "stdio", "tools": 3, "connected": True}],
    )

    _issues, payload = run_doctor(tmp_path, mode="mcp", json_output=True)

    assert payload["mode"] == "mcp"
    assert payload["mcp_status"][0]["name"] == "lean-lsp"
