"""Exchange bounded native metadata messages without starting a model turn."""

from __future__ import annotations

import atexit
import json
import math
import os
import queue
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from typing import Any

from .models import NativeCleanupUnverified
from .processes import (
    ProcessSampler,
    ProcessTrackingUnavailable,
    ProcessTree,
    cleanup_group,
    require_output_eof,
    signal_group,
)

_MAX_LINE = 4 * 1024 * 1024
_RETAINED: set[MetadataSession] = set()
_RETAINED_LOCK = threading.Lock()


class MetadataUnavailable(Exception):
    """Report a credential-free reason why native metadata could not be read."""


class MetadataSession:
    """Own a bounded stdio metadata connection and retain uncertain cleanup for retry."""

    def __init__(
        self,
        command: list[str],
        *,
        timeout: float = 20,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        """Initialize one deadline and retryable ownership before launching metadata helpers."""
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Native metadata needs a positive finite deadline")
        self.command = command
        self.env = env
        self.cwd = cwd
        self.deadline = time.monotonic() + timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.tree: ProcessTree | None = None
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=128)
        self.reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._reader_eof = threading.Event()
        self._close_lock = threading.Lock()
        self._processes_verified = False
        self._output_eof_verified = False
        self.closed = False

    def __enter__(self) -> MetadataSession:
        """Start the native command with discarded diagnostics and bounded output."""
        if self.process is not None:
            raise MetadataUnavailable("The native metadata session has already started.")
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
        try:
            self.tree = ProcessTree(self.process.pid, process=self.process)
            assert self.process.stdout is not None and self.process.stdin is not None
            os.set_blocking(self.process.stdout.fileno(), False)
            os.set_blocking(self.process.stdin.fileno(), False)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
        except BaseException:
            self.close()
            raise
        return self

    def _read(self) -> None:
        """Poll nonblocking output so shutdown never closes a stream behind a blocked reader."""
        assert self.process is not None and self.process.stdout is not None
        assert self.tree is not None
        sampler = ProcessSampler(self.tree)
        buffer = bytearray()
        sample_at = 0.0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                while not self._reader_stop.is_set():
                    now = time.monotonic()
                    if now >= sample_at:
                        sampler.sample()
                        sample_at = time.monotonic() + 0.25
                    for key, _ in selector.select(0.1):
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            if buffer:
                                try:
                                    value = json.loads(buffer)
                                except (ValueError, UnicodeError):
                                    pass
                                else:
                                    if isinstance(value, dict):
                                        self.messages.put_nowait(value)
                            self._reader_eof.set()
                            return
                        buffer.extend(chunk)
                        while b"\n" in buffer:
                            line, _, remaining = buffer.partition(b"\n")
                            if len(line) > _MAX_LINE:
                                return
                            buffer[:] = remaining
                            try:
                                value = json.loads(line)
                            except (ValueError, UnicodeError):
                                continue
                            if isinstance(value, dict):
                                self.messages.put_nowait(value)
                        if len(buffer) > _MAX_LINE:
                            return
        except (OSError, ValueError, queue.Full, ProcessTrackingUnavailable):
            pass
        finally:
            try:
                self.messages.put_nowait(None)
            except queue.Full:
                pass

    def send(self, value: dict[str, Any]) -> None:
        """Write one bounded metadata request without an unbounded buffered pipe flush."""
        assert self.process is not None and self.process.stdin is not None
        payload = (json.dumps(value) + "\n").encode()
        if len(payload) > _MAX_LINE:
            raise MetadataUnavailable("The native metadata request is too large.")
        offset = 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdin, selectors.EVENT_WRITE)
                while offset < len(payload):
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise MetadataUnavailable(
                            "Native model discovery timed out. Refresh to retry."
                        )
                    for key, _ in selector.select(min(0.1, remaining)):
                        try:
                            offset += os.write(key.fd, payload[offset : offset + 65536])
                        except BlockingIOError:
                            continue
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

    def close(self) -> None:
        """Stop the reader, verify owned processes and EOF, then close local handles."""
        assert self.process is not None
        with self._close_lock:
            if self.closed:
                return
            try:
                self._reader_stop.set()
                # The original unreaped Popen reserves this group even if a slow
                # reader is still sampling metadata. Do not wait to terminate it.
                if not self._processes_verified and self.process.returncode is None:
                    if os.name == "posix":
                        signal_group(self.process, signal.SIGTERM)
                    else:
                        self.process.terminate()
                if self.reader is not None and self.reader.ident is not None:
                    self.reader.join(timeout=3)
                    if self.reader.is_alive():
                        raise NativeCleanupUnverified(
                            "Native metadata reader did not stop; retain invocation inputs"
                        )
                if not self._processes_verified:
                    if os.name == "posix":
                        cleanup_group(self.process, self.tree)
                    else:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            self.process.wait(timeout=2)
                    self._processes_verified = True
                if not self._output_eof_verified:
                    if not self._reader_eof.is_set():
                        require_output_eof(self.process, description="Native metadata")
                    self._output_eof_verified = True
                for stream in (self.process.stdin, self.process.stdout):
                    if stream is not None:
                        stream.close()
            except (
                NativeCleanupUnverified,
                OSError,
                ValueError,
                subprocess.SubprocessError,
            ) as error:
                with _RETAINED_LOCK:
                    _RETAINED.add(self)
                if isinstance(error, NativeCleanupUnverified):
                    raise
                raise NativeCleanupUnverified(
                    "Native metadata shutdown is unverified; retain invocation inputs"
                ) from error
            self.closed = True
            with _RETAINED_LOCK:
                _RETAINED.discard(self)

    def __exit__(self, *_: Any) -> None:
        """Keep ownership retryable when shutdown cannot establish physical closure."""
        self.close()


def close_metadata_sessions() -> None:
    """Retry retained metadata ownership without holding the registry lock during shutdown."""
    with _RETAINED_LOCK:
        sessions = tuple(_RETAINED)
    failures: list[Exception] = []
    for session in sessions:
        try:
            session.close()
        except Exception as error:
            failures.append(error)
    if failures:
        raise NativeCleanupUnverified(
            f"Native metadata cleanup remains unverified for {len(failures)} session(s): {failures[0]}"
        ) from failures[0]


atexit.register(close_metadata_sessions)
