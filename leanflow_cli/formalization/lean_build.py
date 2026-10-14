"""Compile frozen local modules offline using explicit pinned dependency artifacts."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import remaining_seconds
from core.file_copy import copy_file
from leanflow_cli.formalization.dependency_inputs import compiled_library_paths, dependency_inputs
from leanflow_cli.formalization.dependency_modules import (
    module_roots,
    output_paths,
    validate_library_roots,
)
from leanflow_cli.formalization.dependency_snapshot import _package_roots
from leanflow_cli.formalization.environment import compiler_executable
from leanflow_cli.formalization.evidence import digest
from leanflow_cli.formalization.lean_configuration import validate_options
from leanflow_cli.formalization.lean_layout import build_membership
from leanflow_cli.workflows.prover.check_process import isolated_command
from leanflow_cli.workflows.prover.source import project_sources


def _artifact_parts(path: Path) -> list[list[str]]:
    """Describe Lean's public/server/private and IR artifact sequence exactly."""
    public = [path, Path(str(path) + ".server"), Path(str(path) + ".private")]
    ir = path.with_suffix(".ir")
    code = [Path(str(ir) + ".sig"), ir]
    for part in [*public, *code]:
        if part.is_symlink():
            raise ValueError(f"Aliased compiled module artifact: {part}")
    return [[str(part) for part in group if part.is_file()] for group in (public, code)]


def _library_artifacts(root: Path, compiler: Path) -> dict[str, list[list[str]]]:
    """Resolve mathematical library modules without loading package configuration or tooling."""
    artifacts: dict[str, list[list[str]]] = {}
    libraries = compiled_library_paths(root)
    validate_library_roots(libraries, compiler)
    for library in libraries:
        paths = sorted(library.rglob("*.olean"))
        if not paths:
            raise ValueError(
                f"Missing compiled mathematical library artifacts: {library}; prepare the pinned dependency library artifacts before formalization"
            )
        for path in paths:
            relative = path.relative_to(library)
            module = relative.with_suffix("").as_posix().replace("/", ".")
            if module in artifacts or (compiler / relative).exists():
                raise ValueError(f"Ambiguous or compiler-shadowing dependency module: {module}")
            artifacts[module] = _artifact_parts(path)
    return artifacts


def _ordered_modules(headers: dict[str, list[str]], requested: list[str]) -> list[str]:
    """Order the requested local import closure and reject cycles before compilation."""
    ordered: list[str] = []
    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(module: str) -> None:
        if module in complete:
            return
        if module in visiting:
            raise ValueError(f"Cyclic local module imports: {module}")
        visiting.add(module)
        for dependency in headers[module]:
            if dependency in headers:
                visit(dependency)
        visiting.remove(module)
        complete.add(module)
        ordered.append(module)

    for module in requested:
        visit(module)
    return ordered


def _setup_artifacts(artifacts: dict[str, list[list[str]]], toolchain: str) -> dict[str, Any]:
    """Encode the pinned compiler's pre/post split-IR ModuleSetup wire format."""
    version = re.match(r"(\d+)\.(\d+)\.", toolchain)
    if version is None:
        raise ValueError("Evaluated compiler version is missing from the build layout")
    if tuple(map(int, version.groups())) >= (4, 33):
        return dict(artifacts)
    result: dict[str, Any] = {}
    for name, (public, code) in artifacts.items():
        if len(public) > 1 and not code:
            raise ValueError(f"Legacy compiler module lacks required IR sidecar: {name}")
        result[name] = [*public[:1], *code[-1:], *public[1:]]
    return result


