"""LeanFlow config and environment management."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from core.filesystem import ensure_directory
from core.home import DEFAULT_HOME, HOME_ENV, leanflow_home

logger = logging.getLogger(__name__)

# Home resolution lives in core.home (the low layer); these aliases preserve the public names.
LEANFLOW_HOME_ENV = HOME_ENV
LEANFLOW_HOME_DEFAULT = DEFAULT_HOME
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Legacy config carried a large registry of optional env vars for removed
# gateway/browser/voice surfaces. The Lean-first kernel no longer needs that
# catalog, but terminal env sanitization still expects this mapping to exist.
OPTIONAL_ENV_VARS: dict[str, dict[str, Any]] = {}


DEFAULT_CONFIG: dict[str, Any] = {
    "leanflow": {
        "project": {
            "template_source": "",
        },
        "workflow": {
            "managed_state_dir": "",
            "autonomous_followups": 6,
        },
        "sandbox": {
            "engine": "auto",
            "image": "leanflow/sandbox:local",
            "env_file": "",
            "cache_dir": "",
            "runs_dir": "",
            "network": True,
            "read_only_root": True,
            "bootstrap_mcp": True,
        },
    },
    "model": {
        "default": "moonshotai/Kimi-K2.7-Code",
        "provider": "auto",
        "base_url": "",
        "api_key": "",
        # Optional light tier for dispatched prover jobs; empty uses the
        # run's main model.
        "prover_light": "",
    },
    "auxiliary": {
        "lean_reasoning": {
            "provider": "main",
            "model": "moonshotai/Kimi-K2.6-int4",
            "reasoning_effort": "high",
            "base_url": "",
            "api_key": "",
            "command_template": "",
            "codex_command_template": "",
            "claude_code_command_template": "",
        },
        "lean_decompose_helpers": {
            "provider": "",
            "model": "",
            "reasoning_effort": "",
            "base_url": "",
            "api_key": "",
            "command_template": "",
            "codex_command_template": "",
            "claude_code_command_template": "",
        },
        "manager_nudge": {
            "provider": "auto",
            "model": "",
            "reasoning_effort": "low",
            "base_url": "",
            "api_key": "",
            "command_template": "",
            "codex_command_template": "",
            "claude_code_command_template": "",
        },
        "orchestration": {
            "provider": "main",
            "model": "",
            "reasoning_effort": "off",
            "base_url": "",
            "api_key": "",
            "command_template": "",
            "codex_command_template": "",
            "claude_code_command_template": "",
        },
        "planner_synthesis": {
            "provider": "",
            "model": "",
            "reasoning_effort": "",
            "base_url": "",
            "api_key": "",
            "command_template": "",
            "codex_command_template": "",
            "claude_code_command_template": "",
        },
        "blueprint_verification": {
            "provider": "main",
            "model": "",
            "reasoning_effort": "",
            "base_url": "",
            "api_key": "",
            "command_template": "",
            "codex_command_template": "",
            "claude_code_command_template": "",
        },
        "autoformalizer_verification": {
            "provider": "local",
            "model": "",
            "reasoning_effort": "",
            "base_url": "",
            "api_key": "",
            "command_template": "",
            "codex_command_template": "",
            "claude_code_command_template": "",
        },
    },
    "toolsets": ["leanflow-cli"],
    "agent": {
        "max_turns": 200,
        "reasoning_effort": "auto",
        "seed": 42,
        "temperature": 0.3,
        "top_p": None,
        "top_k": None,
        "min_p": None,
    },
    "logging": {
        "preview_lines": 8,
        "preview_chars": 1600,
        "tool_output_head_lines": 28,
        "tool_output_tail_lines": 12,
        "activity_preview_chars": 420,
    },
    "compression": {
        "enabled": True,
        "threshold": 0.75,
        "summary_model": "moonshotai/Kimi-K2.6",
        "reserved_output_tokens": 20000,
        "prune_tool_output": True,
        "prune_keep_recent_user_turns": 2,
    },
    "local_models": {
        "default_runtime": "vllm",
        "active_runtime": "",
        "active_model": "",
        "runtimes": {
            "vllm": {
                "host": "127.0.0.1",
                "port": 8000,
                "extra_args": [],
            },
            "ollama": {
                "host": "127.0.0.1",
                "port": 11434,
                "extra_args": [],
            },
            "llama_cpp": {
                "host": "127.0.0.1",
                "port": 8080,
                "extra_args": [],
            },
        },
    },
    "custom_providers": [],
}

DEFAULT_CONFIG_HEADER = """# LeanFlow configuration
#
# Main workflow model:
#   model.default is the primary agent model used for prove/autoprove/formalize.
#   The installation default is moonshotai/Kimi-K2.7-Code.
#
# Auxiliary theorem advisor:
#   auxiliary.lean_reasoning is used by the lean_reasoning_help tool when the
#   primary model is stuck on a hard theorem. By default it uses the same
#   endpoint/API key as the main model (`provider: main`) but asks
#   moonshotai/Kimi-K2.6-int4 with high reasoning effort when the endpoint
#   supports explicit reasoning controls.
#
# Auxiliary helper decomposer:
#   auxiliary.lean_decompose_helpers is used by the lean_decompose_helpers tool
#   when the useful next step is splitting a hard theorem into helper lemmas.
#   Empty values inherit auxiliary.lean_reasoning, so you can leave this blank
#   until you want a separate decomposition-planner model/provider.
#
# Persistence coach:
#   auxiliary.manager_nudge supplies the short encouragement message after a
#   rejected prover turn. The default auto route selects an available auxiliary
#   provider, while low reasoning effort keeps this message-only call fast.
#
# Orchestrator routing turn:
#   auxiliary.orchestration is used by the LLM routing layer over the
#   deterministic orchestrator floor. The default (`provider: main`, empty
#   model) means the strong main-agent model decides routing; set
#   auxiliary.orchestration.model to pin a different one. Its strict JSON turn
#   disables hidden RCP reasoning by default so a thinking model cannot spend
#   its output budget before replying; set reasoning_effort to low/medium/high
#   to opt back in.
#   auxiliary.planner_synthesis is the planner synthesizer turn. Empty values
#   inherit auxiliary.orchestration — i.e. the strong main-agent model by default.
#   Its strict JSON turn also defaults to non-thinking mode; set a planner-
#   specific or orchestration reasoning_effort to opt back in.
#
# Formalization verifiers:
#   auxiliary.blueprint_verification controls the independent statement/source
#   review pass for document formalization blueprints. `main` keeps the existing
#   managed reviewer-agent behavior; codex / claude-code run command reviewers;
#   other auxiliary model providers produce advisory review reports.
#   auxiliary.autoformalizer_verification controls advisory review around the
#   autoformalization handoff verifier. Its default `local` setting uses the
#   deterministic local blueprint/Lean checks only. Non-local providers can
#   review or propose corrections, but cannot override Lean/local verification.
#
# Common model changes:
#   - Change the primary model: model.default
#   - Change the primary provider: model.provider
#   - Use your Codex CLI login as the primary provider:
#     model.provider=codex, with optional LEANFLOW_CODEX_MODEL override
#   - Change a custom OpenAI-compatible endpoint: model.base_url or
#     LEANFLOW_OPENAI_BASE_URL in ~/.leanflow/.env
#   - Override a model context window when provider metadata is missing or
#     wrong: model.context_lengths.<model-id>
#   - Change the theorem advisor model: auxiliary.lean_reasoning.model
#   - Change the theorem advisor reasoning budget:
#     auxiliary.lean_reasoning.reasoning_effort
#   - Use a separate helper-decomposition planner:
#     auxiliary.lean_decompose_helpers.model and
#     auxiliary.lean_decompose_helpers.reasoning_effort
#   - Use a separate persistence coach model:
#     auxiliary.manager_nudge.model and auxiliary.manager_nudge.reasoning_effort
#   - Use separate formalization verifiers:
#     auxiliary.blueprint_verification.provider and
#     auxiliary.autoformalizer_verification.provider
#   - Use a separate theorem advisor endpoint: set
#     auxiliary.lean_reasoning.base_url and auxiliary.lean_reasoning.api_key,
#     or AUXILIARY_LEAN_REASONING_BASE_URL / AUXILIARY_LEAN_REASONING_API_KEY
#     in ~/.leanflow/.env
#   - Use an opt-in command advisor: pass --expert-provider codex or
#     --expert-provider claude-code on a workflow, or set
#     auxiliary.lean_reasoning.provider to codex / claude-code. Override the
#     command with auxiliary.lean_reasoning.command_template or
#     AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE. Commands receive the full
#     advisor prompt on stdin and run without a shell.
#
# Workflow/runtime tuning:
#   - agent.max_turns controls the managed workflow API-step budget.
#   - logging.preview_lines and logging.preview_chars control how much
#     prompt, assistant, and reasoning context appears in run logs.
#   - leanflow.sandbox controls the optional container sandbox runtime.
#
"""

DEFAULT_ENV_TEMPLATE = """# LeanFlow environment overrides
#
# Leave values empty until you want to configure a provider. Values in this
# file are loaded by the LeanFlow CLI and can be overridden by process env vars.

