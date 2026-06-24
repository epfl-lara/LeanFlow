"""Lazy collaborator accessors for :class:`run_agent.AIAgent`.

Each ``_resolve_X(agent)`` returns the collaborator object bound to ``agent``,
materializing and caching it on first use. They are module-level (not methods)
so they work uniformly for: real agents built via ``__init__`` (cache hit),
agents built via ``__new__`` that bypass ``__init__`` (cache miss -> fresh
collaborator), and ``MagicMock`` "fake agents" used by some tests (attribute
access auto-creates a Mock rather than raising, so the ``isinstance`` guard
forces a real collaborator bound to the mock). Caching is best-effort; if
assignment fails we still return a working collaborator.

Extracted verbatim from ``run_agent`` and re-exported there for backwards
compatibility, so ``run_agent._resolve_X`` (used by call sites and tests)
continues to resolve. This module must NOT import ``run_agent`` (no cycle).
"""

from typing import Any

from agent.compression.compression_policy import CompressionPolicy
from agent.compression.conversation_manager import ConversationManager
from agent.display.output_manager import OutputManager
from agent.execution.interrupt_controller import InterruptController
from agent.execution.tool_executor import ToolExecutor
from agent.prompting.prompt_manager import PromptManager
from agent.prompting.response_normalizer import ResponseNormalizer
from agent.providers.anthropic_messages import AnthropicMessagePreparer
from agent.providers.api_caller import ApiCaller


def _resolve_tool_executor(agent: Any) -> ToolExecutor:
    """Return a ToolExecutor bound to ``agent``, materializing/caching on demand.

    Module-level (not a method) so it works uniformly for: real agents built via
    ``__init__`` (cache hit), agents built via ``__new__`` that bypass ``__init__``
    (cache miss → fresh executor), and MagicMock "fake agents" used by some
    interrupt tests (attribute access auto-creates a Mock rather than raising, so
    the isinstance guard forces a real executor bound to the mock). Caching is
    best-effort; if assignment fails we still return a working executor.
    """
    executor = getattr(agent, "_tool_executor_obj", None)
    if not isinstance(executor, ToolExecutor):
        executor = ToolExecutor(agent)
        try:
            agent._tool_executor_obj = executor
        except Exception:
            pass
    return executor


def _resolve_conversation_manager(agent: Any) -> ConversationManager:
    """Return a ConversationManager bound to ``agent``, materializing on demand.

    Module-level (not a method) so it works uniformly for: real agents built via
    ``__init__`` (cache hit), agents built via ``__new__`` that bypass ``__init__``
    (cache miss → fresh manager), and MagicMock "fake agents" (attribute access
    auto-creates a Mock rather than raising, so the isinstance guard forces a real
    manager bound to the mock). Caching is best-effort; if assignment fails we
    still return a working manager. Mirrors ``_resolve_tool_executor``.
    """
    manager = getattr(agent, "_conversation_manager_obj", None)
    if not isinstance(manager, ConversationManager):
        manager = ConversationManager(agent)
        try:
            agent._conversation_manager_obj = manager
        except Exception:
            pass
    return manager


def _resolve_compression_policy(agent: Any) -> CompressionPolicy:
    """Return a CompressionPolicy bound to ``agent``, materializing on demand.

    Module-level (not a method) so it works uniformly for: real agents built via
    ``__init__`` (cache hit), agents built via ``__new__`` that bypass ``__init__``
    (cache miss → fresh policy), and MagicMock "fake agents" used by some tests
    that call ``AIAgent._compress_context(mock_agent, ...)`` unbound (attribute
    access auto-creates a Mock rather than raising, so the isinstance guard forces
    a real policy bound to the mock). Caching is best-effort; if assignment fails
    we still return a working policy. Mirrors ``_resolve_conversation_manager``.
    """
    policy = getattr(agent, "_compression_policy_obj", None)
    if not isinstance(policy, CompressionPolicy):
        policy = CompressionPolicy(agent)
        try:
            agent._compression_policy_obj = policy
        except Exception:
            pass
    return policy


def _resolve_interrupt_controller(agent: Any) -> InterruptController:
    """Return the InterruptController for ``agent``, materializing on demand.

    Module-level (not a method) so it works uniformly for: real agents built via
    ``__init__`` (cache hit), agents built via ``__new__`` that bypass ``__init__``
    (cache miss → fresh controller), and MagicMock "fake agents" used by some
    interrupt tests (attribute access auto-creates a Mock rather than raising, so
    the isinstance guard forces a real controller). Caching is best-effort; if
    assignment fails we still return a working controller. Mirrors
    ``_resolve_tool_executor`` / ``_resolve_conversation_manager``.
    """
    controller = getattr(agent, "_interrupts", None)
    if not isinstance(controller, InterruptController):
        controller = InterruptController()
        try:
            agent._interrupts = controller
        except Exception:
            pass
    return controller


