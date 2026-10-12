"""Reject removed auxiliary model advisors; native workflows own theorem reasoning."""

from __future__ import annotations

import json
from typing import Any

from agent.providers.auxiliary_client import RETIRED_PROVIDER_MESSAGE


def lean_reasoning_help_tool(*args: Any, **kwargs: Any) -> str:
    """Report the retired advisor without constructing a client or reading source."""
    return json.dumps(
        {
            "success": False,
            "status": "removed",
            "provider_called": False,
            "message": RETIRED_PROVIDER_MESSAGE,
        }
    )


def lean_decompose_helpers_tool(*args: Any, **kwargs: Any) -> str:
    """Report that native planning replaces the removed model-backed decomposer."""
    return lean_reasoning_help_tool()
