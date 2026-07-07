"""Small JSON read/write helpers for LeanFlow managed-workflow state.

These wrap ``json`` with the durability contract the workflow-state layer
relies on: writes are crash-atomic (temp file + fsync + rename via
``core.utils.atomic_json_write``), and reads tolerate missing or empty files
(returning ``{}``) but fail LOUD with :class:`WorkflowStateCorruptionError`
when a non-empty state file does not parse — corrupted workflow state must
halt-and-alert, never silently reset to an empty run. Re-exported by
``leanflow_cli.workflows.workflow_state``; many of its functions call these.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write

logger = logging.getLogger(__name__)


class WorkflowStateCorruptionError(RuntimeError):
    """A workflow-state JSON file exists, is non-empty, and cannot be parsed.

    Raised instead of the historical silent ``{}`` fallback: a truncated or
    garbled state file usually means a crashed non-atomic write or external
    tampering, and losing a long run's state silently is strictly worse than
    stopping. Inspect or remove the named file to continue.
    """


def read_json_file(path: Path) -> dict[str, Any]:
    """Return the JSON object stored at ``path``.

    Missing files, empty files, and OS-level read failures return ``{}``
    (tolerant, as before). A non-empty file that is not valid UTF-8 JSON or
    whose top level is not an object raises WorkflowStateCorruptionError.
    """
    try:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowStateCorruptionError(
            f"Corrupted workflow-state JSON at {path}: not valid UTF-8 ({exc}). "
            "Refusing to silently reset workflow state; inspect or remove the file to continue."
        ) from exc
    except OSError:
        logger.debug("Failed to read workflow-state JSON from %s", path, exc_info=True)
        return {}
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkflowStateCorruptionError(
            f"Corrupted workflow-state JSON at {path}: {exc}. "
            "Refusing to silently reset workflow state; inspect or remove the file to continue."
        ) from exc
    if not isinstance(payload, dict):
        raise WorkflowStateCorruptionError(
            f"Corrupted workflow-state JSON at {path}: expected a JSON object, "
            f"got {type(payload).__name__}. "
            "Refusing to silently reset workflow state; inspect or remove the file to continue."
        )
    return payload


def write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    """Write ``payload`` crash-atomically so a crash mid-write never truncates state."""
    atomic_json_write(path, payload, sort_keys=True)
