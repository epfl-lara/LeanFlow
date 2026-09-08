"""Verify fail-closed, bounded Lean worker isolation and real kernel write denial."""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import check_process as checks
from leanflow_cli.workflows.prover import check_sandbox as sandbox
from leanflow_cli.workflows.prover import check_worker


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create separate canonical, scratch, and private roots."""
    paths = tuple(tmp_path / name for name in ("project", "scratch", "private"))
    for path in paths:
        path.mkdir()
    return paths


@pytest.mark.parametrize("system,binary", [("Darwin", "sandbox-exec"), ("Linux", "bwrap")])
def test_missing_os_isolation_never_starts_unprotected_process(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], system: str, binary: str
) -> None:
    project, workspace, _ = roots
    monkeypatch.setattr(sandbox.platform, "system", lambda: system)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        checks.subprocess, "Popen", lambda *a, **kw: pytest.fail("unsafe fallback process")
    )
    result = checks.isolated_command(project_root=project, workspace=workspace, argv=["lean"])
    assert result["error_code"] == "isolation_unavailable"
    assert binary in result["error"]


def test_unsupported_platform_is_actionable(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    project, workspace, private = roots
    monkeypatch.setattr(sandbox.platform, "system", lambda: "UnknownOS")
    with pytest.raises(checks.IsolationUnavailable, match="UnknownOS"):
        checks._sandbox_command(["lean"], project=project, workspace=workspace, private=private)


def test_mac_profile_grants_only_explicit_write_locations(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    project, workspace, private = roots
    artifact = project / "generated.olean"
    artifact.touch()
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    argv = checks._sandbox_command(
        ["lean"],
        project=project,
        workspace=workspace,
        private=private,
        extra_writable_roots=[artifact],
    )
    writes = next(line for line in argv[2].splitlines() if line.startswith("(allow file-write"))
    assert f'(subpath "{workspace}")' in writes
    assert f'(subpath "{private}")' in writes
    assert f'(literal "{artifact}")' in writes
    assert f'(subpath "{project}")' not in writes
    assert "(deny network*)" in argv[2]


def test_linux_mounts_canonical_sources_read_only_and_network_is_explicit(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    project, workspace, private = roots
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")
    args = checks._sandbox_command(["lean"], project=project, workspace=workspace, private=private)
    assert args[args.index(str(project)) - 1] == "--ro-bind"
    assert ["--bind", str(workspace), str(workspace)] in [args[i : i + 3] for i in range(len(args))]
    assert "--unshare-pid" in args and "--unshare-net" in args
    network = checks._sandbox_command(
        ["lake", "update"],
        project=project,
        workspace=workspace,
        private=private,
        network_allowed=True,
    )
    assert "--unshare-net" not in network


def test_workspace_and_extra_roots_cannot_grant_whole_source_authority(
    roots: tuple[Path, Path, Path],
) -> None:
    project, workspace, private = roots
    with pytest.raises(ValueError, match="must not contain"):
        checks._validated_paths(project, project)
    for extra in (project, private):
        with pytest.raises(checks.IsolationUnavailable, match="inside the project"):
            checks._sandbox_command(
                ["lean"],
                project=project,
                workspace=workspace,
                private=private,
                extra_writable_roots=[extra],
            )


def test_preexisting_hardlink_is_not_a_scratch_file(roots: tuple[Path, Path, Path]) -> None:
    project, workspace, _ = roots
    original = project / "Main.lean"
    original.write_text("protected")
    os.link(original, workspace / "alias")
    with pytest.raises(ValueError, match="hardlinked"):
        checks._validated_paths(project, workspace)


def test_checker_environment_excludes_provider_secrets(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    _, _, private = roots
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-pass")
    monkeypatch.setenv("LEANFLOW_PROVER_REQUEST_COUNT_FILE", "/protected/ledger")
    env = checks._environment(private)
    assert "OPENAI_API_KEY" not in env
    assert "LEANFLOW_PROVER_REQUEST_COUNT_FILE" not in env
    assert env["TMPDIR"] == str(private / "tmp")
    assert env["LEANFLOW_HOME"] == str(private / "home")


def test_worker_reuses_process_and_returns_unchanged_verdict(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    project, workspace, _ = roots
    target = workspace / "Proof.lean"
    target.write_text("theorem p : True := by trivial")
    script = "import json,sys,os\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'result':{'success':True,'ok':True,'pid':os.getpid(),'profile':r['include_axiom_profile']}}),flush=True)"
    monkeypatch.setattr(
        checks, "_sandbox_command", lambda *a, **kw: [sys.executable, "-I", "-u", "-c", script]
    )
    try:
        first = checks.check_scratch(
            project_root=project, workspace=workspace, file=target, include_axiom_profile=True
        )
        second = checks.check_scratch(project_root=project, workspace=workspace, file=target)
        assert first["ok"] and first["profile"]
        assert second["pid"] == first["pid"]
        assert not second["profile"]
        assert "error_code" not in second
        process = checks._WORKERS[(project.resolve(), workspace.resolve())].process
        assert process is not None
    finally:
        checks.close_check_workers(workspace)
    assert process.poll() is not None
    assert (project.resolve(), workspace.resolve()) not in checks._WORKERS


@pytest.mark.parametrize(
    "script,code",
    [
        (
            "import sys; print('sandbox-exec: denied setup',file=sys.stderr);sys.exit(1)",
            "check_setup_failed",
        ),
        (
            'import sys;sys.stdin.readline();print(\'{"id":"wrong","result":{}}\',flush=True)',
            "check_setup_failed",
        ),
        ("import time;time.sleep(30)", "check_timeout"),
    ],
)
def test_worker_failure_is_typed_and_reaped(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path], script: str, code: str
) -> None:
    project, workspace, _ = roots
    monkeypatch.setattr(
        checks, "_sandbox_command", lambda *a, **kw: [sys.executable, "-I", "-u", "-c", script]
    )
    worker = checks._CheckWorker(project, workspace)
    started = time.monotonic()
    result = worker.request({"id": "one"}, 1)
    assert result["error_code"] == code
    assert time.monotonic() - started < 4
    assert worker.closed
    assert worker.process is not None and worker.process.poll() is not None
    if "denied setup" in script:
        assert "denied setup" in result["error"]


def test_protocol_forwards_exact_check_target_contract(roots: tuple[Path, Path, Path]) -> None:
    project, workspace, _ = roots
    target = workspace / "Proof.lean"
    target.touch()
    received: dict[str, Any] = {}

    def check(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {"ok": True, "axioms": [], "custom": "unchanged"}

    result = check_worker._check(
        SimpleNamespace(lean_incremental_check=check),
        project,
        workspace,
        {
            "id": "one",
            "file": str(target),
            "declaration": "p",
            "replacement": "theorem p : True := by trivial",
            "include_axiom_profile": True,
            "allow_placeholders_for_elaboration": True,
            "timeout_s": 7,
        },
    )
    assert result == {"ok": True, "axioms": [], "custom": "unchanged"}
    assert received["action"] == "check_target"
    assert received["theorem_id"] == "p"
    assert received["timeout_s"] == received["timeout_ceiling_s"] == 7
    assert received["include_axiom_profile"] is True
    assert received["allow_placeholders_for_elaboration"] is True


def test_private_repl_copies_mutable_configuration_and_reuses_compiled_outputs(
    roots: tuple[Path, Path, Path],
) -> None:
    project, _, private = roots
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.30.0")
    binary = project / ".lake" / "build" / "bin" / "repl"
    binary.parent.mkdir(parents=True)
    binary.write_text("compiled REPL")
    view = check_worker._private_repl(project, private)
    (view / "lean-toolchain").write_text("private version marker")
    assert (project / "lean-toolchain").read_text() == "leanprover/lean4:v4.30.0"
    assert (view / ".lake" / "build" / "bin" / "repl").resolve() == binary.resolve()


def test_preflight_checks_lean_before_model_work_and_removes_test_source(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    project, workspace, _ = roots
    files = []

    def check(**kwargs: Any) -> dict[str, Any]:
        files.append(kwargs["file"])
        assert "theorem leanflowPreflight" in kwargs["file"].read_text()
        assert kwargs["declaration"] == "leanflowPreflight"
        assert kwargs["include_axiom_profile"] is True
        return {"success": True, "ok": True}

    monkeypatch.setattr(checks, "check_scratch", check)
    assert checks.preflight_check(project_root=project, workspace=workspace)["ok"]
    assert len(files) == 1 and not files[0].exists()


def test_worker_startup_failure_is_not_a_proof_rejection(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(check_worker.sys, "argv", ["worker", *map(str, roots)])
    monkeypatch.setattr(check_worker.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"id":"one"}\n')))
    monkeypatch.setattr(check_worker.sys, "stdout", output)
    monkeypatch.setattr(
        check_worker,
        "_configure",
        lambda *a: (_ for _ in ()).throw(RuntimeError("REPL setup needed")),
    )
    assert check_worker.main() == 0
    result = json.loads(output.getvalue())["result"]
    assert result["error_code"] == "check_setup_failed"
    assert "REPL setup needed" in result["error"]


def _require_os_isolation() -> None:
    """Skip runtime-only tests when the host has no supported isolation binary."""
    binary = {"Darwin": "sandbox-exec", "Linux": "bwrap"}.get(platform.system())
    if not binary or not shutil.which(binary):
        pytest.skip("OS write-isolation runtime is unavailable")


def test_real_kernel_denies_direct_symlink_and_hardlink_source_writes(
    roots: tuple[Path, Path, Path],
) -> None:
    _require_os_isolation()
    project, workspace, _ = roots
    source = project / "Main.lean"
    source.write_text("protected")
    script = """import json,os,sys
