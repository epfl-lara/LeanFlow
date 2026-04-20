"""EPFLemma config and environment management."""

from __future__ import annotations

import os
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


DEFAULT_CONFIG: dict[str, Any] = {
    "epflemma": {
        "project": {
            "template_source": "",
        },
        "workflow": {
            "managed_state_dir": "",
            "autonomous_followups": 6,
        },
    },
    "model": {
        "default": "google/gemma-4-31B-it",
        "provider": "auto",
        "base_url": "",
        "api_key": "",
    },
    "toolsets": ["epflemma-cli"],
    "agent": {
        "max_turns": 90,
    },
    "compression": {
        "enabled": True,
        "threshold": 0.50,
        "summary_model": "google/gemma-4-31B-it",
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

    for key in ("model", "toolsets", "agent", "compression", "custom_providers", "local_models"):
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
    for subdir in ("sessions", "logs", "memories", "workflow-state", "local-models"):
        target = home / subdir
        target.mkdir(parents=True, exist_ok=True)
        _secure_dir(target)
    env_path = home / ".env"
    if not env_path.exists():
        env_path.touch()
    _secure_file(env_path)
    return home


def load_config() -> dict[str, Any]:
    ensure_epflemma_home()
    path = get_config_path()
    if not path.exists():
        save_config(DEFAULT_CONFIG)
        return deepcopy(DEFAULT_CONFIG)

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return deepcopy(DEFAULT_CONFIG)
    if not isinstance(payload, Mapping):
        return deepcopy(DEFAULT_CONFIG)
    if any(key in payload for key in ("gauss", "opengauss")) and "epflemma" not in payload:
        payload = _transform_legacy_config(payload)
        save_config(payload)
    return _deep_merge(DEFAULT_CONFIG, payload)


def save_config(config: Mapping[str, Any]) -> None:
    ensure_epflemma_home(import_legacy=False)
    path = get_config_path()
    path.write_text(yaml.safe_dump(dict(config), sort_keys=False), encoding="utf-8")
    _secure_file(path)


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
