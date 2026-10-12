"""Store setup receipts outside projects and invalidate stale readiness claims."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from leanflow_cli.workflows.project_setup.readiness import installed_environment
from leanflow_cli.workflows.project_setup.scaffold import project_root, source_paths
from leanflow_cli.workflows.project_setup.verification import repl_binary


def state_directory(root: Path) -> Path:
    """Return the home-owned state location without creating it."""
    key = hashlib.sha256(str(root).encode()).hexdigest()[:24]
    return leanflow_home() / "setup" / key


def fingerprint(root: Path) -> str:
    """Hash dependency configuration and the executable used by readiness checking."""
    digest = hashlib.sha256()
    for name in ("lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json"):
        path = root / name
        digest.update(name.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    packages = root / ".lake/packages"
    if packages.is_dir():
        for package in sorted(packages.iterdir()):
            digest.update(package.name.encode())
            for marker in (
                package,
                package / ".lake/build",
                package / ".lake/build/lib/lean/Mathlib.olean",
            ):
                digest.update(
                    str(marker.is_dir() if marker.suffix != ".olean" else marker.is_file()).encode()
                )
    binary = repl_binary(root)
    if binary is not None:
        stat = binary.stat()
        digest.update(f"{binary}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def inspect_project(path: str | Path) -> dict[str, Any]:
    """Describe project setup without creating state or launching processes."""
    root = project_root(path)
    existing = (root / "lakefile.toml").is_file() or (root / "lakefile.lean").is_file()
    kind = (
        "existing"
        if existing
        else "standalone" if root.is_dir() and source_paths(root) else "empty"
    )
    receipt: dict[str, Any] = {}
    try:
        value = json.loads((state_directory(root) / "result.json").read_text(encoding="utf-8"))
        if isinstance(value, dict):
            receipt = value
    except (OSError, ValueError):
        pass
    try:
        valid = receipt.get("fingerprint") == fingerprint(root)
    except OSError:
        valid = False
    installed = installed_environment(root)
    if receipt and (not valid or (receipt.get("status") == "ready" and not installed)):
        receipt.update(
            status="failed",
            phase="checking_readiness",
            code="readiness_stale",
            message="Project configuration or verification artifacts changed since setup.",
            remediation="Run project setup again to check and repair the environment.",
        )
    ready = bool(valid and receipt.get("status") == "ready" and installed)
    return {
        "root": str(root),
        "kind": kind,
        "ready": ready,
        "configured": (root / ".leanflow/project.yaml").is_file(),
        "setup_status": receipt.get("status", "detected" if installed else "unknown"),
        "phase": receipt.get("phase", ""),
        "code": receipt.get("code", ""),
        "message": receipt.get(
            "message",
            (
                "Existing Lean project and verification tools detected. You can start a run or check setup."
                if installed
                else ""
            ),
        ),
        "remediation": receipt.get("remediation", ""),
        "log_path": receipt.get("log_path", ""),
        "capabilities": receipt.get("capabilities", {}) if ready else {},
    }


def save_result(root: Path, result: dict[str, Any]) -> None:
    """Atomically persist the latest setup outcome without changing project files."""
    state = state_directory(root)
    result.setdefault("fingerprint", fingerprint(root))
    pending = state / f"{uuid.uuid4().hex}.json"
    pending.write_text(json.dumps(result, indent=2), encoding="utf-8")
    pending.replace(state / "result.json")
