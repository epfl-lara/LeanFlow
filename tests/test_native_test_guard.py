"""Prevent missed mocks from admitting installed native model executables in pytest."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent.runtime.harness import HarnessRequest, IsolationPolicy, run_harness
from tests.conftest import _require_fake_native_command


def _fake(path: Path) -> Path:
    """Create a harmless executable for admission checks without launching it."""
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def test_native_guard_accepts_only_contained_fake_executables(tmp_path: Path) -> None:
    executable = _fake(tmp_path / "claude")
    _require_fake_native_command([str(executable), "-p"], {}, tmp_path)
    _require_fake_native_command(["claude", "-p"], {"PATH": str(tmp_path)}, tmp_path)


def test_native_guard_rejects_symlink_to_outside_executable(tmp_path: Path) -> None:
    fake_root = tmp_path / "fixtures"
    fake_root.mkdir()
    outside = _fake(tmp_path / "outside")
    alias = fake_root / "claude"
    alias.symlink_to(outside)
    with pytest.raises(pytest.fail.Exception, match="Real native inference is disabled"):
        _require_fake_native_command([str(alias), "-p"], {}, fake_root)


def test_native_guard_preserves_missing_executable_failure(tmp_path: Path) -> None:
    _require_fake_native_command([str(tmp_path / "missing")], {}, tmp_path)


def test_suite_guard_stops_unmocked_harness_before_discovery_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.runtime.harness import runner

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Process admission was reached")

    monkeypatch.setattr(runner, "sandbox_command", forbidden)
    with tempfile.TemporaryDirectory(prefix="leanflow-no-inference-") as temporary:
        executable = _fake(Path(temporary) / "claude")
        request = HarnessRequest(
            "claude",
            "must never be sent",
            tmp_path,
            executable=str(executable),
            isolation=IsolationPolicy(tmp_path),
        )
        with pytest.raises(pytest.fail.Exception, match="Real native inference is disabled"):
            run_harness(request)
