"""Distinguish a present isolation executable from a usable native sandbox."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from agent.runtime.harness import preflight


def test_linux_namespace_denial_is_reported_without_model_work(monkeypatch) -> None:
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/bin/bwrap")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stderr="bwrap: uid map permission denied")

    monkeypatch.setattr(preflight.subprocess, "run", run)
    status = preflight.kernel_sandbox_status()
    assert not status["available"]
    assert "permission denied" in status["error"]
    assert "administrator" in status["remediation"]
    assert len(calls) == 1 and calls[0][-1] == "/bin/true"
    assert "--unshare-net" in calls[0]


def test_missing_sandbox_never_spawns_a_process(monkeypatch) -> None:
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)

    def unexpected(*args, **kwargs):
        raise AssertionError("Missing isolation must not launch a native process")

    monkeypatch.setattr(preflight.subprocess, "run", unexpected)
    assert not preflight.kernel_sandbox_status()["available"]


def test_sandbox_probe_timeout_is_a_readiness_failure(monkeypatch) -> None:
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/bin/bwrap")

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(preflight.subprocess, "run", timeout)
    assert not preflight.kernel_sandbox_status()["available"]
