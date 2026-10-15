"""Keep Git authority queries complete beyond diagnostic tails and reject bounded overflow."""

from __future__ import annotations

import dataclasses
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization.preparation_checkout import check_checkout
from leanflow_cli.formalization.preparation_stage import preparation_git_reader
from leanflow_cli.formalization.preparation_transaction import preparation_directory
from leanflow_cli.workflows.project_setup import execution_process, process
from tests.leanflow.test_project_setup_execution import policy as _policy

policy = _policy


def git(root: Path, *arguments: str) -> str:
    """Run local fixture Git without user configuration or hooks."""
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *arguments],
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
        timeout=10,
    )


@pytest.fixture
def checkout(policy, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "owned-state"))
    # Exercise the real bounded process adapter; isolation itself has separate tests.
    monkeypatch.setattr(execution_process, "sandbox_command", lambda argv, *args, **kw: argv)
    stage = policy.isolation.snapshot_root
    package = stage / ".lake/packages/sample"
    package.mkdir(parents=True)
    (package / "A_hidden.lean").write_text("def original : Nat := 1\n")
    for index in range(1100):
        (package / (f"z{index:05d}_a" + "long_source_name_" * 5 + ".lean")).write_text(
            "-- source\n"
        )
    git(package, "init", "--quiet")
    git(package, "add", ".")
    git(
        package,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    preparation_directory(stage).mkdir(parents=True)
    reader = preparation_git_reader(stage, policy, cancel=None, progress=None)
    return (
        stage,
        package,
        {"name": "sample", "rev": git(package, "rev-parse", "HEAD").strip()},
        reader,
    )


def test_large_clean_checkout_uses_complete_listing_instead_of_mid_filename_tail(checkout):
    stage, package, pin, reader = checkout
    expected = git(package, "ls-files", "-v")
    assert len(expected.encode()) > execution_process.TAIL_BYTES
    assert expected[-execution_process.TAIL_BYTES :].splitlines()[0][0].islower()
    assert reader(package, ["ls-files", "-v"]) == expected
    assert check_checkout(stage, pin, reader) == package


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_dirty_first_source_cannot_disappear_behind_a_large_clean_tail(checkout, flag):
    stage, package, pin, reader = checkout
    git(package, "update-index", flag, "A_hidden.lean")
    (package / "A_hidden.lean").write_text("def tampered : Nat := 99\n")
    assert git(package, "status", "--porcelain", "--untracked-files=all") == ""
    listing = reader(package, ["ls-files", "-v"])
    assert len(listing.encode()) > execution_process.TAIL_BYTES
    assert listing.splitlines()[0][0] in {"h", "S"}
    with pytest.raises(ValueError, match="hides tracked source"):
        check_checkout(stage, pin, reader)


def test_status_preserves_early_dirty_source_and_all_untracked_rows(checkout):
    stage, package, pin, reader = checkout
    (package / "A_hidden.lean").write_text("-- changed\n")
    for index in range(1100):
        (package / (f"zz_untracked_{index:05d}_" + "long_resource_name_" * 4)).write_text(
            "resource"
        )
    expected = git(package, "status", "--porcelain", "--untracked-files=all")
    assert len(expected.encode()) > execution_process.TAIL_BYTES
    assert reader(package, ["status", "--porcelain", "--untracked-files=all"]) == expected
    assert expected.splitlines()[0] == " M A_hidden.lean"
    with pytest.raises(ValueError, match="unbound source edits"):
        check_checkout(stage, pin, reader)


@pytest.mark.parametrize("restricted", [False, True])
def test_complete_protocol_overflow_raises_instead_of_returning_a_partial_success(
    policy, monkeypatch, restricted
):
    monkeypatch.setattr(execution_process, "sandbox_command", lambda argv, *args, **kw: argv)
    policy = dataclasses.replace(
        policy, executables={**policy.executables, sys.executable: sys.executable}
    )
    with pytest.raises(process.SetupFailure) as error:
        process.run_command(
            [
                sys.executable,
                "-I",
                "-c",
                "import signal,time,sys;signal.signal(signal.SIGTERM,lambda *_:(time.sleep(.1),sys.exit(0)));print('x'*2048,flush=True);time.sleep(30)",
            ],
            cwd=policy.isolation.snapshot_root,
            log=io.StringIO(),
            cancel=None,
            progress=None,
            phase="checking_pins",
            stdout_only=True,
            stdout_limit_bytes=128,
            execution_policy=policy if restricted else None,
        )
    assert error.value.code == "setup_protocol_output_limit"


@pytest.mark.parametrize("restricted", [False, True])
def test_complete_protocol_preserves_head_bytes_and_excludes_stderr(
    policy, monkeypatch, restricted
):
    monkeypatch.setattr(execution_process, "sandbox_command", lambda argv, *args, **kw: argv)
    policy = dataclasses.replace(
        policy, executables={**policy.executables, sys.executable: sys.executable}
    )
    expected = "H first-source\n" + "H source\n" * 10000
    status, output = process.run_command(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys;print('H first-source');print('H source\\n'*10000,end='');print('warning: diagnostic',file=sys.stderr)",
        ],
        cwd=policy.isolation.snapshot_root,
        log=io.StringIO(),
        cancel=None,
        progress=None,
        phase="checking_pins",
        stdout_only=True,
        stdout_limit_bytes=256000,
        execution_policy=policy if restricted else None,
    )
    assert status == 0 and output == expected


@pytest.mark.parametrize("restricted", [False, True])
def test_read_failure_never_certifies_a_safe_prefix_as_complete_protocol(
    policy, monkeypatch, restricted
):
    monkeypatch.setattr(execution_process, "sandbox_command", lambda argv, *args, **kw: argv)
    policy = dataclasses.replace(
        policy, executables={**policy.executables, sys.executable: sys.executable}
    )
    original_popen = subprocess.Popen
    owned = []
    failures = []
    prefix_bytes = len("H clean\n" * 10000)

    class FailedProtocolReader:
        def __init__(self, stream, child):
            self.stream, self.child = stream, child
            self.read_bytes = 0

        def read(self, method, size):
            if self.read_bytes >= prefix_bytes:
                assert self.child.wait(timeout=2) == 0
                failures.append(True)
                raise OSError("fixture reader lost its protocol channel")
            value = getattr(self.stream, method)(min(size, prefix_bytes - self.read_bytes))
            self.read_bytes += len(value.encode() if isinstance(value, str) else value)
            return value

        def readline(self, size=-1):
            return self.read("readline", size if size >= 0 else prefix_bytes)

        def read1(self, size):
            return self.read("read1", size)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def with_broken_stdout(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        if args[0][-1] == "leanflow-protocol-read-failure-fixture":
            child.stdout = FailedProtocolReader(child.stdout, child)
            owned.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", with_broken_stdout)
    with pytest.raises(process.SetupFailure) as error:
        process.run_command(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys;sys.stdout.write('H clean\\n'*10000+'h hidden.lean\\n');sys.stdout.flush()",
                "leanflow-protocol-read-failure-fixture",
            ],
            cwd=policy.isolation.snapshot_root,
            log=io.StringIO(),
            cancel=None,
            progress=None,
            phase="checking_pins",
            stdout_only=True,
            stdout_limit_bytes=256000,
            execution_policy=policy if restricted else None,
        )
    assert error.value.code == "setup_protocol_read_failed"
    assert failures == [True] and len(owned) == 1 and owned[0].returncode == 0
    assert policy.usage.cleanup_verified