# OpenAI-compatible primary endpoint, including EPFL RCP-style endpoints.
LEANFLOW_OPENAI_BASE_URL=
LEANFLOW_OPENAI_API_KEY=

# OpenRouter fallback/primary endpoint.
OPENROUTER_API_KEY=

# Direct provider keys.
KIMI_API_KEY=
GLM_API_KEY=
ZAI_API_KEY=
ANTHROPIC_API_KEY=
DEEPSEEK_API_KEY=
MINIMAX_API_KEY=

# Optional Codex primary overrides. With model.provider=codex, LeanFlow reads
# the existing Codex CLI login and model settings from ~/.codex.
LEANFLOW_CODEX_MODEL=
LEANFLOW_CODEX_REASONING_EFFORT=

# Optional per-task theorem-advisor overrides for lean_reasoning_help.
AUXILIARY_LEAN_REASONING_PROVIDER=
AUXILIARY_LEAN_REASONING_MODEL=
AUXILIARY_LEAN_REASONING_REASONING_EFFORT=
AUXILIARY_LEAN_REASONING_BASE_URL=
AUXILIARY_LEAN_REASONING_API_KEY=
AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE=

# Optional per-task overrides for lean_decompose_helpers. Leave empty to
# inherit auxiliary.lean_reasoning / AUXILIARY_LEAN_REASONING_*.
AUXILIARY_LEAN_DECOMPOSE_HELPERS_PROVIDER=
AUXILIARY_LEAN_DECOMPOSE_HELPERS_MODEL=
AUXILIARY_LEAN_DECOMPOSE_HELPERS_REASONING_EFFORT=
AUXILIARY_LEAN_DECOMPOSE_HELPERS_BASE_URL=
AUXILIARY_LEAN_DECOMPOSE_HELPERS_API_KEY=
AUXILIARY_LEAN_DECOMPOSE_HELPERS_COMMAND_TEMPLATE=

