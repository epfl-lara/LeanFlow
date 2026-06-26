"""Provider/OpenAI client construction and credential refresh for an agent.

``ProviderClientFactory`` consolidates the scattered provider-client logic that
previously lived inline on ``AIAgent``:

* building an OpenAI-compatible client from ``client_kwargs`` (with the shared/
  request lifecycle logging),
* the provider-specific ``default_headers``/``base_url`` kwargs assembly
  (OpenRouter attribution headers, Kimi user-agent, the routed-client path),
* the credential-*resolution* half of the Codex/Nous/Anthropic refreshers — the
  network/auth call that produces fresh credentials, returned to the caller.

The factory is **stateless**: every method takes the inputs it needs as
parameters and returns the constructed client (or resolved credentials). The
mutable client-lifecycle state (``self.client``, ``self._client_kwargs``,
``self._client_lock``, ``self._anthropic_*``) stays on ``AIAgent``, which keeps
owning the lock and the assignment ``self.client = factory.create_openai_client(...)``.
This keeps the conversation loop's locking/fallback/interrupt behavior intact
while moving the construction + auth logic out of the god class.

Import-cycle note: the OpenAI class used for construction is looked up as
``run_agent.OpenAI`` *at call time* (lazy import inside the method), so the
existing test monkeypatches — ``patch("run_agent.OpenAI")`` and
``monkeypatch.setattr(run_agent, "OpenAI", ...)`` — continue to control client
creation without any test edits. The module never imports ``run_agent`` at the
top level, so there is no import cycle.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from unittest.mock import Mock

from core.constants import OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)


class ProviderClientFactory:
    """Build provider clients and resolve refreshed credentials for an agent.

    Stateless collaborator: holds no per-request state. All inputs are passed
    in; constructed clients / resolved credentials are returned.
    """

    # ── OpenAI-compatible client construction ────────────────────────────────

    @staticmethod
    def create_openai_client(
        client_kwargs: dict,
        *,
        reason: str,
        shared: bool,
        log_context: str = "",
    ) -> Any:
        """Construct an OpenAI-compatible client from ``client_kwargs``.

        ``run_agent.OpenAI`` is resolved lazily at call time so test
        monkeypatches on that symbol still drive construction.
        """
        import run_agent  # lazy: avoid import cycle; honor run_agent.OpenAI patch

        client = run_agent.OpenAI(**client_kwargs)
        logger.info(
            "OpenAI client created (%s, shared=%s) %s",
            reason,
            shared,
            log_context,
        )
        return client

    @staticmethod
    def close_openai_client(
        client: Any,
        *,
        reason: str,
        shared: bool,
        log_context: str = "",
    ) -> None:
        """Close a client, logging success/failure. Tolerates ``None``."""
        if client is None:
            return
        try:
            client.close()
            logger.info(
                "OpenAI client closed (%s, shared=%s) %s",
                reason,
                shared,
                log_context,
            )
        except Exception as exc:
            logger.debug(
                "OpenAI client close failed (%s, shared=%s) %s error=%s",
                reason,
                shared,
                log_context,
                exc,
            )

    @staticmethod
    def is_openai_client_closed(client: Any) -> bool:
        """True if the client's underlying HTTP transport is closed.

        Mocks are treated as open so tests that pass bare mocks behave as before.
        """
        if isinstance(client, Mock):
            return False
        http_client = getattr(client, "_client", None)
        return bool(getattr(http_client, "is_closed", False))

    # ── Provider-specific client_kwargs assembly (used at agent init) ─────────

    @staticmethod
    def openrouter_default_headers() -> dict[str, str]:
        """Attribution headers OpenRouter expects from the LeanFlow agent."""
        return {
            "HTTP-Referer": "https://leanflow.dev",
            "X-OpenRouter-Title": "LeanFlow Agent",
            "X-OpenRouter-Categories": "productivity,cli-agent",
        }

    @classmethod
    def build_explicit_client_kwargs(cls, api_key: str, base_url: str) -> dict:
        """Build ``client_kwargs`` for explicit CLI/gateway credentials.

        Adds provider-specific ``default_headers`` for OpenRouter / Kimi.
        """
        client_kwargs: dict = {"api_key": api_key, "base_url": base_url}
        effective_base = base_url.lower()
        if "openrouter" in effective_base:
            client_kwargs["default_headers"] = cls.openrouter_default_headers()
        elif "api.kimi.com" in effective_base:
            client_kwargs["default_headers"] = {"User-Agent": "KimiCLI/1.3"}
        return client_kwargs

    @classmethod
    def build_routed_client_kwargs(cls, provider: str, model: str) -> dict:
        """Resolve ``client_kwargs`` via the centralized provider router.

        Falls back to a raw OpenRouter key when the router has nothing.
        """
        from agent.providers.auxiliary_client import resolve_provider_client

        routed_client, _ = resolve_provider_client(provider or "auto", model=model, raw_codex=True)
        if routed_client is not None:
            client_kwargs: dict = {
                "api_key": routed_client.api_key,
                "base_url": str(routed_client.base_url),
            }
            # Preserve any default_headers the router set.
            default_headers = getattr(routed_client, "_default_headers", None)
            if default_headers:
                client_kwargs["default_headers"] = dict(default_headers)
            return client_kwargs

        # Final fallback: raw OpenRouter key.
        return {
            "api_key": os.getenv("OPENROUTER_API_KEY", ""),
            "base_url": OPENROUTER_BASE_URL,
            "default_headers": cls.openrouter_default_headers(),
        }

    # ── Anthropic native client ──────────────────────────────────────────────

    @staticmethod
    def build_anthropic_client(api_key: str, base_url: Any = None) -> Any:
        """Build a native Anthropic client via the anthropic adapter."""
        from agent.providers.anthropic_adapter import build_anthropic_client

        return build_anthropic_client(api_key, base_url)

    @staticmethod
    def resolve_anthropic_token() -> Any:
        """Resolve the current Anthropic token via the anthropic adapter."""
        from agent.providers.anthropic_adapter import resolve_anthropic_token

        return resolve_anthropic_token()

    # ── Credential resolution (the auth half of the refreshers) ──────────────

    @staticmethod
    def resolve_codex_credentials(*, force: bool) -> dict | None:
        """Resolve fresh Codex runtime credentials.

        Returns a ``{"api_key", "base_url"}`` dict with non-empty string values,
        or ``None`` when refresh fails / yields invalid creds.
        """
        try:
            from leanflow_cli.runtime.auth import resolve_codex_runtime_credentials

            creds = resolve_codex_runtime_credentials(force_refresh=force, allow_legacy_store=True)
        except Exception as exc:
            logger.debug("Codex credential refresh failed: %s", exc)
            return None

        api_key = creds.get("api_key")
        base_url = creds.get("base_url")
        if not isinstance(api_key, str) or not api_key.strip():
            return None
        if not isinstance(base_url, str) or not base_url.strip():
            return None
        return {"api_key": api_key.strip(), "base_url": base_url.strip().rstrip("/")}

    @staticmethod
    def resolve_nous_credentials(*, force: bool) -> dict | None:
        """Resolve fresh Nous runtime credentials.

        Returns a ``{"api_key", "base_url"}`` dict with non-empty string values,
        or ``None`` when refresh fails / yields invalid creds.
        """
        try:
            from leanflow_cli.runtime.auth import resolve_nous_runtime_credentials

            creds = resolve_nous_runtime_credentials(
                min_key_ttl_seconds=max(
                    60, int(os.getenv("LEANFLOW_NOUS_MIN_KEY_TTL_SECONDS", "1800"))
                ),
                timeout_seconds=float(os.getenv("LEANFLOW_NOUS_TIMEOUT_SECONDS", "15")),
                force_mint=force,
            )
        except Exception as exc:
            logger.debug("Nous credential refresh failed: %s", exc)
            return None

        api_key = creds.get("api_key")
        base_url = creds.get("base_url")
        if not isinstance(api_key, str) or not api_key.strip():
            return None
        if not isinstance(base_url, str) or not base_url.strip():
            return None
        return {"api_key": api_key.strip(), "base_url": base_url.strip().rstrip("/")}
