"""Tests for the bounded-complexity LeanInteract response reader."""

from __future__ import annotations

import io
import threading

from leanflow_cli.lean import lean_interact_compat


class _Input:
    """Record one query written to a fake REPL."""

    def __init__(self) -> None:
        self.value = ""
        self.flushed = False

    def write(self, value: str) -> None:
        self.value += value

    def flush(self) -> None:
        self.flushed = True


class _Process:
    """Expose the stream surface used by LeanInteract."""

    def __init__(self, output: str) -> None:
        self.stdin = _Input()
        self.stdout = io.StringIO(output)


class _Server:
    """Provide the minimal server state used by the patched method."""

    def __init__(self, output: str) -> None:
        self._proc = _Process(output)
        self._lock = threading.Lock()
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def test_read_repl_response_retains_large_payload_and_stops_at_delimiter() -> None:
    """Build a large response once and leave following protocol data unread."""
    lines = [f'{{"entry": {index}}}\n' for index in range(100_000)]
    payload = "".join(lines) + "\nnext-response\n\n"
    stream = io.StringIO(payload)

    result = lean_interact_compat._read_repl_response(stream)

    assert result == "".join(lines) + "\n"
    assert stream.read() == "next-response\n\n"


def test_linear_reader_preserves_query_and_response_protocol() -> None:
    """Preserve LeanInteract's wire behavior while using chunk accumulation."""
    server = _Server('{"env": 17}\n\n')

    result = lean_interact_compat._linear_execute_cmd_in_repl(
        server,
        '{"cmd": "example : True := by trivial"}',
        False,
        1.0,
    )

    assert result == '{"env": 17}\n\n'
    assert server._proc.stdin.value.endswith("\n\n")
    assert server._proc.stdin.flushed is True
    assert server.killed is False


def test_installed_lean_interact_uses_linear_reader() -> None:
    """Patch the vulnerable installed dependency exactly once."""
    from lean_interact import LeanServer

    assert lean_interact_compat.install_linear_repl_reader() is True
    installed = LeanServer._execute_cmd_in_repl
    assert bool(getattr(installed, lean_interact_compat._PATCH_MARKER, False))
    assert lean_interact_compat.install_linear_repl_reader() is True
