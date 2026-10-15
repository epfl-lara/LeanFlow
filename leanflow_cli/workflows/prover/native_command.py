"""Run long agent commands inside a private snapshot under the campaign deadline."""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from agent.runtime.harness.isolation import IsolationPolicy, sandbox_command
from agent.runtime.harness.models import NativeCleanupUnverified
from agent.runtime.harness.processes import (
    ProcessTree,
    cleanup_group,
    process_exited,
    require_output_eof,
)
from core.deadlines import operation_cancelled
from leanflow_cli.workflows.prover.check_command_cleanup import CommandOwnership

_OUTPUT_LIMIT = 64000


def run_command(
    argv: Sequence[str],
    *,
    snapshot: Path,
    directory: Path,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    network_allowed: bool = True,
    owner_workspace: Path | None = None,
) -> dict[str, Any]:
    """Supervise a native tool under its campaign limits and require physical closure on return."""
    if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
        raise ValueError("argv must contain nonempty executable arguments without NUL bytes")
    temporary = directory / "tmp" / uuid.uuid4().hex
    temporary.mkdir(parents=True, exist_ok=True)
    elan_home = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))).expanduser().resolve()
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
            elan_home,
        ]
        if path.exists()
    )
    policy = IsolationPolicy(
        snapshot.resolve(),
        writable_roots=(temporary,),
        readable_roots=reads,
        temp_root=temporary,
        network_allowed=network_allowed,
    )
    command = sandbox_command(argv, policy)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "ELAN_TOOLCHAIN", "LEAN_PATH"}
    }
    environment.update(
        HOME=str(directory),
        # Redirecting HOME must not make Elan search an empty scratch installation.
        ELAN_HOME=str(elan_home),
        TMPDIR=str(temporary),
        TMP=str(temporary),
        TEMP=str(temporary),
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
    tree = ProcessTree(process.pid, process=process)
    ownership = CommandOwnership(
        (owner_workspace or directory).resolve(),
        temporary,
        process,
        tree,
        cleanup_group,
        partial(require_output_eof, description="Native command"),
        input_roots=(snapshot.resolve(),),
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    status = "completed"
    cleaned = False
    drain_deadline: float | None = None
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or not process_exited(process):
                if operation_cancelled() or (cancelled is not None and cancelled()):
                    status = "cancelled"
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    status = "timeout"
                    break
                tree.refresh()
                if process_exited(process) and not cleaned:
                    cleanup_group(process, tree)
                    cleaned = True
                    ownership.processes_verified = True
                    drain_deadline = time.monotonic() + 2.0
                if drain_deadline is not None and time.monotonic() >= drain_deadline:
                    raise NativeCleanupUnverified(
                        "Native command output pipes remain open after leader exit; retain its inputs"
                    )
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(chunk)
                        if len(output[key.data]) > _OUTPUT_LIMIT:
                            del output[key.data][:-_OUTPUT_LIMIT]
    except NativeCleanupUnverified:
        # Final close below owns the retention decision; a verified retry cannot
        # turn interrupted capture into success or claim already-deleted inputs.
        status = "capture_failed"
    finally:
        ownership.close()
    return {
        "success": status == "completed" and process.returncode == 0,
        "status": status,
        "returncode": process.returncode,
        "error": (
            "Native command output capture was interrupted; retry the command."
            if status == "capture_failed"
            else ""
        ),
        "elapsed_s": round(time.monotonic() - started, 3),
        **{key: bytes(value).decode("utf-8", errors="replace") for key, value in output.items()},
    }
