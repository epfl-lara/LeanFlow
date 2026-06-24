"""Provider API-call mediation extracted from run_agent.AIAgent.

``ApiCaller`` owns the *mediation* between the agent loop and the provider
transport: running a request in a background thread so the conversation loop can
detect interrupts without waiting for the full HTTP round-trip, the
timeout/heartbeat watchdog, the streaming (voice TTS) variant, the Codex
Responses streaming helpers, the per-request timeout resolution, and the
assembly of the per-mode ``api_kwargs`` payload. These were the inline
``AIAgent`` methods ``_interruptible_api_call``, ``_streaming_api_call``,
``_run_codex_stream``, ``_run_codex_create_stream_fallback``,
``_provider_request_timeout_seconds`` and ``_build_api_kwargs``.

It mirrors the ``ToolExecutor`` (74e006b), ``ConversationManager`` (c3bbb31),
``TokenAccounter`` (65d037d), ``ProviderClientFactory`` (6027ca4) and
``InterruptController`` (06e67ca) extractions: the collaborator holds a
reference to the owning ``AIAgent`` and reaches the request-lifecycle state and
helpers through it (``api_mode``, ``model``, ``base_url``, the provider
preference fields, the client constructors ``_create_request_openai_client`` /
``_close_request_openai_client`` / ``_ensure_primary_openai_client``, the
abort/heartbeat helpers, the interrupt flag ``_interrupt_requested``, the
Responses-stream collectors, and the kwargs-shaping predicates). That state is
intrinsically part of the agent's run loop, so reaching it via the agent is a
genuine boundary -- the ~330 lines of call mediation below have left the god
class.

Monkeypatch / import-cycle handling: this module does NOT import ``run_agent``
at load time. The interrupt/timeout loops use ``time.monotonic()`` and the
retry tests patch ``run_agent.time``; the streaming variant uses ``uuid`` and
``SimpleNamespace``; ``_build_api_kwargs`` uses ``os``, ``copy`` and
``DEFAULT_AGENT_IDENTITY``. To keep every existing test patch effective (and to
avoid an import cycle, since ``run_agent`` imports this module), all of those
module-level names are resolved through a lazy ``import run_agent`` at call time
via the small ``_ra()`` accessor below, exactly as ``ToolExecutor`` resolves the
``run_agent``-level names it needs.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from run_agent import AIAgent

logger = logging.getLogger(__name__)


def _ra() -> Any:
    """Return the live ``run_agent`` module, imported lazily at call time.

    Resolving ``run_agent``-level names (``time``, ``threading``, ``uuid``,
    ``os``, ``copy``, ``SimpleNamespace``, ``DEFAULT_AGENT_IDENTITY``) through
    this accessor — rather than binding them at import — keeps test
    monkeypatches like ``patch("run_agent.time", ...)`` effective and avoids an
    import cycle (run_agent imports this module).
    """
    import run_agent

    return run_agent


class ApiCaller:
    """Mediates provider API calls on behalf of an :class:`AIAgent`.

    Constructed with the owning agent; all request-lifecycle state and helpers
    are reached through it. The mediation logic is behavior-preserving relative
    to the former ``AIAgent`` methods.
    """

    def __init__(self, agent: AIAgent) -> None:
        self._agent = agent

    # ── Codex Responses streaming ───────────────────────────────────────────

    def run_codex_stream(self, api_kwargs: dict, client: Any = None):
        """Execute one streaming Responses API request and return the final response."""
        agent = self._agent
        active_client = client or agent._ensure_primary_openai_client(reason="codex_stream_direct")
        max_stream_retries = 1
        for attempt in range(max_stream_retries + 1):
            collected_items: dict[int, Any] = {}
            try:
                with active_client.responses.stream(**api_kwargs) as stream:
                    for event in stream:
                        agent._collect_responses_stream_output_item(event, collected_items)
                    response = stream.get_final_response()
                    return agent._repair_empty_responses_stream_output(response, collected_items)
            except RuntimeError as exc:
                err_text = str(exc)
                missing_completed = "response.completed" in err_text
                if missing_completed and attempt < max_stream_retries:
                    logger.debug(
                        "Responses stream closed before completion (attempt %s/%s); retrying. %s",
                        attempt + 1,
                        max_stream_retries + 1,
                        agent._client_log_context(),
                    )
                    continue
                if missing_completed:
                    logger.debug(
                        "Responses stream did not emit response.completed; falling back to create(stream=True). %s",
                        agent._client_log_context(),
                    )
                    return self.run_codex_create_stream_fallback(api_kwargs, client=active_client)
                raise

    def run_codex_create_stream_fallback(self, api_kwargs: dict, client: Any = None):
        """Fallback path for stream completion edge cases on Codex-style Responses backends."""
        agent = self._agent
        active_client = client or agent._ensure_primary_openai_client(reason="codex_create_stream_fallback")
        fallback_kwargs = dict(api_kwargs)
        fallback_kwargs["stream"] = True
        fallback_kwargs = agent._preflight_codex_api_kwargs(fallback_kwargs, allow_stream=True)
        stream_or_response = active_client.responses.create(**fallback_kwargs)

        # Compatibility shim for mocks or providers that still return a concrete response.
        if hasattr(stream_or_response, "output"):
            return stream_or_response
        if not hasattr(stream_or_response, "__iter__"):
            return stream_or_response

        terminal_response = None
        collected_items: dict[int, Any] = {}
        try:
            for event in stream_or_response:
                agent._collect_responses_stream_output_item(event, collected_items)
                event_type = agent._responses_stream_event_type(event)
                if event_type not in {"response.completed", "response.incomplete", "response.failed"}:
                    continue

                terminal_response = agent._responses_stream_event_field(event, "response")
                if terminal_response is not None:
                    return agent._repair_empty_responses_stream_output(terminal_response, collected_items)
        finally:
            close_fn = getattr(stream_or_response, "close", None)
            if callable(close_fn):
                with contextlib.suppress(Exception):
                    close_fn()

        if terminal_response is not None:
            return terminal_response
        raise RuntimeError("Responses create(stream=True) fallback did not emit a terminal response.")

    # ── Per-request timeout ─────────────────────────────────────────────────

    def provider_request_timeout_seconds(self, api_kwargs: dict) -> float:
        os = _ra().os
        timeout_value = api_kwargs.get("timeout", os.getenv("EPFLEMMA_API_TIMEOUT", 1200.0))
        if isinstance(timeout_value, (int, float)) and not isinstance(timeout_value, bool):
            return max(float(timeout_value), 1.0)
        return max(float(os.getenv("EPFLEMMA_API_TIMEOUT", 1200.0)), 1.0)

    # ── Interruptible (non-streaming) call ──────────────────────────────────

    def interruptible_api_call(self, api_kwargs: dict):
        """
        Run the API call in a background thread so the main conversation loop
        can detect interrupts without waiting for the full HTTP round-trip.

        Each worker thread gets its own OpenAI client instance. Interrupts only
        close that worker-local client, so retries and other requests never
        inherit a closed transport.
        """
        agent = self._agent
        ra = _ra()
        result = {"response": None, "error": None}
        request_client_holder = {"client": None}

        def _call():
            try:
                if agent.api_mode == "codex_responses":
                    request_client_holder["client"] = agent._create_request_openai_client(reason="codex_stream_request")
                    result["response"] = self.run_codex_stream(
                        api_kwargs,
                        client=request_client_holder["client"],
                    )
                elif agent.api_mode == "anthropic_messages":
                    result["response"] = agent._anthropic_messages_create(api_kwargs)
                else:
                    request_client_holder["client"] = agent._create_request_openai_client(reason="chat_completion_request")
                    result["response"] = request_client_holder["client"].chat.completions.create(**api_kwargs)
            except Exception as e:
                result["error"] = e
            finally:
                request_client = request_client_holder.get("client")
                if request_client is not None:
                    agent._close_request_openai_client(request_client, reason="request_complete")

        t = ra.threading.Thread(target=_call, daemon=True)
        t.start()
        timeout_seconds = self.provider_request_timeout_seconds(api_kwargs)
        heartbeat_seconds = agent._provider_wait_heartbeat_seconds()
        start_time = ra.time.monotonic()
        next_heartbeat_at = heartbeat_seconds
        while t.is_alive():
            t.join(timeout=0.3)
            elapsed_seconds = ra.time.monotonic() - start_time
            if elapsed_seconds >= next_heartbeat_at:
                agent._emit_provider_wait_heartbeat(
                    elapsed_seconds=elapsed_seconds,
                    timeout_seconds=timeout_seconds,
                    streaming=False,
                )
                next_heartbeat_at += heartbeat_seconds
            if elapsed_seconds >= timeout_seconds:
                with contextlib.suppress(Exception):
                    agent._abort_inflight_provider_request(
                        request_client_holder,
                        reason="request_timeout_abort",
                    )
                raise TimeoutError(
                    f"Provider request exceeded {timeout_seconds:.0f}s without a response."
                )
            if agent._interrupt_requested:
                # Force-close the in-flight worker-local HTTP connection to stop
                # token generation without poisoning the shared client used to
                # seed future retries.
                try:
                    # Preserve the explicit anthropic_messages/build_anthropic_client
                    # interrupt contract in source: the helper below rebuilds the
                    # Anthropic client when api_mode == "anthropic_messages".
                    agent._abort_inflight_provider_request(
                        request_client_holder,
                        reason="interrupt_abort",
                    )
                except Exception:
                    pass
                raise InterruptedError("Agent interrupted during API call")
        if result["error"] is not None:
            raise result["error"]
        return result["response"]

    # ── Streaming (voice TTS) call ──────────────────────────────────────────

    def streaming_api_call(self, api_kwargs: dict, stream_callback):
        """Streaming variant of interruptible_api_call for voice TTS pipeline.

        Uses ``stream=True`` and forwards content deltas to *stream_callback*
        in real-time.  Returns a ``SimpleNamespace`` that mimics a normal
        ``ChatCompletion`` so the rest of the agent loop works unchanged.

        This method is separate from ``interruptible_api_call`` to keep the
        core agent loop untouched for non-voice users.
        """
        agent = self._agent
        ra = _ra()
        SimpleNamespace = ra.SimpleNamespace
        uuid = ra.uuid
        result = {"response": None, "error": None}
        request_client_holder = {"client": None}

        def _call():
            try:
                stream_kwargs = {**api_kwargs, "stream": True}
                request_client_holder["client"] = agent._create_request_openai_client(
                    reason="chat_completion_stream_request"
                )
                stream = request_client_holder["client"].chat.completions.create(**stream_kwargs)

                content_parts: list[str] = []
                tool_calls_acc: dict[int, dict] = {}
                finish_reason = None
                model_name = None
                role = "assistant"

                for chunk in stream:
                    if not chunk.choices:
                        if hasattr(chunk, "model") and chunk.model:
                            model_name = chunk.model
                        continue

                    delta = chunk.choices[0].delta
                    if hasattr(chunk, "model") and chunk.model:
                        model_name = chunk.model

                    if delta and delta.content:
                        content_parts.append(delta.content)
                        with contextlib.suppress(Exception):
                            stream_callback(delta.content)

                    if delta and delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index if tc_delta.index is not None else 0
                            if idx in tool_calls_acc and tc_delta.id and tc_delta.id != tool_calls_acc[idx]["id"]:
                                matched = False
                                for eidx, eentry in tool_calls_acc.items():
                                    if eentry["id"] == tc_delta.id:
                                        idx = eidx
                                        matched = True
                                        break
                                if not matched:
                                    idx = (max(k for k in tool_calls_acc if isinstance(k, int)) + 1) if tool_calls_acc else 0
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": tc_delta.id or "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            entry = tool_calls_acc[idx]
                            if tc_delta.id:
                                entry["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    entry["function"]["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    entry["function"]["arguments"] += tc_delta.function.arguments

                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason

                full_content = "".join(content_parts) or None
                mock_tool_calls = None
                if tool_calls_acc:
                    mock_tool_calls = []
                    for idx in sorted(tool_calls_acc):
                        tc = tool_calls_acc[idx]
                        mock_tool_calls.append(SimpleNamespace(
                            id=tc["id"],
                            type=tc["type"],
                            function=SimpleNamespace(
                                name=tc["function"]["name"],
                                arguments=tc["function"]["arguments"],
                            ),
                        ))

                mock_message = SimpleNamespace(
                    role=role,
                    content=full_content,
                    tool_calls=mock_tool_calls,
                    reasoning_content=None,
                )
                mock_choice = SimpleNamespace(
                    index=0,
                    message=mock_message,
                    finish_reason=finish_reason or "stop",
                )
                mock_response = SimpleNamespace(
                    id="stream-" + str(uuid.uuid4()),
                    model=model_name,
                    choices=[mock_choice],
                    usage=None,
                )
                result["response"] = mock_response

            except Exception as e:
                result["error"] = e
            finally:
                request_client = request_client_holder.get("client")
                if request_client is not None:
                    agent._close_request_openai_client(request_client, reason="stream_request_complete")

        t = ra.threading.Thread(target=_call, daemon=True)
        t.start()
        timeout_seconds = self.provider_request_timeout_seconds(api_kwargs)
        heartbeat_seconds = agent._provider_wait_heartbeat_seconds()
        start_time = ra.time.monotonic()
        next_heartbeat_at = heartbeat_seconds
        while t.is_alive():
            t.join(timeout=0.3)
            elapsed_seconds = ra.time.monotonic() - start_time
            if elapsed_seconds >= next_heartbeat_at:
                agent._emit_provider_wait_heartbeat(
                    elapsed_seconds=elapsed_seconds,
                    timeout_seconds=timeout_seconds,
                    streaming=True,
                )
                next_heartbeat_at += heartbeat_seconds
            if elapsed_seconds >= timeout_seconds:
                with contextlib.suppress(Exception):
                    agent._abort_inflight_provider_request(
                        request_client_holder,
                        reason="stream_request_timeout_abort",
                    )
                raise TimeoutError(
                    f"Provider request exceeded {timeout_seconds:.0f}s without a response."
                )
            if agent._interrupt_requested:
                try:
                    # Preserve the explicit anthropic_messages/build_anthropic_client
                    # interrupt contract in source: the helper below rebuilds the
                    # Anthropic client when api_mode == "anthropic_messages".
                    agent._abort_inflight_provider_request(
                        request_client_holder,
                        reason="stream_interrupt_abort",
                    )
                except Exception:
                    pass
                raise InterruptedError("Agent interrupted during API call")
        if result["error"] is not None:
            raise result["error"]
        return result["response"]

    # ── api_kwargs assembly ─────────────────────────────────────────────────

    def build_api_kwargs(self, api_messages: list) -> dict:
        """Build the keyword arguments dict for the active API mode."""
        agent = self._agent
        ra = _ra()
        if agent.api_mode == "anthropic_messages":
            from agent.providers.anthropic_adapter import build_anthropic_kwargs
            anthropic_messages = agent._prepare_anthropic_messages_for_api(api_messages)
            return build_anthropic_kwargs(
                model=agent.model,
                messages=anthropic_messages,
                tools=agent.tools,
                max_tokens=agent.max_tokens,
                reasoning_config=agent.reasoning_config,
            )

        if agent.api_mode == "codex_responses":
            instructions = ""
            payload_messages = api_messages
            if api_messages and api_messages[0].get("role") == "system":
                instructions = str(api_messages[0].get("content") or "").strip()
                payload_messages = api_messages[1:]
            if not instructions:
                instructions = ra.DEFAULT_AGENT_IDENTITY

            # Resolve reasoning effort: config > default (high)
            reasoning_effort = "high"
            reasoning_enabled = True
            if agent.reasoning_config and isinstance(agent.reasoning_config, dict):
                if agent.reasoning_config.get("enabled") is False:
                    reasoning_enabled = False
                elif agent.reasoning_config.get("effort"):
                    reasoning_effort = agent.reasoning_config["effort"]

            kwargs = {
                "model": agent.model,
                "instructions": instructions,
                "input": agent._chat_messages_to_responses_input(payload_messages),
                "tools": agent._responses_tools(),
                "tool_choice": "auto",
                "parallel_tool_calls": True,
                "store": False,
                "prompt_cache_key": agent.session_id,
            }

            if reasoning_enabled:
                kwargs["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
                kwargs["include"] = ["reasoning.encrypted_content"]
            else:
                kwargs["include"] = []

            if agent.max_tokens is not None:
                kwargs["max_output_tokens"] = agent.max_tokens

            return kwargs

        sanitized_messages = api_messages
        needs_sanitization = False
        for msg in api_messages:
            if not isinstance(msg, dict):
                continue
            if "codex_reasoning_items" in msg:
                needs_sanitization = True
                break

            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    if "call_id" in tool_call or "response_item_id" in tool_call:
                        needs_sanitization = True
                        break
                if needs_sanitization:
                    break

        if needs_sanitization:
            sanitized_messages = ra.copy.deepcopy(api_messages)
            for msg in sanitized_messages:
                if not isinstance(msg, dict):
                    continue

                # Codex-only replay state must not leak into strict chat-completions APIs.
                msg.pop("codex_reasoning_items", None)

                tool_calls = msg.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
                        if isinstance(tool_call, dict):
                            tool_call.pop("call_id", None)
                            tool_call.pop("response_item_id", None)

        provider_preferences = {}
        if agent.providers_allowed:
            provider_preferences["only"] = agent.providers_allowed
        if agent.providers_ignored:
            provider_preferences["ignore"] = agent.providers_ignored
        if agent.providers_order:
            provider_preferences["order"] = agent.providers_order
        if agent.provider_sort:
            provider_preferences["sort"] = agent.provider_sort
        if agent.provider_require_parameters:
            provider_preferences["require_parameters"] = True
        if agent.provider_data_collection:
            provider_preferences["data_collection"] = agent.provider_data_collection

        api_kwargs = {
            "model": agent.model,
            "messages": sanitized_messages,
            "tools": agent.tools if agent.tools else None,
            "timeout": float(ra.os.getenv("EPFLEMMA_API_TIMEOUT", 1200.0)),
        }

        if agent.max_tokens is not None:
            api_kwargs.update(agent._max_tokens_param(agent.max_tokens))
        if isinstance(agent.temperature, (int, float)):
            api_kwargs["temperature"] = float(agent.temperature)
        if isinstance(agent.top_p, (int, float)):
            api_kwargs["top_p"] = float(agent.top_p)
        if isinstance(agent.seed, int) and not isinstance(agent.seed, bool):
            api_kwargs["seed"] = agent.seed

        extra_body = {}

        _is_openrouter = "openrouter" in agent.base_url.lower()

        # Provider preferences (only, ignore, order, sort) are OpenRouter-
        # specific.  Only send to OpenRouter-compatible endpoints.
        # TODO: Nous Portal will add transparent proxy support — re-enable
        # for _is_nous when their backend is updated.
        if provider_preferences and _is_openrouter:
            extra_body["provider"] = provider_preferences
        _is_nous = "nousresearch" in agent.base_url.lower()

        reasoning_enabled, reasoning_effort = agent._reasoning_effort_state()
        if agent._supports_reasoning_extra_body():
            if agent.reasoning_config is not None:
                rc = dict(agent.reasoning_config)
                # Nous Portal requires reasoning enabled — don't send
                # enabled=false to it (would cause 400).
                if _is_nous and rc.get("enabled") is False:
                    pass  # omit reasoning entirely for Nous when disabled
                else:
                    extra_body["reasoning"] = rc
            else:
                extra_body["reasoning"] = {
                    "enabled": True,
                    "effort": "high"
                }
        elif agent._is_rcp_route():
            # EPFL AIaaS forwards extra_body to LiteLLM/vLLM. Qwen hybrid
            # reasoning models use chat_template kwargs while OpenAI-style
            # reasoning models honor reasoning_effort.
            template_kwargs = dict(extra_body.get("chat_template_kwargs") or {})
            template_kwargs["enable_thinking"] = reasoning_enabled
            extra_body["chat_template_kwargs"] = template_kwargs
            if reasoning_enabled:
                extra_body["reasoning_effort"] = agent._map_rcp_reasoning_effort(
                    reasoning_effort
                )
            if isinstance(agent.top_k, int) and not isinstance(agent.top_k, bool):
                extra_body["top_k"] = agent.top_k
            if isinstance(agent.min_p, (int, float)):
                extra_body["min_p"] = float(agent.min_p)

        # Nous Portal product attribution
        if _is_nous:
            extra_body["tags"] = ["product=epflemma-agent"]

        if extra_body:
            api_kwargs["extra_body"] = extra_body

        return api_kwargs
