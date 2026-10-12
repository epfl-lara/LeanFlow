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


NATIVE_CONFIG_VERSION = 2

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": NATIVE_CONFIG_VERSION,
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
    "model": {"provider": "codex", "default": ""},
    "auxiliary": {
        task: {"provider": "", "model": "", "reasoning_effort": ""}
        for task in (
            "lean_reasoning",
            "lean_decompose_helpers",
            "manager_nudge",
            "orchestration",
            "planner_synthesis",
            "blueprint_verification",
            "autoformalizer_verification",
        )
    },
    "toolsets": ["leanflow-cli"],
    "agent": {"reasoning_effort": ""},
    "limits": {"agent_calls": None, "tokens": None, "wall_time_s": None},
    "storage": {"cache_target_gib": 20, "min_free_gib": 10, "automatic_cleanup": True},
    "logging": {
        "preview_lines": 8,
        "preview_chars": 1600,
        "tool_output_head_lines": 28,
        "tool_output_tail_lines": 12,
        "activity_preview_chars": 420,
    },
}

DEFAULT_CONFIG_HEADER = """# LeanFlow configuration
#
# Native backends: codex, claude, lemex. Each CLI owns its login, API credentials,
# endpoint, sampling, context compression, and session history.
# model.default and agent.reasoning_effort are optional explicit overrides.
# Empty values preserve the native CLI's configured defaults.
# Auxiliary role values are optional overrides; empty values inherit the launch.
# limits are global across a campaign: null means unlimited, 0 means exhausted.
# One native CLI invocation is one agent call. Reported token usage can stop
# subsequent calls; an active call can overshoot the token limit.
# storage bounds rebuildable managed caches; proofs and checkpoints are retained.
# The cache target is 20 GiB; new work requires 10 GiB of free disk space.
"""

DEFAULT_ENV_TEMPLATE = """# LeanFlow environment overrides
# Native runtime configuration stays with its CLI and is inherited unchanged.
# Configure OPENAI_API_KEY, ANTHROPIC_API_KEY, or LEMEX_BASE_URL / LEMEX_API_KEY
# in your shell or the corresponding runtime's native setup, not this file.
LEANFLOW_INFERENCE_PROVIDER=
LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS=
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

    # Existing configuration is user-owned, including retired settings. A load
    # can explain migration, but must never silently rewrite keys or choices.
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


def native_config_migration_required(config: Mapping[str, Any]) -> bool:
    """Detect retired routing/defaults without guessing which model was intentional."""
    if config.get("schema_version", NATIVE_CONFIG_VERSION) != NATIVE_CONFIG_VERSION:
        return True
    if any(key in config for key in ("custom_providers", "local_models", "compression")):
        return True
    model = config.get("model")
    if isinstance(model, str) and model.strip():
        return True
    if isinstance(model, Mapping) and str(model.get("provider", "") or "").strip().lower() not in {
        "",
        "codex",
        "claude",
        "lemex",
    }:
        return True
    if isinstance(model, Mapping) and any(
        key in model for key in ("base_url", "api_key", "context_lengths", "prover_light")
    ):
        return True
    agent = config.get("agent")
    if isinstance(agent, Mapping) and any(
        key in agent for key in ("max_turns", "temperature", "seed", "top_p", "top_k", "min_p")
    ):
        return True
    auxiliary = config.get("auxiliary")
    return isinstance(auxiliary, Mapping) and any(
        isinstance(role, Mapping)
        and any(
            key in role
            for key in (
                "base_url",
                "api_key",
                "command_template",
                "codex_command_template",
                "claude_code_command_template",
            )
        )
        for role in auxiliary.values()
    )


def _merge_config_defaults(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Merge current defaults in memory and retain an explicit legacy schema marker."""
    merged = _deep_merge(DEFAULT_CONFIG, payload)
    if native_config_migration_required(payload):
        merged["schema_version"] = 1
    return merged


def ensure_leanflow_home() -> Path:
    home = get_leanflow_home()
    ensure_directory(home)
    _secure_dir(home)
    _ensure_default_soul_md(home)
    for subdir in ("sessions", "logs", "memories", "workflow-state"):
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
    merged = _merge_config_defaults(payload)
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
