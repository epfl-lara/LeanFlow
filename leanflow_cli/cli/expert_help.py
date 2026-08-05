"""Expert-help provider dispatch for LeanFlow advisory workflows."""

from __future__ import annotations

import contextlib
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from leanflow_cli.config import get_env_value, load_config
from leanflow_cli.workflows.workflow_state import (
    append_workflow_activity,
    append_workflow_run_log,
    touch_workflow_runtime_heartbeat,
)
from tools.utilities.interrupt import is_interrupted

COMMAND_PROVIDER_ALIASES = {
    "codex": "codex",
    "codex-cli": "codex",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claude_code": "claude-code",
}

DEFAULT_COMMAND_TEMPLATES = {
    "codex": (
        "codex exec --sandbox read-only --skip-git-repo-check --ephemeral "
        "--ignore-rules --color never --output-last-message {output_file} --cd {cwd} -"
    ),
    "claude-code": "claude --print --permission-mode plan --tools '' --no-session-persistence",
}

# Keep in sync with _AUXILIARY_TASK_FALLBACKS in agent/providers/auxiliary_client.py.
TASK_FALLBACKS = {
    "lean_decompose_helpers": "lean_reasoning",
    "planner_synthesis": "orchestration",
}

_EXPERT_PROCESS_TOKEN_ENV = "LEANFLOW_INTERNAL_EXPERT_PROCESS_TOKEN"
_EXPERT_COMMUNICATE_POLL_S = 0.1
_EXPERT_HEARTBEAT_INTERVAL_S = 15.0
_EXPERT_SHUTDOWN_WAIT_S = 5.0


@dataclass(frozen=True)
class ExpertCommandResult:
    provider: str
    command: list[str]
    exit_status: int | None
    response: str
    stderr: str
    truncated: bool
    response_chars: int
    max_response_chars: int
    timed_out: bool = False


