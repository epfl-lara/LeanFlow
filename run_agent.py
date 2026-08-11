#!/usr/bin/env python3
"""
AI Agent Runner with Tool Calling

This module provides a clean, standalone agent that can execute AI models
with tool calling capabilities. It handles the conversation loop, tool execution,
and response management.

Features:
- Automatic tool calling loop until completion
- Configurable model parameters
- Error handling and recovery
- Message history management
- Support for multiple model providers

Usage:
    from run_agent import AIAgent

    agent = AIAgent(base_url="http://localhost:30000/v1", model="claude-opus-4-20250514")
    response = agent.run_conversation("Tell me about the latest Python updates")
"""

import copy
import hashlib
import json
import logging

logger = logging.getLogger(__name__)
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import fire

from core.home import leanflow_home

# Load .env from the active LeanFlow home first, then project root as dev fallback.
# User-managed env files should override stale shell exports on restart.
from leanflow_cli.runtime.env_loader import (
    load_leanflow_dotenv,
    reassert_native_auxiliary_provider,
)

_leanflow_home = leanflow_home()
_project_env = Path(__file__).parent / ".env"
_loaded_env_paths = load_leanflow_dotenv(leanflow_home=_leanflow_home, project_env=_project_env)
_native_auxiliary_provider = reassert_native_auxiliary_provider()
if _loaded_env_paths:
    for _env_path in _loaded_env_paths:
        logger.info("Loaded environment variables from %s", _env_path)
else:
    logger.info("No .env file found. Using system environment variables.")
if _native_auxiliary_provider:
    logger.info(
        "Forced native auxiliary model lanes onto provider %s",
        _native_auxiliary_provider,
    )


# Import our tool system

from agent.accounting.error_log import ensure_error_log_handler
from agent.accounting.redact import (
    RedactingFormatter,
    redact_sensitive_text,
    redact_sensitive_value,
)
from agent.accounting.token_accounting import TokenAccounter
from agent.compression.compression_policy import CompressionPolicy
from agent.compression.context_compressor import ContextCompressor
from agent.compression.conversation_manager import ConversationManager
from agent.compression.conversation_manager import clean_session_content as _clean_session_content
from agent.display.display import (
    KawaiiSpinner,
)
from agent.display.output_manager import OutputManager
from agent.execution.interrupt_controller import InterruptController
from agent.execution.tool_executor import ToolExecutor

# Agent internals extracted to agent/ package for modularity
from agent.prompting.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
)
from agent.prompting.prompt_manager import PromptManager
from agent.prompting.reasoning_processor import ReasoningProcessor
from agent.prompting.response_normalizer import ResponseNormalizer
from agent.providers.anthropic_messages import (
    AnthropicMessagePreparer,
    content_has_image_parts,
)
from agent.providers.api_caller import (
    TRANSIENT_PROVIDER_MAX_ATTEMPTS,
    ApiCaller,
    TransientProviderRetriesExhausted,
    transient_provider_recovery_deadline_monotonic,
    transient_provider_retry_delay_within_deadline_s,
)
from agent.providers.model_metadata import (
    estimate_messages_tokens_rough,
    estimate_tokens_rough,
    get_next_probe_tier,
    parse_context_limit_from_error,
    save_context_length,
)
from agent.providers.provider_client import ProviderClientFactory
from agent.runtime.trajectory import (
    has_incomplete_scratchpad,
)
from core.constants import OPENROUTER_BASE_URL
from core.provider_availability import (
    extract_provider_usage_limit,
    provider_reset_wait_max_seconds,
)
from model_tools import check_toolset_requirements, get_tool_definitions
from tools.implementations.terminal_tool import cleanup_vm
from tools.utilities.interrupt import set_interrupt as _set_interrupt


def _cleanup_optional_browser_state(task_id: str) -> None:
    """Browser session cleanup was removed with the legacy browser surface."""
    del task_id


from agent.runtime.runtime_helpers import (  # noqa: E402,F401
    _generate_short_session_id,
    _install_safe_stdio,
    _SafeWriter,
)