def _resolve_api_caller(agent: Any) -> ApiCaller:
    """Return an ApiCaller bound to ``agent``, materializing/caching on demand.

    Module-level (not a method) so it works uniformly for: real agents built via
    ``__init__`` (cache hit), agents built via ``__new__`` that bypass ``__init__``
    (cache miss → fresh caller), and MagicMock "fake agents" used by some tests
    (attribute access auto-creates a Mock rather than raising, so the isinstance
    guard forces a real caller bound to the mock). Caching is best-effort; if
    assignment fails we still return a working caller. Mirrors
    ``_resolve_tool_executor`` / ``_resolve_conversation_manager``.
    """
    caller = getattr(agent, "_api_caller_obj", None)
    if not isinstance(caller, ApiCaller):
        caller = ApiCaller(agent)
        try:
            agent._api_caller_obj = caller
        except Exception:
            pass
    return caller


def _resolve_response_normalizer(agent: Any) -> ResponseNormalizer:
    """Return a ResponseNormalizer bound to ``agent``, materializing on demand.

    Module-level (not a method) so it works uniformly for: real agents built via
    ``__init__`` (cache hit), agents built via ``__new__`` that bypass ``__init__``
    (cache miss → fresh normalizer), and MagicMock "fake agents" (attribute access
    auto-creates a Mock rather than raising, so the isinstance guard forces a real
    normalizer bound to the mock). Caching is best-effort; if assignment fails we
    still return a working normalizer. Mirrors ``_resolve_conversation_manager``.
    """
    normalizer = getattr(agent, "_response_normalizer_obj", None)
    if not isinstance(normalizer, ResponseNormalizer):
        normalizer = ResponseNormalizer(agent)
        try:
            agent._response_normalizer_obj = normalizer
        except Exception:
            pass
    return normalizer


def _resolve_anthropic_message_preparer(agent: Any) -> AnthropicMessagePreparer:
    """Return an AnthropicMessagePreparer bound to ``agent``, materializing on demand.

    Module-level (not a method) so it works uniformly for: real agents built via
    ``__init__`` (cache hit), agents built via ``__new__`` that bypass ``__init__``
    (cache miss → fresh preparer with an empty image-description memo), and
    MagicMock "fake agents" (the isinstance guard forces a real preparer bound to
    the mock). Caching is best-effort; if assignment fails we still return a
    working preparer. Mirrors ``_resolve_response_normalizer``.
    """
    preparer = getattr(agent, "_anthropic_message_preparer_obj", None)
    if not isinstance(preparer, AnthropicMessagePreparer):
        preparer = AnthropicMessagePreparer(agent)
        try:
            agent._anthropic_message_preparer_obj = preparer
        except Exception:
            pass
    return preparer


def _resolve_prompt_manager(agent: Any) -> PromptManager:
    """Return a PromptManager bound to ``agent``, materializing on demand.

    Module-level (not a method) so it works uniformly for: real agents built via
    ``__init__`` (cache hit), agents built via ``__new__`` that bypass ``__init__``
    (cache miss → fresh manager), and MagicMock "fake agents" (attribute access
    auto-creates a Mock rather than raising, so the isinstance guard forces a real
    manager bound to the mock). Caching is best-effort; if assignment fails we
    still return a working manager. Mirrors ``_resolve_conversation_manager``.

    NOTE: the PromptManager owns the cached system prompt, so ``_cached_system_prompt``
    on AIAgent is a @property delegating to ``manager.cached``. To avoid resetting
    that cache when this accessor materializes a fresh manager, the property's
    getter/setter use this same accessor — the cache lives on the (single,
    cached) manager instance.
    """
    manager = getattr(agent, "_prompt_manager_obj", None)
    if not isinstance(manager, PromptManager):
        manager = PromptManager(agent)
        try:
            agent._prompt_manager_obj = manager
        except Exception:
            pass
    return manager


def _resolve_output_manager(agent: Any) -> OutputManager:
    """Return an OutputManager bound to ``agent``, materializing on demand.

    Module-level (not a method) so it works uniformly for: real agents built via
    ``__init__`` (cache hit), agents built via ``__new__`` that bypass ``__init__``
    (cache miss → fresh manager), and MagicMock "fake agents" (attribute access
    auto-creates a Mock rather than raising, so the isinstance guard forces a real
    manager bound to the mock). Caching is best-effort; if assignment fails we
    still return a working manager. Mirrors ``_resolve_prompt_manager``.
    """
    manager = getattr(agent, "_output_manager_obj", None)
    if not isinstance(manager, OutputManager):
        manager = OutputManager(agent)
        try:
            agent._output_manager_obj = manager
        except Exception:
            pass
    return manager
