"""Keep historical result imports available while refusing retired SDK subprocess calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

from agent.providers.auxiliary_client import (
    RETIRED_PROVIDER_MESSAGE,
    resolve_auxiliary_call_identity,  # noqa: F401 -- verification_providers import compatibility
)
from core.error_redaction import sanitize_auxiliary_error as sanitize_auxiliary_error


class IsolatedAuxiliaryError(Exception):
    """Report a failed isolated auxiliary request or worker protocol."""

    def __init__(self, message: str, *, provider: str = "", model: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


class IsolatedAuxiliaryTimeout(TimeoutError):
    """Report that the parent-enforced wall-clock deadline expired."""

    def __init__(self, message: str, *, provider: str = "", model: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


class IsolatedAuxiliaryUnavailable(RuntimeError):
    """Report that the isolated worker could not resolve a provider."""

    def __init__(self, message: str, *, provider: str = "", model: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


@dataclass(frozen=True)
class AuxiliaryTextResponse:
    """Carry the normalized text fields needed by control-plane consumers."""

    content: str
    model: str


def run_isolated_auxiliary_text(*args: Any, **kwargs: Any) -> NoReturn:
    """Reject removed auxiliary SDK execution before spawning a process or reading auth."""
    raise IsolatedAuxiliaryUnavailable(RETIRED_PROVIDER_MESSAGE)


def worker_main() -> NoReturn:
    """Refuse the retired subprocess protocol with native-backend migration guidance."""
    raise IsolatedAuxiliaryUnavailable(RETIRED_PROVIDER_MESSAGE)


if __name__ == "__main__":
    worker_main()
