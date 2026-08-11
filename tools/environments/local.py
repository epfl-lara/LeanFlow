"""Local execution environment with interrupt support and non-blocking I/O."""

import errno
import glob
import os
import platform
import secrets
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

_IS_WINDOWS = platform.system() == "Windows"

import contextlib

from tools.environments.base import BaseEnvironment
from tools.environments.persistent_shell import PersistentShellMixin
from tools.utilities.interrupt import is_interrupted
from tools.utilities.process_tree import terminate_process_tree

# Unique marker to isolate real command output from shell init/exit noise.
# printf (no trailing newline) keeps the boundaries clean for splitting.
_OUTPUT_FENCE = "__LEANFLOW_FENCE_a9f7b3__"

# LeanFlow-internal env vars that should NOT leak into terminal subprocesses.
# These are loaded from ~/.leanflow/.env for LeanFlow's own LLM/provider calls
# but can break external CLIs (e.g. codex) that also honor them.
#
# Built dynamically from the provider registry so new providers are
# automatically covered without manual blocklist maintenance.
_LEANFLOW_PROVIDER_ENV_FORCE_PREFIX = "_LEANFLOW_FORCE_"


def _build_provider_env_blocklist() -> frozenset:
    """Derive the blocklist from provider, tool, and gateway config.

    Automatically picks up api_key_env_vars and base_url_env_var from
    every registered provider, plus tool/messaging env vars from the
    optional config registry, so new LeanFlow-managed secrets are blocked
    in subprocesses without having to maintain multiple static lists.
    """
    blocked: set[str] = set()

    try:
        from leanflow_cli.runtime.auth import PROVIDER_REGISTRY

        for pconfig in PROVIDER_REGISTRY.values():
            blocked.update(pconfig.api_key_env_vars)
            if pconfig.base_url_env_var:
                blocked.add(pconfig.base_url_env_var)
    except ImportError:
        pass

    try:
        from leanflow_cli.config import OPTIONAL_ENV_VARS

        for name, metadata in OPTIONAL_ENV_VARS.items():
            category = metadata.get("category")
            if (
                category in {"tool", "messaging"}
                or category == "setting"
                and metadata.get("password")
            ):
                blocked.add(name)
    except ImportError:
        pass

    # Vars not covered above but still LeanFlow-internal / conflict-prone.
    blocked.update(
        {
            "TELEGRAM_BOT_TOKEN",
            "OPENAI_BASE_URL",
            "OPENAI_API_KEY",
            "OPENAI_API_BASE",  # legacy alias
            "OPENAI_ORG_ID",
            "OPENAI_ORGANIZATION",
            "OPENROUTER_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_TOKEN",  # OAuth token (not in registry as env var)
            "CLAUDE_CODE_OAUTH_TOKEN",
            "LLM_MODEL",
            # Expanded isolation for other major providers (Issue #1002)
            "GOOGLE_API_KEY",  # Gemini / Google AI Studio
            "DEEPSEEK_API_KEY",  # DeepSeek
            "MISTRAL_API_KEY",  # Mistral AI
            "GROQ_API_KEY",  # Groq
            "TOGETHER_API_KEY",  # Together AI
            "PERPLEXITY_API_KEY",  # Perplexity
            "COHERE_API_KEY",  # Cohere
            "FIREWORKS_API_KEY",  # Fireworks AI
            "XAI_API_KEY",  # xAI (Grok)
            "HELICONE_API_KEY",  # LLM Observability proxy
            # Gateway/runtime config not represented in OPTIONAL_ENV_VARS.
            "TELEGRAM_HOME_CHANNEL",
            "TELEGRAM_HOME_CHANNEL_NAME",
            "DISCORD_HOME_CHANNEL",
            "DISCORD_HOME_CHANNEL_NAME",
            "DISCORD_REQUIRE_MENTION",
            "DISCORD_FREE_RESPONSE_CHANNELS",
            "DISCORD_AUTO_THREAD",
            "SLACK_HOME_CHANNEL",
            "SLACK_HOME_CHANNEL_NAME",
            "SLACK_ALLOWED_USERS",
            "WHATSAPP_ENABLED",
            "WHATSAPP_MODE",
            "WHATSAPP_ALLOWED_USERS",
            "SLACK_APP_TOKEN",
            "SIGNAL_HTTP_URL",
            "SIGNAL_ACCOUNT",
            "SIGNAL_ALLOWED_USERS",
            "SIGNAL_GROUP_ALLOWED_USERS",
            "SIGNAL_HOME_CHANNEL",
            "SIGNAL_HOME_CHANNEL_NAME",
            "SIGNAL_IGNORE_STORIES",
            "HASS_TOKEN",
            "HASS_URL",
            "EMAIL_ADDRESS",
            "EMAIL_PASSWORD",
            "EMAIL_IMAP_HOST",
            "EMAIL_SMTP_HOST",
            "EMAIL_HOME_ADDRESS",
            "EMAIL_HOME_ADDRESS_NAME",
            "FIRECRAWL_API_KEY",
            "BROWSERBASE_PROJECT_ID",
            "ELEVENLABS_API_KEY",
            "GITHUB_TOKEN",
            "GATEWAY_ALLOWED_USERS",
            "GATEWAY_ALLOW_ALL_USERS",
            # Skills Hub / GitHub app auth paths and aliases.
            "GH_TOKEN",
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY_PATH",
            "GITHUB_APP_INSTALLATION_ID",
        }
    )
    return frozenset(blocked)


