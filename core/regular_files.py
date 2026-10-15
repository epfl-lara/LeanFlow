"""Read bounded regular files without blocking on substituted special files."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_regular_bytes(path: Path, limit: int) -> bytes:
    """Reject aliases and changed versions before returning at most the declared byte bound."""
    if type(limit) is not int or limit < 0:
        raise ValueError("A regular-file read requires a nonnegative byte bound")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError(f"Expected a bounded regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)

        def version(value: os.stat_result) -> tuple[int, ...]:
            """Retain inode and mutation identity while ignoring access-time changes."""
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if version(opened) != version(before):
            raise ValueError(f"Regular file changed before reading: {path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if (
            len(data) > limit
            or version(os.fstat(fd)) != version(opened)
            or version(path.lstat()) != version(before)
        ):
            raise ValueError(f"Regular file changed while reading: {path}")
        return data
    finally:
        os.close(fd)
