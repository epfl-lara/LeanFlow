"""Check private preparation sandbox grants, scrubbed environment, and sampled resource stops."""

from __future__ import annotations

import dataclasses
import io
import platform
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.runtime.storage import StoragePressure
from leanflow_cli.workflows.project_setup import execution, execution_process, process
from leanflow_cli.workflows.project_setup.execution import (
    build_setup_execution_policy,
)


@pytest.mark.parametrize("raw", ["0", "21", "50", "16.5", "nan", ""])
def test_invalid_user_memory_budget_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("LEANFLOW_SETUP_MAX_RSS_GIB", raw)
    with pytest.raises(process.SetupFailure) as error:
        execution.setup_memory_limit_bytes()
    assert error.value.code == "setup_memory_setting"


def test_memory_default_and_large_project_override(monkeypatch):
    monkeypatch.delenv("LEANFLOW_SETUP_MAX_RSS_GIB", raising=False)
    assert execution.setup_memory_limit_bytes() == 16 * 1024**3
    for gib in (8, 20):
        monkeypatch.setenv("LEANFLOW_SETUP_MAX_RSS_GIB", str(gib))
        assert execution.setup_memory_limit_bytes() == gib * 1024**3


def test_user_memory_budget_reaches_policy_and_preserves_explicit_tighter_limit(
    policy, monkeypatch
):
    def build(**overrides):
        return build_setup_execution_policy(
            policy.isolation.snapshot_root,
            canonical_root=policy.isolation.snapshot_root.parent / "canonical",
            toolchain="leanprover/lean4:v4.33.1",
            network_allowed=False,
            deadline=time.monotonic() + 30,
            **overrides,
        )

    monkeypatch.setenv("LEANFLOW_SETUP_MAX_RSS_GIB", "20")
    assert build().evidence()["max_owned_rss_bytes"] == 20 * 1024**3
    monkeypatch.setenv("LEANFLOW_SETUP_MAX_RSS_GIB", "50")
    assert build(max_owned_rss_bytes=8 * 1024**3).max_owned_rss_bytes == 8 * 1024**3
    with pytest.raises(process.SetupFailure):
        build()


@pytest.fixture
def policy(tmp_path, monkeypatch):
    stage = tmp_path / "private-stage"
    canonical = tmp_path / "canonical"
    stage.mkdir()
    canonical.mkdir()
    (canonical / "secret.txt").write_text("Canonical bytes must remain inaccessible")
    elan = tmp_path / "elan"
    compiler = elan / "toolchains/leanprover--lean4---v4.33.1/bin"
    compiler.mkdir(parents=True)
    for tool in ("lake", "lean"):
        binary = compiler / tool
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    monkeypatch.setenv("ELAN_HOME", str(elan))
    selected = build_setup_execution_policy(
        stage,
        canonical_root=canonical,
        toolchain="leanprover/lean4:v4.33.1",
        network_allowed=False,
        deadline=time.monotonic() + 30,
    )
    return dataclasses.replace(
        selected,
        executables={**selected.executables, sys.executable: sys.executable},
        disk_guard=lambda root: None,
    )


@pytest.fixture
def supported_policy(policy):
    system = platform.system()
    binary = "sandbox-exec" if system == "Darwin" else "bwrap" if system == "Linux" else None
    if binary is None or not shutil.which(binary):
        pytest.skip("Real private setup isolation unavailable on this host")
    return policy


def _run(policy, code, *, log=None, **kwargs):
    return process.run_command(
        [sys.executable, "-c", code],
        cwd=policy.isolation.snapshot_root,
        log=log if log is not None else io.StringIO(),
        cancel=kwargs.pop("cancel", None),
        progress=kwargs.pop("progress", None),
        phase="verification",
        execution_policy=policy,
        **kwargs,
    )


