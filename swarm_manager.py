"""Agent swarm manager for background autoformalization tasks.

Tracks spawned autoformalization agents, manages their lifecycle, and
renders Rich table status views for the ``/swarm`` slash command.

Background agents run Claude Code in ``-p`` (print) mode with
``--output-format stream-json`` so the swarm manager can parse live
progress events and update each task's status without blocking the TUI.

Interactive sessions run behind a PTY so users can attach/detach via
``/swarm attach <id>`` without losing the agent's running state.
"""

from __future__ import annotations

import fcntl
import io
import json
import logging
import os
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from rich.table import Table

from gauss_cli.skin_engine import get_active_skin

logger = logging.getLogger(__name__)

_RECENT_OUTPUT_LIMIT = 256 * 1024


def _is_effective_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid):
        try:
            return geteuid() == 0
        except OSError:
            pass
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        try:
            return getuid() == 0
        except OSError:
            pass
    return False


@dataclass
class SwarmTask:
    """Metadata and runtime state for a single autoformalization agent."""

    task_id: str
    description: str
    theorem: str
    workflow_kind: str = ""
    workflow_command: str = ""
    project_name: str = ""
    project_root: str = ""
    working_dir: str = ""
    backend_name: str = ""
    status: str = "queued"
    session_id: Optional[str] = None
    thread: Optional[threading.Thread] = None
    process: Optional[subprocess.Popen] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    progress: str = "Waiting"
    result: Optional[str] = None
    error: Optional[str] = None
    lean_status: Optional[str] = None
    pty_master_fd: Optional[int] = None
    _output_lines: Optional[List[str]] = field(default=None, repr=False)
    _recent_output: Optional[bytearray] = field(default=None, repr=False)
    _attached: bool = field(default=False, repr=False)


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds into a compact human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}h{minutes}m"


def _parse_stream_event(task: SwarmTask, line: str) -> None:
    """Parse a single line of Claude Code ``stream-json`` output and update *task*."""
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return

    etype = event.get("type", "")

    if etype == "assistant" and "message" in event:
        msg = event["message"]
        stop = msg.get("stopReason") or msg.get("stop_reason")
        if stop == "end_turn":
            task.progress = "Agent finished turn"

    elif etype == "content_block_start":
        cb = event.get("content_block", {})
        if cb.get("type") == "tool_use":
            tool_name = cb.get("name", "")
            if "lean" in tool_name.lower() or "lsp" in tool_name.lower():
                task.lean_status = "active"
                task.progress = f"Tool: {tool_name}"
            else:
                task.progress = f"Tool: {tool_name}"

    elif etype == "result":
        task.progress = "Session complete"
        result_text = event.get("result", "")
        if result_text:
            task.result = result_text[:500]
        sub = event.get("subtype", "")
        if sub == "error_max_turns":
            task.progress = "Hit max turns"
        elif sub == "error_api":
            task.error = event.get("error", "API error")

    elif etype == "tool_result":
        content = event.get("content", "")
        if isinstance(content, str) and ("sorry" in content.lower()):
            task.lean_status = "has sorry"
        elif isinstance(content, str) and ("no errors" in content.lower() or "goals accomplished" in content.lower()):
            task.lean_status = "verified"


