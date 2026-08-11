"""Build safe terminal-backend and ambient-process behavior identities."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from leanflow_cli.cli.run_python_identity import (
    _content_identity,
    _normalize_volatile_text,
    _path_identity,
    _path_list_identity,
    _sha256_bytes,
)

_KNOWN_TERMINAL_BACKENDS = frozenset({"local", "ssh", "singularity", "daytona", "docker", "modal"})
_TERMINAL_DEFAULT_IMAGE = "nikolaik/python-nodejs:python3.11-nodejs20"
_TERMINAL_BOOLEAN_DEFAULTS = {
    "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "false",
    "TERMINAL_PERSISTENT_SHELL": "true",
    "TERMINAL_LOCAL_PERSISTENT": "false",
    "TERMINAL_CONTAINER_PERSISTENT": "true",
}
_TERMINAL_INTEGER_DEFAULTS = {
    "TERMINAL_TIMEOUT": "180",
    "TERMINAL_LIFETIME_SECONDS": "300",
    "TERMINAL_SSH_PORT": "22",
    "TERMINAL_CONTAINER_MEMORY": "5120",
    "TERMINAL_CONTAINER_DISK": "51200",
}
_TERMINAL_FLOAT_DEFAULTS = {
    "TERMINAL_CONTAINER_CPU": "1",
    "TERMINAL_DISK_WARNING_GB": "500",
}
_AMBIENT_ENV_KEYS = frozenset(
    {
        "PATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "ELAN_HOME",
        "LAKE_HOME",
        "LEAN_SYSROOT",
        "LEAN_PATH",
        "LEAN_CC",
        "CC",
        "CXX",
        "LIBRARY_PATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "PKG_CONFIG_PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TZ",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "HOME",
    }
)
_PATH_LIST_ENV_KEYS = frozenset(
    {
        "PATH",
        "LEAN_PATH",
        "LIBRARY_PATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "PKG_CONFIG_PATH",
    }
)
_PATH_ENV_KEYS = frozenset(
    {
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "ELAN_HOME",
        "LAKE_HOME",
        "LEAN_SYSROOT",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)
_AMBIENT_PREFIXES = ("ELAN_", "LAKE_", "LEAN_", "GIT_")


def _canonical_number(raw: str, *, floating: bool) -> Any:
    """Return the effective numeric value or a digest-only invalid identity."""
    try:
        value = float(raw) if floating else int(raw)
        if floating and not math.isfinite(value):
            raise ValueError("non-finite terminal control")
        return value
    except (TypeError, ValueError):
        return {"status": "invalid", "value": _content_identity(raw)}


def _terminal_key_identity(raw: str, *, project_root: Path) -> tuple[dict[str, Any], bool]:
    """Bind an SSH key path and content without persisting either."""
    identity = _path_identity(raw, project_root=project_root)
    if not raw:
        return identity, True
    try:
        content = Path(raw).expanduser().resolve(strict=True).read_bytes()
    except (OSError, RuntimeError, ValueError):
        identity["content_status"] = "unreadable"
        return identity, False
    identity.update(
        {
            "content_status": "complete",
            "content_sha256": _sha256_bytes(content),
            "bytes": len(content),
        }
    )
    return identity, True


def _normalize_json_strings(value: Any, *, project_root: Path) -> Any:
    """Normalize volatile roots recursively before hashing structured controls."""
    if isinstance(value, dict):
        return {
            str(key): _normalize_json_strings(item, project_root=project_root)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_normalize_json_strings(item, project_root=project_root) for item in value]
    if isinstance(value, str):
        return _normalize_volatile_text(value, project_root=project_root)
    return value


def collect_ambient_environment_identity(project_root: Path) -> dict[str, Any]:
    """Hash relevant toolchain, locale, proxy, home, and path controls."""
    values: dict[str, dict[str, str]] = {}
    keys = sorted(
        key for key in os.environ if key in _AMBIENT_ENV_KEYS or key.startswith(_AMBIENT_PREFIXES)
    )
    for key in keys:
        raw = str(os.environ[key] or "")
        if key in _PATH_LIST_ENV_KEYS:
            identity = _path_list_identity(raw, project_root=project_root)
        elif key in _PATH_ENV_KEYS:
            canonical = json.dumps(
                _path_identity(raw, project_root=project_root),
                sort_keys=True,
                separators=(",", ":"),
            )
            identity = _content_identity(canonical)
        elif key == "HOME" or key in {"TMPDIR", "TMP", "TEMP"}:
            try:
                root = os.path.normcase(str(Path(raw).expanduser().resolve(strict=False)))
            except (OSError, RuntimeError, ValueError):
                root = raw
            identity = _content_identity(root)
        else:
            identity = _content_identity(_normalize_volatile_text(raw, project_root=project_root))
        # Nest the digest under a neutral field so an environment name ending
        # in API_KEY/TOKEN remains content-bound without violating the durable
        # projection's fail-closed secret-suffix validator.
        values[key] = {"identity": identity}
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "values": values,
        "sha256": _sha256_bytes(encoded),
        "configured_count": len(values),
        "complete": True,
    }


def collect_terminal_runtime_identity(project_root: Path) -> dict[str, Any]:
    """Return canonical credential-safe terminal backend and sandbox controls."""
    from core.home import leanflow_home

    source = os.environ
    raw_backend = str(source.get("TERMINAL_ENV", "local") or "")
    backend = (
        raw_backend if raw_backend in _KNOWN_TERMINAL_BACKENDS else _content_identity(raw_backend)
    )
    default_cwd = (
        os.getcwd() if raw_backend == "local" else "~" if raw_backend == "ssh" else "/root"
    )
    sandbox_default = leanflow_home() / "sandboxes"
    sandbox_raw = str(source.get("TERMINAL_SANDBOX_DIR", str(sandbox_default)) or "")
    scratch_default = Path(sandbox_raw).expanduser() / "singularity"
    controls: dict[str, Any] = {
        "TERMINAL_ENV": backend,
        "TERMINAL_CWD": _path_identity(
            str(source.get("TERMINAL_CWD", default_cwd) or ""), project_root=project_root
        ),
        "TERMINAL_SANDBOX_DIR": _path_identity(sandbox_raw, project_root=project_root),
        "TERMINAL_SCRATCH_DIR": _path_identity(
            str(source.get("TERMINAL_SCRATCH_DIR", str(scratch_default)) or ""),
            project_root=project_root,
        ),
    }
    image_defaults = {
        "TERMINAL_DOCKER_IMAGE": _TERMINAL_DEFAULT_IMAGE,
        "TERMINAL_SINGULARITY_IMAGE": f"docker://{_TERMINAL_DEFAULT_IMAGE}",
        "TERMINAL_MODAL_IMAGE": _TERMINAL_DEFAULT_IMAGE,
        "TERMINAL_DAYTONA_IMAGE": _TERMINAL_DEFAULT_IMAGE,
    }
    for key, default in image_defaults.items():
        normalized = _normalize_volatile_text(
            str(source.get(key, default) or ""), project_root=project_root
        )
        controls[key] = _content_identity(normalized)
    for key, default in _TERMINAL_BOOLEAN_DEFAULTS.items():
        controls[key] = str(source.get(key, default) or "").lower() in {"true", "1", "yes"}
    raw_persistent = str(source.get("TERMINAL_PERSISTENT_SHELL", "true") or "")
    raw_ssh_persistent = str(source.get("TERMINAL_SSH_PERSISTENT", raw_persistent) or "")
    controls["TERMINAL_SSH_PERSISTENT"] = raw_ssh_persistent.lower() in {"true", "1", "yes"}
    for key, default in _TERMINAL_INTEGER_DEFAULTS.items():
        controls[key] = _canonical_number(str(source.get(key, default) or ""), floating=False)
    for key, default in _TERMINAL_FLOAT_DEFAULTS.items():
        controls[key] = _canonical_number(str(source.get(key, default) or ""), floating=True)
    for key in ("TERMINAL_SSH_HOST", "TERMINAL_SSH_USER"):
        normalized = _normalize_volatile_text(
            str(source.get(key, "") or ""), project_root=project_root
        )
        controls[key] = _content_identity(normalized)
    ssh_key, ssh_key_complete = _terminal_key_identity(
        str(source.get("TERMINAL_SSH_KEY", "") or ""), project_root=project_root
    )
    controls["TERMINAL_SSH_KEY"] = ssh_key
    raw_volumes = str(source.get("TERMINAL_DOCKER_VOLUMES", "[]") or "")
    try:
        parsed_volumes = _normalize_json_strings(json.loads(raw_volumes), project_root=project_root)
        canonical_volumes = json.dumps(
            parsed_volumes, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        controls["TERMINAL_DOCKER_VOLUMES"] = {
            "status": "valid",
            "value": _content_identity(canonical_volumes),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        controls["TERMINAL_DOCKER_VOLUMES"] = {
            "status": "invalid",
            "value": _content_identity(
                _normalize_volatile_text(raw_volumes, project_root=project_root)
            ),
        }
    arbitrary = {
        key: {
            "identity": _content_identity(
                _normalize_volatile_text(value, project_root=project_root)
            )
        }
        for key, value in sorted(source.items())
        if key.startswith("TERMINAL_") and key not in controls
    }
    issues = [] if ssh_key_complete else ["terminal-ssh-key-unreadable"]
    payload = {"backend": backend, "controls": controls, "arbitrary_controls": arbitrary}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        **payload,
        "sha256": _sha256_bytes(encoded),
        "complete": not issues,
        "issue_count": len(issues),
        "issues_sha256": _sha256_bytes(
            json.dumps(issues, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ),
    }
