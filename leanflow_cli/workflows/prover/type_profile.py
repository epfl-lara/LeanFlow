"""Inspect kernel declaration types from freshly compiled, isolated Lean artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.prover.models import digest

_MODULE = "LeanFlowTypeProfileInput"
_INSPECTOR = r"""import Lean
open Lean
instance : MonadEnv (StateT Environment IO) where
  getEnv := get
  modifyEnv := modify
def selectedName (names : List String) (name : Name) : Bool :=
  names.any (fun target => name.toString == target || name.toString.endsWith ("." ++ target))
def localTypeDependencies (env : Environment) (idx : ModuleIdx)
    (mutableNames : List String) (type : Expr) : Array Json := Id.run do
  let mut pending := type.getUsedConstants
  let mut seen : NameSet := {}
  let mut result : Array (String × Json) := #[]
  let mut cursor := 0
  while cursor < pending.size do
    let name := pending[cursor]!
    cursor := cursor + 1
    if seen.contains name then continue
    seen := seen.insert name
    if env.getModuleIdxFor? name != some idx then continue
    let some info := env.find? name | continue
    -- Match the constructor rather than calling ConstantInfo.isTheorem: that
    -- accessor does not exist in every supported Lean, and the inspector must
    -- run against whatever toolchain the target project pins.
    let isThm := match info with | .thmInfo _ => true | _ => false
    let value := if isThm || selectedName mutableNames name then none
      else info.value? (allowOpaque := true)
    pending := pending ++ info.type.getUsedConstants
    if let some value := value then pending := pending ++ value.getUsedConstants
    let row := Json.mkObj [
      ("name", toJson name.toString),
      ("type", toJson (repr info.type).pretty),
      ("value", toJson (value.map (fun expr => (repr expr).pretty)))]
    result := result.push (name.toString, row)
  return (result.qsort (fun a b => a.1 < b.1)).map Prod.snd
def main (args : List String) : IO Unit := do
  let some path := args[0]? | throw <| IO.userError "profile path required"
  let some moduleName := args[1]? | throw <| IO.userError "profile module required"
  let module := moduleName.toName
  searchPathRef.modify fun paths => System.FilePath.mk path :: paths
  -- This environment lives until process exit. Avoid reference-counting its
  -- entire imported constant graph while traversing the requested declarations.
  let env ← importModules #[{ module := module }] {} 0 (leakEnv := true)
  let some idx := env.getModuleIdx? module
    | throw <| IO.userError "compiled profile module missing"
  let requested := (args.drop 2).takeWhile (· != "--")
  let mutableNames := ((args.drop 2).dropWhile (· != "--")).drop 1
  -- Report through the writable workspace. stdout is captured with a bounded
  -- rolling tail, and a single kernel type can exceed it by an order of
  -- magnitude, which would silently deliver the tail of one JSON object.
  let profile ← IO.FS.Handle.mk (System.FilePath.mk path / "profile.jsonl") IO.FS.Mode.write
  -- Enumerate this module, not a list copy of the full Mathlib environment.
  for name in env.header.moduleData[idx.toNat]!.constNames do
    let some info := env.find? name | continue
    if env.getModuleIdxFor? name == some idx && selectedName requested name then
      let (axioms, _) ← (collectAxioms name : StateT Environment IO (Array Name)).run env
      profile.putStrLn <| Json.compress <| Json.mkObj [
        ("name", toJson name.toString),
        ("type", toJson (repr info.type).pretty),
        ("levels", toJson (info.levelParams.map Name.toString)),
        ("dependencies", Json.arr (localTypeDependencies env idx mutableNames info.type)),
        ("axioms", toJson (axioms.map Name.toString))]
  profile.flush
