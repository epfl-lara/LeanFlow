"""Coalesce public native fragments into bounded, invocation-scoped observations."""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from .observations import Observation, native_observations

MAX_LIVE_CHARS = 16_384
MAX_LIVE_ITEMS = 128
UPDATE_INTERVAL = 0.35


class NativeObservationStream:
    """Retain short live previews while completed native records stay authoritative."""

    def __init__(self, backend: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.backend = backend
        self.clock = clock
        self.invocation_id = uuid.uuid4().hex
        self.pending: OrderedDict[tuple[str, str], Observation] = OrderedDict()
        self.published: dict[tuple[str, str], tuple[float, str]] = {}
        self.tools: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def _decorate(self, observation: Observation) -> Observation:
        """Attach invocation identity and measured tool duration to each observation."""
        kind, details = observation
        details = {**details, "invocation_id": self.invocation_id}
        item_id = str(details.get("item_id") or "")
        if kind == "agent-tool-start" and item_id:
            self.tools[item_id] = (self.clock(), details)
            while len(self.tools) > MAX_LIVE_ITEMS:
                self.tools.popitem(last=False)
        elif kind in {"agent-tool-progress", "agent-tool-end"} and item_id in self.tools:
            started, original = self.tools[item_id]
            details = {**original, **details, "duration_seconds": max(0.0, self.clock() - started)}
            if kind == "agent-tool-end":
                self.tools.pop(item_id)
        return kind, details

    def _delta(self, event: dict[str, Any]) -> list[Observation]:
        """Append only the documented public text and command-output delta fields."""
        item_id, field = event.get("item_id"), event.get("field")
        item_type, delta = event.get("item_type"), event.get("delta")
        allowed = {
            ("agent_message", "text"),
            ("reasoning", "text"),
            ("reasoning", "raw_text"),
            ("command_execution", "aggregated_output"),
        }
        if (
            not isinstance(item_id, str)
            or not item_id
            or not isinstance(delta, str)
            or not delta
            or not isinstance(item_type, str)
            or not isinstance(field, str)
            or (item_type, field) not in allowed
        ):
            return []
        kind = (
            "agent-tool-progress"
            if field == "aggregated_output"
            else ("agent-reasoning" if item_type == "reasoning" else "agent-output")
        )
        channel = (
            ("trace" if field == "raw_text" else "summary") if kind == "agent-reasoning" else kind
        )
        old = self.pending.get((item_id, channel))
        content_key = "result" if kind == "agent-tool-progress" else "text"
        text = str(old[1].get(content_key) or "") if old else ""
        details: dict[str, Any] = {
            "backend": self.backend,
            "native_event": "item.delta",
            "item_id": item_id,
            "partial": True,
            content_key: (text + delta)[-MAX_LIVE_CHARS:],
        }
        if kind == "agent-reasoning":
            details["reasoning_kind"] = "trace" if field == "raw_text" else "summary"
        if kind == "agent-tool-progress":
            details["status"] = "in_progress"
        return [self._decorate((kind, details))]

    @staticmethod
    def _key(observation: Observation) -> tuple[str, str]:
        """Keep reasoning summaries and traces independent for the same native item."""
        kind, details = observation
        channel = str(details.get("reasoning_kind") or kind)
        return str(details.get("item_id") or ""), channel

    def consume(self, event: dict[str, Any]) -> list[Observation]:
        """Publish throttled live snapshots and every authoritative completion."""
        if event.get("type") == "harness.settings" and event.get("invocation_id"):
            self.invocation_id = str(event["invocation_id"])
        observations = (
            self._delta(event)
            if event.get("type") == "item.delta"
            else [self._decorate(item) for item in native_observations(self.backend, event)]
        )
        item = event.get("item")
        completed_id = (
            str(item.get("id") or "")
            if event.get("type") == "item.completed" and isinstance(item, dict)
            else ""
        )
        if completed_id:
            final_keys = {self._key(observation) for observation in observations}
            for key, (kind, details) in list(self.pending.items()):
                if (
                    key[0] == completed_id
                    and key not in final_keys
                    and kind != "agent-tool-progress"
                ):
                    observations.append(
                        self._decorate(
                            (
                                kind,
                                {
                                    **details,
                                    "text": "",
                                    "partial": False,
                                    "native_event": "item.completed",
                                    "status": "completed",
                                },
                            )
                        )
                    )
        result: list[Observation] = []
        for observation in observations:
            kind, details = observation
            key = self._key(observation)
            # Deltas use the same channel keys as snapshots and terminal records.
            if details.get("partial") is True and key[0]:
                for field in ("text", "result"):
                    if isinstance(details.get(field), str):
                        details[field] = details[field][-MAX_LIVE_CHARS:]
                self.pending[key] = observation
                self.pending.move_to_end(key)
                while len(self.pending) > MAX_LIVE_ITEMS:
                    expired, _ = self.pending.popitem(last=False)
                    self.published.pop(expired, None)
                at, previous = self.published.get(key, (-float("inf"), ""))
                content = str(details.get("text") or details.get("result") or "")
                if content != previous and self.clock() - at >= UPDATE_INTERVAL:
                    self.published[key] = (self.clock(), content)
                    result.append(observation)
            else:
                if kind == "agent-tool-end":
                    self.pending.pop((key[0], "agent-tool-progress"), None)
                    self.published.pop((key[0], "agent-tool-progress"), None)
                self.pending.pop(key, None)
                self.published.pop(key, None)
                result.append(observation)
        if completed_id:
            for key in list(self.pending):
                if key[0] == completed_id:
                    self.pending.pop(key)
                    self.published.pop(key, None)
        return result

    def flush(self) -> list[Observation]:
        """Publish the last changed preview without implying interrupted items completed."""
        result = []
        for key, observation in self.pending.items():
            content = str(observation[1].get("text") or observation[1].get("result") or "")
            if self.published.get(key, (0.0, ""))[1] != content:
                result.append(observation)
        self.pending.clear()
        self.published.clear()
        return result
