"""Shared fixtures for the EPFLemma test suite."""

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_gauss_home(tmp_path, monkeypatch):
    """Redirect legacy and EPFLemma homes so tests never write to real user state."""
    fake_home = tmp_path / "gauss_test"
    fake_epflemma_home = tmp_path / "epflemma_test"
    fake_home.mkdir()
    fake_epflemma_home.mkdir()
    (fake_home / "sessions").mkdir()
    (fake_home / "cron").mkdir()
    (fake_home / "memories").mkdir()
    (fake_home / "skills").mkdir()
    (fake_epflemma_home / "sessions").mkdir()
    (fake_epflemma_home / "logs").mkdir()
    (fake_epflemma_home / "memories").mkdir()
    (fake_epflemma_home / "workflow-state").mkdir()
    (fake_epflemma_home / "local-models").mkdir()
    monkeypatch.setenv("EPFLEMMA_HOME", str(fake_epflemma_home))
    # The one-time legacy seed reads the retired ~/.gauss / ~/.opengauss homes directly via
    # core.home.legacy_homes(); point it at the throwaway fake home so tests never read or copy
    # the developer's real retired state. Tests exercising migration populate fake_home themselves.
    monkeypatch.setattr("core.home.legacy_homes", lambda: (fake_home,))
    monkeypatch.setenv("EPFLEMMA_QUEUE_INVARIANT_CHECKS", "1")
    # Tests should not inherit the agent's current gateway/messaging surface.
    # Individual tests that need gateway behavior set these explicitly.
    monkeypatch.delenv("EPFLEMMA_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("EPFLEMMA_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("EPFLEMMA_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("EPFLEMMA_GATEWAY_SESSION", raising=False)

    # Importing run_agent (and a few CLI entrypoints) runs load_epflemma_dotenv() at
    # module-import time, which is *before* this fixture runs on the first test that
    # imports them. With EPFLEMMA_HOME still unset at that point it resolves to the
    # real ~/.epflemma and loads the developer's real .env into os.environ with
    # override=True. Those provider-resolution vars then leak into every later test in
    # the same process (notably tests/agent/test_auxiliary_client.py, whose own
    # _clean_env only strips the unprefixed OPENAI_* names). Strip the .env-injected
    # provider vars here so provider resolution starts from a clean slate regardless of
    # test order. monkeypatch.delenv restores the originals at teardown.
    for _prefix in ("EPFLEMMA_", "OPENGAUSS_", "GAUSS_", ""):
        for _suffix in (
            "OPENAI_BASE_URL", "OPENAI_API_KEY",
            "OPENROUTER_BASE_URL", "OPENROUTER_API_KEY",
        ):
            monkeypatch.delenv(_prefix + _suffix, raising=False)
    for _key in list(os.environ):
        # Provider/auxiliary routing vars that a real ~/.epflemma/.env can inject.
        if (
            _key.startswith("AUXILIARY_")
            or _key.startswith("CONTEXT_")
            or _key.startswith("EPFLEMMA_CODEX_")
            or _key.startswith("EPFLEMMA_EXPERT_")
            or _key == "EPFLEMMA_INFERENCE_PROVIDER"
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
    """Return a minimal gauss config dict suitable for unit tests."""
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
