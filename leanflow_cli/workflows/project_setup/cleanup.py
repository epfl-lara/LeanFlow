"""Verify owned start identities and process groups are closed before private storage disposal."""

from __future__ import annotations

import subprocess
import time

from agent.runtime.harness.processes import ProcessTree


def process_states() -> dict[int, tuple[int, str, str]]:
    """Read start identity, process group and zombie state without arguments or credentials."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,stat=,lstart="],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    table = {}
    for line in result.stdout.splitlines():
        row = line.split(maxsplit=3)
        if len(row) != 4 or not row[0].isdigit() or not row[1].isdigit():
            raise ValueError("Owned cleanup cannot validate the process table")
        table[int(row[0])] = (int(row[1]), row[2], " ".join(row[3].split()))
    return table


def verify_owned_stopped(
    tree: ProcessTree, *, group: int | None = None, timeout_s: float = 2
) -> bool:
    """Require every tracked start identity and invocation group to have no live members.

    A zombie has exited and the kernel has closed its descriptors; it is harmless
    pending its actual parent's reap. Failed inspection never establishes cleanup.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        table = process_states()
        live = any(
            pid in table
            and table[pid][2] == " ".join(start.split())
            and not table[pid][1].startswith("Z")
            for pid, start in tree.descendants.items()
        ) or any(
            owned_group == group and not state.startswith("Z")
            for owned_group, state, _ in table.values()
            if group is not None
        )
        if not live:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
