"""Shared fixtures for the LeanFlow test suite."""

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_leanflow_home(tmp_path, monkeypatch):
    """Redirect the LeanFlow home so tests never write to real user state."""
    fake_leanflow_home = tmp_path / "leanflow_test"
    fake_leanflow_home.mkdir()
    (fake_leanflow_home / "sessions").mkdir()
    (fake_leanflow_home / "logs").mkdir()
    (fake_leanflow_home / "memories").mkdir()
    (fake_leanflow_home / "workflow-state").mkdir()
    (fake_leanflow_home / "local-models").mkdir()
    monkeypatch.setenv("LEANFLOW_HOME", str(fake_leanflow_home))
    monkeypatch.setenv("LEANFLOW_QUEUE_INVARIANT_CHECKS", "1")
    # Tests should not inherit the agent's current gateway/messaging surface.
    # Individual tests that need gateway behavior set these explicitly.
    monkeypatch.delenv("LEANFLOW_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("LEANFLOW_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("LEANFLOW_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("LEANFLOW_GATEWAY_SESSION", raising=False)
    # Behavior flags a real ~/.leanflow/.env can inject at import time.
    # LEANFLOW_RCP_PREFIX_CACHE=1 ships in the default
    # .env since PR #12 and changes prompt layout; tests that exercise it set it
    # explicitly via monkeypatch.setenv.
    monkeypatch.delenv("LEANFLOW_RCP_PREFIX_CACHE", raising=False)
    # Native runner construction enables its contextual queue guard by setting
    # this process-global escape hatch directly.  Pytest workers reuse the
    # process, so register the key with monkeypatch for every test; teardown
    # then removes a value written by _build_agent instead of leaking it into
    # unrelated file-tool statement-guard tests.
    monkeypatch.delenv("LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS", raising=False)

    # Importing run_agent (and a few CLI entrypoints) runs load_leanflow_dotenv() at
    # module-import time, which is *before* this fixture runs on the first test that
    # imports them. With LEANFLOW_HOME still unset at that point it resolves to the
    # real ~/.leanflow and loads missing values from the developer's real .env into
    # os.environ. Those provider-resolution vars then leak into every later test in
    # the same process (notably tests/agent/test_auxiliary_client.py, whose own
    # _clean_env only strips the unprefixed OPENAI_* names). Strip the .env-injected
    # provider vars here so provider resolution starts from a clean slate regardless of
    # test order. monkeypatch.delenv restores the originals at teardown.
    for _prefix in ("LEANFLOW_", ""):
        for _suffix in (
            "OPENAI_BASE_URL",
            "OPENAI_API_KEY",
            "OPENROUTER_BASE_URL",
            "OPENROUTER_API_KEY",
        ):
            monkeypatch.delenv(_prefix + _suffix, raising=False)
    for _key in list(os.environ):
        # Provider/auxiliary routing vars that a real ~/.leanflow/.env can inject.
        if (
            _key.startswith("AUXILIARY_")
            or _key.startswith("CONTEXT_")
            or _key.startswith("LEANFLOW_CODEX_")
            or _key.startswith("LEANFLOW_EXPERT_")
            or _key == "LEANFLOW_INFERENCE_PROVIDER"
            or _key.endswith("_API_KEY")
            or _key.endswith("_BASE_URL")
        ):
            monkeypatch.delenv(_key, raising=False)


@pytest.fixture()
def tmp_dir(tmp_path):
    """Provide a temporary directory that is cleaned up automatically."""
    return tmp_path


@pytest.fixture()
def mock_config():
    """Return a minimal LeanFlow config dict suitable for unit tests."""
    return {
        "model": "test/mock-model",
        "toolsets": ["terminal", "file"],
        "max_turns": 10,
        "terminal": {
            "backend": "local",
            "cwd": "/tmp",
            "timeout": 30,
        },
        "compression": {"enabled": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "command_allowlist": [],
    }


# ── Global test timeout ─────────────────────────────────────────────────────
# Kill any individual test that takes longer than 30 seconds.
# Prevents hanging tests (subprocess spawns, blocking I/O) from stalling the
# entire test suite.


def _timeout_handler(signum, frame):
    raise TimeoutError("Test exceeded 30 second timeout")


@pytest.fixture(autouse=True)
def _ensure_current_event_loop(request):
    """Provide a default event loop for sync tests that call get_event_loop().

    Python 3.11+ no longer guarantees a current loop for plain synchronous tests.
    A number of gateway tests still use asyncio.get_event_loop().run_until_complete(...).
    Ensure they always have a usable loop without interfering with pytest-asyncio's
    own loop management for @pytest.mark.asyncio tests.
    """
    if request.node.get_closest_marker("asyncio") is not None:
        yield
        return

    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        loop = None

    created = loop is None or loop.is_closed()
    if created:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        yield
    finally:
        if created and loop is not None:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _enforce_test_timeout():
    """Kill any individual test that takes longer than 30 seconds."""
    old = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(30)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)