class IterationBudget:
    """Thread-safe shared iteration counter for parent and child agents.

    Tracks total LLM-call iterations consumed across a parent agent and all
    its subagents.  A single ``IterationBudget`` is created by the parent
    and passed to every child so they share the same cap.

    ``execute_code`` (programmatic tool calling) iterations are refunded via
    :meth:`refund` so they don't eat into the budget.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration.  Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self, count: int = 1) -> None:
        """Give back one or more iterations (e.g. for execute_code turns)."""
        try:
            amount = int(count)
        except (TypeError, ValueError):
            amount = 1
        if amount <= 0:
            return
        with self._lock:
            if self._used > 0:
                self._used = max(0, self._used - amount)

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


# Managed source-edit callbacks use one assignment-bound snapshot on the agent.
# Serializing the entire model-authored batch keeps each edit's preflight,
# execution, and post-result verification atomic with respect to its siblings.
_MANAGED_SOURCE_EDIT_TOOLS = frozenset({"patch", "write_file", "apply_verified_patch"})

# Tools that must never run concurrently. Interactive tools need serialized
# user interaction; decomposition must observe prerequisite source-discovery
# results from earlier calls in the same model-authored batch.
_NEVER_PARALLEL_TOOLS = frozenset({"clarify", "lean_decompose_helpers"}).union(
    _MANAGED_SOURCE_EDIT_TOOLS
)

# Maximum number of concurrent worker threads for parallel tool execution.
_MAX_TOOL_WORKERS = 8

_DEFAULT_MAX_TOOL_RESULT_CHARS = 100_000
_LEAN_REASONING_HELP_MAX_TOOL_RESULT_CHARS = 260_000
_POST_TOOL_TAIL_SLOW_THRESHOLD_S = 1.0

# Re-exported leaf helpers extracted into the agent/ package, kept importable from
# run_agent for backwards compatibility (tests + native_runner reference these paths):
#   - agent/command_safety.py: destructive terminal-command detection
#     (run_agent._is_destructive_command remains valid for internal call sites).
#   - agent/log_formatting.py: tool argument/result log rendering
#     (run_agent._wrap_log_text / _format_tool_result_for_log / ...).


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


# Lazy collaborator accessors extracted into agent/collaborator_resolvers.py,
# re-exported here so call sites (and tests) continue to resolve
# ``run_agent._resolve_X``. See that module for the rationale behind the
# module-level + isinstance-guard pattern.
import contextlib

from openai import OpenAI  # noqa: F401

from agent.accounting.usage_pricing import (
    estimate_cost_usd,  # noqa: F401
    has_known_pricing,  # noqa: F401
    has_listed_pricing,  # noqa: F401
)
from agent.display.display import _detect_tool_failure  # noqa: F401
from agent.display.display import build_tool_preview as _build_tool_preview  # noqa: F401
from agent.display.display import get_cute_tool_message as _get_cute_tool_message_impl  # noqa: F401
from agent.display.display import get_tool_emoji as _get_tool_emoji  # noqa: F401
from agent.display.log_formatting import (
    _format_tool_args_for_log,  # noqa: F401
    _format_tool_result_for_log,  # noqa: F401
    _format_tool_result_for_log_with_limits,  # noqa: F401
    _summarize_arg_value,  # noqa: F401
    _truncate_log_lines,  # noqa: F401
    _wrap_log_text,  # noqa: F401
)
from agent.execution.collaborator_resolvers import (  # noqa: E402
    _resolve_anthropic_message_preparer,
    _resolve_api_caller,
    _resolve_compression_policy,
    _resolve_conversation_manager,
    _resolve_interrupt_controller,
    _resolve_output_manager,
    _resolve_prompt_manager,
    _resolve_response_normalizer,
    _resolve_tool_executor,
)
from agent.execution.command_safety import (
    _DESTRUCTIVE_PATTERNS,  # noqa: F401
    _REDIRECT_OVERWRITE,  # noqa: F401
    _is_destructive_command,  # noqa: F401
)
from agent.prompting.prompt_builder import (
    MEMORY_GUIDANCE,  # noqa: F401
    PLATFORM_HINTS,  # noqa: F401
    SESSION_SEARCH_GUIDANCE,  # noqa: F401
    SKILLS_GUIDANCE,  # noqa: F401
    build_context_files_prompt,  # noqa: F401
    build_skills_system_prompt,  # noqa: F401
)
from agent.runtime.workflow_events import (  # noqa: E402,F401
    _emit_workflow_event,
    _workflow_agent_event_details,
    build_api_request_activity_details,
)
from model_tools import handle_function_call  # noqa: F401
from utils import atomic_json_write  # noqa: F401


class AIAgent:
    """
    AI Agent with tool calling capabilities.

    This class manages the conversation flow, tool execution, and response handling
    for AI models that support function calling.
    """

    def __init__(
        self,
        base_url: str = None,
        api_key: str = None,
        provider: str = None,
        api_mode: str = None,
        model: str = "anthropic/claude-opus-4.6",  # OpenRouter format
        max_iterations: int = 200,  # Default tool-calling iterations (shared with subagents)
        tool_delay: float = 1.0,
        enabled_toolsets: list[str] = None,
        disabled_toolsets: list[str] = None,
        save_trajectories: bool = False,
        verbose_logging: bool = False,
        quiet_mode: bool = False,
        ephemeral_system_prompt: str = None,
        log_prefix_chars: int = 100,
        log_prefix: str = "",
        log_preview_lines: int = 8,
        log_preview_chars: int = 1600,
        tool_output_head_lines: int = 28,
        tool_output_tail_lines: int = 12,
        providers_allowed: list[str] = None,
        providers_ignored: list[str] = None,
        providers_order: list[str] = None,
        provider_sort: str = None,
        provider_require_parameters: bool = False,
        provider_data_collection: str = None,
        session_id: str = None,
        tool_progress_callback: callable = None,
        pre_tool_call_callback: callable = None,
        post_tool_result_callback: callable = None,
        tool_result_projection_callback: callable = None,
        wall_timeout_s: float = None,
        thinking_callback: callable = None,
        reasoning_callback: callable = None,
        clarify_callback: callable = None,
        step_callback: callable = None,
        max_tokens: int = None,
        reasoning_config: dict[str, Any] = None,
        seed: int = 42,
        temperature: float = 0.3,
        top_p: float = None,
        top_k: int = None,
        min_p: float = None,
        prefill_messages: list[dict[str, Any]] = None,
        platform: str = None,
        skip_context_files: bool = False,
        skip_memory: bool = False,
        session_db=None,
        iteration_budget: "IterationBudget" = None,
        fallback_model: dict[str, Any] = None,
        checkpoints_enabled: bool = False,
        checkpoint_max_snapshots: int = 50,
        pass_session_id: bool = False,
        compression_threshold_tokens: int | None = None,
    ):
        """
        Initialize the AI Agent.

        Args:
            base_url (str): Base URL for the model API (optional)
            api_key (str): API key for authentication (optional, uses env var if not provided)
            provider (str): Provider identifier (optional; used for telemetry/routing hints)
            api_mode (str): API mode override: "chat_completions" or "codex_responses"
            model (str): Model name to use (default: "anthropic/claude-opus-4.6")
            max_iterations (int): Maximum number of tool calling iterations (default: 200)
            tool_delay (float): Delay between tool calls in seconds (default: 1.0)
            enabled_toolsets (List[str]): Only enable tools from these toolsets (optional)
            disabled_toolsets (List[str]): Disable tools from these toolsets (optional)
            save_trajectories (bool): Whether to save conversation trajectories to JSONL files (default: False)
            verbose_logging (bool): Enable verbose logging for debugging (default: False)
            quiet_mode (bool): Suppress progress output for clean CLI experience (default: False)
            ephemeral_system_prompt (str): System prompt used during agent execution but NOT saved to trajectories (optional)
            log_prefix_chars (int): Number of characters to show in log previews for tool calls/responses (default: 100)
            log_prefix (str): Prefix to add to all log messages for identification in parallel processing (default: "")
            log_preview_lines (int): Number of lines to show in assistant/reasoning previews.
            log_preview_chars (int): Maximum characters to show in assistant/reasoning previews.
            tool_output_head_lines (int): Number of lines to keep from the start of long tool outputs.
            tool_output_tail_lines (int): Number of lines to keep from the end of long tool outputs.
            providers_allowed (List[str]): OpenRouter providers to allow (optional)
            providers_ignored (List[str]): OpenRouter providers to ignore (optional)
            providers_order (List[str]): OpenRouter providers to try in order (optional)
            provider_sort (str): Sort providers by price/throughput/latency (optional)
            session_id (str): Pre-generated session ID for logging (optional, auto-generated if not provided)
            tool_progress_callback (callable): Callback function(tool_name, args_preview) for progress notifications
            pre_tool_call_callback (callable): Callback function(tool_name, args_dict) invoked before each tool.
                If it returns a string or JSON-serializable object, the tool call is skipped and that value is
                used as the tool result.
            post_tool_result_callback (callable): Callback function(tool_name, args_dict, result_text)
                invoked after each tool finishes. Can request an interrupt to stop after a
                workflow boundary such as the first file edit.
            tool_result_projection_callback (callable): Callback that returns a bounded model-facing
                tool result after audit and managed callbacks have consumed the original result.
            wall_timeout_s (float): Optional per-conversation wall-clock deadline. Provider request
                timeouts are clipped to the remaining budget and the loop stops at a safe boundary.
            clarify_callback (callable): Callback function(question, choices) -> str for interactive user questions.
                Provided by the platform layer (CLI or gateway). If None, the clarify tool returns an error.
            max_tokens (int): Maximum tokens for model responses (optional, uses model default if not set)
            reasoning_config (Dict): OpenRouter reasoning configuration override (e.g. {"effort": "none"} to disable thinking).
                If None, defaults to {"enabled": True, "effort": "high"} for OpenRouter. Set to disable/customize reasoning.
            seed (int): Optional generation seed for reproducible sampling on compatible routes.
            temperature (float): Optional sampling temperature override.
            top_p (float): Optional nucleus sampling override for compatible routes.
            top_k (int): Optional top-k sampling override for compatible vLLM-style routes.
            min_p (float): Optional min-p sampling override for compatible vLLM-style routes.
            prefill_messages (List[Dict]): Messages to prepend to conversation history as prefilled context.
                Useful for injecting a few-shot example or priming the model's response style.
                Example: [{"role": "user", "content": "Hi!"}, {"role": "assistant", "content": "Hello!"}]
            platform (str): The interface platform the user is on (e.g. "cli", "telegram", "discord", "whatsapp").
                Used to inject platform-specific formatting hints into the system prompt.
            skip_context_files (bool): If True, skip auto-injection of SOUL.md, AGENTS.md, and .cursorrules
                into the system prompt. Use this for batch processing and data generation to avoid
                polluting trajectories with user-specific persona or project instructions.
        """
        _install_safe_stdio()

        self.model = model
        self.max_iterations = max_iterations
        # Shared iteration budget — parent creates, children inherit.
        # Consumed by every LLM turn across parent + all subagents.
        self.iteration_budget = iteration_budget or IterationBudget(max_iterations)
        self.tool_delay = tool_delay
        self.save_trajectories = save_trajectories
        self.verbose_logging = verbose_logging
        self.quiet_mode = quiet_mode
        self.ephemeral_system_prompt = ephemeral_system_prompt
        self.platform = platform  # "cli", "telegram", "discord", "whatsapp", etc.
        self.skip_context_files = skip_context_files
        self.pass_session_id = pass_session_id
        self.log_prefix_chars = log_prefix_chars
        self.log_prefix = f"{log_prefix} " if log_prefix else ""
        self.log_preview_lines = _positive_int(log_preview_lines, 8)
        self.log_preview_chars = _positive_int(log_preview_chars, 1600)
        self.tool_output_head_lines = _positive_int(tool_output_head_lines, 28)
        self.tool_output_tail_lines = _positive_int(tool_output_tail_lines, 12)
        # Store effective base URL for feature detection (prompt caching, reasoning, etc.)
        # When no base_url is provided, the client defaults to OpenRouter, so reflect that here.
        self.base_url = base_url or OPENROUTER_BASE_URL
        self.api_key = api_key.strip() if isinstance(api_key, str) else (api_key or "")
        provider_name = (
            provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
        )
        self.provider = provider_name or "openrouter"
        if api_mode in {"chat_completions", "codex_responses", "anthropic_messages"}:
            self.api_mode = api_mode
        elif self.provider == "openai-codex":
            self.api_mode = "codex_responses"
        elif (provider_name is None) and "chatgpt.com/backend-api/codex" in self.base_url.lower():
            self.api_mode = "codex_responses"
            self.provider = "openai-codex"
        elif self.provider == "anthropic" or (
            provider_name is None and "api.anthropic.com" in self.base_url.lower()
        ):
            self.api_mode = "anthropic_messages"
            self.provider = "anthropic"
        else:
            self.api_mode = "chat_completions"

        self.tool_progress_callback = tool_progress_callback
        self.pre_tool_call_callback = pre_tool_call_callback
        self.post_tool_result_callback = post_tool_result_callback
        self.tool_result_projection_callback = tool_result_projection_callback
        self.wall_timeout_s = (
            max(1.0, float(wall_timeout_s))
            if isinstance(wall_timeout_s, (int, float)) and not isinstance(wall_timeout_s, bool)
            else None
        )
        self._conversation_deadline_monotonic: float | None = None
        self._conversation_wall_timeout_reached = False
        self.thinking_callback = thinking_callback
        self.reasoning_callback = reasoning_callback
        self.clarify_callback = clarify_callback
        self.step_callback = step_callback
        self._last_reported_tool = None  # Track for "new tool" mode

        # Interrupt mechanism for breaking out of tool loops. The interrupt flag,
        # message and child registry (for subagent propagation) live on this
        # collaborator; AIAgent exposes them via delegating @property shims below
        # so every existing read/write/mutation keeps working unchanged.
        self._interrupts = InterruptController()
        self._client_lock = threading.RLock()
        # Collaborator that owns provider/OpenAI client construction and the
        # credential-resolution half of the refreshers. AIAgent keeps the lock
        # and the lifecycle state (self.client / self._client_kwargs / the
        # anthropic_* fields); it delegates construction + auth to this factory.
        self._provider_clients = ProviderClientFactory()

        # Tool-call execution strategy (concurrent/sequential dispatch + single
        # invocation + preflight) lives in this collaborator; AIAgent keeps the
        # turn loop state it reaches through (callbacks, interrupt flag, budget,
        # checkpoint mgr, appendix) and delegates dispatch to it.
        self._tool_executor_obj = ToolExecutor(self)

        # Provider API-call mediation (the interruptible/streaming background-thread
        # request runner + timeout/heartbeat watchdog, the Codex Responses stream
        # helpers, per-request timeout resolution and api_kwargs assembly) lives in
        # this collaborator; AIAgent keeps the request-lifecycle state and helpers
        # it reaches through (api_mode/model/base_url/provider prefs, the client
        # constructors, abort/heartbeat helpers, the interrupt flag) and delegates
        # the mediation to it via the thin wrappers below.
        self._api_caller_obj = ApiCaller(self)

        # Session/message persistence + per-turn API-message shaping live in this
        # collaborator; AIAgent keeps the conversation state it reaches through
        # (session_db, session_id, session_log_file, history, cached system
        # prompt, model/provider metadata) and delegates the persistence and
        # message-build logic to it.
        self._conversation_manager_obj = ConversationManager(self)

        # Context-compression gating (the when/how of compression: threshold
        # checks, head/tail-preserving compressor driving, and the
        # retry-after-compression loops at the pre-advisor / suffix-preserving /
        # pre-send sites) lives in this collaborator. AIAgent keeps the
        # compression state it reaches through (context_compressor,
        # compression_enabled, todo store, session DB/id, cached system prompt,
        # the advisor reserve) and delegates the gating logic to it via the thin
        # wrappers below.
        self._compression_policy_obj = CompressionPolicy(self)

        # Assistant-response normalization (raw provider response -> the unified
        # {content, reasoning, finish_reason, tool_calls, ...} schema) lives in
        # this collaborator; AIAgent delegates via thin wrappers below.
        self._response_normalizer_obj = ResponseNormalizer(self)

        # System-prompt build/cache/invalidate lives in this collaborator. It
        # owns the cached system prompt, exposed on AIAgent via the
        # _cached_system_prompt @property below (so the many tests that do
        # `agent._cached_system_prompt = "..."` route through the manager).
        # Created here (before the _cached_system_prompt init below) so that
        # init's `= None` assignment lands on this same manager instance.
        self._prompt_manager_obj = PromptManager(self)
        # Verbose conversation-logging / usage-reporting display helpers live on
        # an OutputManager collaborator; AIAgent keeps thin delegating wrappers.
        self._output_manager_obj = OutputManager(self)

        # Subagent delegation state. The running-child registry used for interrupt
        # propagation lives on self._interrupts and is exposed via the
        # _active_children @property shim below.
        self._delegate_depth = 0  # 0 = top-level agent, incremented for children

        # Store OpenRouter provider preferences
        self.providers_allowed = providers_allowed
        self.providers_ignored = providers_ignored
        self.providers_order = providers_order
        self.provider_sort = provider_sort
        self.provider_require_parameters = provider_require_parameters
        self.provider_data_collection = provider_data_collection

        # Store toolset filtering options
        self.enabled_toolsets = enabled_toolsets
        self.disabled_toolsets = disabled_toolsets

        # Model response configuration
        self.max_tokens = max_tokens  # None = use model default
        self.reasoning_config = (
            reasoning_config  # None = use default (high for reasoning-capable routes)
        )
        self.seed = seed
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.min_p = min_p
        self.prefill_messages = prefill_messages or []  # Prefilled conversation turns

        # Anthropic prompt caching: auto-enabled for Claude models via OpenRouter.
        # Reduces input costs by ~75% on multi-turn conversations by caching the
        # conversation prefix. Uses system_and_3 strategy (4 breakpoints).
        is_openrouter = "openrouter" in self.base_url.lower()
        is_claude = "claude" in self.model.lower()
        is_native_anthropic = self.api_mode == "anthropic_messages"
        self._use_prompt_caching = (is_openrouter and is_claude) or is_native_anthropic
        self._cache_ttl = "5m"  # Default 5-minute TTL (1.25x write cost)

        # Iteration budget pressure: warn the LLM as it approaches max_iterations.
        # Warnings are injected into the last tool result JSON (not as separate
        # messages) so they don't break message structure or invalidate caching.
        self._budget_caution_threshold = 0.7  # 70% — nudge to start wrapping up
        self._budget_warning_threshold = 0.9  # 90% — urgent, respond now
        self._budget_pressure_enabled = True
        self._last_budget_message_content = ""
        try:
            self._advisor_result_context_reserve_tokens = max(
                0,
                int(os.getenv("LEAN_REASONING_HELP_CONTEXT_RESERVE_TOKENS", "90000")),
            )
        except (TypeError, ValueError):
            self._advisor_result_context_reserve_tokens = 90000

        # Error logging is optional and follows the runtime LeanFlow home. The
        # process-global handler manager owns deduplication and home changes.
        ensure_error_log_handler()

        if self.verbose_logging:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%H:%M:%S",
            )
            for handler in logging.getLogger().handlers:
                handler.setFormatter(
                    RedactingFormatter(
                        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                        datefmt="%H:%M:%S",
                    )
                )
            # Keep third-party libraries at WARNING level to reduce noise
            # We have our own retry and error logging that's more informative
            logging.getLogger("openai").setLevel(logging.WARNING)
            logging.getLogger("openai._base_client").setLevel(logging.WARNING)
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("httpcore").setLevel(logging.WARNING)
            logging.getLogger("asyncio").setLevel(logging.WARNING)
            # Suppress Modal/gRPC related debug spam
            logging.getLogger("hpack").setLevel(logging.WARNING)
            logging.getLogger("hpack.hpack").setLevel(logging.WARNING)
            logging.getLogger("grpc").setLevel(logging.WARNING)
            logging.getLogger("modal").setLevel(logging.WARNING)
            logging.getLogger("rex-deploy").setLevel(logging.INFO)  # Keep INFO for sandbox status
            logger.info("Verbose logging enabled (third-party library logs suppressed)")
        else:
            # Set logging to INFO level for important messages only
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(levelname)s - %(message)s",
                datefmt="%H:%M:%S",
            )
            # Suppress noisy library logging
            logging.getLogger("openai").setLevel(logging.ERROR)
            logging.getLogger("openai._base_client").setLevel(logging.ERROR)
            logging.getLogger("httpx").setLevel(logging.ERROR)
            logging.getLogger("httpcore").setLevel(logging.ERROR)
            if self.quiet_mode:
                # In quiet mode (CLI default), suppress all tool/infra log
                # noise. The TUI has its own rich display for status; logger
                # INFO/WARNING messages just clutter it.
                for quiet_logger in [
                    "tools",  # all tools.* (terminal, web, file, etc.)
                    "run_agent",  # agent runner internals
                    "cron",  # legacy scheduler logger if present
                ]:
                    logging.getLogger(quiet_logger).setLevel(logging.ERROR)

        # Internal stream callback (set during streaming TTS).
        # Initialized here so _vprint can reference it before run_conversation.
        self._stream_callback = None

        # Optional current-turn user-message override used when the API-facing
        # user message intentionally differs from the persisted transcript
        # (e.g. CLI voice mode adds a temporary prefix for the live call only).
        self._persist_user_message_idx = None
        self._persist_user_message_override = None

        # Anthropic message preparation (multimodal → text flattening) lives on
        # the AnthropicMessagePreparer collaborator: the native Anthropic route
        # does not forward image content, so image parts are replaced with a short
        # text placeholder.
        self._anthropic_message_preparer_obj = AnthropicMessagePreparer(self)

        # Initialize LLM client via centralized provider router.
        # The router handles auth resolution, base URL, headers, and
        # Codex/Anthropic wrapping for all known providers.
        # raw_codex=True because the main agent needs direct responses.stream()
        # access for Codex Responses API streaming.
        self._anthropic_client = None

        if self.api_mode == "anthropic_messages":
            effective_key = (
                api_key or self._provider_client_factory().resolve_anthropic_token() or ""
            )
            self._anthropic_api_key = effective_key
            self._anthropic_base_url = base_url
            self.api_key = effective_key
            if isinstance(base_url, str) and base_url.strip():
                self.base_url = base_url.strip().rstrip("/")
            self._anthropic_client = self._provider_client_factory().build_anthropic_client(
                effective_key, base_url
            )
            # No OpenAI client needed for Anthropic mode
            self.client = None
            self._client_kwargs = {}
            if not self.quiet_mode:
                print(f"🤖 AI Agent initialized with model: {self.model} (Anthropic native)")
                if effective_key and len(effective_key) > 12:
                    print("🔑 Using configured API credentials")
        else:
            if api_key and base_url:
                # Explicit credentials from CLI/gateway — construct directly.
                # The runtime provider resolver already handled auth for us.
                client_kwargs = self._provider_client_factory().build_explicit_client_kwargs(
                    api_key, base_url
                )
            else:
                # No explicit creds — use the centralized provider router.
                client_kwargs = self._provider_client_factory().build_routed_client_kwargs(
                    self.provider or "auto", self.model
                )

            self._client_kwargs = client_kwargs  # stored for rebuilding after interrupt
            self.api_key = str(client_kwargs.get("api_key") or "")
            self.base_url = str(client_kwargs.get("base_url") or self.base_url).rstrip("/")
            try:
                self.client = self._create_openai_client(
                    client_kwargs, reason="agent_init", shared=True
                )
                if not self.quiet_mode:
                    print(f"🤖 AI Agent initialized with model: {self.model}")
                    if base_url:
                        print(f"🔗 Using custom base URL: {base_url}")
                    # Report only credential presence. Even a truncated key is
                    # credential material and must not enter captured workflow logs.
                    key_used = client_kwargs.get("api_key", "none")
                    if key_used and key_used != "dummy-key" and len(key_used) > 12:
                        print("🔑 Using configured API credentials")
                    else:
                        print("⚠️  Warning: API credentials appear invalid or missing")
            except Exception as e:
                raise RuntimeError(f"Failed to initialize OpenAI client: {e}") from e

        # Provider fallback — a single backup model/provider tried when the
        # primary is exhausted (rate-limit, overload, connection failure).
        # Config shape: {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}
        self._fallback_model = fallback_model if isinstance(fallback_model, dict) else None
        self._fallback_activated = False
        if self._fallback_model:
            fb_p = self._fallback_model.get("provider", "")
            fb_m = self._fallback_model.get("model", "")
            if fb_p and fb_m and not self.quiet_mode:
                print(f"🔄 Fallback model: {fb_m} ({fb_p})")

        # Get available tools with filtering
        self.tools = get_tool_definitions(
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            quiet_mode=self.quiet_mode,
        )

        # Show tool configuration and store valid tool names for validation
        self.valid_tool_names = set()
        if self.tools:
            self.valid_tool_names = {tool["function"]["name"] for tool in self.tools}
            tool_names = sorted(self.valid_tool_names)
            if not self.quiet_mode:
                print(f"🛠️  Loaded {len(self.tools)} tools: {', '.join(tool_names)}")

                # Show filtering info if applied
                if enabled_toolsets:
                    print(f"   ✅ Enabled toolsets: {', '.join(enabled_toolsets)}")
                if disabled_toolsets:
                    print(f"   ❌ Disabled toolsets: {', '.join(disabled_toolsets)}")
        elif not self.quiet_mode:
            print("🛠️  No tools loaded (all tools filtered out or unavailable)")

        # Check tool requirements
        if self.tools and not self.quiet_mode:
            requirements = check_toolset_requirements(enabled_toolsets)
            missing_reqs = [name for name, available in requirements.items() if not available]
            enabled_toolset_names = {str(name) for name in (enabled_toolsets or [])}
            native_lean_only = bool(
                enabled_toolset_names.intersection({"leanflow-native", "leanflow-native-swarm"})
            ) and not enabled_toolset_names.intersection({"web", "search"})
            if native_lean_only:
                missing_reqs = [name for name in missing_reqs if name != "web"]
            if missing_reqs:
                print(f"⚠️  Some tools may not work due to missing requirements: {missing_reqs}")

        # Show trajectory saving status
        if self.save_trajectories and not self.quiet_mode:
            print("📝 Trajectory saving enabled")

        # Show ephemeral system prompt status
        if self.ephemeral_system_prompt and not self.quiet_mode:
            prompt_preview = (
                self.ephemeral_system_prompt[:60] + "..."
                if len(self.ephemeral_system_prompt) > 60
                else self.ephemeral_system_prompt
            )
            print(f"🔒 Ephemeral system prompt: '{prompt_preview}' (not saved to trajectories)")

        # Show prompt caching status
        if self._use_prompt_caching and not self.quiet_mode:
            source = "native Anthropic" if is_native_anthropic else "Claude via OpenRouter"
            print(f"💾 Prompt caching: ENABLED ({source}, {self._cache_ttl} TTL)")

        # Session logging setup - auto-save conversation trajectories for debugging
        self.session_start = datetime.now()
        if session_id:
            # Use provided session ID (e.g., from CLI)
            self.session_id = session_id
        else:
            # Generate a new session ID
            self.session_id = _generate_short_session_id()

        # Session logs go into ~/.leanflow/sessions/ alongside gateway sessions
        self.logs_dir = leanflow_home() / "sessions"
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Session persistence is diagnostic and must not prevent proving.
            pass
        self.session_log_file = self.logs_dir / f"session_{self.session_id}.json"

        # Track conversation messages for session logging
        self._session_messages: list[dict[str, Any]] = []

        # Cached system prompt -- built once per session, only rebuilt on
        # compression. The cache lives on self._prompt_manager_obj; this
        # assignment routes through the _cached_system_prompt @property setter
        # below to initialize the manager's cache to None.
        self._cached_system_prompt = None

        # Filesystem checkpoint manager (transparent — not a tool)
        from tools.utilities.checkpoint_manager import CheckpointManager

        self._checkpoint_mgr = CheckpointManager(
            enabled=checkpoints_enabled,
            max_snapshots=checkpoint_max_snapshots,
        )

        # SQLite session store (optional -- provided by CLI or gateway)
        self._session_db = session_db
        self._last_flushed_db_idx = 0  # tracks DB-write cursor to prevent duplicate writes
        if self._session_db:
            try:
                self._session_db.create_session(
                    session_id=self.session_id,
                    source=self.platform or "cli",
                    model=self.model,
                    model_config={
                        "max_iterations": self.max_iterations,
                        "reasoning_config": reasoning_config,
                        "max_tokens": max_tokens,
                    },
                    user_id=None,
                )
            except Exception as e:
                logger.debug("Session DB create_session failed: %s", e)

        # In-memory todo list for task planning (one per agent/session)
        from tools.implementations.todo_tool import TodoStore

        self._todo_store = TodoStore()

        # Persistent memory (MEMORY.md + USER.md) -- loaded from disk
        self._memory_store = None
        self._memory_enabled = False
        self._user_profile_enabled = False
        self._memory_nudge_interval = 10
        self._memory_flush_min_turns = 6
        if not skip_memory:
            try:
                from leanflow_cli.config import load_config as _load_mem_config

                mem_config = _load_mem_config().get("memory", {})
                self._memory_enabled = mem_config.get("memory_enabled", False)
                self._user_profile_enabled = mem_config.get("user_profile_enabled", False)
                self._memory_nudge_interval = int(mem_config.get("nudge_interval", 10))
                self._memory_flush_min_turns = int(mem_config.get("flush_min_turns", 6))
                if self._memory_enabled or self._user_profile_enabled:
                    from tools.implementations.memory_tool import MemoryStore

                    self._memory_store = MemoryStore(
                        memory_char_limit=mem_config.get("memory_char_limit", 2200),
                        user_char_limit=mem_config.get("user_char_limit", 1375),
                    )
                    self._memory_store.load_from_disk()
            except Exception:
                pass  # Memory is optional -- don't break agent init

        # Skills config: nudge interval for skill creation reminders
        self._skill_nudge_interval = 10
        try:
            from leanflow_cli.config import load_config as _load_skills_config

            skills_config = _load_skills_config().get("skills", {})
            self._skill_nudge_interval = int(skills_config.get("creation_nudge_interval", 15))
        except Exception:
            pass

        # Initialize context compressor for automatic context management.
        compression_cfg = {}
        try:
            from leanflow_cli.config import load_config as _load_runtime_config

            loaded_cfg = _load_runtime_config()
            if isinstance(loaded_cfg.get("compression"), dict):
                compression_cfg = dict(loaded_cfg.get("compression") or {})
        except Exception:
            compression_cfg = {}

        compression_threshold = float(
            os.getenv("CONTEXT_COMPRESSION_THRESHOLD", str(compression_cfg.get("threshold", 0.75)))
        )
        compression_enabled = os.getenv(
            "CONTEXT_COMPRESSION_ENABLED",
            str(compression_cfg.get("enabled", True)).lower(),
        ).lower() in ("true", "1", "yes")
        compression_summary_model = (
            os.getenv("CONTEXT_COMPRESSION_MODEL") or compression_cfg.get("summary_model") or None
        )
        compression_reserved_output = int(
            os.getenv(
                "CONTEXT_COMPRESSION_RESERVED_OUTPUT_TOKENS",
                str(compression_cfg.get("reserved_output_tokens", 0) or 0),
            )
        )
        compression_prune_tool_output = os.getenv(
            "CONTEXT_COMPRESSION_PRUNE_TOOL_OUTPUT",
            str(compression_cfg.get("prune_tool_output", False)).lower(),
        ).lower() in ("true", "1", "yes")
        compression_prune_keep_recent_user_turns = int(
            os.getenv(
                "CONTEXT_COMPRESSION_PRUNE_KEEP_RECENT_USER_TURNS",
                str(compression_cfg.get("prune_keep_recent_user_turns", 2) or 2),
            )
        )

        self.context_compressor = ContextCompressor(
            model=self.model,
            threshold_percent=compression_threshold,
            protect_first_n=3,
            protect_last_n=4,
            summary_target_tokens=500,
            summary_model_override=compression_summary_model,
            quiet_mode=self.quiet_mode,
            base_url=self.base_url,
            api_key=self.api_key,
            main_provider=self.provider,
            main_api_mode=self.api_mode,
            reserved_output_tokens=compression_reserved_output,
            absolute_threshold_tokens=compression_threshold_tokens,
            prune_tool_output=compression_prune_tool_output,
            prune_keep_recent_user_turns=compression_prune_keep_recent_user_turns,
        )
        self.compression_enabled = compression_enabled
        self._user_turn_count = 0

        # Cumulative token usage for the session. The counters and reported cost
        # live on a dedicated TokenAccounter collaborator; the former public
        # attribute names are exposed via delegating @property shims below.
        self._tokens = TokenAccounter()
        self._usage_summary_logged = False
        self._current_run_api_calls = 0

        if not self.quiet_mode:
            if compression_enabled:
                print(
                    f"📊 Context limit: {self.context_compressor.context_length:,} tokens "
                    f"(compress at {self.context_compressor.threshold_description()}, "
                    f"reserve {self.context_compressor.reserved_output_tokens:,} for output)"
                )
            else:
                print(
                    f"📊 Context limit: {self.context_compressor.context_length:,} tokens (auto-compression disabled)"
                )

    # ------------------------------------------------------------------
    # Token/cost accounting delegation
    #
    # The cumulative counters and reported cost live on ``self._tokens``
    # (TokenAccounter). These thin getter/setter properties preserve the
    # original public attribute names so external code and tests that read or
    # mutate (e.g. ``agent.session_prompt_tokens += n``) keep working unchanged.
    # ------------------------------------------------------------------
    @property
    def session_prompt_tokens(self) -> int:
        return self._tokens.session_prompt_tokens

    @session_prompt_tokens.setter
    def session_prompt_tokens(self, value: int) -> None:
        self._tokens.session_prompt_tokens = value

    @property
    def session_completion_tokens(self) -> int:
        return self._tokens.session_completion_tokens

    @session_completion_tokens.setter
    def session_completion_tokens(self, value: int) -> None:
        self._tokens.session_completion_tokens = value

    @property
    def session_total_tokens(self) -> int:
        return self._tokens.session_total_tokens

    @session_total_tokens.setter
    def session_total_tokens(self, value: int) -> None:
        self._tokens.session_total_tokens = value

    @property
    def session_api_calls(self) -> int:
        return self._tokens.session_api_calls

    @session_api_calls.setter
    def session_api_calls(self, value: int) -> None:
        self._tokens.session_api_calls = value

    @property
    def session_reported_cost_usd(self) -> float | None:
        return self._tokens.session_reported_cost_usd

    @session_reported_cost_usd.setter
    def session_reported_cost_usd(self, value: float | None) -> None:
        self._tokens.session_reported_cost_usd = value

    @property
    def _turn_start_prompt_tokens(self) -> int:
        return self._tokens._turn_start_prompt_tokens

    @_turn_start_prompt_tokens.setter
    def _turn_start_prompt_tokens(self, value: int) -> None:
        self._tokens._turn_start_prompt_tokens = value

    @property
    def _turn_start_completion_tokens(self) -> int:
        return self._tokens._turn_start_completion_tokens

    @_turn_start_completion_tokens.setter
    def _turn_start_completion_tokens(self, value: int) -> None:
        self._tokens._turn_start_completion_tokens = value

    @property
    def _turn_start_total_tokens(self) -> int:
        return self._tokens._turn_start_total_tokens

    @_turn_start_total_tokens.setter
    def _turn_start_total_tokens(self, value: int) -> None:
        self._tokens._turn_start_total_tokens = value

    @property
    def _turn_start_api_calls(self) -> int:
        return self._tokens._turn_start_api_calls

    @_turn_start_api_calls.setter
    def _turn_start_api_calls(self, value: int) -> None:
        self._tokens._turn_start_api_calls = value

    # -- interrupt state (delegated to self._interrupts) -------------------
    # The requested flag, message and child registry live on the
    # InterruptController. These shims preserve the exact attribute API the
    # codebase and tests use: reads of ``_interrupt_requested`` return a real
    # bool; ``= True`` (truthy) / ``= False``/``None`` (falsy) toggle just the
    # Event (no message change, no global signal — those belong to
    # interrupt()/clear_interrupt()); ``_interrupt_message`` reads/writes the
    # stored message; ``_active_children`` returns the live list so
    # append/remove/len/iteration/indexing operate on it directly.

    @property
    def _interrupt_requested(self) -> bool:
        return _resolve_interrupt_controller(self).is_requested()

    @_interrupt_requested.setter
    def _interrupt_requested(self, value: Any) -> None:
        _resolve_interrupt_controller(self).set_requested(bool(value))

    @property
    def _interrupt_message(self) -> Any:
        return _resolve_interrupt_controller(self).message

    @_interrupt_message.setter
    def _interrupt_message(self, value: Any) -> None:
        _resolve_interrupt_controller(self).message = value

    @property
    def _active_children(self) -> list:
        return _resolve_interrupt_controller(self).children

    @_active_children.setter
    def _active_children(self, value: list) -> None:
        _resolve_interrupt_controller(self).children = value

    # -- cached system prompt (delegated to self._prompt_manager_obj) -------
    # The PromptManager owns the per-session system-prompt cache. These shims
    # preserve the exact attribute API the codebase and tests use: many tests do
    # ``agent._cached_system_prompt = "..."`` and run_conversation reads/writes
    # it directly. Routing both through the manager keeps a single source of
    # truth for the cache while preserving its lifetime/invalidation semantics.

    @property
    def _cached_system_prompt(self) -> str | None:
        return _resolve_prompt_manager(self).cached

    @_cached_system_prompt.setter
    def _cached_system_prompt(self, value: str | None) -> None:
        _resolve_prompt_manager(self).cached = value

    def _vprint(self, *args, force: bool = False, **kwargs):
        """Verbose print — suppressed when streaming TTS is active.

        Pass ``force=True`` for error/warning messages that should always be
        shown even during streaming TTS playback.
        """
        if not force and getattr(self, "_stream_callback", None) is not None:
            return
        print(*args, **kwargs)

    def _max_tokens_param(self, value: int) -> dict:
        """Return the correct max tokens kwarg for the current provider.

        OpenAI's newer models (gpt-4o, o-series, gpt-5+) require
        'max_completion_tokens'. OpenRouter, local models, and older
        OpenAI models use 'max_tokens'.
        """
        _is_direct_openai = (
            "api.openai.com" in self.base_url.lower() and "openrouter" not in self.base_url.lower()
        )
        if _is_direct_openai:
            return {"max_completion_tokens": value}
        return {"max_tokens": value}

    def _has_content_after_think_block(self, content: str) -> bool:
        """Thin wrapper delegating to ``ReasoningProcessor.has_content_after_think_block``.

        Kept on AIAgent so callers and tests that call it on the agent instance
        keep working unchanged.
        """
        return ReasoningProcessor.has_content_after_think_block(content)

    def _strip_think_blocks(self, content: str) -> str:
        """Thin wrapper delegating to ``ReasoningProcessor.strip_think_blocks``."""
        return ReasoningProcessor.strip_think_blocks(content)

    def _looks_like_codex_intermediate_ack(
        self,
        user_message: str,
        assistant_content: str,
        messages: list[dict[str, Any]],
    ) -> bool:
        """Detect a planning/ack message that should continue instead of ending the turn."""
        if any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in messages):
            return False

        assistant_text = self._strip_think_blocks(assistant_content or "").strip().lower()
        if not assistant_text:
            return False
        if len(assistant_text) > 1200:
            return False

        has_future_ack = bool(
            re.search(
                r"\b(i['’]ll|i will|let me|i can do that|i can help with that)\b", assistant_text
            )
        )
        if not has_future_ack:
            return False

        action_markers = (
            "look into",
            "look at",
            "inspect",
            "scan",
            "check",
            "analyz",
            "review",
            "explore",
            "read",
            "open",
            "run",
            "test",
            "fix",
            "debug",
            "search",
            "find",
            "walkthrough",
            "report back",
            "summarize",
        )
        workspace_markers = (
            "directory",
            "current directory",
            "current dir",
            "cwd",
            "repo",
            "repository",
            "codebase",
            "project",
            "folder",
            "filesystem",
            "file tree",
            "files",
            "path",
        )

        user_text = (user_message or "").strip().lower()
        user_targets_workspace = (
            any(marker in user_text for marker in workspace_markers)
            or "~/" in user_text
            or "/" in user_text
        )
        assistant_mentions_action = any(marker in assistant_text for marker in action_markers)
        assistant_targets_workspace = any(marker in assistant_text for marker in workspace_markers)
        return (user_targets_workspace or assistant_targets_workspace) and assistant_mentions_action

    def _extract_reasoning(self, assistant_message) -> str | None:
        """Thin wrapper delegating to ``ReasoningProcessor.extract_reasoning``.

        Kept on AIAgent so ResponseNormalizer.build_assistant_message (which calls
        ``agent._extract_reasoning``) and tests that call it on the agent instance
        keep working unchanged.
        """
        return ReasoningProcessor.extract_reasoning(assistant_message)

    def _cleanup_task_resources(self, task_id: str) -> None:
        """Clean up task-local runtime resources for a given task."""
        try:
            cleanup_vm(task_id)
        except Exception as e:
            if self.verbose_logging:
                logging.warning(f"Failed to cleanup VM for task {task_id}: {e}")
        try:
            from tools.implementations.terminal_tool import clear_task_env_overrides

            clear_task_env_overrides(task_id)
        except Exception as e:
            if self.verbose_logging:
                logging.warning(f"Failed to clear env overrides for task {task_id}: {e}")
        _cleanup_optional_browser_state(task_id)

    def _apply_persist_user_message_override(self, messages: list[dict]) -> None:
        """Thin delegating wrapper to ``ConversationManager.apply_persist_user_message_override``."""
        return _resolve_conversation_manager(self).apply_persist_user_message_override(messages)

    def _persist_session(self, messages: list[dict], conversation_history: list[dict] = None):
        """Thin delegating wrapper to ``ConversationManager.persist_session``."""
        return _resolve_conversation_manager(self).persist_session(messages, conversation_history)

    def _flush_messages_to_session_db(
        self, messages: list[dict], conversation_history: list[dict] = None
    ):
        """Thin delegating wrapper to ``ConversationManager.flush_messages_to_session_db``."""
        return _resolve_conversation_manager(self).flush_messages_to_session_db(
            messages, conversation_history
        )

    def _get_messages_up_to_last_assistant(self, messages: list[dict]) -> list[dict]:
        """
        Get messages up to (but not including) the last assistant turn.

        This is used when we need to "roll back" to the last successful point
        in the conversation, typically when the final assistant message is
        incomplete or malformed.

        Args:
            messages: Full message list

        Returns:
            Messages up to the last complete assistant turn (ending with user/tool message)
        """
        if not messages:
            return []

        # Find the index of the last assistant message
        last_assistant_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_assistant_idx = i
                break

        if last_assistant_idx is None:
            # No assistant message found, return all messages
            return messages.copy()

        # Return everything up to (not including) the last assistant message
        return messages[:last_assistant_idx]

    def _format_tools_for_system_message(self) -> str:
        """Thin delegating wrapper to ``ConversationManager.format_tools_for_system_message``."""
        return _resolve_conversation_manager(self).format_tools_for_system_message()

    def _convert_to_trajectory_format(
        self, messages: list[dict[str, Any]], user_query: str, completed: bool
    ) -> list[dict[str, Any]]:
        """Thin delegating wrapper to ``ConversationManager.convert_to_trajectory_format``."""
        return _resolve_conversation_manager(self).convert_to_trajectory_format(
            messages, user_query, completed
        )

    def _save_trajectory(self, messages: list[dict[str, Any]], user_query: str, completed: bool):
        """Thin delegating wrapper to ``ConversationManager.save_trajectory``."""
        return _resolve_conversation_manager(self).save_trajectory(messages, user_query, completed)

    def _mask_api_key_for_logs(self, key: str | None) -> str | None:
        """Return a fixed marker without retaining credential fragments."""
        if not key:
            return None
        return "[REDACTED]"

    def _dump_api_request_debug(
        self,
        api_kwargs: dict[str, Any],
        *,
        reason: str,
        error: Exception | None = None,
    ) -> Path | None:
        """
        Dump a debug-friendly HTTP request record for chat.completions.create().

        Captures the request body from api_kwargs (excluding transport-only keys
        like timeout). Intended for debugging provider-side 4xx failures where
        retries are not useful.
        """
        try:
            body = copy.deepcopy(api_kwargs)
            body.pop("timeout", None)
            body = {k: v for k, v in body.items() if v is not None}

            api_key = None
            try:
                api_key = getattr(self.client, "api_key", None)
            except Exception as e:
                logger.debug("Could not extract API key for debug dump: %s", e)

            dump_payload: dict[str, Any] = {
                "timestamp": datetime.now().isoformat(),
                "session_id": self.session_id,
                "reason": reason,
                "request": {
                    "method": "POST",
                    "url": f"{self.base_url.rstrip('/')}/chat/completions",
                    "headers": {
                        "Authorization": f"Bearer {self._mask_api_key_for_logs(api_key)}",
                        "Content-Type": "application/json",
                    },
                    "body": body,
                },
            }

            if error is not None:
                error_info: dict[str, Any] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                for attr_name in ("status_code", "request_id", "code", "param", "type"):
                    attr_value = getattr(error, attr_name, None)
                    if attr_value is not None:
                        error_info[attr_name] = attr_value

                body_attr = getattr(error, "body", None)
                if body_attr is not None:
                    error_info["body"] = body_attr

                response_obj = getattr(error, "response", None)
                if response_obj is not None:
                    try:
                        error_info["response_status"] = getattr(response_obj, "status_code", None)
                        error_info["response_text"] = response_obj.text
                    except Exception as e:
                        logger.debug("Could not extract error response details: %s", e)

                dump_payload["error"] = error_info

            exact_secrets = (str(api_key),) if api_key else ()
            dump_payload = redact_sensitive_value(
                dump_payload,
                exact_secrets=exact_secrets,
            )

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            dump_file = self.logs_dir / f"request_dump_{self.session_id}_{timestamp}.json"
            dump_file.write_text(
                json.dumps(dump_payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

            self._vprint(f"{self.log_prefix}🧾 Request debug dump written to: {dump_file}")

            if os.getenv("LEANFLOW_DUMP_REQUEST_STDOUT", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }:
                print(json.dumps(dump_payload, ensure_ascii=False, indent=2, default=str))

            return dump_file
        except Exception as dump_error:
            if self.verbose_logging:
                logging.warning(f"Failed to dump API request debug payload: {dump_error}")
            return None

    @staticmethod
    def _clean_session_content(content: str) -> str:
        """Convert REASONING_SCRATCHPAD to think tags and clean up whitespace.

        Thin wrapper around ``conversation_manager.clean_session_content``; kept
        as a staticmethod because tests call it on the class.
        """
        return _clean_session_content(content)

    def _save_session_log(self, messages: list[dict[str, Any]] = None):
        """Thin delegating wrapper to ``ConversationManager.save_session_log``."""
        return _resolve_conversation_manager(self).save_session_log(messages)

    def interrupt(self, message: str = None) -> None:
        """
        Request the agent to interrupt its current tool-calling loop.

        Call this from another thread (e.g., input handler, message receiver)
        to gracefully stop the agent and process a new message.

        Also signals long-running tool executions (e.g. terminal commands)
        to terminate early, so the agent can respond immediately.

        Args:
            message: Optional new message that triggered the interrupt.
                     If provided, the agent will include this in its response context.

        Example (CLI):
            # In a separate input thread:
            if user_typed_something:
                agent.interrupt(user_input)

        Example (Messaging):
            # When new message arrives for active session:
            if session_has_running_agent:
                running_agent.interrupt(new_message.text)
        """
        controller = _resolve_interrupt_controller(self)
        # Set the requested event + message and signal all tools to abort any
        # in-flight operations immediately (the global signal is reached through
        # run_agent._set_interrupt, honoring tests that patch it).
        controller.request(message)
        suppress_interrupt_log = bool(getattr(self, "_suppress_next_interrupt_log", False))
        # Propagate interrupt to any running child agents (subagent delegation)
        controller.propagate(message)
        if not self.quiet_mode and not suppress_interrupt_log:
            print(
                "\n⚡ Interrupt requested"
                + (
                    f": '{message[:40]}...'"
                    if message and len(message) > 40
                    else f": '{message}'" if message else ""
                )
            )

    def clear_interrupt(self) -> None:
        """Clear any pending interrupt request and the global tool interrupt signal."""
        _resolve_interrupt_controller(self).clear()
        self._suppress_next_interrupt_log = False

    def register_child(self, child: "AIAgent") -> None:
        """Register a running child agent for interrupt propagation (thread-safe)."""
        _resolve_interrupt_controller(self).register_child(child)

    def unregister_child(self, child: "AIAgent") -> None:
        """Unregister a child agent from interrupt propagation (thread-safe)."""
        _resolve_interrupt_controller(self).unregister_child(child)

    def _hydrate_todo_store(self, history: list[dict[str, Any]]) -> None:
        """
        Recover todo state from conversation history.

        The gateway creates a fresh AIAgent per message, so the in-memory
        TodoStore is empty. We scan the history for the most recent todo
        tool response and replay it to reconstruct the state.
        """
        # Walk history backwards to find the most recent todo tool response
        last_todo_response = None
        for msg in reversed(history):
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            # Quick check: todo responses contain "todos" key
            if '"todos"' not in content:
                continue
            try:
                data = json.loads(content)
                if "todos" in data and isinstance(data["todos"], list):
                    last_todo_response = data["todos"]
                    break
            except (json.JSONDecodeError, TypeError):
                continue

        if last_todo_response:
            # Replay the items into the store (replace mode)
            self._todo_store.write(last_todo_response, merge=False)
            if not self.quiet_mode:
                self._vprint(
                    f"{self.log_prefix}📋 Restored {len(last_todo_response)} todo item(s) from history"
                )
        _set_interrupt(False)

    @property
    def is_interrupted(self) -> bool:
        """Check if an interrupt has been requested."""
        return self._interrupt_requested

    def _build_system_prompt(self, system_message: str = None) -> str:
        """Thin wrapper delegating to ``PromptManager.build_system_prompt``.

        Kept on AIAgent so tests that call ``agent._build_system_prompt(...)`` and
        ``patch.object(AIAgent, '_build_system_prompt', ...)`` keep working, and so
        the manager's continuation alignment can route its build step back through
        this wrapper.
        """
        return _resolve_prompt_manager(self).build_system_prompt(system_message)

    def _repair_tool_call(self, tool_name: str) -> str | None:
        """Attempt to repair a mismatched tool name before aborting.

        1. Try lowercase
        2. Try normalized (lowercase + hyphens/spaces -> underscores)
        3. Try fuzzy match (difflib, cutoff=0.7)

        Returns the repaired name if found in valid_tool_names, else None.
        """
        from difflib import get_close_matches

        # 1. Lowercase
        lowered = tool_name.lower()
        if lowered in self.valid_tool_names:
            return lowered

        # 2. Normalize
        normalized = lowered.replace("-", "_").replace(" ", "_")
        if normalized in self.valid_tool_names:
            return normalized

        # 3. Fuzzy match
        matches = get_close_matches(lowered, self.valid_tool_names, n=1, cutoff=0.7)
        if matches:
            return matches[0]

        return None

    def _invalidate_system_prompt(self):
        """Thin wrapper delegating to ``PromptManager.invalidate``.

        Kept on AIAgent so callers (post-compression) and tests that call it on
        the agent keep working. Resets the cache and reloads memory from disk.
        """
        _resolve_prompt_manager(self).invalidate()

    def _responses_tools(
        self, tools: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]] | None:
        """Convert chat-completions tool schemas to Responses function-tool schemas."""
        source_tools = tools if tools is not None else self.tools
        if not source_tools:
            return None

        converted: list[dict[str, Any]] = []
        for item in source_tools:
            fn = item.get("function", {}) if isinstance(item, dict) else {}
            name = fn.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            converted.append(
                {
                    "type": "function",
                    "name": name,
                    "description": fn.get("description", ""),
                    "strict": False,
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return converted or None

    @staticmethod
    def _split_responses_tool_id(raw_id: Any) -> tuple[str | None, str | None]:
        """Split a stored tool id into (call_id, response_item_id)."""
        if not isinstance(raw_id, str):
            return None, None
        value = raw_id.strip()
        if not value:
            return None, None
        if "|" in value:
            call_id, response_item_id = value.split("|", 1)
            call_id = call_id.strip() or None
            response_item_id = response_item_id.strip() or None
            return call_id, response_item_id
        if value.startswith("fc_"):
            return None, value
        return value, None

    def _derive_responses_function_call_id(
        self,
        call_id: str,
        response_item_id: str | None = None,
    ) -> str:
        """Build a valid Responses `function_call.id` (must start with `fc_`)."""
        if isinstance(response_item_id, str):
            candidate = response_item_id.strip()
            if candidate.startswith("fc_"):
                return candidate

        source = (call_id or "").strip()
        if source.startswith("fc_"):
            return source
        if source.startswith("call_") and len(source) > len("call_"):
            return f"fc_{source[len('call_') :]}"

        sanitized = re.sub(r"[^A-Za-z0-9_-]", "", source)
        if sanitized.startswith("fc_"):
            return sanitized
        if sanitized.startswith("call_") and len(sanitized) > len("call_"):
            return f"fc_{sanitized[len('call_') :]}"
        if sanitized:
            return f"fc_{sanitized[:48]}"

        seed = source or str(response_item_id or "") or uuid.uuid4().hex
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]
        return f"fc_{digest}"

    def _chat_messages_to_responses_input(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert internal chat-style messages to Responses input items."""
        items: list[dict[str, Any]] = []

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "system":
                continue

            if role in {"user", "assistant"}:
                content = msg.get("content", "")
                content_text = str(content) if content is not None else ""

                if role == "assistant":
                    # Replay encrypted reasoning items from previous turns
                    # so the API can maintain coherent reasoning chains.
                    codex_reasoning = msg.get("codex_reasoning_items")
                    if isinstance(codex_reasoning, list):
                        for ri in codex_reasoning:
                            if isinstance(ri, dict) and ri.get("encrypted_content"):
                                items.append(ri)

                    if content_text.strip():
                        items.append({"role": "assistant", "content": content_text})

                    tool_calls = msg.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            if not isinstance(tc, dict):
                                continue
                            fn = tc.get("function", {})
                            fn_name = fn.get("name")
                            if not isinstance(fn_name, str) or not fn_name.strip():
                                continue

                            embedded_call_id, embedded_response_item_id = (
                                self._split_responses_tool_id(tc.get("id"))
                            )
                            call_id = tc.get("call_id")
                            if not isinstance(call_id, str) or not call_id.strip():
                                call_id = embedded_call_id
                            if not isinstance(call_id, str) or not call_id.strip():
                                if (
                                    isinstance(embedded_response_item_id, str)
                                    and embedded_response_item_id.startswith("fc_")
                                    and len(embedded_response_item_id) > len("fc_")
                                ):
                                    call_id = f"call_{embedded_response_item_id[len('fc_') :]}"
                                else:
                                    call_id = f"call_{uuid.uuid4().hex[:12]}"
                            call_id = call_id.strip()

                            arguments = fn.get("arguments", "{}")
                            if isinstance(arguments, dict):
                                arguments = json.dumps(arguments, ensure_ascii=False)
                            elif not isinstance(arguments, str):
                                arguments = str(arguments)
                            arguments = arguments.strip() or "{}"

                            items.append(
                                {
                                    "type": "function_call",
                                    "call_id": call_id,
                                    "name": fn_name,
                                    "arguments": arguments,
                                }
                            )
                    continue

                items.append({"role": role, "content": content_text})
                continue

            if role == "tool":
                raw_tool_call_id = msg.get("tool_call_id")
                call_id, _ = self._split_responses_tool_id(raw_tool_call_id)
                if not isinstance(call_id, str) or not call_id.strip():
                    if isinstance(raw_tool_call_id, str) and raw_tool_call_id.strip():
                        call_id = raw_tool_call_id.strip()
                if not isinstance(call_id, str) or not call_id.strip():
                    continue
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(msg.get("content", "") or ""),
                    }
                )

        return items

    def _preflight_codex_input_items(self, raw_items: Any) -> list[dict[str, Any]]:
        """Validate and normalize input items for Codex Responses API. Accepts function_calls, function_call_outputs, reasoning items, and user/assistant messages, normalizing all argument strings to JSON and enforcing required fields (call_id, name). Raises ValueError on invalid structure or missing required fields."""
        if not isinstance(raw_items, list):
            raise ValueError("Codex Responses input must be a list of input items.")

        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError(f"Codex Responses input[{idx}] must be an object.")

            item_type = item.get("type")
            if item_type == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                if not isinstance(call_id, str) or not call_id.strip():
                    raise ValueError(
                        f"Codex Responses input[{idx}] function_call is missing call_id."
                    )
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(f"Codex Responses input[{idx}] function_call is missing name.")

                arguments = item.get("arguments", "{}")
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                elif not isinstance(arguments, str):
                    arguments = str(arguments)
                arguments = arguments.strip() or "{}"

                normalized.append(
                    {
                        "type": "function_call",
                        "call_id": call_id.strip(),
                        "name": name.strip(),
                        "arguments": arguments,
                    }
                )
                continue

            if item_type == "function_call_output":
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id.strip():
                    raise ValueError(
                        f"Codex Responses input[{idx}] function_call_output is missing call_id."
                    )
                output = item.get("output", "")
                if output is None:
                    output = ""
                if not isinstance(output, str):
                    output = str(output)

                normalized.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id.strip(),
                        "output": output,
                    }
                )
                continue

            if item_type == "reasoning":
                encrypted = item.get("encrypted_content")
                if isinstance(encrypted, str) and encrypted:
                    reasoning_item = {"type": "reasoning", "encrypted_content": encrypted}
                    item_id = item.get("id")
                    if isinstance(item_id, str) and item_id:
                        reasoning_item["id"] = item_id
                    summary = item.get("summary")
                    if isinstance(summary, list):
                        reasoning_item["summary"] = summary
                    else:
                        reasoning_item["summary"] = []
                    normalized.append(reasoning_item)
                continue

            role = item.get("role")
            if role in {"user", "assistant"}:
                content = item.get("content", "")
                if content is None:
                    content = ""
                if not isinstance(content, str):
                    content = str(content)

                normalized.append({"role": role, "content": content})
                continue

            raise ValueError(
                f"Codex Responses input[{idx}] has unsupported item shape (type={item_type!r}, role={role!r})."
            )

        return normalized

    def _preflight_codex_api_kwargs(
        self,
        api_kwargs: Any,
        *,
        allow_stream: bool = False,
    ) -> dict[str, Any]:
        """Validate and normalize a Codex Responses API request dict. Checks required fields (model, instructions, input), normalizes all string fields, validates and normalizes tools list, enforces store=false contract, and passes through optional fields (reasoning, temperature, max_output_tokens, tool_choice, etc.). Raises ValueError on invalid structure or unsupported fields."""
        if not isinstance(api_kwargs, dict):
            raise ValueError("Codex Responses request must be a dict.")

        required = {"model", "instructions", "input"}
        missing = [key for key in required if key not in api_kwargs]
        if missing:
            raise ValueError(
                f"Codex Responses request missing required field(s): {', '.join(sorted(missing))}."
            )

        model = api_kwargs.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Codex Responses request 'model' must be a non-empty string.")
        model = model.strip()

        instructions = api_kwargs.get("instructions")
        if instructions is None:
            instructions = ""
        if not isinstance(instructions, str):
            instructions = str(instructions)
        instructions = instructions.strip() or DEFAULT_AGENT_IDENTITY

        normalized_input = self._preflight_codex_input_items(api_kwargs.get("input"))

        tools = api_kwargs.get("tools")
        normalized_tools = None
        if tools is not None:
            if not isinstance(tools, list):
                raise ValueError("Codex Responses request 'tools' must be a list when provided.")
            normalized_tools = []
            for idx, tool in enumerate(tools):
                if not isinstance(tool, dict):
                    raise ValueError(f"Codex Responses tools[{idx}] must be an object.")
                if tool.get("type") != "function":
                    raise ValueError(
                        f"Codex Responses tools[{idx}] has unsupported type {tool.get('type')!r}."
                    )

                name = tool.get("name")
                parameters = tool.get("parameters")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(f"Codex Responses tools[{idx}] is missing a valid name.")
                if not isinstance(parameters, dict):
                    raise ValueError(f"Codex Responses tools[{idx}] is missing valid parameters.")

                description = tool.get("description", "")
                if description is None:
                    description = ""
                if not isinstance(description, str):
                    description = str(description)

                strict = tool.get("strict", False)
                if not isinstance(strict, bool):
                    strict = bool(strict)

                normalized_tools.append(
                    {
                        "type": "function",
                        "name": name.strip(),
                        "description": description,
                        "strict": strict,
                        "parameters": parameters,
                    }
                )

        store = api_kwargs.get("store", False)
        if store is not False:
            raise ValueError("Codex Responses contract requires 'store' to be false.")

        allowed_keys = {
            "model",
            "instructions",
            "input",
            "tools",
            "store",
            "reasoning",
            "include",
            "max_output_tokens",
            "temperature",
            "tool_choice",
            "parallel_tool_calls",
            "prompt_cache_key",
        }
        normalized: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": normalized_input,
            "tools": normalized_tools,
            "store": False,
        }

        # Pass through reasoning config
        reasoning = api_kwargs.get("reasoning")
        if isinstance(reasoning, dict):
            normalized["reasoning"] = reasoning
        include = api_kwargs.get("include")
        if isinstance(include, list):
            normalized["include"] = include

        # Pass through max_output_tokens and temperature
        max_output_tokens = api_kwargs.get("max_output_tokens")
        if isinstance(max_output_tokens, (int, float)) and max_output_tokens > 0:
            normalized["max_output_tokens"] = int(max_output_tokens)
        temperature = api_kwargs.get("temperature")
        if isinstance(temperature, (int, float)):
            normalized["temperature"] = float(temperature)

        # Pass through tool_choice, parallel_tool_calls, prompt_cache_key
        for passthrough_key in ("tool_choice", "parallel_tool_calls", "prompt_cache_key"):
            val = api_kwargs.get(passthrough_key)
            if val is not None:
                normalized[passthrough_key] = val

        if allow_stream:
            stream = api_kwargs.get("stream")
            if stream is not None and stream is not True:
                raise ValueError("Codex Responses 'stream' must be true when set.")
            if stream is True:
                normalized["stream"] = True
            allowed_keys.add("stream")
        elif "stream" in api_kwargs:
            raise ValueError(
                "Codex Responses stream flag is only allowed in fallback streaming requests."
            )

        unexpected = sorted(key for key in api_kwargs.keys() if key not in allowed_keys)
        if unexpected:
            raise ValueError(
                f"Codex Responses request has unsupported field(s): {', '.join(unexpected)}."
            )

        return normalized

    def _extract_responses_message_text(self, item: Any) -> str:
        """Extract assistant text from a Responses message output item.

        Thin wrapper delegating to ResponseNormalizer (kept on AIAgent so any
        test that calls it on the agent still resolves).
        """
        return _resolve_response_normalizer(self).extract_responses_message_text(item)

    def _extract_responses_reasoning_text(self, item: Any) -> str:
        """Extract a compact reasoning text from a Responses reasoning item.

        Thin wrapper delegating to ResponseNormalizer.
        """
        return _resolve_response_normalizer(self).extract_responses_reasoning_text(item)

    def _normalize_codex_response(self, response: Any) -> tuple[Any, str]:
        """Normalize a Responses API object to an assistant_message-like object.

        Thin wrapper delegating to ResponseNormalizer (the heavy logic lives in
        agent/response_normalizer.py; this wrapper stays on AIAgent so tests that
        call ``agent._normalize_codex_response(...)`` keep working).
        """
        return _resolve_response_normalizer(self).normalize_codex_response(response)

    def _thread_identity(self) -> str:
        thread = threading.current_thread()
        return f"{thread.name}:{thread.ident}"

    def _client_log_context(self) -> str:
        provider = getattr(self, "provider", "unknown")
        base_url = getattr(self, "base_url", "unknown")
        model = getattr(self, "model", "unknown")
        return (
            f"thread={self._thread_identity()} provider={provider} "
            f"base_url={base_url} model={model}"
        )

    def _openai_client_lock(self) -> threading.RLock:
        lock = getattr(self, "_client_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._client_lock = lock
        return lock

    def _provider_client_factory(self) -> ProviderClientFactory:
        # Lazily materialize the factory so agents built via __new__ (e.g. some
        # lifecycle tests that bypass __init__) still get a working collaborator.
        factory = getattr(self, "_provider_clients", None)
        if factory is None:
            factory = ProviderClientFactory()
            self._provider_clients = factory
        return factory

    @staticmethod
    def _is_openai_client_closed(client: Any) -> bool:
        return ProviderClientFactory.is_openai_client_closed(client)

    def _create_openai_client(self, client_kwargs: dict, *, reason: str, shared: bool) -> Any:
        return self._provider_client_factory().create_openai_client(
            client_kwargs,
            reason=reason,
            shared=shared,
            log_context=self._client_log_context(),
        )

    def _close_openai_client(self, client: Any, *, reason: str, shared: bool) -> None:
        self._provider_client_factory().close_openai_client(
            client,
            reason=reason,
            shared=shared,
            log_context=self._client_log_context(),
        )

    def _replace_primary_openai_client(self, *, reason: str) -> bool:
        with self._openai_client_lock():
            old_client = getattr(self, "client", None)
            try:
                new_client = self._create_openai_client(
                    self._client_kwargs, reason=reason, shared=True
                )
            except Exception as exc:
                logger.warning(
                    "Failed to rebuild shared OpenAI client (%s) %s error=%s",
                    reason,
                    self._client_log_context(),
                    exc,
                )
                return False
            self.client = new_client
        self._close_openai_client(old_client, reason=f"replace:{reason}", shared=True)
        return True

    def _ensure_primary_openai_client(self, *, reason: str) -> Any:
        with self._openai_client_lock():
            client = getattr(self, "client", None)
            if client is not None and not self._is_openai_client_closed(client):
                return client

        logger.warning(
            "Detected closed shared OpenAI client; recreating before use (%s) %s",
            reason,
            self._client_log_context(),
        )
        if not self._replace_primary_openai_client(reason=f"recreate_closed:{reason}"):
            raise RuntimeError("Failed to recreate closed OpenAI client")
        with self._openai_client_lock():
            return self.client

    @staticmethod
    def _responses_stream_event_type(event: Any) -> str:
        event_type = getattr(event, "type", None)
        if not event_type and isinstance(event, dict):
            event_type = event.get("type")
        return str(event_type or "")

    @staticmethod
    def _responses_stream_event_field(event: Any, name: str) -> Any:
        value = getattr(event, name, None)
        if value is None and isinstance(event, dict):
            value = event.get(name)
        return value

    def _collect_responses_stream_output_item(
        self, event: Any, collected_items: dict[int, Any]
    ) -> None:
        if self._responses_stream_event_type(event) != "response.output_item.done":
            return
        item = self._responses_stream_event_field(event, "item")
        if item is None:
            return
        raw_index = self._responses_stream_event_field(event, "output_index")
        try:
            output_index = int(raw_index)
        except (TypeError, ValueError):
            output_index = len(collected_items)
        collected_items[output_index] = item

    def _repair_empty_responses_stream_output(
        self, response: Any, collected_items: dict[int, Any]
    ) -> Any:
        if response is None or not collected_items:
            return response
        output = getattr(response, "output", None)
        if isinstance(output, list) and output:
            return response

        repaired_output = [
            collected_items[index]
            for index in sorted(collected_items)
            if collected_items.get(index) is not None
        ]
        if not repaired_output:
            return response

        model_copy = getattr(response, "model_copy", None)
        if callable(model_copy):
            try:
                return model_copy(update={"output": repaired_output})
            except Exception:
                pass
        copy_method = getattr(response, "copy", None)
        if callable(copy_method):
            try:
                return copy_method(update={"output": repaired_output})
            except Exception:
                pass
        try:
            response.output = repaired_output
        except Exception:
            if isinstance(response, dict):
                response_payload = dict(response)
            else:
                try:
                    response_payload = {
                        key: value
                        for key, value in vars(response).items()
                        if not key.startswith("_")
                    }
                except TypeError:
                    response_payload = {
                        "status": getattr(response, "status", None),
                        "model": getattr(response, "model", None),
                        "usage": getattr(response, "usage", None),
                    }
            return SimpleNamespace(
                **{
                    **response_payload,
                    "output": repaired_output,
                }
            )
        return response

    def _create_request_openai_client(self, *, reason: str) -> Any:
        from unittest.mock import Mock

        primary_client = self._ensure_primary_openai_client(reason=reason)
        if isinstance(primary_client, Mock):
            return primary_client
        with self._openai_client_lock():
            request_kwargs = dict(self._client_kwargs)
        # Codex Responses preflight intentionally strips transport-only request
        # fields. Configure the worker-local client with the same effective
        # deadline so the SDK's shorter default read timeout cannot contradict
        # the timeout advertised by the managed provider heartbeat.
        request_kwargs.setdefault("timeout", self._provider_request_timeout_seconds({}))
        return self._create_openai_client(request_kwargs, reason=reason, shared=False)

    def _close_request_openai_client(self, client: Any, *, reason: str) -> None:
        self._close_openai_client(client, reason=reason, shared=False)

    def _run_codex_stream(self, api_kwargs: dict, client: Any = None):
        """Execute one streaming Responses API request and return the final response."""
        return _resolve_api_caller(self).run_codex_stream(api_kwargs, client=client)

    def _run_codex_create_stream_fallback(self, api_kwargs: dict, client: Any = None):
        """Fallback path for stream completion edge cases on Codex-style Responses backends."""
        return _resolve_api_caller(self).run_codex_create_stream_fallback(api_kwargs, client=client)

    def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:
        if self.api_mode != "codex_responses" or self.provider != "openai-codex":
            return False

        creds = self._provider_client_factory().resolve_codex_credentials(force=force)
        if creds is None:
            return False

        self.api_key = creds["api_key"]
        self.base_url = creds["base_url"]
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url

        if not self._replace_primary_openai_client(reason="codex_credential_refresh"):
            return False

        return True

    def _try_refresh_nous_client_credentials(self, *, force: bool = True) -> bool:
        if self.api_mode != "chat_completions" or self.provider != "nous":
            return False

        creds = self._provider_client_factory().resolve_nous_credentials(force=force)
        if creds is None:
            return False

        self.api_key = creds["api_key"]
        self.base_url = creds["base_url"]
        self._client_kwargs["api_key"] = self.api_key
        self._client_kwargs["base_url"] = self.base_url
        # Nous requests should not inherit OpenRouter-only attribution headers.
        self._client_kwargs.pop("default_headers", None)

        if not self._replace_primary_openai_client(reason="nous_credential_refresh"):
            return False

        return True

    def _try_refresh_anthropic_client_credentials(self) -> bool:
        if self.api_mode != "anthropic_messages" or not hasattr(self, "_anthropic_api_key"):
            return False

        try:
            new_token = self._provider_client_factory().resolve_anthropic_token()
        except Exception as exc:
            logger.debug("Anthropic credential refresh failed: %s", exc)
            return False

        if not isinstance(new_token, str) or not new_token.strip():
            return False
        new_token = new_token.strip()
        if new_token == self._anthropic_api_key:
            return False

        with contextlib.suppress(Exception):
            self._anthropic_client.close()

        try:
            self._anthropic_client = self._provider_client_factory().build_anthropic_client(
                new_token, getattr(self, "_anthropic_base_url", None)
            )
        except Exception as exc:
            logger.warning("Failed to rebuild Anthropic client after credential refresh: %s", exc)
            return False

        self._anthropic_api_key = new_token
        return True

    def _anthropic_messages_create(self, api_kwargs: dict):
        if self.api_mode == "anthropic_messages":
            self._try_refresh_anthropic_client_credentials()
        return self._anthropic_client.messages.create(**api_kwargs)

    def _provider_request_timeout_seconds(self, api_kwargs: dict) -> float:
        return _resolve_api_caller(self).provider_request_timeout_seconds(api_kwargs)

    def _provider_wait_heartbeat_seconds(self) -> float:
        raw_value = os.getenv("LEANFLOW_PROVIDER_WAIT_HEARTBEAT", "30.0")
        try:
            heartbeat_seconds = float(raw_value)
        except (TypeError, ValueError):
            heartbeat_seconds = 30.0
        return max(heartbeat_seconds, 1.0)

    def _abort_inflight_provider_request(self, request_client_holder: dict, *, reason: str) -> None:
        if self.api_mode == "anthropic_messages":
            self._anthropic_client.close()
            self._anthropic_client = self._provider_client_factory().build_anthropic_client(
                self._anthropic_api_key,
                getattr(self, "_anthropic_base_url", None),
            )
            return

        request_client = request_client_holder.get("client")
        if request_client is not None:
            self._close_request_openai_client(request_client, reason=reason)

    def _emit_provider_wait_heartbeat(
        self,
        *,
        elapsed_seconds: float,
        timeout_seconds: float,
        streaming: bool,
    ) -> None:
        mode_label = "streaming" if streaming else "non-streaming"
        message = (
            f"Waiting on provider response ({elapsed_seconds:.0f}s elapsed, "
            f"{timeout_seconds:.0f}s timeout, {mode_label})"
        )
        logger.warning(
            "%s %s",
            message,
            self._client_log_context(),
        )
        self._vprint(f"{self.log_prefix}   ⏳ {message}", force=True)
        _emit_workflow_event(
            "provider-wait",
            message,
            **_workflow_agent_event_details(
                self,
                elapsed_seconds=round(elapsed_seconds, 3),
                timeout_seconds=round(timeout_seconds, 3),
                streaming=streaming,
            ),
        )

    def _interruptible_api_call(self, api_kwargs: dict):
        """
        Run the API call in a background thread so the main conversation loop
        can detect interrupts without waiting for the full HTTP round-trip.

        Each worker thread gets its own OpenAI client instance. Interrupts only
        close that worker-local client, so retries and other requests never
        inherit a closed transport.

        The mediation now lives on the ApiCaller collaborator
        (``agent/api_caller.py``); this is a thin delegating wrapper. The
        interrupt contract is unchanged: when ``api_mode == "anthropic_messages"``
        the abort path rebuilds the native Anthropic client via
        ``build_anthropic_client`` (otherwise it force-closes the worker-local
        OpenAI HTTP connection) before raising ``InterruptedError``.
        """
        return _resolve_api_caller(self).interruptible_api_call(api_kwargs)

    def _streaming_api_call(self, api_kwargs: dict, stream_callback):
        """Streaming variant of _interruptible_api_call for voice TTS pipeline.

        Uses ``stream=True`` and forwards content deltas to *stream_callback*
        in real-time.  Returns a ``SimpleNamespace`` that mimics a normal
        ``ChatCompletion`` so the rest of the agent loop works unchanged.

        This method is separate from ``_interruptible_api_call`` to keep the
        core agent loop untouched for non-voice users.

        The mediation now lives on the ApiCaller collaborator
        (``agent/api_caller.py``); this is a thin delegating wrapper. The
        interrupt contract is unchanged: when ``api_mode == "anthropic_messages"``
        the abort path rebuilds the native Anthropic client via
        ``build_anthropic_client`` before raising ``InterruptedError``.
        """
        return _resolve_api_caller(self).streaming_api_call(api_kwargs, stream_callback)

    # ── Provider fallback ──────────────────────────────────────────────────

    def _try_activate_fallback(self) -> bool:
        """Switch to the configured fallback model/provider.

        Called when the primary model is failing after retries.  Swaps the
        OpenAI client, model slug, and provider in-place so the retry loop
        can continue with the new backend.  One-shot: returns False if
        already activated or not configured.

        Uses the centralized provider router (resolve_provider_client) for
        auth resolution and client construction — no duplicated provider→key
        mappings.
        """
        if self._fallback_activated or not self._fallback_model:
            return False

        fb = self._fallback_model
        fb_provider = (fb.get("provider") or "").strip().lower()
        fb_model = (fb.get("model") or "").strip()
        if not fb_provider or not fb_model:
            return False

        # Use centralized router for client construction.
        # raw_codex=True because the main agent needs direct responses.stream()
        # access for Codex providers.
        try:
            from agent.providers.auxiliary_client import resolve_provider_client

            fb_client, _ = resolve_provider_client(fb_provider, model=fb_model, raw_codex=True)
            if fb_client is None:
                logging.warning("Fallback to %s failed: provider not configured", fb_provider)
                return False

            # Determine api_mode from provider
            fb_api_mode = "chat_completions"
            if fb_provider == "openai-codex":
                fb_api_mode = "codex_responses"
            elif fb_provider == "anthropic":
                fb_api_mode = "anthropic_messages"
            fb_base_url = str(fb_client.base_url)

            old_model = self.model
            self.model = fb_model
            self.provider = fb_provider
            self.base_url = fb_base_url
            self.api_mode = fb_api_mode
            self._fallback_activated = True

            if fb_api_mode == "anthropic_messages":
                # Build native Anthropic client instead of using OpenAI client
                effective_key = (
                    fb_client.api_key
                    or self._provider_client_factory().resolve_anthropic_token()
                    or ""
                )
                self._anthropic_api_key = effective_key
                self._anthropic_base_url = getattr(fb_client, "base_url", None)
                self._anthropic_client = self._provider_client_factory().build_anthropic_client(
                    effective_key, self._anthropic_base_url
                )
                self.client = None
                self._client_kwargs = {}
            else:
                # Swap OpenAI client and config in-place
                self.client = fb_client
                self._client_kwargs = {
                    "api_key": fb_client.api_key,
                    "base_url": fb_base_url,
                }

            self.context_compressor.bind_main_summary_route(
                model=fb_model,
                provider=fb_provider,
                api_mode=fb_api_mode,
                base_url=fb_base_url,
                api_key=str(getattr(fb_client, "api_key", "") or ""),
            )

            # Re-evaluate prompt caching for the new provider/model
            is_native_anthropic = fb_api_mode == "anthropic_messages"
            self._use_prompt_caching = (
                "openrouter" in fb_base_url.lower() and "claude" in fb_model.lower()
            ) or is_native_anthropic

            print(
                f"{self.log_prefix}🔄 Primary model failed — switching to fallback: "
                f"{fb_model} via {fb_provider}"
            )
            logging.info(
                "Fallback activated: %s → %s (%s)",
                old_model,
                fb_model,
                fb_provider,
            )
            return True
        except Exception as e:
            logging.error("Failed to activate fallback model: %s", e)
            return False

    # ── End provider fallback ──────────────────────────────────────────────

    @staticmethod
    def _content_has_image_parts(content: Any) -> bool:
        """Thin wrapper delegating to AnthropicMessagePreparer (agent/anthropic_messages.py)."""
        return content_has_image_parts(content)

    def _preprocess_anthropic_content(self, content: Any, role: str) -> Any:
        """Thin wrapper delegating to AnthropicMessagePreparer (agent/anthropic_messages.py)."""
        return _resolve_anthropic_message_preparer(self).preprocess_anthropic_content(content, role)

    def _prepare_anthropic_messages_for_api(self, api_messages: list) -> list:
        """Thin wrapper delegating to AnthropicMessagePreparer (agent/anthropic_messages.py)."""
        return _resolve_anthropic_message_preparer(self).prepare_anthropic_messages_for_api(
            api_messages
        )

    def _build_api_kwargs(self, api_messages: list) -> dict:
        """Build the keyword arguments dict for the active API mode."""
        return _resolve_api_caller(self).build_api_kwargs(api_messages)

    def _supports_reasoning_extra_body(self) -> bool:
        """Return True when reasoning extra_body is safe to send for this route/model.

        OpenRouter forwards unknown extra_body fields to upstream providers.
        Some providers/routes reject `reasoning` with 400s, so gate it to
        known reasoning-capable model families and direct Nous Portal.
        """
        base_url = (self.base_url or "").lower()
        if "nousresearch" in base_url:
            return True
        if "openrouter" not in base_url:
            return False
        if "api.mistral.ai" in base_url:
            return False

        model = (self.model or "").lower()
        reasoning_model_prefixes = (
            "deepseek/",
            "anthropic/",
            "openai/",
            "x-ai/",
            "google/gemini-2",
            "qwen/qwen3",
        )
        return any(model.startswith(prefix) for prefix in reasoning_model_prefixes)

    def _is_rcp_route(self) -> bool:
        """Return True for EPFL AIaaS / RCP OpenAI-compatible endpoints."""
        base_url = (self.base_url or "").lower()
        return "inference.rcp.epfl.ch" in base_url or "inference-rcp.epfl.ch" in base_url

    def _reasoning_effort_state(self) -> tuple[bool, str]:
        """Resolve whether reasoning is enabled and the requested effort."""
        reasoning_enabled = True
        reasoning_effort = "high"
        if self.reasoning_config and isinstance(self.reasoning_config, dict):
            if self.reasoning_config.get("enabled") is False:
                reasoning_enabled = False
            elif self.reasoning_config.get("mode") == "auto":
                reasoning_effort = "high"
            elif self.reasoning_config.get("effort"):
                requested = str(self.reasoning_config["effort"]).lower()
                reasoning_effort = "high" if requested == "auto" else requested
        return reasoning_enabled, reasoning_effort

    @staticmethod
    def _map_rcp_reasoning_effort(effort: str) -> str:
        """Map LeanFlow effort names onto AIaaS/vLLM-compatible values."""
        normalized = str(effort or "high").lower()
        if normalized in {"low", "medium", "high"}:
            return normalized
        if normalized == "minimal":
            return "low"
        if normalized == "xhigh":
            return "high"
        return "high"

    def _build_assistant_message(self, assistant_message, finish_reason: str) -> dict:
        """Build a normalized assistant message dict from an API response message.

        Thin wrapper delegating to ResponseNormalizer (agent/response_normalizer.py).
        Kept on AIAgent so both the tool-call and final-response paths, and tests
        that call ``agent._build_assistant_message(...)``, keep working unchanged.
        The normalizer reaches back through the agent for ``_extract_reasoning``,
        ``verbose_logging``/``reasoning_callback`` and the shared id helpers.
        """
        return _resolve_response_normalizer(self).build_assistant_message(
            assistant_message, finish_reason
        )

    @staticmethod
    def _text_preview_lines(
        text: Any,
        *,
        max_lines: int = 8,
        max_chars: int = 1600,
    ) -> list[str]:
        """Thin staticmethod wrapper delegating to ``OutputManager``.

        Kept as a staticmethod on AIAgent so tests that call
        ``AIAgent._text_preview_lines(...)`` on the class keep working.
        """
        return OutputManager.text_preview_lines(text, max_lines=max_lines, max_chars=max_chars)

    def _log_conversation_start(self, user_message: str) -> None:
        _resolve_output_manager(self).log_conversation_start(user_message)

    @staticmethod
    def _extract_reported_cost_usd(usage: Any) -> float | None:
        return TokenAccounter.extract_reported_cost_usd(usage)

    def _session_usage_summary(self) -> dict[str, Any]:
        return _resolve_output_manager(self).session_usage_summary()

    def _log_session_usage_summary(self) -> None:
        _resolve_output_manager(self).log_session_usage_summary()

    def _log_token_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        _resolve_output_manager(self).log_token_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _reasoning_context_payload_stats(api_messages: list) -> dict[str, int]:
        """Thin staticmethod wrapper delegating to ``OutputManager``.

        Kept as a staticmethod on AIAgent so tests that call
        ``agent._reasoning_context_payload_stats(...)`` keep working.
        """
        return OutputManager.reasoning_context_payload_stats(api_messages)

    def _log_reasoning_replay_accounting(
        self,
        *,
        api_messages: list,
        approx_tokens: int,
        provider_prompt_tokens: int,
    ) -> None:
        _resolve_output_manager(self).log_reasoning_replay_accounting(
            api_messages=api_messages,
            approx_tokens=approx_tokens,
            provider_prompt_tokens=provider_prompt_tokens,
        )

    @staticmethod
    def _reasoning_preview_lines(
        reasoning_text: str | None,
        *,
        max_lines: int = 8,
        max_chars: int = 1600,
    ) -> list[str]:
        """Thin staticmethod wrapper delegating to ``ReasoningProcessor``.

        Kept as a staticmethod on AIAgent so tests that call
        ``AIAgent._reasoning_preview_lines(...)`` on the class keep working.
        """
        return ReasoningProcessor.reasoning_preview_lines(
            reasoning_text, max_lines=max_lines, max_chars=max_chars
        )

    @staticmethod
    def _sanitize_tool_calls_for_strict_api(api_msg: dict) -> dict:
        """Strip Codex Responses API fields from tool_calls for strict providers.

        Providers like Mistral strictly validate the Chat Completions schema
        and reject unknown fields (call_id, response_item_id) with 422.
        These fields are preserved in the internal message history — this
        method only modifies the outgoing API copy.

        Creates new tool_call dicts rather than mutating in-place, so the
        original messages list retains call_id/response_item_id for Codex
        Responses API compatibility (e.g. if the session falls back to a
        Codex provider later).
        """
        tool_calls = api_msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            return api_msg
        _STRIP_KEYS = {"call_id", "response_item_id"}
        api_msg["tool_calls"] = [
            {k: v for k, v in tc.items() if k not in _STRIP_KEYS} if isinstance(tc, dict) else tc
            for tc in tool_calls
        ]
        return api_msg

    def flush_memories(self, messages: list = None, min_turns: int = None):
        """Give the model one turn to persist memories before context is lost.

        Called before compression, session reset, or CLI exit. Injects a flush
        message, makes one API call, executes any memory tool calls, then
        strips all flush artifacts from the message list.

        Args:
            messages: The current conversation messages. If None, uses
                      self._session_messages (last run_conversation state).
            min_turns: Minimum user turns required to trigger the flush.
                       None = use config value (flush_min_turns).
                       0 = always flush (used for compression).
        """
        if self._memory_flush_min_turns == 0 and min_turns is None:
            return
        if "memory" not in self.valid_tool_names or not self._memory_store:
            return
        effective_min = min_turns if min_turns is not None else self._memory_flush_min_turns
        if self._user_turn_count < effective_min:
            return

        if messages is None:
            messages = getattr(self, "_session_messages", None)
        if not messages or len(messages) < 3:
            return

        flush_content = (
            "[System: The session is being compressed. "
            "Save anything worth remembering — prioritize user preferences, "
            "corrections, and recurring patterns over task-specific details.]"
        )
        _sentinel = f"__flush_{id(self)}_{time.monotonic()}"
        flush_msg = {"role": "user", "content": flush_content, "_flush_sentinel": _sentinel}
        messages.append(flush_msg)

        try:
            # Build API messages for the flush call
            _is_strict_api = "api.mistral.ai" in self.base_url.lower()
            api_messages = []
            for msg in messages:
                api_msg = msg.copy()
                if msg.get("role") == "assistant":
                    reasoning = msg.get("reasoning")
                    if reasoning:
                        api_msg["reasoning_content"] = reasoning
                api_msg.pop("reasoning", None)
                api_msg.pop("finish_reason", None)
                api_msg.pop("_flush_sentinel", None)
                if _is_strict_api:
                    self._sanitize_tool_calls_for_strict_api(api_msg)
                api_messages.append(api_msg)

            if self._cached_system_prompt:
                api_messages = [
                    {"role": "system", "content": self._cached_system_prompt}
                ] + api_messages

            # Make one API call with only the memory tool available
            memory_tool_def = None
            for t in self.tools or []:
                if t.get("function", {}).get("name") == "memory":
                    memory_tool_def = t
                    break

            if not memory_tool_def:
                messages.pop()  # remove flush msg
                return

            # Use auxiliary client for the flush call when available --
            # it's cheaper and avoids Codex Responses API incompatibility.
            from agent.providers.auxiliary_client import call_llm as _call_llm

            _aux_available = True
            try:
                response = _call_llm(
                    task="flush_memories",
                    messages=api_messages,
                    tools=[memory_tool_def],
                    temperature=0.3,
                    max_tokens=5120,
                    timeout=30.0,
                )
            except RuntimeError:
                _aux_available = False
                response = None

            if not _aux_available and self.api_mode == "codex_responses":
                # No auxiliary client -- use the Codex Responses path directly
                codex_kwargs = self._build_api_kwargs(api_messages)
                codex_kwargs["tools"] = self._responses_tools([memory_tool_def])
                codex_kwargs["temperature"] = 0.3
                if "max_output_tokens" in codex_kwargs:
                    codex_kwargs["max_output_tokens"] = 5120
                response = self._run_codex_stream(codex_kwargs)
            elif not _aux_available and self.api_mode == "anthropic_messages":
                # Native Anthropic — use the Anthropic client directly
                from agent.providers.anthropic_adapter import (
                    build_anthropic_kwargs as _build_ant_kwargs,
                )

                ant_kwargs = _build_ant_kwargs(
                    model=self.model,
                    messages=api_messages,
                    tools=[memory_tool_def],
                    max_tokens=5120,
                    reasoning_config=None,
                )
                response = self._anthropic_messages_create(ant_kwargs)
            elif not _aux_available:
                api_kwargs = {
                    "model": self.model,
                    "messages": api_messages,
                    "tools": [memory_tool_def],
                    "temperature": 0.3,
                    **self._max_tokens_param(5120),
                }
                response = self._ensure_primary_openai_client(
                    reason="flush_memories"
                ).chat.completions.create(**api_kwargs, timeout=30.0)

            # Extract tool calls from the response, handling all API formats
            tool_calls = []
            if self.api_mode == "codex_responses" and not _aux_available:
                assistant_msg, _ = self._normalize_codex_response(response)
                if assistant_msg and assistant_msg.tool_calls:
                    tool_calls = assistant_msg.tool_calls
            elif self.api_mode == "anthropic_messages" and not _aux_available:
                from agent.providers.anthropic_adapter import (
                    normalize_anthropic_response as _nar_flush,
                )

                _flush_msg, _ = _nar_flush(response)
                if _flush_msg and _flush_msg.tool_calls:
                    tool_calls = _flush_msg.tool_calls
            elif hasattr(response, "choices") and response.choices:
                assistant_message = response.choices[0].message
                if assistant_message.tool_calls:
                    tool_calls = assistant_message.tool_calls

            for tc in tool_calls:
                if tc.function.name == "memory":
                    try:
                        args = json.loads(tc.function.arguments)
                        flush_target = args.get("target", "memory")
                        from tools.implementations.memory_tool import memory_tool as _memory_tool

                        result = _memory_tool(
                            action=args.get("action"),
                            target=flush_target,
                            content=args.get("content"),
                            old_text=args.get("old_text"),
                            store=self._memory_store,
                        )
                        if not self.quiet_mode:
                            print(f"  🧠 Memory flush: saved to {args.get('target', 'memory')}")
                    except Exception as e:
                        logger.debug("Memory flush tool call failed: %s", e)
        except Exception as e:
            logger.debug("Memory flush API call failed: %s", e)
        finally:
            # Strip flush artifacts: remove everything from the flush message onward.
            # Use sentinel marker instead of identity check for robustness.
            while messages and messages[-1].get("_flush_sentinel") != _sentinel:
                messages.pop()
                if not messages:
                    break
            if messages and messages[-1].get("_flush_sentinel") == _sentinel:
                messages.pop()

    def _compress_context(
        self,
        messages: list,
        system_message: str,
        *,
        approx_tokens: int = None,
        task_id: str = "default",
    ) -> tuple:
        """Thin delegating wrapper to ``CompressionPolicy.compress_context``."""
        return _resolve_compression_policy(self).compress_context(
            messages, system_message, approx_tokens=approx_tokens, task_id=task_id
        )

    def _tool_executor(self) -> "ToolExecutor":
        # Lazily materialize the executor so agents built via __new__ (e.g. some
        # lifecycle tests that bypass __init__) still get a working collaborator.
        # Resolution is delegated to the module-level helper so it works even when
        # ``self`` is a MagicMock "fake agent" (some interrupt tests bind only the
        # strategy methods onto a mock); in that case we still hand back a real
        # ToolExecutor bound to that mock so the dispatch logic runs against it.
        return _resolve_tool_executor(self)

    def _execute_tool_calls(
        self, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0
    ) -> None:
        """Execute tool calls from the assistant message and append results to messages.

        Routes between the concurrent and sequential strategies (both of which
        delegate to ``ToolExecutor``). The branch is kept here -- rather than in
        ToolExecutor -- so that callers/tests which patch
        ``agent._execute_tool_calls_sequential`` / ``_execute_tool_calls_concurrent``
        (or bind them onto a mock agent) intercept the dispatch exactly as before
        the extraction.
        """
        tool_calls = assistant_message.tool_calls

        # Single tool call or interactive tool present → sequential
        if len(tool_calls) <= 1 or any(
            tc.function.name in _NEVER_PARALLEL_TOOLS for tc in tool_calls
        ):
            return self._execute_tool_calls_sequential(
                assistant_message, messages, effective_task_id, api_call_count
            )

        # Multiple non-interactive tools → concurrent
        return self._execute_tool_calls_concurrent(
            assistant_message, messages, effective_task_id, api_call_count
        )

    def _invoke_tool(self, function_name: str, function_args: dict, effective_task_id: str) -> str:
        """Invoke a single tool and return the result string. No display logic.

        Thin delegating wrapper to ``ToolExecutor.invoke_tool``.
        """
        return _resolve_tool_executor(self).invoke_tool(
            function_name, function_args, effective_task_id
        )

    def _preflight_tool_call(self, function_name: str, function_args: dict) -> str | None:
        """Thin delegating wrapper to ``ToolExecutor.preflight_tool_call``."""
        return _resolve_tool_executor(self).preflight_tool_call(function_name, function_args)

    def _execute_tool_calls_concurrent(
        self, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0
    ) -> None:
        """Thin delegating wrapper to ``ToolExecutor.execute_concurrent``."""
        return _resolve_tool_executor(self).execute_concurrent(
            assistant_message, messages, effective_task_id, api_call_count
        )

    def stage_tool_result_appendix(self, text: str) -> None:
        """Managed-run contract (see agent.managed_run.ManagedRunContext).

        Stage guidance text to be appended to the NEXT tool result the model sees. Repeated
        calls before a tool result accumulate (blank-line separated). The staged text is
        consumed and cleared exactly once by ``_apply_post_tool_result_appendix``. Backed by the
        legacy ``_post_tool_result_appendix`` attribute so managed-runner code that sets that
        attribute directly keeps working during the transition.
        """
        text = str(text or "").strip()
        if not text:
            return
        previous = str(getattr(self, "_post_tool_result_appendix", "") or "").strip()
        self._post_tool_result_appendix = f"{previous}\n\n{text}".strip() if previous else text

    def set_tool_result_appendix(self, text: str) -> None:
        """Managed-run contract: REPLACE any staged appendix with ``text`` (empty text clears it).

        Unlike ``stage_tool_result_appendix`` (which accumulates), this overwrites — used by the
        managed runner when it has computed a fresh, complete guidance block that supersedes
        anything still pending for the next tool result.
        """
        text = str(text or "").strip()
        if text:
            self._post_tool_result_appendix = text
        else:
            self.clear_tool_result_appendix()

    def clear_tool_result_appendix(self) -> None:
        """Managed-run contract: discard any staged appendix so the next tool result is unmodified.

        Idempotent — afterwards the backing attribute is absent (not merely empty), matching the
        managed runner's prior ``delattr`` once a queued theorem step has been fully consumed.
        """
        if hasattr(self, "_post_tool_result_appendix"):
            delattr(self, "_post_tool_result_appendix")

    def _apply_post_tool_result_appendix(self, tool_msg: dict) -> None:
        """Consume any staged post-tool-result appendix, appending it to ``tool_msg`` once.

        Single source of truth for the appendix mechanism, shared by the sequential and
        concurrent tool-execution paths. If a managed runner (or any post_tool_result_callback)
        staged guidance via ``stage_tool_result_appendix`` / the ``_post_tool_result_appendix``
        attribute, append it to the tool result content the model sees, then clear it (one-shot).
        """
        appendix = getattr(self, "_post_tool_result_appendix", None)
        if appendix:
            tool_msg["content"] = f"{tool_msg['content']}\n\n{appendix}"
            self._post_tool_result_appendix = None

    def _execute_tool_calls_sequential(
        self, assistant_message, messages: list, effective_task_id: str, api_call_count: int = 0
    ) -> None:
        """Thin delegating wrapper to ``ToolExecutor.execute_sequential``."""
        return _resolve_tool_executor(self).execute_sequential(
            assistant_message, messages, effective_task_id, api_call_count
        )

    def _get_budget_warning(self, api_call_count: int) -> str | None:
        """Return a budget pressure string, or None if not yet needed.

        Two-tier system:
          - Caution (70%): nudge to consolidate work
          - Warning (90%): urgent, must respond now
        """
        if not self._budget_pressure_enabled or self.max_iterations <= 0:
            return None
        progress = api_call_count / self.max_iterations
        remaining = self.max_iterations - api_call_count
        # Research runs (LEANFLOW_RESEARCH_MODE) swap the wrap-up tone for a
        # route-request checkpoint — message text only, budget math unchanged.
        research = str(os.environ.get("LEANFLOW_RESEARCH_MODE", "") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if progress >= self._budget_warning_threshold:
            if research:
                return (
                    f"[BUDGET WARNING: Iteration {api_call_count}/{self.max_iterations}. "
                    f"Only {remaining} iteration(s) left. Checkpoint your findings into "
                    "the decision packet NOW, then continue or escalate a route request "
                    "(`decompose` | `negate` | `plan`).]"
                )
            return (
                f"[BUDGET WARNING: Iteration {api_call_count}/{self.max_iterations}. "
                f"Only {remaining} iteration(s) left. "
                "Provide your final response NOW. No more tool calls unless absolutely critical.]"
            )
        if progress >= self._budget_caution_threshold:
            if research:
                return (
                    f"[BUDGET: Iteration {api_call_count}/{self.max_iterations}. "
                    f"{remaining} iterations left. Consolidate findings into the decision "
                    "packet and prefer route-able progress over open-ended exploration.]"
                )
            return (
                f"[BUDGET: Iteration {api_call_count}/{self.max_iterations}. "
                f"{remaining} iterations left. Start consolidating your work.]"
            )
        return None

    def _max_tool_result_chars(self, function_name: str) -> int:
        if str(function_name or "") in {"lean_reasoning_help", "lean_decompose_helpers"}:
            return _LEAN_REASONING_HELP_MAX_TOOL_RESULT_CHARS
        return _DEFAULT_MAX_TOOL_RESULT_CHARS

    def _maybe_append_budget_warning_message(self, messages: list, api_call_count: int) -> bool:
        """Ensure budget pressure is visible even when no tool result carries it."""
        warning = self._get_budget_warning(api_call_count)
        if not warning:
            return False
        content = (
            "[LEANFLOW-RUNTIME STEP BUDGET]\n"
            f"{warning}\n"
            "Use the remaining API steps deliberately. If this is a managed Lean queue item "
            "and you cannot finish it before the budget runs out, preserve a concise failed-attempt "
            "status instead of claiming success."
        )
        last_content = str((messages[-1] or {}).get("content", "") or "") if messages else ""
        if (
            warning in last_content
            or content == last_content
            or content == self._last_budget_message_content
        ):
            return False
        messages.append({"role": "user", "content": content})
        self._last_budget_message_content = content
        if not self.quiet_mode:
            remaining = self.max_iterations - api_call_count
            tier = "⚠️  WARNING" if remaining <= self.max_iterations * 0.1 else "💡 CAUTION"
            print(
                f"{self.log_prefix}{tier}: {remaining} iterations remaining; runtime warning injected into model context"
            )
        return True

    def _maybe_refresh_api_step_budget_after_advisor(self, api_call_count: int) -> int:
        """Let a successful advisor call leave at least half the step budget for exploitation."""
        if self.max_iterations <= 0:
            return api_call_count
        reset_to = max(1, self.max_iterations // 2)
        if api_call_count <= reset_to:
            return api_call_count
        delta = max(api_call_count - reset_to, self.iteration_budget.used - reset_to)
        self.iteration_budget.refund(delta)
        if not self.quiet_mode:
            print(
                f"{self.log_prefix}↻ Lean advisor tool returned; refreshed API step budget "
                f"from {api_call_count}/{self.max_iterations} to {reset_to}/{self.max_iterations}."
            )
        _emit_workflow_event(
            "api-step-budget-refreshed",
            "Refreshed API step budget after Lean advisor tool",
            **_workflow_agent_event_details(
                self,
                previous_iteration=api_call_count,
                reset_iteration=reset_to,
                refunded_iterations=delta,
            ),
        )
        return reset_to

    def _maybe_precompress_before_advisor_tool(
        self,
        messages: list,
        system_message: str,
        active_system_prompt: str,
        *,
        effective_task_id: str,
    ) -> tuple[list, str]:
        """Thin delegating wrapper to ``CompressionPolicy.maybe_precompress_before_advisor_tool``."""
        return _resolve_compression_policy(self).maybe_precompress_before_advisor_tool(
            messages,
            system_message,
            active_system_prompt,
            effective_task_id=effective_task_id,
        )

    def _advisor_precompression_admitted(self, tool_names: set[str]) -> bool:
        """Return whether pending advisor calls may trigger reserve compression."""
        return _resolve_compression_policy(self).advisor_precompression_admitted(tool_names)

    def _compress_context_preserving_suffix(
        self,
        messages: list,
        suffix_start: int,
        system_message: str,
        *,
        approx_tokens: int,
        task_id: str,
    ) -> tuple[list, str]:
        """Thin delegating wrapper to ``CompressionPolicy.compress_context_preserving_suffix``."""
        return _resolve_compression_policy(self).compress_context_preserving_suffix(
            messages,
            suffix_start,
            system_message,
            approx_tokens=approx_tokens,
            task_id=task_id,
        )

    def _build_api_messages_for_turn(self, messages: list, active_system_prompt: str) -> list:
        """Thin delegating wrapper to ``ConversationManager.build_api_messages_for_turn``."""
        return _resolve_conversation_manager(self).build_api_messages_for_turn(
            messages, active_system_prompt
        )

    def _api_payload_size_estimate(self, api_messages: list) -> tuple[int, int]:
        """Thin delegating wrapper to ``CompressionPolicy.api_payload_size_estimate``."""
        return _resolve_compression_policy(self).api_payload_size_estimate(api_messages)

    def _maybe_compress_before_api_send(
        self,
        messages: list,
        system_message: str,
        active_system_prompt: str,
        *,
        api_messages: list,
        approx_tokens: int,
        task_id: str,
    ) -> tuple[list, str, list, int, int]:
        """Thin delegating wrapper to ``CompressionPolicy.maybe_compress_before_api_send``."""
        return _resolve_compression_policy(self).maybe_compress_before_api_send(
            messages,
            system_message,
            active_system_prompt,
            api_messages=api_messages,
            approx_tokens=approx_tokens,
            task_id=task_id,
        )

    def _handle_max_iterations(self, messages: list, api_call_count: int) -> str:
        """Request a summary when max iterations are reached. Returns the final response text."""
        print(f"⚠️  Reached maximum iterations ({self.max_iterations}). Requesting summary...")

        summary_request = (
            "You've reached the maximum number of tool-calling iterations allowed. "
            "Please provide a final response summarizing what you've found and accomplished so far, "
            "without calling any more tools."
        )
        messages.append({"role": "user", "content": summary_request})

        try:
            # Build API messages, stripping internal-only fields
            # (finish_reason, reasoning) that strict APIs like Mistral reject with 422
            _is_strict_api = "api.mistral.ai" in self.base_url.lower()
            api_messages = []
            for msg in messages:
                api_msg = msg.copy()
                for internal_field in ("reasoning", "finish_reason"):
                    api_msg.pop(internal_field, None)
                if _is_strict_api:
                    self._sanitize_tool_calls_for_strict_api(api_msg)
                api_messages.append(api_msg)

            effective_system = self._cached_system_prompt or ""
            if self.ephemeral_system_prompt:
                effective_system = (
                    effective_system + "\n\n" + self.ephemeral_system_prompt
                ).strip()
            if effective_system:
                api_messages = [{"role": "system", "content": effective_system}] + api_messages
            if self.prefill_messages:
                sys_offset = 1 if effective_system else 0
                for idx, pfm in enumerate(self.prefill_messages):
                    api_messages.insert(sys_offset + idx, pfm.copy())

            summary_extra_body = {}
            _is_nous = "nousresearch" in self.base_url.lower()
            if self._supports_reasoning_extra_body():
                if self.reasoning_config is not None:
                    summary_extra_body["reasoning"] = self.reasoning_config
                else:
                    summary_extra_body["reasoning"] = {"enabled": True, "effort": "high"}
            if _is_nous:
                summary_extra_body["tags"] = ["product=leanflow-agent"]

            if self.api_mode == "codex_responses":
                codex_kwargs = self._build_api_kwargs(api_messages)
                codex_kwargs.pop("tools", None)
                summary_response = self._run_codex_stream(codex_kwargs)
                assistant_message, _ = self._normalize_codex_response(summary_response)
                final_response = (
                    (assistant_message.content or "").strip() if assistant_message else ""
                )
            else:
                summary_kwargs = {
                    "model": self.model,
                    "messages": api_messages,
                }
                if self.max_tokens is not None:
                    summary_kwargs.update(self._max_tokens_param(self.max_tokens))

                # Include provider routing preferences
                provider_preferences = {}
                if self.providers_allowed:
                    provider_preferences["only"] = self.providers_allowed
                if self.providers_ignored:
                    provider_preferences["ignore"] = self.providers_ignored
                if self.providers_order:
                    provider_preferences["order"] = self.providers_order
                if self.provider_sort:
                    provider_preferences["sort"] = self.provider_sort
                if provider_preferences:
                    summary_extra_body["provider"] = provider_preferences

                if summary_extra_body:
                    summary_kwargs["extra_body"] = summary_extra_body

                if self.api_mode == "anthropic_messages":
                    from agent.providers.anthropic_adapter import build_anthropic_kwargs as _bak
                    from agent.providers.anthropic_adapter import (
                        normalize_anthropic_response as _nar,
                    )

                    _ant_kw = _bak(
                        model=self.model,
                        messages=api_messages,
                        tools=None,
                        max_tokens=self.max_tokens,
                        reasoning_config=self.reasoning_config,
                    )
                    summary_response = self._anthropic_messages_create(_ant_kw)
                    _msg, _ = _nar(summary_response)
                    final_response = (_msg.content or "").strip()
                else:
                    summary_response = self._ensure_primary_openai_client(
                        reason="iteration_limit_summary"
                    ).chat.completions.create(**summary_kwargs)

                    if summary_response.choices and summary_response.choices[0].message.content:
                        final_response = summary_response.choices[0].message.content
                    else:
                        final_response = ""

            if final_response:
                if "<think>" in final_response:
                    final_response = re.sub(
                        r"<think>.*?</think>\s*", "", final_response, flags=re.DOTALL
                    ).strip()
                if final_response:
                    messages.append({"role": "assistant", "content": final_response})
                else:
                    final_response = (
                        "I reached the iteration limit and couldn't generate a summary."
                    )
            else:
                # Retry summary generation
                if self.api_mode == "codex_responses":
                    codex_kwargs = self._build_api_kwargs(api_messages)
                    codex_kwargs.pop("tools", None)
                    retry_response = self._run_codex_stream(codex_kwargs)
                    retry_msg, _ = self._normalize_codex_response(retry_response)
                    final_response = (retry_msg.content or "").strip() if retry_msg else ""
                elif self.api_mode == "anthropic_messages":
                    from agent.providers.anthropic_adapter import build_anthropic_kwargs as _bak2
                    from agent.providers.anthropic_adapter import (
                        normalize_anthropic_response as _nar2,
                    )

                    _ant_kw2 = _bak2(
                        model=self.model,
                        messages=api_messages,
                        tools=None,
                        max_tokens=self.max_tokens,
                        reasoning_config=self.reasoning_config,
                    )
                    retry_response = self._anthropic_messages_create(_ant_kw2)
                    _retry_msg, _ = _nar2(retry_response)
                    final_response = (_retry_msg.content or "").strip()
                else:
                    summary_kwargs = {
                        "model": self.model,
                        "messages": api_messages,
                    }
                    if self.max_tokens is not None:
                        summary_kwargs.update(self._max_tokens_param(self.max_tokens))
                    if summary_extra_body:
                        summary_kwargs["extra_body"] = summary_extra_body

                    summary_response = self._ensure_primary_openai_client(
                        reason="iteration_limit_summary_retry"
                    ).chat.completions.create(**summary_kwargs)

                    if summary_response.choices and summary_response.choices[0].message.content:
                        final_response = summary_response.choices[0].message.content
                    else:
                        final_response = ""

                if final_response:
                    if "<think>" in final_response:
                        final_response = re.sub(
                            r"<think>.*?</think>\s*", "", final_response, flags=re.DOTALL
                        ).strip()
                    if final_response:
                        messages.append({"role": "assistant", "content": final_response})
                    else:
                        final_response = (
                            "I reached the iteration limit and couldn't generate a summary."
                        )
                else:
                    final_response = (
                        "I reached the iteration limit and couldn't generate a summary."
                    )

        except Exception as e:
            logging.warning(f"Failed to get summary response: {e}")
            final_response = f"I reached the maximum iterations ({self.max_iterations}) but couldn't summarize. Error: {str(e)}"

        return final_response

    def run_conversation(
        self,
        user_message: str,
        system_message: str = None,
        conversation_history: list[dict[str, Any]] = None,
        task_id: str = None,
        stream_callback: Callable | None = None,
        persist_user_message: str | None = None,
    ) -> dict[str, Any]:
        """
        Run a complete conversation with tool calling until completion.

        Args:
            user_message (str): The user's message/question
            system_message (str): Custom system message (optional, overrides ephemeral_system_prompt if provided)
            conversation_history (List[Dict]): Previous conversation messages (optional)
            task_id (str): Unique identifier for this task to isolate VMs between concurrent tasks (optional, auto-generated if not provided)
            stream_callback: Optional callback invoked with each text delta during streaming.
                Used by the TTS pipeline to start audio generation before the full response.
                When None (default), API calls use the standard non-streaming path.
            persist_user_message: Optional clean user message to store in
                transcripts/history when user_message contains API-only
                synthetic prefixes.

        Returns:
            Dict: Complete conversation result with final response and message history
        """
        # Guard stdio against OSError from broken pipes (systemd/headless/daemon).
        # Installed once, transparent when streams are healthy, prevents crash on write.
        _install_safe_stdio()

        # Store stream callback for _interruptible_api_call to pick up
        self._stream_callback = stream_callback
        self._persist_user_message_idx = None
        self._persist_user_message_override = persist_user_message
        self._usage_summary_logged = False
        self._tokens.start_turn()
        self._current_run_api_calls = 0
        self._conversation_wall_timeout_reached = False
        self._conversation_deadline_monotonic = (
            time.monotonic() + self.wall_timeout_s if self.wall_timeout_s is not None else None
        )
        # Generate unique task_id if not provided to isolate VMs between concurrent tasks
        effective_task_id = task_id or str(uuid.uuid4())

        # Reset retry counters and iteration budget at the start of each turn
        # so subagent usage from a previous turn doesn't eat into the next one.
        self._invalid_tool_retries = 0
        self._invalid_json_retries = 0
        self._empty_content_retries = 0
        self._incomplete_scratchpad_retries = 0
        self._codex_incomplete_retries = 0
        self._last_content_with_tools = None
        self._turns_since_memory = 0
        self._iters_since_skill = 0
        self.iteration_budget = IterationBudget(self.max_iterations)

        # Initialize conversation (copy to avoid mutating the caller's list)
        messages = list(conversation_history) if conversation_history else []

        # Hydrate todo store from conversation history (gateway creates a fresh
        # AIAgent per message, so the in-memory store is empty -- we need to
        # recover the todo state from the most recent todo tool response in history)
        if conversation_history and not self._todo_store.has_items():
            self._hydrate_todo_store(conversation_history)

        # Prefill messages (few-shot priming) are injected at API-call time only,
        # never stored in the messages list. This keeps them ephemeral: they won't
        # be saved to session DB, session logs, or batch trajectories, but they're
        # automatically re-applied on every API call (including session continuations).

        # Track user turns for memory flush and periodic nudge logic
        self._user_turn_count += 1

        # Periodic memory nudge: remind the model to consider saving memories.
        # Counter resets whenever the memory tool is actually used.
        if (
            self._memory_nudge_interval > 0
            and "memory" in self.valid_tool_names
            and self._memory_store
        ):
            self._turns_since_memory += 1
            if self._turns_since_memory >= self._memory_nudge_interval:
                user_message += (
                    "\n\n[System: You've had several exchanges. Consider: "
                    "has the user shared preferences, corrected you, or revealed "
                    "something about their workflow worth remembering for future sessions?]"
                )
                self._turns_since_memory = 0

        # Skill creation nudge: fires on the first user message after a long tool loop.
        # The counter increments per API iteration in the tool loop and is checked here.
        if (
            self._skill_nudge_interval > 0
            and self._iters_since_skill >= self._skill_nudge_interval
            and "skill_manage" in self.valid_tool_names
        ):
            user_message += (
                "\n\n[System: The previous task involved many tool calls. "
                "Save the approach as a skill if it's reusable, or update "
                "any existing skill you used if it was wrong or incomplete.]"
            )
            self._iters_since_skill = 0

        # Add user message
        user_msg = {"role": "user", "content": user_message}
        messages.append(user_msg)
        current_turn_user_idx = len(messages) - 1
        self._persist_user_message_idx = current_turn_user_idx

        if not self.quiet_mode:
            self._log_conversation_start(user_message)
        _emit_workflow_event(
            "conversation-start",
            "Agent conversation started",
            **_workflow_agent_event_details(
                self,
                user_message=user_message,
                persist_user_message=persist_user_message,
                system_message=system_message or "",
            ),
        )

        # ── System prompt (cached per session for prefix caching) ──
        # Built once on first call, reused for all subsequent calls. Only rebuilt
        # after context compression events. For continuing sessions, the stored
        # prompt is reused from the session DB rather than rebuilt (so the
        # Anthropic cache prefix matches). The PromptManager owns this logic.
        active_system_prompt = _resolve_prompt_manager(self).resolve_active_system_prompt(
            system_message, conversation_history
        )

        # ── Preflight context compression ──
        # Before entering the main loop, check if the loaded conversation
        # history already exceeds the model's context threshold.  This handles
        # cases where a user switches to a model with a smaller context window
        # while having a large existing session — compress proactively rather
        # than waiting for an API error (which might be caught as a non-retryable
        # 4xx and abort the request entirely).
        if (
            self.compression_enabled
            and len(messages)
            > self.context_compressor.protect_first_n + self.context_compressor.protect_last_n + 1
        ):
            _sys_tok_est = estimate_tokens_rough(active_system_prompt or "")
            _msg_tok_est = estimate_messages_tokens_rough(messages)
            _preflight_tokens = _sys_tok_est + _msg_tok_est

            if _preflight_tokens >= self.context_compressor.threshold_tokens:
                logger.info(
                    "Preflight compression: ~%s tokens >= %s threshold (model %s, ctx %s)",
                    f"{_preflight_tokens:,}",
                    f"{self.context_compressor.threshold_tokens:,}",
                    self.model,
                    f"{self.context_compressor.context_length:,}",
                )
                if not self.quiet_mode:
                    print(
                        f"📦 Preflight compression: ~{_preflight_tokens:,} tokens "
                        f">= {self.context_compressor.threshold_tokens:,} threshold"
                    )
                # May need multiple passes for very large sessions with small
                # context windows (each pass summarises the middle N turns).
                for _pass in range(3):
                    _orig_len = len(messages)
                    messages, active_system_prompt = self._compress_context(
                        messages,
                        system_message,
                        approx_tokens=_preflight_tokens,
                        task_id=effective_task_id,
                    )
                    if len(messages) >= _orig_len:
                        break  # Cannot compress further
                    # Re-estimate after compression
                    _sys_tok_est = estimate_tokens_rough(active_system_prompt or "")
                    _msg_tok_est = estimate_messages_tokens_rough(messages)
                    _preflight_tokens = _sys_tok_est + _msg_tok_est
                    if _preflight_tokens < self.context_compressor.threshold_tokens:
                        break  # Under threshold

        # Main conversation loop
        api_call_count = 0
        final_response = None
        interrupted = False
        codex_ack_continuations = 0
        length_continue_retries = 0
        truncated_response_prefix = ""

        # Clear any stale interrupt state at start
        self.clear_interrupt()

        while api_call_count < self.max_iterations and self.iteration_budget.remaining > 0:
            # Reset per-turn checkpoint dedup so each iteration can take one snapshot
            self._checkpoint_mgr.new_turn()

            # Check for interrupt request (e.g., user sent new message)
            if self._interrupt_requested:
                interrupted = True
                if not self.quiet_mode and not bool(
                    getattr(self, "_suppress_next_interrupt_log", False)
                ):
                    print("\n⚡ Breaking out of tool loop due to interrupt...")
                break

            if (
                self._conversation_deadline_monotonic is not None
                and time.monotonic() >= self._conversation_deadline_monotonic
            ):
                self._conversation_wall_timeout_reached = True
                if not self.quiet_mode:
                    print("\n⏱️  Conversation wall-clock deadline reached at a safe boundary")
                break

            api_call_count += 1
            self._current_run_api_calls = api_call_count
            if not self.iteration_budget.consume():
                if not self.quiet_mode:
                    print(
                        f"\n⚠️  Session iteration budget exhausted ({self.iteration_budget.max_total} total across agent + subagents)"
                    )
                break

            # Fire step_callback for gateway hooks (agent:step event)
            if self.step_callback is not None:
                try:
                    prev_tools = []
                    for _m in reversed(messages):
                        if _m.get("role") == "assistant" and _m.get("tool_calls"):
                            prev_tools = [
                                tc["function"]["name"]
                                for tc in _m["tool_calls"]
                                if isinstance(tc, dict)
                            ]
                            break
                    self.step_callback(api_call_count, prev_tools)
                except Exception as _step_err:
                    logger.debug(
                        "step_callback error (iteration %s): %s", api_call_count, _step_err
                    )

            # Track tool-calling iterations for skill nudge.
            # Counter resets whenever skill_manage is actually used.
            if self._skill_nudge_interval > 0 and "skill_manage" in self.valid_tool_names:
                self._iters_since_skill += 1

            self._maybe_append_budget_warning_message(messages, api_call_count)

            api_messages = self._build_api_messages_for_turn(messages, active_system_prompt)
            approx_tokens, total_chars = self._api_payload_size_estimate(api_messages)
            messages, active_system_prompt, api_messages, approx_tokens, total_chars = (
                self._maybe_compress_before_api_send(
                    messages,
                    system_message,
                    active_system_prompt,
                    api_messages=api_messages,
                    approx_tokens=approx_tokens,
                    task_id=effective_task_id,
                )
            )

            # Thinking spinner for quiet mode (animated during API call)
            thinking_spinner = None

            if not self.quiet_mode:
                self._vprint(f"\n{self.log_prefix}{'─' * 72}")
                self._vprint(f"{self.log_prefix}🔄 API step {api_call_count}/{self.max_iterations}")
                self._vprint(
                    f"{self.log_prefix}   📥 Request: {len(api_messages)} messages, ~{approx_tokens:,} tokens (~{total_chars:,} chars)"
                )
                self._vprint(
                    f"{self.log_prefix}   🔧 Available tools: {len(self.tools) if self.tools else 0}"
                )
            elif (
                not bool(getattr(self, "_suppress_spinners", False))
                and self._stream_callback is None
            ):
                # Animated thinking spinner in quiet mode (skip during streaming TTS)
                face = random.choice(KawaiiSpinner.KAWAII_THINKING)
                verb = random.choice(KawaiiSpinner.THINKING_VERBS)
                if self.thinking_callback:
                    # CLI TUI mode: use prompt_toolkit widget instead of raw spinner
                    self.thinking_callback(f"{face} {verb}...")
                else:
                    spinner_type = random.choice(["brain", "sparkle", "pulse", "moon", "star"])
                    thinking_spinner = KawaiiSpinner(f"{face} {verb}...", spinner_type=spinner_type)
                    thinking_spinner.start()
            _emit_workflow_event(
                "api-request",
                f"API call #{api_call_count}",
                **_workflow_agent_event_details(
                    self,
                    **build_api_request_activity_details(
                        api_messages,
                        iteration=api_call_count,
                        approx_tokens=approx_tokens,
                        total_chars=total_chars,
                        available_tools=(
                            [tool["function"]["name"] for tool in self.tools] if self.tools else []
                        ),
                    ),
                ),
            )

            # Log request details if verbose
            if self.verbose_logging:
                logging.debug(
                    f"API Request - Model: {self.model}, Messages: {len(messages)}, Tools: {len(self.tools) if self.tools else 0}"
                )
                logging.debug(f"Last message role: {messages[-1]['role'] if messages else 'none'}")
                logging.debug(f"Total message size: ~{approx_tokens:,} tokens")

            api_start_time = time.time()
            retry_count = 0
            provider_recovery_deadline_monotonic: float | None = None
            self._transient_provider_recovery_deadline_monotonic = None
            # One initial provider call plus the managed-workflow 5/15/45s
            # transient retry schedule.  The historical name ``retry_count``
            # below counts failed attempts, so the attempt ceiling is four.
            max_retries = TRANSIENT_PROVIDER_MAX_ATTEMPTS
            compression_attempts = 0
            max_compression_attempts = 3
            codex_auth_retry_attempted = False
            anthropic_auth_retry_attempted = False
            nous_auth_retry_attempted = False
            usage_limit_wait_attempted = False
            restart_with_compressed_messages = False
            restart_with_length_continuation = False

            finish_reason = "stop"
            response = None  # Guard against UnboundLocalError if all retries fail
            usage_dict: dict[str, int] = {}

            while retry_count < max_retries:
                try:
                    api_kwargs = self._build_api_kwargs(api_messages)
                    if self.api_mode == "codex_responses":
                        api_kwargs = self._preflight_codex_api_kwargs(
                            api_kwargs, allow_stream=False
                        )

                    if os.getenv("LEANFLOW_DUMP_REQUESTS", "").strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }:
                        self._dump_api_request_debug(api_kwargs, reason="preflight")

                    cb = getattr(self, "_stream_callback", None)
                    if cb is not None and self.api_mode == "chat_completions":
                        response = self._streaming_api_call(api_kwargs, cb)
                    else:
                        response = self._interruptible_api_call(api_kwargs)
                        # Forward full response to TTS callback for non-streaming providers
                        # (e.g. Anthropic) so voice TTS still works via batch delivery.
                        if cb is not None and response:
                            try:
                                content = None
                                # Try choices first — _interruptible_api_call converts all
                                # providers (including Anthropic) to this format.
                                with contextlib.suppress(AttributeError, IndexError):
                                    content = response.choices[0].message.content
                                # Fallback: Anthropic native content blocks
                                if not content and self.api_mode == "anthropic_messages":
                                    text_parts = [
                                        block.text
                                        for block in getattr(response, "content", [])
                                        if getattr(block, "type", None) == "text"
                                        and getattr(block, "text", None)
                                    ]
                                    content = " ".join(text_parts) if text_parts else None
                                if content:
                                    cb(content)
                            except Exception:
                                pass

                    api_duration = time.time() - api_start_time

                    # Stop thinking spinner silently -- the response box or tool
                    # execution messages that follow are more informative.
                    if thinking_spinner:
                        thinking_spinner.stop("")
                        thinking_spinner = None
                    if self.thinking_callback:
                        self.thinking_callback("")

                    if not self.quiet_mode:
                        self._vprint(
                            f"{self.log_prefix}⏱️  API call completed in {api_duration:.2f}s"
                        )

                    if self.verbose_logging:
                        # Log response with provider info if available
                        resp_model = getattr(response, "model", "N/A") if response else "N/A"
                        logging.debug(
                            f"API Response received - Model: {resp_model}, Usage: {response.usage if hasattr(response, 'usage') else 'N/A'}"
                        )

                    # Validate response shape before proceeding
                    response_invalid = False
                    error_details = []
                    if self.api_mode == "codex_responses":
                        output_items = (
                            getattr(response, "output", None) if response is not None else None
                        )
                        if response is None:
                            response_invalid = True
                            error_details.append("response is None")
                        elif not isinstance(output_items, list):
                            response_invalid = True
                            error_details.append("response.output is not a list")
                        elif len(output_items) == 0:
                            response_invalid = True
                            error_details.append("response.output is empty")
                    elif self.api_mode == "anthropic_messages":
                        content_blocks = (
                            getattr(response, "content", None) if response is not None else None
                        )
                        if response is None:
                            response_invalid = True
                            error_details.append("response is None")
                        elif not isinstance(content_blocks, list):
                            response_invalid = True
                            error_details.append("response.content is not a list")
                        elif len(content_blocks) == 0:
                            response_invalid = True
                            error_details.append("response.content is empty")
                    else:
                        if (
                            response is None
                            or not hasattr(response, "choices")
                            or response.choices is None
                            or len(response.choices) == 0
                        ):
                            response_invalid = True
                            if response is None:
                                error_details.append("response is None")
                            elif not hasattr(response, "choices"):
                                error_details.append("response has no 'choices' attribute")
                            elif response.choices is None:
                                error_details.append("response.choices is None")
                            else:
                                error_details.append("response.choices is empty")

                    if response_invalid:
                        # Stop spinner before printing error messages
                        if thinking_spinner:
                            thinking_spinner.stop("(´;ω;`) oops, retrying...")
                            thinking_spinner = None
                        if self.thinking_callback:
                            self.thinking_callback("")

                        # This is often rate limiting or provider returning malformed response
                        retry_count += 1
                        provider_recovery_deadline_monotonic = transient_provider_recovery_deadline_monotonic(
                            current_deadline_monotonic=provider_recovery_deadline_monotonic,
                            conversation_deadline_monotonic=self._conversation_deadline_monotonic,
                        )
                        self._transient_provider_recovery_deadline_monotonic = (
                            provider_recovery_deadline_monotonic
                        )

                        # Check for error field in response (some providers include this)
                        error_msg = "Unknown"
                        provider_name = "Unknown"
                        if response and hasattr(response, "error") and response.error:
                            error_msg = redact_sensitive_text(str(response.error))
                            # Try to extract provider from error metadata
                            if hasattr(response.error, "metadata") and response.error.metadata:
                                provider_name = response.error.metadata.get(
                                    "provider_name", "Unknown"
                                )
                        elif response and hasattr(response, "message") and response.message:
                            error_msg = redact_sensitive_text(str(response.message))

                        # Try to get provider from model field (OpenRouter often returns actual model used)
                        if (
                            provider_name == "Unknown"
                            and response
                            and hasattr(response, "model")
                            and response.model
                        ):
                            provider_name = f"model={response.model}"

                        # Check for x-openrouter-provider or similar metadata
                        if provider_name == "Unknown" and response:
                            # Log all response attributes for debugging
                            resp_attrs = {
                                k: str(v)[:100]
                                for k, v in vars(response).items()
                                if not k.startswith("_")
                            }
                            if self.verbose_logging:
                                logging.debug(
                                    f"Response attributes for invalid response: {resp_attrs}"
                                )

                        self._vprint(
                            f"{self.log_prefix}⚠️  Invalid API response (attempt {retry_count}/{max_retries}): {', '.join(error_details)}",
                            force=True,
                        )
                        self._vprint(
                            f"{self.log_prefix}   🏢 Provider: {provider_name}", force=True
                        )
                        self._vprint(
                            f"{self.log_prefix}   📝 Provider message: {error_msg[:200]}",
                            force=True,
                        )
                        self._vprint(
                            f"{self.log_prefix}   ⏱️  Response time: {api_duration:.2f}s (fast response often indicates rate limiting)",
                            force=True,
                        )

                        if retry_count >= max_retries:
                            # Try fallback before giving up
                            if self._try_activate_fallback():
                                retry_count = 0
                                continue
                            self._vprint(
                                f"{self.log_prefix}❌ Provider attempts exhausted ({max_retries} attempts, 3 retries) for invalid responses. Pausing.",
                                force=True,
                            )
                            _emit_workflow_event(
                                "provider-retry-exhausted",
                                "Transient provider retries exhausted after invalid responses",
                                **_workflow_agent_event_details(
                                    self,
                                    failed_attempt=retry_count,
                                    max_attempts=max_retries,
                                    retries=retry_count - 1,
                                    error_type="invalid_response",
                                    error=", ".join(error_details)[:300],
                                ),
                            )
                            logging.error(
                                f"{self.log_prefix}Invalid API response after {max_retries} attempts."
                            )
                            self._persist_session(messages, conversation_history)
                            return {
                                "messages": messages,
                                "completed": False,
                                "api_calls": api_call_count,
                                "error": "Invalid API response shape. Likely rate limited or malformed provider response.",
                                "failed": True,  # Mark as failure for filtering
                                # The native workflow wrapper historically
                                # supplied its own retry loop.  Mark this
                                # failure so it does not multiply the complete
                                # provider-level 5/15/45 schedule.
                                "provider_retries_exhausted": True,
                            }

                        # Invalid/empty provider responses are usually rate
                        # limiting in disguise; use the same deterministic
                        # transient schedule as explicit provider errors.
                        wait_time = transient_provider_retry_delay_within_deadline_s(
                            retry_count,
                            deadline_monotonic=provider_recovery_deadline_monotonic,
                        )
                        if wait_time is None:
                            _emit_workflow_event(
                                "provider-retry-skipped-deadline",
                                "Skipped provider retry because the recovery deadline is exhausted",
                                **_workflow_agent_event_details(
                                    self,
                                    failed_attempt=retry_count,
                                    max_attempts=max_retries,
                                    error_type="invalid_response",
                                    error=", ".join(error_details)[:300],
                                ),
                            )
                            return {
                                "messages": messages,
                                "completed": False,
                                "api_calls": api_call_count,
                                "error": "Invalid API response and no useful retry window remains.",
                                "failed": True,
                                "provider_retries_exhausted": True,
                                "provider_retry_skipped_deadline": True,
                            }
                        self._vprint(
                            f"{self.log_prefix}⏳ Retrying in {wait_time:g}s (managed transient-provider backoff)...",
                            force=True,
                        )
                        _emit_workflow_event(
                            "provider-retry-scheduled",
                            f"Provider retry {retry_count}/3 scheduled in {wait_time:g}s",
                            **_workflow_agent_event_details(
                                self,
                                failed_attempt=retry_count,
                                max_attempts=max_retries,
                                retry_number=retry_count,
                                wait_seconds=wait_time,
                                provider_recovery_remaining_s=round(
                                    max(
                                        0.0,
                                        provider_recovery_deadline_monotonic - time.monotonic(),
                                    ),
                                    3,
                                ),
                                error_type="invalid_response",
                                error=", ".join(error_details)[:300],
                            ),
                        )
                        logging.warning(
                            f"Invalid API response (retry {retry_count}/{max_retries}): {', '.join(error_details)} | Provider: {provider_name}"
                        )

                        # Sleep in small increments to stay responsive to interrupts
                        sleep_end = time.time() + wait_time
                        while time.time() < sleep_end:
                            if self._interrupt_requested:
                                self._vprint(
                                    f"{self.log_prefix}⚡ Interrupt detected during retry wait, aborting.",
                                    force=True,
                                )
                                self._persist_session(messages, conversation_history)
                                self.clear_interrupt()
                                return {
                                    "final_response": f"Operation interrupted: retrying API call after rate limit (retry {retry_count}/{max_retries}).",
                                    "messages": messages,
                                    "api_calls": api_call_count,
                                    "completed": False,
                                    "interrupted": True,
                                }
                            time.sleep(0.2)
                        continue  # Retry the API call

                    # Check finish_reason before proceeding
                    if self.api_mode == "codex_responses":
                        status = getattr(response, "status", None)
                        incomplete_details = getattr(response, "incomplete_details", None)
                        incomplete_reason = None
                        if isinstance(incomplete_details, dict):
                            incomplete_reason = incomplete_details.get("reason")
                        else:
                            incomplete_reason = getattr(incomplete_details, "reason", None)
                        if status == "incomplete" and incomplete_reason in {
                            "max_output_tokens",
                            "length",
                        }:
                            finish_reason = "length"
                        else:
                            finish_reason = "stop"
                    elif self.api_mode == "anthropic_messages":
                        stop_reason_map = {
                            "end_turn": "stop",
                            "tool_use": "tool_calls",
                            "max_tokens": "length",
                            "stop_sequence": "stop",
                        }
                        finish_reason = stop_reason_map.get(response.stop_reason, "stop")
                    else:
                        finish_reason = response.choices[0].finish_reason

                    if finish_reason == "length":
                        self._vprint(
                            f"{self.log_prefix}⚠️  Response truncated (finish_reason='length') - model hit max output tokens",
                            force=True,
                        )

                        if self.api_mode == "chat_completions":
                            assistant_message = response.choices[0].message
                            if not assistant_message.tool_calls:
                                length_continue_retries += 1
                                interim_msg = self._build_assistant_message(
                                    assistant_message, finish_reason
                                )
                                messages.append(interim_msg)
                                if assistant_message.content:
                                    truncated_response_prefix += assistant_message.content

                                if length_continue_retries < 3:
                                    self._vprint(
                                        f"{self.log_prefix}↻ Requesting continuation "
                                        f"({length_continue_retries}/3)..."
                                    )
                                    continue_msg = {
                                        "role": "user",
                                        "content": (
                                            "[System: Your previous response was truncated by the output "
                                            "length limit. Continue exactly where you left off. Do not "
                                            "restart or repeat prior text. Finish the answer directly.]"
                                        ),
                                    }
                                    messages.append(continue_msg)
                                    self._session_messages = messages
                                    self._save_session_log(messages)
                                    restart_with_length_continuation = True
                                    break

                                partial_response = self._strip_think_blocks(
                                    truncated_response_prefix
                                ).strip()
                                self._cleanup_task_resources(effective_task_id)
                                self._persist_session(messages, conversation_history)
                                return {
                                    "final_response": partial_response or None,
                                    "messages": messages,
                                    "api_calls": api_call_count,
                                    "completed": False,
                                    "partial": True,
                                    "error": "Response remained truncated after 3 continuation attempts",
                                }

                        # If we have prior messages, roll back to last complete state
                        if len(messages) > 1:
                            self._vprint(
                                f"{self.log_prefix}   ⏪ Rolling back to last complete assistant turn"
                            )
                            rolled_back_messages = self._get_messages_up_to_last_assistant(messages)

                            self._cleanup_task_resources(effective_task_id)
                            self._persist_session(messages, conversation_history)

                            return {
                                "final_response": None,
                                "messages": rolled_back_messages,
                                "api_calls": api_call_count,
                                "completed": False,
                                "partial": True,
                                "error": "Response truncated due to output length limit",
                            }
                        else:
                            # First message was truncated - mark as failed
                            self._vprint(
                                f"{self.log_prefix}❌ First response truncated - cannot recover",
                                force=True,
                            )
                            self._persist_session(messages, conversation_history)
                            return {
                                "final_response": None,
                                "messages": messages,
                                "api_calls": api_call_count,
                                "completed": False,
                                "failed": True,
                                "error": "First response truncated due to output length limit",
                            }

                    # Track actual token usage from response for context management
                    if hasattr(response, "usage") and response.usage:
                        if self.api_mode in ("codex_responses", "anthropic_messages"):
                            prompt_tokens = getattr(response.usage, "input_tokens", 0) or 0
                            completion_tokens = getattr(response.usage, "output_tokens", 0) or 0
                            total_tokens = getattr(response.usage, "total_tokens", None) or (
                                prompt_tokens + completion_tokens
                            )
                        else:
                            prompt_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
                            completion_tokens = getattr(response.usage, "completion_tokens", 0) or 0
                            total_tokens = getattr(response.usage, "total_tokens", 0) or (
                                prompt_tokens + completion_tokens
                            )
                        usage_dict = {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": total_tokens,
                        }
                        reported_cost = self._extract_reported_cost_usd(response.usage)
                        self.context_compressor.update_from_response(usage_dict)

                        # Cache discovered context length after successful call
                        if self.context_compressor._context_probed:
                            ctx = self.context_compressor.context_length
                            save_context_length(self.model, self.base_url, ctx)
                            print(
                                f"{self.log_prefix}💾 Cached context length: {ctx:,} tokens for {self.model}"
                            )
                            self.context_compressor._context_probed = False

                        self._tokens.record_usage(
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=total_tokens,
                            reported_cost_usd=reported_cost,
                        )

                        if not self.quiet_mode:
                            self._log_token_usage(
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=total_tokens,
                            )
                            self._log_reasoning_replay_accounting(
                                api_messages=api_messages,
                                approx_tokens=approx_tokens,
                                provider_prompt_tokens=prompt_tokens,
                            )

                        # Persist token counts to session DB for /insights.
                        # Gateway sessions persist via session_store.update_session()
                        # after run_conversation returns, so only persist here for
                        # CLI (and other non-gateway) platforms to avoid double-counting.
                        if (
                            self._session_db
                            and self.session_id
                            and getattr(self, "platform", None) == "cli"
                        ):
                            try:
                                self._session_db.update_token_counts(
                                    self.session_id,
                                    input_tokens=prompt_tokens,
                                    output_tokens=completion_tokens,
                                    model=self.model,
                                )
                            except Exception:
                                pass  # never block the agent loop

                        if self.verbose_logging:
                            logging.debug(
                                f"Token usage: prompt={usage_dict['prompt_tokens']:,}, completion={usage_dict['completion_tokens']:,}, total={usage_dict['total_tokens']:,}"
                            )

                        # Log cache hit stats when prompt caching is active, OR on the self-hosted
                        # RCP route where vLLM automatic prefix caching populates cached_tokens even
                        # though _use_prompt_caching is False — so the cache work is measurable.
                        if self._use_prompt_caching or self._is_rcp_route():
                            if self.api_mode == "anthropic_messages":
                                # Anthropic uses cache_read_input_tokens / cache_creation_input_tokens
                                cached = getattr(response.usage, "cache_read_input_tokens", 0) or 0
                                written = (
                                    getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                                )
                            else:
                                # OpenRouter uses prompt_tokens_details.cached_tokens
                                details = getattr(response.usage, "prompt_tokens_details", None)
                                cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
                                written = (
                                    getattr(details, "cache_write_tokens", 0) or 0 if details else 0
                                )
                            prompt = usage_dict["prompt_tokens"]
                            hit_pct = (cached / prompt * 100) if prompt > 0 else 0
                            if not self.quiet_mode:
                                self._vprint(
                                    f"{self.log_prefix}   💾 Cache: {cached:,}/{prompt:,} tokens ({hit_pct:.0f}% hit, {written:,} written)"
                                )
                    elif not self.quiet_mode:
                        self._vprint(
                            f"{self.log_prefix}   📊 Tokens: unavailable from provider response"
                        )

                    break  # Success, exit retry loop

                except InterruptedError:
                    if thinking_spinner:
                        thinking_spinner.stop("")
                        thinking_spinner = None
                    if self.thinking_callback:
                        self.thinking_callback("")
                    api_elapsed = time.time() - api_start_time
                    self._vprint(f"{self.log_prefix}⚡ Interrupted during API call.", force=True)
                    self._persist_session(messages, conversation_history)
                    interrupted = True
                    final_response = f"Operation interrupted: waiting for model response ({api_elapsed:.1f}s elapsed)."
                    break

                except Exception as api_error:
                    # Stop spinner before printing error messages
                    if thinking_spinner:
                        thinking_spinner.stop("(╥_╥) error, retrying...")
                        thinking_spinner = None
                    if self.thinking_callback:
                        self.thinking_callback("")

                    status_code = getattr(api_error, "status_code", None)
                    safe_api_error = redact_sensitive_text(str(api_error))
                    usage_limit = extract_provider_usage_limit(
                        api_error,
                        now_epoch=time.time(),
                    )
                    if usage_limit is not None:
                        retry_after = usage_limit.to_mapping()
                        max_reset_wait = provider_reset_wait_max_seconds()
                        _emit_workflow_event(
                            "provider-usage-limit",
                            "Provider usage limit reached",
                            **_workflow_agent_event_details(
                                self,
                                **retry_after,
                                max_wait_seconds=max_reset_wait,
                            ),
                        )
                        if self._try_activate_fallback():
                            continue
                        wait_seconds = int(usage_limit.retry_after_seconds)
                        if not usage_limit_wait_attempted and wait_seconds <= max_reset_wait:
                            usage_limit_wait_attempted = True
                            self._vprint(
                                f"{self.log_prefix}⏳ Provider usage resets in "
                                f"{wait_seconds}s; waiting without spending a transient retry...",
                                force=True,
                            )
                            _emit_workflow_event(
                                "provider-reset-wait",
                                f"Waiting {wait_seconds}s for the provider usage reset",
                                **_workflow_agent_event_details(
                                    self,
                                    **retry_after,
                                    max_wait_seconds=max_reset_wait,
                                ),
                            )
                            sleep_end = time.time() + wait_seconds
                            while time.time() < sleep_end:
                                if self._interrupt_requested:
                                    self._persist_session(messages, conversation_history)
                                    self.clear_interrupt()
                                    return {
                                        "final_response": (
                                            "Operation interrupted while waiting for the "
                                            "provider usage reset."
                                        ),
                                        "messages": messages,
                                        "api_calls": api_call_count,
                                        "completed": False,
                                        "interrupted": True,
                                        "provider_retry_after": retry_after,
                                    }
                                time.sleep(min(0.2, max(0.0, sleep_end - time.time())))
                            _emit_workflow_event(
                                "provider-reset-wait-finished",
                                "Provider reset wait finished; retrying once",
                                **_workflow_agent_event_details(self, **retry_after),
                            )
                            continue

                        pause_callback = getattr(
                            self,
                            "_managed_provider_usage_limit_callback",
                            None,
                        )
                        if callable(pause_callback):
                            try:
                                # Publish before session persistence and resource
                                # cleanup. Those can be slow enough for a parent
                                # research heartbeat to launch another request.
                                pause_callback(retry_after)
                            except Exception:
                                # The returned structured result remains the
                                # durable fallback for the native supervisor.
                                pass
                        self._vprint(
                            f"{self.log_prefix}⏸️  Provider usage limit remains active for "
                            f"{wait_seconds}s; checkpointing instead of hammering the API.",
                            force=True,
                        )
                        self._persist_session(messages, conversation_history)
                        return {
                            "final_response": None,
                            "messages": messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "failed": True,
                            "partial": True,
                            "error": (
                                "Provider usage limit reached; available after epoch "
                                f"{usage_limit.unavailable_until_epoch}."
                            ),
                            # This dedicated pause owns the retry timing. The native
                            # wrapper must not multiply it by its ordinary 5/15/45
                            # transient-provider schedule.
                            "provider_retries_exhausted": True,
                            "provider_globally_unavailable": True,
                            "provider_retry_after": retry_after,
                        }
                    if (
                        self.api_mode == "codex_responses"
                        and self.provider == "openai-codex"
                        and status_code == 401
                        and not codex_auth_retry_attempted
                    ):
                        codex_auth_retry_attempted = True
                        if self._try_refresh_codex_client_credentials(force=True):
                            self._vprint(
                                f"{self.log_prefix}🔐 Codex auth refreshed after 401. Retrying request..."
                            )
                            continue
                    if (
                        self.api_mode == "chat_completions"
                        and self.provider == "nous"
                        and status_code == 401
                        and not nous_auth_retry_attempted
                    ):
                        nous_auth_retry_attempted = True
                        if self._try_refresh_nous_client_credentials(force=True):
                            print(
                                f"{self.log_prefix}🔐 Nous agent key refreshed after 401. Retrying request..."
                            )
                            continue
                    if (
                        self.api_mode == "anthropic_messages"
                        and status_code == 401
                        and hasattr(self, "_anthropic_api_key")
                        and not anthropic_auth_retry_attempted
                    ):
                        anthropic_auth_retry_attempted = True
                        from agent.providers.anthropic_adapter import _is_oauth_token

                        if self._try_refresh_anthropic_client_credentials():
                            print(
                                f"{self.log_prefix}🔐 Anthropic credentials refreshed after 401. Retrying request..."
                            )
                            continue
                        # Credential refresh didn't help — show diagnostic info
                        key = self._anthropic_api_key
                        auth_method = (
                            "Bearer (OAuth/setup-token)"
                            if _is_oauth_token(key)
                            else "x-api-key (API key)"
                        )
                        print(f"{self.log_prefix}🔐 Anthropic 401 — authentication failed.")
                        print(f"{self.log_prefix}   Auth method: {auth_method}")
                        credential_status = "configured" if key and len(key) > 12 else "missing"
                        print(f"{self.log_prefix}   Credential status: {credential_status}")
                        print(f"{self.log_prefix}   Troubleshooting:")
                        print(
                            f"{self.log_prefix}     • Check ANTHROPIC_TOKEN in ~/.leanflow/.env for LeanFlow-managed OAuth/setup tokens"
                        )
                        print(
                            f"{self.log_prefix}     • Check ANTHROPIC_API_KEY in ~/.leanflow/.env for API keys or legacy token values"
                        )
                        print(
                            f"{self.log_prefix}     • For API keys: verify at https://console.anthropic.com/settings/keys"
                        )
                        print(
                            f"{self.log_prefix}     • For Claude Code: run 'claude /login' to refresh, then retry"
                        )
                        print(
                            f'{self.log_prefix}     • Clear stale keys: leanflow config set ANTHROPIC_TOKEN ""'
                        )
                        print(
                            f'{self.log_prefix}     • Legacy cleanup: leanflow config set ANTHROPIC_API_KEY ""'
                        )

                    retry_count += 1
                    elapsed_time = time.time() - api_start_time

                    # Enhanced error logging
                    error_type = type(api_error).__name__
                    error_msg = str(api_error).lower()
                    logger.warning(
                        "API call failed (attempt %s/%s) error_type=%s %s error=%s",
                        retry_count,
                        max_retries,
                        error_type,
                        self._client_log_context(),
                        safe_api_error,
                    )

                    self._vprint(
                        f"{self.log_prefix}⚠️  API call failed (attempt {retry_count}/{max_retries}): {error_type}",
                        force=True,
                    )
                    self._vprint(
                        f"{self.log_prefix}   ⏱️  Time elapsed before failure: {elapsed_time:.2f}s"
                    )
                    self._vprint(
                        f"{self.log_prefix}   📝 Error: {safe_api_error[:200]}", force=True
                    )
                    self._vprint(
                        f"{self.log_prefix}   📊 Request context: {len(api_messages)} messages, ~{approx_tokens:,} tokens, {len(self.tools) if self.tools else 0} tools"
                    )

                    # Check for interrupt before deciding to retry
                    if self._interrupt_requested:
                        self._vprint(
                            f"{self.log_prefix}⚡ Interrupt detected during error handling, aborting retries.",
                            force=True,
                        )
                        self._persist_session(messages, conversation_history)
                        self.clear_interrupt()
                        return {
                            "final_response": f"Operation interrupted: handling API error ({error_type}: {safe_api_error[:80]}).",
                            "messages": messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "interrupted": True,
                        }

                    # Check for 413 payload-too-large BEFORE generic 4xx handler.
                    # A 413 is a payload-size error — the correct response is to
                    # compress history and retry, not abort immediately.
                    status_code = getattr(api_error, "status_code", None)
                    is_payload_too_large = (
                        status_code == 413
                        or "request entity too large" in error_msg
                        or "payload too large" in error_msg
                        or "error code: 413" in error_msg
                    )

                    if is_payload_too_large:
                        compression_attempts += 1
                        if compression_attempts > max_compression_attempts:
                            self._vprint(
                                f"{self.log_prefix}❌ Max compression attempts ({max_compression_attempts}) reached for payload-too-large error.",
                                force=True,
                            )
                            logging.error(
                                f"{self.log_prefix}413 compression failed after {max_compression_attempts} attempts."
                            )
                            self._persist_session(messages, conversation_history)
                            return {
                                "messages": messages,
                                "completed": False,
                                "api_calls": api_call_count,
                                "error": f"Request payload too large: max compression attempts ({max_compression_attempts}) reached.",
                                "partial": True,
                            }
                        self._vprint(
                            f"{self.log_prefix}⚠️  Request payload too large (413) — compression attempt {compression_attempts}/{max_compression_attempts}..."
                        )

                        original_len = len(messages)
                        messages, active_system_prompt = self._compress_context(
                            messages,
                            system_message,
                            approx_tokens=approx_tokens,
                            task_id=effective_task_id,
                        )

                        if len(messages) < original_len:
                            self._vprint(
                                f"{self.log_prefix}   🗜️  Compressed {original_len} → {len(messages)} messages, retrying..."
                            )
                            time.sleep(2)  # Brief pause between compression retries
                            restart_with_compressed_messages = True
                            break
                        else:
                            self._vprint(
                                f"{self.log_prefix}❌ Payload too large and cannot compress further.",
                                force=True,
                            )
                            logging.error(
                                f"{self.log_prefix}413 payload too large. Cannot compress further."
                            )
                            self._persist_session(messages, conversation_history)
                            return {
                                "messages": messages,
                                "completed": False,
                                "api_calls": api_call_count,
                                "error": "Request payload too large (413). Cannot compress further.",
                                "partial": True,
                            }

                    # Check for context-length errors BEFORE generic 4xx handler.
                    # Local backends (LM Studio, Ollama, llama.cpp) often return
                    # HTTP 400 with messages like "Context size has been exceeded"
                    # which must trigger compression, not an immediate abort.
                    is_context_length_error = any(
                        phrase in error_msg
                        for phrase in [
                            "context length",
                            "context size",
                            "maximum context",
                            "token limit",
                            "too many tokens",
                            "reduce the length",
                            "exceeds the limit",
                            "context window",
                            "request entity too large",  # OpenRouter/Nous 413 safety net
                            "prompt is too long",  # Anthropic: "prompt is too long: N tokens > M maximum"
                        ]
                    )

                    if is_context_length_error:
                        compressor = self.context_compressor
                        old_ctx = compressor.context_length

                        # Try to parse the actual limit from the error message
                        parsed_limit = parse_context_limit_from_error(error_msg)
                        if parsed_limit and parsed_limit < old_ctx:
                            new_ctx = parsed_limit
                            self._vprint(
                                f"{self.log_prefix}⚠️  Context limit detected from API: {new_ctx:,} tokens (was {old_ctx:,})",
                                force=True,
                            )
                        else:
                            # Step down to the next probe tier
                            new_ctx = get_next_probe_tier(old_ctx)

                        if new_ctx and new_ctx < old_ctx:
                            compressor.context_length = new_ctx
                            compressor.threshold_tokens = int(
                                new_ctx * compressor.threshold_percent
                            )
                            compressor._context_probed = True
                            self._vprint(
                                f"{self.log_prefix}⚠️  Context length exceeded — stepping down: {old_ctx:,} → {new_ctx:,} tokens",
                                force=True,
                            )
                        else:
                            self._vprint(
                                f"{self.log_prefix}⚠️  Context length exceeded at minimum tier — attempting compression...",
                                force=True,
                            )

                        compression_attempts += 1
                        if compression_attempts > max_compression_attempts:
                            self._vprint(
                                f"{self.log_prefix}❌ Max compression attempts ({max_compression_attempts}) reached.",
                                force=True,
                            )
                            logging.error(
                                f"{self.log_prefix}Context compression failed after {max_compression_attempts} attempts."
                            )
                            self._persist_session(messages, conversation_history)
                            return {
                                "messages": messages,
                                "completed": False,
                                "api_calls": api_call_count,
                                "error": f"Context length exceeded: max compression attempts ({max_compression_attempts}) reached.",
                                "partial": True,
                            }
                        self._vprint(
                            f"{self.log_prefix}   🗜️  Context compression attempt {compression_attempts}/{max_compression_attempts}..."
                        )

                        original_len = len(messages)
                        messages, active_system_prompt = self._compress_context(
                            messages,
                            system_message,
                            approx_tokens=approx_tokens,
                            task_id=effective_task_id,
                        )

                        if len(messages) < original_len or new_ctx and new_ctx < old_ctx:
                            if len(messages) < original_len:
                                self._vprint(
                                    f"{self.log_prefix}   🗜️  Compressed {original_len} → {len(messages)} messages, retrying..."
                                )
                            time.sleep(2)  # Brief pause between compression retries
                            restart_with_compressed_messages = True
                            break
                        else:
                            # Can't compress further and already at minimum tier
                            self._vprint(
                                f"{self.log_prefix}❌ Context length exceeded and cannot compress further.",
                                force=True,
                            )
                            self._vprint(
                                f"{self.log_prefix}   💡 The conversation has accumulated too much content.",
                                force=True,
                            )
                            logging.error(
                                f"{self.log_prefix}Context length exceeded: {approx_tokens:,} tokens. Cannot compress further."
                            )
                            self._persist_session(messages, conversation_history)
                            return {
                                "messages": messages,
                                "completed": False,
                                "api_calls": api_call_count,
                                "error": f"Context length exceeded ({approx_tokens:,} tokens). Cannot compress further.",
                                "partial": True,
                            }

                    # Check for non-retryable client errors (4xx HTTP status codes).
                    # These indicate a problem with the request itself (bad model ID,
                    # invalid API key, forbidden, etc.) and will never succeed on retry.
                    # Note: 413 and context-length errors are excluded — handled above.
                    # Also catch local validation errors (ValueError, TypeError) — these
                    # are programming bugs, not transient failures.
                    is_local_validation_error = isinstance(api_error, (ValueError, TypeError))
                    is_client_status_error = (
                        isinstance(status_code, int)
                        and 400 <= status_code < 500
                        and status_code != 413
                    )
                    is_client_error = (
                        is_local_validation_error
                        or is_client_status_error
                        or any(
                            phrase in error_msg
                            for phrase in [
                                "error code: 401",
                                "error code: 403",
                                "error code: 404",
                                "error code: 422",
                                "is not a valid model",
                                "invalid model",
                                "model not found",
                                "invalid api key",
                                "invalid_api_key",
                                "authentication",
                                "unauthorized",
                                "forbidden",
                                "not found",
                            ]
                        )
                    ) and not is_context_length_error

                    if is_client_error:
                        # Try fallback before aborting — a different provider
                        # may not have the same issue (rate limit, auth, etc.)
                        if self._try_activate_fallback():
                            retry_count = 0
                            continue
                        self._dump_api_request_debug(
                            api_kwargs,
                            reason="non_retryable_client_error",
                            error=api_error,
                        )
                        self._vprint(
                            f"{self.log_prefix}❌ Non-retryable client error detected. Aborting immediately.",
                            force=True,
                        )
                        self._vprint(
                            f"{self.log_prefix}   💡 This type of error won't be fixed by retrying.",
                            force=True,
                        )
                        logging.error(
                            f"{self.log_prefix}Non-retryable client error: {safe_api_error}"
                        )
                        self._persist_session(messages, conversation_history)
                        return {
                            "final_response": None,
                            "messages": messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "failed": True,
                            "error": safe_api_error,
                        }

                    if retry_count >= max_retries:
                        # Try fallback before giving up entirely
                        if self._try_activate_fallback():
                            retry_count = 0
                            continue
                        self._vprint(
                            f"{self.log_prefix}❌ Provider attempts exhausted ({max_retries} attempts, 3 retries). Pausing.",
                            force=True,
                        )
                        _emit_workflow_event(
                            "provider-retry-exhausted",
                            "Transient provider retries exhausted",
                            **_workflow_agent_event_details(
                                self,
                                failed_attempt=retry_count,
                                max_attempts=max_retries,
                                retries=retry_count - 1,
                                error_type=error_type,
                                error=safe_api_error[:300],
                            ),
                        )
                        logging.error(
                            f"{self.log_prefix}API call failed after {max_retries} attempts. Last error: {safe_api_error}"
                        )
                        logging.error(
                            f"{self.log_prefix}Request details - Messages: {len(api_messages)}, Approx tokens: {approx_tokens:,}"
                        )
                        raise TransientProviderRetriesExhausted(api_error) from api_error

                    provider_recovery_deadline_monotonic = (
                        transient_provider_recovery_deadline_monotonic(
                            current_deadline_monotonic=provider_recovery_deadline_monotonic,
                            conversation_deadline_monotonic=self._conversation_deadline_monotonic,
                        )
                    )
                    self._transient_provider_recovery_deadline_monotonic = (
                        provider_recovery_deadline_monotonic
                    )
                    wait_time = transient_provider_retry_delay_within_deadline_s(
                        retry_count,
                        deadline_monotonic=provider_recovery_deadline_monotonic,
                    )
                    if wait_time is None:
                        _emit_workflow_event(
                            "provider-retry-skipped-deadline",
                            "Skipped provider retry because the recovery deadline is exhausted",
                            **_workflow_agent_event_details(
                                self,
                                failed_attempt=retry_count,
                                max_attempts=max_retries,
                                error_type=error_type,
                                error=safe_api_error[:300],
                            ),
                        )
                        raise TransientProviderRetriesExhausted(api_error) from api_error
                    logger.warning(
                        "Retrying API call in %ss (attempt %s/%s) %s error=%s",
                        wait_time,
                        retry_count,
                        max_retries,
                        self._client_log_context(),
                        safe_api_error,
                    )
                    self._vprint(
                        f"{self.log_prefix}⏳ Provider retry {retry_count}/3 in {wait_time:g}s...",
                        force=True,
                    )
                    _emit_workflow_event(
                        "provider-retry-scheduled",
                        f"Provider retry {retry_count}/3 scheduled in {wait_time:g}s",
                        **_workflow_agent_event_details(
                            self,
                            failed_attempt=retry_count,
                            max_attempts=max_retries,
                            retry_number=retry_count,
                            wait_seconds=wait_time,
                            provider_recovery_remaining_s=round(
                                max(
                                    0.0,
                                    provider_recovery_deadline_monotonic - time.monotonic(),
                                ),
                                3,
                            ),
                            error_type=error_type,
                            error=safe_api_error[:300],
                        ),
                    )

                    # Sleep in small increments so we can respond to interrupts quickly
                    # instead of blocking the entire wait_time in one sleep() call
                    sleep_end = time.time() + wait_time
                    while time.time() < sleep_end:
                        if self._interrupt_requested:
                            self._vprint(
                                f"{self.log_prefix}⚡ Interrupt detected during retry wait, aborting.",
                                force=True,
                            )
                            self._persist_session(messages, conversation_history)
                            self.clear_interrupt()
                            return {
                                "final_response": f"Operation interrupted: retrying API call after error (retry {retry_count}/{max_retries}).",
                                "messages": messages,
                                "api_calls": api_call_count,
                                "completed": False,
                                "interrupted": True,
                            }
                        time.sleep(0.2)  # Check interrupt every 200ms

            # If the API call was interrupted, skip response processing
            if interrupted:
                break

            if restart_with_compressed_messages:
                api_call_count -= 1
                self._current_run_api_calls = api_call_count
                self.iteration_budget.refund()
                continue

            if restart_with_length_continuation:
                continue

            # Guard: if all retries exhausted without a successful response
            # (e.g. repeated context-length errors that exhausted retry_count),
            # the `response` variable is still None. Break out cleanly.
            if response is None:
                print(f"{self.log_prefix}❌ All API retries exhausted with no successful response.")
                self._persist_session(messages, conversation_history)
                break

            try:
                if self.api_mode == "codex_responses":
                    assistant_message, finish_reason = self._normalize_codex_response(response)
                elif self.api_mode == "anthropic_messages":
                    from agent.providers.anthropic_adapter import normalize_anthropic_response

                    assistant_message, finish_reason = normalize_anthropic_response(response)
                else:
                    assistant_message = response.choices[0].message

                # Normalize content to string — some OpenAI-compatible servers
                # (llama-server, etc.) return content as a dict or list instead
                # of a plain string, which crashes downstream .strip() calls.
                if assistant_message.content is not None and not isinstance(
                    assistant_message.content, str
                ):
                    raw = assistant_message.content
                    if isinstance(raw, dict):
                        assistant_message.content = (
                            raw.get("text", "") or raw.get("content", "") or json.dumps(raw)
                        )
                    elif isinstance(raw, list):
                        # Multimodal content list — extract text parts
                        parts = []
                        for part in raw:
                            if isinstance(part, str):
                                parts.append(part)
                            elif isinstance(part, dict) and part.get("type") == "text":
                                parts.append(part.get("text", ""))
                            elif isinstance(part, dict) and "text" in part:
                                parts.append(str(part["text"]))
                        assistant_message.content = "\n".join(parts)
                    else:
                        assistant_message.content = str(raw)

                reasoning_preview_lines = self._reasoning_preview_lines(
                    self._extract_reasoning(assistant_message),
                    max_lines=max(self.log_preview_lines, 1),
                    max_chars=self.log_preview_chars,
                )

                # Handle assistant response
                if assistant_message.content and not self.quiet_mode:
                    if self.verbose_logging:
                        self._vprint(f"\n{self.log_prefix}┌─ Agent")
                        for line in (assistant_message.content or "").splitlines() or [""]:
                            self._vprint(f"{self.log_prefix}│  {line}")
                        if reasoning_preview_lines:
                            self._vprint(f"{self.log_prefix}│  ")
                            self._vprint(f"{self.log_prefix}│  Reasoning preview:")
                            for line in reasoning_preview_lines:
                                self._vprint(f"{self.log_prefix}│    {line}")
                        self._vprint(f"{self.log_prefix}└─")
                    else:
                        preview_lines = [
                            line.strip()
                            for line in (assistant_message.content or "").splitlines()
                            if line.strip()
                        ]
                        if not preview_lines:
                            preview_lines = [""]
                        preview_text = "\n".join(preview_lines[: self.log_preview_lines])
                        if len(preview_text) > self.log_preview_chars:
                            preview_text = preview_text[: self.log_preview_chars - 3] + "..."
                        self._vprint(f"\n{self.log_prefix}┌─ Agent")
                        for line in preview_text.splitlines():
                            self._vprint(f"{self.log_prefix}│  {line}")
                        if reasoning_preview_lines:
                            self._vprint(f"{self.log_prefix}│  ")
                            self._vprint(f"{self.log_prefix}│  Reasoning preview:")
                            for line in reasoning_preview_lines:
                                self._vprint(f"{self.log_prefix}│    {line}")
                        self._vprint(f"{self.log_prefix}└─")
                elif reasoning_preview_lines and not self.quiet_mode:
                    self._vprint(f"\n{self.log_prefix}┌─ Agent")
                    self._vprint(f"{self.log_prefix}│  Reasoning preview:")
                    for line in reasoning_preview_lines:
                        self._vprint(f"{self.log_prefix}│    {line}")
                    self._vprint(f"{self.log_prefix}└─")

                # Notify progress callback of model's thinking (used by subagent
                # delegation to relay the child's reasoning to the parent display).
                # Guard: only fire for subagents (_delegate_depth >= 1) to avoid
                # spamming gateway platforms with the main agent's every thought.
                if (
                    assistant_message.content
                    and self.tool_progress_callback
                    and getattr(self, "_delegate_depth", 0) > 0
                ):
                    _think_text = assistant_message.content.strip()
                    # Strip reasoning XML tags that shouldn't leak to parent display
                    _think_text = re.sub(
                        r"</?(?:REASONING_SCRATCHPAD|think|reasoning)>", "", _think_text
                    ).strip()
                    first_line = _think_text.split("\n")[0][:80] if _think_text else ""
                    if first_line:
                        with contextlib.suppress(Exception):
                            self.tool_progress_callback("_thinking", first_line)

                _emit_workflow_event(
                    "assistant-response",
                    "Assistant response received",
                    **_workflow_agent_event_details(
                        self,
                        iteration=api_call_count,
                        finish_reason=finish_reason,
                        content=assistant_message.content or "",
                        reasoning=getattr(assistant_message, "reasoning", None),
                        reasoning_content=getattr(assistant_message, "reasoning_content", None),
                        tool_calls=[
                            {
                                "id": getattr(tc, "id", ""),
                                "name": getattr(getattr(tc, "function", None), "name", ""),
                                "arguments": getattr(
                                    getattr(tc, "function", None), "arguments", ""
                                ),
                            }
                            for tc in (assistant_message.tool_calls or [])
                        ],
                        usage=usage_dict if "usage_dict" in locals() else {},
                        response_model=getattr(response, "model", ""),
                    ),
                )

                # Check for incomplete <REASONING_SCRATCHPAD> (opened but never closed)
                # This means the model ran out of output tokens mid-reasoning — retry up to 2 times
                if has_incomplete_scratchpad(assistant_message.content or ""):
                    if not hasattr(self, "_incomplete_scratchpad_retries"):
                        self._incomplete_scratchpad_retries = 0
                    self._incomplete_scratchpad_retries += 1

                    self._vprint(
                        f"{self.log_prefix}⚠️  Incomplete <REASONING_SCRATCHPAD> detected (opened but never closed)"
                    )

                    if self._incomplete_scratchpad_retries <= 2:
                        self._vprint(
                            f"{self.log_prefix}🔄 Retrying API call ({self._incomplete_scratchpad_retries}/2)..."
                        )
                        # Don't add the broken message, just retry
                        continue
                    else:
                        # Max retries - discard this turn and save as partial
                        self._vprint(
                            f"{self.log_prefix}❌ Max retries (2) for incomplete scratchpad. Saving as partial.",
                            force=True,
                        )
                        self._incomplete_scratchpad_retries = 0

                        rolled_back_messages = self._get_messages_up_to_last_assistant(messages)
                        self._cleanup_task_resources(effective_task_id)
                        self._persist_session(messages, conversation_history)

                        return {
                            "final_response": None,
                            "messages": rolled_back_messages,
                            "api_calls": api_call_count,
                            "completed": False,
                            "partial": True,
                            "error": "Incomplete REASONING_SCRATCHPAD after 2 retries",
                        }

                # Reset incomplete scratchpad counter on clean response
                if hasattr(self, "_incomplete_scratchpad_retries"):
                    self._incomplete_scratchpad_retries = 0

                if self.api_mode == "codex_responses" and finish_reason == "incomplete":
                    if not hasattr(self, "_codex_incomplete_retries"):
                        self._codex_incomplete_retries = 0
                    self._codex_incomplete_retries += 1

                    interim_msg = self._build_assistant_message(assistant_message, finish_reason)
                    interim_has_content = bool((interim_msg.get("content") or "").strip())
                    interim_has_reasoning = (
                        bool(interim_msg.get("reasoning", "").strip())
                        if isinstance(interim_msg.get("reasoning"), str)
                        else False
                    )

                    if interim_has_content or interim_has_reasoning:
                        last_msg = messages[-1] if messages else None
                        duplicate_interim = (
                            isinstance(last_msg, dict)
                            and last_msg.get("role") == "assistant"
                            and last_msg.get("finish_reason") == "incomplete"
                            and (last_msg.get("content") or "")
                            == (interim_msg.get("content") or "")
                            and (last_msg.get("reasoning") or "")
                            == (interim_msg.get("reasoning") or "")
                        )
                        if not duplicate_interim:
                            messages.append(interim_msg)

                    if self._codex_incomplete_retries < 3:
                        if not self.quiet_mode:
                            self._vprint(
                                f"{self.log_prefix}↻ Codex response incomplete; continuing turn ({self._codex_incomplete_retries}/3)"
                            )
                        self._session_messages = messages
                        self._save_session_log(messages)
                        continue

                    self._codex_incomplete_retries = 0
                    self._persist_session(messages, conversation_history)
                    return {
                        "final_response": None,
                        "messages": messages,
                        "api_calls": api_call_count,
                        "completed": False,
                        "partial": True,
                        "error": "Codex response remained incomplete after 3 continuation attempts",
                    }
                elif hasattr(self, "_codex_incomplete_retries"):
                    self._codex_incomplete_retries = 0

                # Check for tool calls
                if assistant_message.tool_calls:
                    if not self.quiet_mode:
                        self._vprint(
                            f"\n{self.log_prefix}🔧 Processing {len(assistant_message.tool_calls)} tool call(s)..."
                        )
                    _emit_workflow_event(
                        "tool-call-batch",
                        f"Processing {len(assistant_message.tool_calls)} tool call(s)",
                        **_workflow_agent_event_details(
                            self,
                            iteration=api_call_count,
                            tool_calls=[
                                {
                                    "id": getattr(tc, "id", ""),
                                    "name": getattr(getattr(tc, "function", None), "name", ""),
                                    "arguments": getattr(
                                        getattr(tc, "function", None), "arguments", ""
                                    ),
                                }
                                for tc in assistant_message.tool_calls
                            ],
                        ),
                    )

                    if self.verbose_logging:
                        for tc in assistant_message.tool_calls:
                            logging.debug(
                                f"Tool call: {tc.function.name} with args: {tc.function.arguments[:200]}..."
                            )

                    # Validate tool call names - detect model hallucinations
                    # Repair mismatched tool names before validating
                    for tc in assistant_message.tool_calls:
                        if tc.function.name not in self.valid_tool_names:
                            repaired = self._repair_tool_call(tc.function.name)
                            if repaired:
                                print(
                                    f"{self.log_prefix}🔧 Auto-repaired tool name: '{tc.function.name}' -> '{repaired}'"
                                )
                                tc.function.name = repaired
                    invalid_tool_calls = [
                        tc.function.name
                        for tc in assistant_message.tool_calls
                        if tc.function.name not in self.valid_tool_names
                    ]
                    if invalid_tool_calls:
                        # Track retries for invalid tool calls
                        if not hasattr(self, "_invalid_tool_retries"):
                            self._invalid_tool_retries = 0
                        self._invalid_tool_retries += 1

                        # Return helpful error to model — model can self-correct next turn
                        available = ", ".join(sorted(self.valid_tool_names))
                        invalid_name = invalid_tool_calls[0]
                        invalid_preview = (
                            invalid_name[:80] + "..." if len(invalid_name) > 80 else invalid_name
                        )
                        self._vprint(
                            f"{self.log_prefix}⚠️  Unknown tool '{invalid_preview}' — sending error to model for self-correction ({self._invalid_tool_retries}/3)"
                        )

                        if self._invalid_tool_retries >= 3:
                            self._vprint(
                                f"{self.log_prefix}❌ Max retries (3) for invalid tool calls exceeded. Stopping as partial.",
                                force=True,
                            )
                            self._invalid_tool_retries = 0
                            self._persist_session(messages, conversation_history)
                            return {
                                "final_response": None,
                                "messages": messages,
                                "api_calls": api_call_count,
                                "completed": False,
                                "partial": True,
                                "error": f"Model generated invalid tool call: {invalid_preview}",
                            }

                        assistant_msg = self._build_assistant_message(
                            assistant_message, finish_reason
                        )
                        messages.append(assistant_msg)
                        for tc in assistant_message.tool_calls:
                            if tc.function.name not in self.valid_tool_names:
                                content = f"Tool '{tc.function.name}' does not exist. Available tools: {available}"
                            else:
                                content = "Skipped: another tool call in this turn used an invalid name. Please retry this tool call."
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc.id,
                                    "content": content,
                                }
                            )
                        continue
                    # Reset retry counter on successful tool call validation
                    if hasattr(self, "_invalid_tool_retries"):
                        self._invalid_tool_retries = 0

                    # Validate tool call arguments are valid JSON
                    # Handle empty strings as empty objects (common model quirk)
                    invalid_json_args = []
                    for tc in assistant_message.tool_calls:
                        args = tc.function.arguments
                        if isinstance(args, (dict, list)):
                            tc.function.arguments = json.dumps(args)
                            continue
                        if args is not None and not isinstance(args, str):
                            tc.function.arguments = str(args)
                            args = tc.function.arguments
                        # Treat empty/whitespace strings as empty object
                        if not args or not args.strip():
                            tc.function.arguments = "{}"
                            continue
                        try:
                            json.loads(args)
                        except json.JSONDecodeError as e:
                            invalid_json_args.append((tc.function.name, str(e)))

                    if invalid_json_args:
                        # Track retries for invalid JSON arguments
                        self._invalid_json_retries += 1

                        tool_name, error_msg = invalid_json_args[0]
                        self._vprint(
                            f"{self.log_prefix}⚠️  Invalid JSON in tool call arguments for '{tool_name}': {error_msg}"
                        )

                        if self._invalid_json_retries < 3:
                            self._vprint(
                                f"{self.log_prefix}🔄 Retrying API call ({self._invalid_json_retries}/3)..."
                            )
                            # Don't add anything to messages, just retry the API call
                            continue
                        else:
                            # Instead of returning partial, inject a helpful message and let model recover
                            self._vprint(
                                f"{self.log_prefix}⚠️  Injecting recovery message for invalid JSON..."
                            )
                            self._invalid_json_retries = 0  # Reset for next attempt

                            # Add a user message explaining the issue
                            recovery_msg = (
                                f"Your tool call to '{tool_name}' had invalid JSON arguments. "
                                f"Error: {error_msg}. "
                                f"For tools with no required parameters, use an empty object: {{}}. "
                                f"Please either retry the tool call with valid JSON, or respond without using that tool."
                            )
                            recovery_dict = {"role": "user", "content": recovery_msg}
                            messages.append(recovery_dict)
                            continue

                    # Reset retry counter on successful JSON validation
                    self._invalid_json_retries = 0

                    _tc_names = {tc.function.name for tc in assistant_message.tool_calls}
                    _advisor_tool_names = {"lean_reasoning_help", "lean_decompose_helpers"}
                    _requested_advisor_tools = _tc_names & _advisor_tool_names
                    advisor_suffix_start = None
                    if _requested_advisor_tools and self._advisor_precompression_admitted(
                        _requested_advisor_tools
                    ):
                        messages, active_system_prompt = (
                            self._maybe_precompress_before_advisor_tool(
                                messages,
                                system_message,
                                active_system_prompt,
                                effective_task_id=effective_task_id,
                            )
                        )
                        advisor_suffix_start = len(messages)

                    assistant_msg = self._build_assistant_message(assistant_message, finish_reason)

                    # If this turn has both content AND tool_calls, capture the content
                    # as a fallback final response. Common pattern: model delivers its
                    # answer and calls memory/skill tools as a side-effect in the same
                    # turn. If the follow-up turn after tools is empty, we use this.
                    turn_content = assistant_message.content or ""
                    if turn_content and self._has_content_after_think_block(turn_content):
                        self._last_content_with_tools = turn_content
                        # Show intermediate commentary so the user can follow along
                        if self.quiet_mode:
                            clean = self._strip_think_blocks(turn_content).strip()
                            if clean:
                                self._vprint(f"  ┊ 💬 {clean}")

                    messages.append(assistant_msg)

                    _msg_count_before_tools = len(messages)
                    self._execute_tool_calls(
                        assistant_message, messages, effective_task_id, api_call_count
                    )
                    _post_tool_tail_started = time.monotonic()

                    # Refund the iteration if the ONLY tool(s) called were
                    # execute_code (programmatic tool calling).  These are
                    # cheap RPC-style calls that shouldn't eat the budget.
                    if _tc_names == {"execute_code"}:
                        self.iteration_budget.refund()
                    if _tc_names & _advisor_tool_names:
                        refreshed_count = self._maybe_refresh_api_step_budget_after_advisor(
                            api_call_count
                        )
                        if refreshed_count != api_call_count:
                            api_call_count = refreshed_count
                            self._current_run_api_calls = api_call_count

                    # Estimate next prompt size using real token counts from the
                    # last API response + rough estimate of newly appended tool
                    # results.  This catches cases where tool results push the
                    # context past the limit that last_prompt_tokens alone misses
                    # (e.g. large file reads, web extractions).
                    _compressor = self.context_compressor
                    _next_prompt_estimate_started = time.monotonic()
                    _new_tool_msgs = messages[_msg_count_before_tools:]
                    _new_chars = sum(len(str(m.get("content", "") or "")) for m in _new_tool_msgs)
                    _estimated_next_prompt = (
                        _compressor.last_prompt_tokens
                        + _compressor.last_completion_tokens
                        + _new_chars // 3  # conservative: JSON-heavy tool results ≈ 3 chars/token
                    )
                    _next_prompt_estimate_elapsed = max(
                        0.0, time.monotonic() - _next_prompt_estimate_started
                    )
                    _compression_triggered = (
                        self.compression_enabled
                        and _compressor.should_compress(_estimated_next_prompt)
                    )
                    _compression_started = time.monotonic()
                    if _compression_triggered:
                        if advisor_suffix_start is not None:
                            messages, active_system_prompt = (
                                self._compress_context_preserving_suffix(
                                    messages,
                                    advisor_suffix_start,
                                    system_message,
                                    approx_tokens=_estimated_next_prompt,
                                    task_id=effective_task_id,
                                )
                            )
                        else:
                            messages, active_system_prompt = self._compress_context(
                                messages,
                                system_message,
                                approx_tokens=_estimated_next_prompt,
                                task_id=effective_task_id,
                            )
                    _compression_elapsed = max(0.0, time.monotonic() - _compression_started)

                    # Save session log incrementally (so progress is visible even if interrupted)
                    self._session_messages = messages
                    _session_log_started = time.monotonic()
                    self._save_session_log(messages)
                    _session_log_elapsed = max(0.0, time.monotonic() - _session_log_started)
                    _post_tool_tail_elapsed = max(0.0, time.monotonic() - _post_tool_tail_started)
                    if _post_tool_tail_elapsed >= _POST_TOOL_TAIL_SLOW_THRESHOLD_S:
                        _emit_workflow_event(
                            "post-tool-tail-slow",
                            f"Post-tool turn preparation took {_post_tool_tail_elapsed:.1f}s",
                            **_workflow_agent_event_details(
                                self,
                                iteration=api_call_count,
                                tools=sorted(_tc_names),
                                elapsed_s=round(_post_tool_tail_elapsed, 3),
                                compression_triggered=_compression_triggered,
                                estimated_next_prompt_tokens=_estimated_next_prompt,
                                phase_seconds={
                                    "next_prompt_estimate": round(_next_prompt_estimate_elapsed, 3),
                                    "compression": round(_compression_elapsed, 3),
                                    "session_log": round(_session_log_elapsed, 3),
                                },
                            ),
                        )

                    # Continue loop for next response
                    continue

                else:
                    # No tool calls - this is the final response
                    final_response = assistant_message.content or ""

                    # Check if response only has think block with no actual content after it
                    if not self._has_content_after_think_block(final_response):
                        # If the previous turn already delivered real content alongside
                        # tool calls (e.g. "You're welcome!" + memory save), the model
                        # has nothing more to say. Use the earlier content immediately
                        # instead of wasting API calls on retries that won't help.
                        fallback = getattr(self, "_last_content_with_tools", None)
                        if fallback:
                            logger.debug(
                                "Empty follow-up after tool calls — using prior turn content as final response"
                            )
                            self._last_content_with_tools = None
                            self._empty_content_retries = 0
                            for i in range(len(messages) - 1, -1, -1):
                                msg = messages[i]
                                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                                    tool_names = []
                                    for tc in msg["tool_calls"]:
                                        fn = tc.get("function", {})
                                        tool_names.append(fn.get("name", "unknown"))
                                    msg["content"] = (
                                        f"Calling the {', '.join(tool_names)} tool{'s' if len(tool_names) > 1 else ''}..."
                                    )
                                    break
                            final_response = self._strip_think_blocks(fallback).strip()
                            self._response_was_previewed = True
                            break

                        # No fallback available — this is a genuine empty response.
                        # Retry in case the model just had a bad generation.
                        if not hasattr(self, "_empty_content_retries"):
                            self._empty_content_retries = 0
                        self._empty_content_retries += 1

                        reasoning_text = self._extract_reasoning(assistant_message)
                        self._vprint(
                            f"{self.log_prefix}⚠️  Response only contains think block with no content after it"
                        )
                        if reasoning_text:
                            reasoning_preview = (
                                reasoning_text[:500] + "..."
                                if len(reasoning_text) > 500
                                else reasoning_text
                            )
                            self._vprint(f"{self.log_prefix}   Reasoning: {reasoning_preview}")
                        else:
                            content_preview = (
                                final_response[:80] + "..."
                                if len(final_response) > 80
                                else final_response
                            )
                            self._vprint(f"{self.log_prefix}   Content: '{content_preview}'")

                        if self._empty_content_retries < 3:
                            self._vprint(
                                f"{self.log_prefix}🔄 Retrying API call ({self._empty_content_retries}/3)..."
                            )
                            continue
                        else:
                            self._vprint(
                                f"{self.log_prefix}❌ Max retries (3) for empty content exceeded.",
                                force=True,
                            )
                            self._empty_content_retries = 0

                            # If a prior tool_calls turn had real content, salvage it:
                            # rewrite that turn's content to a brief tool description,
                            # and use the original content as the final response here.
                            fallback = getattr(self, "_last_content_with_tools", None)
                            if fallback:
                                self._last_content_with_tools = None
                                # Find the last assistant message with tool_calls and rewrite it
                                for i in range(len(messages) - 1, -1, -1):
                                    msg = messages[i]
                                    if msg.get("role") == "assistant" and msg.get("tool_calls"):
                                        tool_names = []
                                        for tc in msg["tool_calls"]:
                                            fn = tc.get("function", {})
                                            tool_names.append(fn.get("name", "unknown"))
                                        msg["content"] = (
                                            f"Calling the {', '.join(tool_names)} tool{'s' if len(tool_names) > 1 else ''}..."
                                        )
                                        break
                                # Strip <think> blocks from fallback content for user display
                                final_response = self._strip_think_blocks(fallback).strip()
                                self._response_was_previewed = True
                                break

                            # No fallback -- append the empty message as-is
                            empty_msg = {
                                "role": "assistant",
                                "content": final_response,
                                "reasoning": reasoning_text,
                                "finish_reason": finish_reason,
                            }
                            messages.append(empty_msg)

                            self._cleanup_task_resources(effective_task_id)
                            self._persist_session(messages, conversation_history)

                            return {
                                "final_response": final_response or None,
                                "messages": messages,
                                "api_calls": api_call_count,
                                "completed": False,
                                "partial": True,
                                "error": "Model generated only think blocks with no actual response after 3 retries",
                            }

                    # Reset retry counter on successful content
                    if hasattr(self, "_empty_content_retries"):
                        self._empty_content_retries = 0

                    if (
                        self.api_mode == "codex_responses"
                        and self.valid_tool_names
                        and codex_ack_continuations < 2
                        and self._looks_like_codex_intermediate_ack(
                            user_message=user_message,
                            assistant_content=final_response,
                            messages=messages,
                        )
                    ):
                        codex_ack_continuations += 1
                        interim_msg = self._build_assistant_message(assistant_message, "incomplete")
                        messages.append(interim_msg)

                        continue_msg = {
                            "role": "user",
                            "content": (
                                "[System: Continue now. Execute the required tool calls and only "
                                "send your final answer after completing the task.]"
                            ),
                        }
                        messages.append(continue_msg)
                        self._session_messages = messages
                        self._save_session_log(messages)
                        continue

                    codex_ack_continuations = 0

                    if truncated_response_prefix:
                        final_response = truncated_response_prefix + final_response

                    # Strip <think> blocks from user-facing response (keep raw in messages for trajectory)
                    final_response = self._strip_think_blocks(final_response).strip()

                    final_msg = self._build_assistant_message(assistant_message, finish_reason)

                    messages.append(final_msg)

                    if not self.quiet_mode:
                        print(
                            f"📋 Agent ended its turn after {api_call_count} API call(s) "
                            "— final message had no tool calls. This ends the turn, not "
                            "necessarily the task; the workflow verification gate decides success."
                        )
                    break

            except Exception as e:
                error_msg = f"Error during OpenAI-compatible API call #{api_call_count}: {str(e)}"
                print(f"❌ {error_msg}")

                if self.verbose_logging:
                    logging.exception("Detailed error information:")

                # If an assistant message with tool_calls was already appended,
                # the API expects a role="tool" result for every tool_call_id.
                # Fill in error results for any that weren't answered yet.
                pending_handled = False
                for idx in range(len(messages) - 1, -1, -1):
                    msg = messages[idx]
                    if not isinstance(msg, dict):
                        break
                    if msg.get("role") == "tool":
                        continue
                    if msg.get("role") == "assistant" and msg.get("tool_calls"):
                        answered_ids = {
                            m["tool_call_id"]
                            for m in messages[idx + 1 :]
                            if isinstance(m, dict) and m.get("role") == "tool"
                        }
                        for tc in msg["tool_calls"]:
                            if tc["id"] not in answered_ids:
                                err_msg = {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": f"Error executing tool: {error_msg}",
                                }
                                messages.append(err_msg)
                        pending_handled = True
                    break

                if not pending_handled:
                    # Error happened before tool processing (e.g. response parsing).
                    # Use a user-role message so the model can see what went wrong
                    # without confusing the API with a fabricated assistant turn.
                    sys_err_msg = {
                        "role": "user",
                        "content": f"[System error during processing: {error_msg}]",
                    }
                    messages.append(sys_err_msg)

                # If we're near the limit, break to avoid infinite loops
                if api_call_count >= self.max_iterations - 1:
                    final_response = f"I apologize, but I encountered repeated errors: {error_msg}"
                    break

        if final_response is None and (
            api_call_count >= self.max_iterations or self.iteration_budget.remaining <= 0
        ):
            if self.iteration_budget.remaining <= 0 and not self.quiet_mode:
                print(
                    f"\n⚠️  Session iteration budget exhausted ({self.iteration_budget.used}/{self.iteration_budget.max_total} used, including subagents)"
                )
            final_response = self._handle_max_iterations(messages, api_call_count)

        # Determine if conversation completed successfully
        completed = final_response is not None and api_call_count < self.max_iterations
        if self._conversation_wall_timeout_reached:
            completed = False
            exit_reason = "wall_timeout"
        elif completed:
            exit_reason = "completed"
        elif interrupted:
            exit_reason = "interrupted"
        elif api_call_count >= self.max_iterations:
            exit_reason = "max_iterations"
        elif self.iteration_budget.remaining <= 0:
            exit_reason = "iteration_budget_exhausted"
        else:
            exit_reason = "partial"

        # Save trajectory if enabled
        self._save_trajectory(messages, user_message, completed)

        # Clean up VM and browser for this task after conversation completes
        self._cleanup_task_resources(effective_task_id)

        # Persist session to both JSON log and SQLite
        self._persist_session(messages, conversation_history)

        # Extract reasoning from the last assistant message (if any)
        last_reasoning = None
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                last_reasoning = msg["reasoning"]
                break

        # Build result with interrupt info if applicable
        result = {
            "final_response": final_response,
            "last_reasoning": last_reasoning,
            "messages": messages,
            "api_calls": api_call_count,
            "usage": self._session_usage_summary(),
            "completed": completed,
            "exit_reason": exit_reason,
            "partial": False,  # True only when stopped due to invalid tool calls
            "interrupted": interrupted,
            "wall_timed_out": self._conversation_wall_timeout_reached,
            "response_previewed": getattr(self, "_response_was_previewed", False),
        }
        self._response_was_previewed = False

        # Include interrupt message if one triggered the interrupt
        if interrupted and self._interrupt_message:
            result["interrupt_message"] = self._interrupt_message

        _emit_workflow_event(
            "conversation-end",
            "Agent conversation finished",
            **_workflow_agent_event_details(
                self,
                completed=completed,
                interrupted=interrupted,
                exit_reason=exit_reason,
                api_calls=api_call_count,
                usage=result["usage"],
                final_response=final_response,
                response_previewed=getattr(self, "_response_was_previewed", False),
                message_count=len(messages),
            ),
        )

        # Clear interrupt state after handling
        self.clear_interrupt()

        # Clear stream callback so it doesn't leak into future calls
        self._stream_callback = None

        return result

    def chat(self, message: str, stream_callback: Callable | None = None) -> str:
        """
        Simple chat interface that returns just the final response.

        Args:
            message (str): User message
            stream_callback: Optional callback invoked with each text delta during streaming.

        Returns:
            str: Final assistant response
        """
        result = self.run_conversation(message, stream_callback=stream_callback)
        return result["final_response"]