@dataclass(frozen=True)
class _IsolatedCommandResult:
    """Capture one command result after process-group timeout handling."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class _ExpertProcessIdentity:
    """Identify one token-bearing advisor process at a point in time."""

    pid: int
    ppid: int
    pgid: int


@dataclass
class _ActiveExpertCommand:
    """Track one advisor until its owner thread proves process-tree cleanup."""

    process: subprocess.Popen[str]
    process_token: str
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


_ACTIVE_EXPERT_COMMANDS_LOCK = threading.Lock()
_ACTIVE_EXPERT_COMMANDS: dict[int, _ActiveExpertCommand] = {}
_EXPERT_SHUTDOWN_GENERATION = 0


def normalize_expert_provider(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    return COMMAND_PROVIDER_ALIASES.get(normalized, normalized)


def is_command_expert_provider(value: str) -> bool:
    normalized = normalize_expert_provider(value)
    if (
        normalized == "codex"
        and str(os.getenv("LEANFLOW_SANDBOX", "") or "").strip().lower()
        in {"1", "true", "yes", "on"}
        and shutil.which("codex") is None
    ):
        # The sandbox intentionally carries Codex OAuth credentials but not
        # the desktop Codex executable. Let model-backed advisor callers route
        # the same explicit provider through the Responses API adapter.
        return False
    return normalized in set(DEFAULT_COMMAND_TEMPLATES)


def _fallback_task(task: str) -> str:
    return TASK_FALLBACKS.get(str(task or "").strip(), "")


def resolve_expert_provider(task: str = "lean_reasoning", explicit: str | None = None) -> str:
    if explicit and str(explicit).strip():
        return normalize_expert_provider(str(explicit))

    env_provider = _read_task_env(task, "PROVIDER")
    if env_provider:
        return normalize_expert_provider(env_provider)

    task_config = _task_config(task)
    cfg_provider = str(task_config.get("provider", "") or "").strip()
    if cfg_provider:
        return normalize_expert_provider(cfg_provider)

    fallback_task = _fallback_task(task)
    if fallback_task:
        fallback_env_provider = _read_task_env(fallback_task, "PROVIDER")
        if fallback_env_provider:
            return normalize_expert_provider(fallback_env_provider)
        fallback_config = _task_config(fallback_task)
        fallback_cfg_provider = str(fallback_config.get("provider", "") or "").strip()
        if fallback_cfg_provider:
            return normalize_expert_provider(fallback_cfg_provider)
    return "auto"


def _task_config(task: str) -> dict[str, Any]:
    try:
        config = load_config()
    except Exception:
        return {}
    aux = config.get("auxiliary", {}) if isinstance(config, Mapping) else {}
    task_config = aux.get(task, {}) if isinstance(aux, Mapping) else {}
    return dict(task_config) if isinstance(task_config, Mapping) else {}


def _read_task_env(task: str, suffix: str) -> str:
    task_key = str(task or "").strip().upper()
    if not task_key:
        return ""
    name = f"AUXILIARY_{task_key}_{suffix}"
    return str(os.getenv(name, "") or get_env_value(name, "") or "").strip()


def _provider_env_name(provider: str) -> str:
    return normalize_expert_provider(provider).replace("-", "_").upper()


def resolve_expert_command_template(provider: str, task: str = "lean_reasoning") -> str:
    """Resolve a command template for executing expert help via a specified provider. Checks task-specific environment variables and config first, then provider environment variables, falling back to task fallbacks and finally DEFAULT_COMMAND_TEMPLATES."""
    provider = normalize_expert_provider(provider)
    task_template = _read_task_env(task, "COMMAND_TEMPLATE")
    if task_template:
        return task_template

    provider_env = f"LEANFLOW_EXPERT_{_provider_env_name(provider)}_COMMAND_TEMPLATE"
    provider_template = str(
        os.getenv(provider_env, "") or get_env_value(provider_env, "") or ""
    ).strip()
    if provider_template:
        return provider_template

    task_config = _task_config(task)
    generic_template = str(task_config.get("command_template", "") or "").strip()
    provider_template_key = f"{provider.replace('-', '_')}_command_template"
    specific_template = str(task_config.get(provider_template_key, "") or "").strip()
    if specific_template or generic_template:
        return specific_template or generic_template

    fallback_task = _fallback_task(task)
    if fallback_task:
        fallback_task_template = _read_task_env(fallback_task, "COMMAND_TEMPLATE")
        if fallback_task_template:
            return fallback_task_template
        fallback_config = _task_config(fallback_task)
        fallback_generic_template = str(fallback_config.get("command_template", "") or "").strip()
        fallback_specific_template = str(
            fallback_config.get(provider_template_key, "") or ""
        ).strip()
        if fallback_specific_template or fallback_generic_template:
            return fallback_specific_template or fallback_generic_template

    return DEFAULT_COMMAND_TEMPLATES[provider]


def _resolve_command_task_setting(task: str, suffix: str) -> str:
    """Return one task or fallback setting for a command-backed advisor."""
    env_value = _read_task_env(task, suffix)
    if env_value:
        return env_value
    config_key = suffix.lower()
    task_value = str(_task_config(task).get(config_key, "") or "").strip()
    if task_value:
        return task_value
    fallback_task = _fallback_task(task)
    if not fallback_task:
        return ""
    fallback_env_value = _read_task_env(fallback_task, suffix)
    if fallback_env_value:
        return fallback_env_value
    return str(_task_config(fallback_task).get(config_key, "") or "").strip()


def _apply_command_runtime_overrides(
    command: list[str],
    *,
    provider: str,
    task: str,
) -> list[str]:
    """Bind a Codex command advisor to the workflow model and effort.

    Explicit ``--provider codex`` launches reassert the selected runtime into
    task-specific auxiliary environment variables. Command-backed advisors
    must consume those values too; otherwise ``codex exec`` silently falls
    back to the desktop config and can run a different model or effort than
    every model-backed lane.
    """
    if (
        normalize_expert_provider(provider) != "codex"
        or len(command) < 2
        or Path(command[0]).name != "codex"
        or command[1] != "exec"
    ):
        return command
    enriched = list(command)
    insert_at = 2 if len(enriched) >= 2 and enriched[1] == "exec" else 1
    model = _resolve_command_task_setting(task, "MODEL")
    has_model = any(
        token in {"-m", "--model"} or token.startswith("--model=") for token in enriched
    )
    if model and not has_model:
        enriched[insert_at:insert_at] = ["--model", model]
        insert_at += 2
    reasoning_effort = _resolve_command_task_setting(task, "REASONING_EFFORT")
    has_reasoning_effort = any("model_reasoning_effort" in token for token in enriched)
    if reasoning_effort and not has_reasoning_effort:
        enriched[insert_at:insert_at] = [
            "--config",
            f'model_reasoning_effort="{reasoning_effort}"',
        ]
    return enriched


def _max_response_chars() -> int:
    raw = str(
        os.getenv("LEANFLOW_EXPERT_MAX_RESPONSE_CHARS", "")
        or get_env_value("LEANFLOW_EXPERT_MAX_RESPONSE_CHARS", "")
        or ""
    ).strip()
    if raw:
        try:
            return max(1000, int(raw))
        except ValueError:
            pass
    return 64000


def _render_command_token(token: str, values: Mapping[str, str]) -> str:
    rendered = token
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def _build_command(template: str, values: Mapping[str, str]) -> list[str]:
    try:
        tokens = shlex.split(template)
    except ValueError as exc:
        raise RuntimeError(f"invalid expert command template: {exc}") from exc
    command = [_render_command_token(token, values) for token in tokens]
    if not command:
        raise RuntimeError("expert command template produced an empty command")
    return command


def _subprocess_text(value: Any) -> str:
    """Return captured subprocess output as text across Python timeout variants."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _valid_descendant_pid(process_id: int) -> bool:
    """Return whether a PID is safe to target as an advisor descendant."""
    return process_id > 1 and process_id != os.getpid()


