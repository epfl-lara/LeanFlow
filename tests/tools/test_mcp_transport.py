"""Tests for tools/mcp_transport.py (stdio/HTTP transport plumbing).

Covers re-export identity (every moved name is the SAME object on
``tools.mcp.mcp_tool``) plus a few behavior checks on the trickiest helpers.
"""

import os

import tools.mcp.mcp_tool as mcp_tool
import tools.mcp.mcp_transport as mcp_transport

_REEXPORTED = [
    "_build_safe_env",
    "_sanitize_error",
    "_prepend_path",
    "_resolve_stdio_command",
    "_resolve_stdio_cwd",
    "_truthy_env_value",
    "_read_lean_toolchain_from_root",
    "_disable_incompatible_local_loogle",
    "_augment_lean_stdio_env",
    "_repair_loogle_cache_if_needed",
    "_effective_connect_timeout",
    "_format_connect_error",
    "_CREDENTIAL_PATTERN",
    "_SAFE_ENV_KEYS",
    "_DEFAULT_CONNECT_TIMEOUT",
    "_LOCAL_LOOGLE_CONNECT_TIMEOUT",
    "_LOOGLE_STALE_ARTIFACT_SCAN_LIMIT",
    "_LEAN_MODULE_PART_PATTERN",
]


class TestReExportIdentity:
    """Every moved name resolves to the same object on both modules."""

    def test_all_names_identical(self):
        for name in _REEXPORTED:
            assert getattr(mcp_tool, name) is getattr(mcp_transport, name), name

    def test_transport_does_not_import_mcp_tool(self):
        # The new module is a leaf: importing it must not pull in its origin,
        # otherwise the re-export shim would form a cycle.
        import importlib
        import sys

        sys.modules.pop("tools.mcp.mcp_tool", None)
        sys.modules.pop("tools.mcp.mcp_transport", None)
        importlib.import_module("tools.mcp.mcp_transport")
        assert "tools.mcp.mcp_tool" not in sys.modules


