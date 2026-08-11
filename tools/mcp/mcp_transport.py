#!/usr/bin/env python3
"""Stdio/HTTP transport plumbing for MCP servers.

Pure, dependency-light helpers that prepare the subprocess environment,
resolve stdio commands and working directories, augment / repair managed
Lean MCP runtimes, compute effective connect timeouts, and render MCP
connection errors into actionable short messages.

This module is a leaf: it does NOT import :mod:`tools.mcp.mcp_tool`, so the
re-export shim in ``mcp_tool`` introduces no import cycle. Every name here
is re-exported from ``tools.mcp.mcp_tool`` for backwards compatibility, so
callers and tests that resolve ``tools.mcp.mcp_tool.<name>`` keep working.
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TextIO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CONNECT_TIMEOUT = 60  # seconds for initial connection per server
_LOCAL_LOOGLE_CONNECT_TIMEOUT = 600

# Environment variables that are safe to pass to stdio subprocesses
_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TERM",
        "SHELL",
        "TMPDIR",
        "ELAN_HOME",
    }
)
_LOOGLE_STALE_ARTIFACT_SCAN_LIMIT = 80
_LEAN_MODULE_PART_PATTERN = re.compile(r"^[A-Z_][A-Za-z0-9_']*$")
_DISPATCH_LOCAL_LOOGLE_ENV = "LEANFLOW_DISPATCH_LOCAL_LOOGLE"
_RESEARCH_LOCAL_LOOGLE_ENV = "LEANFLOW_RESEARCH_LOCAL_LOOGLE"

# Regex for credential patterns to strip from error messages
_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{1,255}"  # GitHub PAT
    r"|sk-[A-Za-z0-9_]{1,255}"  # OpenAI-style key
    r"|Bearer\s+\S+"  # Bearer token
    r"|token=[^\s&,;\"']{1,255}"  # token=...
    r"|key=[^\s&,;\"']{1,255}"  # key=...
    r"|API_KEY=[^\s&,;\"']{1,255}"  # API_KEY=...
    r"|password=[^\s&,;\"']{1,255}"  # password=...
    r"|secret=[^\s&,;\"']{1,255}"  # secret=...
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------


def _build_safe_env(user_env: dict | None) -> dict:
    """Build a filtered environment dict for stdio subprocesses.

    Only passes through safe baseline variables (PATH, HOME, etc.) and XDG_*
    variables from the current process environment, plus any variables
    explicitly specified by the user in the server config.

    This prevents accidentally leaking secrets like API keys, tokens, or
    credentials to MCP server subprocesses.
    """
    env = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_KEYS or key.startswith("XDG_"):
            env[key] = value
    if user_env:
        env.update(user_env)
    return env


def _sanitize_error(text: str) -> str:
    """Strip credential-like patterns from error text before returning to LLM.

    Replaces tokens, keys, and other secrets with [REDACTED] to prevent
    accidental credential exposure in tool error responses.
    """
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", text)


def _prepend_path(env: dict, directory: str) -> dict:
    """Prepend *directory* to env PATH if it is not already present."""
    updated = dict(env or {})
    if not directory:
        return updated

    existing = updated.get("PATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if directory not in parts:
        parts = [directory, *parts]
    updated["PATH"] = os.pathsep.join(parts) if parts else directory
    return updated


def _resolve_stdio_command(command: str, env: dict) -> tuple[str, dict]:
    """Resolve a stdio MCP command against the exact subprocess environment.

    This primarily exists to make bare ``npx``/``npm``/``node`` commands work
    reliably even when MCP subprocesses run under a filtered PATH.
    """
    resolved_command = os.path.expanduser(str(command).strip())
    resolved_env = dict(env or {})

    if os.sep not in resolved_command:
        path_arg = resolved_env["PATH"] if "PATH" in resolved_env else None
        which_hit = shutil.which(resolved_command, path=path_arg)
        if which_hit:
            resolved_command = which_hit
        elif resolved_command in {"npx", "npm", "node"}:
            leanflow_home = os.path.expanduser(
                os.getenv("LEANFLOW_HOME", os.path.join(os.path.expanduser("~"), ".leanflow"))
            )
            candidates = [
                os.path.join(leanflow_home, "node", "bin", resolved_command),
                os.path.join(os.path.expanduser("~"), ".local", "bin", resolved_command),
            ]
            for candidate in candidates:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    resolved_command = candidate
                    break

    command_dir = os.path.dirname(resolved_command)
    if command_dir:
        resolved_env = _prepend_path(resolved_env, command_dir)

    return resolved_command, resolved_env


def _resolve_stdio_cwd(server_name: str, config: dict) -> str | None:
    configured = str(config.get("cwd", "") or "").strip()
    if configured:
        path = os.path.expanduser(configured)
        return path if os.path.isdir(path) else None

    if str(server_name or "").startswith("lean-"):
        project_root = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or "").strip()
        if project_root:
            path = os.path.expanduser(project_root)
            if os.path.isdir(path):
                return path
    return None


def _truthy_env_value(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _dispatch_local_loogle_allowed() -> bool:
    """Return whether this process may start a private local Loogle index.

    A research worker already owns an isolated Lean service tree. Starting local
    Loogle in every such tree eagerly materializes the same multi-gigabyte index
    once per concurrent lane. Keep the foreground behavior unchanged and require
    an explicit worker-only opt-in when a deployment has provisioned that memory.
    """
    if not _truthy_env_value(os.getenv("LEANFLOW_DISPATCH_WORKER")):
        return True
    return _truthy_env_value(os.getenv(_DISPATCH_LOCAL_LOOGLE_ENV))


def _apply_dispatch_local_loogle_policy(server_name: str, env: dict) -> dict:
    """Disable private local Loogle in a worker while retaining remote search."""
    updated = dict(env or {})
    if str(server_name or "") != "lean-lsp":
        return updated
    if not _truthy_env_value(updated.get("LEAN_LOOGLE_LOCAL")):
        return updated
    if _dispatch_local_loogle_allowed():
        return updated
    updated["LEAN_LOOGLE_LOCAL"] = "false"
    logger.info(
        "Local Loogle disabled in this research worker to avoid duplicating its "
        "multi-gigabyte index; remote Loogle and native text-search fallbacks remain enabled. "
        "Set %s=1 only for explicitly memory-provisioned workers.",
        _DISPATCH_LOCAL_LOOGLE_ENV,
    )
    return updated


def _research_local_loogle_allowed() -> bool:
    """Return whether a research foreground may retain local Loogle."""
    if not _truthy_env_value(os.getenv("LEANFLOW_RESEARCH_MODE")):
        return True
    return _truthy_env_value(os.getenv(_RESEARCH_LOCAL_LOOGLE_ENV))


def _apply_research_local_loogle_policy(server_name: str, env: dict) -> dict:
    """Disable only local Loogle in research mode while retaining lean-lsp."""
    updated = dict(env or {})
    if str(server_name or "") != "lean-lsp":
        return updated
    if not _truthy_env_value(updated.get("LEAN_LOOGLE_LOCAL")):
        return updated
    if _research_local_loogle_allowed():
        return updated
    updated["LEAN_LOOGLE_LOCAL"] = "false"
    logger.info(
        "Local Loogle disabled for this research campaign to avoid retaining its "
        "multi-gigabyte index; foreground lean-lsp, remote Loogle, and native "
        "search remain enabled. Set %s=1 only for an explicitly memory-provisioned run.",
        _RESEARCH_LOCAL_LOOGLE_ENV,
    )
    return updated


def _mcp_stderr_log_dir(cwd: str | None) -> Path:
    """Resolve the directory for per-server MCP stderr logs.

    Prefers the project-local ``.leanflow/workflow-state/mcp-logs`` (so logs sit
    next to the run that produced them); falls back to the managed home.
    """
    root = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or "").strip() or str(cwd or "").strip()
    if root:
        candidate = Path(root).expanduser()
        if candidate.is_dir():
            return candidate / ".leanflow" / "workflow-state" / "mcp-logs"
    home = Path(os.path.expanduser(os.getenv("LEANFLOW_HOME", os.path.join("~", ".leanflow"))))
    return home / "workflow-state" / "mcp-logs"


def open_mcp_stderr_log(server_name: str, cwd: str | None) -> TextIO | None:
    """Return a writable file for a stdio MCP server's stderr, or ``None`` to inherit ``sys.stderr``.

    The managed Lean MCP servers (notably ``lean-lsp``) emit a high volume of *benign*
    runtime logging. Every ``lean_multi_attempt`` candidate edits the file in the Lean
    language server, which supersedes the previous version and cancels the in-flight
    request — the server then logs ``the file worker for ... has been terminated`` (LSP
    error ``-32801``) plus an asyncio ``Future exception was never retrieved``. These are
    NOT LeanFlow failures: the tool call still returns ``success: True`` with its
    diagnostics. But ``stdio_client`` defaults ``errlog=sys.stderr``, so that chatter is
    piped straight into the workflow console where it reads as a wall of errors.

    Routing these servers' stderr to a per-server log file keeps the detail available for
    debugging (``.leanflow/workflow-state/mcp-logs/<server>.stderr.log``) without polluting
    the run. Genuine connection/startup failures are unaffected — they surface through the
    ``stdio_client`` exception path, independent of this stream.

    Only the managed Lean servers are redirected; other servers keep inheriting the console.
    Set ``LEANFLOW_MCP_STDERR_INHERIT=1`` to force every server back to ``sys.stderr``.
    """
    if _truthy_env_value(os.getenv("LEANFLOW_MCP_STDERR_INHERIT")):
        return None
    if not str(server_name or "").startswith("lean-"):
        return None
    try:
        log_dir = _mcp_stderr_log_dir(cwd)
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(server_name or "server")).strip("-")
        path = log_dir / f"{safe_name or 'server'}.stderr.log"
        return open(path, "a", encoding="utf-8", buffering=1)
    except Exception as exc:  # never let log plumbing break a connection
        logger.debug(
            "Could not open MCP stderr log for %s (%s); inheriting console stderr",
            server_name,
            exc,
        )
        return None


def _read_lean_toolchain_from_root(path: str | os.PathLike[str] | None) -> str:
    if not path:
        return ""
    try:
        root = Path(path).expanduser().resolve()
        return (root / "lean-toolchain").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _disable_incompatible_local_loogle(server_name: str, env: dict, cwd: str | None) -> dict:
    """Disable local Loogle when its cached Lean toolchain cannot read project .olean files."""
    if str(server_name or "") != "lean-lsp":
        return env
    if not _truthy_env_value((env or {}).get("LEAN_LOOGLE_LOCAL")):
        return env

    raw_cache_dir = str((env or {}).get("LEAN_LOOGLE_CACHE_DIR", "") or "").strip()
    if not raw_cache_dir:
        return env
    cache_dir = Path(raw_cache_dir).expanduser()
    loogle_toolchain = _read_lean_toolchain_from_root(cache_dir / "repo")
    project_root = str(
        (env or {}).get("LEAN_PROJECT_PATH")
        or (env or {}).get("LEANFLOW_PROJECT_ROOT")
        or cwd
        or ""
    ).strip()
    project_toolchain = _read_lean_toolchain_from_root(project_root)
    if not loogle_toolchain or not project_toolchain or loogle_toolchain == project_toolchain:
        return env

    updated = dict(env)
    updated["LEAN_LOOGLE_LOCAL"] = "false"
    logger.info(
        "Local Loogle disabled for this run: managed cache uses %s, project uses %s; "
        "remote Loogle fallback remains enabled.",
        loogle_toolchain,
        project_toolchain,
    )
    return updated


def _augment_lean_stdio_env(server_name: str, env: dict, cwd: str | None) -> dict:
    """Add project-local Lean runtime env expected by managed Lean MCP servers."""
    updated = _apply_dispatch_local_loogle_policy(server_name, env)
    updated = _apply_research_local_loogle_policy(server_name, updated)
    if not str(server_name or "").startswith("lean-") or not cwd:
        return updated

    updated.setdefault("LEAN_PROJECT_PATH", cwd)
    updated.setdefault("LEANFLOW_PROJECT_ROOT", cwd)

    # Point the lean-lsp server's local Loogle at a per-toolchain cache dir keyed on the
    # project's Lean toolchain, so switching projects on different toolchains does not thrash
    # a single shared Loogle build. The builder + status resolve the SAME dir via
    # mcp_bootstrap.managed_loogle_cache_dir(toolchain=...); the slug convention here MUST
    # match mcp_bootstrap.loogle_toolchain_slug. Only rewrites the generic ``loogle`` base —
    # an already per-toolchain or custom path is left untouched.
    if str(server_name or "") == "lean-lsp":
        toolchain = _read_lean_toolchain_from_root(cwd)
        base = str(updated.get("LEAN_LOOGLE_CACHE_DIR", "") or "").strip()
        if toolchain and base:
            base_path = Path(base).expanduser()
            if base_path.name == "loogle":
                slug = re.sub(r"[^A-Za-z0-9._-]+", "-", toolchain.strip()).strip("-")
                if slug:
                    updated["LEAN_LOOGLE_CACHE_DIR"] = str(base_path.parent / f"loogle-{slug}")
    return updated


def _repair_loogle_cache_if_needed(server_name: str, env: dict) -> None:
    """Repair stale local Loogle cache artifacts before the MCP server starts."""
    if str(server_name or "") != "lean-lsp":
        return
    if not _truthy_env_value((env or {}).get("LEAN_LOOGLE_LOCAL")):
        return
    cache_dir = Path(str((env or {}).get("LEAN_LOOGLE_CACHE_DIR", "") or "")).expanduser()
    if not cache_dir:
        return
    repo_dir = cache_dir / "repo"
    binary = repo_dir / ".lake" / "build" / "bin" / ("loogle.exe" if os.name == "nt" else "loogle")
    if not binary.is_file():
        return

    packages_dir = repo_dir / ".lake" / "packages"
    if not packages_dir.is_dir():
        return

    missing_modules: list[str] = []
    for package_dir in sorted(packages_dir.iterdir(), key=lambda item: item.name):
        if not package_dir.is_dir():
            continue
        build_lib = package_dir / ".lake" / "build" / "lib" / "lean"
        for source in package_dir.rglob("*.lean"):
            try:
                relative = source.relative_to(package_dir)
            except ValueError:
                continue
            if relative.parts and relative.parts[0] in {".lake", ".git"}:
                continue
            if not (build_lib / relative.with_suffix(".olean")).is_file():
                module = ".".join(relative.with_suffix("").parts)
                valid_module = all(
                    _LEAN_MODULE_PART_PATTERN.match(part) for part in relative.with_suffix("").parts
                )
                if valid_module and "Test" not in module and module not in missing_modules:
                    missing_modules.append(module)
                if len(missing_modules) >= _LOOGLE_STALE_ARTIFACT_SCAN_LIMIT:
                    break
        if len(missing_modules) >= _LOOGLE_STALE_ARTIFACT_SCAN_LIMIT:
            break
    if not missing_modules:
        return

    try:
        result = subprocess.run(
            ["lake", "build", *missing_modules],
            cwd=str(repo_dir),
            env=dict(os.environ, LAKE_ARTIFACT_CACHE="false"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=900,
            check=False,
        )
    except Exception as exc:
        logger.warning("Failed to repair stale local Loogle cache: %s", exc)
        return
    if result.returncode != 0:
        logger.warning(
            "Failed to repair stale local Loogle cache (exit %s): %s",
            result.returncode,
            ((result.stderr or result.stdout or "").strip())[:500],
        )


def _effective_connect_timeout(name: str, config: dict) -> float:
    try:
        timeout = float(config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT))
    except Exception:
        timeout = float(_DEFAULT_CONNECT_TIMEOUT)
    env = config.get("env")
    if (
        str(name or "") == "lean-lsp"
        and isinstance(env, dict)
        and _truthy_env_value(env.get("LEAN_LOOGLE_LOCAL"))
        and _dispatch_local_loogle_allowed()
        and _research_local_loogle_allowed()
    ):
        timeout = max(timeout, float(_LOCAL_LOOGLE_CONNECT_TIMEOUT))
    return timeout


def _format_connect_error(exc: BaseException) -> str:
    """Render nested MCP connection errors into an actionable short message."""

    def _find_missing(current: BaseException) -> str | None:
        nested = getattr(current, "exceptions", None)
        if nested:
            for child in nested:
                missing = _find_missing(child)
                if missing:
                    return missing
            return None
        if isinstance(current, FileNotFoundError):
            if getattr(current, "filename", None):
                return str(current.filename)
            match = re.search(r"No such file or directory: '([^']+)'", str(current))
            if match:
                return match.group(1)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                missing = _find_missing(nested_exc)
                if missing:
                    return missing
        return None

    def _flatten_messages(current: BaseException) -> list[str]:
        nested = getattr(current, "exceptions", None)
        if nested:
            flattened: list[str] = []
            for child in nested:
                flattened.extend(_flatten_messages(child))
            return flattened
        messages = []
        text = str(current).strip()
        if text:
            messages.append(text)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                messages.extend(_flatten_messages(nested_exc))
        return messages or [current.__class__.__name__]

    missing = _find_missing(exc)
    if missing:
        message = f"missing executable '{missing}'"
        if os.path.basename(missing) in {"npx", "npm", "node"}:
            message += (
                " (ensure Node.js is installed and PATH includes its bin directory, "
                "or set mcp_servers.<name>.command to an absolute path and include "
                "that directory in mcp_servers.<name>.env.PATH)"
            )
        return _sanitize_error(message)

    deduped: list[str] = []
    for item in _flatten_messages(exc):
        if item not in deduped:
            deduped.append(item)
    return _sanitize_error("; ".join(deduped[:3]))
