"""Capture and revalidate workflow process ownership identities."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROCESS_TOKEN_ENV = "LEANFLOW_NATIVE_PROCESS_TOKEN"
_TOKEN_PATTERN = re.compile(rf"(?:^|\s){re.escape(PROCESS_TOKEN_ENV)}=([A-Za-z0-9_-]+)(?=\s|$)")


@dataclass(frozen=True)
class ProcessIdentity:
    """Describe one workflow process using a launch token and POSIX boundaries."""

    pid: int
    process_group_id: int
    session_id: int
    token_sha256: str

    @property
    def verifiable(self) -> bool:
        """Return whether this identity contains the token required for signaling."""
        return self.pid > 1 and bool(self.token_sha256)


def process_token_sha256(token: str) -> str:
    """Return the stable SHA-256 fingerprint for one opaque launch token."""
    normalized = str(token or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def current_process_identity() -> ProcessIdentity:
    """Return the current process identity without minting an unverifiable token."""
    pid = os.getpid()
    try:
        process_group_id = os.getpgid(pid)
    except (AttributeError, OSError):
        process_group_id = 0
    try:
        session_id = os.getsid(pid)
    except (AttributeError, OSError):
        session_id = 0
    return ProcessIdentity(
        pid=pid,
        process_group_id=process_group_id,
        session_id=session_id,
        token_sha256=process_token_sha256(os.getenv(PROCESS_TOKEN_ENV, "")),
    )


def process_identity_from_mapping(payload: object) -> ProcessIdentity:
    """Build a process identity from persisted workflow fields."""
    if not isinstance(payload, Mapping):
        return ProcessIdentity(0, 0, 0, "")

    def integer(key: str) -> int:
        try:
            return int(payload.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return ProcessIdentity(
        pid=integer("process_id"),
        process_group_id=integer("process_group_id"),
        session_id=integer("process_session_id"),
        token_sha256=str(payload.get("process_token_sha256", "") or "").strip(),
    )


def process_identity_details(identity: ProcessIdentity | None = None) -> dict[str, object]:
    """Return workflow-state fields for one process identity."""
    selected = identity or current_process_identity()
    return {
        "process_id": selected.pid,
        "process_group_id": selected.process_group_id,
        "process_session_id": selected.session_id,
        "process_token_sha256": selected.token_sha256,
    }


def _linux_process_token(pid: int) -> str:
    """Read an initial process token from procfs when available."""
    path = Path(f"/proc/{pid}/environ")
    if not path.is_file():
        return ""
    try:
        entries = path.read_bytes().split(b"\0")
    except (OSError, PermissionError):
        return ""
    prefix = f"{PROCESS_TOKEN_ENV}=".encode()
    for entry in entries:
        if entry.startswith(prefix):
            return entry[len(prefix) :].decode("utf-8", errors="replace").strip()
    return ""


def _posix_process_token(pid: int) -> str:
    """Read an initial process token from a POSIX process listing."""
    try:
        completed = subprocess.run(
            ["ps", "eww", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    match = _TOKEN_PATTERN.search(completed.stdout)
    return match.group(1) if match else ""


def _process_token(pid: int) -> str:
    """Return the launch token still attached to one live process."""
    token = _linux_process_token(pid)
    if token:
        return token
    if os.name == "posix":
        return _posix_process_token(pid)
    return ""


def process_identity_matches(identity: ProcessIdentity) -> bool:
    """Revalidate exact workflow ownership and fail closed when it is unavailable."""
    if not identity.verifiable:
        return False
    if identity.pid == os.getpid():
        current = current_process_identity()
        return bool(
            current.token_sha256 == identity.token_sha256
            and (
                identity.process_group_id <= 0
                or current.process_group_id == identity.process_group_id
            )
            and (identity.session_id <= 0 or current.session_id == identity.session_id)
        )
    try:
        if identity.process_group_id > 0 and os.getpgid(identity.pid) != identity.process_group_id:
            return False
        if identity.session_id > 0 and os.getsid(identity.pid) != identity.session_id:
            return False
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        return False
    return process_token_sha256(_process_token(identity.pid)) == identity.token_sha256
