"""Inspect kernel declaration types from freshly compiled, isolated Lean artifacts."""

from __future__ import annotations

import json
import tempfile
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
    let value := if info.isTheorem || selectedName mutableNames name then none
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
  searchPathRef.modify fun paths => System.FilePath.mk path :: paths
  let env ← importModules #[{ module := `LeanFlowTypeProfileInput }] {} 0
  let some idx := env.getModuleIdx? `LeanFlowTypeProfileInput
    | throw <| IO.userError "compiled profile module missing"
  let requested := (args.drop 1).takeWhile (· != "--")
  let mutableNames := ((args.drop 1).dropWhile (· != "--")).drop 1
  for (name, info) in env.constants.toList do
    if env.getModuleIdxFor? name == some idx && selectedName requested name then
      let (axioms, _) ← (collectAxioms name : StateT Environment IO (Array Name)).run env
      IO.println <| Json.compress <| Json.mkObj [
        ("name", toJson name.toString),
        ("type", toJson (repr info.type).pretty),
        ("levels", toJson (info.levelParams.map Name.toString)),
        ("dependencies", Json.arr (localTypeDependencies env idx mutableNames info.type)),
        ("axioms", toJson (axioms.map Name.toString))]
"""


def compiled_type_profiles(
    *,
    root: Path,
    source: str,
    names: list[str],
    workspace: Path,
    timeout_s: int,
    mutable_names: list[str] | None = None,
) -> dict[str, Any]:
    """Compile untouched source before importing trusted introspection support.

    Importing Lean's metaprogramming modules into the source being checked could
    itself alter its elaboration. Compile first, then inspect the exported raw
    Expr in a separate process; notation and pretty-printer settings cannot hide
    implicit arguments or changed typeclass instances in that representation.
    """
    from leanflow_cli.workflows.prover.check_process import isolated_command

    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="type-profile-", dir=workspace) as temporary:
        directory = Path(temporary)
        input_file = directory / f"{_MODULE}.lean"
        artifact = input_file.with_suffix(".olean")
        input_file.write_bytes(source.encode("utf-8"))
        compiled = isolated_command(
            project_root=root,
            workspace=directory,
            argv=[
                "lake",
                "env",
                "lean",
                "-R",
                str(directory),
                "-o",
                str(artifact),
                str(input_file),
            ],
            timeout_s=timeout_s,
        )
        if not compiled.get("success") or not artifact.is_file():
            return {
                "accepted": False,
                "error": "exact source compilation failed",
                "compile": compiled,
            }
        # Create trusted inspector bytes only after all candidate compile processes
        # have been reaped. The inspector loads the artifact dynamically at run time.
        inspector = directory / "Inspect.lean"
        inspector.write_text(_INSPECTOR)
        inspected = isolated_command(
            project_root=root,
            workspace=directory,
            argv=[
                "lake",
                "env",
                "lean",
                "--run",
                str(inspector),
                str(directory),
                *names,
                "--",
                *(mutable_names or []),
            ],
            timeout_s=timeout_s,
        )
        if not inspected.get("success"):
            return {
                "accepted": False,
                "error": "kernel type inspection failed",
                "inspect": inspected,
            }
        try:
            rows = [
                json.loads(line)
                for line in inspected.get("stdout", "").splitlines()
                if line.strip()
            ]
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
            return {"accepted": True, "profiles": profiles}
        except (TypeError, ValueError, KeyError) as exc:
            return {"accepted": False, "error": f"invalid kernel type profile: {exc}"}
