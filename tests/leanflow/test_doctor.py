from __future__ import annotations

import pytest

from leanflow_cli.cli.doctor import DOCTOR_MODES, _web_search_payload, run_doctor


def test_run_doctor_json_is_structured_in_degraded_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.resolve_runtime_provider",
        lambda: {
            "provider": "custom",
            "base_url": "https://example.test/v1",
            "api_mode": "chat",
            "model": "demo-model",
        },
    )
    monkeypatch.setattr("leanflow_cli.cli.doctor.get_mcp_status", lambda: [])

    issues, payload = run_doctor(tmp_path, json_output=True)

    assert isinstance(payload, dict)
    assert payload["mode"] == "all"
    assert "capability_report" in payload
    assert payload["capability_report"]["project_valid"] is False
    assert "issues" in payload
    assert issues


def test_run_doctor_supports_mcp_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.resolve_runtime_provider",
        lambda: {
            "provider": "custom",
            "base_url": "https://example.test/v1",
            "api_mode": "chat",
            "model": "demo-model",
        },
    )
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.get_mcp_status",
        lambda: [{"name": "lean-lsp", "transport": "stdio", "tools": 3, "connected": True}],
    )

    _issues, payload = run_doctor(tmp_path, mode="mcp", json_output=True)

    assert payload["mode"] == "mcp"
    assert payload["mcp_status"][0]["name"] == "lean-lsp"


def test_run_doctor_mcp_mode_surfaces_bootstrap_recommendation(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.resolve_runtime_provider",
        lambda: {
            "provider": "custom",
            "base_url": "https://example.test/v1",
            "api_mode": "chat",
            "model": "demo-model",
        },
    )
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.get_mcp_status",
        lambda: [
            {
                "name": "lean-proof-auto",
                "transport": "stdio",
                "tools": 0,
                "connected": False,
                "role": "secondary-automation-context",
                "managed": True,
                "installed": False,
                "configured": False,
                "bootstrap_recommended": True,
            }
        ],
    )

    issues, payload = run_doctor(tmp_path, mode="mcp", json_output=True)

    assert payload["mcp_status"][0]["bootstrap_recommended"] is True
    assert any("bootstrap recommended" in issue.lower() for issue in issues)


def test_run_doctor_supported_modes_cover_readme_surface():
    # README advertises these modes; keeping the set in sync prevents silent drift.
    assert {"all", "env", "mcp", "search", "web-search", "migrate", "cleanup"} == DOCTOR_MODES


def test_run_doctor_unknown_mode_normalizes_to_all(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.resolve_runtime_provider",
        lambda: {
            "provider": "custom",
            "base_url": "https://x/v1",
            "api_mode": "chat",
            "model": "m",
        },
    )
    monkeypatch.setattr("leanflow_cli.cli.doctor.get_mcp_status", lambda: [])

    _issues, payload = run_doctor(tmp_path, mode="totally-bogus", json_output=True)

    assert payload["mode"] == "all"
    # mode=all implies every optional section is populated
    for section in ("mcp_status", "search", "migrate", "cleanup"):
        assert section in payload


def test_run_doctor_text_output_renders_structured_report(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.resolve_runtime_provider",
        lambda: {
            "provider": "custom",
            "base_url": "https://rcp.example/v1",
            "api_mode": "chat_completions",
            "model": "demo-model",
        },
    )
    monkeypatch.setattr("leanflow_cli.cli.doctor.get_mcp_status", lambda: [])

    _issues, text = run_doctor(tmp_path, json_output=False)

    assert isinstance(text, str)
    assert "Doctor" in text
    assert "Runtime provider:" in text
    assert "custom" in text
    assert "Issues:" in text


def test_run_doctor_reports_provider_error_without_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    def _blow_up():
        raise RuntimeError("no credentials configured")

    monkeypatch.setattr("leanflow_cli.cli.doctor.resolve_runtime_provider", _blow_up)
    monkeypatch.setattr("leanflow_cli.cli.doctor.get_mcp_status", lambda: [])

    issues, payload = run_doctor(tmp_path, json_output=True)

    assert payload["provider"]["available"] is False
    assert "no credentials configured" in payload["provider"]["error"]
    assert any("no credentials" in issue for issue in issues)


def test_run_doctor_cleanup_mode_lists_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.resolve_runtime_provider",
        lambda: {
            "provider": "custom",
            "base_url": "https://x/v1",
            "api_mode": "chat",
            "model": "m",
        },
    )
    monkeypatch.setattr("leanflow_cli.cli.doctor.get_mcp_status", lambda: [])

    _issues, payload = run_doctor(tmp_path, mode="cleanup", json_output=True)

    assert payload["mode"] == "cleanup"
    assert "cleanup" in payload
    assert payload["cleanup"]["apply_supported"] is False
    assert any(path.endswith(".claude") for path in payload["cleanup"]["candidate_paths"])


def test_run_doctor_web_search_mode_uses_real_tool_surface(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor._web_search_payload",
        lambda: (
            {
                "available": True,
                "tool_exposed": True,
                "lean_search_guidance": True,
                "query": "prime number theorem formalization Lean",
                "success": True,
                "result_count": 2,
                "providers": ["arxiv", "sourcegraph"],
                "kinds": ["code", "paper"],
                "degraded_reasons": [],
                "first_results": [
                    {
                        "provider": "arxiv",
                        "kind": "paper",
                        "title": "A Formal Proof",
                        "url": "https://arxiv.org/abs/2501.00001",
                    }
                ],
                "firecrawl_error": False,
            },
            [],
        ),
    )

    issues, payload = run_doctor(tmp_path, mode="web-search", json_output=True)
    _text_issues, text = run_doctor(tmp_path, mode="web-search", json_output=False)

    assert issues == []
    assert payload["mode"] == "web-search"
    assert payload["web_search"]["available"] is True
    assert "Lean-first guidance: yes" in text
    assert "sourcegraph" in text


def test_web_search_doctor_accepts_case_insensitive_lean_first_guidance(monkeypatch):
    from tools.implementations import web_tools

    monkeypatch.setattr(
        web_tools,
        "web_search_tool",
        lambda query, limit=5: (
            '{"success":true,"data":{"web":[{"provider":"arxiv","kind":"paper",'
            '"title":"A Formal Proof","url":"https://arxiv.org/abs/2501.00001"}]},'
            '"degraded_reasons":[]}'
        ),
    )

    payload, issues = _web_search_payload()

    assert payload["lean_search_guidance"] is True
    assert payload["available"] is True
    assert issues == []


@pytest.mark.parametrize(
    "mode", sorted({"all", "env", "mcp", "search", "web-search", "migrate", "cleanup"})
)
def test_run_doctor_never_throws_for_each_supported_mode(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.resolve_runtime_provider",
        lambda: {
            "provider": "custom",
            "base_url": "https://x/v1",
            "api_mode": "chat",
            "model": "m",
        },
    )
    monkeypatch.setattr("leanflow_cli.cli.doctor.get_mcp_status", lambda: [])
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor._web_search_payload",
        lambda: ({"available": True, "issues": []}, []),
    )

    issues, payload = run_doctor(tmp_path, mode=mode, json_output=True)

    assert payload["mode"] == mode
    assert isinstance(issues, list)
