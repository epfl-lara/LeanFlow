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
    monkeypatch.setattr(lean_build, "compiled_library_paths", lambda root, **kwargs: [])
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
    monkeypatch.setattr(lean_build, "compiled_library_paths", lambda root, **kwargs: [library])
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
    monkeypatch.setattr(lean_build, "compiled_library_paths", lambda root, **kwargs: [library])
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


def test_closed_capture_interruption_requires_one_fresh_successful_invocation(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A safely closed interrupted header capture may recover only from fresh output."""
    root, layout, calls = build_case
    command = lean_build.isolated_command
    attempts = []
    interrupted = {
        "success": False,
        "ok": False,
        "error_code": "check_capture_failed",
        "error": "Interrupted capture",
        "capture_failure": {
            "boundary": "owned_stop",
            "leader_exited_before_cleanup": True,
            "leader_returncode": 0,
            "causes": [],
        },
        "capture_cleanup": {
            "processes_verified": True,
            "output_eof_verified": True,
            "closed": True,
        },
    }

    def capture(**kwargs: object) -> dict:
        attempts.append(kwargs)
        return interrupted if len(attempts) == 1 else command(**kwargs)

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is True
    assert len(attempts) == len(calls) + 1
    recovery = result["capture_recovery"]
    assert len(recovery) == 1 and recovery[0]["first_attempt"]["success"] is False
    assert recovery[0]["fresh_attempt"]["success"] is True


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


def _closed_capture_failure() -> dict:
    """Construct the exact internal contract emitted only after positive final closure."""
    return {
        "success": False,
        "ok": False,
        "error_code": "check_capture_failed",
        "error": "Interrupted capture",
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "capture_failure": {
            "boundary": "owned_stop",
            "leader_exited_before_cleanup": True,
            "leader_returncode": 0,
            "causes": [{"type": "NativeCleanupUnverified", "message": "first stop failed"}],
        },
        "capture_cleanup": {
            "processes_verified": True,
            "output_eof_verified": True,
            "closed": True,
        },
    }


@pytest.mark.parametrize(
    "changed",
    [
        {"capture_cleanup": {}},
        {
            "capture_cleanup": {
                "closed": True,
                "processes_verified": True,
                "output_eof_verified": False,
            }
        },
        {"error_code": "check_cancelled"},
        {"error_code": "check_timeout", "timed_out": True},
        {"error_code": "check_setup_failed"},
        {"error_code": None, "returncode": 1},
        {
            "capture_failure": {
                "boundary": "unknown",
                "leader_exited_before_cleanup": True,
                "leader_returncode": 0,
            }
        },
        {
            "capture_failure": {
                "boundary": "output_eof",
                "leader_exited_before_cleanup": True,
                "leader_returncode": 0,
            }
        },
        {
            "capture_failure": {
                "boundary": "owned_stop",
                "leader_exited_before_cleanup": False,
                "leader_returncode": 0,
            }
        },
        *[
            {
                "capture_failure": {
                    "boundary": "owned_stop",
                    "leader_exited_before_cleanup": True,
                    "leader_returncode": code,
                }
            }
            for code in (None, -9, 1, True)
        ],
    ],
)
def test_only_known_closed_capture_after_prior_normal_exit_is_eligible(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch, changed: dict
) -> None:
    """Ordinary compiler errors, cancellation and uncertain or killed leaders fail closed."""
    root, layout, _ = build_case
    attempts = []
    failure = {**_closed_capture_failure(), **changed}

    def capture(**kwargs):
        attempts.append(kwargs)
        return failure

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    assert lean_build.build_project(root, layout, root.parent / "check") == failure
    assert len(attempts) == 1


def test_a_second_capture_failure_stops_without_a_third_attempt(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never turn a build-global one-shot recovery into an unbounded retry loop."""
    root, layout, _ = build_case
    attempts = []

    def capture(**kwargs):
        attempts.append(kwargs)
        return _closed_capture_failure()

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is False
    assert len(attempts) == 2
    assert result["capture_recovery"][0]["fresh_attempt"]["success"] is False


def test_header_recovery_spends_the_only_retry_for_all_later_modules(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later module cannot acquire a second retry after successful header recovery."""
    root, layout, _ = build_case
    command = lean_build.isolated_command
    attempts = []

    def capture(**kwargs):
        attempts.append(kwargs)
        return command(**kwargs) if len(attempts) == 2 else _closed_capture_failure()

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is False and result["module"] == "Paper.Base"
    assert len(attempts) == 3 and len(result["capture_recovery"]) == 1


@pytest.mark.parametrize("stop", ["explicit_cancel", "outer_cancel", "deadline", "resource"])
def test_original_cancellation_deadline_and_resource_policy_prevent_retry(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """No new invocation may escape the original caller's cancellation or time budget."""
    from core.deadlines import operation_cancellation
    from leanflow_cli.formalization import lean_build_capture

    root, layout, _ = build_case
    attempts = []

    def capture(**kwargs):
        attempts.append(kwargs)
        return _closed_capture_failure()

    def cancelled():
        if stop == "resource" and attempts:
            raise RuntimeError("fixture aggregate memory policy failure")
        return bool(attempts) and stop == "explicit_cancel"

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    monkeypatch.setattr(
        lean_build_capture,
        "remaining_seconds",
        lambda _: 0 if attempts and stop == "deadline" else 10,
    )
    with operation_cancellation(lambda: bool(attempts) and stop == "outer_cancel"):
        if stop == "resource":
            with pytest.raises(RuntimeError, match="aggregate memory"):
                lean_build.build_project(root, layout, root.parent / "check", cancelled=cancelled)
        else:
            result = lean_build.build_project(
                root, layout, root.parent / "check", cancelled=cancelled
            )
            assert result["success"] is False
            assert result["capture_recovery"][0]["retry_skipped"] in {
                "cancelled",
                "deadline_expired",
            }
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "mutation", ["source", "new_source", "configuration", "compiler", "dependency", "header_output"]
)
def test_input_or_unowned_output_changes_prevent_fresh_capture(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Retain conflicting inputs rather than weakening guards to make recovery proceed."""
    root, layout, _ = build_case
    attempts = []
    conflicting = root.parent / "check/unowned.txt"

    def capture(**kwargs):
        attempts.append(kwargs)
        if mutation == "source":
            (root / "src/Paper/Base.lean").write_text("def base := 999\n")
        elif mutation == "new_source":
            (root / "Added.lean").write_text("def added := 1\n")
        elif mutation == "configuration":
            (root / "lean-toolchain").write_text("changed\n")
        elif mutation == "compiler":
            Path(kwargs["argv"][0]).write_bytes(b"changed compiler")
        elif mutation == "dependency":
            monkeypatch.setattr(
                lean_build, "dependency_inputs", lambda _: {"packages": {"changed": {}}}
            )
        else:
            conflicting.write_text("retain this conflicting output")
        return _closed_capture_failure()

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is False and len(attempts) == 1
    assert result["capture_recovery"][0]["retry_skipped"] == "inputs_or_outputs_changed"
    if mutation == "header_output":
        assert conflicting.read_text() == "retain this conflicting output"


def test_module_recovery_discards_partial_outputs_and_rechecks_from_source(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first command's unaccepted artifact must be absent when the fresh command starts."""
    root, layout, _ = build_case
    command = lean_build.isolated_command
    interrupted = False
    attempts = []

    def capture(**kwargs):
        nonlocal interrupted
        attempts.append(kwargs)
        if "--deps-json" not in kwargs["argv"] and not interrupted:
            interrupted = True
            args = kwargs["argv"]
            Path(args[args.index("-o") + 1]).write_bytes(b"unaccepted partial artifact")
            return _closed_capture_failure()
        return command(**kwargs)

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is True and len(attempts) == 5
    assert result["capture_recovery"][0]["first_attempt"]["success"] is False
    assert (root / "outputs/lib/lean/Paper/Base.olean").read_bytes() == b"fresh compiled Paper.Base"


def test_real_spawned_capture_failure_recovery_uses_a_different_process(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise physical command closure and a fresh independent capture with real children."""
    import sys

    from agent.runtime.harness.models import NativeCleanupUnverified
    from leanflow_cli.workflows.prover import check_process

    root, layout, _ = build_case
    command = lean_build.isolated_command
    kill = check_process._kill
    stopped = []
    results = []

    def transient(process, tree=None):
        stopped.append(process)
        if len(stopped) == 1:
            raise NativeCleanupUnverified("fixture first capture stop failed")
        return kill(process, tree)

    def capture(**kwargs):
        if "--deps-json" not in kwargs["argv"]:
            return command(**kwargs)
        expected = command(**kwargs)
        kwargs["argv"] = [
            sys.executable,
            "-I",
            "-c",
            "import sys,time;print(sys.argv[1],flush=True);time.sleep(.03)",
            expected["stdout"],
        ]
        result = check_process.isolated_command(**kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(check_process, "_sandbox_command", lambda argv, **_: list(argv))
    monkeypatch.setattr(check_process, "_kill", transient)
    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is True
    assert len(results) == 2 and results[0]["success"] is False and results[1]["success"] is True
    assert results[0]["stdout"] == "" and results[0]["capture_failure"]["leader_returncode"] == 0
    assert len({process.pid for process in stopped}) == 2
    assert all(process.returncode == 0 for process in stopped)


@pytest.mark.parametrize(
    "conflict", ["unexpected", "symlink", "hardlink", "setup", "earlier_artifact"]
)
def test_module_recovery_preserves_conflicting_outputs_or_changed_earlier_artifacts(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    """Do not delete conflicting files or reuse changed artifacts merely to enable another check."""
    root, layout, _ = build_case
    command = lean_build.isolated_command
    interrupted = False
    attempts = []
    retained = []
    sentinel = root.parent / "sentinel"
    sentinel.write_bytes(b"external sentinel")

    def capture(**kwargs):
        nonlocal interrupted
        attempts.append(kwargs)
        args = kwargs["argv"]
        earlier = root / "outputs/lib/lean/Paper/Base.olean"
        if (
            "--deps-json" not in args
            and not interrupted
            and (conflict != "earlier_artifact" or earlier.exists())
        ):
            interrupted = True
            output = Path(args[args.index("-o") + 1])
            if conflict == "unexpected":
                output.write_bytes(b"unaccepted partial artifact")
                unknown = Path(kwargs["workspace"]) / "unknown.txt"
                unknown.write_text("preserve me")
                retained.extend([output, unknown])
            elif conflict == "symlink":
                output.symlink_to(sentinel)
                retained.append(output)
            elif conflict == "hardlink":
                output.hardlink_to(sentinel)
                retained.append(output)
            elif conflict == "setup":
                setup = Path(
                    next(arg for arg in args if arg.startswith("--setup=")).removeprefix("--setup=")
                )
                setup.write_text("changed setup")
                retained.append(setup)
            else:
                earlier.write_bytes(b"changed earlier artifact")
                retained.append(earlier)
            return _closed_capture_failure()
        return command(**kwargs)

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is False and interrupted
    assert len(attempts) == (3 if conflict == "earlier_artifact" else 2)
    assert result["capture_recovery"][0]["retry_skipped"] == "inputs_or_outputs_changed"
    assert sentinel.read_bytes() == b"external sentinel"
    assert all(path.exists() for path in retained)


def test_changed_inputs_after_fresh_success_keep_both_attempts_and_fail_the_build(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh compiler success cannot overrule an input guard failure discovered afterward."""
    root, layout, _ = build_case
    command = lean_build.isolated_command
    attempts = []

    def capture(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            return _closed_capture_failure()
        result = command(**kwargs)
        (root / "src/Paper/Base.lean").write_text("def base := 999\n")
        return result

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is False and result["error_code"] == "check_inputs_changed"
    assert len(attempts) == 2
    record = result["capture_recovery"][0]
    assert (
        record["first_attempt"]["success"] is False and record["fresh_attempt"]["success"] is True
    )
    assert "guard_error" in record


def test_fresh_compiler_error_stays_rejected_and_preserves_original_capture_cause(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh ordinary type error is final, with the earlier capture problem kept as context."""
    root, layout, _ = build_case
    attempts = []

    def capture(**kwargs):
        attempts.append(kwargs)
        return (
            _closed_capture_failure()
            if len(attempts) == 1
            else {"success": False, "returncode": 1, "stderr": "type mismatch"}
        )

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is False and result["stderr"] == "type mismatch"
    assert len(attempts) == 2
    record = result["capture_recovery"][0]
    assert record["first_attempt"]["capture_failure"]["causes"][0]["message"] == "first stop failed"
    assert record["fresh_attempt"]["returncode"] == 1


def test_capture_retry_uses_only_remaining_original_deadline(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spending time on a failed capture never grants the fresh command a reset timeout."""
    import time

    root, layout, _ = build_case
    command = lean_build.isolated_command
    attempts = []

    def capture(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            time.sleep(0.02)
            return _closed_capture_failure()
        return command(**kwargs)

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    result = lean_build.build_project(
        root, layout, root.parent / "check", deadline=time.monotonic() + 10
    )
    assert result["success"] is True
    assert attempts[0]["timeout_s"] > attempts[1]["timeout_s"] > 0


def test_uncertain_fresh_capture_propagates_with_both_attempt_diagnostics(
    build_case: tuple[Path, dict, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncertain final ownership never becomes a failure return that could permit input disposal."""
    from agent.runtime.harness.models import NativeCleanupUnverified

    root, layout, _ = build_case
    attempts = []

    def capture(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            return _closed_capture_failure()
        raise NativeCleanupUnverified("fixture second capture retains owned inputs")

    monkeypatch.setattr(lean_build, "isolated_command", capture)
    with pytest.raises(NativeCleanupUnverified) as caught:
        lean_build.build_project(root, layout, root.parent / "check")
    assert len(attempts) == 2
    note = caught.value.__notes__[0]
    assert "first stop failed" in note
    assert '"cleanup_unverified": true' in note


@pytest.mark.parametrize("alias_kind", ["library_root", "internal_directory"])
def test_compiler_directory_alias_preserves_normal_build_and_input_guards(
    build_case: tuple[Path, dict, list], alias_kind: str
) -> None:
    """Allow normal builds through directory aliases while binding their target and descendants."""
    import os

    from leanflow_cli.formalization.lean_build_capture import compiler_version

    root, layout, calls = build_case
    library = root.parent / "compiler/lib/lean"
    if alias_kind == "library_root":
        target = root.parent / "actual-library"
        library.rename(target)
        link = library
        link.symlink_to(target, target_is_directory=True)
        leaf = target / "Init.olean"
    else:
        target = library / "Target"
        target.mkdir()
        leaf = target / "Extra.olean"
        leaf.write_bytes(b"first")
        link = library / "Alias"
        link.symlink_to(target, target_is_directory=True)
    result = lean_build.build_project(root, layout, root.parent / "check")
    assert result["success"] is True and len(calls) == 4
    assert "capture_recovery" not in result
    before = compiler_version(library)
    assert before[str(link)][5] == os.readlink(link)
    assert before[str(link)][-1] == ""
    assert any(Path(name).name == leaf.name and version[-1] for name, version in before.items())
    leaf.write_bytes(b"other")
    assert compiler_version(library) != before
    second = compiler_version(library)
    replacement = root.parent / "replacement-library"
    replacement.mkdir()
    (replacement / leaf.name).write_bytes(b"other")
    link.unlink()
    link.symlink_to(replacement, target_is_directory=True)
    assert compiler_version(library) != second


def test_late_output_eof_closure_never_authorizes_fresh_capture_or_output_reset(tmp_path):
    """Later pipe EOF cannot prove an unknown writer stopped, even after all owned cleanup passed."""
    from leanflow_cli.formalization.lean_build_capture import BuildCapture

    failure = _closed_capture_failure()
    failure["capture_failure"]["boundary"] = "output_eof"
    commands, guards, resets = [], [], []
    partial = tmp_path / "partial.olean"
    partial.write_bytes(b"unaccepted output must remain untouched")

    def command(**kwargs):
        commands.append(kwargs)
        return failure

    def reset_outputs():
        resets.append(True)
        partial.unlink()

    capture = BuildCapture(deadline=None, cancelled=None, verify_inputs=lambda: guards.append(True))
    result = capture.run(command, reset_outputs=reset_outputs)
    assert result is failure and result["success"] is False
    assert len(commands) == 1 and guards == resets == []
    assert capture.recovery == [] and capture.result(result) is result
    assert partial.read_bytes() == b"unaccepted output must remain untouched"
