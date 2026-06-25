"""Managed local-model runtime helpers for LeanFlow."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from leanflow_cli.config import load_config, save_config

SUPPORTED_LOCAL_RUNTIMES = ("vllm", "ollama", "llama_cpp")


def _state_root() -> Path:
    from leanflow_cli.config import ensure_leanflow_home, get_leanflow_home

    ensure_leanflow_home()
    root = get_leanflow_home() / "local-models"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _state_path(runtime: str) -> Path:
    return _state_root() / f"{runtime}.json"


def _log_path(runtime: str) -> Path:
    return _state_root() / f"{runtime}.log"


def _load_state(runtime: str) -> dict[str, Any]:
    path = _state_path(runtime)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_state(runtime: str, payload: dict[str, Any]) -> None:
    _state_path(runtime).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _runtime_config(runtime: str) -> dict[str, Any]:
    config = load_config()
    return (
        (
            (
                (config.get("local_models") or {})
                if isinstance(config.get("local_models"), dict)
                else {}
            ).get("runtimes")
            or {}
        )
        if isinstance(
            (
                (
                    (config.get("local_models") or {})
                    if isinstance(config.get("local_models"), dict)
                    else {}
                ).get("runtimes")
            ),
            dict,
        )
        else {}
    ).get(runtime, {})


def _runtime_base_url(runtime: str, host: str, port: int) -> str:
    if runtime == "ollama":
        return f"http://{host}:{port}/v1"
    return f"http://{host}:{port}/v1"


def _health_check(base_url: str, timeout: float = 2.0) -> bool:
    candidates = [base_url.rstrip("/") + "/models"]
    stripped = base_url.rstrip("/")
    if not stripped.endswith("/v1"):
        candidates.append(stripped + "/v1/models")
    for url in candidates:
        try:
            response = httpx.get(url, timeout=timeout)
            if response.status_code < 500:
                return True
        except Exception:
            continue
    return False


def _build_command(
    runtime: str, model: str, host: str, port: int, extra_args: list[str]
) -> list[str]:
    if runtime == "vllm":
        return [
            sys.executable,
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--host",
            host,
            "--port",
            str(port),
            "--model",
            model,
            *extra_args,
        ]
    if runtime == "ollama":
        return ["ollama", "serve", *extra_args]
    if runtime == "llama_cpp":
        return ["llama-server", "-m", model, "--host", host, "--port", str(port), *extra_args]
    raise ValueError(f"Unsupported local runtime: {runtime}")


def list_local_runtimes() -> list[dict[str, Any]]:
    return [get_local_runtime_status(runtime) for runtime in SUPPORTED_LOCAL_RUNTIMES]


def get_local_runtime_status(runtime: str) -> dict[str, Any]:
    if runtime not in SUPPORTED_LOCAL_RUNTIMES:
        raise ValueError(f"Unsupported runtime: {runtime}")

    state = _load_state(runtime)
    config = _runtime_config(runtime)
    host = str(state.get("host") or config.get("host") or "127.0.0.1")
    port = int(state.get("port") or config.get("port") or 0 or 0)
    base_url = str(state.get("base_url") or _runtime_base_url(runtime, host, port)) if port else ""
    pid = int(state.get("pid") or 0)
    running = _pid_is_running(pid)
    healthy = _health_check(base_url) if running and base_url else False
    return {
        "runtime": runtime,
        "configured": bool(config),
        "pid": pid,
        "running": running,
        "healthy": healthy,
        "model": str(state.get("model", "") or ""),
        "host": host,
        "port": port,
        "base_url": base_url,
        "log_path": str(_log_path(runtime)),
        "command": state.get("command") or [],
    }


def start_local_runtime(
    runtime: str, *, model: str, host: str | None = None, port: int | None = None
) -> dict[str, Any]:
    """Start a local model server (vllm/ollama/llama_cpp) as a subprocess, returning its status; returns existing runtime status if already running. Resolves host/port from arguments or config, builds the runtime-specific command with extra args, spawns it in a new session with log appending, and persists pid/model/command to state."""
    if runtime not in SUPPORTED_LOCAL_RUNTIMES:
        raise ValueError(f"Unsupported runtime: {runtime}")

    current = get_local_runtime_status(runtime)
    if current["running"]:
        return current

    config = _runtime_config(runtime)
    resolved_host = str(host or config.get("host") or "127.0.0.1")
    resolved_port = int(port or config.get("port") or (11434 if runtime == "ollama" else 8000))
    extra_args = [str(arg) for arg in (config.get("extra_args") or [])]
    command = _build_command(runtime, model, resolved_host, resolved_port, extra_args)

    log_path = _log_path(runtime)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    payload = {
        "runtime": runtime,
        "pid": process.pid,
        "model": model,
        "host": resolved_host,
        "port": resolved_port,
        "base_url": _runtime_base_url(runtime, resolved_host, resolved_port),
        "command": command,
        "started_at": time.time(),
    }
    _save_state(runtime, payload)
    return get_local_runtime_status(runtime)


def stop_local_runtime(runtime: str) -> dict[str, Any]:
    if runtime not in SUPPORTED_LOCAL_RUNTIMES:
        raise ValueError(f"Unsupported runtime: {runtime}")
    state = _load_state(runtime)
    pid = int(state.get("pid") or 0)
    if pid and _pid_is_running(pid):
        os.killpg(pid, signal.SIGTERM)
        deadline = time.time() + 5
        while time.time() < deadline and _pid_is_running(pid):
            time.sleep(0.1)
        if _pid_is_running(pid):
            os.killpg(pid, signal.SIGKILL)
    state["stopped_at"] = time.time()
    _save_state(runtime, state)
    return get_local_runtime_status(runtime)


def read_local_runtime_logs(runtime: str, *, tail: int = 80) -> str:
    log_path = _log_path(runtime)
    if not log_path.exists():
        return ""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])


def use_local_runtime(runtime: str, *, model: str) -> dict[str, Any]:
    if runtime not in SUPPORTED_LOCAL_RUNTIMES:
        raise ValueError(f"Unsupported runtime: {runtime}")
    config = load_config()
    config.setdefault("local_models", {})
    config["local_models"]["active_runtime"] = runtime
    config["local_models"]["active_model"] = model
    config.setdefault("model", {})
    if not isinstance(config["model"], dict):
        config["model"] = {}
    config["model"]["provider"] = "local"
    config["model"]["default"] = model
    save_config(config)
    return resolve_active_local_runtime()


def resolve_active_local_runtime() -> dict[str, Any] | None:
    config = load_config()
    local_cfg = config.get("local_models") if isinstance(config.get("local_models"), dict) else {}
    runtime = str(local_cfg.get("active_runtime", "") or "").strip()
    model = str(
        local_cfg.get("active_model", "") or config.get("model", {}).get("default", "")
    ).strip()
    if not runtime:
        runtime = str(local_cfg.get("default_runtime", "vllm") or "vllm").strip()
    if runtime not in SUPPORTED_LOCAL_RUNTIMES:
        return None
    status = get_local_runtime_status(runtime)
    if status["base_url"]:
        status["api_key"] = "local"
        status["model"] = model or status.get("model", "")
        return status
    return {
        "runtime": runtime,
        "base_url": _runtime_base_url(
            runtime,
            status["host"] or "127.0.0.1",
            status["port"] or (11434 if runtime == "ollama" else 8000),
        ),
        "api_key": "local",
        "model": model,
    }
