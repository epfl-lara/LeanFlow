"""EPFLemma config and environment management."""

from __future__ import annotations

import os
import re
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml

EPFLEMMA_HOME_ENV = "EPFLEMMA_HOME"
LEGACY_BRANDED_HOME_ENV = "OPENGAUSS_HOME"
LEGACY_HOME_ENV = "GAUSS_HOME"
LEGACY_BRANDED_HOME_DEFAULT = Path.home() / ".opengauss"
LEGACY_HOME_DEFAULT = Path.home() / ".gauss"
EPFLEMMA_HOME_DEFAULT = Path.home() / ".epflemma"
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Legacy config carried a large registry of optional env vars for removed
# gateway/browser/voice surfaces. The Lean-first kernel no longer needs that
# catalog, but terminal env sanitization still expects this mapping to exist.
OPTIONAL_ENV_VARS: dict[str, dict[str, Any]] = {}


DEFAULT_CONFIG: dict[str, Any] = {
    "epflemma": {
        "project": {
            "template_source": "",
        },
        "workflow": {
            "managed_state_dir": "",
            "autonomous_followups": 6,
        },
        "sandbox": {
            "engine": "auto",
            "image": "epflemma/sandbox:local",
            "env_file": "",
            "cache_dir": "",
            "runs_dir": "",
            "network": True,
            "read_only_root": True,
            "bootstrap_mcp": True,
        },
    },
    "model": {
        "default": "moonshotai/Kimi-K2.6",
        "provider": "auto",
        "base_url": "",
        "api_key": "",
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
    "toolsets": ["epflemma-cli"],
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

DEFAULT_CONFIG_HEADER = """# EPFLemma configuration
#
# Main workflow model:
#   model.default is the primary agent model used for prove/autoprove/formalize.
#   The installation default is moonshotai/Kimi-K2.6.
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
#     model.provider=codex, with optional EPFLEMMA_CODEX_MODEL override
#   - Change a custom OpenAI-compatible endpoint: model.base_url or
#     EPFLEMMA_OPENAI_BASE_URL in ~/.epflemma/.env
#   - Override a model context window when provider metadata is missing or
#     wrong: model.context_lengths.<model-id>
#   - Change the theorem advisor model: auxiliary.lean_reasoning.model
#   - Change the theorem advisor reasoning budget:
#     auxiliary.lean_reasoning.reasoning_effort
#   - Use a separate helper-decomposition planner:
#     auxiliary.lean_decompose_helpers.model and
#     auxiliary.lean_decompose_helpers.reasoning_effort
#   - Use separate formalization verifiers:
#     auxiliary.blueprint_verification.provider and
#     auxiliary.autoformalizer_verification.provider
#   - Use a separate theorem advisor endpoint: set
#     auxiliary.lean_reasoning.base_url and auxiliary.lean_reasoning.api_key,
#     or AUXILIARY_LEAN_REASONING_BASE_URL / AUXILIARY_LEAN_REASONING_API_KEY
#     in ~/.epflemma/.env
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
#   - epflemma.sandbox controls the optional container sandbox runtime.
#
"""

DEFAULT_ENV_TEMPLATE = """# EPFLemma environment overrides
#
# Leave values empty until you want to configure a provider. Values in this
# file are loaded by the EPFLemma CLI and can be overridden by process env vars.

# OpenAI-compatible primary endpoint, including EPFL RCP-style endpoints.
EPFLEMMA_OPENAI_BASE_URL=
EPFLEMMA_OPENAI_API_KEY=

# OpenRouter fallback/primary endpoint.
OPENROUTER_API_KEY=

# Direct provider keys.
KIMI_API_KEY=
GLM_API_KEY=
ZAI_API_KEY=
ANTHROPIC_API_KEY=
DEEPSEEK_API_KEY=
MINIMAX_API_KEY=

# Optional Codex primary overrides. With model.provider=codex, EPFLemma reads
# the existing Codex CLI login and model settings from ~/.codex.
EPFLEMMA_CODEX_MODEL=
EPFLEMMA_CODEX_REASONING_EFFORT=

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

EPFLEMMA_EXPERT_CODEX_COMMAND_TEMPLATE=
EPFLEMMA_EXPERT_CLAUDE_CODE_COMMAND_TEMPLATE=

# Escape hatch for intentional Lean statement refactors. Keep empty by default.
EPFLEMMA_ALLOW_LEAN_STATEMENT_EDITS=
"""

DEFAULT_SOUL_MD = """# EPFLemma

You are EPFLemma, a Lean-first automation kernel.

Prioritize Lean proving, formalization, verification, and clear workflow state.
Do not optimize for generic assistant breadth when it conflicts with finishing
the current Lean task cleanly.
"""


def get_epflemma_home() -> Path:
    explicit = os.getenv(EPFLEMMA_HOME_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    branded_legacy = os.getenv(LEGACY_BRANDED_HOME_ENV, "").strip()
    if branded_legacy:
        return Path(branded_legacy).expanduser()
    legacy = os.getenv(LEGACY_HOME_ENV, "").strip()
    if legacy and Path(legacy).expanduser().name == ".epflemma":
        return Path(legacy).expanduser()
    return EPFLEMMA_HOME_DEFAULT


def get_legacy_homes() -> list[Path]:
    homes: list[Path] = []
    explicit_envs = (
        (LEGACY_HOME_ENV, None),
        (LEGACY_BRANDED_HOME_ENV, None),
    )
    fallback_defaults = (
        LEGACY_BRANDED_HOME_DEFAULT,
        LEGACY_HOME_DEFAULT,
    )

    for env_name, _ in explicit_envs:
        value = os.getenv(env_name, "").strip()
        if not value:
            continue
        candidate = Path(value).expanduser()
        if candidate not in homes:
            homes.append(candidate)

    for default in fallback_defaults:
        candidate = Path(default).expanduser()
        if candidate not in homes:
            homes.append(candidate)
    return homes


def get_config_path() -> Path:
    return get_epflemma_home() / "config.yaml"


def get_env_path() -> Path:
    return get_epflemma_home() / ".env"


def get_install_root_path() -> Path:
    return get_epflemma_home() / "install-root"


def _secure_dir(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass


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

    if any(key in payload for key in ("gauss", "opengauss")) and "epflemma" not in payload:
        merged = _transform_legacy_config(payload)
    else:
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
    return (existing.rstrip() + "\n\n# Added by EPFLemma for provider/model setup.\n" + "\n".join(additions) + "\n")


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


def _transform_legacy_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    merged = _deep_merge(DEFAULT_CONFIG, {})

    legacy_root = payload.get("epflemma")
    if not isinstance(legacy_root, Mapping):
        legacy_root = payload.get("opengauss")
    if not isinstance(legacy_root, Mapping):
        legacy_root = payload.get("gauss")
    if isinstance(legacy_root, Mapping):
        merged["epflemma"]["project"]["template_source"] = str(
            ((legacy_root.get("project") or {}) if isinstance(legacy_root.get("project"), Mapping) else {}).get(
                "template_source", ""
            )
            or ""
        ).strip()
        workflow_state_dir = str(
            ((legacy_root.get("autoformalize") or {}) if isinstance(legacy_root.get("autoformalize"), Mapping) else {}).get(
                "managed_state_dir", ""
            )
            or ""
        ).strip()
        merged["epflemma"]["workflow"]["managed_state_dir"] = workflow_state_dir

    for key in ("model", "toolsets", "agent", "logging", "compression", "custom_providers", "local_models"):
        value = payload.get(key)
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        elif value is not None:
            merged[key] = value

    toolsets = merged.get("toolsets")
    if isinstance(toolsets, list):
        rewritten = []
        for name in toolsets:
            text = str(name).strip()
            if text in {"gauss-native", "opengauss-native"}:
                text = "epflemma-native"
            elif text in {"gauss-cli", "opengauss-cli"}:
                text = "epflemma-cli"
            elif text.startswith("gauss-") or text.startswith("opengauss-"):
                continue
            if text and text not in rewritten:
                rewritten.append(text)
        merged["toolsets"] = rewritten or ["epflemma-cli"]

    return merged


def _import_legacy_home(home: Path) -> None:
    for legacy_home in get_legacy_homes():
        if home.exists() or not legacy_home.exists() or legacy_home.resolve() == home.resolve():
            continue

        home.mkdir(parents=True, exist_ok=True)
        _secure_dir(home)

        legacy_config = legacy_home / "config.yaml"
        if legacy_config.exists() and not get_config_path().exists():
            try:
                payload = yaml.safe_load(legacy_config.read_text(encoding="utf-8")) or {}
                transformed = _transform_legacy_config(payload if isinstance(payload, Mapping) else {})
                save_config(transformed)
            except Exception:
                pass

        legacy_env = legacy_home / ".env"
        if legacy_env.exists() and not get_env_path().exists():
            get_env_path().write_text(legacy_env.read_text(encoding="utf-8"), encoding="utf-8")
            _secure_file(get_env_path())

        if get_config_path().exists() or get_env_path().exists():
            break


def ensure_epflemma_home(import_legacy: bool = True) -> Path:
    home = get_epflemma_home()
    if import_legacy:
        _import_legacy_home(home)
    home.mkdir(parents=True, exist_ok=True)
    _secure_dir(home)
    _ensure_default_soul_md(home)
    for subdir in ("sessions", "logs", "memories", "workflow-state", "local-models"):
        target = home / subdir
        target.mkdir(parents=True, exist_ok=True)
        _secure_dir(target)
    _ensure_default_config_file(home)
    _ensure_default_env_file(home)
    return home


# Process-lifetime cache of the parsed config. load_config() is called from hot per-item loops
# (e.g. workflow activity summarization at exit), and each uncached call does expensive work:
# ensure_epflemma_home() (mkdir + chmod on several dirs, default-file checks) plus a YAML parse.
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
    global _CONFIG_CACHE, _CONFIG_CACHE_KEY
    # Key the cache on the resolved config path so a changed EPFLEMMA_HOME (notably per-test
    # isolation, but also any in-process home switch) is a cache miss and re-reads from disk.
    path = get_config_path()
    cache_key = str(path)
    if _CONFIG_CACHE is not None and _CONFIG_CACHE_KEY == cache_key:
        return deepcopy(_CONFIG_CACHE)

    ensure_epflemma_home()
    path = get_config_path()
    if not path.exists():
        ensure_epflemma_home(import_legacy=False)
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
    if any(key in payload for key in ("gauss", "opengauss")) and "epflemma" not in payload:
        payload = _transform_legacy_config(payload)
        save_config(payload)  # invalidates the cache; re-cached just below
    merged = _deep_merge(DEFAULT_CONFIG, payload)
    _CONFIG_CACHE = merged
    _CONFIG_CACHE_KEY = cache_key
    return deepcopy(merged)


def save_config(config: Mapping[str, Any]) -> None:
    ensure_epflemma_home(import_legacy=False)
    path = get_config_path()
    path.write_text(yaml.safe_dump(dict(config), sort_keys=False), encoding="utf-8")
    _secure_file(path)
    invalidate_config_cache()


def _descend_config(config: dict[str, Any], key_path: str, create: bool = False) -> tuple[dict[str, Any], str]:
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
    ensure_epflemma_home()
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