# Optional persistence-coach overrides. The default provider is auto and the
# default reasoning effort is low.
AUXILIARY_MANAGER_NUDGE_PROVIDER=
AUXILIARY_MANAGER_NUDGE_MODEL=
AUXILIARY_MANAGER_NUDGE_REASONING_EFFORT=
AUXILIARY_MANAGER_NUDGE_BASE_URL=
AUXILIARY_MANAGER_NUDGE_API_KEY=
AUXILIARY_MANAGER_NUDGE_COMMAND_TEMPLATE=

# Optional orchestrator routing overrides. Reasoning defaults to off because
# this turn returns a small strict JSON decision. The advisory turn defaults
# to 75 seconds outside research mode. Research mode uses a target-scoped
# 12,000-character digest and caps the isolated foreground consult at 20
# seconds; LEANFLOW_ORCHESTRATOR_LLM_TIMEOUT_S may lower that ceiling.
AUXILIARY_ORCHESTRATION_PROVIDER=
AUXILIARY_ORCHESTRATION_MODEL=
AUXILIARY_ORCHESTRATION_REASONING_EFFORT=
AUXILIARY_ORCHESTRATION_BASE_URL=
AUXILIARY_ORCHESTRATION_API_KEY=
AUXILIARY_ORCHESTRATION_COMMAND_TEMPLATE=
LEANFLOW_ORCHESTRATOR_LLM_TIMEOUT_S=

# Optional formalization verifier overrides. Providers use the same names as
# expert help: main/auto/openrouter/custom for model-backed review, codex or
# claude-code for command review, and local for deterministic local checks.
AUXILIARY_BLUEPRINT_VERIFICATION_PROVIDER=
AUXILIARY_BLUEPRINT_VERIFICATION_MODEL=
AUXILIARY_BLUEPRINT_VERIFICATION_REASONING_EFFORT=
AUXILIARY_BLUEPRINT_VERIFICATION_BASE_URL=
AUXILIARY_BLUEPRINT_VERIFICATION_API_KEY=
AUXILIARY_BLUEPRINT_VERIFICATION_COMMAND_TEMPLATE=

