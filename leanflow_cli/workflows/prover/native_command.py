"""Run long agent commands inside a private snapshot under the campaign deadline."""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from agent.runtime.harness.isolation import IsolationPolicy, sandbox_command
from agent.runtime.harness.processes import ProcessTree, cleanup_group
from core.deadlines import operation_cancelled

_OUTPUT_LIMIT = 64000


def run_command(
    argv: Sequence[str],
    *,
    snapshot: Path,
    directory: Path,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Start a process group and stop its descendants only on explicit cancellation/deadline."""
    if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
        raise ValueError("argv must contain nonempty executable arguments without NUL bytes")
    temporary = directory / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    reads = tuple(
        path.resolve()
        for path in [
            Path(p)
            for p in (
                "/System",
                "/Library/Frameworks",
                "/Library/Developer",
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
                "/etc",
                "/opt/homebrew",
            )
        ]
        + [
            Path(sys.prefix),
            Path(sys.base_prefix),
            Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))),
        ]
        if path.exists()
    )
    policy = IsolationPolicy(
        snapshot.resolve(), writable_roots=(temporary,), readable_roots=reads, temp_root=temporary
    )
    command = sandbox_command(argv, policy)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "ELAN_HOME", "LEAN_PATH"}
    }
    environment.update(
        HOME=str(directory), TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary)
    )
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=snapshot,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    tree = ProcessTree(process.pid)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    status = "completed"
    cleaned = False
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                if operation_cancelled() or (cancelled is not None and cancelled()):
                    status = "cancelled"
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    status = "timeout"
                    break
                tree.refresh()
                if process.poll() is not None and not cleaned:
                    cleanup_group(process, tree)
                    cleaned = True
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(chunk)
                        if len(output[key.data]) > _OUTPUT_LIMIT:
                            del output[key.data][:-_OUTPUT_LIMIT]
    finally:
        cleanup_group(process, tree)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
    return {
        "success": status == "completed" and process.returncode == 0,
        "status": status,
        "returncode": process.returncode,
        "elapsed_s": round(time.monotonic() - started, 3),
        **{key: bytes(value).decode("utf-8", errors="replace") for key, value in output.items()},
    }
