"""Verify dependency constants without executing package configuration or initializers."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from core.deadlines import deadline_after
from leanflow_cli.formalization import dependency_inputs, lean_build, lean_policy
from leanflow_cli.formalization.environment import compiler_executable


@pytest.fixture
def imported_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Set LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN for pinned kernel integration")
    assert toolchain is not None
    (tmp_path / "lean-toolchain").write_text(toolchain)
    # The independent inspector must never evaluate package configuration.
    (tmp_path / "lakefile.lean").write_text("this is deliberately not a Lake configuration\n")
    library = tmp_path / ".lake/build/lib/lean"
    library.mkdir(parents=True)
    dependency = tmp_path / ".lake/packages/mathematics/.lake/build/lib/lean"
    dependency.mkdir(parents=True)
    libraries = lambda root: [dependency] if next(dependency.rglob("*.olean"), None) else []
    monkeypatch.setattr(dependency_inputs, "compiled_library_paths", libraries)
    monkeypatch.setattr(lean_build, "compiled_library_paths", libraries)
    return tmp_path, dependency


def _compile(root: Path, library: Path, module: str, source: str, *imports: Path) -> None:
    """Create an actual dependency artifact; this deliberately bypasses source admission."""
    source_path = library / f"{module}.lean"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source)
    subprocess.run(
        [
            compiler_executable(root, "lean"),
            "--root",
            str(library),
            "-o",
            str(library / f"{module}.olean"),
            str(source_path),
        ],
        cwd=root,
        env={**os.environ, "LEAN_PATH": os.pathsep.join(str(path) for path in imports)},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


def _local_artifacts(root: Path) -> dict[str, Any]:
    library = root / ".lake/build/lib/lean"
    return {
        path.relative_to(library)
        .with_suffix("")
        .as_posix()
        .replace("/", "."): {
            "parts": parts,
            "sha256": {
                name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                for group in parts
                for name in group
            },
        }
        for path in library.rglob("*.olean")
        for parts in [lean_build._artifact_parts(path)]
    }


def _inspect(root: Path, module: str, name: str) -> dict[str, Any]:
    workspace = root / "inspection"
    workspace.mkdir(exist_ok=True)
    return lean_policy._inspect(
        root,
        [module],
        workspace,
        [name],
        ("propext", "Classical.choice", "Quot.sound"),
        deadline_after(60),
        None,
        root / ".lake/build/lib/lean",
        dependency=True,
        local_artifacts=_local_artifacts(root),
    )


@pytest.mark.integration
def test_imported_kernel_closure_and_definition_evidence(
    imported_project: tuple[Path, Path],
) -> None:
    root, library = imported_project
    _compile(
        root,
        library,
        "Dependency",
        "def library_value : Nat := 3\n"
        "theorem library_fact : library_value = 3 := rfl\n"
        "unsafe def implementation_only : Nat := 5\n",
    )
    result = _inspect(root, "Dependency", "library_fact")
    assert result["accepted"], result
    assert result["kernel_replay"]["scope"] == "referenced-dependency-closure"
    assert result["kernel_replay"]["modules"] == ["Dependency"]
    assert "implementation_only" in result["kernel_replay"]["skipped_implementation_declarations"]
    rows = {row["name"]: row for row in result["library_declarations"]}
    assert {"library_fact", "library_value"} <= rows.keys()
    assert "nat_lit 3" in rows["library_value"]["pretty_body"]
    assert "@OfNat.ofNat" in rows["library_value"]["pretty_body"]
    assert rows["library_value"]["body_hash"]
    assert rows["library_value"]["authority"] == "kernel-checked-referenced-compiled-library"


@pytest.mark.integration
def test_meta_budget_starts_after_import_and_replay(
    imported_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, library = imported_project
    _compile(root, library, "Dependency", "theorem library_fact : True := True.intro\n")
    # Importing Init already consumes more than this allowance. A fresh budget
    # must apply to each Meta query rather than all prior process allocations.
    monkeypatch.setattr(
        lean_policy,
        "INSPECTOR",
        lean_policy.INSPECTOR.replace(
            "`maxHeartbeats (200000 : Nat)", "`maxHeartbeats (1 : Nat)"
        ).replace(
            "(Meta.isProp type).run'",
            '(show MetaM Bool from do Core.checkMaxHeartbeats "budget regression"; Meta.isProp type).run\'',
        ),
    )
    result = _inspect(root, "Dependency", "library_fact")
    assert result["accepted"], result


@pytest.mark.integration
def test_library_evidence_traverses_project_seeds_without_relabeling_them(
    imported_project: tuple[Path, Path],
) -> None:
    root, library = imported_project
    _compile(root, library, "Dependency", "def library_value : Nat := 3\n")
    _compile(
        root,
        root / ".lake/build/lib/lean",
        "Main",
        "import Dependency\ntheorem own_fact : library_value = 3 := rfl\n",
        library,
    )
    result = _inspect(root, "Main", "own_fact")
    assert result["accepted"], result
    assert {row["name"] for row in result["library_declarations"]} == {"library_value"}


@pytest.mark.integration
@pytest.mark.parametrize("malformed", ["axiom", "proof"])
def test_global_axiom_hygiene_and_referenced_proof_acceptance(
    imported_project: tuple[Path, Path], malformed: str
) -> None:
    root, library = imported_project
    source = (
        "axiom unexpected_assumption : False\n"
        if malformed == "axiom"
        else (
            "import Lean\nopen Lean Elab Command\n"
            "set_option debug.skipKernelTC true in\n"
            "run_elab addDecl <| .thmDecl {\n"
            "  name := `forged, levelParams := [],\n"
            "  type := mkConst ``False, value := mkConst ``True.intro }\n"
            "theorem via_forged : False := forged\n"
        )
    )
    _compile(root, library, "Dependency", source + "theorem library_fact : True := True.intro\n")
    result = _inspect(root, "Dependency", "library_fact")
    if malformed == "proof":
        assert result["accepted"], result
        assert result["kernel_replay"]["declarations"] == 1
        assert result["kernel_replay"]["scanned_declarations"] > 1
        direct = _inspect(root, "Dependency", "forged")
        assert not direct["accepted"], direct
        assert "forged" in direct["details"]["stderr"], direct
        result = _inspect(root, "Dependency", "via_forged")
    assert not result["accepted"], result
    assert result["error"] == "Independent kernel inspection failed"
    expected = "unapproved noncompiler axiom" if malformed == "axiom" else "forged"
    assert expected in result["details"]["stderr"], result


@pytest.mark.integration
def test_every_generated_declaration_is_a_replay_root(
    imported_project: tuple[Path, Path],
) -> None:
    root, _ = imported_project
    library = root / ".lake/build/lib/lean"
    _compile(
        root,
        library,
        "Main",
        "import Lean\nopen Lean Elab Command\n"
        "set_option debug.skipKernelTC true in\n"
        "run_elab addDecl <| .thmDecl {\n"
        '  name := Name.str (Name.num `_private.Main 0) "hidden_forgery", levelParams := [],\n'
        "  type := mkConst ``False, value := mkConst ``True.intro }\n"
        "theorem visible_fact : True := True.intro\n",
    )
    workspace = root / "inspection"
    workspace.mkdir()
    result = lean_policy._inspect(
        root,
        ["Main"],
        workspace,
        [],
        ("propext", "Classical.choice", "Quot.sound"),
        deadline_after(60),
        None,
        library,
        local_artifacts=_local_artifacts(root),
    )
    assert not result["accepted"], result
    assert "hidden_forgery" in result["details"]["stderr"], result


@pytest.mark.integration
def test_safe_declaration_cannot_depend_on_skipped_implementation(
    imported_project: tuple[Path, Path],
) -> None:
    root, library = imported_project
    _compile(
        root,
        library,
        "Dependency",
        "import Lean\nopen Lean Elab Command\n"
        "unsafe def implementation_only : Nat := 5\n"
        "set_option debug.skipKernelTC true in\n"
        "run_elab addDecl <| .defnDecl {\n"
        "  name := `exposed, levelParams := [], type := mkConst ``Nat,\n"
        "  value := mkConst ``implementation_only, hints := .opaque, safety := .safe }\n"
        "theorem library_fact : True := True.intro\n",
    )
    result = _inspect(root, "Dependency", "exposed")
    assert not result["accepted"], result
    assert "depends on unchecked implementation" in result["details"]["stderr"], result


@pytest.mark.integration
def test_shadowed_compiler_import_cannot_enter_trusted_base(
    imported_project: tuple[Path, Path],
) -> None:
    root, library = imported_project
    (library / "Std").mkdir()
    generator = root / "ForgeShadow.lean"
    generator.write_text(
        "import Lean\nopen Lean\n"
        "def appendUnchecked (env : Environment) (decl : Declaration) : IO Environment := do\n"
        "  let action : CoreM Environment := do addDecl decl; getEnv\n"
        "  let options := ({} : Options).setBool `debug.skipKernelTC true\n"
        '  let context : Core.Context := { fileName := "forge", fileMap := default, options }\n'
        "  let result ← (action.run' context { env }).toBaseIO\n"
        '  IO.ofExcept (result.mapError (fun _ => "forge failed"))\n'
        "def main : IO Unit := do\n"
        "  let env ← importModules #[{module := `Init}] {} 0 (loadExts := false)\n"
        "  let env := env.setMainModule `Std.Time\n"
        "  let env ← appendUnchecked env (.thmDecl {\n"
        "    name := `shadowed_forgery, levelParams := [], type := mkConst ``False,\n"
        "    value := mkConst `shadowed_forgery })\n"
        "  let env ← appendUnchecked env (.thmDecl {\n"
        "    name := `library_fact, levelParams := [], type := mkConst ``True,\n"
        "    value := mkConst ``True.intro })\n"
        f"  writeModule env {json.dumps(str(library / 'Std/Time.olean'))} false\n"
    )
    subprocess.run(
        [compiler_executable(root, "lean"), "--run", str(generator)],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    compiler_library = Path(compiler_executable(root, "lean")).parent.parent / "lib/lean"
    # Lean selects a package root as a unit. Mirror its other artifacts through
    # real compiler paths so the test isolates one shadowed module; this kernel
    # regression deliberately bypasses the adapter's earlier alias rejection.
    for path in (compiler_library / "Std").iterdir():
        target = library / "Std" / path.name
        if not target.exists():
            target.symlink_to(path, target_is_directory=path.is_dir())
    for suffix in (".olean", ".olean.private", ".olean.server", ".ir"):
        path = compiler_library / f"Std{suffix}"
        if path.exists():
            (library / path.name).symlink_to(path)
    # The real compiler's Std module imports Std.Time transitively. Reimporting
    # Std under dependency-first search paths would trust the forgery, allowing
    # a renamed replay of its self-reference to typecheck against that forgery.
    with pytest.raises(ValueError, match="compiler"):
        _inspect(root, "Std", "library_fact")


@pytest.mark.integration
@pytest.mark.parametrize("native_type,value", [("Nat", "0"), ("Bool", "false")])
def test_native_reduction_in_unused_library_body_is_rejected_without_execution(
    imported_project: tuple[Path, Path], native_type: str, value: str
) -> None:
    root, library = imported_project
    marker = root / "inspection/native-reduction-ran"
    _compile(
        root,
        library,
        "Dependency",
        f"unsafe def markerImpl : {native_type} :=\n"
        f'  (unsafeIO do IO.FS.writeFile "{marker}" "executed"; pure {value}).toOption.getD {value}\n'
        f"@[implemented_by markerImpl] opaque markerValue : {native_type} := {value}\n"
        f"def innocent._sunfold : {native_type} := Lean.reduce{native_type} markerValue\n"
        "theorem library_fact : True := True.intro\n",
    )
    marker.unlink(missing_ok=True)
    result = _inspect(root, "Dependency", "library_fact")
    assert not result["accepted"], result
    assert "references native reduction" in result["details"]["stderr"], result
    assert not marker.exists()


@pytest.mark.integration
def test_semantic_surface_excludes_proof_only_dependencies(
    imported_project: tuple[Path, Path],
) -> None:
    root, library = imported_project
    _compile(
        root,
        library,
        "Dependency",
        "def library_value : Nat := 3\n"
        "theorem proof_only (n : Nat) : n = n := rfl\n"
        "theorem library_fact : library_value = library_value := proof_only library_value\n",
    )
    result = _inspect(root, "Dependency", "library_fact")
    assert result["accepted"], result
    rows = {row["name"]: row for row in result["library_declarations"]}
    assert rows["library_fact"]["semantic_surface"]
    assert rows["library_value"]["semantic_surface"]
    assert not rows["proof_only"]["semantic_surface"]
    assert "@Eq" in rows["library_fact"]["pretty_type"]


@pytest.mark.integration
def test_import_does_not_execute_dependency_initializers(
    imported_project: tuple[Path, Path],
) -> None:
    root, library = imported_project
    marker = root / "inspection/initializer-ran"
    _compile(
        root,
        library,
        "Dependency",
        f'initialize do IO.FS.writeFile "{marker}" "executed"\n'
        "theorem library_fact : True := True.intro\n",
    )
    marker.unlink(missing_ok=True)
    result = _inspect(root, "Dependency", "library_fact")
    assert result["accepted"], result
    assert not marker.exists()


@pytest.mark.integration
@pytest.mark.parametrize("transitive", [False, True])
def test_direct_and_transitive_tooling_import_is_unavailable(
    imported_project: tuple[Path, Path], transitive: bool
) -> None:
    root, library = imported_project
    tooling = root / ".lake/packages/repl/.lake/build/lib/lean"
    tooling.mkdir(parents=True)
    _compile(root, tooling, "REPL", "theorem tooling_fact : True := True.intro\n")
    module, name = "REPL", "tooling_fact"
    if transitive:
        _compile(
            root,
            library,
            "Dependency",
            "import REPL\ntheorem library_fact : True := tooling_fact\n",
            tooling,
        )
        module, name = "Dependency", "library_fact"
    result = _inspect(root, module, name)
    assert not result["accepted"], result
    assert "REPL" in result["details"]["stderr"], result


@pytest.mark.integration
def test_public_dependency_inspection_uses_real_closed_input_admission(tmp_path: Path) -> None:
    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Set LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN for pinned kernel integration")
    assert toolchain is not None
    (tmp_path / "lean-toolchain").write_text(toolchain)
    revision = "a" * 40
    (tmp_path / "lakefile.toml").write_text(
        'name = "project"\n[[lean_lib]]\nname = "Main"\n'
        '[[require]]\nname = "mathematics"\ngit = "https://example.invalid/math"\n'
        f'rev = "{revision}"\n'
    )
    (tmp_path / "Main.lean").write_text("")
    (tmp_path / "lake-manifest.json").write_text(
        json.dumps(
            {
                "version": "1.1.0",
                "packagesDir": ".lake/packages",
                "name": "project",
                "lakeDir": ".lake",
                "packages": [
                    {
                        "name": "mathematics",
                        "type": "git",
                        "url": "https://example.invalid/math",
                        "rev": revision,
                        "inputRev": revision,
                        "inherited": False,
                        "configFile": "lakefile.toml",
                        "manifestFile": "lake-manifest.json",
                        "subDir": None,
                    }
                ],
            }
        )
    )
    package = tmp_path / ".lake/packages/mathematics"
    library = package / ".lake/build/lib/lean"
    library.mkdir(parents=True)
    (package / "lakefile.toml").write_text(
        'name = "mathematics"\n[[lean_lib]]\nname = "Dependency"\n'
    )
    _compile(tmp_path, library, "Dependency", "theorem library_fact : True := True.intro\n")
    result = lean_policy.inspect_dependency(
        tmp_path,
        ["library_fact"],
        tmp_path / "inspection",
        imports=["Dependency"],
        deadline=deadline_after(60),
    )
    assert result["accepted"], result
    assert result["dependency_authority"] == "kernel-checked-referenced-compiled-library"
    assert result["library_declarations"][0]["name"] == "library_fact"
    local = tmp_path / ".lake/build/lib/lean"
    local.mkdir(parents=True, exist_ok=True)
    _compile(tmp_path, local, "Main", "theorem local_fact : True := True.intro\n")
    rejected = lean_policy.inspect_dependency(
        tmp_path,
        ["local_fact"],
        tmp_path / "local-inspection",
        imports=["Main"],
        deadline=deadline_after(60),
    )
    assert not rejected["accepted"], rejected
    assert "Main" in rejected["details"]["stderr"]


@pytest.mark.integration
def test_exact_artifact_map_ignores_planted_shadow_and_reports_local_context(
    imported_project: tuple[Path, Path],
) -> None:
    root, dependency = imported_project
    local = root / ".lake/build/lib/lean"
    _compile(root, dependency, "Dependency", "def library_value : Nat := 3\n")
    _compile(root, local, "Defs", "def local_value : Nat := 7\n")
    _compile(
        root,
        local,
        "Main",
        "import Dependency\nimport Defs\ntheorem own_fact : library_value + local_value = 10 := rfl\n",
        dependency,
        local,
    )
    exact = _local_artifacts(root)
    # Simulate unrelated artifacts planted after compilation in the old search path.
    _compile(root, local, "Dependency", "def library_value : Nat := 5\n")
    workspace = root / "inspection"
    workspace.mkdir()
    result = lean_policy._inspect(
        root,
        ["Main"],
        workspace,
        [],
        ("propext", "Classical.choice", "Quot.sound"),
        deadline_after(60),
        None,
        local,
        local_artifacts=exact,
    )
    assert result["accepted"], result
    rows = {row["name"]: row for row in result["library_declarations"]}
    assert rows["local_value"]["role"] == "project"
    assert "nat_lit 7" in rows["local_value"]["pretty_body"]
    assert rows["library_value"]["role"] == "dependency"
    assert "nat_lit 3" in rows["library_value"]["pretty_body"]
    assert "own_fact" not in rows


@pytest.mark.integration
def test_library_reuse_cannot_import_unrecorded_local_module(
    imported_project: tuple[Path, Path],
) -> None:
    root, _ = imported_project
    local = root / ".lake/build/lib/lean"
    _compile(root, local, "Main", "theorem local_fact : True := True.intro\n")
    workspace = root / "inspection"
    workspace.mkdir()
    result = lean_policy._inspect(
        root,
        ["Main"],
        workspace,
        ["local_fact"],
        (),
        deadline_after(60),
        None,
        local,
        dependency=True,
    )
    assert not result["accepted"], result
    assert "Main" in result["details"]["stderr"]


@pytest.mark.integration
def test_forged_stored_axiom_cache_cannot_hide_sorry(imported_project: tuple[Path, Path]) -> None:
    root, library = imported_project
    compiler = Path(compiler_executable(root, "lean")).parent.parent
    source = compiler / "src/lean/Lean/Util/CollectAxioms.lean"
    if "exportedAxiomsExt" not in source.read_text():
        pytest.skip("Pinned compiler predates stored axiom-cache extension")
    generator = root / "ForgeCache.lean"
    generator.write_text(
        "import Lean\nopen Lean\n"
        "unsafe def main : IO Unit := do\n"
        "  let env ← importModules #[{module := `Init}] {} 0 (loadExts := false)\n"
        "  let env := env.setMainModule `Dependency\n"
        "  let action : CoreM Environment := do\n"
        "    addDecl <| .thmDecl {\n"
        "      name := `hidden_hole, levelParams := [],\n"
        "      type := mkConst ``True, value := mkApp2 (mkConst ``sorryAx [.zero]) (mkConst ``True) (mkConst ``Bool.true) }\n"
        "    getEnv\n"
        '  let env ← action.toIO\' {fileName := "forge", fileMap := default} {env}\n'
        "  let mut found := false\n"
        "  let extensions ← persistentEnvExtensionsRef.get\n"
        "  let extensions := extensions.map fun ext =>\n"
        '    if ext.name.toString.endsWith "exportedAxiomsExt" then\n'
        "      {ext with exportEntriesFn := fun _ _ => .uniform (unsafeCast #[(`hidden_hole, (#[] : Array Name))])}\n"
        "    else ext\n"
        "  for ext in extensions do\n"
        '    if ext.name.toString.endsWith "exportedAxiomsExt" then found := true\n'
        '  if !found then throw <| IO.userError "axiom cache extension missing"\n'
        "  persistentEnvExtensionsRef.set extensions\n"
        f"  writeModule env {json.dumps(str(library / 'Dependency.olean'))} false\n"
    )
    subprocess.run(
        [str(compiler / "bin/lean"), "--run", str(generator)],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    # Confirm the stored metadata really lies under the compiler's usual lookup.
    driver = root / "ReadCache.lean"
    driver.write_text("import Dependency\n#print axioms hidden_hole\n")
    observed = subprocess.run(
        [str(compiler / "bin/lean"), str(driver)],
        cwd=root,
        env={**os.environ, "LEAN_PATH": str(library)},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert "does not depend on any axioms" in observed.stdout, observed.stdout
    with pytest.raises(ValueError, match="already verified declaration"):
        _inspect(root, "Dependency", "hidden_hole")
    rows = [
        json.loads(line)
        for line in (root / "inspection/declarations.jsonl").read_text().splitlines()
    ]
    assert rows[0]["axioms"] == ["sorryAx"]