def _ensure_workspace_trusted(cwd: str) -> None:
    """Pre-trust *cwd* in ``~/.claude.json`` so CC skips the trust dialog."""
    claude_json = os.path.expanduser("~/.claude.json")
    try:
        with open(claude_json, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    projects = data.setdefault("projects", {})
    resolved = os.path.realpath(cwd)

    entry = projects.get(resolved, {})
    if entry.get("hasTrustDialogAccepted") and entry.get("hasTrustDialogHooksAccepted"):
        return

    entry.setdefault("allowedTools", [])
    entry["hasTrustDialogAccepted"] = True
    entry["hasTrustDialogHooksAccepted"] = True
    projects[resolved] = entry
    data["projects"] = projects

    try:
        with open(claude_json, "w") as f:
            json.dump(data, f, indent=2)
        logger.debug("Pre-trusted workspace %s in ~/.claude.json", resolved)
    except OSError as exc:
        logger.warning("Could not write ~/.claude.json: %s", exc)


def _normalize_claude_session_launch(
    argv: Sequence[str],
    env: Dict[str, str],
    cwd: str = "",
) -> tuple[List[str], Dict[str, str]]:
    """Force Claude Code child sessions into bypass/yolo mode."""
    normalized_argv = list(argv)
    normalized_env = dict(env)
    if not normalized_argv:
        return normalized_argv, normalized_env

    executable = os.path.basename(str(normalized_argv[0]))
    if executable != "claude":
        return normalized_argv, normalized_env

    if cwd:
        _ensure_workspace_trusted(cwd)

    stripped_argv: List[str] = [normalized_argv[0]]
    i = 1
    while i < len(normalized_argv):
        arg = str(normalized_argv[i])
        if arg == "--dangerously-skip-permissions":
            i += 1
            continue
        if arg == "--permission-mode":
            i += 2
            continue
        if arg.startswith("--permission-mode="):
            i += 1
            continue
        stripped_argv.append(normalized_argv[i])
        i += 1

    if _is_effective_root():
        stripped_argv[1:1] = ["--permission-mode", "bypassPermissions"]
    else:
        stripped_argv.insert(1, "--dangerously-skip-permissions")
    normalized_env["GAUSS_YOLO_MODE"] = "1"
    return stripped_argv, normalized_env


def _remember_recent_output(task: SwarmTask, chunk: bytes) -> None:
    """Keep a rolling raw-output buffer so attaches can replay the current screen."""
    if not chunk:
        return
    if task._recent_output is None:
        task._recent_output = bytearray()
    task._recent_output.extend(chunk)
    if len(task._recent_output) > _RECENT_OUTPUT_LIMIT:
        del task._recent_output[:-_RECENT_OUTPUT_LIMIT]


def _replay_recent_output(task: SwarmTask, stdout_fd: int) -> None:
    """Replay buffered PTY bytes to restore the child UI immediately on attach."""
    if not task._recent_output:
        return
    os.write(stdout_fd, bytes(task._recent_output))


def _run_claude_code_background(
    task: SwarmTask,
    argv: Sequence[str],
    cwd: str,
    env: Dict[str, str],
) -> None:
    """Run a Claude Code subprocess in print mode and stream events to *task*.

    This is the target function for daemon threads spawned by
    :meth:`SwarmManager.spawn_claude`.
    """
    task.status = "running"
    task.start_time = time.time()
    task.lean_status = "starting"
    task.progress = "Launching Claude Code..."

    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            shell=False,
        )
        task.process = proc
        task.progress = "Claude Code running"

        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            _parse_stream_event(task, line)
            if task.status == "cancelled":
                proc.terminate()
                break

        proc.wait()

        if task.status == "cancelled":
            task.progress = "Cancelled"
        elif proc.returncode == 0:
            if task.status == "running":
                task.status = "complete"
            if not task.result:
                task.progress = "Session ended successfully"
        else:
            stderr_tail = ""
            if proc.stderr:
                stderr_tail = proc.stderr.read()[-300:] if proc.stderr.readable() else ""
            task.status = "failed"
            task.error = f"exit {proc.returncode}" + (f": {stderr_tail}" if stderr_tail else "")
            task.progress = "Claude Code exited with error"

    except FileNotFoundError:
        task.status = "failed"
        task.error = "claude CLI not found"
        task.progress = "Launch failed"
    except Exception as exc:
        task.status = "failed"
        task.error = str(exc)[:300]
        task.progress = "Unexpected error"
    finally:
        task.process = None
        if task.end_time is None:
            task.end_time = time.time()



