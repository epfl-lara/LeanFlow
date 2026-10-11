"""Refuse the retired direct-API auxiliary router and point callers to native runtimes.

Import-compatible names keep deterministic legacy collaborators inspectable. They
never construct SDK clients, choose fallback models, or read native credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

RETIRED_PROVIDER_MESSAGE = (
    "LeanFlow direct-API auxiliary providers have been removed. Use the native "
    "codex, claude, or lemex workflow backend; each runtime owns authentication, "
    "model requests, sampling, and context management. Configure external endpoints "
    "with LEMEX_BASE_URL and LEMEX_API_KEY in Lemex's native setup."
)


class RetiredProviderError(RuntimeError):
    """Report use of the removed direct-API provider surface."""


@dataclass(frozen=True)
class AuxiliaryCallIdentity:
    """Retain the credential-free shape used by historical observer records."""

    provider: str
    model: str


def resolve_provider_client(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject SDK client resolution before inspecting auth or provider configuration."""
    raise RetiredProviderError(RETIRED_PROVIDER_MESSAGE)


def get_text_auxiliary_client(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject the removed automatic provider fallback chain."""
    raise RetiredProviderError(RETIRED_PROVIDER_MESSAGE)


def get_async_text_auxiliary_client(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject asynchronous SDK client creation."""
    raise RetiredProviderError(RETIRED_PROVIDER_MESSAGE)


def resolve_auxiliary_call_identity(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject credential-backed legacy identity resolution."""
    raise RetiredProviderError(RETIRED_PROVIDER_MESSAGE)


def call_llm(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject direct auxiliary requests and their old compression/timeout overrides."""
    raise RetiredProviderError(RETIRED_PROVIDER_MESSAGE)


async def async_call_llm(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject asynchronous direct auxiliary requests."""
    raise RetiredProviderError(RETIRED_PROVIDER_MESSAGE)
