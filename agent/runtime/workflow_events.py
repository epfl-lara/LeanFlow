"""Workflow activity-event emission helpers for the agent.

Leaf module: emits managed-workflow activity events (gated on the workflow-project env) and
shapes per-agent event detail payloads. Extracted verbatim from run_agent.py and re-exported
there for the historical run_agent._emit_workflow_event / _workflow_agent_event_details surface
(bare-name callers + tests). workflow_state is imported lazily inside the emitter, so no cycle.
"""

from __future__ import annotations

import logging
import os
from typing import Any

# Keep the original "run_agent" logger name so the failure-path debug record is byte-identical
# to before this code was extracted from run_agent.py (no behavior change).
logger = logging.getLogger("run_agent")


def _emit_workflow_event(event_type: str, message: str, **details: Any) -> None:
    if not (
        os.getenv("EPFLEMMA_PROJECT_ROOT")
        or os.getenv("OPENGAUSS_PROJECT_ROOT")
        or os.getenv("GAUSS_PROJECT_ROOT")
    ):
        return
    try:
        from epflemma_cli.workflows.workflow_state import append_workflow_activity

        append_workflow_activity(event_type, message, **details)
    except Exception:
        logger.debug("Failed to append workflow event %s", event_type, exc_info=True)


def _workflow_agent_event_details(agent: Any, **details: Any) -> dict[str, Any]:
    payload = dict(details)
    payload.setdefault("agent_session_id", str(getattr(agent, "session_id", "") or ""))
    payload.setdefault(
        "parent_agent_session_id",
        str(getattr(agent, "_parent_session_id", "") or ""),
    )
    try:
        payload.setdefault("delegate_depth", int(getattr(agent, "_delegate_depth", 0) or 0))
    except Exception:
        payload.setdefault("delegate_depth", 0)
    payload.setdefault("model", str(getattr(agent, "model", "") or ""))
    payload.setdefault("provider", str(getattr(agent, "provider", "") or ""))
    payload.setdefault("api_mode", str(getattr(agent, "api_mode", "") or ""))
    payload.setdefault("base_url", str(getattr(agent, "base_url", "") or ""))
    payload.setdefault("process_id", os.getpid())
    return payload
