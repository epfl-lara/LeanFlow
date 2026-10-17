"""Pin compiler-only inspector readiness without mathematical acceptance or dependency scans."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.formalization import inspector_readiness as readiness


def _record(compiler: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "readiness_only": True,
        "success": True,
        "proof_acceptance": False,
        "mode": "compiler-only-empty-kernel-replay",
        "checker_sha256": readiness._checker_sha256(),
        "compiler_library": str(compiler),
        "requested_modules": ["Init.Prelude"],
        "requested_names": [],
        "declaration_rows": 0,
        "origin_parent_rows": 0,
        "library_rows": 0,
        "kernel_replay": {
            "modules": [],
            "declarations": 0,
            "roots": [],
            "scanned_modules": [],
            "scanned_declarations": 0,
            "axiom_hygiene_scope": "noncompiler-import-closure",
            "skipped_implementation_declarations": [],
            "trusted_modules": ["Init.Prelude"],
            "imports": [
                {
                    "module": "Init.Prelude",
                    "path": str(compiler / "Init/Prelude.olean"),
                    "role": "compiler",
                }
            ],
            "scope": "referenced-dependency-closure",
        },
    }


@pytest.fixture
def compiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    prefix = tmp_path / "compiler"
    library = prefix / "lib/lean"
    library.mkdir(parents=True)
    monkeypatch.setattr(readiness, "compiler_executable", lambda *_: str(prefix / "bin/lean"))
    monkeypatch.delenv("LEAN_PATH", raising=False)
    monkeypatch.delenv("ELAN_TOOLCHAIN", raising=False)
    return library


def test_readiness_runs_only_empty_exact_compiler_replay(
    tmp_path: Path, compiler: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    cancel = lambda: False

    def run(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        argv = kwargs["argv"]
        work = kwargs["workspace"]
        assert argv[1:3] == ["-j1", "--run"]
        assert argv[4:8] == [str(work), str(compiler), str(compiler), "readiness"]
        assert argv[9:] == ["Init.Prelude", "--"]
        assert json.loads(Path(argv[8]).read_text()) == {"paths": [], "artifacts": {}, "roles": {}}
        assert kwargs["cancelled"] is cancel
        for filename in readiness._REPORTS.values():
            (work / filename).write_text("")
        (work / "inspector-readiness.json").write_text(json.dumps(readiness._MARKER))
        (work / "kernel-replay.json").write_text(json.dumps(_record(compiler)["kernel_replay"]))
        return {"success": True}

    monkeypatch.setattr(readiness, "isolated_command", run)
    before = time.monotonic()
    record = readiness.check_inspector_runtime(
        tmp_path, tmp_path / "checks", deadline=before + 30, cancelled=cancel
    )
    assert readiness.valid_inspector_readiness(record)
    assert "accepted" not in record and "profiles" not in record and "obligations" not in record
    assert len(calls) == 1 and 0 < calls[0]["timeout_s"] <= 30
    assert calls[0]["workspace"].is_relative_to(tmp_path / "checks")
    assert calls[0][
        "workspace"
    ].is_dir()  # Caller-owned diagnostics survive until verified cleanup.


@pytest.mark.parametrize(
    "fault",
    [
        {"checker_sha256": "obsolete"},
        {"success": False},
        {"readiness_only": 1},
        {"proof_acceptance": True},
        {"proof_acceptance": 0},
        {"declaration_rows": 1},
        {"origin_parent_rows": 1},
        {"library_rows": 1},
        {"library_rows": False},
        {"requested_names": ["True.intro"]},
        {"requested_modules": ["Lean"]},
        {"mode": "closure"},
        {"accepted": True},
        {"schema_version": True},
        {"compiler_library": "relative"},
        {"kernel_replay": None},
    ],
)
def test_readiness_record_cannot_be_promoted_or_reused_with_stale_checker(
    compiler: Path, fault: dict[str, Any]
) -> None:
    record = _record(compiler)
    record.update(fault)
    assert not readiness.valid_inspector_readiness(record)


@pytest.mark.parametrize(
    "fault",
    [
        {"declarations": 1},
        {"declarations": False},
        {"roots": ["True.intro"]},
        {"modules": ["Main"]},
        {"scanned_modules": ["Mathlib"]},
        {"scanned_declarations": 1},
        {"trusted_modules": ["Lean"]},
        {"imports": []},
        {"scope": "project-only"},
        {"axiom_hygiene_scope": "project-only"},
        {"unexpected": "suppression"},
    ],
)
def test_readiness_requires_empty_kernel_replay(compiler: Path, fault: dict[str, Any]) -> None:
    record = _record(compiler)
    record["kernel_replay"].update(fault)
    assert not readiness.valid_inspector_readiness(record)


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "dependency"),
        ("path", "/outside/Init/Prelude.olean"),
        ("module", "Unrecorded"),
        ("path", "/compiler/../external.olean"),
    ],
)
def test_readiness_trusts_only_recorded_compiler_imports(
    compiler: Path, field: str, value: str
) -> None:
    record = _record(compiler)
    record["kernel_replay"]["imports"][0][field] = value
    assert not readiness.valid_inspector_readiness(record)


def test_compile_failure_keeps_diagnostics_and_never_accepts(
    tmp_path: Path, compiler: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = {"success": False, "stderr": "unknown constant 'Lean.reduceBool'", "timed_out": False}
    monkeypatch.setattr(readiness, "isolated_command", lambda **_: failed)
    result = readiness.check_inspector_runtime(
        tmp_path, tmp_path / "checks", deadline=None, cancelled=None
    )
    assert result["success"] is False and result["proof_acceptance"] is False
    assert result["details"] == failed and not readiness.valid_inspector_readiness(result)
    assert "accepted" not in result


def test_stop_after_compilation_cannot_publish_readiness(
    tmp_path: Path, compiler: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = False

    def run(**_: Any) -> dict[str, Any]:
        nonlocal stopped
        stopped = True
        return {"success": True}

    monkeypatch.setattr(readiness, "isolated_command", run)
    result = readiness.check_inspector_runtime(
        tmp_path, tmp_path / "checks", deadline=None, cancelled=lambda: stopped
    )
    assert result["success"] is False and result["proof_acceptance"] is False
    assert result["details"]["error_code"] == "check_cancelled"


@pytest.mark.parametrize("kind", ["cancel", "deadline"])
def test_stop_before_readiness_has_no_compiler_or_workspace_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(
        readiness, "compiler_executable", lambda *_: pytest.fail("No launch after Stop")
    )
    workspace = tmp_path / "checks"
    result = readiness.check_inspector_runtime(
        tmp_path,
        workspace,
        deadline=time.monotonic() - 1 if kind == "deadline" else None,
        cancelled=(lambda: True) if kind == "cancel" else None,
    )
    assert result["success"] is False and not workspace.exists()
    assert result["details"]["error_code"] == (
        "check_cancelled" if kind == "cancel" else "check_timeout"
    )


def test_empty_reports_cannot_hide_nonempty_declarations(
    tmp_path: Path, compiler: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(**kwargs: Any) -> dict[str, Any]:
        work = kwargs["workspace"]
        for filename in readiness._REPORTS.values():
            (work / filename).write_text("{}\n")
        (work / "inspector-readiness.json").write_text(json.dumps(readiness._MARKER))
        (work / "kernel-replay.json").write_text(json.dumps(_record(compiler)["kernel_replay"]))
        return {"success": True}

    monkeypatch.setattr(readiness, "isolated_command", run)
    result = readiness.check_inspector_runtime(
        tmp_path, tmp_path / "checks", deadline=None, cancelled=None
    )
    assert not result["success"] and "nonempty" in result["details"]["error"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "case",
    ["ready", "artifact", "role", "module", "name", "mode", "compile_fail", "stop", "legacy_quote"],
)
def test_real_readiness_fence_and_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Run readiness and refusal paths on the explicitly selected installed compiler."""
    from leanflow_cli.formalization.preparation_resources import supervise_preparation
    from leanflow_cli.workflows.project_setup.execution import build_setup_execution_policy

    toolchain = os.environ.get("LEANFLOW_FORMALIZATION_TEST_TOOLCHAIN")
    if not toolchain:
        pytest.skip("Select an installed Lean compiler for actual readiness checks")
    if case == "legacy_quote" and "v4.35" not in toolchain:
        pytest.skip("The retained unknown-constant regression requires Lean 4.35")
    root, stage = tmp_path / "project", tmp_path / "supervision"
    root.mkdir()
    stage.mkdir()
    (root / "lean-toolchain").write_text(toolchain)
    policy = build_setup_execution_policy(
        stage,
        canonical_root=root,
        toolchain=toolchain,
        network_allowed=False,
        deadline=time.monotonic() + 120,
        max_owned_rss_bytes=16 * 1024**3,
    )
    original = readiness.isolated_command
    if case in {"compile_fail", "legacy_quote"}:
        source = (
            readiness.INSPECTOR + "\nthis is not Lean syntax\n"
            if case == "compile_fail"
            else readiness.INSPECTOR.replace(
                "dependency == `Lean.reduceBool || dependency == `Lean.reduceNat",
                "dependency == ``Lean.reduceBool || dependency == ``Lean.reduceNat",
            )
        )
        monkeypatch.setattr(readiness, "INSPECTOR", source)

    def command(**kwargs: Any) -> dict[str, Any]:
        argv = list(kwargs["argv"])
        inputs = Path(argv[8])
        config = json.loads(inputs.read_text())
        if case == "artifact":
            config["artifacts"] = {"Fake": [["/unknown.olean"], []]}
        elif case == "role":
            config["roles"] = {"Fake": "dependency"}
        elif case == "module":
            argv[9] = "Lean"
        elif case == "name":
            argv.append("True.intro")
        elif case == "mode":
            argv[7] = "unsupported-readiness"
        inputs.write_text(json.dumps(config))
        return original(**{**kwargs, "argv": argv})

    monkeypatch.setattr(readiness, "isolated_command", command)
    with supervise_preparation(policy) as cancelled:
        result = readiness.check_inspector_runtime(
            root,
            root / ".leanflow/readiness",
            deadline=policy.deadline,
            cancelled=(lambda: True) if case == "stop" else cancelled.is_set,
        )
        if case == "ready":
            assert readiness.valid_inspector_readiness(result), result
        else:
            assert result["success"] is False and result["proof_acceptance"] is False, result
            if case == "legacy_quote":
                diagnostic = result["details"]["stdout"]
                assert (
                    "unknown constant" in diagnostic.lower() and "Lean.reduceBool" in diagnostic
                ), result
            elif case == "mode":
                assert "unsupported replay scope" in result["details"]["stderr"], result
            elif case not in {"stop", "compile_fail"}:
                assert "readiness" in result["details"]["stderr"], result
    assert policy.usage.cleanup_verified
