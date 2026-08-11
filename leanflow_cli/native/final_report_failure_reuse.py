"""Guard same-turn reuse of unchanged exact-target rejection evidence."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_KEY = "_final_report_failure_check"
SNAPSHOT_ATTR = "_managed_exact_check_source_snapshot"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REPLACEMENT_KEYS = frozenset(
    {
        "replacement_matches_target",
        "replacement_declarations",
        "replacement_mismatch_reason",
        "verification_scope",
    }
)


def source_sha256(path: str | Path) -> str:
    """Return a raw file SHA-256, or empty when the source is unavailable."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


@dataclass(frozen=True)
class PreCheckIdentity:
    """Bind one exact check request to its assignment, source, and provider turn."""

    assignment_scope: str
    source_sha256: str
    provider_turn_key: str

    @property
    def valid(self) -> bool:
        """Return whether every fail-closed identity field is present and valid."""
        return bool(
            self.assignment_scope
            and self.provider_turn_key
            and _SHA256_RE.fullmatch(self.source_sha256)
        )

    def to_mapping(self, *, target_symbol: str, active_file: str) -> dict[str, str]:
        """Render the pre-tool snapshot with diagnostic assignment labels."""
        return {
            "assignment_scope": self.assignment_scope,
            "target_symbol": str(target_symbol or ""),
            "active_file": str(active_file or ""),
            "source_sha256": self.source_sha256,
            "provider_turn_key": self.provider_turn_key,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> PreCheckIdentity:
        """Parse a pre-tool identity without trusting unknown persisted fields."""
        value = dict(raw or {})
        return cls(
            assignment_scope=str(value.get("assignment_scope", "") or ""),
            source_sha256=str(value.get("source_sha256", "") or ""),
            provider_turn_key=str(value.get("provider_turn_key", "") or ""),
        )


@dataclass(frozen=True)
class FailureIdentity:
    """Bind cached negative evidence to the complete current declaration state."""

    assignment_scope: str
    source_sha256: str
    declaration_sha256: str
    provider_turn_key: str

    @property
    def valid(self) -> bool:
        """Return whether this identity is complete enough to authorize reuse."""
        return bool(
            self.assignment_scope
            and self.provider_turn_key
            and _SHA256_RE.fullmatch(self.source_sha256)
            and _SHA256_RE.fullmatch(self.declaration_sha256)
        )

    def matches_precheck(self, precheck: PreCheckIdentity) -> bool:
        """Return whether the exact source remained stable across the tool call."""
        return bool(
            self.valid
            and precheck.valid
            and self.assignment_scope == precheck.assignment_scope
            and self.source_sha256 == precheck.source_sha256
            and self.provider_turn_key == precheck.provider_turn_key
        )

    def to_mapping(self) -> dict[str, str]:
        """Render the exact failure identity for the transient cache entry."""
        return {
            "assignment_scope": self.assignment_scope,
            "source_sha256": self.source_sha256,
            "declaration_sha256": self.declaration_sha256,
            "provider_turn_key": self.provider_turn_key,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> FailureIdentity:
        """Parse a cached identity without accepting missing legacy fields."""
        value = dict(raw or {})
        return cls(
            assignment_scope=str(value.get("assignment_scope", "") or ""),
            source_sha256=str(value.get("source_sha256", "") or ""),
            declaration_sha256=str(value.get("declaration_sha256", "") or ""),
            provider_turn_key=str(value.get("provider_turn_key", "") or ""),
        )


def completed_on_disk_failure_payload(
    manager_check: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Return a completed source-check payload that is eligible for negative reuse.

    This only recognizes the structural envelope. The runner still supplies
    exact-assignment matching and mathematical-vs-operational classification.
    """
    checked = dict(manager_check or {})
    incremental = checked.get("incremental")
    payload: Mapping[str, Any] = incremental if isinstance(incremental, Mapping) else checked
    action = str(payload.get("action", "") or "").strip().lower().replace("-", "_")
    if (
        action != "check_target"
        or payload.get("success") is not True
        or bool(checked.get("ok"))
        or bool(payload.get("timed_out"))
        or bool(payload.get("cancelled"))
        or bool(str(payload.get("error_code", "") or "").strip())
        or any(key in payload for key in _REPLACEMENT_KEYS)
    ):
        return None
    return payload


def remember(
    state: dict[str, Any],
    *,
    precheck: PreCheckIdentity,
    identity: FailureIdentity,
    manager_check: Mapping[str, Any],
    manager_tool: str,
    reusable: bool,
) -> None:
    """Replace the transient cache with one fail-closed exact rejection."""
    state.pop(STATE_KEY, None)
    if (
        manager_tool != "lean_incremental_check"
        or not reusable
        or not identity.matches_precheck(precheck)
    ):
        return
    state[STATE_KEY] = {
        **identity.to_mapping(),
        "manager_tool": manager_tool,
        "manager_check": copy.deepcopy(dict(manager_check)),
    }


def take(
    state: dict[str, Any],
    *,
    identity: FailureIdentity,
) -> tuple[dict[str, Any], str] | None:
    """Take cached negative evidence only under an identical current identity."""
    cached = state.pop(STATE_KEY, None)
    if not isinstance(cached, Mapping) or not identity.valid:
        return None
    entry = dict(cached)
    cached_identity = FailureIdentity.from_mapping(entry)
    manager_tool = str(entry.get("manager_tool", "") or "")
    manager_check = entry.get("manager_check")
    if (
        cached_identity != identity
        or manager_tool != "lean_incremental_check"
        or not isinstance(manager_check, Mapping)
    ):
        return None
    return copy.deepcopy(dict(manager_check)), manager_tool
