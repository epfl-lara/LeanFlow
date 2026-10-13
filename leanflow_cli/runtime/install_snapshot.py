"""Capture runtime inputs and isolate wheel packaging from checkout build artifacts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.runtime.install_identity import source_identity, source_package_files
from leanflow_cli.runtime.install_inputs import companion_identity

INPUT_RECEIPT = ".leanflow-install-inputs.json"


def captured_install_inputs(venv: Path) -> dict[str, Any]:
    """Read managed provenance or the receipt owned by a fresh explicit environment."""
    for path, kind in (
        (venv / INPUT_RECEIPT, "runtime-inputs"),
        (venv.parent / ".leanflow-storage.json", "runtime"),
    ):
        if path.is_symlink():
            raise RuntimeError(f"Linked installation input receipt: {path}")
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("kind") == kind:
            return payload
    raise RuntimeError(
        "Missing captured installation inputs; choose a fresh environment and rerun."
    )


def write_explicit_input_receipt(venv: Path, payload: dict[str, Any]) -> None:
    """Claim a fresh caller-selected directory and persist its source identity."""
    venv.mkdir(parents=True, exist_ok=False)
    atomic_json_write(venv / INPUT_RECEIPT, {**payload, "kind": "runtime-inputs"})


def materialize_leanflow_input(venv: Path) -> Path:
    """Build wheels from fresh inputs so deleted modules cannot survive in build/lib."""
    payload = captured_install_inputs(venv)
    original = payload.get("source_root")
    identity = payload.get("identity")
    if not isinstance(original, str) or not isinstance(identity, dict):
        raise RuntimeError("Invalid captured LeanFlow package inputs")
    source = Path(original)
    mode = str(payload.get("install_mode", "wheel"))
    if mode != "wheel":
        raise RuntimeError("Editable installations must retain their original source path")
    if payload.get("local_search"):
        mode += ":local-search"
    companion = payload.get("lean_probe_source")
    companion_path = Path(companion) if isinstance(companion, str) and companion else None
    expected = identity.get("source")

    def matches(path: Path) -> bool:
        """Check the exact captured package identity without generated metadata."""
        return source_identity(path, mode=mode, lean_probe_source=companion_path) == expected

    if not isinstance(expected, str) or not matches(source):
        raise RuntimeError("LeanFlow inputs changed before packaging; retry stable source")
    snapshot = venv / ".leanflow-package-inputs" / "LeanFlow"
    if snapshot.exists() or snapshot.is_symlink():
        raise RuntimeError("LeanFlow build inputs already exist; choose a fresh runtime")
    snapshot.mkdir(parents=True)
    for path in source_package_files(source):
        destination = snapshot / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    if not matches(snapshot) or not matches(source):
        raise RuntimeError("LeanFlow inputs changed while copying; retry stable source")
    return snapshot


def materialize_companion_input(venv: Path) -> Path | None:
    """Copy packaged inputs per runtime so parallel builds never share build metadata."""
    payload = captured_install_inputs(venv)
    original = payload.get("lean_probe_source")
    if not original:
        return None
    if not isinstance(original, str):
        raise RuntimeError("Invalid captured LeanProbe source path")
    source = Path(original)
    expected = payload.get("lean_probe_digest")
    if not isinstance(expected, str) or companion_identity(source) != expected:
        raise RuntimeError(
            "LeanProbe inputs changed before packaging; retry against stable source."
        )
    snapshot = venv / ".leanflow-package-inputs" / "LeanProbe"
    if snapshot.exists() or snapshot.is_symlink():
        raise RuntimeError("Companion build inputs already exist; choose a fresh runtime.")
    snapshot.mkdir(parents=True)
    files = [source / name for name in ("pyproject.toml", "README.md", "LICENSE")]
    files.extend((source / "src").rglob("*"))
    for path in files:
        if (
            path.name.endswith((".pyc", ".pyo"))
            or "__pycache__" in path.parts
            or any(part.endswith(".egg-info") for part in path.parts)
        ):
            continue
        if path.is_symlink():
            raise RuntimeError(f"Linked companion build input: {path}")
        if not path.is_file():
            continue
        destination = snapshot / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    if companion_identity(snapshot) != expected or companion_identity(source) != expected:
        raise RuntimeError("LeanProbe inputs changed while copying; retry against stable source.")
    return snapshot
