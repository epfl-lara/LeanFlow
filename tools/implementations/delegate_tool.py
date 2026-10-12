"""Reject the retired in-process agent delegation path without invoking a model."""

from __future__ import annotations

import json
from typing import Any

from agent.providers.auxiliary_client import RETIRED_PROVIDER_MESSAGE


def check_delegate_requirements() -> bool:
    """Keep the removed direct-model delegation tool out of tool discovery."""
    return False


def delegate_task(*args: Any, **kwargs: Any) -> str:
    """Return actionable migration guidance to historical dispatch callers."""
    return json.dumps({"error": RETIRED_PROVIDER_MESSAGE, "status": "removed"})
