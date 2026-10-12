"""Pin shared setup aliases, structured outcomes, cancellation, and shell behavior."""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest

from leanflow_cli.cli import project_command
from leanflow_cli.main import main
from leanflow_cli.shell import InteractiveShell


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))


@pytest.mark.parametrize("alias", ["setup", "init", "create"])
def test_project_aliases_stream_shared_pipeline(alias, monkeypatch, tmp_path, capsys):
    calls = []
    root = tmp_path / "project with spaces"

    def prepare(path, **options):
        calls.append((path, options))
        options["progress"](
            {"type": "progress", "phase": "dependencies", "message": "Downloading cache"}
        )
        return {"status": "ready", "root": str(root), "capabilities": {"repl": True}}

    monkeypatch.setattr(project_command, "prepare_project", prepare)
    monkeypatch.setattr(project_command, "resolve_template_source", lambda *_: "")
    assert main(["project", alias, str(root), "--json"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["type"] for event in events] == ["progress", "result"]
    assert events[-1]["status"] == "ready"
    assert calls[0][0] == str(root)
    assert calls[0][1]["preset"] == "mathlib"
    assert not calls[0][1]["cancel"].is_set()


@pytest.mark.parametrize("status,code", [("failed", 1), ("cancelled", 130)])
def test_setup_failure_preserves_recovery_details(status, code, monkeypatch, tmp_path, capsys):
    result = {
        "status": status,
        "phase": "dependencies",
        "code": "download_failed",
        "message": "Cache download failed",
        "remediation": "Check the connection and retry.",
        "log_path": str(tmp_path / "setup.log"),
    }
    monkeypatch.setattr(project_command, "prepare_project", lambda *_args, **_kwargs: result)
    assert main(["project", "setup", str(tmp_path), "--json"]) == code
    event = json.loads(capsys.readouterr().out)
    assert event == {**result, "type": "result"}
    assert main(["project", "setup", str(tmp_path)]) == code
    output = capsys.readouterr().out
    assert "dependencies" in output and result["remediation"] in output
    assert result["log_path"] in output


def test_setup_restores_signal_handlers_and_passes_cancellation(monkeypatch, tmp_path, capsys):
    original = signal.getsignal(signal.SIGTERM)

    def prepare(_path, **options):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert options["cancel"].is_set()
        return {"status": "cancelled", "phase": "verification", "message": "Cancelled"}

    monkeypatch.setattr(project_command, "prepare_project", prepare)
    assert main(["project", "setup", str(tmp_path), "--json"]) == 130
    assert signal.getsignal(signal.SIGTERM) is original
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"


def test_inspect_is_read_only_and_does_not_invoke_setup(monkeypatch, tmp_path, capsys):
    result = {"root": str(tmp_path), "kind": "empty", "setup_status": "unknown", "ready": None}
    monkeypatch.setattr(project_command, "inspect_project", lambda _: result)
    monkeypatch.setattr(
        project_command,
        "prepare_project",
        lambda *_a, **_k: pytest.fail("inspection mutated project"),
    )
    assert main(["project", "inspect", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == result


def test_setup_passes_explicit_source_and_preset(monkeypatch, tmp_path, capsys):
    source = tmp_path / "Problem.lean"
    calls = []
    monkeypatch.setattr(
        project_command,
        "prepare_project",
        lambda *a, **kw: calls.append((a, kw)) or {"status": "ready"},
    )
    assert (
        main(
            [
                "project",
                "setup",
                str(tmp_path / "new"),
                "--source-file",
                str(source),
                "--preset",
                "lean",
                "--name",
                "My proof",
            ]
        )
        == 0
    )
    assert calls[0][1]["source_file"] == str(source)
    assert calls[0][1]["preset"] == "lean"
    assert calls[0][1]["name"] == "My proof"


def test_shell_setup_failure_keeps_current_directory(monkeypatch, tmp_path, capsys):
    shell = InteractiveShell()
    shell.cwd = tmp_path
    monkeypatch.setattr(
        project_command,
        "prepare_project",
        lambda *_a, **_k: {
            "status": "failed",
            "phase": "toolchain",
            "message": "Missing toolchain",
            "remediation": "Install Elan and retry.",
        },
    )
    assert shell._run_project_command(["setup", "new"]) == 1
    assert shell.cwd == tmp_path
    assert "Install Elan and retry" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["setup", "inspect"])
def test_inaccessible_state_returns_actionable_json(command, monkeypatch, tmp_path, capsys):
    def inaccessible(*_args, **_kwargs):
        raise PermissionError("Setup state is not writable")

    target = "prepare_project" if command == "setup" else "inspect_project"
    monkeypatch.setattr(project_command, target, inaccessible)
    assert main(["project", command, str(tmp_path), "--json"]) == (1 if command == "setup" else 0)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["code"] == "setup_io_failed"
    assert "permissions" in result["remediation"]
    assert result["log_path"] == ""
