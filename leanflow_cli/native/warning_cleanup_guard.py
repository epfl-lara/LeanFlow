"""Track source changes across a theorem's bounded warning-cleanup turn."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, MutableMapping

STATE_KEY = "warning_cleanup_grants"
_MAX_RECORDS = 128


def _scope_key(target_symbol: str, active_file: str) -> str:
    """Return a stable cleanup scope key for one declaration."""
    target = str(target_symbol or "").strip()
    file = os.path.realpath(str(active_file or "").strip())
    return f"{file}::{target}" if target and file else ""


def declaration_digest(declaration: str) -> str:
    """Return the normalized digest used to detect a meaningful source edit."""
    normalized = str(declaration or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.splitlines()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def remember_grant(
    state: MutableMapping[str, object],
    *,
    target_symbol: str,
    active_file: str,
    declaration: str,
) -> None:
    """Remember the source image for which cleanup feedback was granted."""
    key = _scope_key(target_symbol, active_file)
    digest = declaration_digest(declaration)
    if not key or not digest:
        return
    raw = state.setdefault(STATE_KEY, {})
    records = dict(raw) if isinstance(raw, Mapping) else {}
    records[key] = digest
    if len(records) > _MAX_RECORDS:
        records = dict(list(records.items())[-_MAX_RECORDS:])
    state[STATE_KEY] = records


def unchanged_since_grant(
    state: Mapping[str, object],
    *,
    target_symbol: str,
    active_file: str,
    declaration: str,
) -> bool:
    """Return whether a cleanup recheck observes the exact granted source image."""
    key = _scope_key(target_symbol, active_file)
    digest = declaration_digest(declaration)
    raw = state.get(STATE_KEY)
    records = dict(raw) if isinstance(raw, Mapping) else {}
    return bool(key and digest and records.get(key) == digest)


def clear_grant(
    state: MutableMapping[str, object],
    *,
    target_symbol: str,
    active_file: str,
) -> None:
    """Forget cleanup source identity after an edit is accepted or scope changes."""
    key = _scope_key(target_symbol, active_file)
    raw = state.get(STATE_KEY)
    if not key or not isinstance(raw, Mapping):
        return
    records = dict(raw)
    records.pop(key, None)
    if records:
        state[STATE_KEY] = records
    else:
        state.pop(STATE_KEY, None)