from pathlib import Path
source,scratch=map(Path,sys.argv[1:])
(scratch/'own').write_text('allowed')
out=[]
for action in ('direct','symlink','hardlink'):
    try:
        if action=='direct': source.write_text('bad')
        elif action=='symlink':
            alias=scratch/'symlink';alias.symlink_to(source);alias.write_text('bad')
        else:
            alias=scratch/'hardlink';os.link(source,alias);alias.write_text('bad')
        out.append(action+':allowed')
    except PermissionError: out.append(action+':denied')
print(json.dumps(out))
"""
    result = checks.isolated_command(
        project_root=project,
        workspace=workspace,
        argv=[sys.executable, "-I", "-c", script, str(source), str(workspace)],
    )
    if result.get("error_code") == "isolation_unavailable":
        pytest.skip(result["error"])
    assert result["success"], result
    assert json.loads(result["stdout"]) == ["direct:denied", "symlink:denied", "hardlink:denied"]
    assert source.read_text() == "protected"
    assert (workspace / "own").read_text() == "allowed"


def test_real_command_grants_only_named_build_artifact_and_bounds_output(
    monkeypatch: pytest.MonkeyPatch, roots: tuple[Path, Path, Path]
) -> None:
    _require_os_isolation()
    project, workspace, _ = roots
    artifact = project / "generated.olean"
    artifact.touch()
    monkeypatch.setattr(checks, "_MAX_MESSAGE", 1024)
    monkeypatch.setattr(checks, "_MAX_ERROR", 512)
    script = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('compiled');print('x'*100000);print('y'*100000,file=sys.stderr)"
    result = checks.isolated_command(
        project_root=project,
        workspace=workspace,
        argv=[sys.executable, "-I", "-c", script, str(artifact)],
        extra_writable_roots=[artifact],
    )
    if result.get("error_code") == "isolation_unavailable":
        pytest.skip(result["error"])
    assert result["success"], result
    assert artifact.read_text() == "compiled"
    assert len(result["stdout"]) == 1024 and len(result["stderr"]) == 512


def test_real_timeout_kills_command_and_same_group_child(roots: tuple[Path, Path, Path]) -> None:
    _require_os_isolation()
    import fcntl

    project, workspace, _ = roots
    lock_file = workspace / "child.lock"
    ready_file = workspace / "child.ready"
    child = "import fcntl,pathlib,sys,time;f=open(sys.argv[1],'w');fcntl.flock(f,fcntl.LOCK_EX);pathlib.Path(sys.argv[2]).touch();time.sleep(30)"
    script = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],*sys.argv[2:]]);time.sleep(30)"
    started = time.monotonic()
    result = checks.isolated_command(
        project_root=project,
        workspace=workspace,
        argv=[sys.executable, "-I", "-c", script, child, str(lock_file), str(ready_file)],
        timeout_s=1,
    )
    if result.get("error_code") == "isolation_unavailable":
        pytest.skip(result["error"])
    assert result["error_code"] == "check_timeout"
    assert time.monotonic() - started < 4
    assert ready_file.exists()
    # File locks cross PID namespaces, unlike the child's reported PID on Linux.
    # A surviving descendant would retain this lock after the leader is killed.
    with lock_file.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.skipif(
    not os.environ.get("LEANFLOW_TEST_LEAN_PROJECT"),
    reason="Set LEANFLOW_TEST_LEAN_PROJECT to a prebuilt Lean project for integration",
)
def test_real_lean_feedback_is_warm_and_cannot_write_canonical_source(tmp_path: Path) -> None:
    _require_os_isolation()
    project = Path(os.environ["LEANFLOW_TEST_LEAN_PROJECT"]).resolve()
    workspace = tmp_path / "scratch"
    workspace.mkdir()
    target = workspace / "Probe.lean"
    # Use an existing protected file without risking its contents: attempt the
    # same bytes, which still requires write authority and must be denied.
    protected = project / "lean-toolchain"
    before = protected.read_bytes()
    try:
        target.write_text("import Lean\ntheorem probe : True := by trivial\n")
        first = checks.check_scratch(
            project_root=project,
            workspace=workspace,
            file=target,
            declaration="probe",
            include_axiom_profile=True,
            timeout_s=60,
        )
        assert first.get("ok") is True, first
        worker = checks._WORKERS[(project, workspace.resolve())]
        second = checks.check_scratch(
            project_root=project,
            workspace=workspace,
            file=target,
            declaration="probe",
            include_axiom_profile=True,
            timeout_s=30,
        )
        assert second.get("ok") is True, second
        assert checks._WORKERS[(project, workspace.resolve())] is worker
        target.write_text(
            "import Lean\n#eval IO.FS.writeFile "
            + json.dumps(str(protected))
            + " "
            + json.dumps(before.decode())
            + "\ntheorem probe : True := by trivial\n"
        )
        denied = checks.check_scratch(
            project_root=project, workspace=workspace, file=target, timeout_s=30
        )
        assert denied.get("ok") is not True, denied
        assert (
            "permission" in json.dumps(denied).lower() or "permitted" in json.dumps(denied).lower()
        ), denied
        assert protected.read_bytes() == before
        target.write_text("import Lean\n#eval IO.sleep 30000\n")
        timeout = checks.check_scratch(
            project_root=project, workspace=workspace, file=target, timeout_s=1
        )
        assert timeout.get("error_code") == "check_timeout", timeout
        assert worker.closed
    finally:
        checks.close_check_workers(workspace)


def test_merged_usr_symlinks_are_recreated_inside_the_sandbox(tmp_path):
    """Without these, nothing in the sandbox can exec at all.

    _roots() resolves before binding, so on a merged-/usr distribution /bin and
    /lib64 collapse into /usr and the link names never exist in the sandbox.
    Every dynamically linked binary names its interpreter /lib64/ld-linux-*.so,
    so exec fails with a bare "No such file or directory".
    """
    from leanflow_cli.workflows.prover.check_sandbox import _merged_usr_links

    (tmp_path / "usr" / "bin").mkdir(parents=True)
    (tmp_path / "usr" / "lib64").mkdir()
    (tmp_path / "etc").mkdir()
    (tmp_path / "bin").symlink_to("usr/bin")
    (tmp_path / "lib64").symlink_to("usr/lib64")

    pairs = dict((link, target) for target, link in _merged_usr_links(tmp_path))
    assert pairs == {"/bin": "usr/bin", "/lib64": "usr/lib64"}
    # A real directory must be bound, never replaced by a symlink.
    assert "/etc" not in pairs


def test_non_merged_usr_layout_adds_no_symlinks(tmp_path):
    """A split-/usr host (or macOS) must be left exactly as it was."""
    from leanflow_cli.workflows.prover.check_sandbox import _merged_usr_links

    for name in ("bin", "lib", "lib64", "usr"):
        (tmp_path / name).mkdir()
    assert _merged_usr_links(tmp_path) == []


def test_bounded_capture_reports_when_it_drops_output(monkeypatch):
    """A caller cannot distinguish a whole stream from the tail of a longer one.

    The tails are bounded and drop the HEAD, and the process still exits 0, so
    without this flag a truncated capture looks like a complete success. That is
    how a 50MB single-line kernel profile arrived as its last 4MB and was parsed
    as if it were a whole JSON record.
    """
    from leanflow_cli.workflows.prover import check_process

    monkeypatch.setattr(check_process, "_MAX_MESSAGE", 1024)

    big = subprocess.Popen(
        [sys.executable, "-c", "print('x' * 20000)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _stderr, truncated = check_process._capture_command(big, 30)
    assert len(stdout) == 1024
    assert truncated["stdout"] is True

    small = subprocess.Popen(
        [sys.executable, "-c", "print('ok')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _stderr, truncated = check_process._capture_command(small, 30)
    assert stdout.strip() == b"ok"
    assert truncated["stdout"] is False


def test_inspector_writes_its_profile_to_the_workspace_not_stdout():
    """The kernel profile must not ride a bounded pipe.

    One `repr`-printed kernel type can exceed the capture cap by an order of
    magnitude (measured: 47.9MiB for a single Euclidean geometry statement),
    and it is emitted as ONE line, so a bounded tail can never contain a line
    start.
    """
    from leanflow_cli.workflows.prover import type_profile

    source = type_profile._INSPECTOR
    assert "profile.jsonl" in source
    assert "profile.putStrLn" in source
    assert "profile.flush" in source
    # The profile record itself must no longer go to stdout.
    assert "IO.println <| Json.compress" not in source
