"""Small JSON read/write helpers for LeanFlow managed-workflow state.

These wrap ``json`` with the error-tolerant behaviour the workflow-state layer
relies on: reads never raise (returning ``{}`` on any failure, logging at debug
level) and writes create parent directories. Re-exported by
``leanflow_cli.workflows.workflow_state``; many of its functions call these.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        logger.debug("Failed to load workflow-state JSON from %s", path, exc_info=True)
    return {}


def write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