def _run_claude_code_interactive(
    task: SwarmTask,
    argv: Sequence[str],
    cwd: str,
    env: Dict[str, str],
) -> None:
    """Run Claude Code interactively behind a PTY.

    The background thread reads from the PTY master only while the user
    is **not** attached.  When ``task._attached`` is set, this loop
    yields the master fd entirely so :func:`attach_to_task` is the sole
    reader/writer -- avoiding the data-race that causes garbled I/O.
    """
    task.status = "running"
    task.start_time = time.time()
    task.lean_status = "starting"
    task.progress = "Launching Claude Code..."
    task._output_lines = []
    task._recent_output = bytearray()

    master_fd: Optional[int] = None
    try:
        master_fd, slave_fd = os.openpty()

        try:
            rows, cols = 24, 80
            try:
                sz = os.get_terminal_size()
                rows, cols = sz.lines, sz.columns
            except OSError:
                pass
            win = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, win)
        except Exception:
            pass

        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            shell=False,
        )
        os.close(slave_fd)
        slave_fd = -1

        task.process = proc
        task.pty_master_fd = master_fd
        task.progress = "Claude Code running"

        buf = b""
        while True:
            if task._attached:
                time.sleep(0.25)
                ret = proc.poll()
                if ret is not None:
                    break
                if task.status == "cancelled":
                    proc.terminate()
                    break
                continue

            try:
                ready, _, _ = select.select([master_fd], [], [], 0.5)
            except (ValueError, OSError):
                break

            if master_fd in ready:
                if task._attached:
                    continue
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break

                _remember_recent_output(task, chunk)
                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
                    if task._output_lines is not None:
                        task._output_lines.append(line)
                        if len(task._output_lines) > 2000:
                            task._output_lines = task._output_lines[-1000:]
                    _parse_stream_event(task, line)

            ret = proc.poll()
            if ret is not None:
                break
            if task.status == "cancelled":
                proc.terminate()
                break

        proc.wait()

        if task.status == "cancelled":
            task.progress = "Cancelled"
        elif proc.returncode == 0:
            if task.status == "running":
                task.status = "complete"
            if not task.result:
                task.progress = "Session ended successfully"
        else:
            task.status = "failed"
            task.error = f"exit {proc.returncode}"
            task.progress = "Claude Code exited with error"

    except FileNotFoundError:
        task.status = "failed"
        task.error = "claude CLI not found"
        task.progress = "Launch failed"
    except Exception as exc:
        task.status = "failed"
        task.error = str(exc)[:300]
        task.progress = "Unexpected error"
    finally:
        task.process = None
        task.pty_master_fd = None
        if master_fd is not None and master_fd >= 0:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if task.end_time is None:
            task.end_time = time.time()


def _write_raw(s: bytes | str) -> None:
    """Write bytes directly to stdout fd."""
    if isinstance(s, str):
        s = s.encode()
    os.write(sys.stdout.fileno(), s)


def _set_terminal_title(title: str) -> None:
    _write_raw(f"\x1b]0;{title}\x07")


def _get_terminal_size(fd: int) -> tuple[int, int]:
    """Return (rows, cols) for the terminal on *fd*."""
    buf = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
    rows, cols = struct.unpack("HHHH", buf)[:2]
    return rows, cols


def _set_pty_size(master_fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0))


def _draw_status_bar(rows: int, cols: int, task_id: str) -> None:
    """Draw the status bar on row *rows* using direct cursor addressing.

    No cursor save/restore -- those conflict with the child TUI's own
    cursor state.  Instead we move to the target row, draw, and leave
    the cursor there.  The child will reposition its own cursor on the
    next output chunk via its normal escape sequences.
    """
    label = f" Gauss \u203a {task_id}  \u2502  Ctrl-] detach "
    pad = max(0, cols - len(label))
    _write_raw(f"\x1b[{rows};1H\x1b[7m{label}{' ' * pad}\x1b[0m")


def _setup_status_bar(real_fd: int, master_fd: int, task_id: str) -> tuple[int, int]:
    """Set scroll region to rows 1..(N-1), draw bar on row N, resize PTY."""
    rows, cols = _get_terminal_size(real_fd)
    _write_raw(f"\x1b[1;{rows - 1}r")
    _write_raw("\x1b[1;1H")
    _set_pty_size(master_fd, rows - 1, cols)
    _draw_status_bar(rows, cols, task_id)
    return rows, cols


def _teardown_status_bar(real_fd: int, master_fd: int) -> None:
    """Reset scroll region to full terminal and resize PTY back."""
    rows, cols = _get_terminal_size(real_fd)
    _write_raw(f"\x1b[1;{rows}r")
    _write_raw(f"\x1b[{rows};1H\x1b[2K")
    _set_pty_size(master_fd, rows, cols)


