"""Expose credential-free native backend setup metadata to editor integrations."""

from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from leanflow_cli.runtime.provider_models import model_catalog
from leanflow_cli.runtime.runtime_provider import (
    NATIVE_BACKENDS,
    RuntimeProviderError,
    resolve_backend_executable,
    resolve_requested_provider,
    resolve_runtime_provider,
)

KEY_NAMES = {
    "codex": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "lemex": "LEMEX_API_KEY",
}


def _probe(executable: str, args: list[str]) -> tuple[int, str]:
    """Bound metadata commands only; never make an inference request."""
    try:
        result = subprocess.run([executable, *args], capture_output=True, text=True, timeout=10)
        return result.returncode, result.stdout + result.stderr
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def backend_setup(backend: str, *, check: bool = False) -> dict[str, Any]:
    """Report installation and credential presence without reading auth files."""
    try:
        executable = resolve_backend_executable(backend)
    except RuntimeProviderError:
        executable = ""
    except Exception:
        # A corrupt managed installation is unavailable; its raw diagnostic may
        # include user configuration and is better inspected with the doctor.
        executable = ""
    home_name = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR", "lemex": "LEMEX_HOME"}[
        backend
    ]
    home = Path(os.environ.get(home_name) or str(Path.home() / f".{backend}"))
    key_present = bool(os.environ.get(KEY_NAMES[backend]))
    if backend == "codex":
        key_present = key_present or bool(os.environ.get("CODEX_API_KEY"))
    auth = "environment" if key_present else "not_checked"
    version = ""
    if check and executable:
        code, output = _probe(executable, ["--version"])
        # Only a recognizable version number crosses the editor boundary.
        match = re.search(r"\b\d+\.\d+\.\d+[\w.+-]*", output)
        version = match.group(0) if code == 0 and match else ""
        if not key_present and backend != "lemex":
            args = ["auth", "status", "--json"] if backend == "claude" else ["login", "status"]
            code, output = _probe(executable, args)
            if backend == "claude":
                try:
                    logged_in = json.loads(output).get("loggedIn")
                except (ValueError, AttributeError):
                    logged_in = None
                auth = (
                    "native_login"
                    if logged_in is True
                    else "login_needed" if logged_in is False else "unknown"
                )
            else:
                auth = "native_login" if code == 0 else "login_needed" if code == 1 else "unknown"
    endpoint = ""
    if backend == "lemex":
        try:
            url = urlsplit(os.environ.get("LEMEX_BASE_URL", ""))
            if url.scheme in {"http", "https"} and url.hostname:
                host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
                if url.port is not None:
                    host += f":{url.port}"
                endpoint = urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            pass
    return {
        "id": backend,
        "installed": bool(executable),
        "executable": executable,
        "version": version,
        "auth": auth,
        "key_env": KEY_NAMES[backend],
        "config_path": str(home / ("settings.json" if backend == "claude" else "config.toml")),
        "endpoint": endpoint,
        "checked": check,
        "model_catalog": model_catalog(backend, executable, check=check),
    }


def provider_setup(*, requested: str | None = None, check: bool = False) -> dict[str, Any]:
    """Describe all backends and the effective default without changing native state."""
    try:
        selected = resolve_requested_provider(requested)
    except RuntimeProviderError:
        selected = ""
    # Native metadata requests have independent connections; slow login checks
    # for one provider must not serially delay every other provider's catalog.
    with ThreadPoolExecutor(max_workers=len(NATIVE_BACKENDS)) as executor:
        backends = list(
            executor.map(lambda name: backend_setup(name, check=check), NATIVE_BACKENDS)
        )
    try:
        overrides = resolve_runtime_provider(requested=selected or None)
    except RuntimeProviderError:
        overrides = {}
    return {
        "version": 1,
        "selected": selected,
        "model_override": overrides.get("model", ""),
        "reasoning_effort_override": overrides.get("reasoning_effort", ""),
        "backends": backends,
    }
