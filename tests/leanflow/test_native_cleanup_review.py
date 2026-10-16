"""Pin cleanup fanout, retained enclosing inputs, and truthful capture failures."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.runtime.harness.models import NativeCleanupUnverified
from leanflow_cli.workflows.prover import check_command_cleanup, check_process, libraries
from leanflow_cli.workflows.prover.verification import LeanVerifier
from tests.leanflow.library_checkout_fixture import REQUEST


@pytest.fixture(autouse=True)
def isolated_retention_registry(monkeypatch):
    """Keep synthetic retained owners from leaking across cleanup regression fixtures."""
    monkeypatch.setattr(check_command_cleanup, "_RETAINED", {})
    monkeypatch.setattr(check_command_cleanup, "_RETAINED_DIRECTORIES", set())


def test_retained_command_failure_still_closes_later_owners(monkeypatch, tmp_path):
    """A failed ownership retry must not strand another retained invocation."""
    calls = []

    def failed():
        calls.append("failed")
        raise NativeCleanupUnverified("fixture pipe remains open")

    owners = {
        tmp_path / "a": SimpleNamespace(workspace=tmp_path, close=failed),
        tmp_path / "b": SimpleNamespace(workspace=tmp_path, close=lambda: calls.append("later")),
    }
    monkeypatch.setattr(check_command_cleanup, "_RETAINED", owners)
    with pytest.raises(NativeCleanupUnverified):
        check_command_cleanup.close_commands(tmp_path)
    assert calls == ["failed", "later"]


@pytest.mark.parametrize("all_workspaces", [False, True])
def test_retained_command_failure_still_closes_warm_workers(monkeypatch, tmp_path, all_workspaces):
    """Workspace and exit cleanup must fan out before preserving the original failure."""
    calls = []

    def failed(*args):
        calls.append("command")
        raise NativeCleanupUnverified("fixture retained command")

    worker = SimpleNamespace(close=lambda: calls.append("warm"))
    monkeypatch.setattr(check_process, "close_commands", failed)
    monkeypatch.setattr(check_process, "_WORKERS", {(tmp_path / "project", tmp_path): worker})
    with pytest.raises(NativeCleanupUnverified):
        (
            check_process._close_all()
            if all_workspaces
            else check_process.close_check_workers(tmp_path)
        )
    assert calls == ["command", "warm"]
    assert not check_process._WORKERS


@pytest.mark.parametrize("operation", ["helper", "final", "library"])
def test_controller_temporary_workspace_preserves_retained_command_inputs(
    monkeypatch, tmp_path, operation
):
    """A surrounding temporary context cannot override an uncertain command owner's retention."""
    retained = {}
    captured = {}
    monkeypatch.setattr(check_command_cleanup, "_RETAINED", retained)

    def isolated(**kwargs):
        workspace = kwargs["workspace"].resolve()
        captured["workspace"] = workspace
        (workspace / "retain-me.txt").write_text("live invocation input")
        retained[workspace / "private"] = SimpleNamespace(
            workspace=workspace, input_roots=(kwargs["project_root"].resolve(),), closed=False
        )
        raise NativeCleanupUnverified("fixture retained command inputs")

    monkeypatch.setattr(check_process, "isolated_command", isolated)
    project = tmp_path / "project"
    project.mkdir()
    (project / "Main.lean").write_text("theorem fixture : True := by trivial\n")
    verifier = LeanVerifier(project, ())
    with pytest.raises(NativeCleanupUnverified):
        if operation == "helper":
            verifier.compile_module("Main.lean")
        elif operation == "final":
            verifier.final([project / "Main.lean"])
        else:
            libraries._run_update(project, [], ["fixture"], 1)
    workspace = captured["workspace"]
    try:
        assert workspace.is_dir()
        assert (workspace / "retain-me.txt").read_text() == "live invocation input"
    finally:
        import shutil

        shutil.rmtree(workspace, ignore_errors=True)


def test_uncertain_library_update_preserves_private_project_and_propagates(monkeypatch, tmp_path):
    """Do not recover/promote/delete a staged project while its update may still be alive."""
    import json

    project = tmp_path / "project"
    project.mkdir()
    (project / "lakefile.toml").write_text('name = "fixture"\n')
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.30.0\n")
    (project / "lake-manifest.json").write_text(json.dumps({"version": "1.2.0", "packages": []}))
    monkeypatch.delenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", raising=False)
    monkeypatch.setattr(
        libraries, "_public_connection", lambda *_: (SimpleNamespace(close=lambda: None), "", "")
    )
    retained = {}
    captured = {}
    monkeypatch.setattr(check_command_cleanup, "_RETAINED", retained)

    def isolated(**kwargs):
        workspace = kwargs["workspace"].resolve()
        stage = kwargs["project_root"].resolve()
        captured.update(workspace=workspace, stage=stage)
        retained[workspace / "private"] = SimpleNamespace(
            workspace=workspace, input_roots=(stage,), closed=False
        )
        raise NativeCleanupUnverified("fixture update closure unknown")

    monkeypatch.setattr(check_process, "isolated_command", isolated)
    try:
        with pytest.raises(NativeCleanupUnverified):
            libraries.install_libraries(project, [REQUEST])
        assert captured["workspace"].is_dir()
        assert (captured["stage"] / "lakefile.toml").is_file()
        assert (project / "lakefile.toml").read_text() == 'name = "fixture"\n'
    finally:
        import shutil

        if captured:
            shutil.rmtree(captured["workspace"], ignore_errors=True)
            shutil.rmtree(captured["stage"].parent, ignore_errors=True)


