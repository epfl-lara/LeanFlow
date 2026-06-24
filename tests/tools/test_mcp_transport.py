"""Tests for tools/mcp_transport.py (stdio/HTTP transport plumbing).

Covers re-export identity (every moved name is the SAME object on
``tools.mcp_tool``) plus a few behavior checks on the trickiest helpers.
"""

import os

import tools.mcp_tool as mcp_tool
import tools.mcp_transport as mcp_transport

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

        sys.modules.pop("tools.mcp_tool", None)
        sys.modules.pop("tools.mcp_transport", None)
        importlib.import_module("tools.mcp_transport")
        assert "tools.mcp_tool" not in sys.modules


class TestBuildSafeEnv:
    def test_filters_unsafe_keys(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("SECRET_TOKEN", "ghp_should_not_leak")
        monkeypatch.setenv("XDG_DATA_HOME", "/x")
        result = mcp_transport._build_safe_env(None)
        assert result.get("PATH") == "/usr/bin"
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


class TestFormatConnectError:
    def test_unwraps_missing_executable(self):
        err = FileNotFoundError(2, "No such file or directory", "npx")
        msg = mcp_transport._format_connect_error(err)
        assert "missing executable 'npx'" in msg
        assert "Node.js" in msg