def attach_to_task(task: SwarmTask) -> int:
    """Attach the current terminal to a running task's PTY.

    While attached, **this function is the sole reader/writer** on the
    PTY master fd.  The background thread in
    :func:`_run_claude_code_interactive` yields the fd when
    ``task._attached`` is ``True``.

    Uses the same status-bar technique as tmux: ANSI scroll region
    confines the child to rows 1..(N-1), with a persistent bar on row N.
    The PTY is sized to (N-1, cols) so the child's layout matches.

    The bar is drawn **once on attach and on SIGWINCH only** -- never
    interleaved with the child's output stream, which was the cause of
    the earlier rendering glitches (cursor save/restore injected mid-
    render corrupted the child's cursor state).

    Detach with **Ctrl-]** (0x1d), same convention as ``screen``/``telnet``.
    Returns the child exit code, or -1 on detach.
    """
    master_fd = task.pty_master_fd
    if master_fd is None or task.process is None:
        raise RuntimeError("Task is not running or has no PTY")

    real_stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    old_tty_attrs = termios.tcgetattr(real_stdin_fd)

    try:
        tty.setraw(real_stdin_fd)
        _set_terminal_title(f"Gauss \u203a {task.task_id}  \u2502  Ctrl-] detach")

        # Sync PTY size to the real terminal (no scroll region / status bar --
        # CC has its own bottom chrome that conflicts with ours).
        rows, cols = _get_terminal_size(real_stdin_fd)
        _set_pty_size(master_fd, rows, cols)

        task._attached = True

        if task.process is not None:
            try:
                task.process.send_signal(signal.SIGWINCH)
            except (OSError, ProcessLookupError):
                pass

        try:
            _replay_recent_output(task, stdout_fd)
        except OSError:
            pass

        def _handle_sigwinch(signum, frame):
            nonlocal rows, cols
            try:
                rows, cols = _get_terminal_size(real_stdin_fd)
                _set_pty_size(master_fd, rows, cols)
            except Exception:
                pass
            if task.process is not None:
                try:
                    task.process.send_signal(signal.SIGWINCH)
                except (OSError, ProcessLookupError):
                    pass

        prev_sigwinch = signal.signal(signal.SIGWINCH, _handle_sigwinch)

        _trust_check_buf = bytearray()
        _trust_handled = False

        try:
            while True:
                try:
                    rlist, _, _ = select.select([real_stdin_fd, master_fd], [], [], 0.25)
                except (ValueError, OSError):
                    break

                if real_stdin_fd in rlist:
                    try:
                        data = os.read(real_stdin_fd, 1024)
                    except OSError:
                        break
                    if not data:
                        break
                    if b"\x1d" in data:
                        break
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        break

                if master_fd in rlist:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    _remember_recent_output(task, data)
                    try:
                        os.write(stdout_fd, data)
                    except OSError:
                        break

                    if not _trust_handled:
                        _trust_check_buf.extend(data)
                        text = _trust_check_buf.decode("utf-8", errors="replace").lower()
                        if "yes, i trust" in text or "trust this folder" in text:
                            os.write(master_fd, b"\r")
                            _trust_handled = True
                            _trust_check_buf.clear()
                        elif len(_trust_check_buf) > 8192:
                            _trust_handled = True
                            _trust_check_buf.clear()

                if task.process is not None and task.process.poll() is not None:
                    try:
                        rlist2, _, _ = select.select([master_fd], [], [], 0.1)
                        if master_fd in rlist2:
                            remaining = os.read(master_fd, 4096)
                            if remaining:
                                os.write(stdout_fd, remaining)
                    except OSError:
                        pass
                    break
        finally:
            signal.signal(signal.SIGWINCH, prev_sigwinch)
    finally:
        task._attached = False
        try:
            _set_terminal_title("Gauss")
        except Exception:
            pass
        termios.tcsetattr(real_stdin_fd, termios.TCSAFLUSH, old_tty_attrs)

    if task.process is not None:
        ret = task.process.poll()
        return ret if ret is not None else -1
    return -1


