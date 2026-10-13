"""Fetch and retain the exact reviewed LeanProbe source for standalone installs."""

from __future__ import annotations

import fcntl
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.home import leanflow_home
from leanflow_cli.runtime.install_inputs import companion_identity, resolve_lean_probe_source

LEAN_PROBE_URL = "https://github.com/epfl-lara/LeanProbe.git"
LEAN_PROBE_COMMIT = "0252843eae96cd24601dc999626d5f262337d221"


def _git(*arguments: str, cwd: Path) -> str:
    """Run a bounded source operation without interactive credential prompts."""
    import os

    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *arguments],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )
    return result.stdout.strip()


def fetch_pinned_lean_probe() -> Path:
    """Reuse verified source or fetch the pinned commit before preparing a runtime."""
    root = leanflow_home().resolve() / "vendor" / "lean-probe-source"
    root.mkdir(parents=True, exist_ok=True)
    source = root / LEAN_PROBE_COMMIT
    receipt = root / f"{LEAN_PROBE_COMMIT}.sha256"
    with (root / "install.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if source.exists() or source.is_symlink():
            if source.is_symlink() or receipt.is_symlink():
                raise RuntimeError("Pinned LeanProbe source or its receipt is a symbolic link.")
            try:
                valid = (
                    _git("rev-parse", "HEAD", cwd=source) == LEAN_PROBE_COMMIT
                    and companion_identity(source) == receipt.read_text().strip()
                )
                resolve_lean_probe_source(source.parent, source)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"Cannot validate pinned LeanProbe source at {source}.") from exc
            if not valid:
                raise RuntimeError(
                    f"Pinned LeanProbe source at {source} changed. "
                    "Use --lean-probe-source with a matching clean checkout."
                )
            return source
        staging = Path(tempfile.mkdtemp(prefix="fetch-", dir=root))
        try:
            _git("init", "--quiet", cwd=staging)
            _git("fetch", "--quiet", "--depth", "1", LEAN_PROBE_URL, LEAN_PROBE_COMMIT, cwd=staging)
            _git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=staging)
            if _git("rev-parse", "HEAD", cwd=staging) != LEAN_PROBE_COMMIT:
                raise RuntimeError("Fetched LeanProbe revision differs from the installation pin.")
            resolve_lean_probe_source(staging.parent, staging)
            digest = companion_identity(staging)
            receipt.write_text(digest + "\n")
            staging.rename(source)
            return source
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            # A transport helper may outlive Git; retain its private staging path.
            raise RuntimeError(
                f"LeanProbe download stopped; retained temporary source at {staging}. "
                "Retry or supply --lean-probe-source PATH."
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            shutil.rmtree(staging)
            raise RuntimeError(
                "Cannot download the pinned LeanProbe source. Check Git/network access "
                "or supply --lean-probe-source PATH."
            ) from exc
        except BaseException:
            shutil.rmtree(staging)
            raise