def test_capture_cleanup_retry_does_not_report_retained_deleted_inputs(monkeypatch, tmp_path):
    """A verified final retry still fails the check but cannot claim inputs remain retained."""
    import sys

    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(check_process, "_sandbox_command", lambda argv, **kwargs: list(argv))
    original = check_process._kill
    calls = []

    def transient(process, tree=None):
        calls.append(process)
        if len(calls) == 1:
            raise NativeCleanupUnverified("fixture first cleanup failed; retain its inputs")
        return original(process, tree)

    monkeypatch.setattr(check_process, "_kill", transient)
    result = check_process.isolated_command(
        project_root=project,
        workspace=workspace,
        argv=[sys.executable, "-I", "-c", "import time;time.sleep(.05)"],
        timeout_s=3,
    )
    assert result["success"] is False
    assert result["error_code"] == "check_capture_failed"
    assert "retain" not in result["error"]
    assert result["capture_cleanup"] == {
        "processes_verified": True,
        "output_eof_verified": True,
        "closed": True,
    }
    assert result["capture_failure"]["boundary"] == "owned_stop"
    assert any(
        "fixture first cleanup failed" in item["message"]
        for item in result["capture_failure"]["causes"]
    )
    assert len(calls) == 2
    assert not any(
        owner.workspace == workspace for owner in check_command_cleanup._RETAINED.values()
    )


def test_enclosing_workspace_and_stage_are_released_only_after_last_verified_retry(tmp_path):
    """Retain both private roots during uncertainty, then reclaim them after positive closure."""
    allowed = False

    def kill(process, tree):
        if not allowed:
            raise NativeCleanupUnverified("fixture owner remains uncertain")

    with check_command_cleanup.command_workspace(prefix="stage-", directory=tmp_path) as stage:
        project = stage / "project"
        project.mkdir()
        (project / "source.txt").write_text("source input")
        with check_command_cleanup.command_workspace(
            prefix="workspace-", directory=tmp_path
        ) as workspace:
            private = workspace / "private"
            private.mkdir()
            owner = check_command_cleanup.CommandOwnership(
                workspace,
                private,
                SimpleNamespace(stdout=None, stderr=None),
                SimpleNamespace(),
                kill,
                lambda process: None,
                input_roots=(project,),
            )
            with pytest.raises(NativeCleanupUnverified):
                owner.close()
    assert stage.is_dir() and workspace.is_dir() and private.is_dir()
    allowed = True
    check_command_cleanup.close_commands(workspace)
    assert owner.closed
    assert not stage.exists() and not workspace.exists()
    assert not check_command_cleanup._RETAINED_DIRECTORIES


def test_capture_diagnostics_preserve_bounded_forcibly_redacted_original_cause(
    monkeypatch, tmp_path
):
    """Diagnostic evidence survives optional redaction being disabled without leaking credentials."""
    import json
    import sys

    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    monkeypatch.setattr(check_process, "_sandbox_command", lambda argv, **kwargs: list(argv))
    original = check_process._kill
    calls = []
    secret = "ghp_" + "x" * 40

    def transient(process, tree=None):
        calls.append(process)
        if len(calls) == 1:
            raise NativeCleanupUnverified("original boundary " + secret + " detail" * 10000)
        return original(process, tree)

    monkeypatch.setattr(check_process, "_kill", transient)
    result = check_process.isolated_command(
        project_root=project,
        workspace=workspace,
        argv=[sys.executable, "-I", "-c", "import time;time.sleep(.03)"],
        timeout_s=3,
    )
    diagnostic = result["capture_failure"]
    encoded = json.dumps(diagnostic)
    assert secret not in encoded and "original boundary" in encoded
    assert len(encoded) < 24000
    assert diagnostic["boundary"] == "owned_stop"
    assert diagnostic["leader_exited_before_cleanup"] is True
    assert diagnostic["leader_returncode"] == 0
    assert any(
        frame["function"] == "transient"
        for cause in diagnostic["causes"]
        for frame in cause["frames"]
    )
    assert all(
        set(frame) == {"file", "function", "line"}
        for cause in diagnostic["causes"]
        for frame in cause["frames"]
    )


