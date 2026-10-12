"""Check kernel sandbox profiles protect controller state and reject link escapes."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.runtime.harness.isolation import (
    IsolationPolicy,
    IsolationUnavailable,
    _carve_root,
    build_runtime_isolation,
    sandbox_command,
)


def test_missing_sandbox_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(IsolationUnavailable):
        sandbox_command(["echo"], IsolationPolicy(tmp_path))


def test_escaping_symlink_and_hardlink_are_rejected(tmp_path: Path):
    workspace = tmp_path / "snapshot"
    workspace.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("private")
    link = workspace / "link"
    link.symlink_to(secret)
    with pytest.raises(IsolationUnavailable, match="symlink"):
        sandbox_command(["echo"], IsolationPolicy(workspace))
    link.unlink()
    os.link(secret, link)
    with pytest.raises(IsolationUnavailable, match="hardlink"):
        sandbox_command(["echo"], IsolationPolicy(workspace))


def test_linux_mount_carving_excludes_controller_tree(tmp_path: Path):
    native_home = tmp_path / "native"
    native_home.mkdir()
    controller = native_home / "worktrees" / "project"
    controller.mkdir(parents=True)
    allowed = native_home / "config.toml"
    allowed.write_text("model='native'")
    assert _carve_root(native_home, (controller,)) == [allowed]


def test_linux_mount_carving_excludes_alias_to_protected_ancestor(tmp_path: Path):
    project = tmp_path / "project"
    private = project / ".leanflow"
    private.mkdir(parents=True)
    (private / "state.json").write_text("controller private")
    (project / "alias").symlink_to(project, target_is_directory=True)
    source = project / "Main.lean"
    source.write_text("theorem t : True := by trivial")
    assert _carve_root(project, (private,)) == [source]


def test_linux_resolver_target_is_granted_without_its_runtime_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Expose a symlinked DNS file without exposing unrelated host runtime files."""
    resolver = Path("/etc/resolv.conf")
    target = tmp_path / "host-runtime" / "stub-resolv.conf"
    target.parent.mkdir()
    target.write_text("nameserver 127.0.0.53\n")
    original_resolve, original_exists = Path.resolve, Path.exists

    def resolve(path: Path, *args, **kwargs) -> Path:
        """Simulate the system resolver link while preserving all other paths."""
        return target if path == resolver else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "exists", lambda path: path == resolver or original_exists(path))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/bwrap")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    policy = build_runtime_isolation("codex", snapshot, env={"HOME": str(tmp_path / "home")})
    assert target in policy.readable_roots
    assert target.parent not in policy.readable_roots
    assert target not in policy.writable_roots
    command = sandbox_command(["/usr/bin/true"], policy)
    assert any(
        command[index : index + 3] == ["--ro-bind", str(target), str(target)]
        for index in range(len(command) - 2)
    )


@pytest.mark.skipif(
    platform.system() != "Darwin" or not shutil.which("sandbox-exec"),
    reason="macOS seatbelt integration",
)
def test_kernel_denies_canonical_but_allows_nested_snapshot(tmp_path: Path):
    canonical = tmp_path / "canonical"
    snapshot = canonical / ".leanflow" / "job" / "snapshot"
    snapshot.mkdir(parents=True)
    secret = canonical / "private-proof"
    secret.write_text("secret")
    writable = snapshot / "result"
    policy = IsolationPolicy(
        snapshot,
        readable_roots=(Path("/bin"), Path("/usr"), Path("/System"), Path("/dev")),
        denied_roots=(canonical,),
    )
    script = 'test ! -r "$1" && ! cat "$1" >/dev/null 2>&1 && printf okay > "$2"'
    completed = subprocess.run(
        sandbox_command(["/bin/sh", "-c", script, "sh", str(secret), str(writable)], policy),
        capture_output=True,
    )
    # Metadata reads are permitted, so -r may report true even when data is denied.
    if completed.returncode:
        completed = subprocess.run(
            sandbox_command(
                [
                    "/bin/sh",
                    "-c",
                    '! cat "$1" >/dev/null 2>&1 && printf okay > "$2"',
                    "sh",
                    str(secret),
                    str(writable),
                ],
                policy,
            ),
            capture_output=True,
        )
    assert completed.returncode == 0, completed.stderr.decode()
    assert writable.read_text() == "okay"
    assert secret.read_text() == "secret"
