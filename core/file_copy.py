"""Copy isolated files with shared disk blocks and publish complete directory copies."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path

_UNSUPPORTED = {errno.EXDEV, errno.EINVAL, errno.ENOSYS, errno.ENOTTY, errno.EOPNOTSUPP}
_FREE_RESERVE = 256 * 1024 * 1024


def _clone_file(source: Path, destination: Path) -> bool:
    """Clone disk extents into a distinct inode, or report unsupported filesystems."""
    if sys.platform == "darwin":
        library = ctypes.CDLL(None, use_errno=True)
        clone = getattr(library, "clonefile", None)
        if clone is None:
            return False
        clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        clone.restype = ctypes.c_int
        # Never follow a source symlink or copy ownership into the private workspace.
        if clone(os.fsencode(source), os.fsencode(destination), 3) == 0:
            return True
        error = ctypes.get_errno()
    elif sys.platform == "linux":
        import fcntl

        with source.open("rb") as original, destination.open("xb") as copied:
            try:
                fcntl.ioctl(copied.fileno(), 0x40049409, original.fileno())  # FICLONE
                return True
            except OSError as exc:
                error = exc.errno
        destination.unlink()
    else:
        return False
    if error in _UNSUPPORTED:
        return False
    raise OSError(error, os.strerror(error or errno.EIO), str(destination))


def copy_file(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    reserve_bytes: int = _FREE_RESERVE,
) -> str:
    """Copy bytes and metadata without shared inodes, preferring copy-on-write storage.

    Unsupported filesystems use an exclusive ordinary copy. That fallback reserves
    disk space for checkpoints and fails before consuming the last available bytes.
    Permission errors and exhausted clone operations never trigger a full-copy retry.
    """
    original, copied = Path(source), Path(destination)
    if isinstance(reserve_bytes, bool) or reserve_bytes < 0:
        raise ValueError("Copy reserve must be nonnegative bytes")
    if original.is_symlink():
        raise ValueError("Isolated file copies cannot follow symlinks")
    if copied.exists() or copied.is_symlink():
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(copied))
    created = False
    try:
        if _clone_file(original, copied):
            created = True
        else:
            required = original.stat().st_size + reserve_bytes
            if shutil.disk_usage(copied.parent).free < required:
                raise OSError(
                    errno.ENOSPC,
                    "Insufficient disk space for an isolated copy and checkpoint reserve; "
                    "use a copy-on-write filesystem or reclaim dependency caches",
                    str(copied),
                )
            with original.open("rb") as incoming, copied.open("xb") as outgoing:
                created = True
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        shutil.copystat(original, copied, follow_symlinks=False)
    except BaseException:
        if created:
            copied.unlink(missing_ok=True)
        raise
    return str(copied)


def copy_tree(
    source: Path,
    destination: Path,
    *,
    ignore: Callable[[str, list[str]], Iterable[str]] | None = None,
    reserve_bytes: int | None = None,
) -> Path:
    """Publish a complete isolated directory copy and remove failed staging trees."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".copy-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "tree"
        copier = (
            copy_file if reserve_bytes is None else partial(copy_file, reserve_bytes=reserve_bytes)
        )
        shutil.copytree(source, staged, copy_function=copier, symlinks=True, ignore=ignore)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(str(destination))
        staged.rename(destination)
    return destination