def _parse_tagged_processes(output: str, process_token: str) -> list[_ExpertProcessIdentity]:
    """Parse only token-bearing process identities without retaining command text."""
    if not process_token:
        return []
    token_entry = f"{_EXPERT_PROCESS_TOKEN_ENV}={process_token}"
    tagged: list[_ExpertProcessIdentity] = []
    for line in output.splitlines():
        fields = line.lstrip().split(maxsplit=3)
        if len(fields) != 4 or token_entry not in fields[3]:
            continue
        try:
            process_id, parent_id, process_group_id = (int(field) for field in fields[:3])
        except ValueError:
            continue
        if not _valid_descendant_pid(process_id) or parent_id < 0 or process_group_id <= 0:
            continue
        tagged.append(
            _ExpertProcessIdentity(
                pid=process_id,
                ppid=parent_id,
                pgid=process_group_id,
            )
        )

    by_pid = {identity.pid: identity for identity in tagged}

    def depth(identity: _ExpertProcessIdentity) -> int:
        current = identity
        visited = {identity.pid}
        result = 0
        while current.ppid in by_pid and current.ppid not in visited:
            visited.add(current.ppid)
            current = by_pid[current.ppid]
            result += 1
        return result

    tagged.sort(key=lambda identity: (depth(identity), identity.pid), reverse=True)
    return tagged


