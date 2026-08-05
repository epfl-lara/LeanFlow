"""Build provider-backed or deterministic context-compaction handoffs."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from agent.providers.auxiliary_client import call_llm
from agent.providers.isolated_auxiliary import run_isolated_auxiliary_text
from agent.runtime.workflow_events import _emit_workflow_event

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION] Earlier turns in this conversation were compacted "
    "to save context space. The summary below describes work that was "
    "already completed, and the current session state may still reflect "
    "that work (for example, files may already be changed). Use the summary "
    "and the current state to continue from where things left off. Compaction "
    "does not start a new task: do not repeat capability discovery, bootstrap "
    "inspection, broad search, or an already-recorded failed attempt solely "
    "because context was compacted; resume from the strongest preserved "
    "checked evidence and recheck only when required state is missing or stale:"
)
LEGACY_SUMMARY_PREFIX = "[CONTEXT SUMMARY]:"
AUXILIARY_SUMMARY_TIMEOUT_S = 10.0
MAIN_SUMMARY_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class CompressionSummaryHandoff:
    """Generate one bounded continuation handoff for compacted turns."""

    model: str
    main_provider: str = ""
    main_api_mode: str = ""
    base_url: str = ""
    api_key: str = ""
    summary_model: str = ""
    summary_target_tokens: int = 2500
    call: Callable[..., Any] = call_llm
    process_isolated: bool = True
    auxiliary_enabled: bool = True
    on_auxiliary_failure: Callable[[str], None] | None = None
    main_enabled: bool = True
    main_circuit_failure: str = ""
    on_main_failure: Callable[[str], None] | None = None

    def _emit_route_event(
        self,
        event_type: str,
        *,
        route: str,
        outcome: str,
        duration_seconds: float = 0.0,
        failure_type: str = "",
        turn_count: int = 0,
        prompt_chars: int = 0,
    ) -> None:
        """Emit bounded timing for one otherwise-hidden summary request."""
        _emit_workflow_event(
            event_type,
            f"Context compression summary route {route}: {outcome}",
            route=route,
            outcome=outcome,
            duration_seconds=round(max(0.0, duration_seconds), 3),
            failure_type=str(failure_type or "")[:100],
            turn_count=max(0, int(turn_count)),
            prompt_chars=max(0, int(prompt_chars)),
            summary_model=(self.summary_model if route == "auxiliary" else self.model),
        )

    def _open_auxiliary_circuit(self, failure_type: str) -> None:
        """Notify the owning compressor that this auxiliary route failed."""
        if self.on_auxiliary_failure is None:
            return
        try:
            self.on_auxiliary_failure(str(failure_type or "UnknownFailure"))
        except Exception:
            logger.debug("Compression auxiliary circuit callback failed", exc_info=True)

    def _open_main_circuit(self, failure_type: str) -> None:
        """Notify the owning compressor that this main-summary route failed."""
        if self.on_main_failure is None:
            return
        try:
            self.on_main_failure(str(failure_type or "UnknownFailure"))
        except Exception:
            logger.debug("Compression main circuit callback failed", exc_info=True)

    @staticmethod
    def with_summary_prefix(summary: str) -> str:
        """Normalize summary text to the current compaction handoff format."""
        text = (summary or "").strip()
        for prefix in (LEGACY_SUMMARY_PREFIX, SUMMARY_PREFIX):
            if text.startswith(prefix):
                text = text[len(prefix) :].lstrip()
                break
        return f"{SUMMARY_PREFIX}\n{text}" if text else SUMMARY_PREFIX

    @staticmethod
    def _response_summary_text(response: Any) -> str:
        """Return usable text from a chat-shaped response, or an empty string."""
        direct_content = (
            response.get("content")
            if isinstance(response, dict)
            else getattr(response, "content", None)
        )
        if isinstance(direct_content, str):
            return direct_content.strip()
        choices = response.get("choices") if isinstance(response, dict) else None
        if choices is None:
            choices = getattr(response, "choices", None)
        if not isinstance(choices, (list, tuple)) or not choices:
            return ""

        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if message is None:
            message = getattr(choice, "message", None)
        if message is None:
            return ""

        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(message, dict):
            content = getattr(message, "content", None)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            text = content.get("text")
            return text.strip() if isinstance(text, str) else ""
        if isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, str) and part.strip():
                    text_parts.append(part.strip())
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"].strip())
            return "\n".join(part for part in text_parts if part).strip()
        return ""

    @classmethod
    def _usable_generated_summary(cls, response: Any) -> str:
        """Normalize generated text and reject empty wrapper-only output."""
        text = cls._response_summary_text(response)
        for prefix in (LEGACY_SUMMARY_PREFIX, SUMMARY_PREFIX):
            if text.startswith(prefix):
                text = text[len(prefix) :].lstrip()
                break
        return cls.with_summary_prefix(text) if text else ""

    def _effective_main_provider(self) -> str:
        """Return the auxiliary-router provider matching the main API mode."""
        api_mode = (self.main_api_mode or "").strip().lower()
        if api_mode == "codex_responses":
            return "openai-codex"
        if api_mode == "anthropic_messages":
            return "anthropic"

        provider = (self.main_provider or "").strip().lower()
        if provider == "codex":
            return "openai-codex"
        if provider in {
            "anthropic",
            "deepseek",
            "kimi-coding",
            "minimax",
            "minimax-cn",
            "nous",
            "openai-codex",
            "openrouter",
            "zai",
        }:
            return provider
        if provider in {"custom", "local", "main"} or self.base_url:
            return "custom"
        return "auto"

    def _main_call_kwargs(self, prompt: str) -> dict[str, Any]:
        """Build one credential-safe main-model summary request."""
        provider = self._effective_main_provider()
        kwargs: dict[str, Any] = {
            "task": None,
            "provider": provider,
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": self.summary_target_tokens * 2,
            "timeout": MAIN_SUMMARY_TIMEOUT_S,
        }
        # Codex must resolve through its Responses adapter. Only an
        # OpenAI-compatible custom route receives the main endpoint directly.
        if provider == "custom":
            if self.base_url:
                kwargs["base_url"] = self.base_url
            if self.api_key:
                kwargs["api_key"] = self.api_key
        return kwargs

    def _invoke(self, kwargs: dict[str, Any]) -> Any:
        """Execute one summary route behind a hard process deadline."""
        if not self.process_isolated:
            return self.call(**kwargs)
        return run_isolated_auxiliary_text(
            task=kwargs.get("task"),
            provider=kwargs.get("provider"),
            model=kwargs.get("model"),
            base_url=kwargs.get("base_url"),
            api_key=kwargs.get("api_key"),
            messages=list(kwargs.get("messages") or []),
            temperature=kwargs.get("temperature"),
            max_tokens=kwargs.get("max_tokens"),
            timeout=float(kwargs.get("timeout", AUXILIARY_SUMMARY_TIMEOUT_S)),
        )

    def _safe_extract_text(self, value: Any) -> str:
        """Return deterministic redacted text for a local recovery handoff."""
        if isinstance(value, str):
            text = value
        elif isinstance(value, (dict, list, tuple)):
            try:
                text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            except (TypeError, ValueError):
                text = str(value)
        elif value is None:
            text = ""
        else:
            text = str(value)

        # The main key is known even when configurable global redaction is
        # disabled. Never serialize that credential into a compaction handoff.
        if isinstance(self.api_key, str) and len(self.api_key) >= 8:
            text = text.replace(self.api_key, "[REDACTED]")
        return " ".join(redact_sensitive_text(text).split())

    def deterministic_extract(self, turns: list[dict[str, Any]]) -> str:
        """Build a bounded local handoff from evenly sampled turn excerpts."""
        max_chars = max(
            len(SUMMARY_PREFIX) + 512,
            min(max(1, self.summary_target_tokens) * 4, 12_000),
        )
        header = (
            "## Deterministic Recovery Handoff\n"
            "Remote summary generation was unavailable. These bounded local "
            "excerpts preserve the compacted work; provider failure details are omitted.\n\n"
            "## Extracted Turns\n"
        )
        footer = (
            "\n\n## Continue\n"
            "Continue from the preserved recent turns and current workspace state. "
            "Do not restart capability discovery, bootstrap inspection, or broad search "
            "solely because compaction occurred. Recheck extracted claims only when the "
            "required exact evidence is missing or the workspace may have changed."
        )
        body_limit = max_chars - len(SUMMARY_PREFIX) - 1
        entries_budget = max(128, body_limit - len(header) - len(footer))

        candidates: list[tuple[int, str, str]] = []
        for index, message in enumerate(turns):
            role = str(message.get("role", "unknown") or "unknown").upper()
            content = self._safe_extract_text(message.get("content"))
            tool_names = [
                str(call.get("function", {}).get("name", "?") or "?")
                for call in message.get("tool_calls") or []
                if isinstance(call, dict)
            ]
            if tool_names:
                content = f"{content} [Tool calls: {', '.join(tool_names)}]".strip()
            candidates.append((index + 1, role, content or "[no textual content]"))

        selected_count = min(len(candidates), 8)
        if selected_count <= 1:
            selected = candidates
        else:
            last_index = len(candidates) - 1
            selected_indices = {
                round(slot * last_index / (selected_count - 1)) for slot in range(selected_count)
            }
            selected = [candidates[index] for index in sorted(selected_indices)]

        lines: list[str] = []
        if selected:
            per_entry = max(48, entries_budget // len(selected))
            for turn_number, role, content in selected:
                prefix = f"- Turn {turn_number} {role}: "
                excerpt_limit = max(16, per_entry - len(prefix) - 1)
                excerpt = content
                if len(excerpt) > excerpt_limit:
                    excerpt = excerpt[: max(1, excerpt_limit - 1)].rstrip() + "…"
                lines.append(prefix + excerpt)
        else:
            lines.append("- No textual middle turns were available to extract.")

        body = header + "\n".join(lines) + footer
        if len(body) > body_limit:
            body = body[:body_limit].rstrip()
        return self.with_summary_prefix(body)

    def _prompt(self, turns: list[dict[str, Any]]) -> str:
        """Build the bounded resume-oriented summarization prompt."""
        parts: list[str] = []
        for message in turns:
            role = str(message.get("role", "unknown") or "unknown")
            content = self._safe_extract_text(message.get("content"))
            if len(content) > 2000:
                content = content[:1000] + "\n...[truncated]...\n" + content[-500:]
            tool_names = [
                str(call.get("function", {}).get("name", "?") or "?")
                for call in message.get("tool_calls") or []
                if isinstance(call, dict)
            ]
            if tool_names:
                content += f"\n[Tool calls: {', '.join(tool_names)}]"
            parts.append(f"[{role.upper()}]: {content}")

        content_to_summarize = "\n\n".join(parts)
        return f"""Create a concise but high-signal handoff for a later assistant that will continue this conversation after earlier turns are compacted.