def validate_build_inputs(
    root: Path, layout: dict[str, Any], *, inventory: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Admit build paths, module ownership and artifact inputs without running compilation."""
    inventory = dependency_inputs(root) if inventory is None else inventory
    declarative = [
        name
        for name, package in inventory["packages"].items()
        if package["mode"] == "declarative-source"
    ]
    if declarative:
        raise ValueError(
            "unsupported_declarative_dependency_build: "
            + ", ".join(sorted(declarative))
            + "; private formalization builds require fully pinned prebuilt libraries. Source-only dependencies need a separate recorded static-package build adapter"
        )
    missing = [
        name
        for name, package in inventory["packages"].items()
        if package["mode"] == "compiled-library" and not package.get("library_dir")
    ]
    if missing:
        raise ValueError(
            "Missing compiled mathematical library artifacts: "
            + ", ".join(sorted(missing))
            + "; prepare the pinned dependency library artifacts before formalization"
        )
    packages = _package_roots(root, inventory)
    build, library = output_paths(root, layout, list(packages))
    lean = compiler_executable(root, "lean")
    compiler = Path(lean).parent.parent / "lib/lean"
    artifacts = _library_artifacts(root, compiler)
    local: dict[str, Path] = {}
    targets: dict[str, Any] = {}
    excluded: list[str] = []
    external_roots = {name.split(".")[0] for name in artifacts} | module_roots(compiler)
    for source in project_sources(root):
        if source.name == "lakefile.lean" or source.is_relative_to(build):
            continue
        relative = source.relative_to(root).as_posix()
        membership = build_membership(layout, relative)
        if membership is None:
            excluded.append(relative)
            continue
        module, target = membership
        if module in local:
            raise ValueError(f"Multiple local files define module {module}")
        if module.split(".")[0] in external_roots:
            raise ValueError(f"Local module shadows an admitted library: {module}")
        validate_options(target.get("lean_options", {}))
        local[module], targets[module] = source, target
    return {
        "lean": lean,
        "compiler": compiler,
        "library": library,
        "artifacts": artifacts,
        "local": local,
        "targets": targets,
        "excluded_sources": sorted(excluded),
    }


def _check_output_census(library: Path, artifacts: dict[str, Any]) -> None:
    """Reject additional files, aliases and empty import-capturing directories."""
    expected = {Path(path) for record in artifacts.values() for path in record["sha256"]}
    directories = {
        parent for path in expected for parent in path.parents if parent.is_relative_to(library)
    }
    for path in library.rglob("*"):
        if (
            path.is_symlink()
            or (path.is_dir() and path not in directories)
            or (not path.is_dir() and path not in expected)
        ):
            raise ValueError(f"Unexpected local compiled artifact: {path.relative_to(library)}")


def build_project(
    root: Path,
    layout: dict[str, Any],
    workspace: Path,
    *,
    files: list[str] | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Compile the requested closure or every local module without executing Lake.

    Package Git metadata and Lake hooks are irrelevant to the frozen mathematical
    inputs. Controller-created setup records bind exact compiled library paths;
    headers still supply their own imports, and each local dependency is rebuilt.
    Source compilation is untrusted and remains inside the existing write sandbox.
    Independent kernel inspection is a separate later process.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    admitted = validate_build_inputs(root, layout)
    lean, compiler = admitted["lean"], admitted["compiler"]
    local, targets = admitted["local"], admitted["targets"]
    library = admitted["library"]
    if not local:
        return {
            "success": True,
            "modules": [],
            "authority": "direct-pinned-lean",
            "local_artifacts": {},
            "excluded_sources": admitted["excluded_sources"],
        }
    by_file = {path.relative_to(root).as_posix(): name for name, path in local.items()}
    if files is not None and any(name not in by_file for name in files):
        raise ValueError("Requested local module has no declared library/executable membership")
    requested = [by_file[name] for name in files] if files is not None else sorted(local)
    before = {name: digest(path.read_bytes()) for name, path in local.items()}
    parsed = isolated_command(
        project_root=root,
        workspace=workspace,
        argv=[lean, "-j1", "--deps-json", *(str(path) for path in local.values())],
        timeout_s=remaining_seconds(deadline),
        cancelled=cancelled,
    )
    if not parsed.get("success"):
        return parsed
    rows = json.loads(parsed.get("stdout", ""))["imports"]
    if not isinstance(rows, list) or len(rows) != len(local):
        raise ValueError("Pinned compiler returned an incomplete module-header census")
    headers: dict[str, list[str]] = {}
    for module, row in zip(local, rows, strict=True):
        if row.get("errors") or not isinstance(row.get("result"), dict):
            return {
                "success": False,
                "returncode": 1,
                "error": f"Invalid import header in {module}",
                "stderr": "\n".join(row.get("errors", [])),
            }
        headers[module] = [item["module"] for item in row["result"]["imports"]]
    order = _ordered_modules(headers, requested)
    artifacts = admitted["artifacts"]
    if library.exists():
        shutil.rmtree(library)
    library.mkdir(parents=True)
    local_artifacts: dict[str, Any] = {}
    for index, module in enumerate(order):
        for dependency in headers[module]:
            relative = Path(*dependency.split(".")).with_suffix(".olean")
            if dependency not in artifacts and not (compiler / relative).is_file():
                raise ValueError(f"Missing admitted mathematical import {dependency} in {module}")
        if "roots" in targets[module] and any(
            "root" in targets.get(dependency, {}) for dependency in headers[module]
        ):
            raise ValueError(f"Mathematical library cannot import an executable root: {module}")
        compile_workspace = workspace / f"module-{index:04d}"
        if compile_workspace.exists():
            shutil.rmtree(compile_workspace)
        compile_workspace.mkdir()
        setup = compile_workspace / "module-setup.json"
        setup.write_text(
            json.dumps(
                {
                    "name": module,
                    "package": None,
                    "isModule": False,
                    "imports": None,
                    "importArts": _setup_artifacts(artifacts, layout["toolchain"]),
                    "plugins": [],
                    "dynlibs": [],
                    "options": validate_options(targets[module].get("lean_options", {})),
                }
            ),
            encoding="utf-8",
        )
        setup_bytes = setup.read_bytes()
        relative_output = Path(*module.split(".")).with_suffix(".olean")
        output = compile_workspace / relative_output
        output.parent.mkdir(parents=True, exist_ok=True)
        result = isolated_command(
            project_root=root,
            workspace=compile_workspace,
            argv=[lean, "-j1", "--setup=" + str(setup), "-o", str(output), str(local[module])],
            timeout_s=remaining_seconds(deadline),
            cancelled=cancelled,
        )
        if not result.get("success"):
            return {**result, "module": module}
        if not output.is_file() or output.is_symlink():
            raise ValueError(f"Pinned compiler did not produce a private artifact: {module}")
        parts = _artifact_parts(output)
        expected = {setup, *(Path(part) for group in parts for part in group)}
        allowed_directories = {
            parent
            for path in expected
            for parent in path.parents
            if parent.is_relative_to(compile_workspace)
        }
        for path in compile_workspace.rglob("*"):
            if (
                path.is_symlink()
                or (path.is_dir() and path not in allowed_directories)
                or (not path.is_dir() and path not in expected)
            ):
                raise ValueError(
                    f"Unexpected private compiler output: {path.relative_to(compile_workspace)}"
                )
        if setup.read_bytes() != setup_bytes:
            raise ValueError("Compiler modified its controller-owned setup record")
        for previous in local_artifacts.values():
            if any(
                not Path(path).is_file() or digest(Path(path).read_bytes()) != sha
                for path, sha in previous["sha256"].items()
            ):
                raise ValueError("Compiler modified an earlier local artifact")
        final_parts: list[list[str]] = []
        for group in parts:
            final_group = []
            for part in group:
                path = Path(part)
                final = library / path.relative_to(compile_workspace)
                final.parent.mkdir(parents=True, exist_ok=True)
                # Do not preserve an inode that the untrusted compiler (or a
                # child process) could still hold open for writing.
                copy_file(path, final)
                final_group.append(str(final))
            final_parts.append(final_group)
        artifacts[module] = final_parts
        local_artifacts[module] = {
            "parts": final_parts,
            "sha256": {
                path: digest(Path(path).read_bytes()) for group in final_parts for path in group
            },
        }
        _check_output_census(library, local_artifacts)
    if {name: digest(path.read_bytes()) for name, path in local.items()} != before:
        raise ValueError("Local module source changed during direct compilation")
    return {
        "success": True,
        "modules": order,
        "authority": "direct-pinned-lean",
        "local_artifacts": local_artifacts,
        "excluded_sources": admitted["excluded_sources"],
    }
