"""Acquire bounded repositories for managed research workflows.

Shallow-clone a repository into ``<cwd>/.leanflow/workspace/repos/`` so
research workers can inspect concrete proof developments locally. The tool
uses sanitized destinations, rejects symlink escapes, enforces a post-clone
size cap, cleans up failed acquisitions, and returns normalized JSON.

Layering: stdlib + ``tools.response`` + ``tools.registry`` only — no
``leanflow_cli`` imports.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from tools.response import dumps, error
from tools.utilities.repository_research_policy import repository_research_disabled

REPO_CLONE_DIRNAME = ".leanflow/workspace/repos"
REPO_CLONE_MAX_BYTES = 500 * 1024 * 1024  # post-clone size cap (bytes)
REPO_CLONE_TIMEOUT_SECONDS = 180

_ALLOWED_SCHEMES = {"https", "git"}
# `--branch` values reach git argv: keep them boring (tag/branch/short-sha).
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")


def _safe_repo_dirname(url: str, override: str) -> str:
    """Directory basename for the clone; sanitized like web_download names."""
    name = (override or "").strip()
    if not name:
        tail = os.path.basename(urlparse(url).path.rstrip("/"))
        name = tail[: -len(".git")] if tail.endswith(".git") else tail
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "repo"
    return name[:200]


def _tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            try:
                total += (Path(dirpath) / filename).stat().st_size
            except OSError:
                continue
    return total


def _clone_timeout_seconds() -> int:
    """Return the bounded clone timeout, honoring the LeanFlow env contract."""
    raw = str(os.getenv("LEANFLOW_REPO_CLONE_TIMEOUT_SECONDS", "") or "").strip()
    try:
        value = int(raw) if raw else REPO_CLONE_TIMEOUT_SECONDS
    except ValueError:
        value = REPO_CLONE_TIMEOUT_SECONDS
    return max(5, min(600, value))


def repo_clone_tool(
    url: str, name: str = "", ref: str = "", max_bytes: int = REPO_CLONE_MAX_BYTES
) -> str:
    """Shallow single-branch clone into the project's repos workspace."""
    if repository_research_disabled():
        return error("Repository cloning is disabled for this clean-room run")
    url = (url or "").strip()
    if not url:
        return error("repo_clone requires a non-empty 'url'")
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return error(f"repo_clone only accepts {sorted(_ALLOWED_SCHEMES)} URLs, got {url!r}")
    ref = (ref or "").strip()
    if ref and not _SAFE_REF_RE.match(ref):
        return error(f"repo_clone refuses suspicious ref {ref!r}")
    try:
        cap = int(max_bytes)
    except (TypeError, ValueError):
        cap = REPO_CLONE_MAX_BYTES
    env_cap = os.getenv("LEANFLOW_REPO_CLONE_MAX_BYTES", "")
    if env_cap:
        try:
            cap = int(env_cap)
        except ValueError:
            pass
    cap = max(1, cap)

    project_root = Path.cwd().resolve()
    dest_dir = (project_root / REPO_CLONE_DIRNAME).resolve()
    # A symlinked repos dir resolving outside the project would defeat the
    # per-clone parent check below — refuse up front (web_download pattern).
    if project_root != dest_dir and project_root not in dest_dir.parents:
        return error("Refusing to clone: repos directory escapes the project sandbox")
    dest = (dest_dir / _safe_repo_dirname(url, name)).resolve()
    if dest.parent != dest_dir:
        return error("Refusing to clone outside the repos directory")

    if dest.exists():
        if not (dest / ".git").exists():
            # Never adopt (or later rmtree) a directory this tool didn't create.
            return error(
                f"Destination {dest.name!r} already exists and is not a git clone; "
                "pick a different 'name'"
            )
        origin = _git_out(dest, "config", "--get", "remote.origin.url")
        if origin != url:
            # Missing/unreadable origin is a mismatch too — never vacuously
            # hand an unidentified repository back as a cache hit.
            return error(
                f"Destination {dest.name!r} already holds a clone of "
                f"{origin or '<no origin>'!r}; pick a different 'name'"
            )
        if ref:
            current = _git_out(dest, "rev-parse", "--abbrev-ref", "HEAD")
            if current == "HEAD":  # detached (tag clone): resolve the tag name
                current = _git_out(dest, "describe", "--tags", "--exact-match") or current
            if current != ref:
                return error(
                    f"Destination {dest.name!r} is checked out at {current!r}, not the "
                    f"requested ref {ref!r}; pick a different 'name'"
                )
        return dumps(
            {
                "success": True,
                "url": url,
                "path": str(dest),
                "bytes": _tree_bytes(dest),
                "ref": ref,
                "head_commit": _git_out(dest, "rev-parse", "HEAD"),
                "cached": True,
            }
        )
    dest_dir.mkdir(parents=True, exist_ok=True)

    argv = ["git", "clone", "--depth", "1", "--single-branch"]
    if ref:
        argv += ["--branch", ref]
    argv += ["--", url, str(dest)]
    timeout_s = _clone_timeout_seconds()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        return error(f"git clone timed out after {timeout_s}s for {url}")
    except Exception as exc:
        shutil.rmtree(dest, ignore_errors=True)
        return error(f"Failed to clone {url}: {exc}")
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        return error(f"git clone failed for {url}: {detail}")

    total = _tree_bytes(dest)
    if total > cap:
        shutil.rmtree(dest, ignore_errors=True)
        return error(f"Clone is {total} bytes, over the {cap}-byte cap; removed")
    return dumps(
        {
            "success": True,
            "url": url,
            "path": str(dest),
            "bytes": total,
            "ref": ref,
            "head_commit": _git_out(dest, "rev-parse", "HEAD"),
            "cached": False,
        }
    )


def _git_out(repo_dir: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def check_repo_clone_available() -> bool:
    """repo_clone needs a working git binary."""
    return not repository_research_disabled() and shutil.which("git") is not None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry  # noqa: E402

REPO_CLONE_SCHEMA = {
    "name": "repo_clone",
    "description": (
        "Shallow-clone a git repository (https/git URL) into "
        ".leanflow/workspace/repos/<name> for local inspection — proof "
        "developments, mathlib forks, paper artifacts. Depth-1, single "
        "branch, no submodules; size-capped (500MB default); re-cloning an "
        "existing target returns it with cached=true. Returns {path, bytes, "
        "ref, head_commit, cached}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Repository URL (https:// or git://)."},
            "name": {
                "type": "string",
                "description": "Optional destination directory name (basename only; sanitized).",
            },
            "ref": {
                "type": "string",
                "description": "Optional branch or tag to clone (git clone --branch).",
            },
        },
        "required": ["url"],
    },
}

registry.register(
    name="repo_clone",
    toolset="web",
    schema=REPO_CLONE_SCHEMA,
    handler=lambda args, **kw: repo_clone_tool(
        args.get("url", ""), name=args.get("name", ""), ref=args.get("ref", "")
    ),
    check_fn=check_repo_clone_available,
    requires_env=[],
    emoji="🌱",
)
