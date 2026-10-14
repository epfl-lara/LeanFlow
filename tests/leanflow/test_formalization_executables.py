"""Bind formalization compiler invocations to the fingerprinted prefix despite PATH shadows."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from core.deadlines import deadline_after
from leanflow_cli.formalization import environment, lean_layout, lean_policy


@pytest.fixture
def compiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    prefix = tmp_path / "compiler"
    binary = prefix / "bin"
    shadow = tmp_path / "shadow"
    binary.mkdir(parents=True)
    (prefix / "lib/lean").mkdir(parents=True)
    shadow.mkdir()
    (binary / "lean").write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(str(prefix)) + "\n")
    (binary / "lake").write_text("#!/bin/sh\nprintf '%s\\n' pinned-lake\n")
    (shadow / "lake").write_text("#!/bin/sh\nprintf '%s\\n' shadow-lake\n")
    for path in [binary / "lean", binary / "lake", shadow / "lake"]:
        path.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(shadow), str(binary), os.environ["PATH"]]))
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_text("pinned\n")
    (root / "lakefile.toml").write_text('name = "example"\n')
    return prefix, root


def test_shadow_lake_cannot_replace_fingerprinted_compiler(compiler: tuple[Path, Path]) -> None:
    prefix, root = compiler
    assert subprocess.check_output(["lake"], text=True).strip() == "shadow-lake"
    executable = environment.compiler_executable(root, "lake")
    assert executable == str(prefix / "bin/lake")
    assert subprocess.check_output([executable], text=True).strip() == "pinned-lake"


def test_layout_and_inspector_use_absolute_pinned_executables(
    compiler: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prefix, root = compiler
    calls = []

    def check(**kwargs):
        calls.append(kwargs["argv"])
        return {"success": False}

    monkeypatch.setattr(lean_layout, "isolated_command", check)
    monkeypatch.setattr(lean_policy, "isolated_command", check)
    assert not lean_layout.inspect_layout(root)["accepted"]
    workspace = tmp_path / "inspect"
    workspace.mkdir()
    assert not lean_policy._inspect(root, [], workspace, [], (), deadline_after(5), None, root)[
        "accepted"
    ]
    assert not lean_policy._inspect(
        root,
        [],
        workspace,
        [],
        (),
        deadline_after(5),
        None,
        root,
        full_import_replay=False,
    )["accepted"]
    assert calls[0][0] == str(prefix / "bin/lean")
    assert calls[1][:2] == [str(prefix / "bin/lean"), "--run"]
    assert calls[2][:3] == [str(prefix / "bin/lake"), "env", str(prefix / "bin/lean")]


def test_compiler_executable_alias_cannot_escape_hashed_prefix(
    compiler: tuple[Path, Path], tmp_path: Path
) -> None:
    prefix, root = compiler
    executable = prefix / "bin/lake"
    executable.unlink()
    executable.symlink_to(tmp_path / "shadow/lake")
    with pytest.raises(ValueError, match="escapes"):
        environment.compiler_executable(root, "lake")


@pytest.mark.parametrize("variable", ["LEAN_PATH", "ELAN_TOOLCHAIN"])
def test_full_inspector_blocks_loader_overrides_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str
) -> None:
    monkeypatch.setenv(variable, "unrecorded-loader-input")
    monkeypatch.setattr(
        lean_policy,
        "compiler_executable",
        lambda *args: pytest.fail("must reject before compiler startup"),
    )
    with pytest.raises(ValueError, match="loader overrides"):
        lean_policy._inspect(tmp_path, [], tmp_path, [], (), None, None, tmp_path)


def test_unavailable_compiler_returns_honest_layout_failure(
    compiler: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, root = compiler

    def unavailable(_: Path) -> Path:
        raise subprocess.CalledProcessError(1, ["lean", "--print-prefix"])

    monkeypatch.setattr(environment, "_compiler_root", unavailable)
    result = lean_layout.inspect_layout(root)
    assert not result["accepted"]
    assert "could not be resolved" in result["error"]


@pytest.mark.parametrize(
    "diagnostic",
    ["maximum number of heartbeats exceeded", "maximum recursion depth has been reached"],
)
def test_inspector_resource_exhaustion_is_operational(
    compiler: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diagnostic: str
) -> None:
    _, root = compiler
    workspace = tmp_path / "inspection"
    workspace.mkdir()
    monkeypatch.setattr(
        lean_policy, "isolated_command", lambda **kwargs: {"success": False, "stderr": diagnostic}
    )
    result = lean_policy._inspect(root, [], workspace, [], (), None, None, root)
    assert not result["accepted"]
    assert result["details"]["error_code"] == "check_timeout"


@pytest.mark.parametrize("when", ["before", "during"])
def test_exact_local_artifact_hash_binds_inspector_input(
    compiler: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    _, root = compiler
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    artifact = root / "Main.olean"
    artifact.write_bytes(b"recorded")
    binding = {
        "Main": {
            "parts": [[str(artifact)], []],
            "sha256": {str(artifact): hashlib.sha256(artifact.read_bytes()).hexdigest()},
        }
    }
    workspace = tmp_path / "inspection"
    workspace.mkdir()

    def invoke(**kwargs):
        assert when == "during", "Changed artifact must fail before launching the checker"
        artifact.write_bytes(b"changed")
        return {"success": False}

    monkeypatch.setattr(lean_policy, "isolated_command", invoke)
    if when == "before":
        artifact.write_bytes(b"changed")
    with pytest.raises(ValueError, match="Compiled local artifact changed"):
        lean_policy._inspect(
            root, ["Main"], workspace, [], (), None, None, root, local_artifacts=binding
        )
