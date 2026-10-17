"""Resolve formalization module ownership from the pinned, evaluated Lake configuration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import remaining_seconds
from leanflow_cli.formalization.environment import compiler_executable
from leanflow_cli.formalization.evidence import identity
from leanflow_cli.formalization.lean_configuration import (
    static_configuration as _static_configuration,
)
from leanflow_cli.formalization.lean_configuration import (
    validate_options,
)
from leanflow_cli.workflows.prover.check_command_cleanup import command_workspace
from leanflow_cli.workflows.prover.check_process import isolated_command

_MODULE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*")
_INSPECTOR = r"""import Lake
import Lake.Load.Workspace
open Lean Lake
def main (args : List String) : IO Unit := do
  let some root := args[0]? | throw <| IO.userError "configuration root required"
  let some output := args[1]? | throw <| IO.userError "report path required"
  let (elan?, lean?, lake?) ← findInstall?
  let some lean := lean? | throw <| IO.userError "Lean installation unavailable"
  let some lake := lake? | throw <| IO.userError "Lake installation unavailable"
  let .ok env ← (Env.compute lake lean elan?).toBaseIO
    | throw <| IO.userError "Lake environment unavailable"
  let some ws ← (loadWorkspaceRoot {lakeEnv := env, wsDir := System.FilePath.mk root}).toBaseIO
    | throw <| IO.userError "Lake configuration evaluation failed"
  let p := ws.root
  let result := Json.mkObj [
    ("toolchain", toJson Lean.versionString),
    ("compiler", toJson Lean.githash),
    ("default_targets", toJson (p.defaultTargets.map Name.toString)),
    ("build_dir", toJson p.buildDir.toString),
    ("lean_lib_dir", toJson p.leanLibDir.toString),
    ("libraries", toJson (p.leanLibs.map fun lib => Json.mkObj [
      ("name", toJson lib.name.toString),
      ("src_dir", toJson lib.srcDir.toString),
      ("roots", toJson (lib.roots.map Name.toString)),
      ("lean_args", toJson lib.leanArgs),
      ("lean_options", toJson lib.leanOptions),
      ("roots_buildable", toJson (lib.roots.map fun r => lib.isBuildableModule r))])),
    ("executables", toJson (p.leanExes.map fun exe => Json.mkObj [
      ("name", toJson exe.name.toString),
      ("src_dir", toJson exe.toLeanLib.srcDir.toString),
      ("root", toJson exe.root.name.toString),
      ("lean_args", toJson exe.toLeanLib.leanArgs),
      ("lean_options", toJson exe.toLeanLib.leanOptions)]))]
  IO.FS.writeFile (System.FilePath.mk output) result.compress