AUXILIARY_AUTOFORMALIZER_VERIFICATION_PROVIDER=
AUXILIARY_AUTOFORMALIZER_VERIFICATION_MODEL=
AUXILIARY_AUTOFORMALIZER_VERIFICATION_REASONING_EFFORT=
AUXILIARY_AUTOFORMALIZER_VERIFICATION_BASE_URL=
AUXILIARY_AUTOFORMALIZER_VERIFICATION_API_KEY=
AUXILIARY_AUTOFORMALIZER_VERIFICATION_COMMAND_TEMPLATE=

LEANFLOW_EXPERT_CODEX_COMMAND_TEMPLATE=
LEANFLOW_EXPERT_CLAUDE_CODE_COMMAND_TEMPLATE=

# Escape hatch for intentional Lean statement refactors. Keep empty by default.
LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS=

# Prefix-cache prompt optimization. On autonomous continuation cycles, stop
# re-sending the static skill contract (it stays available via the system-prompt
# skills catalog and skill_view). Reduces per-cycle input tokens ~8-12% on both
# RCP and codex routes with no measured quality regression. Enabled by default;
# set to 0 to disable.
LEANFLOW_RCP_PREFIX_CACHE=1
"""

DEFAULT_SOUL_MD = """# LeanFlow

You are LeanFlow, a Lean-first automation kernel.