def test_policy_excludes_native_credentials_home_and_canonical_grants(policy, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    assert "OPENAI_API_KEY" not in policy.environment
    assert Path(policy.environment["HOME"]).is_relative_to(policy.isolation.snapshot_root)
    assert Path.home().resolve() in policy.isolation.denied_roots
    assert (policy.isolation.snapshot_root.parent / "canonical") in policy.isolation.denied_roots
    assert not policy.isolation.network_allowed
    assert not any(root == Path.home().resolve() for root in policy.isolation.readable_roots)
    assert policy.environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert policy.environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert Path(policy.executables["lake"]).parent.parent in policy.isolation.readonly_roots


def test_builder_never_grants_installation_writes_or_executes_missing_elan_toolchain(
    tmp_path, monkeypatch
):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setenv("ELAN_HOME", str(tmp_path / "missing-elan"))
    with pytest.raises(process.SetupFailure) as error:
        build_setup_execution_policy(
            stage,
            canonical_root=tmp_path / "canonical",
            toolchain="leanprover/lean4:v4.33.1",
            network_allowed=True,
            deadline=time.monotonic() + 30,
        )
    assert error.value.code == "setup_toolchain_unavailable"
    assert not (tmp_path / "missing-elan").exists()


def test_private_command_real_sandbox_denies_canonical_read_write_and_scrubs_env(
    supported_policy, monkeypatch
):
    policy = supported_policy
    canonical = policy.isolation.snapshot_root.parent / "canonical/secret.txt"
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    code = f"""
import os
from pathlib import Path
path = Path({str(canonical)!r})
for operation in (lambda: path.read_text(), lambda: path.write_text('overwritten')):
    try:
        operation()
        raise AssertionError('canonical access escaped isolation')
    except (PermissionError, FileNotFoundError):
        pass
assert 'OPENAI_API_KEY' not in os.environ
Path('allowed.txt').write_text('private stage only')
print('isolated')
"""
    status, output = _run(policy, code)
    assert status == 0, output
    assert canonical.read_text() == "Canonical bytes must remain inaccessible"
    assert (policy.isolation.snapshot_root / "allowed.txt").read_text() == "private stage only"
    assert policy.evidence()["cleanup_verified"]


def test_private_readiness_preserves_stdout_and_redacts_a_secret_across_read_chunks(
    supported_policy,
):
    log = io.StringIO()
    events = []
    code = "import sys; print('warning: manifest update', file=sys.stderr); print('x'*4070 + ' https://user:secret@example.com/x API_KEY=private-value', file=sys.stderr); print('{\"env\":0}')"
    status, output = _run(supported_policy, code, log=log, progress=events.append, stdout_only=True)
    assert status == 0 and output.strip() == '{"env":0}'
    assert "warning: manifest update" in log.getvalue()
    assert "user:secret" not in log.getvalue() and "private-value" not in str(events)


def test_carriage_return_progress_is_bounded_and_preserves_exact_stdout(supported_policy):
    log = io.StringIO()
    events = []
    # Delimiters cross read chunks; total progress exceeds the logical-line cap.
    content = b"cache progress API_KEY=private-value\r" * 1000 + b"done\r\n"
    code = f"import os; os.write(1, {content!r})"
    status, output = _run(supported_policy, code, log=log, progress=events.append, stdout_only=True)
    assert len(content) > 20000 and status == 0
    assert output.encode() == content
    assert len(events) == 1001
    assert all("private-value" not in row["message"] for row in events)
    assert events[-1]["message"] == "done"


def test_crlf_split_between_reads_emits_one_redacted_logical_record(supported_policy):
    log = io.StringIO()
    events = []
    content = b"x" * 4095 + b"\r\n" + b"API_KEY=secret\r\nend\n"
    status, output = _run(
        supported_policy,
        f"import os; os.write(1, {content!r})",
        log=log,
        progress=events.append,
        stdout_only=True,
    )
    assert status == 0 and output.encode() == content
    assert len(events) == 3 and "secret" not in log.getvalue()


def test_private_output_budget_kills_flood_without_a_newline(supported_policy):
    policy = dataclasses.replace(supported_policy, max_output_bytes=1000)
    log = io.StringIO()
    with pytest.raises(process.SetupFailure) as error:
        _run(policy, "import os, time; os.write(1, b'x'*1000000); time.sleep(30)", log=log)
    assert error.value.code == "setup_output_limit"
    assert len(log.getvalue()) < 1000
    assert policy.usage.cleanup_verified and policy.usage.output_bytes <= 4096


def test_private_line_budget_refuses_unbounded_lines_before_persisting(supported_policy):
    policy = supported_policy
    log = io.StringIO()
    with pytest.raises(process.SetupFailure) as error:
        _run(policy, "import os, time; os.write(1, b'x'*100000); time.sleep(30)", log=log)
    assert error.value.code == "setup_line_limit"
    assert len(log.getvalue()) < 1000


def test_private_memory_guard_samples_and_stops_owned_tree(supported_policy, monkeypatch):
    policy = dataclasses.replace(supported_policy, max_owned_rss_bytes=1024)
    monkeypatch.setattr(execution_process, "owned_rss_bytes", lambda child, tree: 2048)
    with pytest.raises(process.SetupFailure) as error:
        _run(policy, "import time; time.sleep(30)")
    assert error.value.code == "setup_memory_limit"
    assert policy.usage.peak_owned_rss_bytes == 2048
    assert policy.usage.cleanup_verified


def test_private_execution_records_nonzero_real_observed_rss(supported_policy):
    status, _ = _run(
        supported_policy,
        "import time; value=bytearray(4*1024*1024); time.sleep(0.7); print('done')",
    )
    assert status == 0
    assert supported_policy.usage.peak_owned_rss_bytes >= 4 * 1024**2
    assert supported_policy.environment["LEAN_NUM_THREADS"] == "1"
    assert supported_policy.max_owned_rss_bytes == 16 * 1024**3
    assert (
        supported_policy.evidence()["rss_measurement"]
        == "conservative-sum-of-sampled-owned-process-rss"
    )


@pytest.mark.parametrize("cancelled", [False, True])
def test_private_deadline_and_cancellation_stop_during_command(supported_policy, cancelled):
    event = threading.Event()
    policy = dataclasses.replace(
        supported_policy, deadline=time.monotonic() + (20 if cancelled else 0.3)
    )
    timer = threading.Timer(0.3, event.set)
    if cancelled:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(process.SetupCancelled if cancelled else process.SetupFailure) as error:
            _run(policy, "import time; time.sleep(30)", cancel=event)
        if not cancelled:
            assert error.value.code == "setup_deadline"
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5
    assert policy.usage.cleanup_verified


def test_private_policy_never_falls_back_when_sandbox_is_unavailable(policy, monkeypatch):
    from agent.runtime.harness.isolation import IsolationUnavailable

    def reject(*args, **kwargs):
        raise IsolationUnavailable("sandbox absent")

    monkeypatch.setattr(execution_process, "sandbox_command", reject)
    with pytest.raises(process.SetupFailure) as error:
        _run(policy, "raise AssertionError('should not launch')")
    assert error.value.code == "setup_isolation_unavailable"
    assert policy.usage.commands == 0


def test_resource_policy_rejects_parallel_ownership(policy):
    with policy.owner:
        with pytest.raises(process.SetupFailure) as error:
            _run(policy, "print('not started')")
    assert error.value.code == "setup_concurrency"


def test_private_disk_reserve_pressure_cancels_before_launch(policy):
    def reject(root):
        raise StoragePressure(10, 100)

    selected = dataclasses.replace(policy, disk_guard=reject)
    with pytest.raises(StoragePressure):
        _run(selected, "print('not started')")
    assert selected.usage.commands == 0


def test_memory_measurement_ignores_unowned_or_reused_pid(monkeypatch):
    start = "Wed Oct  7 12:00:00 2026"
    child = SimpleNamespace(pid=1, returncode=None)
    tree = SimpleNamespace(
        refresh=lambda: None,
        process=child,
        root=1,
        root_identity=start,
        descendants={2: start, 3: "other start"},
    )
    monkeypatch.setattr(
        execution.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="1 1 Wed Oct  7 12:00:00 2026\n2 2 Wed Oct  7 12:00:00 2026\n3 999 Wed Oct  7 12:00:00 2026\n4 999 Wed Oct  7 12:00:00 2026\n"
        ),
    )
    assert execution.owned_rss_bytes(child, tree) == 3 * 1024


def test_selected_git_real_sandbox_produces_exact_clean_commit_identity(supported_policy):
    selected = supported_policy

    def run(arguments):
        return process.run_command(
            ["git", *arguments],
            cwd=selected.isolation.snapshot_root,
            log=io.StringIO(),
            cancel=None,
            progress=None,
            phase="checking_pins",
            execution_policy=selected,
        )

    status, output = run(["init", "--quiet"])
    assert status == 0, output
    (selected.isolation.snapshot_root / "README").write_text("A local setup test\n")
    assert run(["add", "README"])[0] == 0
    status, output = run(
        [
            "-c",
            "user.name=Setup Test",
            "-c",
            "user.email=setup@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "setup fixture",
        ]
    )
    assert status == 0, output
    status, revision = run(["rev-parse", "HEAD"])
    assert status == 0 and len(revision.strip()) == 40
    assert all(character in "0123456789abcdef" for character in revision.strip())
    if sys.platform == "darwin":
        assert selected.executables["git"] != "/usr/bin/git"
        assert selected.environment["PATH"].split(":")[0] == str(
            Path(selected.executables["git"]).parent
        )
