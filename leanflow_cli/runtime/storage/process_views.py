"""Detect orphaned native processes before reclaiming their private dependencies."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessViews:
    """Describe one current-user process inventory, failing closed if incomplete."""

    working_directories: tuple[str, ...]
    commands: tuple[str, ...]
    complete: bool

    def uses(self, workspace: Path) -> bool:
        """Protect an uncertain inventory or any process using this private view."""
        if not self.complete:
            return True
        roots = {workspace.absolute(), workspace.resolve()}
        for directory in self.working_directories:
            if any(Path(directory).is_relative_to(root) for root in roots):
                return True
        return any(
            re.search(re.escape(str(root)) + r"(?=$|[/\\\s\"'`<>()\[\]{},;:!?])", command)
            for root in roots
            for command in self.commands
        )


def _system_session_helper(process: Path) -> bool:
    """Recognize privileged login helpers that cannot own native workspaces.

    SSH and PAM deliberately hide cwd even from their login user. Require their
    real service parent, rather than trusting a process name alone, so they do
    not make every remote-host cleanup permanently unavailable.
    """
    try:
        status = dict(
            line.split(":", 1)
            for line in (process / "status").read_text().splitlines()
            if ":" in line
        )
        parent = process.parent / status["PPid"].strip()
        name = (process / "comm").read_text().strip()
        if name == "sshd" and (parent / "comm").read_text().strip() == "sshd":
            parent_status = (parent / "status").read_text()
            uid = next(
                line.split()[1:3] for line in parent_status.splitlines() if line.startswith("Uid:")
            )
            return uid == ["0", "0"]
        if name == "(sd-pam)":
            executable = (parent / "exe").resolve(strict=True)
            return (
                str(executable) in {"/usr/lib/systemd/systemd", "/lib/systemd/systemd"}
                and executable.stat().st_uid == 0
            )
    except (OSError, KeyError, ValueError, StopIteration):
        pass
    return False


def _linux_views() -> ProcessViews:
    """Inspect current-user procfs cwd and command lines without reading environments."""
    directories: list[str] = []
    commands: list[str] = []
    complete = True
    uid = os.getuid()
    try:
        processes = list(Path("/proc").iterdir())
    except OSError:
        return ProcessViews((), (), False)
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != uid:
                continue
            # Zombies have no open cwd and cannot read dependency files.
            state = (process / "stat").read_text(encoding="utf-8")
            if state[state.rfind(")") + 2 :].split()[0] == "Z":
                continue
            directories.append(os.readlink(process / "cwd"))
            commands.append(
                (process / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            )
        except PermissionError:
            if process.exists() and not _system_session_helper(process):
                complete = False
        except (OSError, IndexError):
            if process.exists():
                complete = False
    return ProcessViews(tuple(directories), tuple(commands), complete)


def _posix_views() -> ProcessViews:
    """Collect macOS process commands and current-user cwd once per cleanup pass."""
    uid = os.getuid()
    try:
        listing = subprocess.run(
            ["ps", "-ww", "-axo", "pid=,uid=,stat=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        cwd = subprocess.run(
            ["lsof", "-nP", "-u", str(uid), "-a", "-d", "cwd", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ProcessViews((), (), False)
    if listing.returncode != 0 or cwd.returncode != 0 or cwd.stderr.strip():
        return ProcessViews((), (), False)
    commands: list[str] = []
    listed_pids: set[int] = set()
    for line in listing.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 3 or not fields[0].isdigit() or not fields[1].isdigit():
            return ProcessViews((), (), False)
        if int(fields[1]) == uid and not fields[2].startswith("Z"):
            listed_pids.add(int(fields[0]))
            if len(fields) == 4:
                commands.append(fields[3])
    directories: list[str] = []
    seen_pids: set[int] = set()
    current_pid = 0
    for line in cwd.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current_pid = int(line[1:])
        elif line == "fcwd" and current_pid:
            continue
        elif line.startswith("n/") and current_pid:
            directories.append(line[1:])
            seen_pids.add(current_pid)
        else:
            return ProcessViews((), (), False)
    complete = bool(directories) and bool(listed_pids)
    for pid in listed_pids - seen_pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # ps itself commonly exits between the two inventory commands.
            continue
        except OSError:
            complete = False
        else:
            complete = False
    return ProcessViews(tuple(directories), tuple(commands), complete)


def process_views() -> ProcessViews:
    """Inspect live users of private views while avoiding credentials in process environments."""
    return _linux_views() if Path("/proc").is_dir() else _posix_views()


def cached_process_views() -> Callable[[], ProcessViews]:
    """Build a lazy inventory supplier shared by all stages of one maintenance pass."""
    cached: ProcessViews | None = None

    def inspect() -> ProcessViews:
        nonlocal cached
        if cached is None:
            cached = process_views()
        return cached

    return inspect
