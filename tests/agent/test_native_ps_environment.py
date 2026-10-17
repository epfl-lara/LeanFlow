"""Keep process identity and RSS inspection stable across locale and date spacing."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent.runtime.harness import processes, resources


@pytest.mark.parametrize("probe", ["parentage", "cleanup", "group", "rss"])
def test_every_native_ps_probe_pins_c_locale_and_preserves_timezone(monkeypatch, probe):
    """Inherited Japanese dates cannot change the machine-readable identity format."""
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    monkeypatch.setenv("LC_ALL", "ja_JP.UTF-8")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    observed = []

    def run(command, **kwargs):
        observed.append(kwargs)
        assert kwargs.get("env", {}).get("LC_ALL") == "C"
        assert kwargs["env"]["TZ"] == "Asia/Tokyo"
        fields = command[-1]
        if fields == "pid=,ppid=,lstart=":
            output = "101 1 Wed Oct  7 12:00:00 2026\n"
        elif fields == "pid=,pgid=,stat=,lstart=":
            output = "101 101 S Wed Oct  7 12:00:00 2026\n"
        elif fields == "pgid=,stat=":
            output = "101 S\n"
        else:
            assert fields == "pid=,rss=,lstart="
            output = "101 2 Wed Oct  7 12:00:00 2026\n"
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(processes.subprocess, "run", run)
    if probe == "parentage":
        assert processes._processes()[101][1] == "Wed Oct 7 12:00:00 2026"
    elif probe == "cleanup":
        assert processes._cleanup_states(2)[101][2] == "Wed Oct 7 12:00:00 2026"
    elif probe == "group":
        assert processes._group_has_live_processes(101)
    else:
        assert resources._rss_entries()[101] == ("Wed Oct 7 12:00:00 2026", 2048)
    assert len(observed) == 1
    assert os.environ["LC_ALL"] == "ja_JP.UTF-8" and os.environ["TZ"] == "Asia/Tokyo"


def test_single_digit_date_identity_is_canonical_across_parentage_and_signal(monkeypatch):
    """Whitespace changes in the same start identity cannot strand a tracked child."""
    outputs = iter(
        [
            "101 1 Wed Oct  7 12:00:00 2026\n202 101 Wed Oct  7 12:00:01 2026\n",
            "101 1 Wed Oct 7 12:00:00 2026\n202 101 Wed Oct 7 12:00:01 2026\n",
        ]
    )
    monkeypatch.setattr(
        processes.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=next(outputs))
    )
    signals = []
    monkeypatch.setattr(processes.os, "kill", lambda pid, sig: signals.append(pid))
    tree = processes.ProcessTree(101)
    tree.refresh()
    tree.signal(__import__("signal").SIGTERM)
    assert signals == [202]


@pytest.mark.skipif(os.name != "posix", reason="The actual native probe requires POSIX ps")
def test_actual_native_rss_probe_works_with_inherited_japanese_locale(monkeypatch):
    """Probe a real child while retaining the caller's locale and original Popen binding."""
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    monkeypatch.setenv("LC_ALL", "ja_JP.UTF-8")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time;time.sleep(10)"], start_new_session=True
    )
    try:
        tree = processes.ProcessTree(child.pid, process=child)
        assert resources.owned_rss_bytes(child, tree) > 0
        assert child.returncode is None
    finally:
        child.kill()
        child.wait(timeout=2)


@pytest.mark.parametrize("legacy", ["金 10月 16 12:00:00 2026", "ven. oct. 16 12:00:00 2026"])
def test_uncomparable_legacy_start_cannot_prove_owned_pid_reuse(monkeypatch, legacy):
    """A still-present PID with a legacy locale date remains unknown, never safely absent."""
    from leanflow_cli.workflows.project_setup import cleanup

    current = "Fri Oct 16 12:00:00 2026"
    monkeypatch.setattr(cleanup, "process_states", lambda: {101: (101, "S", current)})
    tree = processes.ProcessTree(800, {101: legacy})
    with pytest.raises(ValueError, match="uncomparable legacy date"):
        cleanup.verify_owned_stopped(tree, timeout_s=0.05)
    monkeypatch.setattr(processes, "_cleanup_states", lambda timeout: {101: (101, "S", current)})
    with pytest.raises(ValueError, match="uncomparable legacy date"):
        processes.verify_owned_stopped(tree, group=800, timeout_s=0.05)


def test_shared_preparation_cleanup_probe_matches_native_identity_frame(monkeypatch):
    """Both layers must use C dates and retain the same inherited timezone."""
    from leanflow_cli.workflows.project_setup import cleanup

    monkeypatch.setenv("LC_ALL", "ja_JP.UTF-8")
    monkeypatch.setenv("TZ", "Asia/Tokyo")

    def run(command, **kwargs):
        assert kwargs["env"]["LC_ALL"] == "C"
        assert kwargs["env"]["TZ"] == "Asia/Tokyo"
        return SimpleNamespace(stdout="101 101 S Wed Oct  7 12:00:00 2026\n")

    monkeypatch.setattr(cleanup.subprocess, "run", run)
    assert cleanup.process_states()[101][2] == "Wed Oct 7 12:00:00 2026"
    assert processes.start_identity_matches("Wed Oct 7 12:00:00 2026", "Wed Oct  7 12:00:00 2026")
    assert not processes.start_identity_matches("new-root", "old-root")
