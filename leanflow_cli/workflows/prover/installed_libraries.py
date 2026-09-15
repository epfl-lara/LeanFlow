"""Accept offline dependency requests only when an exact locked checkout already exists."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.libraries import (
    _configuration,
    _entries,
    _lock_packages,
    _validate_layout,
)


def require_installed_libraries(root: Path, entries: list[dict[str, Any]]) -> None:
    """Validate an already-satisfied request without networking or filesystem writes.

    The manifest's resolved revision is authoritative even when the lakefile
    names a tag. A missing package or changed identity still requires explicit
    online installation; accepting it here would silently bypass offline policy.
    """
    try:
        validated = _entries(entries, time.monotonic() + 10, verify_remote=False)
        root = root.resolve()
        _validate_layout(root)
        _configuration(root)
        manifest = root / "lake-manifest.json"
        if manifest.is_symlink():
            raise ValueError("The dependency manifest cannot be a symlink")
        locked = _lock_packages(manifest.read_bytes())
        for entry in validated:
            name = entry["name"]
            package = locked.get(name, {})
            if (
                package.get("type") != "git"
                or package.get("rev") != entry["rev"]
                or package.get("url") != entry["git"]
            ):
                raise ValueError(f"Library {name} does not match an existing locked dependency")
            checkout = root / ".lake/packages" / name
            if not checkout.is_dir():
                raise ValueError(f"Library {name} has no installed checkout")
            # Ignore inherited Git directory overrides and disable optional
            # writes. rev-parse reads local metadata; it cannot fetch packages.
            result = subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "-C",
                    str(checkout),
                    "rev-parse",
                    "--show-toplevel",
                    "HEAD",
                ],
                env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            lines = result.stdout.strip().splitlines()
            if (
                result.returncode != 0
                or len(lines) != 2
                or Path(lines[0]).resolve() != checkout.resolve()
                or lines[1] != entry["rev"]
            ):
                raise ValueError(f"Library {name} checkout differs from the requested revision")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise ValueError(
            "Internet access is disabled; use only already installed libraries. "
            f"{error}. Omit dependency installation requests (libraries: []) when no new library is needed."
        ) from error
