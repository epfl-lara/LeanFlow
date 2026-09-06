"""Reuse provider adapters while issuing exactly one request per admission."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping
from typing import Any

_IMPORT_LOCK = threading.Lock()
_agent_class: Any = None


def _load_agent_class() -> Any:
    """Import the provider adapter without starting unused legacy MCP services.

    The dedicated prover process supplies its own tools. Suppression covers
    only the first adapter import, serialized across parallel jobs, and restores
    the caller's environment even on failure. Explicit lean_search requests
    retain normal on-demand discovery through lean_services afterward.
    """
    global _agent_class
    with _IMPORT_LOCK:
        if _agent_class is None:
            previous = os.environ.get("LEANFLOW_DISABLE_MCP")
            os.environ["LEANFLOW_DISABLE_MCP"] = "1"
            try:
                from run_agent import AIAgent

                _agent_class = AIAgent
            finally:
                if previous is None:
                    os.environ.pop("LEANFLOW_DISABLE_MCP", None)
                else:
                    os.environ["LEANFLOW_DISABLE_MCP"] = previous
        return _agent_class


def build_transport(config: Mapping[str, Any], schemas: list[dict[str, Any]]) -> Any:
    """Build an adapter without entering the legacy agent conversation loop."""
    agent_class = _load_agent_class()

    model = str(config.get("model") or os.getenv("LEANFLOW_NATIVE_MODEL", ""))
    if not model:
        raise ValueError("A prover model must be configured")
    effort = str(
        config.get("reasoning_effort") or os.getenv("LEANFLOW_NATIVE_REASONING_EFFORT", "")
    )
    agent = agent_class(
        model=model,
        base_url=os.getenv("LEANFLOW_NATIVE_BASE_URL", ""),
        api_key=os.getenv("LEANFLOW_NATIVE_API_KEY", ""),
        provider=os.getenv("LEANFLOW_NATIVE_PROVIDER", ""),
        api_mode=os.getenv("LEANFLOW_NATIVE_API_MODE", ""),
        enabled_toolsets=["leanflow-prover-session"],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        checkpoints_enabled=False,
        max_tokens=int(config.get("max_output_tokens", 8192)),
        reasoning_config={"effort": effort} if effort else {},
    )
    agent.tools = schemas
    agent.valid_tool_names = {schema["function"]["name"] for schema in schemas}
    return agent


def request_once(
    agent: Any,
    messages: list[dict[str, Any]],
    timeout_s: float,
    *,
    final_report_only: bool = False,
) -> tuple[dict[str, Any], Any]:
    """Send one SDK request with retries disabled and normalize its response.

    Stream recovery and extra final-report calls in the legacy loop are absent.
    An interrupted stream consumes its admitted call and returns an error. A
    reporting-only request retains schemas/history while disabling new tools.
    """
    prepared = agent._build_api_messages_for_turn(messages[1:], str(messages[0]["content"]))
    kwargs = agent._build_api_kwargs(prepared)
    if final_report_only:
        # Tool history still needs its schemas, especially for native Anthropic.
        # Disable only new calls; never strip definitions or prior tool results.
        kwargs["tool_choice"] = (
            {"type": "none"} if agent.api_mode == "anthropic_messages" else "none"
        )
    if agent.api_mode != "codex_responses":
        kwargs["timeout"] = max(1.0, timeout_s)
    if agent.api_mode == "anthropic_messages":
        from agent.providers.anthropic_adapter import normalize_anthropic_response

        # The shared factory disables SDK retries. Closing a with_options copy
        # here would also close the original client's shared HTTP transport.
        response = agent._anthropic_client.messages.create(**kwargs)
        assistant, reason = normalize_anthropic_response(response)
    else:
        client = agent._create_request_openai_client(reason="bounded_prover_request")
        try:
            if agent.api_mode == "codex_responses":
                # The ChatGPT Codex route rejects these public Responses API
                # fields; match the existing Codex auxiliary adapter before
                # dispatch, without spending a failed request to discover it.
                kwargs.pop("max_output_tokens", None)
                kwargs.pop("temperature", None)
                kwargs = agent._preflight_codex_api_kwargs(kwargs)
                kwargs["timeout"] = max(1.0, timeout_s)
                collected: dict[int, Any] = {}
                deadline = time.monotonic() + max(1.0, timeout_s)
                with client.responses.stream(**kwargs) as stream:
                    for event in stream:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Provider stream exceeded the request deadline")
                        agent._collect_responses_stream_output_item(event, collected)
                    response = stream.get_final_response()
                # Some Codex streams put completed items only in the per-item
                # events. Reconstruct their final envelope without another send.
                response = agent._repair_empty_responses_stream_output(response, collected)
                assistant, reason = agent._normalize_codex_response(response)
            else:
                response = client.chat.completions.create(**kwargs)
                assistant = response.choices[0].message
                reason = response.choices[0].finish_reason
        finally:
            agent._close_request_openai_client(client, reason="bounded_prover_complete")
    return agent._build_assistant_message(assistant, reason), getattr(response, "usage", None)


def close_transport(agent: Any) -> None:
    """Release the provider clients retained by an adapter."""
    for name in ("client", "_anthropic_client"):
        client = getattr(agent, name, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
