"""Resolve campaign launch routes and encode frozen prover settings without secrets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def resolve_launch_provider(config: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the explicitly selected native backend without campaign-owned credentials."""
    from leanflow_cli.runtime.runtime_provider import resolve_runtime_provider

    return resolve_runtime_provider(requested=str(config.get("provider") or "codex"))


def encode_setting(value: Any) -> str:
    """Encode explicit native launch choices and global limits for the worker."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return ",".join(value)
    return str(value)


def require_native_campaign(manifest: Mapping[str, Any]) -> None:
    """Reject resuming legacy API-budget experiments through a different runtime."""
    if manifest.get("version") != 2:
        raise ValueError(
            "This campaign uses the retired direct-API runtime. Keep its frozen runtime "
            "for historical results, or prepare a new native-backend campaign; old API "
            "request budgets cannot be converted to native agent invocations."
        )
