"""Authorize one exact source edit from previously verified evidence."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerifiedEditAuthorization:
    """Describe one expiring, single-use source transition."""

    token: str
    path: str
    theorem_id: str
    before_sha256: str
    after_sha256: str
    verified_declaration: str
    axiom_profile_axioms: tuple[str, ...]
    expires_at: float


_LOCK = threading.Lock()
_AUTHORIZATIONS: dict[str, VerifiedEditAuthorization] = {}


def _canonical_path(value: str) -> str:
    """Return a stable path spelling without requiring the file to exist."""
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return ""


def _prune(now: float) -> None:
    """Drop expired authorizations while the registry lock is held."""
    expired = [
        token for token, authorization in _AUTHORIZATIONS.items() if authorization.expires_at <= now
    ]
    for token in expired:
        _AUTHORIZATIONS.pop(token, None)


def register(
    *,
    path: str,
    theorem_id: str,
    before_sha256: str,
    after_sha256: str,
    verified_declaration: str,
    axiom_profile_axioms: tuple[str, ...],
    ttl_s: float = 300.0,
) -> str:
    """Register one exact edit authorization and return its opaque token."""
    canonical = _canonical_path(path)
    if (
        not canonical
        or not theorem_id.strip()
        or len(before_sha256) != 64
        or len(after_sha256) != 64
        or not verified_declaration.strip()
    ):
        return ""
    now = time.monotonic()
    token = secrets.token_urlsafe(32)
    authorization = VerifiedEditAuthorization(
        token=token,
        path=canonical,
        theorem_id=theorem_id.strip(),
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        verified_declaration=verified_declaration.strip(),
        axiom_profile_axioms=tuple(axiom_profile_axioms),
        expires_at=now + max(1.0, float(ttl_s)),
    )
    with _LOCK:
        _prune(now)
        _AUTHORIZATIONS[token] = authorization
    return token


def consume(
    token: str,
    *,
    path: str,
    theorem_id: str,
    before_sha256: str,
    after_sha256: str,
) -> VerifiedEditAuthorization | None:
    """Consume and return an authorization only when every source identity matches."""
    opaque = str(token or "").strip()
    if not opaque:
        return None
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        authorization = _AUTHORIZATIONS.pop(opaque, None)
    if authorization is None:
        return None
    if (
        authorization.path != _canonical_path(path)
        or authorization.theorem_id != str(theorem_id or "").strip()
        or authorization.before_sha256 != str(before_sha256 or "").strip()
        or authorization.after_sha256 != str(after_sha256 or "").strip()
    ):
        return None
    return authorization


def clear_for_tests() -> None:
    """Clear process-local authorizations for isolated tests."""
    with _LOCK:
        _AUTHORIZATIONS.clear()