class TestBuildSafeEnv:
    def test_filters_unsafe_keys(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("ELAN_HOME", "/leanflow-cache/elan")
        monkeypatch.setenv("SECRET_TOKEN", "ghp_should_not_leak")
        monkeypatch.setenv("XDG_DATA_HOME", "/x")
        result = mcp_transport._build_safe_env(None)
        assert result.get("PATH") == "/usr/bin"
        assert result.get("ELAN_HOME") == "/leanflow-cache/elan"
        assert result.get("XDG_DATA_HOME") == "/x"
        assert "SECRET_TOKEN" not in result

    def test_user_env_overrides(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        result = mcp_transport._build_safe_env({"MY_VAR": "v", "PATH": "/custom"})
        assert result["MY_VAR"] == "v"
        assert result["PATH"] == "/custom"


class TestSanitizeError:
    def test_redacts_github_pat_and_bearer(self):
        out = mcp_transport._sanitize_error("ghp_abc123 Bearer eyJfoo")
        assert "ghp_abc123" not in out
        assert "Bearer eyJfoo" not in out
        assert "[REDACTED]" in out

    def test_passes_through_clean_text(self):
        assert mcp_transport._sanitize_error("plain error") == "plain error"


class TestPrependPath:
    def test_prepends_when_missing(self):
        out = mcp_transport._prepend_path({"PATH": "/usr/bin"}, "/opt/bin")
        assert out["PATH"].split(os.pathsep)[0] == "/opt/bin"
        assert "/usr/bin" in out["PATH"]

    def test_noop_when_already_present(self):
        out = mcp_transport._prepend_path({"PATH": "/opt/bin:/usr/bin"}, "/opt/bin")
        assert out["PATH"].count("/opt/bin") == 1


class TestTruthyEnvValue:
    def test_truthy_and_falsy(self):
        for v in ("1", "true", "YES", "On"):
            assert mcp_transport._truthy_env_value(v) is True
        for v in ("0", "false", "", None, "nope"):
            assert mcp_transport._truthy_env_value(v) is False


class TestEffectiveConnectTimeout:
    def test_uses_config_value(self):
        assert mcp_transport._effective_connect_timeout("other", {"connect_timeout": 10}) == 10

    def test_default_on_bad_value(self):
        assert mcp_transport._effective_connect_timeout("other", {"connect_timeout": "x"}) == float(
            mcp_transport._DEFAULT_CONNECT_TIMEOUT
        )

    def test_lean_lsp_local_loogle_raises_floor(self):
        out = mcp_transport._effective_connect_timeout(
            "lean-lsp", {"connect_timeout": 10, "env": {"LEAN_LOOGLE_LOCAL": "true"}}
        )
        assert out == float(mcp_transport._LOCAL_LOOGLE_CONNECT_TIMEOUT)

    def test_dispatch_worker_without_local_loogle_opt_in_keeps_normal_timeout(self, monkeypatch):
        monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
        monkeypatch.delenv("LEANFLOW_DISPATCH_LOCAL_LOOGLE", raising=False)

        out = mcp_transport._effective_connect_timeout(
            "lean-lsp", {"connect_timeout": 10, "env": {"LEAN_LOOGLE_LOCAL": "true"}}
        )

        assert out == 10

    def test_research_foreground_without_local_loogle_opt_in_keeps_normal_timeout(
        self, monkeypatch
    ):
        monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
        monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
        monkeypatch.delenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", raising=False)

        out = mcp_transport._effective_connect_timeout(
            "lean-lsp", {"connect_timeout": 10, "env": {"LEAN_LOOGLE_LOCAL": "true"}}
        )

        assert out == 10


class TestDispatchLocalLooglePolicy:
    def test_worker_disables_private_index_but_keeps_server_environment(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
        monkeypatch.delenv("LEANFLOW_DISPATCH_LOCAL_LOOGLE", raising=False)
        configured = {
            "LEAN_LOOGLE_LOCAL": "true",
            "LEAN_LOOGLE_CACHE_DIR": str(tmp_path / "loogle"),
            "LEAN_REPL": "true",
        }

        out = mcp_transport._augment_lean_stdio_env("lean-lsp", configured, str(tmp_path))

        assert out["LEAN_LOOGLE_LOCAL"] == "false"
        assert out["LEAN_REPL"] == "true"
        assert out["LEAN_PROJECT_PATH"] == str(tmp_path)
        assert configured["LEAN_LOOGLE_LOCAL"] == "true"

    def test_worker_can_explicitly_restore_private_index(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
        monkeypatch.setenv("LEANFLOW_DISPATCH_LOCAL_LOOGLE", "1")

        out = mcp_transport._augment_lean_stdio_env(
            "lean-lsp", {"LEAN_LOOGLE_LOCAL": "true"}, str(tmp_path)
        )

        assert out["LEAN_LOOGLE_LOCAL"] == "true"

    def test_foreground_local_loogle_behavior_is_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
        monkeypatch.delenv("LEANFLOW_DISPATCH_LOCAL_LOOGLE", raising=False)

        out = mcp_transport._augment_lean_stdio_env(
            "lean-lsp", {"LEAN_LOOGLE_LOCAL": "true"}, str(tmp_path)
        )

        assert out["LEAN_LOOGLE_LOCAL"] == "true"

    def test_policy_does_not_change_non_lean_lsp_servers(self, monkeypatch):
        monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
        configured = {"LEAN_LOOGLE_LOCAL": "true"}

        out = mcp_transport._apply_dispatch_local_loogle_policy("lean-proof-auto", configured)

        assert out == configured


class TestResearchLocalLooglePolicy:
    def test_research_foreground_disables_only_private_index(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
        monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
        monkeypatch.delenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", raising=False)
        configured = {
            "LEAN_LOOGLE_LOCAL": "true",
            "LEAN_LOOGLE_CACHE_DIR": str(tmp_path / "loogle"),
            "LEAN_REPL": "true",
        }

        out = mcp_transport._augment_lean_stdio_env("lean-lsp", configured, str(tmp_path))

        assert out["LEAN_LOOGLE_LOCAL"] == "false"
        assert out["LEAN_REPL"] == "true"
        assert out["LEAN_PROJECT_PATH"] == str(tmp_path)
        assert configured["LEAN_LOOGLE_LOCAL"] == "true"

    def test_research_foreground_can_explicitly_restore_private_index(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
        monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
        monkeypatch.setenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", "1")

        out = mcp_transport._augment_lean_stdio_env(
            "lean-lsp", {"LEAN_LOOGLE_LOCAL": "true"}, str(tmp_path)
        )

        assert out["LEAN_LOOGLE_LOCAL"] == "true"

    def test_non_research_foreground_keeps_private_index(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
        monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)
        monkeypatch.delenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", raising=False)

        out = mcp_transport._augment_lean_stdio_env(
            "lean-lsp", {"LEAN_LOOGLE_LOCAL": "true"}, str(tmp_path)
        )

        assert out["LEAN_LOOGLE_LOCAL"] == "true"


class TestFormatConnectError:
    def test_unwraps_missing_executable(self):
        err = FileNotFoundError(2, "No such file or directory", "npx")
        msg = mcp_transport._format_connect_error(err)
        assert "missing executable 'npx'" in msg
        assert "Node.js" in msg