def main(
    query: str = None,
    model: str = "anthropic/claude-opus-4.6",
    api_key: str = None,
    base_url: str = "https://openrouter.ai/api/v1",
    max_turns: int = 10,
    enabled_toolsets: str = None,
    disabled_toolsets: str = None,
    list_tools: bool = False,
    save_trajectories: bool = False,
    save_sample: bool = False,
    verbose: bool = False,
    log_prefix_chars: int = 20,
):
    """
    Main function for running the agent directly.

    Args:
        query (str): Natural language query for the agent. Defaults to Python 3.13 example.
        model (str): Model name to use (OpenRouter format: provider/model). Defaults to anthropic/claude-sonnet-4.6.
        api_key (str): API key for authentication. Uses OPENROUTER_API_KEY env var if not provided.
        base_url (str): Base URL for the model API. Defaults to https://openrouter.ai/api/v1
        max_turns (int): Maximum number of API call iterations. Defaults to 10.
        enabled_toolsets (str): Comma-separated list of toolsets to enable. Supports predefined
                              toolsets (e.g., "autoformalize", "web", "file", "browser").
                              Multiple toolsets can be combined: "web,file"
        disabled_toolsets (str): Comma-separated list of toolsets to disable (e.g., "browser")
        list_tools (bool): Just list available tools and exit
        save_trajectories (bool): Save conversation trajectories to JSONL files (appends to trajectory_samples.jsonl). Defaults to False.
        save_sample (bool): Save a single trajectory sample to a UUID-named JSONL file for inspection. Defaults to False.
        verbose (bool): Enable verbose logging for debugging. Defaults to False.
        log_prefix_chars (int): Number of characters to show in log previews for tool calls/responses. Defaults to 20.

    Toolset Examples:
        - "autoformalize": Minimal LeanFlow workflow with file and web tools
    """
    print("🤖 AI Agent with Tool Calling")
    print("=" * 50)

    # Handle tool listing
    if list_tools:
        from model_tools import get_all_tool_names, get_available_toolsets, get_toolset_for_tool
        from toolsets import get_all_toolsets, get_toolset_info

        print("📋 Available Tools & Toolsets:")
        print("-" * 50)

        # Show new toolsets system
        print("\n🎯 Predefined Toolsets (New System):")
        print("-" * 40)
        all_toolsets = get_all_toolsets()

        # Group by category
        basic_toolsets = []
        composite_toolsets = []
        scenario_toolsets = []

        for name, toolset in all_toolsets.items():
            info = get_toolset_info(name)
            if info:
                entry = (name, info)
                if name in ["web", "search", "file", "browser"]:
                    basic_toolsets.append(entry)
                elif name in ["autoformalize", "leanflow-cli", "leanflow-native"]:
                    composite_toolsets.append(entry)
                else:
                    scenario_toolsets.append(entry)

        # Print basic toolsets
        print("\n📌 Basic Toolsets:")
        for name, info in basic_toolsets:
            tools_str = ", ".join(info["resolved_tools"]) if info["resolved_tools"] else "none"
            print(f"  • {name:15} - {info['description']}")
            print(f"    Tools: {tools_str}")

        # Print composite toolsets
        print("\n📂 Composite Toolsets (built from other toolsets):")
        for name, info in composite_toolsets:
            includes_str = ", ".join(info["includes"]) if info["includes"] else "none"
            print(f"  • {name:15} - {info['description']}")
            print(f"    Includes: {includes_str}")
            print(f"    Total tools: {info['tool_count']}")

        # Print scenario-specific toolsets
        print("\n🎭 Scenario-Specific Toolsets:")
        for name, info in scenario_toolsets:
            print(f"  • {name:20} - {info['description']}")
            print(f"    Total tools: {info['tool_count']}")

        # Show legacy toolset compatibility
        print("\n📦 Legacy Toolsets (for backward compatibility):")
        legacy_toolsets = get_available_toolsets()
        for name, info in legacy_toolsets.items():
            status = "✅" if info["available"] else "❌"
            print(f"  {status} {name}: {info['description']}")
            if not info["available"]:
                print(f"    Requirements: {', '.join(info['requirements'])}")

        # Show individual tools
        all_tools = get_all_tool_names()
        print(f"\n🔧 Individual Tools ({len(all_tools)} available):")
        for tool_name in sorted(all_tools):
            toolset = get_toolset_for_tool(tool_name)
            print(f"  📌 {tool_name} (from {toolset})")

        print("\n💡 Usage Examples:")
        print("  # Use predefined toolsets")
        print(
            "  python run_agent.py --enabled_toolsets=autoformalize --query='read this theorem and inspect the repo'"
        )
        print("  python run_agent.py --enabled_toolsets=file --query='update these Lean files'")
        print(
            "  python run_agent.py --enabled_toolsets=browser --query='open arxiv and inspect the paper page'"
        )
        print("  ")
        print("  # Combine multiple toolsets")
        print(
            "  python run_agent.py --enabled_toolsets=web,file --query='look up docs and patch the workspace'"
        )
        print("  ")
        print("  # Disable toolsets")
        print(
            "  python run_agent.py --disabled_toolsets=browser --query='work from local files only'"
        )
        print("  ")
        print("  # Run with trajectory saving enabled")
        print("  python run_agent.py --save_trajectories --query='your question here'")
        return

    # Parse toolset selection arguments
    enabled_toolsets_list = None
    disabled_toolsets_list = None

    if enabled_toolsets:
        enabled_toolsets_list = [t.strip() for t in enabled_toolsets.split(",")]
        print(f"🎯 Enabled toolsets: {enabled_toolsets_list}")

    if disabled_toolsets:
        disabled_toolsets_list = [t.strip() for t in disabled_toolsets.split(",")]
        print(f"🚫 Disabled toolsets: {disabled_toolsets_list}")

    if save_trajectories:
        print("💾 Trajectory saving: ENABLED")
        print("   - Successful conversations → trajectory_samples.jsonl")
        print("   - Failed conversations → failed_trajectories.jsonl")

    # Initialize agent with provided parameters
    try:
        agent = AIAgent(
            base_url=base_url,
            model=model,
            api_key=api_key,
            max_iterations=max_turns,
            enabled_toolsets=enabled_toolsets_list,
            disabled_toolsets=disabled_toolsets_list,
            save_trajectories=save_trajectories,
            verbose_logging=verbose,
            log_prefix_chars=log_prefix_chars,
        )
    except RuntimeError as e:
        print(f"❌ Failed to initialize agent: {e}")
        return

    # Use provided query or default to Python 3.13 example
    if query is None:
        user_query = (
            "Tell me about the latest developments in Python 3.13 and what new features "
            "developers should know about. Please search for current information and try it out."
        )
    else:
        user_query = query

    print(f"\n📝 User Query: {user_query}")
    print("\n" + "=" * 50)

    # Run conversation
    result = agent.run_conversation(user_query)

    print("\n" + "=" * 50)
    print("📋 CONVERSATION SUMMARY")
    print("=" * 50)
    print(f"✅ Completed: {result['completed']}")
    print(f"📞 API Calls: {result['api_calls']}")
    print(f"💬 Messages: {len(result['messages'])}")

    if result["final_response"]:
        print("\n🎯 FINAL RESPONSE:")
        print("-" * 30)
        print(result["final_response"])

    # Save sample trajectory to UUID-named file if requested
    if save_sample:
        sample_id = str(uuid.uuid4())[:8]
        sample_filename = f"sample_{sample_id}.json"

        # Convert messages to the persisted trajectory format used by LeanFlow.
        trajectory = agent._convert_to_trajectory_format(
            result["messages"], user_query, result["completed"]
        )

        entry = {
            "conversations": trajectory,
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "completed": result["completed"],
            "query": user_query,
        }

        try:
            with open(sample_filename, "w", encoding="utf-8") as f:
                # Pretty-print JSON with indent for readability
                f.write(json.dumps(entry, ensure_ascii=False, indent=2))
            print(f"\n💾 Sample trajectory saved to: {sample_filename}")
        except Exception as e:
            print(f"\n⚠️ Failed to save sample: {e}")

    print("\n👋 Agent execution completed!")


if __name__ == "__main__":
    fire.Fire(main)