def _snapshot_tagged_expert_processes(
    process_token: str,
) -> list[_ExpertProcessIdentity]:
    """Return token-bearing advisor processes ordered deepest-first on POSIX."""
    if os.name == "nt" or not process_token:
        return []
    try:
        completed = subprocess.run(
            ["ps", "e", "-ww", "-axo", "pid=,ppid=,pgid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return _parse_tagged_processes(completed.stdout, process_token)


def _process_identity_still_tagged(
    identity: _ExpertProcessIdentity,
    process_token: str,
) -> bool:
    """Revalidate a PID's unique advisor token immediately before signaling."""
    if os.name == "nt" or not _valid_descendant_pid(identity.pid) or not process_token:
        return False
    try:
        completed = subprocess.run(
            [
                "ps",
                "e",
                "-ww",
                "-p",
                str(identity.pid),
                "-o",
                "pid=,ppid=,pgid=,command=",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(
        current.pid == identity.pid and current.pgid == identity.pgid
        for current in _parse_tagged_processes(completed.stdout, process_token)
    )


def _signal_tagged_process(
    identity: _ExpertProcessIdentity,
    process_token: str,
    sig: signal.Signals,
) -> None:
    """Signal one advisor PID only after its unique token is revalidated."""
    if not _process_identity_still_tagged(identity, process_token):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(identity.pid, sig)


def _signal_tagged_process_group(
    process_group_id: int,
    process_token: str,
    sig: signal.Signals,
) -> None:
    """Signal an advisor group only while a tagged member still belongs to it."""
    if process_group_id <= 1 or process_group_id == os.getpgrp() or not hasattr(os, "killpg"):
        return
    identities = _snapshot_tagged_expert_processes(process_token)
    if not any(
        identity.pgid == process_group_id
        and _process_identity_still_tagged(identity, process_token)
        for identity in identities
    ):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process_group_id, sig)


def _close_expert_process_pipes(process: subprocess.Popen[str]) -> None:
    """Close retained advisor pipes after the leader has been reaped."""
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def _terminate_expert_process_tree(
    process: subprocess.Popen[str],
    *,
    process_token: str,
    grace_s: float = 0.5,
) -> None:
    """Terminate and reap an advisor tree, including detached POSIX groups."""
    if os.name == "nt" or not hasattr(os, "killpg"):
        # Full Windows tree cleanup requires a Job Object. Keep the fallback
        # bounded to the direct child instead of pretending it is recursive.
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=max(0.0, grace_s))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        return

    process_group_id = int(process.pid or 0)
    # The live Popen object is stronger authority than a best-effort process
    # inventory: this exact child was launched in a new session, so its PID is
    # also the owned process-group id until the leader exits. Signal it first;
    # restricted hosts may deny the `ps e` token scan used below for detached
    # descendants, and waiting five seconds before killing the known child can
    # otherwise outlive the native runner's bounded shutdown gate.
    if process.poll() is None and process_group_id > 1 and process_group_id != os.getpgrp():
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process_group_id, signal.SIGTERM)
    identities = _snapshot_tagged_expert_processes(process_token)
    for identity in identities:
        _signal_tagged_process(identity, process_token, signal.SIGTERM)
    _signal_tagged_process_group(
        process_group_id,
        process_token,
        signal.SIGTERM,
    )

    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        if not _snapshot_tagged_expert_processes(process_token):
            break
        time.sleep(0.01)

    survivors = _snapshot_tagged_expert_processes(process_token)
    for identity in survivors:
        _signal_tagged_process(identity, process_token, signal.SIGKILL)
    _signal_tagged_process_group(
        process_group_id,
        process_token,
        signal.SIGKILL,
    )

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def _drain_terminated_expert_process(
    process: subprocess.Popen[str],
    *,
    partial_stdout: str = "",
    partial_stderr: str = "",
) -> tuple[str, str]:
    """Drain terminated advisor pipes without allowing an escaped holder to block."""
    try:
        # Detached descendants may inherit the leader's pipes. The leader has
        # already been terminated/reaped, so a long drain cannot add process
        # authority and only delays native shutdown; retain partial output and
        # close the pipes after one ordinary communication poll.
        stdout, stderr = process.communicate(timeout=_EXPERT_COMMUNICATE_POLL_S)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _close_expert_process_pipes(process)
        return partial_stdout, partial_stderr
    return (
        _subprocess_text(stdout) or partial_stdout,
        _subprocess_text(stderr) or partial_stderr,
    )


def _expert_shutdown_generation() -> int:
    """Return the current process-owner shutdown generation."""
    with _ACTIVE_EXPERT_COMMANDS_LOCK:
        return _EXPERT_SHUTDOWN_GENERATION


def _register_active_expert_command(
    active: _ActiveExpertCommand,
    *,
    launch_generation: int,
) -> None:
    """Register one command and cancel it if shutdown crossed its launch."""
    with _ACTIVE_EXPERT_COMMANDS_LOCK:
        _ACTIVE_EXPERT_COMMANDS[id(active)] = active
        if launch_generation != _EXPERT_SHUTDOWN_GENERATION:
            active.cancel_requested.set()


def _unregister_active_expert_command(active: _ActiveExpertCommand) -> None:
    """Publish owner-thread completion and retire one active command."""
    with _ACTIVE_EXPERT_COMMANDS_LOCK:
        _ACTIVE_EXPERT_COMMANDS.pop(id(active), None)
        active.finished.set()


def shutdown_active_expert_commands(
    *,
    timeout_s: float = _EXPERT_SHUTDOWN_WAIT_S,
) -> tuple[int, ...]:
    """Cancel active advisors and return PIDs whose owner threads did not finish."""
    global _EXPERT_SHUTDOWN_GENERATION

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    with _ACTIVE_EXPERT_COMMANDS_LOCK:
        _EXPERT_SHUTDOWN_GENERATION += 1

    while True:
        with _ACTIVE_EXPERT_COMMANDS_LOCK:
            active = tuple(_ACTIVE_EXPERT_COMMANDS.values())
            for command in active:
                command.cancel_requested.set()
        if not active:
            return ()

        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        # Every owner polls at 100 ms; short waits also admit commands that
        # crossed Popen while this shutdown generation was being published.
        active[0].finished.wait(timeout=min(_EXPERT_COMMUNICATE_POLL_S, remaining))

    with _ACTIVE_EXPERT_COMMANDS_LOCK:
        residual = tuple(_ACTIVE_EXPERT_COMMANDS.values())
        for command in residual:
            command.cancel_requested.set()
    return tuple(
        sorted(
            {
                int(command.process.pid or 0)
                for command in residual
                if int(command.process.pid or 0) > 1
            }
        )
    )


def _run_isolated_expert_command(
    command: list[str],
    *,
    input: str,
    cwd: str,
    timeout: int,
) -> _IsolatedCommandResult:
    """Run one advisor and clean its full process tree on every abnormal exit."""
    if is_interrupted():
        raise InterruptedError("expert command interrupted before launch")

    launch_generation = _expert_shutdown_generation()
    process_token = secrets.token_urlsafe(32)
    environment = dict(os.environ)
    environment[_EXPERT_PROCESS_TOKEN_ENV] = process_token
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
        env=environment,
    )
    active = _ActiveExpertCommand(process=process, process_token=process_token)
    _register_active_expert_command(active, launch_generation=launch_generation)
    effective_timeout_s = max(1, int(timeout or 0))
    started = time.monotonic()
    deadline = started + effective_timeout_s
    communicate_input: str | None = input
    partial_stdout = ""
    partial_stderr = ""
    next_heartbeat_at = time.monotonic() + _EXPERT_HEARTBEAT_INTERVAL_S
    try:
        while True:
            if active.cancel_requested.is_set() or is_interrupted():
                raise InterruptedError("expert command interrupted")
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                _terminate_expert_process_tree(process, process_token=process_token)
                stdout, stderr = _drain_terminated_expert_process(
                    process,
                    partial_stdout=partial_stdout,
                    partial_stderr=partial_stderr,
                )
                return _IsolatedCommandResult(
                    returncode=None,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=True,
                )
            try:
                stdout, stderr = process.communicate(
                    input=communicate_input,
                    timeout=min(_EXPERT_COMMUNICATE_POLL_S, remaining),
                )
            except subprocess.TimeoutExpired as exc:
                communicate_input = None
                partial_stdout = _subprocess_text(exc.stdout) or partial_stdout
                partial_stderr = _subprocess_text(exc.stderr) or partial_stderr
                now = time.monotonic()
                if now >= next_heartbeat_at:
                    with contextlib.suppress(Exception):
                        touch_workflow_runtime_heartbeat()
                    record_expert_help_activity(
                        "expert-help-heartbeat",
                        "Expert help command remains active",
                        mode="command",
                        elapsed_s=round(max(0.0, now - started), 1),
                        timeout_s=effective_timeout_s,
                        partial_response_available=False,
                    )
                    next_heartbeat_at = now + _EXPERT_HEARTBEAT_INTERVAL_S
                continue

            # A signal can race the final pipe drain. Honor it and sweep any
            # token-bearing descendant even though the command leader exited.
            if active.cancel_requested.is_set() or is_interrupted():
                raise InterruptedError("expert command interrupted")
            return _IsolatedCommandResult(
                returncode=process.returncode,
                stdout=_subprocess_text(stdout),
                stderr=_subprocess_text(stderr),
                timed_out=False,
            )
    except BaseException:
        _terminate_expert_process_tree(process, process_token=process_token)
        _drain_terminated_expert_process(
            process,
            partial_stdout=partial_stdout,
            partial_stderr=partial_stderr,
        )
        raise
    finally:
        _unregister_active_expert_command(active)


