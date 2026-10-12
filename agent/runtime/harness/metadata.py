"""Exchange bounded native metadata messages without starting a model turn."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping
from typing import Any

from .processes import cleanup_group

_MAX_LINE = 4 * 1024 * 1024


class MetadataUnavailable(Exception):
    """Report a credential-free reason why native metadata could not be read."""


class MetadataSession:
    """Own a short-lived stdio metadata connection and always reap its process."""

    def __init__(
        self,
        command: list[str],
        *,
        timeout: float = 20,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.command = command
        self.env = env
        self.cwd = cwd
        self.deadline = time.monotonic() + timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=128)
        self.reader: threading.Thread | None = None

    def __enter__(self) -> MetadataSession:
        """Start the native command with discarded diagnostics and bounded output."""
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
                env=self.env,
                cwd=self.cwd,
            )
        except OSError:
            raise MetadataUnavailable("The native runtime could not be started.") from None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        return self

    def _read(self) -> None:
        """Drain native JSON lines without allowing unbounded buffers or queues."""
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := self.process.stdout.readline(_MAX_LINE + 1):
                if len(line) > _MAX_LINE:
                    break
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict):
                    self.messages.put_nowait(value)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            try:
                self.messages.put_nowait(None)
            except queue.Full:
                pass

    def send(self, value: dict[str, Any]) -> None:
        """Write one metadata request to the running native process."""
        assert self.process is not None and self.process.stdin is not None
        try:
            self.process.stdin.write((json.dumps(value) + "\n").encode())
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise MetadataUnavailable("The native metadata connection closed.") from None

    def receive(self) -> dict[str, Any]:
        """Wait within the metadata deadline, which never applies to agent runs."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MetadataUnavailable("Native model discovery timed out. Refresh to retry.")
        try:
            value = self.messages.get(timeout=max(0, remaining))
        except queue.Empty:
            raise MetadataUnavailable(
                "Native model discovery timed out. Refresh to retry."
            ) from None
        if value is None:
            raise MetadataUnavailable("The native runtime closed model discovery.")
        return value

    def __exit__(self, *_: Any) -> None:
        """Stop the metadata process, including its background descendants."""
        assert self.process is not None
        try:
            if os.name == "posix":
                # macOS returns EPERM when signalling a group containing only
                # an unreaped zombie. Reap the leader before group cleanup.
                self.process.poll()
                cleanup_group(self.process)
            else:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        finally:
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        # A child that exits before consuming stdin can leave
                        # buffered writes; closing must preserve the real error.
                        pass
            if self.reader is not None:
                self.reader.join(timeout=1)
