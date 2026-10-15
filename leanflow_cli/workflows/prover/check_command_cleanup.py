"""Retain failed one-shot checker ownership until process and physical output closure succeed."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from agent.runtime.harness.models import NativeCleanupUnverified
from agent.runtime.harness.processes import ProcessTree

_RETAINED: dict[Path, CommandOwnership] = {}
_LOCK = threading.Lock()
_RETAINED_DIRECTORIES: set[Path] = set()


@dataclass
class CommandOwnership:
    """Keep one failed invocation retryable without revisiting a verified former process group."""

    workspace: Path
    private: Path
    process: subprocess.Popen[bytes]
    tree: ProcessTree
    kill: Callable[[subprocess.Popen[bytes], ProcessTree | None], None]
    require_eof: Callable[[subprocess.Popen[bytes]], None]
    input_roots: tuple[Path, ...] = field(default=(), kw_only=True)
    processes_verified: bool = field(default=False, init=False)
    output_eof_verified: bool = field(default=False, init=False)
    closed: bool = field(default=False, init=False)
    _close_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def close(self) -> None:
        """Retain inputs and handles on uncertainty; retry only the unfinished closure boundary."""
        with self._close_lock:
            if self.closed:
                return
            try:
                if not self.processes_verified:
                    self.kill(self.process, self.tree)
                    self.processes_verified = True
                if not self.output_eof_verified:
                    self.require_eof(self.process)
                    self.output_eof_verified = True
                for stream in (self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()
            except BaseException as error:
                with _LOCK:
                    _RETAINED[self.private] = self
                if not isinstance(error, Exception):
                    raise
                raise NativeCleanupUnverified(
                    f"{error}; retained command inputs: {self.private}; workspace: {self.workspace}"
                ) from error
            shutil.rmtree(self.private, ignore_errors=True)
            self.closed = True
            with _LOCK:
                if _RETAINED.get(self.private) is self:
                    _RETAINED.pop(self.private)
            _release_directories()


def _retains_inputs(owner: CommandOwnership, directory: Path) -> bool:
    """Bind enclosing scratch retention to the owner's exact input roots."""
    return not owner.closed and any(
        path.is_relative_to(directory) for path in (owner.workspace, *owner.input_roots)
    )


def _release_directories() -> None:
    """Remove abandoned scratch only after every related retained invocation closes."""
    with _LOCK:
        released = [
            directory
            for directory in _RETAINED_DIRECTORIES
            if not any(_retains_inputs(owner, directory) for owner in _RETAINED.values())
        ]
        _RETAINED_DIRECTORIES.difference_update(released)
    for directory in released:
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def command_workspace(*, prefix: str, directory: Path | None = None) -> Iterator[Path]:
    """Delete private scratch on verified closure and retain enclosing uncertain inputs."""
    workspace = Path(tempfile.mkdtemp(prefix=prefix, dir=directory)).resolve()
    try:
        yield workspace
    finally:
        with _LOCK:
            retained = any(_retains_inputs(owner, workspace) for owner in _RETAINED.values())
            if retained:
                _RETAINED_DIRECTORIES.add(workspace)
        if not retained:
            shutil.rmtree(workspace, ignore_errors=True)


def close_commands(workspace: Path | None = None) -> None:
    """Retry retained invocation closure outside the registry lock for this exact workspace."""
    work = workspace.resolve() if workspace is not None else None
    with _LOCK:
        commands = tuple(
            owner for owner in _RETAINED.values() if work is None or owner.workspace == work
        )
    failures: list[Exception] = []
    for owner in commands:
        try:
            owner.close()
        except Exception as error:
            failures.append(error)
    _release_directories()
    if failures:
        raise NativeCleanupUnverified(
            f"Command cleanup remains unverified for {len(failures)} invocation(s): {failures[0]}"
        ) from failures[0]