Use this structure:
## Goal
[What the user is trying to accomplish]

## Instructions
- [Important user instructions, constraints, and preferences]

## Discoveries
[Important findings, tool results, file names, and technical facts]

## Accomplished
[What is already done, what changed, and what remains]

## Next Steps
- [Concrete next action]

Keep it factual and resume-oriented. Mention relevant files and avoid repeating stale tool output unless it matters. Target ~{self.summary_target_tokens} tokens.

---
TURNS TO SUMMARIZE:
{content_to_summarize}
---

Write only the summary body. Do not include any preamble or prefix; the system will add the handoff wrapper."""

    def generate(self, turns: list[dict[str, Any]]) -> str:
        """Try auxiliary and main routes once each, then extract locally."""
        prompt = self._prompt(turns)
        common_kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": self.summary_target_tokens * 2,
            # This route is optional and already has main/local fallbacks. A
            # dead small-model endpoint must not hold every tool turn for the
            # generic 30-second provider timeout before that recovery starts.
            "timeout": AUXILIARY_SUMMARY_TIMEOUT_S,
        }
        auxiliary_failure = "CircuitOpen"
        if self.auxiliary_enabled:
            auxiliary_started = time.monotonic()
            self._emit_route_event(
                "compression-summary-route-start",
                route="auxiliary",
                outcome="started",
                turn_count=len(turns),
                prompt_chars=len(prompt),
            )
            try:
                call_kwargs = {"task": "compression", **common_kwargs}
                if self.summary_model:
                    call_kwargs["model"] = self.summary_model
                response = self._invoke(call_kwargs)
                summary = self._usable_generated_summary(response)
                if summary:
                    self._emit_route_event(
                        "compression-summary-route-finished",
                        route="auxiliary",
                        outcome="succeeded",
                        duration_seconds=time.monotonic() - auxiliary_started,
                        turn_count=len(turns),
                        prompt_chars=len(prompt),
                    )
                    return summary
                auxiliary_failure = "UnusableSummaryResponse"
                self._open_auxiliary_circuit(auxiliary_failure)
            except Exception as exc:
                auxiliary_failure = type(exc).__name__
                self._open_auxiliary_circuit(auxiliary_failure)
            self._emit_route_event(
                "compression-summary-route-finished",
                route="auxiliary",
                outcome="failed",
                duration_seconds=time.monotonic() - auxiliary_started,
                failure_type=auxiliary_failure,
                turn_count=len(turns),
                prompt_chars=len(prompt),
            )
        else:
            self._emit_route_event(
                "compression-summary-route-skipped",
                route="auxiliary",
                outcome="circuit-open",
                failure_type=auxiliary_failure,
                turn_count=len(turns),
                prompt_chars=len(prompt),
            )

        main_failure = str(self.main_circuit_failure or "CircuitOpen")
        if not self.main_enabled:
            self._emit_route_event(
                "compression-summary-route-skipped",
                route="main",
                outcome="circuit-open",
                failure_type=main_failure,
                turn_count=len(turns),
                prompt_chars=len(prompt),
            )
            return self._deterministic_fallback(
                turns,
                prompt=prompt,
                auxiliary_failure=auxiliary_failure,
                main_failure=main_failure,
            )

        main_failure = ""
        main_started = time.monotonic()
        self._emit_route_event(
            "compression-summary-route-start",
            route="main",
            outcome="started",
            turn_count=len(turns),
            prompt_chars=len(prompt),
        )
        try:
            response = self._invoke(self._main_call_kwargs(prompt))
            summary = self._usable_generated_summary(response)
            if summary:
                self._emit_route_event(
                    "compression-summary-route-finished",
                    route="main",
                    outcome="succeeded",
                    duration_seconds=time.monotonic() - main_started,
                    turn_count=len(turns),
                    prompt_chars=len(prompt),
                )
                logger.warning(
                    "Context compression auxiliary summary failed (%s); "
                    "main-model fallback succeeded.",
                    auxiliary_failure,
                )
                return summary
            main_failure = "UnusableSummaryResponse"
            self._open_main_circuit(main_failure)
        except Exception as exc:
            main_failure = type(exc).__name__
            self._open_main_circuit(main_failure)
        self._emit_route_event(
            "compression-summary-route-finished",
            route="main",
            outcome="failed",
            duration_seconds=time.monotonic() - main_started,
            failure_type=main_failure,
            turn_count=len(turns),
            prompt_chars=len(prompt),
        )

        return self._deterministic_fallback(
            turns,
            prompt=prompt,
            auxiliary_failure=auxiliary_failure,
            main_failure=main_failure,
        )

    def _deterministic_fallback(
        self,
        turns: list[dict[str, Any]],
        *,
        prompt: str,
        auxiliary_failure: str,
        main_failure: str,
    ) -> str:
        """Build and report one local handoff after remote routes fail."""
        logger.warning(
            "Context compression summary routes failed "
            "(auxiliary=%s, main=%s); using deterministic extractive handoff.",
            auxiliary_failure or "UnknownFailure",
            main_failure or "UnknownFailure",
        )
        deterministic_started = time.monotonic()
        summary = self.deterministic_extract(turns)
        self._emit_route_event(
            "compression-summary-route-finished",
            route="deterministic",
            outcome="succeeded",
            duration_seconds=time.monotonic() - deterministic_started,
            turn_count=len(turns),
            prompt_chars=len(prompt),
        )
        return summary
