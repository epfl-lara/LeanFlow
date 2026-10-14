"""Compile formalization modules without Git materialization or dependency Lake execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.formalization import lean_build
from leanflow_cli.formalization.lean_layout import _static_configuration


@pytest.fixture
def build_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict, list]:
    root = tmp_path / "project"
    (root / "src/Paper").mkdir(parents=True)
    (root / "src/Paper/Base.lean").write_text("def base := 1\n")
    (root / "src/Paper.lean").write_text("import Paper.Base\ndef whole := base\n")
    (root / "src/Paper/Unused.lean").write_text("def unrelated := 2\n")
    compiler = tmp_path / "compiler"
    (compiler / "bin").mkdir(parents=True)
    (compiler / "bin/lean").write_bytes(b"pinned compiler")
    (compiler / "lib/lean").mkdir(parents=True)
    (compiler / "lib/lean/Init.olean").write_bytes(b"pinned Init")
    layout = {
        "toolchain": "4.34.1",
        "libraries": [{"src_dir": "src", "roots": ["Paper"]}],
        "build_dir": "outputs",
        "lean_lib_dir": "outputs/lib/lean",
    }
    calls: list[dict] = []

    def command(**kwargs: object) -> dict:
        calls.append(kwargs)
        args = kwargs["argv"]
        assert "-j1" in args
        assert isinstance(args, list)
        assert args[0] == str(compiler / "bin/lean")
        if "--deps-json" in args:
            imports = []
            for file in args[args.index("--deps-json") + 1 :]:
                text = Path(file).read_text()
                names = ["Init"] + [
                    line.split()[1] for line in text.splitlines() if line.startswith("import ")
                ]
                imports.append(
                    {"errors": [], "result": {"imports": [{"module": name} for name in names]}}
                )
            return {"success": True, "stdout": json.dumps({"imports": imports})}
        setup_arg = next(arg for arg in args if arg.startswith("--setup="))
        setup = json.loads(Path(setup_arg.removeprefix("--setup=")).read_text())
        assert setup["plugins"] == setup["dynlibs"] == []
        assert setup["imports"] is None
        output = Path(args[args.index("-o") + 1])
        assert not output.exists(), "Every selected module must rebuild from source"
        output.write_bytes(b"fresh compiled " + setup["name"].encode())
        return {"success": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(lean_build, "compiler_executable", lambda *args: str(compiler / "bin/lean"))
    monkeypatch.setattr(lean_build, "compiled_library_paths", lambda root: [])
    monkeypatch.setattr(lean_build, "dependency_inputs", lambda root: {"packages": {}})
    monkeypatch.setattr(lean_build, "isolated_command", command)
    return root, layout, calls


def test_requested_module_rebuilds_local_import_closure_with_custom_source_and_build_dirs(
    build_case: tuple[Path, dict, list],
) -> None:
    root, layout, calls = build_case
    stale = root / "outputs/lib/lean/Paper/Base.olean"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"untrusted stale source output")
    result = lean_build.build_project(root, layout, root.parent / "check", files=["src/Paper.lean"])
    assert result["modules"] == ["Paper.Base", "Paper"]
    assert len(calls) == 3
    assert (root / "outputs/lib/lean/Paper.olean").is_file()
    assert not (root / "outputs/lib/lean/Paper/Unused.olean").exists()
    assert all(
        call.get("extra_writable_roots", ()) in [(), (root / "outputs/lib/lean",)] for call in calls
    )


def test_whole_project_build_includes_unreferenced_local_modules(
    build_case: tuple[Path, dict, list],
) -> None:
    root, layout, _ = build_case
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert set(result["modules"]) == {"Paper", "Paper.Base", "Paper.Unused"}


def test_cycles_are_rejected_without_compiling_any_module(
    build_case: tuple[Path, dict, list],
) -> None:
    root, layout, calls = build_case
    (root / "src/Paper/Base.lean").write_text("import Paper\n")
    with pytest.raises(ValueError, match="Cyclic local"):
        lean_build.build_project(root, layout, root.parent / "check")
    assert len(calls) == 1


def test_tooling_and_missing_dependencies_are_not_implicit_import_paths(
    build_case: tuple[Path, dict, list],
) -> None:
    root, layout, calls = build_case
    (root / "src/Paper/Base.lean").write_text("import REPL\n")
    with pytest.raises(ValueError, match="Missing admitted mathematical import REPL"):
        lean_build.build_project(root, layout, root.parent / "check")
    assert len(calls) == 1


def test_compilation_uses_exact_prebuilt_library_without_git_or_loading_lake(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout, _ = build_case
    library = root / ".lake/packages/library/.lake/build/lib/lean"
    library.mkdir(parents=True)
    for suffix in ("olean", "olean.server", "olean.private", "ir.sig", "ir"):
        (library / f"Library.{suffix}").write_bytes(b"recorded dependency part")
    (root / "src/Paper/Base.lean").write_text("import Library\n")
    monkeypatch.setattr(lean_build, "compiled_library_paths", lambda root: [library])
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"]
    setup = json.loads(
        sorted((root.parent / "check").glob("module-*/module-setup.json"))[-1].read_text()
    )
    assert len(setup["importArts"]["Library"][0]) == 3
    assert len(setup["importArts"]["Library"][1]) == 2
    assert not (library.parents[3] / ".git").exists()


@pytest.mark.parametrize("module", ["Init", "Init.Captured", "Paper", "Paper.Other"])
def test_duplicate_or_shadowing_libraries_are_rejected(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    root, layout, _ = build_case
    library = root / ".lake/packages/library/.lake/build/lib/lean"
    library.mkdir(parents=True)
    artifact = library.joinpath(*module.split(".")).with_suffix(".olean")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"shadow")
    monkeypatch.setattr(lean_build, "compiled_library_paths", lambda root: [library])
    with pytest.raises(ValueError, match="shadow|Ambiguous module root"):
        lean_build.build_project(root, layout, root.parent / "check")


def test_unconfigured_scripts_are_reported_separately_from_declared_project_modules(
    build_case: tuple[Path, dict, list],
) -> None:
    root, layout, calls = build_case
    (root / "Outside.lean").write_text("def unnoticed := 1\n")
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["excluded_sources"] == ["Outside.lean"]
    assert "Outside" not in result["modules"]


def test_process_cancellation_is_returned_without_launching_another_module(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout, _ = build_case
    failure = {"success": False, "error_code": "check_cancelled", "error": "Cancelled"}
    monkeypatch.setattr(lean_build, "isolated_command", lambda **kwargs: failure)
    assert lean_build.build_project(root, layout, root.parent / "check") == failure


def test_source_only_dependency_build_has_an_explicit_unsupported_result(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout, calls = build_case
    monkeypatch.setattr(
        lean_build,
        "dependency_inputs",
        lambda root: {"packages": {"safe_path_package": {"mode": "declarative-source"}}},
    )
    with pytest.raises(
        ValueError, match="unsupported_declarative_dependency_build.*safe_path_package"
    ):
        lean_build.build_project(root, layout, root.parent / "check")
    assert not calls


@pytest.mark.parametrize("version", ["4.31.0", "4.32.0-rc1"])
def test_legacy_compiler_setup_uses_flat_ir_slot_before_server_and_private(version: str) -> None:
    parts = {"Library": [["a.olean", "a.olean.server", "a.olean.private"], ["a.ir"]]}
    assert lean_build._setup_artifacts(parts, version) == {
        "Library": ["a.olean", "a.ir", "a.olean.server", "a.olean.private"]
    }
    assert lean_build._setup_artifacts({"Basic": [["basic.olean"], []]}, version) == {
        "Basic": ["basic.olean"]
    }


def test_split_ir_compiler_setup_retains_independent_ir_artifact_parts() -> None:
    parts = {"Library": [["a.olean", "a.olean.server", "a.olean.private"], ["a.ir.sig", "a.ir"]]}
    assert lean_build._setup_artifacts(parts, "4.34.1") == parts


@pytest.mark.parametrize("scope", ["package", "library"])
@pytest.mark.parametrize("build_type", ["debug", "relWithDebInfo", "minSizeRel", "unknown"])
def test_nondefault_lake_build_options_cannot_be_silently_omitted(
    tmp_path: Path, scope: str, build_type: str
) -> None:
    config = 'name = "paper"\n'
    if scope == "library":
        config += '[[lean_lib]]\nname = "Paper"\n'
    config += f'buildType = "{build_type}"\n'
    with pytest.raises(ValueError, match="Non-release Lake buildType"):
        _static_configuration(tmp_path / "lakefile.toml", config)


@pytest.mark.parametrize("setting", ["", 'buildType = "release"\n'])
def test_default_release_build_options_remain_supported(tmp_path: Path, setting: str) -> None:
    _static_configuration(
        tmp_path / "lakefile.toml", 'name = "paper"\n' + setting + '[[lean_lib]]\nname = "Paper"\n'
    )


def test_compile_options_and_exact_artifact_hashes_are_retained(
    build_case: tuple[Path, dict, list],
) -> None:
    from leanflow_cli.formalization.evidence import digest

    root, layout, calls = build_case
    layout["libraries"][0]["lean_options"] = {"autoImplicit": False, "maxSynthPendingDepth": 3}
    result = lean_build.build_project(root, layout, root.parent / "check")
    for call in calls[1:]:
        setup_arg = next(arg for arg in call["argv"] if arg.startswith("--setup="))
        setup = json.loads(Path(setup_arg.removeprefix("--setup=")).read_text())
        assert setup["options"] == layout["libraries"][0]["lean_options"]
        assert not call.get(
            "extra_writable_roots"
        ), "Compiler cannot write earlier module artifacts"
    for record in result["local_artifacts"].values():
        assert record["sha256"] == {
            name: digest(Path(name).read_bytes()) for parts in record["parts"] for name in parts
        }


@pytest.mark.parametrize("injection", ["Mathlib/Evil.olean", "Mathlib", "Paper/Extra.olean"])
def test_compiler_cannot_plant_extra_module_outputs(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch, injection: str
) -> None:
    root, layout, _ = build_case
    command = lean_build.isolated_command

    def inject(**kwargs: object) -> dict:
        result = command(**kwargs)
        if "--deps-json" not in kwargs["argv"]:
            path = Path(kwargs["workspace"]) / injection
            if path.suffix:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"planted artifact")
            else:
                path.mkdir()
        return result

    monkeypatch.setattr(lean_build, "isolated_command", inject)
    with pytest.raises(ValueError, match="Unexpected private compiler output"):
        lean_build.build_project(root, layout, root.parent / "check")


def test_compiler_mutation_of_earlier_artifact_fails_even_if_sandbox_is_bypassed(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, layout, _ = build_case
    command = lean_build.isolated_command

    def mutate(**kwargs: object) -> dict:
        result = command(**kwargs)
        earlier = root / "outputs/lib/lean/Paper/Base.olean"
        if "--deps-json" not in kwargs["argv"] and earlier.exists():
            earlier.write_bytes(b"changed compiled meaning")
        return result

    monkeypatch.setattr(lean_build, "isolated_command", mutate)
    with pytest.raises(ValueError, match="modified an earlier"):
        lean_build.build_project(root, layout, root.parent / "check")


def test_fresh_library_removes_stale_shadow_roots(build_case: tuple[Path, dict, list]) -> None:
    root, layout, _ = build_case
    stale = root / "outputs/lib/lean/Mathlib/Shadow.olean"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale malicious artifact")
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] and not (root / "outputs/lib/lean/Mathlib").exists()


@pytest.mark.parametrize(
    "build,library",
    [(".lake", ".lake/packages"), (".lake/packages/dep/out", ".lake/packages/dep/out/lib")],
)
def test_output_dependency_overlap_is_rejected_before_any_compiler_or_write(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch, build: str, library: str
) -> None:
    root, layout, calls = build_case
    package = root / ".lake/packages/dep"
    package.mkdir(parents=True)
    protected = package / "sentinel"
    protected.write_bytes(b"intact")
    layout.update(build_dir=build, lean_lib_dir=library)
    monkeypatch.setattr(
        lean_build,
        "dependency_inputs",
        lambda root: {
            "packages": {
                "dep": {
                    "mode": "compiled-library",
                    "directory": ".lake/packages/dep",
                    "library_dir": ".lake/build/lib/lean",
                }
            }
        },
    )
    with pytest.raises(ValueError, match="disjoint from dependencies"):
        lean_build.build_project(root, layout, root.parent / "check")
    assert not calls and protected.read_bytes() == b"intact"


def test_standard_main_executable_is_compiled_with_library(
    build_case: tuple[Path, dict, list],
) -> None:
    root, layout, _ = build_case
    (root / "src/Main.lean").write_text('import Paper\ndef main : IO Unit := IO.println "hi"\n')
    layout["executables"] = [
        {"src_dir": "src", "root": "Main", "lean_options": {"autoImplicit": False}}
    ]
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert "Main" in result["modules"] and result["modules"].index("Paper") < result[
        "modules"
    ].index("Main")


def test_library_cannot_import_an_executable_root(build_case: tuple[Path, dict, list]) -> None:
    root, layout, _ = build_case
    (root / "src/Main.lean").write_text("def main : IO Unit := pure ()\n")
    (root / "src/Paper/Base.lean").write_text("import Main\n")
    layout["executables"] = [{"src_dir": "src", "root": "Main"}]
    with pytest.raises(ValueError, match="cannot import an executable"):
        lean_build.build_project(root, layout, root.parent / "check")
