"""Project-toolchain-matched local Loogle: build, locking, and lifecycle patches.

lean-lsp-mcp ships Loogle pinned to Loogle's OWN Lean toolchain, but LeanFlow only enables
local Loogle when that toolchain matches the active project's — which it almost never does,
so local Loogle stays ``incompatible`` and search silently falls back to remote. This module
owns the fix: resolve a per-toolchain cache dir, (re)pin Loogle to the project's toolchain and
build it (under an exclusive lock so concurrent builders never collide), and gate that work
behind a fast no-build check. It also patches the managed lean-lsp-mcp environment so timed-out
and completed sessions reap their Loogle subprocess. The low-level primitives it builds on (``managed_loogle_cache_dir``,
``local_loogle_supported``, ``_read_lean_toolchain``, …) live in :mod:`leanflow_cli.cli.mcp_bootstrap`;
the lean-lsp server is pointed at the SAME per-toolchain dir by
``tools.mcp.mcp_transport._augment_lean_stdio_env``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from leanflow_cli.cli.mcp_bootstrap import (
    _lean_lsp_env_from_home,
    _read_lean_toolchain,
    _truthy,
    local_loogle_supported,
    managed_loogle_cache_dir,
)
from leanflow_cli.config import get_leanflow_home

_LOOGLE_REPO_URL = "https://github.com/nomeata/loogle.git"


def _active_loogle_cache_dir(home_path: Path) -> Path:
    """Resolve the Loogle cache dir the runtime actually uses (config value, else default)."""
    raw = str(_lean_lsp_env_from_home(home_path).get("LEAN_LOOGLE_CACHE_DIR", "") or "").strip()
    return Path(raw).expanduser() if raw else managed_loogle_cache_dir(home_path)


def _loogle_binary_path(repo_dir: Path) -> Path:
    return repo_dir / ".lake" / "build" / "bin" / ("loogle.exe" if os.name == "nt" else "loogle")


def loogle_cache_dir_for_project(
    home_path: Path, project_root: str | os.PathLike[str] | None
) -> Path:
    """Resolve the per-toolchain Loogle cache dir for a project.

    Keyed on the project's Lean toolchain so different toolchains get isolated builds (no
    rebuild thrash when switching projects). Falls back to the config/generic dir when the
    project toolchain is unknown. The lean-lsp server is pointed at the SAME dir by
    ``_augment_lean_stdio_env``, so build, server, and status stay consistent.
    """
    tc = _read_lean_toolchain(project_root)
    if tc:
        return managed_loogle_cache_dir(home_path, toolchain=tc)
    return _active_loogle_cache_dir(home_path)


def _loogle_build_lock_path(cache_dir: Path) -> Path:
    return cache_dir / ".loogle-build.lock"


def local_loogle_needs_build(
    project_root: str | os.PathLike[str] | None, home: str | os.PathLike[str] | None = None
) -> bool:
    """Fast check (no build): does local Loogle need a (re)build for this project's toolchain?

    True only when local Loogle is supported, enabled in config, a project toolchain is
    known, git+lake are present, and the cached Loogle is either missing or was built for
    a *different* toolchain. False otherwise (including when a build couldn't succeed), so
    callers can gate work without ever shelling out.
    """
    if not local_loogle_supported():
        return False
    project_tc = _read_lean_toolchain(project_root)
    if not project_tc:
        return False
    if not (shutil.which("git") and shutil.which("lake")):
        return False
    home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
    if not _truthy(_lean_lsp_env_from_home(home_path).get("LEAN_LOOGLE_LOCAL")):
        return False
    repo_dir = loogle_cache_dir_for_project(home_path, project_root) / "repo"
    if not _loogle_binary_path(repo_dir).is_file():
        return True
    return _read_lean_toolchain(repo_dir) != project_tc


def ensure_local_loogle_for_project(
    project_root: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None = None,
    *,
    timeout: int = 1200,
) -> dict[str, Any]:
    """Build managed local Loogle against the PROJECT's Lean toolchain (idempotent).

    Repins Loogle's ``lean-toolchain`` to the project's and rebuilds the binary so the two
    match and local Loogle actually activates (instead of falling back to remote). Loogle has
    no Mathlib dependency, so the build is light (~1-2 min) and cached until the project
    toolchain changes. Serialized by an exclusive lock on ``<cache>/.loogle-build.lock`` —
    the same file the patched lean-lsp-mcp server takes — so two builders never run
    ``lake build`` in the same repo at once.

    Best-effort: returns a status dict and never raises. A no-op (``action="already-built"``)
    when the binary already exists and was built for the project's toolchain.
    """
    result: dict[str, Any] = {"ok": False, "action": "skipped", "reason": "", "toolchain": ""}
    try:
        if not local_loogle_supported():
            result["reason"] = "unsupported-platform"
            return result
        project_tc = _read_lean_toolchain(project_root)
        if not project_tc:
            result["reason"] = "no-project-toolchain"
            return result
        if not (shutil.which("git") and shutil.which("lake")):
            result["reason"] = "missing-git-or-lake"
            return result
        home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
        cache_dir = loogle_cache_dir_for_project(home_path, project_root)
        repo_dir = cache_dir / "repo"
        binary = _loogle_binary_path(repo_dir)
        result.update(toolchain=project_tc, cache_dir=str(cache_dir))

        if binary.is_file() and _read_lean_toolchain(repo_dir) == project_tc:
            result.update(ok=True, action="already-built")
            return result

        cache_dir.mkdir(parents=True, exist_ok=True)
        # Exclusive build lock: serialize with any other builder of THIS cache dir — a
        # concurrent LeanFlow workflow or the lean-lsp-mcp server (which is patched to take
        # the same lock). Whoever gets the lock builds; the others re-check is_installed and
        # skip. fcntl is POSIX-only, which is fine: local Loogle is gated to non-Windows.
        import fcntl

        lock_handle = open(_loogle_build_lock_path(cache_dir), "w", encoding="utf-8")
        try:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_handle, fcntl.LOCK_EX)
            # Re-check after acquiring: another builder may have just finished.
            if binary.is_file() and _read_lean_toolchain(repo_dir) == project_tc:
                result.update(ok=True, action="already-built")
                return result

            has_lakefile = (repo_dir / "lakefile.lean").exists() or (
                repo_dir / "lakefile.toml"
            ).exists()
            if not has_lakefile:
                clone = subprocess.run(
                    ["git", "clone", "--depth", "1", _LOOGLE_REPO_URL, str(repo_dir)],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if clone.returncode != 0:
                    result.update(
                        reason="clone-failed", detail=((clone.stderr or clone.stdout) or "")[:400]
                    )
                    return result

            # Pin Loogle to the project's toolchain, then force a rebuild of the binary so it
            # is compiled with that exact toolchain (and can therefore read the project oleans).
            (repo_dir / "lean-toolchain").write_text(project_tc + "\n", encoding="utf-8")
            with contextlib.suppress(OSError):
                binary.unlink()
            build = subprocess.run(
                ["lake", "build"],
                cwd=str(repo_dir),
                env={**os.environ, "LAKE_ARTIFACT_CACHE": "false"},
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if build.returncode != 0 or not binary.is_file():
                result.update(
                    reason="build-failed", detail=((build.stderr or build.stdout) or "")[-400:]
                )
                return result
            result.update(ok=True, action="built")
            return result
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()
    except Exception as exc:  # never break callers; remote Loogle remains the fallback
        result["reason"] = f"error: {exc}"
        return result


def ensure_local_loogle_for_project_async(
    project_root: str | os.PathLike[str] | None, home: str | os.PathLike[str] | None = None
) -> bool:
    """Kick off :func:`ensure_local_loogle_for_project` in a detached background process.

    Returns True if a build was launched, False when no build was needed (the common
    steady state) or one could not be started. Non-blocking: the current run proceeds and
    uses remote Loogle while the build is in flight; the next run picks up local Loogle.
    """
    try:
        if not local_loogle_needs_build(project_root, home):
            return False
        home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
        log_dir = loogle_cache_dir_for_project(home_path, project_root)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_dir / "loogle-build.log", "a", encoding="utf-8")
        try:
            code = (
                "import sys;"
                "from leanflow_cli.cli.loogle_local import ensure_local_loogle_for_project as e;"
                "print(e(sys.argv[1] or None, sys.argv[2] or None))"
            )
            subprocess.Popen(  # noqa: S603 - fixed argv, detached best-effort build
                [sys.executable, "-c", code, str(project_root or ""), str(home or "")],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        return True
    except Exception:
        return False


def patch_lean_lsp_loogle_build_lock(venv_dir: Path) -> bool:
    """Patch lean-lsp-mcp so its Loogle build takes the same lock LeanFlow uses.

    Without this, the lean-lsp-mcp server could run ``lake build`` in the Loogle repo at the
    same time as LeanFlow's managed build, corrupting the build dir. Wrapping ``_build_loogle``
    in an exclusive flock on ``<cache_dir>/.loogle-build.lock`` (the same file LeanFlow locks)
    makes the two serialize: whoever wins builds, the loser re-checks ``is_installed`` and skips.
    Idempotent via the ``_leanflow_build_loogle_inner`` marker.
    """
    candidates = [
        *venv_dir.glob("lib/python*/site-packages/lean_lsp_mcp/loogle.py"),
        venv_dir / "Lib" / "site-packages" / "lean_lsp_mcp" / "loogle.py",
    ]
    target = next((candidate for candidate in candidates if candidate.is_file()), None)
    if target is None:
        return False
    text = target.read_text(encoding="utf-8")
    if "_leanflow_build_loogle_inner" in text:
        return True
    needle = (
        "    def _build_loogle(self) -> bool:\n"
        "        if self.is_installed:\n"
        "            return True\n"
    )
    if needle not in text:
        return False
    replacement = (
        "    def _build_loogle(self) -> bool:\n"
        "        import fcntl\n"
        "        self.cache_dir.mkdir(parents=True, exist_ok=True)\n"
        '        _lf_lock = open(self.cache_dir / ".loogle-build.lock", "w")\n'
        "        try:\n"
        "            try:\n"
        "                fcntl.flock(_lf_lock, fcntl.LOCK_EX)\n"
        "            except Exception:\n"
        "                pass\n"
        "            return self._leanflow_build_loogle_inner()\n"
        "        finally:\n"
        "            try:\n"
        "                fcntl.flock(_lf_lock, fcntl.LOCK_UN)\n"
        "            except Exception:\n"
        "                pass\n"
        "            _lf_lock.close()\n"
        "\n"
        "    def _leanflow_build_loogle_inner(self) -> bool:\n"
        "        if self.is_installed:\n"
        "            return True\n"
    )
    target.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
    return True


def patch_lean_lsp_loogle_lifecycle(venv_dir: Path) -> bool:
    """Patch lean-lsp-mcp to reap local Loogle on startup failure and session exit.

    Upstream keeps the local Loogle process in a shared singleton, but its
    startup-timeout path leaves the subprocess running and the stdio lifespan
    does not stop a successfully started singleton.  Both paths orphan a large
    search process after a LeanFlow run exits.  Patch the managed, isolated MCP
    environment so timeout and lifespan teardown both await ``stop()``.
    """
    package_dirs = [
        *venv_dir.glob("lib/python*/site-packages/lean_lsp_mcp"),
        venv_dir / "Lib" / "site-packages" / "lean_lsp_mcp",
    ]
    package_dir = next((candidate for candidate in package_dirs if candidate.is_dir()), None)
    if package_dir is None:
        return False

    loogle_path = package_dir / "loogle.py"
    server_path = package_dir / "server.py"
    if not loogle_path.is_file() or not server_path.is_file():
        return False

    loogle_text = loogle_path.read_text(encoding="utf-8")
    timeout_marker = "# LeanFlow: reap Loogle after startup timeout"
    if timeout_marker not in loogle_text:
        timeout_needle = (
            "        except asyncio.TimeoutError:\n"
            '            logger.error("Loogle startup timeout")\n'
            "            return False\n"
        )
        timeout_replacement = (
            "        except asyncio.TimeoutError:\n"
            f"            {timeout_marker}\n"
            '            logger.error("Loogle startup timeout")\n'
            "            await self.stop()\n"
            "            return False\n"
        )
        if timeout_needle not in loogle_text:
            return False
        loogle_text = loogle_text.replace(timeout_needle, timeout_replacement, 1)

    server_text = server_path.read_text(encoding="utf-8")
    teardown_marker = "# LeanFlow: stop the shared local Loogle with the stdio session"
    if teardown_marker not in server_text:
        teardown_needle = (
            '        logger.info("Session ending — cleaning up per-session resources")\n' "\n"
        )
        teardown_replacement = (
            '        logger.info("Session ending — cleaning up per-session resources")\n'
            "\n"
            f"        {teardown_marker}\n"
            "        if context and context.loogle_manager:\n"
            "            try:\n"
            "                await context.loogle_manager.stop()\n"
            "            except Exception:\n"
            '                logger.exception("Local Loogle close failed during app_lifespan teardown")\n'
            "\n"
        )
        if teardown_needle not in server_text:
            return False
        server_text = server_text.replace(teardown_needle, teardown_replacement, 1)

    loogle_path.write_text(loogle_text, encoding="utf-8")
    server_path.write_text(server_text, encoding="utf-8")
    return True
