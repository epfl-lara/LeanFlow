"""Bound campaign copies and preserve immutable runtime and dependency identities."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from leanflow_cli.runtime.dependency_cache import (
    dependency_cache_key,
    ensure_dependency_entry,
    materialize_dependencies,
)
from leanflow_cli.runtime.dependency_cache.identity import _identity, _key
from leanflow_cli.runtime.runtime_install import source_identity
from leanflow_cli.runtime.storage import (
    ensure_storage_capacity,
    lease_entry,
    register_workspace,
    storage_lock,
)
from leanflow_cli.runtime.storage.entries import read_entry
from leanflow_cli.workflows.prover.native_snapshot import release_snapshot_dependencies

_REINSTALL = "Reinstall LeanFlow from this exact source before preparing a new campaign."


@contextmanager
def installed_runtime(repo: Path) -> Iterator[dict[str, Any]]:
    """Lease the ready immutable installation matching the exact frozen source."""
    home = leanflow_home().resolve()
    ensure_storage_capacity(repo)
    marker = home / "install-root"
    try:
        if marker.is_symlink():
            raise ValueError("Linked installation metadata")
        fields = dict(line.split("=", 1) for line in marker.read_text().splitlines() if "=" in line)
        venv = Path(fields["venv_dir"])
        if not venv.is_absolute() or venv.name != "venv" or venv.parent.parent != home / "runtimes":
            raise ValueError("Installation is not an immutable managed runtime")
        with storage_lock():
            entry = venv.parent
            payload = read_entry(entry)
            if (
                payload is None
                or payload.get("kind") != "runtime"
                or payload.get("ready") is not True
            ):
                raise ValueError("Installation has no ready runtime identity")
            mode = str(payload.get("install_mode", "editable"))
            expected = source_identity(repo, mode=mode)
            identity = payload.get("identity")
            if not isinstance(identity, dict) or identity.get("source") != expected:
                raise ValueError("Installed runtime source differs from campaign source")
            python = venv / "bin/python"
            if not python.is_file():
                raise ValueError("Installed runtime interpreter is absent")
            lease = lease_entry(entry)
            lease.__enter__()
    except (OSError, KeyError, ValueError) as exc:
        raise ValueError(
            f"Cannot freeze this campaign's Python runtime: {exc}. {_REINSTALL}"
        ) from exc
    try:
        yield {
            "python": str(python),
            "python_runtime_entry": str(entry),
            "python_runtime_identity": expected,
        }
    finally:
        lease.__exit__(None, None, None)


def frozen_python(directory: Path) -> Path:
    """Resolve the recorded interpreter, retaining old private-env campaign replay."""
    provenance = json.loads((directory / "provenance.json").read_text())
    python = Path(provenance.get("python", str(directory / "python-env/bin/python")))
    if not python.is_file():
        raise ValueError(
            f"Frozen campaign interpreter is absent: {python}; reinstall its recorded source/runtime before resuming"
        )
    return python


@contextmanager
def lease_frozen_runtime(directory: Path) -> Iterator[None]:
    """Pin active campaign inputs without retaining old runtimes after it stops."""
    provenance = json.loads((directory / "provenance.json").read_text())
    frozen_python(directory)
    with ExitStack() as leases:
        with storage_lock():
            raw = provenance.get("python_runtime_entry")
            if raw:
                entry = Path(raw)
                payload = read_entry(entry)
                identity = payload.get("identity") if payload else None
                if (
                    payload is None
                    or payload.get("kind") != "runtime"
                    or payload.get("ready") is not True
                    or not isinstance(identity, dict)
                    or identity.get("source") != provenance.get("python_runtime_identity")
                ):
                    raise ValueError(
                        "Recorded campaign runtime is absent or changed; reinstall its exact source before resuming"
                    )
                leases.enter_context(lease_entry(entry))
            key = provenance.get("dependency_cache_key")
            if key:
                if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                    raise ValueError("Recorded campaign dependency cache identity is invalid")
                dependency = leanflow_home().resolve() / "cache/dependencies/v1" / key
                record = read_entry(dependency)
                if record is None or record.get("kind") != "dependency":
                    raise ValueError(
                        "Recorded campaign dependencies were evicted; run project setup for its exact frozen pins before resuming"
                    )
                leases.enter_context(lease_entry(dependency))
        yield


def freeze_dependencies(source: Path) -> str:
    """Publish pinned libraries once globally, without copying them into every baseline."""
    entry = ensure_dependency_entry(source)
    if entry is None:
        raise ValueError(
            "Pinned campaign dependencies are not ready; run LeanFlow project setup first"
        )
    return entry.name


def copy_dependencies(source: Path, destination: Path, *, seed: bool) -> None:
    """Use the exact global dependency cache while retaining private Lake checkouts."""
    ready = materialize_dependencies(source, destination, seed=seed)
    if not ready and not seed and (source / ".lake/packages").is_dir():
        # Old campaigns retain an exact private baseline; validate and seed it
        # rather than breaking replay or silently fetching different revisions.
        ready = materialize_dependencies(source, destination, seed=True)
    if not ready:
        raise ValueError(
            "Pinned campaign dependencies are not ready; run LeanFlow project setup first"
        )


def register_cell(project: Path) -> None:
    """Pin a campaign's writable dependency view while its controller is active."""
    register_workspace(project)


def restore_cell_dependencies(directory: Path, project: Path) -> None:
    """Restore a released cell from its unchanged frozen dependency inputs."""
    with storage_lock():
        marker = project / ".leanflow-dependencies-released"
        if not marker.exists():
            register_cell(project)
            return
        baseline = directory / "baseline"
        identity = _identity(project)
        expected = dependency_cache_key(baseline)
        if identity is None or _key(identity) != expected:
            raise ValueError(
                "Cell dependency inputs changed; restore its exact libraries before resuming"
            )
        packages = project / ".lake/packages"
        if packages.exists():
            raise ValueError("A released campaign dependency view is partial; restore it manually")
        register_cell(project)
        copy_dependencies(baseline, packages, seed=False)
        marker.unlink()
        register_cell(project)


def release_cell_dependencies(directory: Path, cell: dict[str, Any]) -> None:
    """Drop only finished cell dependency views after durable evidence was saved.

    Preserve unfamiliar or changed dependencies rather than risking a checkpoint's
    resumability. Proofs, results, local scratch builds, and checkpoints remain.
    """
    project = Path(str(cell.get("project", "")))
    if not cell.get("project") or not project.is_dir():
        return
    try:
        key = dependency_cache_key(project)
        if key is None or key != dependency_cache_key(directory / "baseline"):
            return
        entry = leanflow_home().resolve() / "cache/dependencies/v1" / key
        record = read_entry(entry)
        if (
            record is None
            or record.get("identity") != _identity(project)
            or not (entry / "packages").is_dir()
        ):
            return
        release_snapshot_dependencies(project)
    except (OSError, ValueError, RuntimeError) as exc:
        cell["dependency_cleanup_error"] = str(exc)
