"""Expert-help provider dispatch for EPFLemma advisory workflows."""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from epflemma_cli.config import get_env_value, load_config
from epflemma_cli.workflows.workflow_state import append_workflow_activity

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

TASK_FALLBACKS = {
    "lean_decompose_helpers": "lean_reasoning",
}


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


def normalize_expert_provider(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    return COMMAND_PROVIDER_ALIASES.get(normalized, normalized)


def is_command_expert_provider(value: str) -> bool:
    return normalize_expert_provider(value) in set(DEFAULT_COMMAND_TEMPLATES)


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
    provider = normalize_expert_provider(provider)
    task_template = _read_task_env(task, "COMMAND_TEMPLATE")
    if task_template:
        return task_template

    provider_env = f"EPFLEMMA_EXPERT_{_provider_env_name(provider)}_COMMAND_TEMPLATE"
    provider_template = str(os.getenv(provider_env, "") or get_env_value(provider_env, "") or "").strip()
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
        fallback_specific_template = str(fallback_config.get(provider_template_key, "") or "").strip()
        if fallback_specific_template or fallback_generic_template:
            return fallback_specific_template or fallback_generic_template

    return DEFAULT_COMMAND_TEMPLATES[provider]


def _max_response_chars() -> int:
    raw = str(os.getenv("EPFLEMMA_EXPERT_MAX_RESPONSE_CHARS", "") or get_env_value("EPFLEMMA_EXPERT_MAX_RESPONSE_CHARS", "") or "").strip()
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


def run_command_expert_help(
    *,
    provider: str,
    prompt: str,
    task: str = "lean_reasoning",
    cwd: str = "",
    timeout_s: int = 1200,
) -> ExpertCommandResult:
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
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="epflemma-expert-", suffix=".md", delete=False) as handle:
            handle.write(prompt)
            prompt_file_path = handle.name
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="epflemma-expert-output-", suffix=".txt", delete=False) as handle:
            output_file_path = handle.name
        values = {
            "cwd": workdir,
            "prompt": prompt,
            "prompt_file": prompt_file_path,
            "output_file": output_file_path,
            "provider": provider,
        }
        command = _build_command(template, values)
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
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=workdir,
            timeout=max(1, int(timeout_s or 0)),
            check=False,
        )
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
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "").strip()
        stderr = str(exc.stderr or "").strip()
        truncated = len(stdout) > max_chars
        response = stdout[:max_chars].rstrip() if truncated else stdout
        result = ExpertCommandResult(
            provider=provider,
            command=getattr(exc, "cmd", []) if isinstance(getattr(exc, "cmd", []), list) else [],
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
    finally:
        if prompt_file_path:
            with contextlib.suppress(OSError):
                os.unlink(prompt_file_path)
        if output_file_path:
            with contextlib.suppress(OSError):
                os.unlink(output_file_path)


def record_expert_help_activity(event_type: str, message: str, **details: Any) -> None:
    with contextlib.suppress(Exception):
        append_workflow_activity(event_type, message, **details)
