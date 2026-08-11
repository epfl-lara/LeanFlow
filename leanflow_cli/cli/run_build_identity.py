"""Hash Lean toolchain and Lake dependency/build configuration inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _optional_file_identity(path: Path) -> dict[str, str]:
    """Return a digest and status for one optional build-configuration file."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"status": "missing", "sha256": _sha256_bytes(b"")}
    except OSError:
        return {"status": "unreadable", "sha256": _sha256_bytes(b"")}
    return {"status": "complete", "sha256": _sha256_bytes(raw)}


def collect_build_configuration_identity(
    project_root: Path,
    *,
    lean_toolchain: str,
    toolchain_status: str,
    dependency_manifest_sha256: str,
    dependency_manifest_status: str,
    dependencies_complete: bool,
) -> dict[str, Any]:
    """Return a sealed identity for behavior-defining Lean/Lake inputs."""
    lakefile_lean = _optional_file_identity(project_root / "lakefile.lean")
    lakefile_toml = _optional_file_identity(project_root / "lakefile.toml")
    payload = {
        "toolchain_status": toolchain_status,
        "lean_toolchain_sha256": _sha256_bytes(
            lean_toolchain.encode("utf-8", errors="surrogatepass")
        ),
        "dependency_manifest_status": dependency_manifest_status,
        "dependency_manifest_sha256": dependency_manifest_sha256,
        "lakefile_lean_status": lakefile_lean["status"],
        "lakefile_lean_sha256": lakefile_lean["sha256"],
        "lakefile_toml_status": lakefile_toml["status"],
        "lakefile_toml_sha256": lakefile_toml["sha256"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    complete = bool(
        toolchain_status == "complete"
        and dependency_manifest_status == "complete"
        and dependencies_complete
        and lakefile_lean["status"] in {"complete", "missing"}
        and lakefile_toml["status"] in {"complete", "missing"}
    )
    return {
        **payload,
        "build_configuration_sha256": _sha256_bytes(encoded),
        "build_configuration_complete": complete,
    }
