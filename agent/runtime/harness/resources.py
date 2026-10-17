"""Measure identity-bound native process memory and enforce bounded sampling failures."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import BackendError
from .processes import ProcessTree, process_inspection_environment, start_identity_matches

SAMPLE_INTERVAL_S = 0.25


class NativeMemoryLimit(BackendError):
    """Stop a native invocation whose sampled owned RSS exceeds its configured ceiling."""


class NativeResourceUnavailable(BackendError):
    """Stop after bounded consecutive failures to measure owned process memory."""


def native_memory_limit_bytes(environment: Mapping[str, str]) -> int:
    """Resolve the native 1–20 GiB ceiling before admitting any native process."""
    raw = environment.get(
        "LEANFLOW_NATIVE_MAX_RSS_GIB", environment.get("LEANFLOW_SETUP_MAX_RSS_GIB", "16")
    )
    try:
        gib = int(raw) if isinstance(raw, str) else 0
    except (TypeError, ValueError):
        gib = 0
    if not 1 <= gib <= 20:
        raise BackendError(
            "Native memory must be a whole number from 1 to 20 GiB. "
            "Set LEANFLOW_NATIVE_MAX_RSS_GIB, or its LEANFLOW_SETUP_MAX_RSS_GIB fallback."
        )
    return gib * 1024**3


def _rss_entries() -> dict[int, tuple[str, int]]:
    """Read one complete strict memory table without collecting arguments or environment."""
    table = subprocess.run(
        ["/bin/ps", "-axo", "pid=,rss=,lstart="],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
        env=process_inspection_environment(),
    )
    entries: dict[int, tuple[str, int]] = {}
    for line in table.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if (
            len(parts) != 3
            or not parts[0].isdigit()
            or not parts[1].isdigit()
            or len(parts[2].split()) != 5
        ):
            raise ValueError("Native memory measurement cannot validate the process table")
        pid = int(parts[0])
        if pid <= 0 or pid in entries:
            raise ValueError("Native memory measurement received an invalid process identity")
        entries[pid] = (" ".join(parts[2].split()), int(parts[1]) * 1024)
    if not entries:
        raise ValueError("Native memory measurement received an empty process table")
    return entries


def _observed_total(entries: dict[int, tuple[str, int]], identities: Mapping[int, str]) -> int:
    """Count only cached matching start identities, never rediscovering ownership."""
    total = 0
    for pid, identity in identities.items():
        if (
            type(pid) is not int
            or pid <= 0
            or not isinstance(identity, str)
            or not identity.strip()
        ):
            raise ValueError("Native memory measurement requires observed process identities")
        current = entries.get(pid)
        if current is not None and start_identity_matches(current[0], identity):
            total += current[1]
    return total


def observed_descendant_rss_bytes(identities: Mapping[int, str]) -> int:
    """Measure an exact already-observed descendant set without adding its controller.

    Exited or reused identities contribute nothing. Empty or malformed inspection
    output still raises; an empty owned set never turns failed inspection into zero.
    """
    observed = dict(identities)
    return _observed_total(_rss_entries(), observed)


def owned_rss_bytes(process: subprocess.Popen[bytes], tree: ProcessTree) -> int:
    """Sum sampled RSS only for the original unreaped root and matching tracked descendants.

    Sampling cannot bound a process peak between observations. Missing or malformed
    metadata cannot establish zero usage; callers must stop or retry boundedly.
    """
    if tree.process is not process or tree.root != process.pid:
        raise ValueError("Native memory measurement requires the original bound root Popen")
    tree.refresh()
    entries = _rss_entries()
    total = 0
    if process.returncode is None:
        original = " ".join((tree.root_identity or "").split())
        current = entries.get(process.pid)
        if not original or current is None or not start_identity_matches(current[0], original):
            raise ValueError("Native memory measurement cannot identify the unreaped root")
        total += current[1]
    return total + _observed_total(
        entries, {pid: start for pid, start in tree.descendants.items() if pid != process.pid}
    )


@dataclass
class NativeResourceSampler:
    """Retain the same process ownership tracker while bounding consecutive sampling gaps."""

    process: subprocess.Popen[bytes]
    tree: ProcessTree
    max_owned_rss_bytes: int
    max_failures: int = 5
    peak_owned_rss_bytes: int = field(default=0, init=False)
    failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Reject invalid byte ceilings and retry bounds without admitting a process."""
        if any(
            type(value) is not int or value <= 0
            for value in (self.max_owned_rss_bytes, self.max_failures)
        ):
            raise ValueError("Native resource limits and retry bounds must be positive integers")

    def sample(self) -> bool:
        """Measure aggregate RSS or stop on the fifth consecutive metadata failure."""
        try:
            current = owned_rss_bytes(self.process, self.tree)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            self.failures += 1
            if self.failures >= self.max_failures:
                raise NativeResourceUnavailable(
                    "Native memory measurement or process tracking metadata is unavailable after bounded "
                    "retries; verify owned process cleanup before retrying."
                ) from error
            return False
        self.failures = 0
        self.peak_owned_rss_bytes = max(self.peak_owned_rss_bytes, current)
        if current > self.max_owned_rss_bytes:
            raise NativeMemoryLimit("Native aggregate owned-process memory limit exceeded.")
        return True
