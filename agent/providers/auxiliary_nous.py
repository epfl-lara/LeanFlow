"""Nous Portal auth/endpoint helpers for the auxiliary router.

Reads ``~/.leanflow/auth.json`` (via ``leanflow_cli.config.get_leanflow_home``) to
detect an active Nous provider, and resolves the API key and inference base URL
for it. These helpers hold no auxiliary routing state, so they form a closed
cluster that ``auxiliary_client`` re-exports unchanged (importers and tests keep
resolving ``auxiliary_client.<name>``).

This module must NOT import ``agent.auxiliary_client`` — that would create an
import cycle, since ``auxiliary_client`` re-exports these names.
"""

import json
import logging
import os

from leanflow_cli.config import get_leanflow_home

logger = logging.getLogger(__name__)

_NOUS_DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"


def _read_nous_auth() -> dict | None:
    """Read and validate ~/.leanflow/auth.json for an active Nous provider.

    Returns the provider state dict if Nous is active with tokens,
    otherwise None.
    """
    try:
        auth_path = get_leanflow_home() / "auth.json"
        if not auth_path.is_file():
            return None
        data = json.loads(auth_path.read_text())
        if data.get("active_provider") != "nous":
            return None
        provider = data.get("providers", {}).get("nous", {})
        # Must have at least an access_token or agent_key
        if not provider.get("agent_key") and not provider.get("access_token"):
            return None
        return provider
    except Exception as exc:
        logger.debug("Could not read Nous auth: %s", exc)
        return None


def _nous_api_key(provider: dict) -> str:
    """Extract the best API key from a Nous provider state dict."""
    return provider.get("agent_key") or provider.get("access_token", "")


def _nous_base_url() -> str:
    """Resolve the Nous inference base URL from env or default."""
    return os.getenv("NOUS_INFERENCE_BASE_URL", _NOUS_DEFAULT_BASE_URL)
