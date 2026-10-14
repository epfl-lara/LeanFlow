"""Provide conflict-preserving host operations and project publication ownership."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from leanflow_cli.formalization.evidence import (
    contained,
    digest,
    normalized_relative,
    sync_directory,
)

try:
    import fcntl
except ImportError:  # Native Windows has no supported publication contract.
    fcntl = None  # type: ignore[assignment]


class PublicationConflict(ValueError):
    """Preserve all versions and block consumers until an explicit conflict resolution."""


class PublicationUnsupported(RuntimeError):
    """Leave the accepted revision staged on unsupported hosts or filesystems."""


class PublicationCancelled(RuntimeError):
    """Report pre-commit cancellation after attempting a conflict-preserving rollback."""


_owners = threading.local()


def mkdir_durable(path: Path) -> None:
    """Create each missing controller directory and flush its parent entry."""
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for entry in reversed(missing):
        entry.mkdir(exist_ok=True)
        sync_directory(entry.parent)


@contextmanager
def publication_lock(root: Path) -> Iterator[None]:
    """Serialize host mutations, allowing nested checks by the owning thread."""
    if fcntl is None:
        raise PublicationUnsupported(
            "Native host lacks publication locking; revision remains staged"
        )
    identity = str(root.resolve())
    held = getattr(_owners, "held", None)
    if held is None:
        held = _owners.held = {}
    for name, (pid, inherited) in list(held.items()):
        if pid != os.getpid():
            os.close(inherited)
            del held[name]
    if identity in held:
        yield
        return
    lock = contained(root, ".leanflow/formalization-publication.lock")
    mkdir_durable(lock.parent)
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        held[identity] = (os.getpid(), descriptor)
        yield
    finally:
        held.pop(identity, None)
        os.close(descriptor)


def entry_path(root: Path, relative: str) -> Path:
    """Validate parents while retaining an unknown final directory entry for recovery."""
    normalized_relative(relative)
    parent, _, leaf = relative.rpartition("/")
    return (contained(root, parent) if parent else root.resolve()) / leaf


def entry_hash(path: Path) -> str | None:
    """Identify bytes without following links; preserve non-regular entries as unknown."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return "nonregular"
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return "nonregular"
            return digest(handle.read())
    except FileNotFoundError:
        return None


def rename_exclusive(source: Path, target: Path) -> None:
    """Move a directory entry without replacing a noncooperating editor's save."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(libc, "renamex_np", None)
        args = (os.fsencode(source), os.fsencode(target), 0x4)
        types = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        args = (-100, os.fsencode(source), -100, os.fsencode(target), 1)
        types = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    else:
        function, args, types = None, (), ()
    if function is None:
        raise PublicationUnsupported("Host lacks no-replace publication; revision remains staged")
    function.argtypes = list(types)
    function.restype = ctypes.c_int
    if function(*args):
        code = ctypes.get_errno()
        if code in {errno.ENOSYS, errno.ENOTSUP, errno.EXDEV, errno.EINVAL}:
            raise PublicationUnsupported("Filesystem cannot publish with no-replace semantics")
        raise OSError(code, os.strerror(code), str(target))
    sync_directory(source.parent)
    if source.parent != target.parent:
        sync_directory(target.parent)


def check_filesystem(root: Path, directory: Path, targets: list[Path]) -> None:
    """Reject cross-device and unrecognized publication before any project file moves.

    These contracts cover process termination with flushed records. They do not
    claim independently tested power-loss durability or hostile-directory safety.
    """
    if sys.platform == "darwin":
        filesystem, allowed = _darwin_filesystem(root), {"apfs"}
    elif sys.platform.startswith("linux"):
        args = ["stat", "-f", "-c", "%T", str(root)]
        allowed = {"ext2/ext3", "ext4", "xfs", "btrfs", "tmpfs", "overlayfs"}
        try:
            filesystem = (
                subprocess.run(args, check=True, capture_output=True, text=True, timeout=10)
                .stdout.strip()
                .lower()
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise PublicationUnsupported(
                "Cannot establish publication filesystem capabilities"
            ) from error
    else:
        raise PublicationUnsupported(
            "Automatic formalization publication is unavailable on this host"
        )
    if filesystem not in allowed:
        raise PublicationUnsupported(
            f"Automatic publication is not supported on {filesystem}; revision remains staged"
        )
    device = root.stat().st_dev
    inodes: set[tuple[int, int]] = set()
    for candidate in [directory, *targets]:
        cursor = candidate
        while not cursor.exists():
            cursor = cursor.parent
        info = cursor.stat()
        if info.st_dev != device:
            raise PublicationUnsupported("Publication must remain on one supported filesystem")
        if candidate in targets and candidate.exists():
            key = (info.st_dev, info.st_ino)
            if key in inodes:
                raise PublicationConflict(
                    "Multiple publication paths alias the same destination inode"
                )
            inodes.add(key)


def _darwin_filesystem(root: Path) -> str:
    """Read Darwin's statfs64 type name using the public sys/mount.h structure."""

    class Statfs64(ctypes.Structure):
        _fields_ = [
            ("bsize", ctypes.c_uint32),
            ("iosize", ctypes.c_int32),
            ("counts", ctypes.c_uint64 * 5),
            ("fsid", ctypes.c_int32 * 2),
            ("owner", ctypes.c_uint32),
            ("type", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("subtype", ctypes.c_uint32),
            ("name", ctypes.c_char * 16),
            ("mount", ctypes.c_char * 1024),
            ("source", ctypes.c_char * 1024),
            ("flags_ext", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32 * 7),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "statfs64", None)
    if function is None:
        raise PublicationUnsupported("Darwin statfs64 is unavailable")
    function.argtypes = [ctypes.c_char_p, ctypes.POINTER(Statfs64)]
    function.restype = ctypes.c_int
    result = Statfs64()
    if function(os.fsencode(root), ctypes.byref(result)):
        raise PublicationUnsupported("Cannot inspect the publication filesystem")
    return bytes(result.name).decode("ascii", errors="replace").lower()
