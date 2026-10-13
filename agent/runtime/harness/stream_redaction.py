"""Publish additive native fragments without exposing split credential material."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .redaction import redact_native_value

MAX_PENDING_CHARS = 8192
MAX_PENDING_FIELDS = 128
_SECRET_NAME = r"[A-Z_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Z_]*"
_JSON_SECRET_NAME = (
    r"api_?key|token|secret|password|access_token|refresh_token|auth_token|bearer|"
    r"secret_value|raw_secret|secret_input|key_material"
)
_PENDING_PATTERNS = (
    # An assignment's name and operator may arrive before its value. Keep that
    # context so ordinary redaction still recognizes a value in a later delta.
    re.compile(
        rf"(?<![\w]){_SECRET_NAME}(?:\s*(?:=\s*(?:\"[^\"]*|'[^']*|[^\s'\"]*)?)?)?\Z",
        re.IGNORECASE,
    ),
    re.compile(r"\bAuthorization\s*:\s*[^\r\n]*\Z", re.IGNORECASE),
    re.compile(r"\bBearer(?:\s+[^\r\n]*)?\Z", re.IGNORECASE),
    re.compile(rf'"(?:{_JSON_SECRET_NAME})"\s*(?::\s*(?:"[^"]*)?)?\Z', re.IGNORECASE),
    re.compile(r"-----BEGIN\b[^\r\n]*\Z"),
    # Short unfinished tokens fall below the normal full-token regex thresholds.
    re.compile(r"(?<![\w-])(?:sk-|ghp_|github_pat_|xox[baprs]-|pplx-|hf_|npm_)[\w-]*\Z"),
)
_PRIVATE_START = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")
_PRIVATE_END = re.compile(r"-----END[A-Z ]*PRIVATE KEY-----")
_QUOTED_ASSIGNMENT = re.compile(
    rf"(?<![\w])({_SECRET_NAME})\s*=\s*(['\"])(.*?)\2", re.IGNORECASE | re.DOTALL
)
_JSON_CREDENTIAL = re.compile(rf'"(?:{_JSON_SECRET_NAME})"\s*:\s*"[^"]*"', re.IGNORECASE)
_PRIVATE_BLOCK = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)
_BARE_BEARER = re.compile(r"(\bBearer\s+)(\S+)", re.IGNORECASE)
_ALLOWED_FIELDS = {
    "reasoning": frozenset({"text", "raw_text"}),
    "agent_message": frozenset({"text"}),
    "command_execution": frozenset({"aggregated_output"}),
}


@dataclass
class _PendingField:
    """Retain one incomplete token and the metadata needed for its safe delta."""

    metadata: dict[str, str]
    text: str = ""
    suppressed: bool = False


class NativeStreamRedactor:
    """Delay incomplete tokens while preserving additive public stream semantics.

    State belongs to one invocation and one item/field. A field that exceeds the
    bounded pending buffer is suppressed until its authoritative completed item,
    preventing the discarded token's later suffix from escaping as new prose.
    """

    def __init__(
        self,
        secrets: tuple[str, ...],
        *,
        max_pending_chars: int = MAX_PENDING_CHARS,
        max_fields: int = MAX_PENDING_FIELDS,
    ) -> None:
        self.secrets = secrets
        self.max_pending_chars = max(1, max_pending_chars)
        self.max_fields = max(1, max_fields)
        self._pending: dict[tuple[str, str], _PendingField] = {}

    def _unsafe_start(self, text: str) -> int:
        """Find credential context or an exact-secret prefix that must remain private."""
        boundary = len(text)
        for pattern in _PENDING_PATTERNS:
            if match := pattern.search(text):
                boundary = min(boundary, match.start())
        for match in _PRIVATE_START.finditer(text):
            if _PRIVATE_END.search(text, match.end()) is None:
                boundary = min(boundary, match.start())
                break
        for secret in self.secrets:
            # Exact values can contain whitespace, so holding the final word
            # alone does not protect a credential split across word boundaries.
            for size in range(min(len(text), len(secret) - 1), 0, -1):
                if text.endswith(secret[:size]):
                    boundary = min(boundary, len(text) - size)
                    break
        return boundary

    def _redact(self, text: str) -> str:
        """Mask complete values and explicitly labelled quoted or bearer credentials."""
        # Apply exact values before pattern substitutions can split a credential
        # containing spaces or a nested assignment into unmatched fragments.
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = _QUOTED_ASSIGNMENT.sub(lambda match: f"{match[1]}=[REDACTED]", text)
        text = _BARE_BEARER.sub(lambda match: match[1] + "[REDACTED]", text)
        return str(redact_native_value(text, self.secrets))

    def _split(self, text: str, *, final: bool) -> tuple[str, str]:
        """Release only stable redacted text, suppressing an unfinished secret on flush."""
        boundary = self._unsafe_start(text)
        if not final:
            trailing = re.search(r"\S*\Z", text)
            assert trailing is not None
            boundary = min(boundary, trailing.start())
        # A complete secret may span several words. A token boundary inside it
        # would release a prefix that the complete-value redactor cannot mask.
        spans = [
            (match.start(), match.end())
            for pattern in (_QUOTED_ASSIGNMENT, _JSON_CREDENTIAL, _PRIVATE_BLOCK)
            for match in pattern.finditer(text)
        ]
        for secret in self.secrets:
            if not secret:
                continue
            start = text.find(secret)
            while start >= 0:
                spans.append((start, start + len(secret)))
                start = text.find(secret, start + len(secret))
        for start, end in sorted(spans, reverse=True):
            if start < boundary < end:
                boundary = start
        public = self._redact(text[:boundary])
        pending = text[boundary:]
        if final and pending:
            return public + "[REDACTED]", ""
        return public, pending

    def _record(self, state: _PendingField, text: str) -> dict[str, Any]:
        """Return one safe additive fragment without changing its native item identity."""
        return {"type": "item.delta", **state.metadata, "delta": text}

    def _flush_keys(self, keys: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """Safely close selected fields without guessing whether a token will continue."""
        result: list[dict[str, Any]] = []
        for key in keys:
            state = self._pending.pop(key)
            if not state.suppressed:
                text, _pending = self._split(state.text, final=True)
                if text:
                    result.append(self._record(state, text))
        return result

    def consume(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Return safe public records while the caller consumes the original native event."""
        if event.get("type") != "item.delta":
            result: list[dict[str, Any]] = []
            item = event.get("item")
            if event.get("type") == "item.completed" and isinstance(item, dict):
                item_id = item.get("id")
                result.extend(self._flush_keys([key for key in self._pending if key[0] == item_id]))
                # Authoritative completion replaces live output, so it must not
                # reintroduce a credential masked in the additive stream.
                item = dict(item)
                item_type = item.get("type")
                if isinstance(item_type, str):
                    for completed_field in _ALLOWED_FIELDS.get(item_type, frozenset()):
                        if isinstance(item.get(completed_field), str):
                            item[completed_field], _pending = self._split(
                                item[completed_field], final=True
                            )
                event = {**event, "item": item}
            result.append(redact_native_value(event, self.secrets))
            return result
        item_id, item_type, field, delta = (
            event.get("item_id"),
            event.get("item_type"),
            event.get("field"),
            event.get("delta"),
        )
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 256
            or not isinstance(item_type, str)
            or not isinstance(field, str)
            or field not in _ALLOWED_FIELDS.get(item_type, frozenset())
            or not isinstance(delta, str)
        ):
            # An unscoped or unrecognized delta cannot safely share buffering
            # state. Do not persist its fragment as an unrelated generic event.
            return []
        key = (item_id, field)
        state = self._pending.get(key)
        if state is None:
            if len(self._pending) >= self.max_fields:
                return []
            metadata = {"item_id": item_id, "item_type": item_type, "field": field}
            reasoning_kind = event.get("reasoning_kind")
            if isinstance(reasoning_kind, str) and reasoning_kind in {"summary", "trace"}:
                metadata["reasoning_kind"] = reasoning_kind
            state = self._pending[key] = _PendingField(metadata)
        elif state.metadata["item_type"] != item_type:
            state.text = ""
            state.suppressed = True
            return []
        if state.suppressed:
            return []
        public, state.text = self._split(state.text + delta, final=False)
        if len(state.text) > self.max_pending_chars:
            state.text = ""
            state.suppressed = True
            public += "[REDACTED: incomplete streamed token]"
        return [self._record(state, public)] if public else []

    def flush(self) -> list[dict[str, Any]]:
        """Close all pending fragments safely when the invocation ends or is interrupted."""
        return self._flush_keys(list(self._pending))
