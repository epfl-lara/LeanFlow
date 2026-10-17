"""Resolve REPL releases only from complete bounded Git stdout across setup routes."""

from __future__ import annotations

import dataclasses
import sys

import pytest

from leanflow_cli.formalization import preparation_stage
from leanflow_cli.formalization.preparation_transaction import preparation_directory
from leanflow_cli.workflows.project_setup import execution_process, process, service, verification
from tests.leanflow.test_project_setup import existing_project
from tests.leanflow.test_project_setup import fake_tools as _fake_tools
from tests.leanflow.test_project_setup_execution import policy as _policy

fake_tools = _fake_tools
policy = _policy


@pytest.fixture(params=["service", "catalog"])
def repl_setup(request, policy, fake_tools, monkeypatch, tmp_path):
    """Run both production setup wrappers with real bounded lookup subprocesses."""
    route = request.param
    if route == "service":
        root = existing_project(tmp_path / "ordinary-project")
    else:
        root = policy.isolation.snapshot_root
        (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
        (root / "lakefile.toml").write_text('name = "demo"\n')
    policy = dataclasses.replace(policy, toolchain="leanprover/lean4:v4.20.0")
    monkeypatch.setattr(execution_process, "sandbox_command", lambda argv, *args, **kw: argv)
    fallback = service.run_command
    lookup_calls = []

    def run_lookup(command, **options):
        if command[:4] != ["git", "ls-remote", "--tags", "--refs"]:
            assert route == "service"
            return fallback(command, **options)
        lookup_calls.append(command)
        limit = options.get("stdout_limit_bytes")
        # Keep overflowing producers alive until the controller terminates them.
        hold_open = limit is not None and (root / "catalog-stdout.txt").stat().st_size > limit
        return process.run_command(
            [
                sys.executable,
                "-I",
                "-c",
                "import pathlib,signal,sys,time; "
                "signal.signal(signal.SIGTERM,lambda *_:(time.sleep(.1),sys.exit(0))); "
                "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()); "
                "sys.stdout.flush(); "
                "sys.stderr.buffer.write(pathlib.Path(sys.argv[2]).read_bytes()); "
                "sys.stderr.flush(); time.sleep(float(sys.argv[3]))",
                str(root / "catalog-stdout.txt"),
                str(root / "catalog-stderr.txt"),
                "30" if hold_open else "0",
            ],
            **options,
        )

    monkeypatch.setattr(service, "run_command", run_lookup)
    monkeypatch.setattr(preparation_stage, "run_command", run_lookup)
    canonical = tmp_path / "canonical"
    preparation_directory(canonical).mkdir(parents=True, exist_ok=True)

    def configure(stdout, stderr=""):
        (root / "catalog-stdout.txt").write_text(stdout)
        (root / "catalog-stderr.txt").write_text(stderr)
        if route == "service":
            return service.prepare_project(root, preset="lean")["code"]
        try:
            preparation_stage.configure_preparation_catalog(
                canonical,
                root,
                {"catalog_requirements": [{"name": "repl"}]},
                policy,
                cancel=None,
                progress=None,
            )
        except process.SetupFailure as error:
            return error.code
        return "ready"

    return root, configure, lookup_calls, policy


def test_compatible_head_tag_survives_more_than_one_diagnostic_tail(repl_setup):
    root, configure, calls, _ = repl_setup
    stdout = "a" * 40 + "\trefs/tags/v4.20.0\n" + ("b" * 40 + "\trefs/tags/v9.0.0\n") * 1500
    assert len(stdout.encode()) > execution_process.TAIL_BYTES
    assert "v4.20.0" not in stdout[-execution_process.TAIL_BYTES :]
    assert configure(stdout) == "ready"
    assert 'rev = "v4.20.0"' in (root / "lakefile.toml").read_text()
    assert len(calls) == 1


def test_stderr_tag_cannot_authorize_an_unavailable_repl_release(repl_setup):
    root, configure, calls, _ = repl_setup
    before = (root / "lakefile.toml").read_bytes()
    assert (
        configure("a\trefs/tags/v9.0.0\n", "diagnostic\trefs/tags/v4.20.0\n")
        == "repl_revision_unavailable"
    )
    assert (root / "lakefile.toml").read_bytes() == before
    assert len(calls) == 1


def test_oversized_ref_stdout_is_rejected_before_project_mutation(repl_setup, monkeypatch):
    root, configure, calls, _ = repl_setup
    monkeypatch.setattr(verification, "MAX_PROTOCOL_STDOUT_BYTES", 128)
    before = (root / "lakefile.toml").read_bytes()
    stdout = "a\trefs/tags/v4.20.0\n" + "b\trefs/tags/v9.0.0\n" * 100
    assert configure(stdout) == "setup_protocol_output_limit"
    assert (root / "lakefile.toml").read_bytes() == before
    assert len(calls) == 1
