"""Exercise provider queue isolation, cancellation, and durable call accounting."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover import agent_session
from leanflow_cli.workflows.prover.session_admission import (
    ProviderQueueStopped,
    provider_request_slot,
)


def agent(key: str = "queue-test-key", **fields):
    """Build only the provider fields needed by admission and the session loop."""
    return SimpleNamespace(
        model="test", base_url="https://inference.rcp.epfl.ch/v1", api_key=key, **fields
    )


def slot(adapter, *, cancelled=lambda: False, on_wait=lambda: None, deadline=None):
    """Bound every test lock acquisition to avoid stranding test processes."""
    return provider_request_slot(
        adapter,
        deadline=time.monotonic() + 5 if deadline is None else deadline,
        cancelled=cancelled,
        on_wait=on_wait,
    )


def test_same_key_serializes_across_processes_and_other_keys_do_not_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path))
    script = """
import sys,time
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from leanflow_cli.workflows.prover.session_admission import provider_request_slot
agent=SimpleNamespace(base_url='https://inference.rcp.epfl.ch/v1',api_key='queue-test-key')
with provider_request_slot(agent,deadline=time.monotonic()+10,cancelled=lambda:False,on_wait=lambda:None):
    print('locked',flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-I", "-u", "-c", script, str(Path(__file__).resolve().parents[2])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with slot(agent("other-key"), on_wait=lambda: pytest.fail("unrelated key waited")):
            pass
        waited = []

        def release():
            waited.append(True)
            child.stdin.write("release\n")
            child.stdin.flush()

        with slot(agent(), on_wait=release):
            assert waited
        assert child.wait(timeout=5) == 0
        for path in (tmp_path / "runtime/rcp-request-slots").iterdir():
            assert "queue-test-key" not in path.name
            assert path.read_text() == ""
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_queue_releases_after_exception_and_ignores_non_rcp(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="request failed"), slot(agent()):
        raise RuntimeError("request failed")
    with slot(agent(), on_wait=lambda: pytest.fail("slot leaked")):
        pass
    with slot(SimpleNamespace(base_url="https://codex.invalid")):
        pass


@pytest.mark.parametrize("stop", ["interrupted", "timeout"])
def test_queue_cancellation_and_deadline_do_not_acquire_a_busy_slot(tmp_path, monkeypatch, stop):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path))
    cancelled = False

    def waiting():
        nonlocal cancelled
        cancelled = stop == "interrupted"

    with slot(agent()), pytest.raises(ProviderQueueStopped) as error:
        with slot(
            agent(),
            cancelled=lambda: cancelled,
            on_wait=waiting,
            deadline=time.monotonic() + (5 if stop == "interrupted" else 0.03),
        ):
            pytest.fail("busy credential was admitted")
    assert error.value.status == stop


def test_waiting_session_preserves_ledger_and_can_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    adapter = agent()
    monkeypatch.setattr(agent_session, "build_transport", lambda *_: adapter)
    monkeypatch.setattr(agent_session, "close_transport", lambda *_: None)
    sent = []

    def request(*_):
        sent.append(True)
        ledger = json.loads((tmp_path / ".runtime/job/request-count.json").read_text())
        assert ledger["used"] == 1
        return {"role": "assistant", "content": '{"proof":"by trivial"}'}, {}

    monkeypatch.setattr(agent_session, "request_once", request)
    cancelled = False
    events = []

    def event(kind, details):
        nonlocal cancelled
        events.append(kind)
        if kind == "provider-wait":
            assert details["api_calls"] == 0
            assert not (tmp_path / ".runtime/job/request-count.json").exists()
            cancelled = True

    args = dict(
        role="prover",
        prompt="Prove the goal",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "_cancelled": lambda: cancelled},
        api_budget=3,
        log_path=tmp_path / "session.jsonl",
        context={},
        on_event=event,
    )
    with slot(adapter):
        result = agent_session.run_session(**args)
    assert result["status"] == "interrupted"
    assert result["api_calls"] == 0 and not sent
    assert "provider-wait" in events and "api-request" not in events
    cancelled = False
    result = agent_session.run_session(**args)
    assert result["status"] == "completed" and result["api_calls"] == 1
    assert len(sent) == 1