Prioritize Lean proving, formalization, verification, and clear workflow state.
Do not optimize for generic assistant breadth when it conflicts with finishing
the current Lean task cleanly.
"""


def get_leanflow_home() -> Path:
    return leanflow_home()


def get_config_path() -> Path:
    return get_leanflow_home() / "config.yaml"


def get_env_path() -> Path:
    return get_leanflow_home() / ".env"


def _secure_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def _secure_file(path: Path) -> None:
    try:
        if path.exists():
            path.chmod(0o600)
    except OSError:
        pass


def _ensure_default_soul_md(home: Path) -> None:
    soul_path = home / "SOUL.md"
    if soul_path.exists():
        return
    soul_path.write_text(DEFAULT_SOUL_MD, encoding="utf-8")
    _secure_file(soul_path)


def default_config_yaml(config: Mapping[str, Any] | None = None) -> str:
    payload = DEFAULT_CONFIG if config is None else config
    return DEFAULT_CONFIG_HEADER + yaml.safe_dump(dict(payload), sort_keys=False)


def _ensure_default_config_file(home: Path) -> None:
    config_path = home / "config.yaml"
    if not config_path.exists():
        config_path.write_text(default_config_yaml(DEFAULT_CONFIG), encoding="utf-8")
        _secure_file(config_path)
        return

    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        _secure_file(config_path)
        return
    if not isinstance(payload, Mapping):
        _secure_file(config_path)
        return

    merged = _deep_merge(DEFAULT_CONFIG, payload)
    if merged != payload:
        config_path.write_text(default_config_yaml(merged), encoding="utf-8")
    _secure_file(config_path)


def _merge_env_template(existing: str) -> str:
    existing_lines = existing.splitlines()
    existing_keys = {
        line.split("=", 1)[0].strip()
        for line in existing_lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    additions = []
    for line in DEFAULT_ENV_TEMPLATE.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key and key not in existing_keys:
            additions.append(line)
    if not existing.strip():
        return DEFAULT_ENV_TEMPLATE.rstrip() + "\n"
    if not additions:
        return existing if existing.endswith("\n") else existing + "\n"
    return (
        existing.rstrip()
        + "\n\n# Added by LeanFlow for provider/model setup.\n"
        + "\n".join(additions)
        + "\n"
    )


def _ensure_default_env_file(home: Path) -> None:
    env_path = home / ".env"
    if env_path.exists():
        updated = _merge_env_template(env_path.read_text(encoding="utf-8"))
    else:
        updated = DEFAULT_ENV_TEMPLATE.rstrip() + "\n"
    if not env_path.exists() or env_path.read_text(encoding="utf-8") != updated:
        env_path.write_text(updated, encoding="utf-8")
    _secure_file(env_path)


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def ensure_leanflow_home() -> Path:
    home = get_leanflow_home()
    ensure_directory(home)
    _secure_dir(home)
    _ensure_default_soul_md(home)
    for subdir in ("sessions", "logs", "memories", "workflow-state", "local-models"):
        target = ensure_directory(home / subdir)
        _secure_dir(target)
    _ensure_default_config_file(home)
    _ensure_default_env_file(home)
    return home


# Process-lifetime cache of the parsed config. load_config() is called from hot per-item loops
# (e.g. workflow activity summarization at exit), and each uncached call does expensive work:
# ensure_leanflow_home() (mkdir + chmod on several dirs, default-file checks) plus a YAML parse.
# On a large history that turned exit cleanup into a multi-minute ~100% CPU spin. We memoize the
# merged result and hand callers a deepcopy (they may freely mutate it); save_config() and
# invalidate_config_cache() drop the cache so writers still observe their own changes.
_CONFIG_CACHE: dict[str, Any] | None = None
_CONFIG_CACHE_KEY: str | None = None  # the config path the cache was built for


def invalidate_config_cache() -> None:
    """Drop the in-process load_config() cache (call after writing the config file)."""
    global _CONFIG_CACHE, _CONFIG_CACHE_KEY
    _CONFIG_CACHE = None
    _CONFIG_CACHE_KEY = None


def load_config() -> dict[str, Any]:
    """Return the merged LeanFlow configuration with process-lifetime caching. Reads config.yaml from LEANFLOW_HOME, merges it with defaults, and caches the result keyed by config path to detect per-test home changes; callers receive a deepcopy and may freely mutate it. Handles transient read errors (which bypass caching). Does not cache on malformed payloads."""
    global _CONFIG_CACHE, _CONFIG_CACHE_KEY
    # Key the cache on the resolved config path so a changed LEANFLOW_HOME (notably per-test
    # isolation, but also any in-process home switch) is a cache miss and re-reads from disk.
    path = get_config_path()
    cache_key = str(path)
    if _CONFIG_CACHE is not None and cache_key == _CONFIG_CACHE_KEY:
        return deepcopy(_CONFIG_CACHE)

    ensure_leanflow_home()
    path = get_config_path()
    if not path.exists():
        ensure_leanflow_home()
        path.write_text(default_config_yaml(DEFAULT_CONFIG), encoding="utf-8")
        _secure_file(path)
        _CONFIG_CACHE = deepcopy(DEFAULT_CONFIG)
        _CONFIG_CACHE_KEY = cache_key
        return deepcopy(_CONFIG_CACHE)

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return deepcopy(DEFAULT_CONFIG)  # transient read/parse error: do not cache
    if not isinstance(payload, Mapping):
        return deepcopy(DEFAULT_CONFIG)  # malformed config: do not cache
    merged = _deep_merge(DEFAULT_CONFIG, payload)
    _CONFIG_CACHE = merged
    _CONFIG_CACHE_KEY = cache_key
    return deepcopy(merged)


def save_config(config: Mapping[str, Any]) -> None:
    ensure_leanflow_home()
    path = get_config_path()
    path.write_text(yaml.safe_dump(dict(config), sort_keys=False), encoding="utf-8")
    _secure_file(path)
    invalidate_config_cache()


def _descend_config(
    config: dict[str, Any], key_path: str, create: bool = False
) -> tuple[dict[str, Any], str]:
    parts = [part for part in key_path.split(".") if part]
    if not parts:
        raise KeyError("config key path must not be empty")
    node = config
    for part in parts[:-1]:
        if part not in node:
            if not create:
                raise KeyError(key_path)
            node[part] = {}
        child = node[part]
        if not isinstance(child, dict):
            if not create:
                raise KeyError(key_path)
            child = {}
            node[part] = child
        node = child
    return node, parts[-1]


def get_config_value(key_path: str, default: Any = None) -> Any:
    config = load_config()
    try:
        node, leaf = _descend_config(config, key_path)
        return node.get(leaf, default)
    except KeyError:
        return default


def set_config_value(key_path: str, value: Any) -> None:
    config = load_config()
    node, leaf = _descend_config(config, key_path, create=True)
    node[leaf] = value
    save_config(config)


def load_env_file() -> dict[str, str]:
    ensure_leanflow_home()
    result: dict[str, str] = {}
    for line in get_env_path().read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value
    return result


def load_env() -> dict[str, str]:
    """Compatibility helper for modules that need a snapshot of env-file values."""
    return load_env_file()


def save_env_value(key: str, value: str) -> None:
    values = load_env_file()
    values[key] = value
    lines = [f"{env_key}={env_value}" for env_key, env_value in sorted(values.items())]
    get_env_path().write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    _secure_file(get_env_path())


def get_env_value(key: str, default: str | None = None) -> str | None:
    if key in os.environ:
        return os.environ[key]
    return load_env_file().get(key, default)
