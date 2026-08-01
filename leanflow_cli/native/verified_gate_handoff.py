"""Carry authenticated post-verification state across a queue boundary."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

from leanflow_cli.native import source_only_startup

_HANDOFF_ATTR = "_managed_step_boundary_live_state"
_HANDOFF_STATE_KEY = "_verified_gate_handoff_live_state"


def remember(agent: Any, state: Mapping[str, Any]) -> bool:
    """Store a source-revision-bound live state for the next outer-loop cycle."""
    revision = source_only_startup.SourceRevision.from_mapping(
        dict(state.get("source_revision") or {})
    )
    if revision is None or not source_only_startup.source_revision_is_current(revision):
        return False
    setattr(agent, _HANDOFF_ATTR, dict(state))
    return True


def take(agent: Any) -> dict[str, Any]:
    """Consume the pending handoff only while its exact source revision is current."""
    raw = getattr(agent, _HANDOFF_ATTR, None)
    with_sentinel = hasattr(agent, _HANDOFF_ATTR)
    if with_sentinel:
        delattr(agent, _HANDOFF_ATTR)
    state = dict(raw or {})
    revision = source_only_startup.SourceRevision.from_mapping(
        dict(state.get("source_revision") or {})
    )
    if revision is None or not source_only_startup.source_revision_is_current(revision):
        return {}
    return state


def clear(agent: Any) -> None:
    """Discard any pending post-verification handoff."""
    if hasattr(agent, _HANDOFF_ATTR):
        delattr(agent, _HANDOFF_ATTR)


def remember_mapping(owner: MutableMapping[str, Any], state: Mapping[str, Any]) -> bool:
    """Store a revision-bound handoff in mutable workflow state."""
    revision = source_only_startup.SourceRevision.from_mapping(
        dict(state.get("source_revision") or {})
    )
    if revision is None or not source_only_startup.source_revision_is_current(revision):
        return False
    owner[_HANDOFF_STATE_KEY] = dict(state)
    return True


def take_mapping(owner: MutableMapping[str, Any]) -> dict[str, Any]:
    """Consume a workflow-state handoff only while its source is unchanged."""
    state = dict(owner.pop(_HANDOFF_STATE_KEY, {}) or {})
    revision = source_only_startup.SourceRevision.from_mapping(
        dict(state.get("source_revision") or {})
    )
    if revision is None or not source_only_startup.source_revision_is_current(revision):
        return {}
    return state