def test_uncertain_final_physical_eof_never_returns_positive_capture_cleanup(monkeypatch, tmp_path):
    """A later uncertain close raises and retains ownership instead of returning retry evidence."""
    import sys

    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    monkeypatch.setattr(check_process, "_sandbox_command", lambda argv, **kwargs: list(argv))
    original_kill, original_eof = check_process._kill, check_process._require_output_eof
    calls = []
    secret = "ghp_" + "q" * 40

    def transient(process, tree=None):
        calls.append(process)
        if len(calls) == 1:
            raise NativeCleanupUnverified("fixture first stop failed " + secret + " detail" * 10000)
        return original_kill(process, tree)

    def uncertain_eof(process):
        raise NativeCleanupUnverified("fixture physical EOF remains uncertain")

    monkeypatch.setattr(check_process, "_kill", transient)
    monkeypatch.setattr(check_process, "_require_output_eof", uncertain_eof)
    try:
        with pytest.raises(
            NativeCleanupUnverified, match="physical EOF remains uncertain"
        ) as caught:
            check_process.isolated_command(
                project_root=project,
                workspace=workspace,
                argv=[sys.executable, "-I", "-c", "import time;time.sleep(.03)"],
                timeout_s=3,
            )
        owners = list(check_command_cleanup._RETAINED.values())
        assert len(owners) == 1
        assert owners[0].private.is_dir()
        assert owners[0].processes_verified is True
        assert owners[0].output_eof_verified is False and owners[0].closed is False
        note = "\n".join(getattr(caught.value, "__notes__", []))
        assert "fixture first stop failed" in note
        assert secret not in note and len(note) < 66000
        assert '"capture_cleanup"' not in note and '"leader_returncode"' not in note
    finally:
        for owner in tuple(check_command_cleanup._RETAINED.values()):
            owner.kill, owner.require_eof = original_kill, original_eof
            owner.close()


def test_proof_verifier_does_not_retry_or_publish_a_safely_closed_capture_failure(
    monkeypatch, tmp_path
):
    """The new build-only recovery contract cannot grant acceptance to ordinary proof callers."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "Main.lean").write_text("theorem fixture : True := by trivial\n")
    attempts = []

    def interrupted(**kwargs):
        attempts.append(kwargs)
        args = kwargs["argv"]
        from pathlib import Path

        Path(args[args.index("-o") + 1]).write_bytes(b"unaccepted first output")
        return {
            "success": False,
            "ok": False,
            "error_code": "check_capture_failed",
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "capture_failure": {
                "boundary": "owned_stop",
                "leader_exited_before_cleanup": True,
                "leader_returncode": 0,
            },
            "capture_cleanup": {
                "closed": True,
                "processes_verified": True,
                "output_eof_verified": True,
            },
        }

    monkeypatch.setattr(check_process, "isolated_command", interrupted)
    verifier = LeanVerifier(project, ())
    result = verifier.compile_module("Main.lean")
    assert result["accepted"] is False and result["success"] is False
    assert len(attempts) == 1
    assert not verifier._module_output("Main.lean").exists()


def test_failed_warm_worker_does_not_prevent_later_worker_cleanup(monkeypatch, tmp_path):
    """Keep only the failing warm worker registered after an attempted whole-workspace close."""
    calls = []

    def failed():
        calls.append("failed")
        raise NativeCleanupUnverified("fixture warm worker")

    first = (tmp_path / "first", tmp_path)
    second = (tmp_path / "second", tmp_path)
    monkeypatch.setattr(
        check_process,
        "_WORKERS",
        {
            first: SimpleNamespace(close=failed),
            second: SimpleNamespace(close=lambda: calls.append("later")),
        },
    )
    with pytest.raises(NativeCleanupUnverified):
        check_process.close_check_workers(tmp_path)
    assert calls == ["failed", "later"]
    assert set(check_process._WORKERS) == {first}


def test_native_tool_verified_final_retry_does_not_claim_deleted_input_retention(
    monkeypatch, tmp_path
):
    """The native tool path must distinguish interrupted capture from unresolved final closure."""
    import sys
    import time

    from leanflow_cli.workflows.prover import native_command

    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    original = native_command.cleanup_group
    calls = []

    def transient(process, tree):
        calls.append(process)
        if len(calls) == 1:
            raise NativeCleanupUnverified("fixture transient cleanup; retain invocation inputs")
        return original(process, tree)

    monkeypatch.setattr(native_command, "cleanup_group", transient)
    monkeypatch.setattr(native_command, "sandbox_command", lambda argv, policy: list(argv))
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)']); "
        "print('partial',flush=True);time.sleep(.05)"
    )
    try:
        result = native_command.run_command(
            [sys.executable, "-I", "-c", script],
            snapshot=project,
            directory=workspace,
            deadline=time.monotonic() + 3,
        )
        assert result["success"] is False and result["status"] == "capture_failed"
        assert "retain" not in result["error"]
        assert len(calls) == 2
        assert not list((workspace / "tmp").iterdir())
    finally:
        for owner in tuple(check_command_cleanup._RETAINED.values()):
            if owner.workspace == workspace.resolve():
                owner.kill = original
                owner.close()