"""


def module_name(relative: str) -> str:
    """Derive the Lean module a project-relative source path compiles to."""
    return Path(relative).with_suffix("").as_posix().replace("/", ".")


def _shadow_package_root(shadow: Path, published: Path) -> None:
    """Expose already published sibling artifacts beside a freshly compiled module.

    Lean resolves every module of one package root from the first search path
    entry holding that root. The fresh artifact and its ancestor directories
    already exist under ``shadow``; everything else links to the project build.
    """
    if not published.is_dir():
        return
    shadow.mkdir(parents=True, exist_ok=True)
    for entry in published.iterdir():
        destination = shadow / entry.name
        if destination.is_symlink():
            continue
        if destination.exists():
            if entry.is_dir() and not entry.is_symlink() and destination.is_dir():
                _shadow_package_root(destination, entry)
            continue
        os.symlink(entry, destination, target_is_directory=entry.is_dir())


def compiled_type_profiles(
    *,
    root: Path,
    source: str,
    names: list[str],
    workspace: Path,
    timeout_s: float,
    mutable_names: list[str] | None = None,
    module: str | None = None,
    artifact: Path | None = None,
    on_stage: Callable[[str, Callable[[], dict[str, Any]]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile untouched source before importing trusted introspection support.

    Importing Lean's metaprogramming modules into the source being checked could
    itself alter its elaboration. Compile first, then inspect the exported raw
    Expr in a separate process; notation and pretty-printer settings cannot hide
    implicit arguments or changed typeclass instances in that representation.

    With ``module`` the source compiles under its real module name, so the
    checked artifact can later be published without another compilation. When
    ``artifact`` names a controller-private destination, the compiled bytes are
    copied there as soon as the compiler has been reaped and inspection then
    reads that same copy; the result reports its path and SHA-256.
    """
    from leanflow_cli.workflows.prover.check_process import isolated_command

    deadline = time.monotonic() + timeout_s
    compiled_module = module or _MODULE
    timing: dict[str, float] = {}

    def run_stage(stage: str, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Publish each expensive stage and retain its duration even on failure."""
        started = time.monotonic()
        try:
            return on_stage(stage, action) if on_stage else action()
        finally:
            timing[f"{stage}_s"] = round(time.monotonic() - started, 3)

    workspace.mkdir(parents=True, exist_ok=True)
    # Transient symlinks must not enter the durable resume tree: a hard kill
    # cannot run cleanup, and resume deliberately refuses symlinked evidence.
    with tempfile.TemporaryDirectory(prefix="leanflow-type-profile-") as temporary:
        directory = Path(temporary).resolve()
        input_file = directory.joinpath(*compiled_module.split(".")).with_suffix(".lean")
        input_file.parent.mkdir(parents=True, exist_ok=True)
        compiled_artifact = input_file.with_suffix(".olean")
        input_file.write_bytes(source.encode("utf-8"))
        compiled = run_stage(
            "compile",
            lambda: isolated_command(
                project_root=root,
                workspace=directory,
                argv=[
                    "lake",
                    "env",
                    "lean",
                    "-R",
                    str(directory),
                    "-o",
                    str(compiled_artifact),
                    str(input_file),
                ],
                timeout_s=max(0.01, deadline - time.monotonic()),
            ),
        )
        if not compiled.get("success") or not compiled_artifact.is_file():
            return {
                "accepted": False,
                "error": "exact source compilation failed",
                "compile": compiled,
                "timing": timing,
            }
        retained: dict[str, Any] = {}
        if artifact is not None:
            # Copy immediately after the candidate compiler has been reaped, then
            # inspect exactly the retained bytes so publication never trusts more
            # than the kernel inspection did.
            content = compiled_artifact.read_bytes()
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(content)
            compiled_artifact.unlink()
            os.symlink(artifact, compiled_artifact)
            retained = {
                "artifact": str(artifact),
                "artifact_sha256": hashlib.sha256(content).hexdigest(),
            }
        if module is not None:
            package = compiled_module.split(".")[0]
            _shadow_package_root(
                directory / package, root / ".lake" / "build" / "lib" / "lean" / package
            )
            published_root = (root / ".lake" / "build" / "lib" / "lean" / package).with_suffix(
                ".olean"
            )
            if published_root.is_file() and not (directory / f"{package}.olean").exists():
                os.symlink(published_root, directory / f"{package}.olean")
        # Create trusted inspector bytes only after all candidate compile processes
        # have been reaped. The inspector loads the artifact dynamically at run time.
        inspector = directory / "Inspect.lean"
        inspector.write_text(_INSPECTOR)
        inspected = run_stage(
            "inspect",
            lambda: isolated_command(
                project_root=root,
                workspace=directory,
                argv=[
                    "lake",
                    "env",
                    "lean",
                    "--run",
                    str(inspector),
                    str(directory),
                    compiled_module,
                    *names,
                    "--",
                    *(mutable_names or []),
                ],
                timeout_s=max(0.01, deadline - time.monotonic()),
            ),
        )
        if not inspected.get("success"):
            return {
                "accepted": False,
                "error": "kernel type inspection failed",
                "inspect": inspected,
                "timing": timing,
            }
        try:
            report = directory / "profile.jsonl"
            if report.is_file():
                payload = report.read_text(encoding="utf-8")
            else:
                # Older inspectors printed to stdout. That capture keeps only a
                # bounded tail, so a payload at the cap is very likely the tail
                # of a longer stream; parsing it would mis-report a complete
                # kernel profile from a fragment.
                payload = inspected.get("stdout", "")
                if inspected.get("stdout_truncated"):
                    raise ValueError(
                        "kernel inspection output exceeded the capture limit; "
                        "the inspector did not write profile.jsonl"
                    )
            rows = [json.loads(line) for line in payload.splitlines() if line.strip()]
            profiles: dict[str, dict[str, Any]] = {}
            for name in names:
                matches = [
                    row
                    for row in rows
                    if isinstance(row, dict)
                    and isinstance(row.get("name"), str)
                    and (row["name"] == name or row["name"].endswith("." + name))
                ]
                if len(matches) != 1:
                    raise ValueError(f"expected one exact compiled declaration for {name}")
                row = matches[0]
                if not isinstance(row.get("type"), str) or not row["type"]:
                    raise ValueError("missing raw kernel type")
                if (
                    not isinstance(row.get("dependencies"), list)
                    or not isinstance(row.get("levels"), list)
                    or not isinstance(row.get("axioms"), list)
                ):
                    raise ValueError("missing complete kernel declaration evidence")
                profiles[name] = {
                    **row,
                    "sha256": digest(
                        json.dumps(
                            [row["levels"], row["type"], row["dependencies"]], ensure_ascii=False
                        )
                    ),
                }
            return {"accepted": True, "profiles": profiles, "timing": timing, **retained}
        except (TypeError, ValueError, KeyError) as exc:
            return {
                "accepted": False,
                "error": f"invalid kernel type profile: {exc}",
                "timing": timing,
            }
