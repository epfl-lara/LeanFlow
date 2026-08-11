"""Terminate subprocess trees that split into independent process groups."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessRecord:
    """Describe one process relationship from a point-in-time system snapshot."""

    pid: int
    parent_pid: int
    process_group_id: int
    session_id: int


def _process_snapshot() -> dict[int, ProcessRecord] | None:
    """Return the current POSIX process table, or ``None`` when unavailable."""
    if os.name != "posix":
        return None
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    records: dict[int, ProcessRecord] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            pid, parent_pid, process_group_id = map(int, fields)
            # macOS ``ps sess`` is not the POSIX session id (and commonly
            # reports zero). getsid() gives the same durable ownership token
            # on every supported POSIX host.
            session_id = os.getsid(pid)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            continue
        records[pid] = ProcessRecord(
            pid=pid,
            parent_pid=parent_pid,
            process_group_id=process_group_id,
            session_id=session_id,
        )
    return records


def _validated_live_records(
    records: dict[int, ProcessRecord],
    *,
    expected_session_id: int | None,
) -> dict[int, ProcessRecord]:
    """Revalidate prior records without trusting PIDs after snapshot failure.

    An isolated expected session is the durable identity when available. For
    callers without one, require both the prior session and process group so a
    recycled PID is not escalated merely because it still exists.
    """
    validated: dict[int, ProcessRecord] = {}
    for pid, record in records.items():
        try:
            current_session_id = os.getsid(pid)
            current_group_id = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        if expected_session_id is not None:
            if current_session_id != expected_session_id:
                continue
        elif current_session_id != record.session_id or current_group_id != record.process_group_id:
            continue
        validated[pid] = ProcessRecord(
            pid=pid,
            parent_pid=record.parent_pid,
            process_group_id=current_group_id,
            session_id=current_session_id,
        )
    return validated


def _refresh_owned_processes(
    root_pid: int,
    *,
    expected_session_id: int | None,
    prior_records: dict[int, ProcessRecord],
) -> dict[int, ProcessRecord]:
    """Refresh owned identities, degrading to syscall validation if ps fails."""
    snapshot = _process_snapshot()
    if snapshot is None:
        return _validated_live_records(
            prior_records,
            expected_session_id=expected_session_id,
        )
    return _owned_processes(
        root_pid,
        expected_session_id=expected_session_id,
        snapshot=snapshot,
    )


def _owned_processes(
    root_pid: int,
    *,
    expected_session_id: int | None,
    snapshot: dict[int, ProcessRecord],
) -> dict[int, ProcessRecord]:
    """Return descendants plus members of the isolated session owned by a root."""
    children: dict[int, list[int]] = {}
    for record in snapshot.values():
        children.setdefault(record.parent_pid, []).append(record.pid)

    root = snapshot.get(root_pid)
    root_identity_valid = root is not None and (
        expected_session_id is None or root.session_id == expected_session_id
    )
    owned_ids: set[int] = {root_pid} if root_identity_valid else set()
    stack = [root_pid] if root_identity_valid else []
    while stack:
        parent_pid = stack.pop()
        for child_pid in children.get(parent_pid, []):
            if child_pid in owned_ids:
                continue
            owned_ids.add(child_pid)
            stack.append(child_pid)

    # Local terminal commands start with setsid(), making root_pid the session
    # id. Session membership remains stable after the shell is reparented, so
    # it closes the exact hole where bash dies before its Python child does.
    if expected_session_id and expected_session_id > 1:
        owned_ids.update(
            record.pid for record in snapshot.values() if record.session_id == expected_session_id
        )

    current_pid = os.getpid()
    return {
        pid: snapshot[pid]
        for pid in owned_ids
        if pid in snapshot and pid not in {0, 1, current_pid}
    }


def _signal_owned_processes(
    records: dict[int, ProcessRecord],
    signum: int,
    *,
    root_pid: int,
    include_root: bool,
) -> None:
    """Signal owned groups and individual members without touching the caller."""
    if not records:
        return
    current_group = os.getpgrp()
    root_group = records.get(root_pid, ProcessRecord(0, 0, 0, 0)).process_group_id
    group_ids = {
        record.process_group_id
        for pid, record in records.items()
        if record.process_group_id > 1
        and record.process_group_id != current_group
        and (include_root or record.process_group_id != root_group)
        and (include_root or pid != root_pid)
    }
    for process_group_id in sorted(group_ids):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(process_group_id, signum)
    for pid in sorted(records, reverse=True):
        if not include_root and pid == root_pid:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signum)


def _any_process_alive(
    records: dict[int, ProcessRecord], *, include_root: bool, root_pid: int
) -> bool:
    """Return whether any selected process still accepts a signal-zero probe."""
    for pid in records:
        if not include_root and pid == root_pid:
            continue
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        return True
    return False


def terminate_process_tree(
    root_pid: int,
    *,
    expected_session_id: int | None = None,
    include_root: bool = True,
    term_grace_s: float = 1.0,
    kill_grace_s: float = 1.0,
) -> tuple[int, ...]:
    """Terminate one owned POSIX process tree and escalate surviving groups.

    Descendant discovery alone is insufficient because killing a wrapper shell
    reparents its children to PID 1. Local terminal roots therefore pass their
    isolated session id as an additional durable ownership boundary. Every
    process group selected here belongs to that spawned session or was observed
    below its root, so unrelated processes are never selected by cwd or command
    text.
    """
    if os.name != "posix" or root_pid <= 1 or root_pid == os.getpid():
        return ()
    first_snapshot = _process_snapshot()
    if first_snapshot is None:
        # Preserve a narrow fallback when ps is unavailable. The root was
        # created with setsid(), so this group is still owned by the caller.
        fallback = ProcessRecord(root_pid, 0, root_pid, expected_session_id or root_pid)
        owned = {root_pid: fallback}
    else:
        owned = _owned_processes(
            root_pid,
            expected_session_id=expected_session_id,
            snapshot=first_snapshot,
        )
        if not owned:
            return ()
    _signal_owned_processes(owned, signal.SIGTERM, root_pid=root_pid, include_root=include_root)

    deadline = time.monotonic() + max(0.0, term_grace_s)
    while time.monotonic() < deadline:
        if not _any_process_alive(owned, include_root=include_root, root_pid=root_pid):
            return tuple(sorted(owned))
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    refreshed = _refresh_owned_processes(
        root_pid,
        expected_session_id=expected_session_id,
        prior_records=owned,
    )
    if not refreshed:
        return tuple(sorted(owned))
    _signal_owned_processes(
        refreshed,
        signal.SIGKILL,
        root_pid=root_pid,
        include_root=include_root,
    )

    # The root is our direct Popen child and its caller reaps it with wait().
    # Detached descendants are different: killing the root can reparent them,
    # and signal-zero continues to report a just-killed zombie until the system
    # reaper catches up. Keep the execution boundary closed until those exact
    # session members disappear, while re-snapshotting so a reused PID is never
    # signaled or mistaken for an owned survivor.
    kill_deadline = time.monotonic() + max(0.0, kill_grace_s)
    while time.monotonic() < kill_deadline:
        remaining = _refresh_owned_processes(
            root_pid,
            expected_session_id=expected_session_id,
            prior_records=refreshed,
        )
        remaining_descendants = {
            pid: record for pid, record in remaining.items() if pid != root_pid
        }
        if not remaining_descendants:
            break
        time.sleep(min(0.05, max(0.0, kill_deadline - time.monotonic())))
    return tuple(sorted(set(owned) | set(refreshed)))
