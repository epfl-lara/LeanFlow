"""Focused tests for the AnthropicMessagePreparer collaborator extracted from AIAgent.

Covers the resolve-accessor materialization (real agents, ``__new__`` agents,
MagicMock fakes), the AIAgent delegation identity (wrappers forward to the
collaborator), and the moved logic: no-image passthrough, the image-part
detector, and the full multimodal→text flattening. The native Anthropic route
does not forward image content, so image parts flatten to a static placeholder
(no vision/multimodal analysis).
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.providers.anthropic_messages import (
    AnthropicMessagePreparer,
    content_has_image_parts,
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


def test_resolve_materializes_for_mock_agent():
    """A MagicMock fake agent gets a real AnthropicMessagePreparer (isinstance guard)."""
    fake = MagicMock()
    preparer = _resolve_anthropic_message_preparer(fake)
    assert isinstance(preparer, AnthropicMessagePreparer)


def test_wrappers_delegate_to_collaborator(agent):
    preparer = agent._anthropic_message_preparer_obj
    preparer.prepare_anthropic_messages_for_api = MagicMock(return_value=["sentinel"])
    preparer.preprocess_anthropic_content = MagicMock(return_value="flat")

    assert agent._prepare_anthropic_messages_for_api([{"role": "user"}]) == ["sentinel"]
    assert agent._preprocess_anthropic_content(["x"], "user") == "flat"


# ── image-part detector ──────────────────────────────────────────────────────


def test_content_has_image_parts():
    assert content_has_image_parts([{"type": "image_url", "image_url": {"url": "x"}}]) is True
    assert content_has_image_parts([{"type": "input_image"}]) is True
    assert content_has_image_parts([{"type": "text", "text": "hi"}]) is False
    assert content_has_image_parts("plain string") is False
    # AIAgent staticmethod wrapper forwards identically.
    assert AIAgent._content_has_image_parts([{"type": "image_url"}]) is True


# ── behavior: flattening ─────────────────────────────────────────────────────


def test_prepare_passthrough_when_no_images(agent):
    messages = [{"role": "user", "content": "hello"}]
    # Identity passthrough (no deep-copy) when no image parts present.
    assert agent._prepare_anthropic_messages_for_api(messages) is messages


def test_prepare_flattens_image_message_to_placeholder(agent):
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
    # Original untouched (deep-copied); output content flattened to a string with a
    # placeholder for the image (no vision analysis) plus the text part preserved.
    assert isinstance(messages[0]["content"], list)
    assert isinstance(out[0]["content"], str)
    assert "image content is not processed" in out[0]["content"]
    assert "what is this?" in out[0]["content"]
