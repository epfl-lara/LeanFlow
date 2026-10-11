"""Supervise native process groups and drain independent output channels."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass, field

from .models import BackendError


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
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            entries[int(parts[0])] = (int(parts[1]), parts[2])
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


def signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    """Signal the invocation group, including children after its leader exits."""
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


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
        except (OSError, subprocess.SubprocessError) as error:
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
    process.wait()
    if metadata_errors:
        raise BackendError(
            "Native process group was stopped, but descendant cleanup could not be verified: "
            + str(metadata_errors[0])
        ) from metadata_errors[0]
