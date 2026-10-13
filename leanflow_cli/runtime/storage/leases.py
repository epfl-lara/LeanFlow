"""Pin managed storage entries across copying, installation, and live runs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from core.home import leanflow_home
from core.utils import atomic_json_write

try:
    import fcntl
except ImportError:  # pragma: no cover - unsupported platform fails closed
    fcntl = None  # type: ignore[assignment]

_MUTEX = threading.RLock()
_LOCAL = threading.local()


class StorageSafetyError(RuntimeError):
    """Report storage state that cannot safely permit mutation."""


def storage_home(home: Path | None = None) -> Path:
    """Return the canonical shared home using the sole home authority."""
    return (home if home is not None else leanflow_home()).expanduser().resolve()


def confined_path(path: Path, home: Path | None = None) -> Path:
    """Reject links and paths outside explicitly managed storage parents."""
    root = storage_home(home)
    selected = path.expanduser().absolute()
    try:
        relative = selected.relative_to(root)
    except ValueError as exc:
        raise StorageSafetyError(f"unmanaged storage path: {selected}") from exc
    parts = relative.parts
    valid = (
        len(parts) >= 4
        and parts[:3] in {("cache", "dependencies", "v1"), ("cache", "project-builds", "v1")}
        or len(parts) >= 2
        and parts[0] in {"runtimes", "releases"}
    )
    if not valid or ".." in parts:
        raise StorageSafetyError(f"unmanaged storage path: {selected}")
    cursor = root
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise StorageSafetyError(f"linked storage path: {cursor}")
    return selected


def _state_directory(home: Path) -> Path:
    """Create a private registry directory without following a substituted link."""
    path = home / "storage"
    if path.is_symlink():
        raise StorageSafetyError(f"linked storage registry: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextlib.contextmanager
def storage_lock(home: Path | None = None) -> Iterator[None]:
    """Serialize ownership, lease, and eviction transactions across processes."""
    root = storage_home(home)
    key = str(root)
    pid = os.getpid()
    with _MUTEX:
        held: dict[str, tuple[int, IO[str]]] = getattr(_LOCAL, "held", {})
        _LOCAL.held = held
        existing = held.get(key)
        if existing is not None and existing[0] == pid:
            yield
            return
        if existing is not None:
            # A fork must not reuse the parent's advisory lock description.
            existing[1].close()
            held.pop(key, None)
        if fcntl is None:
            raise StorageSafetyError("managed storage requires POSIX file locking")
        lock = _state_directory(root) / "registry.lock"
        if lock.is_symlink():
            raise StorageSafetyError(f"linked storage lock: {lock}")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[key] = (pid, handle)
            try:
                yield
            finally:
                held.pop(key, None)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _process_start(pid: int) -> str | None:
    """Read an operating-system process start identity without environment data."""
    if Path("/proc").is_dir():
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # comm may itself contain spaces or parentheses; fields begin after it.
            return "linux:" + raw[raw.rfind(")") + 2 :].split()[19]
        except (OSError, IndexError):
            return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stdout.strip()
    return f"posix:{text}" if result.returncode == 0 and text else None


def _lease_active(payload: object) -> bool:
    """Protect malformed or unverifiable owners; release only confirmed dead ones."""
    if not isinstance(payload, dict):
        return True
    pid = payload.get("pid")
    expected = payload.get("process_start")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return True
    if not isinstance(expected, str) or not expected:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    current = _process_start(pid)
    return current is None or current == expected


def _lease_directory(entry: Path, home: Path) -> Path:
    """Name a registry slot independently of writable or sealed cache contents."""
    digest = hashlib.sha256(str(entry).encode()).hexdigest()
    directory = _state_directory(home) / "leases" / digest
    for path in (directory.parent, directory):
        if path.is_symlink():
            raise StorageSafetyError(f"linked lease registry: {path}")
    return directory


def entry_is_leased(entry: Path, home: Path | None = None) -> bool:
    """Inspect and discard confirmed stale leases while holding the registry lock."""
    root = storage_home(home)
    entry = confined_path(entry, root)
    directory = _lease_directory(entry, root)
    if not directory.exists():
        return False
    active = False
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            active = True
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            active = True
            continue
        if _lease_active(payload):
            active = True
        else:
            path.unlink()
    return active


@contextlib.contextmanager
def lease_entry(entry: Path, home: Path | None = None) -> Iterator[Path]:
    """Pin a published or future managed entry until the operation completes."""
    root = storage_home(home)
    selected = confined_path(entry, root)
    start = _process_start(os.getpid())
    if start is None:
        raise StorageSafetyError("cannot verify storage lease process identity")
    with storage_lock(root):
        directory = _lease_directory(selected, root)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = directory / f"{uuid.uuid4().hex}.json"
        atomic_json_write(
            record,
            {"pid": os.getpid(), "process_start": start, "entry": str(selected)},
        )
    try:
        yield selected
    finally:
        with storage_lock(root):
            if record.is_symlink():
                raise StorageSafetyError(f"linked lease record: {record}")
            record.unlink(missing_ok=True)