class SwarmManager:
    """Singleton registry that tracks all spawned autoformalization agents.

    Thread-safe: all mutable state is guarded by ``_lock``.
    """

    _instance: Optional[SwarmManager] = None

    def __new__(cls) -> SwarmManager:
        if cls._instance is None:
            inst = super().__new__(cls)
            inst.tasks: Dict[str, SwarmTask] = {}
            inst._counter = 0
            inst._lock = threading.Lock()
            inst._on_complete: Optional[Callable[[SwarmTask], None]] = None
            cls._instance = inst
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Tear down the singleton (for testing)."""
        cls._instance = None

    def set_on_complete(self, callback: Optional[Callable[[SwarmTask], None]]) -> None:
        """Register a callback invoked when any task finishes."""
        self._on_complete = callback

    def spawn(
        self,
        theorem: str,
        description: str,
        *,
        workflow_kind: str = "",
        workflow_command: str = "",
        project_name: str = "",
        project_root: str = "",
        working_dir: str = "",
        backend_name: str = "",
        run_fn: Any = None,
        run_kwargs: Optional[Dict[str, Any]] = None,
    ) -> SwarmTask:
        """Create a new SwarmTask and, if *run_fn* is provided, start it in a daemon thread.

        Parameters
        ----------
        theorem:
            The theorem statement to formalize.
        description:
            Short human-readable label for the task.
        run_fn:
            Callable ``(task, **run_kwargs) -> None`` executed on a daemon thread.
            When *None* the task stays in ``queued`` status for external dispatch.
        run_kwargs:
            Extra keyword arguments forwarded to *run_fn*.
        """
        with self._lock:
            self._counter += 1
            task_id = f"af-{self._counter:03d}"
            task = SwarmTask(
                task_id=task_id,
                description=description,
                theorem=theorem,
                workflow_kind=workflow_kind,
                workflow_command=workflow_command,
                project_name=project_name,
                project_root=project_root,
                working_dir=working_dir,
                backend_name=backend_name,
            )
            self.tasks[task_id] = task

        if run_fn is not None:

            def _target() -> None:
                task.status = "running"
                task.start_time = time.time()
                try:
                    run_fn(task, **(run_kwargs or {}))
                    with self._lock:
                        if task.status == "running":
                            task.status = "complete"
                except Exception as exc:
                    with self._lock:
                        task.status = "failed"
                        task.error = str(exc)
                finally:
                    with self._lock:
                        if task.end_time is None:
                            task.end_time = time.time()
                    if self._on_complete:
                        try:
                            self._on_complete(task)
                        except Exception:
                            pass

            session_id = f"af_{uuid.uuid4().hex[:8]}"
            task.session_id = session_id
            thread = threading.Thread(
                target=_target,
                daemon=True,
                name=f"swarm-{task_id}",
            )
            task.thread = thread
            thread.start()

        return task

    def spawn_claude(
        self,
        theorem: str,
        description: str,
        *,
        argv: Sequence[str],
        cwd: str,
        env: Dict[str, str],
        workflow_kind: str = "",
        workflow_command: str = "",
        project_name: str = "",
        project_root: str = "",
        backend_name: str = "",
    ) -> SwarmTask:
        """Spawn a background Claude Code process tracked as a swarm task.

        The process runs in ``-p --output-format stream-json`` mode so we
        can parse live events and update the task's progress without
        blocking the TUI.
        """
        with self._lock:
            self._counter += 1
            task_id = f"af-{self._counter:03d}"
            task = SwarmTask(
                task_id=task_id,
                description=description,
                theorem=theorem,
                workflow_kind=workflow_kind,
                workflow_command=workflow_command,
                project_name=project_name,
                project_root=project_root,
                working_dir=cwd,
                backend_name=backend_name,
            )
            self.tasks[task_id] = task

        session_id = f"af_{uuid.uuid4().hex[:8]}"
        task.session_id = session_id
        argv, env = _normalize_claude_session_launch(argv, env, cwd=cwd)

        def _target() -> None:
            _run_claude_code_background(task, argv, cwd, env)
            if self._on_complete:
                try:
                    self._on_complete(task)
                except Exception:
                    pass

        thread = threading.Thread(
            target=_target,
            daemon=True,
            name=f"swarm-{task_id}",
        )
        task.thread = thread
        thread.start()
        return task

    def spawn_interactive(
        self,
        theorem: str,
        description: str,
        *,
        argv: Sequence[str],
        cwd: str,
        env: Dict[str, str],
        workflow_kind: str = "",
        workflow_command: str = "",
        project_name: str = "",
        project_root: str = "",
        backend_name: str = "",
    ) -> SwarmTask:
        """Spawn an interactive Claude Code session behind a PTY.

        Unlike :meth:`spawn_claude`, this launches CC in full interactive
        mode (no ``-p``).  The user can later attach via
        ``/swarm attach <id>`` to interact live, and detach with Ctrl-].
        While detached the session continues running in the background.
        """
        with self._lock:
            self._counter += 1
            task_id = f"af-{self._counter:03d}"
            task = SwarmTask(
                task_id=task_id,
                description=description,
                theorem=theorem,
                workflow_kind=workflow_kind,
                workflow_command=workflow_command,
                project_name=project_name,
                project_root=project_root,
                working_dir=cwd,
                backend_name=backend_name,
            )
            self.tasks[task_id] = task

        session_id = f"af_{uuid.uuid4().hex[:8]}"
        task.session_id = session_id
        argv, env = _normalize_claude_session_launch(argv, env, cwd=cwd)

        def _target() -> None:
            _run_claude_code_interactive(task, argv, cwd, env)
            if self._on_complete:
                try:
                    self._on_complete(task)
                except Exception:
                    pass

        thread = threading.Thread(
            target=_target,
            daemon=True,
            name=f"swarm-{task_id}",
        )
        task.thread = thread
        thread.start()
        return task

    def get_task(self, task_id: str) -> Optional[SwarmTask]:
        with self._lock:
            return self.tasks.get(task_id)

    def latest_task(
        self,
        *,
        status: Optional[str] = None,
        require_pty: bool = False,
    ) -> Optional[SwarmTask]:
        """Return the most recently created task matching the given filters."""
        with self._lock:
            tasks = list(self.tasks.values())
        for task in reversed(tasks):
            if status is not None and task.status != status:
                continue
            if require_pty and task.pty_master_fd is None:
                continue
            return task
        return None

    def list_tasks(self, status: Optional[str] = None) -> List[SwarmTask]:
        with self._lock:
            tasks = list(self.tasks.values())
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def cancel(self, task_id: str) -> bool:
        """Mark a task as cancelled and terminate the subprocess if running."""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return False
            if task.status in ("complete", "failed", "cancelled"):
                return False
            task.status = "cancelled"
            task.end_time = time.time()
        if task.process is not None:
            try:
                task.process.terminate()
            except OSError:
                pass
        return True

    def counts(self) -> Dict[str, int]:
        """Return ``{status: count}`` across all tasks."""
        out: Dict[str, int] = {}
        with self._lock:
            for t in self.tasks.values():
                out[t.status] = out.get(t.status, 0) + 1
        return out

    # ------------------------------------------------------------------
    # Rich table rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _status_indicator(status: str) -> str:
        """Return a styled status token using the Math, Inc. palette."""
        skin = get_active_skin()
        ok = skin.get_color("ui_ok", "#8B9B7A")
        accent = skin.get_color("ui_accent", "#C8C0B0")
        dim = skin.get_color("banner_dim", "#6B6B60")
        err = skin.get_color("ui_error", "#B07070")

        mapping = {
            "complete": f"[{ok}]● done[/]",
            "running": f"[{accent}]● active[/]",
            "queued": f"[{dim}]○ queued[/]",
            "failed": f"[{err}]● failed[/]",
            "cancelled": f"[{dim}]● cancel[/]",
        }
        return mapping.get(status, status)

    def render_table(self) -> Table:
        """Build a Rich Table summarising all swarm tasks."""
        skin = get_active_skin()
        border = skin.get_color("banner_border", "#5B6B4F")
        title_color = skin.get_color("banner_title", "#C8C0B0")
        text_color = skin.get_color("banner_text", "#C8C0B0")
        dim = skin.get_color("banner_dim", "#6B6B60")

        table = Table(
            title=f"[bold {title_color}]Gauss Workflow Swarm[/]",
            border_style=border,
            show_lines=False,
            pad_edge=True,
            expand=False,
        )
        table.add_column("ID", style=text_color, no_wrap=True)
        table.add_column("Workflow", style=text_color, max_width=26)
        table.add_column("Project", style=text_color, max_width=18)
        table.add_column("Status", no_wrap=True)
        table.add_column("Time", style=dim, no_wrap=True, justify="right")
        table.add_column("Lean", style=text_color, no_wrap=True)
        table.add_column("Progress", style=dim)

        now = time.time()
        for task in self.list_tasks():
            elapsed: str
            if task.start_time is not None:
                end = task.end_time if task.end_time is not None else now
                elapsed = _format_elapsed(end - task.start_time)
            else:
                elapsed = "\u2014"

            lean = task.lean_status or "\u2014"
            progress = task.progress
            if task.pty_master_fd is not None and task.status == "running":
                progress += f" [{dim}]› attach[/]"
            workflow = task.workflow_kind or "workflow"
            if task.description:
                workflow = f"{workflow}: {task.description}"
            project = task.project_name or (task.project_root.rsplit("/", 1)[-1] if task.project_root else "\u2014")
            table.add_row(
                task.task_id,
                workflow,
                project,
                self._status_indicator(task.status),
                elapsed,
                lean,
                progress,
            )

        return table

    def render_detail(self, task_id: str) -> Optional[Table]:
        """Build a Rich Table with extended detail for a single task."""
        task = self.get_task(task_id)
        if task is None:
            return None

        skin = get_active_skin()
        border = skin.get_color("banner_border", "#5B6B4F")
        title_color = skin.get_color("banner_title", "#C8C0B0")
        text_color = skin.get_color("banner_text", "#C8C0B0")
        dim = skin.get_color("banner_dim", "#6B6B60")

        table = Table(
            title=f"[bold {title_color}]Task {task.task_id}[/]",
            border_style=border,
            show_header=False,
            pad_edge=True,
            expand=False,
        )
        table.add_column("Key", style=f"bold {dim}", no_wrap=True)
        table.add_column("Value", style=text_color)

        now = time.time()
        elapsed = "\u2014"
        if task.start_time is not None:
            end = task.end_time if task.end_time is not None else now
            elapsed = _format_elapsed(end - task.start_time)

        table.add_row("ID", task.task_id)
        table.add_row("Description", task.description)
        table.add_row("Theorem", task.theorem)
        if task.workflow_kind:
            table.add_row("Workflow", task.workflow_kind)
        if task.workflow_command:
            table.add_row("Command", task.workflow_command)
        if task.backend_name:
            table.add_row("Backend", task.backend_name)
        if task.project_name:
            table.add_row("Project", task.project_name)
        elif task.project_root:
            table.add_row("Project", task.project_root.rsplit("/", 1)[-1])
        if task.project_root:
            table.add_row("Project Root", task.project_root)
        if task.working_dir:
            table.add_row("Working Dir", task.working_dir)
        table.add_row("Status", self._status_indicator(task.status))
        table.add_row("Elapsed", elapsed)
        table.add_row("Lean", task.lean_status or "\u2014")
        table.add_row("Progress", task.progress)
        if task.session_id:
            table.add_row("Session", task.session_id)
        if task.pty_master_fd is not None and task.status == "running":
            ok = skin.get_color("ui_ok", "#8B9B7A")
            table.add_row("Attach", f"[{ok}]/swarm attach {task.task_id}[/]  (Ctrl-] to detach)")
        if task.result:
            table.add_row("Result", task.result)
        if task.error:
            err_color = skin.get_color("ui_error", "#B07070")
            table.add_row("Error", f"[{err_color}]{task.error}[/]")

        return table

    def summary_line(self) -> Optional[str]:
        """Return a one-line Rich-markup summary for the welcome banner, or ``None``."""
        c = self.counts()
        if not c:
            return None
        skin = get_active_skin()
        ok = skin.get_color("ui_ok", "#8B9B7A")
        accent = skin.get_color("ui_accent", "#C8C0B0")
        dim = skin.get_color("banner_dim", "#6B6B60")
        err = skin.get_color("ui_error", "#B07070")

        parts: List[str] = []
        running = c.get("running", 0)
        complete = c.get("complete", 0)
        queued = c.get("queued", 0)
        failed = c.get("failed", 0)

        if running:
            noun = "agent" if running == 1 else "agents"
            parts.append(f"[{accent}]{running} {noun} running[/]")
        if complete:
            parts.append(f"[{ok}]{complete} complete[/]")
        if queued:
            parts.append(f"[{dim}]{queued} queued[/]")
        if failed:
            parts.append(f"[{err}]{failed} failed[/]")

        if not parts:
            return None
        return f"[{dim}]Swarm:[/] " + f" [{dim}]·[/] ".join(parts)

    def status_bar_fragment(self) -> Optional[str]:
        """Return a compact plain-text fragment for the TUI status bar, or ``None``."""
        c = self.counts()
        running = c.get("running", 0)
        if not running and not c:
            return None
        total = sum(c.values())
        if running:
            return f"af:{running}/{total}"
        return f"af:{total}"