"""


def _relative_path(value: str, temporary: Path) -> str:
    """Keep evaluated paths within the copied package instead of guessing prefixes."""
    path = Path(value)
    if not path.is_absolute():
        path = temporary / path
    return path.resolve().relative_to(temporary.resolve()).as_posix()


def inspect_layout(
    root: Path, *, deadline: float | None = None, cancelled: Callable[[], bool] | None = None
) -> dict[str, Any]:
    """Evaluate a bounded configuration copy without writing canonical project files."""
    try:
        root = root.resolve()
        config = root / "lakefile.lean"
        if not config.is_file():
            config = root / "lakefile.toml"
        if not (root / "lean-toolchain").is_file():
            raise ValueError("A pinned lean-toolchain is required")
        if config.is_symlink():
            raise ValueError("Lake configuration symlinks are unsupported")
        content = config.read_bytes()
        _static_configuration(config, content.decode("utf-8"))
        with command_workspace(prefix="leanflow-layout-") as temporary:
            directory = temporary
            package = directory / "project"
            package.mkdir()
            (package / config.name).write_bytes(content)
            if config.suffix == ".lean":
                # Native Lake can evaluate its own static DSL on toolchains whose
                # interpreter cannot execute all of Lake's compiled internals.
                # The copy has no dependency files or manifest; offline translation
                # preserves Lake's field/option semantics without package builds.
                translated = isolated_command(
                    project_root=root,
                    workspace=directory,
                    argv=[
                        compiler_executable(root, "lake"),
                        "--offline",
                        "-d",
                        str(package),
                        "translate-config",
                        "toml",
                    ],
                    timeout_s=remaining_seconds(deadline) if deadline is not None else 60,
                    cancelled=cancelled,
                )
                if not translated.get("success") or not (package / "lakefile.toml").is_file():
                    return {
                        "accepted": False,
                        "error": "Pinned static Lake translation failed",
                        "details": translated,
                    }
                _static_configuration(package / "lakefile.toml")
                (package / config.name).unlink(missing_ok=True)
            script = directory / "Inspect.lean"
            script.write_text(_INSPECTOR, encoding="utf-8")
            report = directory / "layout.json"
            result = isolated_command(
                project_root=root,
                workspace=directory,
                argv=[
                    compiler_executable(root, "lean"),
                    "--run",
                    str(script),
                    str(package),
                    str(report),
                ],
                timeout_s=remaining_seconds(deadline) if deadline is not None else 60,
                cancelled=cancelled,
            )
            if not result.get("success") or not report.is_file():
                return {
                    "accepted": False,
                    "error": "Pinned Lake inspection failed",
                    "details": result,
                }
            data = json.loads(report.read_text(encoding="utf-8"))
            for field in ("build_dir", "lean_lib_dir"):
                data[field] = _relative_path(data[field], package)
            if data["build_dir"] == "." or data["lean_lib_dir"] == ".":
                raise ValueError("Lake build outputs cannot overlap the package root")
            for library in data["libraries"]:
                library["src_dir"] = _relative_path(library["src_dir"], package)
                if not library["roots"] or not all(library["roots_buildable"]):
                    raise ValueError(
                        "Selective or rootless library globs require an explicit membership adapter"
                    )
                if library["lean_args"]:
                    raise ValueError(
                        "Custom Lean compiler arguments require a recorded-input adapter"
                    )
            for target in [*data["libraries"], *data.get("executables", [])]:
                target["src_dir"] = _relative_path(target["src_dir"], package)
                target["lean_options"] = validate_options(target.get("lean_options", {}))
                if target.get("lean_args"):
                    raise ValueError(
                        "Custom Lean compiler arguments require a recorded-input adapter"
                    )
            data.update(accepted=True, configuration_sha256=hashlib.sha256(content).hexdigest())
            return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"accepted": False, "error": f"Unsupported Lake layout: {exc}"}


def resolve_layout(root: Path, document_id: str) -> dict[str, Any]:
    """Allocate an owned subtree under a real existing library root."""
    data = inspect_layout(root)
    if not data.get("accepted"):
        return data
    candidates = [
        (library, module)
        for library in data["libraries"]
        for module, buildable in zip(library["roots"], library["roots_buildable"], strict=True)
        if buildable and _MODULE.fullmatch(module)
    ]
    if not candidates:
        return {
            **data,
            "accepted": False,
            "error": "No supported buildable library root; explicit layout configuration is required",
        }
    candidates.sort(
        key=lambda pair: (pair[0]["name"] not in data["default_targets"], pair[0]["name"], pair[1])
    )
    library, module = candidates[0]
    identifier = "Document_" + hashlib.sha256(document_id.encode()).hexdigest()[:16]
    prefix = f"{module}.Formalization.{identifier}"
    path = Path(library["src_dir"]).joinpath(*prefix.split("."))
    aggregator = path.with_suffix(".lean").as_posix()
    return {
        **data,
        "library": library["name"],
        "src_dir": library["src_dir"],
        "root_module": module,
        "module_prefix": prefix,
        "owned_prefix": path.as_posix(),
        "aggregator": aggregator,
        "root_file": Path(library["src_dir"])
        .joinpath(*module.split("."))
        .with_suffix(".lean")
        .as_posix(),
        "modules": [prefix],
        "build_targets": [f"+{prefix}:olean"],
    }


def generation_layout(layout: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Allocate each prepared translation independently of older published declarations."""
    if record.get("workflow_protocol") != 2 or not layout.get("accepted"):
        return layout
    preparation_id = record.get("preparation_id")
    if not isinstance(preparation_id, str) or not preparation_id:
        raise ValueError("A prepared translation requires its controller preparation identity")
    prefix = layout["module_prefix"] + ".Run_" + identity(preparation_id)[:12]
    owned = Path(layout["src_dir"]).joinpath(*prefix.split(".")).as_posix()
    return {
        **layout,
        "module_prefix": prefix,
        "owned_prefix": owned,
        "aggregator": owned + ".lean",
        "modules": [prefix],
        "build_targets": ["+" + prefix + ":olean"],
    }


def module_for_file(layout: dict[str, Any], relative: str) -> str:
    """Resolve a source path uniquely against evaluated library source directories."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".lean":
        raise ValueError(f"Invalid project-relative Lean file: {relative}")
    matches: set[str] = set()
    for library in layout.get("libraries", []):
        try:
            module = (
                path.relative_to(library["src_dir"]).with_suffix("").as_posix().replace("/", ".")
            )
        except ValueError:
            continue
        if _MODULE.fullmatch(module) and any(
            module == prefix or module.startswith(prefix + ".") for prefix in library["roots"]
        ):
            matches.add(module)
    if len(matches) != 1:
        raise ValueError(f"Source has no unique evaluated Lake membership: {relative}")
    return next(iter(matches))


def build_membership(layout: dict[str, Any], relative: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve declared library modules and executable roots with their effective options."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".lean":
        raise ValueError(f"Invalid project-relative Lean file: {relative}")
    matches = []
    for target in [*layout.get("libraries", []), *layout.get("executables", [])]:
        try:
            module = (
                path.relative_to(target["src_dir"]).with_suffix("").as_posix().replace("/", ".")
            )
        except ValueError:
            continue
        roots = target.get("roots", [])
        if _MODULE.fullmatch(module) and (
            any(module == prefix or module.startswith(prefix + ".") for prefix in roots)
            or module == target.get("root")
        ):
            matches.append((module, target))
    if len(matches) > 1:
        raise ValueError(f"Source has ambiguous evaluated Lake membership: {relative}")
    return matches[0] if matches else None
