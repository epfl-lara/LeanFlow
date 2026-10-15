"""Check actual immutable dependency revisions without trusting manifest-only readiness."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

_PIN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_METADATA_ENTRIES = 262144


def _metadata(checkout: Path, check: Callable[[], None]) -> tuple[int, int]:
    """Reject worktree pointers, aliases and nonregular Git metadata before invoking Git."""
    check()
    if checkout.is_symlink() or not checkout.is_dir() or checkout.resolve() != checkout.absolute():
        raise ValueError(f"Dependency checkout is missing or aliased: {checkout}")
    metadata = checkout / ".git"
    if metadata.is_symlink() or not metadata.is_dir():
        raise ValueError(f"Dependency requires an ordinary private Git directory: {metadata}")
    for unsupported in (metadata / "commondir", metadata / "objects/info/alternates"):
        if unsupported.exists() or unsupported.is_symlink():
            raise ValueError(f"Dependency cannot borrow shared Git metadata: {unsupported}")
    for name in ("HEAD", "config"):
        path = metadata / name
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError(f"Dependency Git control is missing or aliased: {path}")
    count = 0
    for directory, names, files in os.walk(metadata, followlinks=False):
        check()
        for name in (*names, *files):
            count += 1
            if count > _METADATA_ENTRIES:
                raise ValueError("Dependency Git metadata exceeds its supported entry bound")
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError(
                    f"Dependency Git metadata contains an alias or nonregular entry: {path}"
                )
    info = metadata.stat()
    return info.st_dev, info.st_ino


def require_checkout(checkout: Path, revision: str, *, deadline: float | None = None) -> None:
    """Require a confined ordinary Git checkout at the exact immutable commit, without networking."""
    if not isinstance(revision, str) or not _PIN.fullmatch(revision):
        raise ValueError("Dependency checkout verification requires an immutable full commit")

    def check() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Dependency checkout verification deadline exceeded")

    metadata_identity = _metadata(checkout, check)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    result = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-C",
            str(checkout),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            "rev-parse",
            "--show-toplevel",
            "HEAD^{commit}",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10 if deadline is None else min(10, max(0.001, deadline - time.monotonic())),
        check=False,
    )
    check()
    lines = result.stdout.strip().splitlines()
    if (
        result.returncode
        or len(lines) != 2
        or Path(lines[0]).resolve() != checkout.resolve()
        or lines[1] != revision
        or _metadata(checkout, check) != metadata_identity
    ):
        raise ValueError(
            f"Dependency checkout differs from its requested immutable revision: {checkout}"
        )
