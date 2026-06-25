"""Focused tests for the AnthropicMessagePreparer collaborator extracted from AIAgent.

Covers the resolve-accessor materialization (real agents, ``__new__`` agents,
MagicMock fakes), the AIAgent delegation identity (wrappers forward to the
collaborator), the ``_anthropic_image_fallback_cache`` property shim (read/write
share the collaborator's owned memo), and behavior of the moved logic:
no-image passthrough, the static image-part / data-url helpers, the cached
vision-fallback description, and the full multimodal→text flattening.
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from agent.providers.anthropic_messages import (
    AnthropicMessagePreparer,
    content_has_image_parts,
    materialize_data_url_for_vision,
)
from run_agent import AIAgent, _resolve_anthropic_message_preparer


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


# ── resolve accessor + delegation identity ──────────────────────────────────


def test_init_builds_real_preparer(agent):
    assert isinstance(agent._anthropic_message_preparer_obj, AnthropicMessagePreparer)
    assert agent._anthropic_message_preparer_obj._agent is agent


def test_resolve_returns_cached_instance(agent):
    preparer = _resolve_anthropic_message_preparer(agent)
    assert preparer is agent._anthropic_message_preparer_obj
    assert _resolve_anthropic_message_preparer(agent) is preparer


def test_resolve_materializes_for_new_agent():
    """Agents built via __new__ (bypassing __init__) get a fresh real preparer."""
    a = AIAgent.__new__(AIAgent)
    preparer = _resolve_anthropic_message_preparer(a)
    assert isinstance(preparer, AnthropicMessagePreparer)
    assert preparer is _resolve_anthropic_message_preparer(a)
    assert preparer.image_fallback_cache == {}


def test_resolve_materializes_for_mock_agent():
    """A MagicMock fake agent gets a real AnthropicMessagePreparer (isinstance guard)."""
    fake = MagicMock()
    preparer = _resolve_anthropic_message_preparer(fake)
    assert isinstance(preparer, AnthropicMessagePreparer)


def test_wrappers_delegate_to_collaborator(agent):
    preparer = agent._anthropic_message_preparer_obj
    preparer.prepare_anthropic_messages_for_api = MagicMock(return_value=["sentinel"])
    preparer.preprocess_anthropic_content = MagicMock(return_value="flat")
    preparer.describe_image_for_anthropic_fallback = MagicMock(return_value="note")

    assert agent._prepare_anthropic_messages_for_api([{"role": "user"}]) == ["sentinel"]
    assert agent._preprocess_anthropic_content(["x"], "user") == "flat"
    assert agent._describe_image_for_anthropic_fallback("u", "user") == "note"


# ── cache property shim ──────────────────────────────────────────────────────


def test_cache_property_reads_collaborator_memo(agent):
    agent._anthropic_message_preparer_obj.image_fallback_cache["k"] = "v"
    assert agent._anthropic_image_fallback_cache == {"k": "v"}
    assert (
        agent._anthropic_image_fallback_cache
        is agent._anthropic_message_preparer_obj.image_fallback_cache
    )


def test_cache_property_indexed_write_reaches_collaborator(agent):
    agent._anthropic_image_fallback_cache["hit"] = "cached-note"
    assert agent._anthropic_message_preparer_obj.image_fallback_cache["hit"] == "cached-note"


def test_cache_property_setter_replaces_memo(agent):
    agent._anthropic_image_fallback_cache = {"replaced": "yes"}
    assert agent._anthropic_message_preparer_obj.image_fallback_cache == {"replaced": "yes"}


# ── static helpers ───────────────────────────────────────────────────────────


def test_content_has_image_parts():
    assert content_has_image_parts([{"type": "image_url", "image_url": {"url": "x"}}]) is True
    assert content_has_image_parts([{"type": "input_image"}]) is True
    assert content_has_image_parts([{"type": "text", "text": "hi"}]) is False
    assert content_has_image_parts("plain string") is False
    # AIAgent staticmethod wrapper forwards identically.
    assert AIAgent._content_has_image_parts([{"type": "image_url"}]) is True


def test_materialize_data_url_for_vision_writes_temp_png():
    payload = base64.b64encode(b"\x89PNG-bytes").decode("ascii")
    data_url = f"data:image/png;base64,{payload}"
    src, path = materialize_data_url_for_vision(data_url)
    try:
        assert path is not None
        assert path.exists()
        assert str(path).endswith(".png")
        assert src == str(path)
        assert path.read_bytes() == b"\x89PNG-bytes"
    finally:
        if path is not None and path.exists():
            path.unlink()


# ── behavior: prepare / describe ─────────────────────────────────────────────


def test_prepare_passthrough_when_no_images(agent):
    messages = [{"role": "user", "content": "hello"}]
    # Identity passthrough (no deep-copy) when no image parts present.
    assert agent._prepare_anthropic_messages_for_api(messages) is messages


def test_describe_caches_per_image(agent):
    preparer = agent._anthropic_message_preparer_obj
    fake_result = json.dumps({"analysis": "a red square"})
    calls = {"n": 0}

    async def fake_vision(**_kwargs):
        calls["n"] += 1
        return fake_result

    # The real code lazily imports ``vision_analyze_tool`` and runs it via
    # asyncio.run; inject a fake async tool so the per-image memo is exercised
    # for real (it must run analysis only once per distinct image).
    import sys
    from types import ModuleType

    mod = ModuleType("tools.implementations.vision_tools")
    mod.vision_analyze_tool = fake_vision  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"tools.implementations.vision_tools": mod}):
        note1 = preparer.describe_image_for_anthropic_fallback("https://img/1.png", "user")
        note2 = preparer.describe_image_for_anthropic_fallback("https://img/1.png", "user")

    assert "a red square" in note1
    assert note1 == note2
    # Second call served from cache → vision analysis ran exactly once.
    assert calls["n"] == 1


def test_prepare_flattens_image_message(agent):
    preparer = agent._anthropic_message_preparer_obj
    preparer.image_fallback_cache[
        __import__("hashlib").sha256(b"https://img/x.png").hexdigest()
    ] = "[The user attached an image. Here's what it contains:\na cat]"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://img/x.png"}},
                {"type": "text", "text": "what is this?"},
            ],
        }
    ]
    out = agent._prepare_anthropic_messages_for_api(messages)
    # Original untouched (deep-copied), output content flattened to a string.
    assert isinstance(messages[0]["content"], list)
    assert isinstance(out[0]["content"], str)
    assert "a cat" in out[0]["content"]
    assert "what is this?" in out[0]["content"]
