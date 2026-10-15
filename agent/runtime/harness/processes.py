"""Supervise native process groups and drain independent output channels."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field

from .models import BackendError, NativeCleanupUnverified


class ProcessTrackingUnavailable(BackendError):
    """Stop an invocation after bounded consecutive process-metadata sampling failures."""


def _processes() -> dict[int, tuple[int, str]]:
    """Read parentage and start identity without collecting arguments or environment."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,lstart="],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    entries: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError("Native tracking cannot validate the process table")
        entries[int(parts[0])] = (int(parts[1]), parts[2])
    if not entries:
        raise ValueError("Native tracking received an empty process table")
    return entries


@dataclass
class ProcessTree:
    """Track native descendants that create their own sessions or process groups."""

    root: int
    descendants: dict[int, str] = field(default_factory=dict)

    def refresh(self) -> None:
        """Remember child identities before the native leader can exit."""
        entries = _processes()
        parents = {
            self.root,
            *(
                pid
                for pid, identity in self.descendants.items()
                if entries.get(pid, (0, ""))[1] == identity
            ),
        }
        while True:
            added = {
                pid
                for pid, (parent, _) in entries.items()
                if parent in parents and pid not in parents
            }
            if not added:
                return
            for pid in added:
                self.descendants[pid] = entries[pid][1]
            parents.update(added)

    def signal(self, sig: signal.Signals) -> None:
        """Signal tracked processes only when their start identity still matches."""
        entries = _processes()
        for pid, identity in self.descendants.items():
            if entries.get(pid, (0, ""))[1] != identity:
                continue
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass


@dataclass
class ProcessSampler:
    """Tolerate bounded sampling gaps without weakening strict cleanup evidence."""

    tree: ProcessTree
    max_failures: int = 5
    _consecutive_failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Require a positive integer retry bound rather than an unbounded tracker."""
        if (
            isinstance(self.max_failures, bool)
            or not isinstance(self.max_failures, int)
            or self.max_failures <= 0
        ):
            raise ValueError("Process tracking needs a positive integer failure bound")

    def sample(self) -> bool:
        """Refresh tracked identities or stop after consecutive metadata failures."""
        try:
            self.tree.refresh()
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.max_failures:
                raise ProcessTrackingUnavailable(
                    "Native process tracking metadata is unavailable after repeated samples; "
                    "stop the invocation, verify owned process cleanup, and retry when process "
                    "inspection is available"
                ) from error
            return False
        self._consecutive_failures = 0
        return True


def _group_has_live_processes(group: int) -> bool:
    """Check whether a group still has signalable members after a macOS EPERM race."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pgid=,stat="],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            return True  # An unverified process table cannot establish completed cleanup.
        if int(fields[0]) == group and not fields[1].startswith("Z"):
            return True
    return False


def signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    """Signal the invocation group, including children after its leader exits."""
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin can return EPERM rather than ESRCH once only reaping remains.
        # Never hide an actual permission failure against a live process group.
        if process.poll() is None or _group_has_live_processes(process.pid):
            raise


def _cleanup_states(timeout_s: float) -> dict[int, tuple[int, str, str]]:
    """Read bounded group, zombie and start identity evidence without process arguments."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,stat=,lstart="],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout_s,
    )
    table: dict[int, tuple[int, str, str]] = {}
    for line in result.stdout.splitlines():
        row = line.split(maxsplit=3)
        if len(row) != 4 or not row[0].isdigit() or not row[1].isdigit():
            raise ValueError("Native cleanup cannot validate the process table")
        table[int(row[0])] = (int(row[1]), row[2], " ".join(row[3].split()))
    if not table:
        raise ValueError("Native cleanup received an empty process table")
    return table


def verify_owned_stopped(tree: ProcessTree | None, *, group: int, timeout_s: float = 2.0) -> bool:
    """Require bounded independent absence of live group members and tracked start identities.

    Zombies have exited with descriptors closed. Unobserved descendants that escaped
    before tracking are outside this evidence; metadata uncertainty never proves closure.
    """
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("Native cleanup verification needs a positive finite bound")
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        table = _cleanup_states(min(2.0, remaining))
        identities = tree.descendants if tree is not None else {}
        live = any(
            pid in table
            and table[pid][2] == " ".join(start.split())
            and not table[pid][1].startswith("Z")
            for pid, start in identities.items()
        ) or any(pgid == group and not state.startswith("Z") for pgid, state, _ in table.values())
        if not live:
            return True
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def cleanup_group(process: subprocess.Popen[bytes], tree: ProcessTree | None = None) -> None:
    """Reap the invocation and prevent native background tools from surviving it."""
    metadata_errors: list[Exception] = []

    def inspect_and_signal(sig: signal.Signals, *, refresh: bool = False) -> None:
        """Keep group cleanup unconditional if process-identity inspection fails."""
        if tree is None:
            return
        try:
            if refresh:
                tree.refresh()
            tree.signal(sig)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            metadata_errors.append(error)

    inspect_and_signal(signal.SIGTERM, refresh=True)
    signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # Kill remaining descendants even when the native CLI itself already exited.
    signal_group(process, signal.SIGKILL)
    inspect_and_signal(signal.SIGKILL)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise NativeCleanupUnverified(
            "Native leader did not exit after termination; retain invocation inputs"
        ) from error
    if metadata_errors:
        raise NativeCleanupUnverified(
            "Native descendant cleanup could not be verified; retain invocation inputs: "
            + str(metadata_errors[0])
        ) from metadata_errors[0]
    try:
        verified = verify_owned_stopped(tree, group=process.pid)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise NativeCleanupUnverified(
            "Native cleanup evidence is unavailable; retain invocation inputs: " + str(error)
        ) from error
    if not verified:
        raise NativeCleanupUnverified(
            "Native process group or tracked descendants remain live; retain invocation inputs"
        )
