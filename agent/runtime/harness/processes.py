"""Supervise native process groups and drain independent output channels."""

from __future__ import annotations

import math
import os
import re
import select
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field

from .models import BackendError, NativeCleanupUnverified


class ProcessTrackingUnavailable(BackendError):
    """Stop an invocation after bounded consecutive process-metadata sampling failures."""


def process_inspection_environment() -> dict[str, str]:
    """Pin process identity dates without changing the controller or native child's locale."""
    return {**os.environ, "LC_ALL": "C"}


_C_START = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?:[1-9]|[12]\d|3[01]) (?:[01]\d|2[0-3]):[0-5]\d:[0-6]\d \d{4}\Z"
)


def start_identity_matches(current: str, expected: str) -> bool:
    """Compare canonical starts, withholding legacy locale dates that cannot prove PID reuse."""
    current, expected = " ".join(current.split()), " ".join(expected.split())
    if current == expected:
        return True
    for value in (current, expected):
        date_like = re.search(r"\d{1,2}:\d{2}:\d{2}", value) and re.search(r"\b\d{4}\b", value)
        if not value.isascii() or (date_like and _C_START.fullmatch(value) is None):
            raise ValueError(
                "Process start identity uses an uncomparable legacy date format; "
                "retain inputs and use observation-bound recovery"
            )
    return False


def _processes() -> dict[int, tuple[int, str]]:
    """Read parentage and start identity without collecting arguments or environment."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,lstart="],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
        env=process_inspection_environment(),
    )
    entries: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError("Native tracking cannot validate the process table")
        entries[int(parts[0])] = (int(parts[1]), " ".join(parts[2].split()))
    if not entries:
        raise ValueError("Native tracking received an empty process table")
    return entries


def process_exited(process: subprocess.Popen[bytes]) -> bool:
    """Observe child exit without reaping the PID that reserves its owned process group."""
    if process.returncode is not None:
        return True
    if os.name != "posix":
        return process.poll() is not None
    try:
        waitid = getattr(os, "waitid", None)
        if waitid is not None:
            result = waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            return result is not None and result.si_pid == process.pid
        if hasattr(select, "kqueue"):
            queue = select.kqueue()
            try:
                event = select.kevent(
                    process.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                return bool(queue.control([event], 1, 0))
            finally:
                queue.close()
    except InterruptedError:
        return False
    except OSError as error:
        if process.returncode is not None:
            return True
        raise ProcessTrackingUnavailable(
            "Native child exit could not be observed without releasing its PID"
        ) from error
    raise ProcessTrackingUnavailable("Non-reaping native child exit observation is unavailable")


@dataclass
class ProcessTree:
    """Track native descendants that create their own sessions or process groups."""

    root: int
    descendants: dict[int, str] = field(default_factory=dict)
    process: subprocess.Popen[bytes] | None = field(default=None, kw_only=True, repr=False)
    root_identity: str | None = field(default=None, init=False)
    _root_retired: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind child ownership to the exact root PID represented by its original Popen."""
        if self.process is not None and self.process.pid != self.root:
            raise ValueError("Process tracking must bind the original root Popen")

    def refresh(self) -> None:
        """Expand only the original unreaped root or matching cached descendants."""
        entries = _processes()
        # Popen's cached returncode is deliberate: poll() can reap the child and
        # release its PID between ownership inspection and signaling.
        if self.process is not None and self.process.returncode is not None:
            self._root_retired = True
        current_root = entries.get(self.root)
        if not self._root_retired:
            if current_root is None:
                self._root_retired = True
            elif self.root_identity is None:
                self.root_identity = current_root[1]
            elif not start_identity_matches(current_root[1], self.root_identity):
                self._root_retired = True
        parents = {
            *(
                pid
                for pid, identity in self.descendants.items()
                if pid in entries and start_identity_matches(entries[pid][1], identity)
            ),
        }
        if not self._root_retired:
            parents.add(self.root)
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
            if pid not in entries or not start_identity_matches(entries[pid][1], identity):
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
        env=process_inspection_environment(),
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
        if not process_exited(process) or _group_has_live_processes(process.pid):
            raise


def _cleanup_states(timeout_s: float) -> dict[int, tuple[int, str, str]]:
    """Read bounded group, zombie and start identity evidence without process arguments."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,stat=,lstart="],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout_s,
        env=process_inspection_environment(),
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


def group_has_owned_member(process: subprocess.Popen[bytes], tree: ProcessTree | None) -> bool:
    """Require cached live membership before signaling an already reaped root's group."""
    if tree is None or not tree.descendants:
        return False
    table = _cleanup_states(2.0)
    return any(
        pid in table
        and table[pid][0] == process.pid
        and not table[pid][1].startswith("Z")
        and start_identity_matches(table[pid][2], identity)
        for pid, identity in tree.descendants.items()
    )


def require_output_eof(
    process: subprocess.Popen[bytes], *, timeout_s: float = 2.0, description: str = "Native"
) -> None:
    """Drain nonblocking streams within a finite bound and require physical output closure."""
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("Output closure verification needs a positive finite bound")
    deadline = time.monotonic() + timeout_s
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NativeCleanupUnverified(
                        f"{description} output pipes remain open; retain invocation inputs"
                    )
                for key, _ in selector.select(min(0.05, remaining)):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
    except (OSError, ValueError) as error:
        raise NativeCleanupUnverified(
            f"{description} output closure is unverified; retain invocation inputs"
        ) from error


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
            and start_identity_matches(table[pid][2], start)
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
        """Keep cached descendants signalable without rediscovering a reaped root."""
        if tree is None:
            return
        try:
            if refresh and process.returncode is None:
                tree.refresh()
            tree.signal(sig)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            metadata_errors.append(error)

    def signal_owned_group(sig: signal.Signals) -> None:
        """An unreaped child reserves its PID; a former group needs cached ownership."""
        try:
            if process.returncode is None or group_has_owned_member(process, tree):
                signal_group(process, sig)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            metadata_errors.append(error)

    inspect_and_signal(signal.SIGTERM, refresh=True)
    signal_owned_group(signal.SIGTERM)
    # Keep an exited leader unreaped until escalation. Its reserved PID proves
    # ownership of late same-group children even when they escaped the last scan.
    term_deadline = time.monotonic() + 2
    while process.returncode is None:
        try:
            if process_exited(process) and not _group_has_live_processes(process.pid):
                break
        except (
            ProcessTrackingUnavailable,
            OSError,
            subprocess.SubprocessError,
            ValueError,
        ) as error:
            metadata_errors.append(error)
            break
        remaining = term_deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    # Escalate before the single final wait releases the original PID.
    signal_owned_group(signal.SIGKILL)
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