def run_command_expert_help(
    *,
    provider: str,
    prompt: str,
    task: str = "lean_reasoning",
    cwd: str = "",
    timeout_s: int = 1200,
) -> ExpertCommandResult:
    """Execute an expert help command via subprocess, streaming prompt through stdin or temp file. Manages timeout, truncates response to max_response_chars, records activity to workflow log, and returns result with exit status, stderr, and truncation state."""
    provider = normalize_expert_provider(provider)
    if provider not in DEFAULT_COMMAND_TEMPLATES:
        raise RuntimeError(f"{provider!r} is not a command expert provider")

    prompt = str(prompt or "")
    workdir = str(Path(cwd or os.getcwd()).expanduser().resolve())
    template = resolve_expert_command_template(provider, task)
    max_chars = _max_response_chars()
    prompt_file_path = ""
    output_file_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", prefix="leanflow-expert-", suffix=".md", delete=False
        ) as handle:
            handle.write(prompt)
            prompt_file_path = handle.name
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", prefix="leanflow-expert-output-", suffix=".txt", delete=False
        ) as handle:
            output_file_path = handle.name
        values = {
            "cwd": workdir,
            "prompt": prompt,
            "prompt_file": prompt_file_path,
            "output_file": output_file_path,
            "provider": provider,
        }
        command = _build_command(template, values)
        command = _apply_command_runtime_overrides(
            command,
            provider=provider,
            task=task,
        )
        record_expert_help_activity(
            "expert-help-request",
            "Expert help command started",
            provider=provider,
            mode="command",
            prompt=prompt,
            command=command,
            cwd=workdir,
            timeout_s=timeout_s,
        )
        completed = _run_isolated_expert_command(
            command,
            input=prompt,
            cwd=workdir,
            timeout=max(1, int(timeout_s or 0)),
        )
        if bool(getattr(completed, "timed_out", False)):
            stdout = str(completed.stdout or "").strip()
            stderr = str(completed.stderr or "").strip()
            truncated = len(stdout) > max_chars
            response = stdout[:max_chars].rstrip() if truncated else stdout
            result = ExpertCommandResult(
                provider=provider,
                command=command,
                exit_status=None,
                response=response,
                stderr=stderr,
                truncated=truncated,
                response_chars=len(stdout),
                max_response_chars=max_chars,
                timed_out=True,
            )
            record_expert_help_activity(
                "expert-help-result",
                "Expert help command timed out",
                provider=provider,
                mode="command",
                prompt=prompt,
                command=result.command,
                exit_status=None,
                response=result.response,
                stderr=result.stderr,
                truncated=result.truncated,
                response_chars=result.response_chars,
                max_response_chars=result.max_response_chars,
                timed_out=True,
            )
            return result
        response = str(completed.stdout or "").strip()
        try:
            output_file_response = Path(output_file_path).read_text(encoding="utf-8").strip()
        except OSError:
            output_file_response = ""
        if output_file_response:
            response = output_file_response
        stderr = str(completed.stderr or "").strip()
        response_chars = len(response)
        truncated = response_chars > max_chars
        if truncated:
            response = response[:max_chars].rstrip()
        result = ExpertCommandResult(
            provider=provider,
            command=command,
            exit_status=completed.returncode,
            response=response,
            stderr=stderr,
            truncated=truncated,
            response_chars=response_chars,
            max_response_chars=max_chars,
            timed_out=False,
        )
        record_expert_help_activity(
            "expert-help-result",
            "Expert help command finished",
            provider=provider,
            mode="command",
            prompt=prompt,
            command=command,
            exit_status=result.exit_status,
            response=result.response,
            stderr=result.stderr,
            truncated=result.truncated,
            response_chars=result.response_chars,
            max_response_chars=result.max_response_chars,
            timed_out=False,
        )
        return result
    finally:
        if prompt_file_path:
            with contextlib.suppress(OSError):
                os.unlink(prompt_file_path)
        if output_file_path:
            with contextlib.suppress(OSError):
                os.unlink(output_file_path)


def record_expert_help_activity(event_type: str, message: str, **details: Any) -> None:
    """Persist expert activity and expose command heartbeats in the primary console log."""
    with contextlib.suppress(Exception):
        append_workflow_activity(event_type, message, **details)
    if event_type != "expert-help-heartbeat":
        return
    elapsed_s = float(details.get("elapsed_s", 0.0) or 0.0)
    timeout_s = float(details.get("timeout_s", 0.0) or 0.0)
    mode = str(details.get("mode", "") or "expert")
    heartbeat = (
        f"   ⏳ {message} ({elapsed_s:.0f}s elapsed, " f"{timeout_s:.0f}s timeout, {mode})\n"
    )
    with contextlib.suppress(Exception):
        append_workflow_run_log(heartbeat)
    stream = getattr(sys, "__stdout__", None)
    if stream is not None:
        with contextlib.suppress(Exception):
            stream.write(heartbeat)
            stream.flush()
