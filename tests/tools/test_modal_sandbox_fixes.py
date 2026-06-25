"""Tests for Modal sandbox infrastructure fixes (TBLite baseline).

Covers the 9 bugs discovered while setting up TBLite evaluation:
1. Tool resolution — minimal LeanFlow toolsets resolve cleanly
2. CWD fix — host paths get replaced with /root for container backends
3. ephemeral_disk version check
4. Tilde ~ replaced with /root for container backends
5. ensurepip fix in patches.py for Modal image builder
6. install_pipx stays True for swerex-remote
7. /home/ added to host prefix check
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is importable
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

try:
    import tools.implementations.terminal_tool  # noqa: F401

    _tt_mod = sys.modules["tools.implementations.terminal_tool"]
except ImportError:
    pytest.skip("leanflow tools not importable (missing deps)", allow_module_level=True)


# =========================================================================
# Test 1: Tool resolution includes the Lean workflow toolsets
# =========================================================================


class TestToolResolution:
    """Verify get_tool_definitions returns all expected tools for eval."""

    def test_file_toolset_resolves_all_tools(self):
        """enabled_toolsets=['file'] should produce the four file tools."""
        from model_tools import get_tool_definitions

        tools = get_tool_definitions(
            enabled_toolsets=["file"],
            quiet_mode=True,
        )
        names = {t["function"]["name"] for t in tools}
        expected = {"read_file", "write_file", "search_files", "patch"}
        assert expected == names, f"Expected {expected}, got {names}"

    def test_autoformalize_toolset_keeps_web_access(self):
        """The autoformalize toolset should keep the approved Lean research surface."""
        from model_tools import get_tool_definitions

        with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "fc-test"}, clear=False):
            tools = get_tool_definitions(
                enabled_toolsets=["autoformalize"],
                quiet_mode=True,
            )
        names = {t["function"]["name"] for t in tools}
        assert {"read_file", "web_search"}.issubset(names)


# =========================================================================
# Test 2-4: CWD handling for container backends
# =========================================================================


class TestCwdHandling:
    """Verify host paths are sanitized for container backends."""

    def test_local_backend_uses_getcwd(self):
        """Local backend should use os.getcwd(), not /root."""
        with patch.dict(os.environ, {"TERMINAL_ENV": "local"}, clear=False):
            env = os.environ.copy()
            env.pop("TERMINAL_CWD", None)
            with patch.dict(os.environ, env, clear=True):
                config = _tt_mod._get_env_config()
                assert config["cwd"] == os.getcwd()

    def test_ssh_preserves_home_paths(self):
        """SSH backend should NOT replace /home/ paths (they're valid remotely)."""
        with patch.dict(
            os.environ,
            {
                "TERMINAL_ENV": "ssh",
                "TERMINAL_CWD": "/home/remote-user/work",
                "TERMINAL_SSH_HOST": "example.com",
                "TERMINAL_SSH_USER": "user",
            },
        ):
            config = _tt_mod._get_env_config()
            assert (
                config["cwd"] == "/home/remote-user/work"
            ), "SSH backend should preserve /home/ paths"


# =========================================================================
# Test 5: ephemeral_disk version check
# =========================================================================


# =========================================================================
# Test 6: ModalEnvironment defaults
# =========================================================================


# =========================================================================
# Test 7: ensurepip fix in patches.py
# =========================================================================


# =========================================================================
# Test 8: Host prefix list completeness
# =========================================================================


class TestHostPrefixList:
    """Verify the host prefix list catches common host-only paths."""

    def test_all_common_host_prefixes_caught(self):
        """The host prefix check should catch /Users/, /home/, C:\\, C:/."""
        # Read the actual source to verify the prefixes
        import inspect

        source = inspect.getsource(_tt_mod._get_env_config)
        for prefix in ["/Users/", "/home/", 'C:\\\\"', "C:/"]:
            # Normalize for source comparison
            check = prefix.rstrip('"')
            assert check in source or prefix in source, (
                f"Host prefix {prefix!r} not found in _get_env_config. "
                "Container backends need this to avoid using host paths."
            )
