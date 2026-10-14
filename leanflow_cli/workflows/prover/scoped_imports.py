"""Prepare exact local import artifacts privately without executing project Lake hooks."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import operation_cancelled, remaining_seconds
from leanflow_cli.formalization.dependency_inputs import dependency_inputs
from leanflow_cli.formalization.evidence import contained, digest
from leanflow_cli.formalization.lean_build import build_project, validate_build_inputs
from leanflow_cli.formalization.lean_layout import inspect_layout
from leanflow_cli.formalization.publication import publication_guard
from leanflow_cli.runtime.storage import ensure_storage_capacity
from leanflow_cli.workflows.prover.scoped_cache import (
    CacheReplacement,
    CacheVersion,
    cache_refresh_needed,
    cache_version,
    install_cache_batch,
)


def _active(deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
    """Poll the campaign while doing controller-side source and cache work."""
    if operation_cancelled() or (cancelled is not None and cancelled()):
        raise InterruptedError("Scoped import preparation cancelled")
    remaining = remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        raise TimeoutError("Scoped import preparation deadline exhausted")


def _artifact_paths(base: Path) -> tuple[Path, ...]:
    """Include complete Lean parts and their stale Lake build metadata in one batch."""
    parts = (
        base,
        Path(str(base) + ".server"),
        Path(str(base) + ".private"),
        base.with_suffix(".ir"),
        Path(str(base.with_suffix(".ir")) + ".sig"),
        base.with_suffix(".ilean"),
    )
    return (
        *parts,
        base.with_suffix(".trace"),
        *(Path(str(part) + suffix) for part in parts for suffix in (".trace", ".hash")),
    )


def prepare_scoped_imports(
    *,
    project_root: Path,
    workspace: Path,
    files: list[str] | None = None,
    modules: list[str] | None = None,
    expected_modules: dict[str, str] | None = None,
    expected_sources: dict[str, str] | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Freeze and compile the requested static closure before installing derived cache bytes.

    Compilation grants only private per-module outputs. Source/configuration
    drift and concurrent cache changes refuse publication; replaced cache inodes
    remain under controller state rather than deleting uncertain prior inputs.
    This readiness result grants no mathematical proof acceptance.
    """
    from leanflow_cli.formalization.lean_policy import validate_source

    root = project_root.resolve()
    workspace = workspace.resolve()
    if (files is None) == (modules is None):
        raise ValueError("Select exact source files or local import modules")
    _active(deadline, cancelled)
    configurations = {
        name: digest((root / name).read_bytes()) if (root / name).is_file() else None
        for name in ("lean-toolchain", "lakefile.toml", "lakefile.lean", "lake-manifest.json")
    }
    layout = inspect_layout(root, deadline=deadline, cancelled=cancelled)
    if not layout.get("accepted"):
        return {"success": False, "error": layout.get("error"), "layout": layout}
    configuration = (
        "lakefile.lean" if configurations["lakefile.lean"] is not None else "lakefile.toml"
    )
    if layout.get("configuration_sha256") != configurations[configuration]:
        raise ValueError("Evaluated scoped layout differs from frozen configuration")
    refresh = cache_refresh_needed(root)
    dependencies = dependency_inputs(root)
    admitted = validate_build_inputs(root, layout, inventory=dependencies)
    local: dict[str, Path] = admitted["local"]
    by_file = {path.relative_to(root).as_posix(): name for name, path in local.items()}
    if expected_modules and any(
        by_file.get(file) != name for file, name in expected_modules.items()
    ):
        raise ValueError("Scoped module names differ from the evaluated static library")
    if modules is not None and any(name not in local for name in modules):
        raise ValueError("Requested local import has no evaluated library membership")
    selected = (
        files
        if files is not None
        else [local[name].relative_to(root).as_posix() for name in modules or []]
    )
    if refresh is not None:
        # A global barrier may describe another module's interrupted sidecars.
        # Rebuild all admitted local members before granting readiness again.
        selected = list(by_file)
    source_hashes = {}
    for path in local.values():
        _active(deadline, cancelled)
        source_hashes[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    if expected_sources and any(
        source_hashes.get(file) != sha for file, sha in expected_sources.items()
    ):
        raise ValueError("Scoped project sources differ from their frozen proof baseline")
    for path in local.values():
        _active(deadline, cancelled)
        validate_source(path.read_text(encoding="utf-8"))
    library: Path = admitted["library"]
    observed: dict[Path, CacheVersion | None] = {}
    for name in local:
        base = library.joinpath(*name.split(".")).with_suffix(".olean")
        for path in _artifact_paths(base):
            path = contained(root, path.relative_to(root).as_posix())
            observed[path] = cache_version(path)
    ensure_storage_capacity(workspace)
    private = workspace / ("scoped-imports-" + uuid.uuid4().hex)
    private.mkdir(parents=True)
    compiled = build_project(
        root,
        layout,
        private,
        files=selected,
        deadline=deadline,
        cancelled=cancelled,
        output_library=private / "library",
        inventory=dependencies,
    )
    if not compiled.get("success"):
        return {
            **compiled,
            "error": compiled.get("error") or "Scoped local imports failed to compile",
        }

    def unchanged() -> None:
        """Reject source or static configuration drift before cache visibility changes."""
        _active(deadline, cancelled)
        if (
            any(digest((root / file).read_bytes()) != sha for file, sha in source_hashes.items())
            or any(
                (digest((root / name).read_bytes()) if (root / name).is_file() else None) != sha
                for name, sha in configurations.items()
            )
            or compiled["source_sha256"] != source_hashes
        ):
            raise ValueError("Scoped compilation inputs changed before artifact publication")
        _active(deadline, cancelled)

    if refresh is not None and set(compiled["modules"]) != set(local):
        raise ValueError("Scoped cache refresh did not compile every admitted local module")

    def before_commit() -> None:
        """Refuse a new blocker that appeared after selecting the compiled closure."""
        unchanged()
        if cache_refresh_needed(root) != refresh:
            raise ValueError("Scoped cache refresh authority changed during compilation")

    retained = contained(root, ".leanflow/scoped-import-cache/" + uuid.uuid4().hex)
    replacements: dict[Path, CacheReplacement] = {}
    for module, record in compiled["local_artifacts"].items():
        _active(deadline, cancelled)
        produced: dict[Path, bytes] = {}
        for path, sha in record["sha256"].items():
            source = Path(path)
            content = source.read_bytes()
            if source.is_symlink() or digest(content) != sha:
                raise ValueError("Private scoped artifact changed after verified compilation")
            target = contained(
                root,
                (library / source.relative_to(private / "library")).relative_to(root).as_posix(),
            )
            produced[target] = content
        base = library.joinpath(*module.split(".")).with_suffix(".olean")
        for path in _artifact_paths(base):
            target = contained(root, path.relative_to(root).as_posix())
            replacements[target] = (produced.get(target), observed[target])

    def ready() -> None:
        """Check the whole frozen input boundary once after the bounded cache batch."""
        unchanged()
        if dependency_inputs(root) != dependencies:
            raise ValueError("Scoped dependency inputs changed before readiness")
        unchanged()

    with publication_guard(root):
        installed = install_cache_batch(
            root,
            replacements,
            retained,
            before_commit=before_commit,
            after_commit=ready,
            full_refresh=refresh is not None,
        )
    return {
        "success": True,
        "authority": "direct-pinned-lean",
        "modules": compiled["modules"],
        "source_sha256": source_hashes,
        **installed,
        "layout": layout,
        "proof_acceptance": False,
        "private_workspace": str(private),
    }
