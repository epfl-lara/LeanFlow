"""Resolve campaign launch routes and encode frozen prover settings without secrets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def resolve_launch_provider(config: Mapping[str, Any]) -> dict[str, Any]:
    """Use the explicit RCP route for all-RCP arms; retain the legacy Codex launch route."""
    if config.get("provider") == "rcp" and config.get("orchestrator_provider") in {None, "", "rcp"}:
        from leanflow_cli.workflows.prover.session_provider import resolve_session_provider

        return resolve_session_provider(config)
    from leanflow_cli.runtime.runtime_provider import resolve_runtime_provider

    return resolve_runtime_provider(requested="openai-codex")


def encode_setting(value: Any) -> str:
    """Preserve structured model context profiles across the worker environment."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return ",".join(value)
    return str(value)
