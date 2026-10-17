"""Check owned setup subprocess cancellation and credential-safe output."""

from __future__ import annotations

import os
import sys
import threading
import time
from io import StringIO

import pytest

from leanflow_cli.workflows.project_setup import process


def test_streaming_output_redacts_credentials_without_corrupting_raw_protocol(tmp_path):
    log = StringIO()
    events = []
    code, raw = process.run_command(
        [
            sys.executable,
            "-c",
            'print("https://username:secret@example.com/path API_KEY=private-value")',
        ],
        cwd=tmp_path,
        log=log,
        cancel=None,
        progress=events.append,
        phase="dependencies",
    )
    assert code == 0
    assert "username:secret" in raw
    assert "private-value" in raw
    assert "username:secret" not in log.getvalue()
    assert "private-value" not in log.getvalue()
    assert "private-value" not in str(events)


@pytest.mark.skipif(os.name == "nt", reason="POSIX owned process groups")
def test_cancellation_terminates_owned_child_processes(tmp_path):
    ready = tmp_path / "ready"
    stopped = tmp_path / "stopped"
    child = (
        "import signal, pathlib, time, sys; "
        f"signal.signal(signal.SIGTERM, lambda *args: (pathlib.Path({str(stopped)!r}).write_text('stopped'), sys.exit(0))); "
        f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(30)"
    )
    parent = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}])"
    cancel = threading.Event()

    def request_cancel():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    thread = threading.Thread(target=request_cancel)
    thread.start()
    started = time.monotonic()
    with pytest.raises(process.SetupCancelled):
        process.run_command(
            [sys.executable, "-c", parent],
            cwd=tmp_path,
            log=StringIO(),
            cancel=cancel,
            progress=None,
            phase="verification",
        )
    thread.join()
    assert time.monotonic() - started < 5
    deadline = time.monotonic() + 1
    while not stopped.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stopped.read_text() == "stopped"


def test_editor_path_falls_back_to_native_elan_home(tmp_path, monkeypatch):
    monkeypatch.setattr(process.shutil, "which", lambda _: None)
    monkeypatch.setenv("ELAN_HOME", str(tmp_path / "elan"))
    binary = tmp_path / "elan/bin/lake"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert process.find_executable("lake") == str(binary)
    assert process.find_executable("git") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX owned process groups")
def test_cancellation_kills_descendant_after_leader_exits(tmp_path):
    ready = tmp_path / "ready"
    child = (
        "import signal, pathlib, time, os; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    # Keep the leader owned until the normal sampler observes its child; the
    # separate unobserved-writer fixture exercises fail-closed retention.
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(0.4)"
    cancel = threading.Event()

    def request_cancel():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.6)
        cancel.set()

    thread = threading.Thread(target=request_cancel)
    thread.start()
    with pytest.raises(process.SetupCancelled):
        process.run_command(
            [sys.executable, "-c", parent],
            cwd=tmp_path,
            log=StringIO(),
            cancel=cancel,
            progress=None,
            phase="verification",
        )
    thread.join()
    pid = int(ready.read_text())
    # An unreaped orphan can remain a zombie briefly; it must not be executing.
    import subprocess

    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    assert not state or state.startswith("Z")


def test_protocol_stdout_excludes_lake_warning_but_log_retains_it(tmp_path):
    log = StringIO()
    events = []
    code, output = process.run_command(
        [
            sys.executable,
            "-c",
            'import sys; print("warning: manifest out of date", file=sys.stderr); print(\'{"env": 0}\')',
        ],
        cwd=tmp_path,
        log=log,
        cancel=None,
        progress=events.append,
        phase="checking_readiness",
        stdout_only=True,
    )
    assert code == 0
    assert output.strip() == '{"env": 0}'
    assert "warning: manifest out of date" in log.getvalue()
    assert any(event["message"] == "warning: manifest out of date" for event in events)


def test_failed_protocol_command_keeps_stderr_error_detail(tmp_path):
    code, output = process.run_command(
        [
            sys.executable,
            "-c",
            'import sys; print("missing native library", file=sys.stderr); sys.exit(1)',
        ],
        cwd=tmp_path,
        log=StringIO(),
        cancel=None,
        progress=None,
        phase="checking_readiness",
        stdout_only=True,
    )
    assert code == 1
    assert "missing native library" in output