_LEANFLOW_PROVIDER_ENV_BLOCKLIST = _build_provider_env_blocklist()


def _sanitize_subprocess_env(base_env: dict | None, extra_env: dict | None = None) -> dict:
    """Filter LeanFlow-managed secrets from a subprocess environment.

    `_LEANFLOW_FORCE_<VAR>` entries in ``extra_env`` opt a blocked variable back in
    intentionally for callers that truly need it.
    """
    sanitized: dict[str, str] = {}

    for key, value in (base_env or {}).items():
        if key.startswith(_LEANFLOW_PROVIDER_ENV_FORCE_PREFIX):
            continue
        if key not in _LEANFLOW_PROVIDER_ENV_BLOCKLIST:
            sanitized[key] = value

    for key, value in (extra_env or {}).items():
        if key.startswith(_LEANFLOW_PROVIDER_ENV_FORCE_PREFIX):
            real_key = key[len(_LEANFLOW_PROVIDER_ENV_FORCE_PREFIX) :]
            sanitized[real_key] = value
        elif key not in _LEANFLOW_PROVIDER_ENV_BLOCKLIST:
            sanitized[key] = value

    return sanitized


def _find_bash() -> str:
    """Find bash for command execution.

    The fence wrapper uses bash syntax (semicolons, $?, printf), so we
    must use bash — not the user's $SHELL which could be fish/zsh/etc.
    On Windows: uses Git Bash (bundled with Git for Windows).
    """
    if not _IS_WINDOWS:
        return (
            shutil.which("bash")
            or ("/usr/bin/bash" if os.path.isfile("/usr/bin/bash") else None)
            or ("/bin/bash" if os.path.isfile("/bin/bash") else None)
            or os.environ.get("SHELL")  # last resort: whatever they have
            or "/bin/sh"
        )

    # Windows: look for Git Bash (installed with Git for Windows).
    # Allow override via env var (same pattern as Claude Code).
    custom = os.environ.get("LEANFLOW_GIT_BASH_PATH")
    if custom and os.path.isfile(custom):
        return custom

    # shutil.which finds bash.exe if Git\bin is on PATH
    found = shutil.which("bash")
    if found:
        return found

    # Check common Git for Windows install locations
    for candidate in (
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
        os.path.join(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "bin", "bash.exe"
        ),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate

    raise RuntimeError(
        "Git Bash not found. LeanFlow requires Git for Windows on Windows.\n"
        "Install it from: https://git-scm.com/download/win\n"
        "Or set LEANFLOW_GIT_BASH_PATH to your bash.exe location."
    )


# Backward compat — process_registry.py imports this name
_find_shell = _find_bash


# Noise lines emitted by interactive shells when stdin is not a terminal.
# Used as a fallback when output fence markers are missing.
_SHELL_NOISE_SUBSTRINGS = (
    # bash
    "bash: cannot set terminal process group",
    "bash: no job control in this shell",
    "no job control in this shell",
    "cannot set terminal process group",
    "tcsetattr: Inappropriate ioctl for device",
    # zsh / oh-my-zsh / macOS terminal session
    "Restored session:",
    "Saving session...",
    "Last login:",
    "command not found:",
    "Oh My Zsh",
    "compinit:",
)


def _clean_shell_noise(output: str) -> str:
    """Strip shell startup/exit warnings that leak when using -i without a TTY.

    Removes lines matching known noise patterns from both the beginning
    and end of the output.  Lines in the middle are left untouched.
    """

    def _is_noise(line: str) -> bool:
        return any(noise in line for noise in _SHELL_NOISE_SUBSTRINGS)

    lines = output.split("\n")

    # Strip leading noise
    while lines and _is_noise(lines[0]):
        lines.pop(0)

    # Strip trailing noise (walk backwards, skip empty lines from split)
    end = len(lines) - 1
    while end >= 0 and (not lines[end] or _is_noise(lines[end])):
        end -= 1

    if end < 0:
        return ""

    cleaned = lines[: end + 1]
    result = "\n".join(cleaned)

    # Preserve trailing newline if original had one
    if output.endswith("\n") and result and not result.endswith("\n"):
        result += "\n"
    return result


_SANE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _make_run_env(env: dict) -> dict:
    """Build a run environment with a sane PATH and provider-var stripping."""
    merged = dict(os.environ | env)
    run_env = {}
    for k, v in merged.items():
        if k.startswith(_LEANFLOW_PROVIDER_ENV_FORCE_PREFIX):
            real_key = k[len(_LEANFLOW_PROVIDER_ENV_FORCE_PREFIX) :]
            run_env[real_key] = v
        elif k not in _LEANFLOW_PROVIDER_ENV_BLOCKLIST:
            run_env[k] = v
    existing_path = run_env.get("PATH", "")
    if "/usr/bin" not in existing_path.split(":"):
        run_env["PATH"] = f"{existing_path}:{_SANE_PATH}" if existing_path else _SANE_PATH
    return run_env


def _extract_fenced_output(raw: str) -> str:
    """Extract real command output from between fence markers.

    The execute() method wraps each command with printf(FENCE) markers.
    This function finds the first and last fence and returns only the
    content between them, which is the actual command output free of
    any shell init/exit noise.

    Falls back to pattern-based _clean_shell_noise if fences are missing.
    """
    first = raw.find(_OUTPUT_FENCE)
    if first == -1:
        return _clean_shell_noise(raw)

    start = first + len(_OUTPUT_FENCE)
    last = raw.rfind(_OUTPUT_FENCE)

    if last <= first:
        # Only start fence found (e.g. user command called `exit`)
        return _clean_shell_noise(raw[start:])

    return raw[start:last]


def _has_complete_output_fence(raw: str) -> bool:
    """Return whether both protocol fences have reached the output reader."""
    first = raw.find(_OUTPUT_FENCE)
    return first >= 0 and raw.rfind(_OUTPUT_FENCE) > first


def _extract_interrupted_output(raw: str) -> str:
    """Extract partial command output without exposing shell protocol noise."""
    output = _extract_fenced_output(raw).rstrip("\r\n")
    lines = output.splitlines()
    while lines and lines[-1].strip() == "logout":
        lines.pop()
    return "\n".join(lines)


def _open_atomic_stage(parent: Path, target_name: str) -> tuple[int, str]:
    """Open a unique same-directory stage while honoring the process umask."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for _ in range(8):
        candidate = parent / f".{target_name}.{secrets.token_hex(12)}.leanflow-tmp"
        try:
            # Unlike mkstemp(), os.open applies the process umask to 0o666. New
            # files therefore retain the mode produced by the former `cat >`
            # implementation, while O_EXCL keeps stage creation race-safe.
            return os.open(candidate, flags, 0o666), str(candidate)
        except FileExistsError:
            continue
    raise FileExistsError(f"could not allocate atomic stage in {parent}")


class LocalEnvironment(PersistentShellMixin, BaseEnvironment):
    """Run commands directly on the host machine.

    Features:
    - Popen + polling for interrupt support (user can cancel mid-command)
    - Background stdout drain thread to prevent pipe buffer deadlocks
    - stdin_data support for piping content (bypasses ARG_MAX limits)
    - sudo -S transform via SUDO_PASSWORD env var
    - Uses interactive login shell so full user env is available
    - Optional persistent shell mode (cwd/env vars survive across calls)
    """

    supports_atomic_text_writes = True

    def __init__(
        self, cwd: str = "", timeout: int = 60, env: dict = None, persistent: bool = False
    ):
        super().__init__(cwd=cwd or os.getcwd(), timeout=timeout, env=env)
        self.persistent = persistent
        self._active_processes: dict[int, subprocess.Popen] = {}
        self._active_processes_lock = threading.Lock()
        if self.persistent:
            self._init_persistent_shell()

    @property
    def _temp_prefix(self) -> str:
        return f"/tmp/leanflow-local-{self._session_id}"

    def _spawn_shell_process(self) -> subprocess.Popen:
        user_shell = _find_bash()
        run_env = _make_run_env(self.env)
        return subprocess.Popen(
            [user_shell, "-l"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=run_env,
            preexec_fn=None if _IS_WINDOWS else os.setsid,
        )

    def _read_temp_files(self, *paths: str) -> list[str]:
        results = []
        for path in paths:
            if os.path.exists(path):
                with open(path) as f:
                    results.append(f.read())
            else:
                results.append("")
        return results

    def _kill_shell_children(self):
        shell_proc = self._shell_proc
        if shell_proc is None or shell_proc.poll() is not None:
            return
        if _IS_WINDOWS:
            return
        terminate_process_tree(
            shell_proc.pid,
            expected_session_id=shell_proc.pid,
            include_root=False,
        )

    def _track_process(self, proc: subprocess.Popen) -> None:
        """Register an in-flight oneshot so runner shutdown can reap it."""
        with self._active_processes_lock:
            self._active_processes[proc.pid] = proc

    def _untrack_process(self, proc: subprocess.Popen) -> None:
        """Forget an in-flight oneshot after its execution boundary closes."""
        with self._active_processes_lock:
            self._active_processes.pop(proc.pid, None)

    def _terminate_process(self, proc: subprocess.Popen) -> bool:
        """Terminate one live command tree and report whether signaling was needed."""
        if proc.poll() is not None:
            return False
        if _IS_WINDOWS:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                proc.terminate()
        else:
            terminate_process_tree(
                proc.pid,
                expected_session_id=proc.pid,
                include_root=True,
            )
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                proc.kill()
        return True

    def cleanup(self):
        """Terminate active local commands before releasing persistent-shell state."""
        with self._active_processes_lock:
            active = list(self._active_processes.values())
        for proc in active:
            self._terminate_process(proc)
        if self.persistent:
            self._kill_shell_children()
        super().cleanup()

    def _cleanup_temp_files(self):
        for f in glob.glob(f"{self._temp_prefix}-*"):
            if os.path.exists(f):
                os.remove(f)

    def write_text_atomic(
        self,
        path: str,
        content: str,
        *,
        cwd: str = "",
        complete_on_interrupt: bool = False,
    ) -> dict:
        """Atomically replace one local text file with staged UTF-8 content.

        Normal writes honor an interrupt before committing and therefore leave
        the old file intact. Bounded recovery writes may opt into completing
        after an interrupt so managed-artifact rollback cannot itself be torn.
        """
        if is_interrupted() and not complete_on_interrupt:
            return {
                "output": "[Command interrupted — user sent a new message]",
                "returncode": 130,
            }

        base = Path(cwd or self.cwd or os.getcwd()).expanduser()
        requested = Path(path).expanduser()
        if not requested.is_absolute():
            requested = base / requested
        target = requested.resolve(strict=False)
        parent = target.parent
        dirs_created = not parent.exists()
        fd: int | None = None
        temp_path: str | None = None

        try:
            if target.exists() and not os.access(target, os.W_OK):
                # Replacement only needs directory permission and would
                # otherwise bypass a denial that the former `cat > target`
                # path correctly surfaced from the target itself.
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(target))
            parent.mkdir(parents=True, exist_ok=True)
            existing_mode = None
            with contextlib.suppress(FileNotFoundError):
                existing_mode = stat.S_IMODE(target.stat().st_mode)

            fd, temp_path = _open_atomic_stage(parent, target.name)
            if existing_mode is not None:
                os.fchmod(fd, existing_mode)
            stream = os.fdopen(fd, "w", encoding="utf-8", newline="")
            fd = None  # fd ownership transferred to the stream.
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())

            if is_interrupted() and not complete_on_interrupt:
                return {
                    "output": "[Command interrupted — user sent a new message]",
                    "returncode": 130,
                }

            os.replace(temp_path, target)
            temp_path = None
            if not _IS_WINDOWS:
                with contextlib.suppress(OSError):
                    directory_fd = os.open(parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            return {
                "output": "",
                "returncode": 0,
                "bytes_written": len(content.encode("utf-8")),
                "dirs_created": dirs_created,
            }
        except (OSError, UnicodeError) as exc:
            return {"output": str(exc), "returncode": 1}
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temp_path)

    def _execute_oneshot(
        self,
        command: str,
        cwd: str = "",
        *,
        timeout: int | None = None,
        stdin_data: str | None = None,
    ) -> dict:
        """Execute a command as a oneshot subprocess with interrupt/timeout support and noise-free output extraction. Spawns daemon threads to write stdin and drain stdout in parallel, preventing I/O deadlocks on large inputs; uses fence markers to isolate real output from shell initialization noise; polls for user interrupts or timeout, killing the process group appropriately on either condition."""
        work_dir = cwd or self.cwd or os.getcwd()
        effective_timeout = timeout or self.timeout
        exec_command, sudo_stdin = self._prepare_command(command)

        if sudo_stdin is not None and stdin_data is not None:
            effective_stdin = sudo_stdin + stdin_data
        elif sudo_stdin is not None:
            effective_stdin = sudo_stdin
        else:
            effective_stdin = stdin_data

        user_shell = _find_bash()
        fenced_cmd = (
            f"printf '{_OUTPUT_FENCE}';"
            # Keep the fence trailer on a fresh line. A heredoc terminator
            # must be the only token on its line; appending ``;`` here turns
            # a valid ``EOF`` into shell input for the child program.
            f" {exec_command}\n"
            f" __leanflow_rc=$?;"
            f" printf '{_OUTPUT_FENCE}';"
            f" exit $__leanflow_rc"
        )
        run_env = _make_run_env(self.env)

        proc = subprocess.Popen(
            [user_shell, "-lic", fenced_cmd],
            text=True,
            cwd=work_dir,
            env=run_env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if effective_stdin is not None else subprocess.DEVNULL,
            preexec_fn=None if _IS_WINDOWS else os.setsid,
        )
        self._track_process(proc)
        try:
            return self._wait_for_oneshot_process(
                proc,
                effective_stdin=effective_stdin,
                effective_timeout=effective_timeout,
            )
        finally:
            self._untrack_process(proc)

    def _wait_for_oneshot_process(
        self,
        proc: subprocess.Popen,
        *,
        effective_stdin: str | None,
        effective_timeout: int,
    ) -> dict:
        """Wait for one tracked command and enforce interrupt and timeout cleanup."""

        if effective_stdin is not None:

            def _write_stdin():
                try:
                    proc.stdin.write(effective_stdin)
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

            threading.Thread(target=_write_stdin, daemon=True).start()

        _output_chunks: list[str] = []

        def _drain_stdout():
            try:
                for line in proc.stdout:
                    _output_chunks.append(line)
            except ValueError:
                pass
            finally:
                with contextlib.suppress(Exception):
                    proc.stdout.close()

        reader = threading.Thread(target=_drain_stdout, daemon=True)
        reader.start()
        deadline = time.monotonic() + effective_timeout

        while proc.poll() is None:
            if is_interrupted():
                # The command can finish after the loop's poll but before the
                # interrupt check. Preserve its truthful exit status in that
                # race instead of overwriting it with synthetic status 130.
                completed_returncode = proc.poll()
                if completed_returncode is not None:
                    reader.join(timeout=5)
                    return {
                        "output": _extract_fenced_output("".join(_output_chunks)),
                        "returncode": completed_returncode,
                    }

                # A closing fence proves the wrapped command has completed;
                # give the login shell a bounded chance to finish logout and
                # expose the wrapped command's real return code.
                raw_output = "".join(_output_chunks)
                if _has_complete_output_fence(raw_output):
                    try:
                        completed_returncode = proc.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        reader.join(timeout=5)
                        return {
                            "output": _extract_fenced_output("".join(_output_chunks)),
                            "returncode": completed_returncode,
                        }

                terminated = self._terminate_process(proc)
                reader.join(timeout=2)
                if not terminated:
                    return {
                        "output": _extract_fenced_output("".join(_output_chunks)),
                        "returncode": proc.returncode,
                    }
                interrupted_output = _extract_interrupted_output("".join(_output_chunks))
                if interrupted_output:
                    interrupted_output += "\n"
                return {
                    "output": interrupted_output
                    + "[Command interrupted — user sent a new message]",
                    "returncode": 130,
                }
            if time.monotonic() > deadline:
                terminated = self._terminate_process(proc)
                reader.join(timeout=2)
                if not terminated:
                    return {
                        "output": _extract_fenced_output("".join(_output_chunks)),
                        "returncode": proc.returncode,
                    }
                return self._timeout_result(effective_timeout)
            time.sleep(0.2)

        reader.join(timeout=5)
        output = _extract_fenced_output("".join(_output_chunks))
        return {"output": output, "returncode": proc.returncode}
