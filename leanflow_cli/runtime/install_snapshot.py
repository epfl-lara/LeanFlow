"""Capture external runtime inputs and isolate companion packaging side effects."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
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
