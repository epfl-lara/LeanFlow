"""Preserve enclosing inspector and compiler inputs when one-shot closure is uncertain."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from agent.runtime.harness.models import NativeCleanupUnverified
from leanflow_cli.formalization import lean_layout, lean_policy
from leanflow_cli.workflows.prover import (
    check_command_cleanup,
    check_process,
    libraries,
    planning_controller,
    source_transaction,
    type_profile,
)


@pytest.mark.parametrize(
    "stage",
    [
        "draft_build",
        "draft_inspect",
        "dependency_inspect",
        "layout",
        "profile_compile",
        "profile_inspect",
        "helper_config",
    ],
)
def test_enclosing_formalization_scratch_survives_uncertain_owned_command(
    tmp_path, monkeypatch, stage
):
    """Call the real wrapper and require its exact input directory to survive its failure."""
    retained = {}
    monkeypatch.setattr(check_command_cleanup, "_RETAINED", retained)
    monkeypatch.setattr(check_command_cleanup, "_RETAINED_DIRECTORIES", set())
    root = tmp_path / "project"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "fixture"\n')
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    (root / "Main.lean").write_text("theorem goal : True := by sorry\n")
    workspace = tmp_path / "checks"
    observed = []

    def uncertain(directory, project):
        observed.append(directory)
        (directory / "retained-input.txt").write_text("needed by an uncertain invocation")
        retained[directory / "private-owner"] = SimpleNamespace(
            workspace=directory, input_roots=(project,), closed=False
        )
        raise NativeCleanupUnverified("fixture invocation closure remains uncertain")

    def isolated(**kwargs):
        directory = kwargs["workspace"]
        argv = kwargs["argv"]
        if stage == "profile_inspect" and "-o" in argv:
            from pathlib import Path

            Path(argv[argv.index("-o") + 1]).write_bytes(b"compiled fixture")
            return {"success": True}
        return uncertain(directory, kwargs["project_root"])

    monkeypatch.setattr(check_process, "isolated_command", isolated)
    monkeypatch.setattr(lean_layout, "isolated_command", isolated)
    monkeypatch.setattr(lean_layout, "compiler_executable", lambda *args: "lean")
    monkeypatch.setattr(
        lean_policy,
        "inspect_layout",
        lambda *args, **kwargs: {"accepted": True, "lean_lib_dir": ".lake/build/lib/lean"},
    )
    monkeypatch.setattr(
        lean_policy,
        "_admitted_project_sources",
        lambda *args, **kwargs: {"Main.lean": "fixture-hash"},
    )
    monkeypatch.setattr(lean_policy, "module_for_file", lambda *args: "Main")
    monkeypatch.setattr(
        lean_policy,
        "_inspect",
        lambda project, imports, directory, *args, **kwargs: uncertain(directory, project),
    )
    monkeypatch.setattr(
        lean_policy,
        "build_project",
        lambda project, layout, directory, **kwargs: (
            uncertain(directory, project)
            if stage == "draft_build"
            else {"success": True, "local_artifacts": {}}
        ),
    )
    monkeypatch.setattr(
        libraries, "ensure_helper_library", lambda preview: uncertain(preview, preview)
    )
    monkeypatch.setattr(
        source_transaction, "begin_materialization", lambda runtime: {"id": "fixture-journal"}
    )
    monkeypatch.setattr(source_transaction, "recover_materialization", lambda *args: None)
    dag = SimpleNamespace(nodes=[], by_id=lambda: {})
    runtime = SimpleNamespace(
        root=root,
        documents={},
        dag=dag,
        state={"changes": []},
        _assert_sources=lambda: None,
        progress=SimpleNamespace(publish=lambda: None),
    )
    try:
        with pytest.raises(NativeCleanupUnverified, match="closure remains uncertain"):
            if stage in {"draft_build", "draft_inspect"}:
                lean_policy.inspect_draft(root, ["Main.lean"], workspace, ())
            elif stage == "dependency_inspect":
                lean_policy.inspect_dependency(root, ["goal"], workspace, imports=["Main"])
            elif stage == "layout":
                lean_layout.inspect_layout(root)
            elif stage in {"profile_compile", "profile_inspect"}:
                type_profile.compiled_type_profiles(
                    root=root,
                    source="theorem goal : True := by trivial\n",
                    names=["goal"],
                    workspace=workspace,
                    timeout_s=3,
                )
            else:
                planning_controller._materialize_locked(
                    runtime, dag, {"helper": "theorem helper : True := by trivial"}, []
                )
        assert len(observed) == 1
        assert observed[0].is_dir()
        assert (
            observed[0] / "retained-input.txt"
        ).read_text() == "needed by an uncertain invocation"
        assert (root / "lakefile.toml").read_text() == 'name = "fixture"\n'
    finally:
        for directory in observed:
            shutil.rmtree(directory, ignore_errors=True)
