"""Check the computation failures actually returned to research agents."""

import json
import signal
import subprocess
from pathlib import Path

import pytest

from leanflow_cli.workflows.prover.session_tools import SessionTools


def invoke_compute(tmp_path: Path) -> dict:
    tools = SessionTools(role="orchestrator", project_root=tmp_path, workspace=tmp_path, context={})
    return tools.invoke("compute", {"program": "print(2 + 2)"})


def test_exact_computation_result_is_preserved(tmp_path: Path) -> None:
    result = invoke_compute(tmp_path)
    assert result["success"]
    assert result["output"] == "4\n"


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGXCPU])
def test_killed_compute_reports_resource_limit_to_agent(tmp_path, monkeypatch, sig):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, -sig, "", "")
    )
    result = invoke_compute(tmp_path)
    assert result["success"] is False
    assert result["status"] == "empirical_compute_resource_limit"
    assert result["returncode"] == -sig
    assert "smaller" in result["error"]
    assert "JSON" not in result["error"]


def test_parent_timeout_is_actionable(tmp_path, monkeypatch):
    def expired(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", expired)
    result = invoke_compute(tmp_path)
    assert result["success"] is False
    assert result["status"] == "empirical_compute_timeout"
    assert "smaller" in result["error"]


@pytest.mark.parametrize(
    ("code", "stdout"),
    [(1, ""), (0, "not JSON"), (0, "[]"), (1, '{"success": true, "output": "bad"}')],
)
def test_invalid_or_failed_child_cannot_report_success(tmp_path, monkeypatch, code, stdout):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, code, stdout, "child failed"),
    )
    result = invoke_compute(tmp_path)
    assert result["success"] is False
    assert result["status"] == "empirical_compute_error"
    assert result["returncode"] == code


def test_runtime_denial_preserves_capability_feedback(tmp_path, monkeypatch):
    payload = {
        "success": False,
        "status": "empirical_compute_denied",
        "error": "math.sin is not available",
        "capabilities": "Use Fraction and integer operations",
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(payload), ""),
    )
    assert invoke_compute(tmp_path) == payload
