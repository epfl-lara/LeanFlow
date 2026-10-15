"""Exercise actual CLI Stop before native admission and exact verified owner handoff."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from leanflow_cli.formalization import preflight_owner as owner
from leanflow_cli.workflows import workflow_state

REPO = Path(__file__).resolve().parents[2]


def _wait_json(path: Path, process: subprocess.Popen[str], timeout: float = 12) -> dict:
    """Wait finitely for a fixture boundary without treating an exited child as success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            pytest.fail(f"Fixture exited before boundary: {stdout[-3000:]} {stderr[-3000:]}")
        time.sleep(0.02)
    pytest.fail(f"Fixture did not reach {path.name}")


def test_actual_cli_reexec_preserves_pid_flags_and_public_stop_cancels_preflight(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "project").resolve()
    root.mkdir()
    (root / "paper.tex").write_text(r"\begin{theorem}A true proposition.\end{theorem}")
    status = root / ".leanflow/workflow-state/live_status.json"
    status.parent.mkdir(parents=True)
    status.write_text(
        json.dumps(
            {
                "phase": "completed",
                "terminal": True,
                "process_id": 0,
                "target_symbol": "preserved.mathematics",
                "proof_solved": 17,
            }
        )
    )
    script = tmp_path / "preflight.py"
    before, admitted, actor_pid = (
        tmp_path / name for name in ("before.json", "admitted.json", "actor-pid")
    )
    script.write_text(textwrap.dedent(f"""\
        import importlib,json,os,sys,time
        from pathlib import Path
        sys.path.insert(0,{str(REPO)!r})
        from agent.runtime.harness.processes import _cleanup_states
        from core.process_identity import process_identity_details
        before=Path({str(before)!r})
        if not before.exists():
            before.write_text(json.dumps({{"pid":os.getpid(),"start":_cleanup_states(2)[os.getpid()][2]}}))
        from leanflow_cli import workflow
        from leanflow_cli.formalization import preparation_launch as launch
        from leanflow_cli.workflows.project_setup.public_process import run_public_command,bind_invocation_root
        from io import StringIO
        launch.inspect_project=lambda root:{{"ready":True,"configured":True}}
        def unavailable(root): raise ValueError("Not previously prepared")
        launch.assert_preparation_current=unavailable
        launch.load_config=lambda:{{"limits":{{"agent_calls":3,"wall_time_s":30,"tokens":None}}}}
        workflow.resolve_runtime_provider=lambda **kw:{{"provider":"codex","model":"gpt-6.1-sol","reasoning_effort":"max"}}
        workflow.apply_runtime_model_override=lambda runtime,**kw:{{**runtime,"model":kw["model"]}}
        def discover(*args,**kw):
            Path({str(admitted)!r}).write_text(json.dumps({{**process_identity_details(),"pid":os.getpid(),"start":_cleanup_states(2)[os.getpid()][2],"isolated":sys.flags.isolated,"no_bytecode":sys.dont_write_bytecode}}))
            command=[sys.executable,"-c",{('import os,pathlib,time; pathlib.Path('+repr(str(actor_pid))+').write_text(str(os.getpid())); time.sleep(30)')!r}]
            directory=Path({str(tmp_path / 'actor-evidence')!r});directory.mkdir()
            import threading
            class Cancellation:
                def is_set(self): return kw["cancelled"]()
            with bind_invocation_root(directory):
                run_public_command(command,cwd=directory,log=StringIO(),cancel=Cancellation(),progress=None,phase="fake-discovery")
            raise AssertionError("Cancellation did not stop actor")
        launch.discover_preparation_plan=discover
        launch.prepare_document_project=lambda *a,**k:(_ for _ in ()).throw(AssertionError("Preparation after cancellation"))
        cli=importlib.import_module("leanflow_cli.main")
        raise SystemExit(cli.main())
    """))
    environment = {
        **os.environ,
        "LEANFLOW_HOME": str(tmp_path / "home"),
        "LEANFLOW_PROJECT_ROOT": str(root),
        "LEANFLOW_WORKFLOW_RUN_ID": "formalize-vscode-run-preflight-test",
        "LEANFLOW_NATIVE_MAX_RSS_GIB": "1",
        "LEANFLOW_SETUP_MAX_RSS_GIB": "1",
    }
    environment.pop("LEANFLOW_NATIVE_PROCESS_TOKEN", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            str(script),
            "workflow",
            "formalize",
            "paper.tex",
            "--provider",
            "codex",
            "--model",
            "gpt-6.1-sol",
        ],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    actor_identity = None
    try:
        original = _wait_json(before, process)
        observed = _wait_json(admitted, process)
        assert observed["pid"] == original["pid"] == process.pid
        assert observed["start"] == original["start"]
        assert observed["isolated"] and observed["no_bytecode"]
        deadline = time.monotonic() + 5
        while not actor_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert actor_pid.exists()
        from agent.runtime.harness.processes import _cleanup_states

        actor_number = int(actor_pid.read_text())
        actor_identity = (actor_number, _cleanup_states(2)[actor_number][2])
        stop_script = f"import sys;sys.path.insert(0,{str(REPO)!r});from leanflow_cli.main import main;raise SystemExit(main(['runs','--project',{str(root)!r},'stop','formalize-vscode-run-preflight-test','--json']))"
        stopped = subprocess.run(
            [sys.executable, "-I", "-B", "-c", stop_script],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert stopped.returncode == 0, stopped.stderr + stopped.stdout
        reply = json.loads(stopped.stdout)
        assert reply["stopped"] is True and reply["identity_verified"] is True
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode != 0
        final = json.loads(status.read_text())
        assert final["target_symbol"] == "preserved.mathematics" and final["proof_solved"] == 17
        assert final["process_id"] == 0 and final["preflight_release"]["outcome"] == "cancelled"
        records = list((tmp_path / "home/setup").glob("*/commands/*.json"))
        assert records and all(json.loads(path.read_text())["cleanup_verified"] for path in records)
        assert not (root / ".leanflow/native-workflows").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if actor_identity is not None:
            from agent.runtime.harness.processes import _cleanup_states, start_identity_matches

            actor_number, start = actor_identity
            table = _cleanup_states(2)
            if actor_number in table and start_identity_matches(table[actor_number][2], start):
                try:
                    os.killpg(actor_number, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_exact_release_does_not_erase_a_later_owner_or_math_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "fixture-token")
    workflow_state.mark_workflow_live_status_startup(
        phase="starting",
        metadata={
            "run_id": "formalize-old",
            "owner_role": "formalization-preflight",
            "target_symbol": "unchanged",
        },
    )
    assert not workflow_state.release_workflow_startup_owner("formalize-other", outcome="prepared")
    assert workflow_state.release_workflow_startup_owner("formalize-old", outcome="prepared")
    value = workflow_state.read_json_file(workflow_state.workflow_live_status_path())
    assert value["target_symbol"] == "unchanged" and value["process_id"] == 0
    workflow_state.save_workflow_live_status(
        {"run_id": "formalize-new", "owner_role": "native-controller", "target_symbol": "new"}
    )
    assert not workflow_state.release_workflow_startup_owner("formalize-old", outcome="failed")
    assert (
        workflow_state.read_json_file(workflow_state.workflow_live_status_path())["run_id"]
        == "formalize-new"
    )


def test_reexec_guard_rejects_runtime_only_token_after_one_boundary(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "runtime-only")
    monkeypatch.setattr(owner, "_process_token", lambda pid: "")
    monkeypatch.setitem(sys._xoptions, owner._REEXEC, "1")
    monkeypatch.setattr(os, "execve", lambda *args: pytest.fail("Repeated exec"))
    with pytest.raises(RuntimeError, match="one re-exec"):
        owner.ensure_cli_preflight_token()


def test_reexec_failure_preserves_environment_and_reports_no_admission(monkeypatch):
    monkeypatch.setattr(owner, "_process_token", lambda pid: "")
    monkeypatch.delenv("LEANFLOW_NATIVE_PROCESS_TOKEN", raising=False)
    monkeypatch.delitem(sys._xoptions, owner._REEXEC, raising=False)
    before = dict(os.environ)
    captured = []

    def fail(executable, argv, environment):
        captured.append((executable, argv, environment))
        raise OSError("denied")

    monkeypatch.setattr(os, "execve", fail)
    with pytest.raises(RuntimeError, match="no preparation or model"):
        owner.ensure_cli_preflight_token()
    assert dict(os.environ) == before
    assert captured[0][1][3:] == sys.orig_argv[1:]
    assert captured[0][2]["LEANFLOW_NATIVE_PROCESS_TOKEN"]


def test_real_preflight_release_allows_next_native_owner_while_parent_stays_alive(tmp_path):
    root = tmp_path.resolve()
    script = tmp_path / "handoff.py"
    child = textwrap.dedent(f"""\
        import json,os,sys
        sys.path.insert(0,{str(REPO)!r})
        from leanflow_cli.native.harness_observer import NativeObserver
        from leanflow_cli.workflows.workflow_state import read_json_file,workflow_live_status_path
        from pathlib import Path
        observer=NativeObserver(Path({str(root)!r}),"formalize-handoff","formalize","codex")
        observer.start({{"status":"running"}})
        print(json.dumps(read_json_file(workflow_live_status_path())),flush=True)
    """)
    script.write_text(textwrap.dedent(f"""\
        import json,os,subprocess,sys
        from pathlib import Path
        sys.path.insert(0,{str(REPO)!r})
        from leanflow_cli.formalization.preflight_owner import preflight_owner
        from leanflow_cli.runtime.project_operations import project_setup
        from leanflow_cli.workflows.workflow_state import read_json_file,workflow_live_status_path
        with preflight_owner(Path({str(root)!r}),dict(os.environ)):
            status=read_json_file(workflow_live_status_path())
            assert status["process_id"]==os.getpid() and status["owner_role"]=="formalization-preflight"
            # Public preparation may own setup without bypassing its exclusive lock.
            with project_setup(Path({str(root)!r})): pass
        released=read_json_file(workflow_live_status_path())
        assert released["process_id"]==0 and released["preflight_release"]["outcome"]=="prepared"
        result=subprocess.run([sys.executable,"-I","-B","-c",{child!r}],env={{**os.environ,"LEANFLOW_NATIVE_PROCESS_TOKEN":"new-native-handoff-token"}},capture_output=True,text=True,timeout=10)
        assert result.returncode==0,result.stderr
        final=json.loads(result.stdout)
        assert final["process_id"]!=os.getpid() and final["run_id"]=="formalize-handoff"
        print(json.dumps({{"parent_still_alive":os.getpid(),"native_pid":final["process_id"],"released":True}}))
    """))
    environment = {
        **os.environ,
        "LEANFLOW_HOME": str(tmp_path / "home"),
        "LEANFLOW_PROJECT_ROOT": str(root),
        "LEANFLOW_WORKFLOW_RUN_ID": "formalize-handoff",
        "LEANFLOW_NATIVE_PROCESS_TOKEN": "initial-preflight-handoff-token",
        "LEANFLOW_NATIVE_MAX_RSS_GIB": "1",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(script)],
        env=environment,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["released"] is True


@pytest.mark.parametrize(
    "difference", ["role", "kind", "phase", "pid", "group", "session", "token", "initial_token"]
)
def test_setup_self_preflight_exception_requires_every_verified_identity_field(
    tmp_path,
    monkeypatch,
    difference,
):
    from core.process_identity import current_process_identity, process_identity_details
    from leanflow_cli.runtime import project_operations

    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "self-preflight-token")
    monkeypatch.setattr(project_operations, "_process_token", lambda pid: "self-preflight-token")
    monkeypatch.setattr(project_operations, "_process_is_live", lambda payload: True)
    payload = {
        **process_identity_details(current_process_identity()),
        "owner_role": "formalization-preflight",
        "workflow_kind": "formalize",
        "phase": "starting",
        "project_root": str(tmp_path),
    }
    path = tmp_path / "status.json"
    path.write_text(json.dumps(payload))
    project_operations._check_status(path, tmp_path)
    key = {
        "role": "owner_role",
        "kind": "workflow_kind",
        "phase": "phase",
        "pid": "process_id",
        "group": "process_group_id",
        "session": "process_session_id",
        "token": "process_token_sha256",
    }.get(difference)
    if key:
        payload[key] = payload[key] + 1 if isinstance(payload[key], int) else "different"
    else:
        monkeypatch.setattr(project_operations, "_process_token", lambda pid: "")
    path.write_text(json.dumps(payload))
    with pytest.raises(project_operations.ProjectBusyError):
        project_operations._check_status(path, tmp_path)
