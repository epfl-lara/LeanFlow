"""Serialize immutable MCP installations and publish configuration atomically."""

from __future__ import annotations

import contextlib
import errno
import importlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from core.error_redaction import sanitize_auxiliary_error
from leanflow_cli.runtime.mcp_protocol import mcp_protocol_ready

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}
_HELD = threading.local()


class MCPValidationCleanupUncertain(RuntimeError):
    """Keep candidate paths when protocol validation cannot prove server shutdown."""


_MCP_INITIALIZATION_CHECK = """\
import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def check():
    command = os.environ.pop('LEANFLOW_MCP_VALIDATE_COMMAND')
    args = json.loads(os.environ.pop('LEANFLOW_MCP_VALIDATE_ARGS'))
    params = StdioServerParameters(command=command, args=args, env=dict(os.environ))
    async with stdio_client(params, errlog=sys.stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.list_tools()
    print('leanflow-mcp-initialized')
asyncio.run(asyncio.wait_for(check(), timeout=30))
"""


def _validation_stderr_tail(stderr: str | bytes | None, env: Mapping[str, str]) -> str:
    """Retain bounded failure context without emitting known credential values."""
    text = (
        stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr or "")
    )
    for name, value in env.items():
        if value and re.search(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", name, re.I):
            text = text.replace(value, "[REDACTED]")
    return sanitize_auxiliary_error(text[-4096:], limit=4096)[-2000:] or "no stderr captured"


def validate_managed_mcp_candidate(
    name: str,
    venv_dir: Path,
    command: Path,
    *,
    args: tuple[str, ...],
    env: Mapping[str, str],
    cwd: Path,
) -> dict[str, Any]:
    """Verify console execution, FastMCP and a completed initialize/list-tools cycle."""
    if not command.is_file() or (os.name != "nt" and not os.access(command, os.X_OK)):
        raise RuntimeError(f"Managed MCP console command is unavailable: {name}")
    if not mcp_protocol_ready(venv_dir):
        raise RuntimeError(f"Managed MCP FastMCP import failed: {name}")
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        result = subprocess.run(
            [str(python), "-c", _MCP_INITIALIZATION_CHECK],
            cwd=str(cwd),
            env={
                **env,
                "LEANFLOW_MCP_VALIDATE_COMMAND": str(command),
                "LEANFLOW_MCP_VALIDATE_ARGS": json.dumps(list(args)),
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
            start_new_session=os.name != "nt",
        )
    except subprocess.TimeoutExpired as exc:
        raise MCPValidationCleanupUncertain(
            f"Managed MCP initialization timed out; candidate retained for safe cleanup: {name}. "
            f"Stderr tail: {_validation_stderr_tail(exc.stderr, env)}"
        ) from exc
    if result.returncode != 0 or "leanflow-mcp-initialized" not in result.stdout:
        raise MCPValidationCleanupUncertain(
            f"Managed MCP initialization failed; candidate retained for safe cleanup: {name}. "
            f"Stderr tail: {_validation_stderr_tail(result.stderr, env)}"
        )
    return {"console_script": True, "fastmcp": True, "initialized": True}


@contextlib.contextmanager
def managed_mcp_install_lock(home: Path) -> Iterator[None]:
    """Hold a reentrant thread and process lock through installation and selection."""
    root = home.resolve() / "mcp"
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "install.lock"
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(lock_path, threading.RLock())
    with lock:
        held: set[Path] = getattr(_HELD, "paths", set())
        if lock_path in held:
            yield
            return
        with lock_path.open("a+b") as handle:
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")

                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"\0")
                    handle.flush()
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                            raise
                        time.sleep(0.1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _HELD.paths = held | {lock_path}
            try:
                yield
            finally:
                _HELD.paths = held
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def managed_config_write_target(path: Path) -> Path:
    """Preserve a configured dotfile link or reject a missing/non-file target."""
    if path.is_symlink():
        # Dotfile-managed configuration keeps its link. Resolve only an
        # existing regular target; never silently replace a broken link.
        target = path.resolve(strict=True)
        if not target.is_file():
            raise ValueError(f"Managed configuration link does not target a regular file: {path}")
        return target
    return path


def atomic_managed_config_write(path: Path, content: str) -> None:
    """Publish complete configuration bytes on the same filesystem after fsync."""
    path = managed_config_write_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", prefix=f".{path.name}-", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            # Directory syncing strengthens crash durability. A failure after
            # replacement cannot undo the already-published complete document.
            with contextlib.suppress(OSError):
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
