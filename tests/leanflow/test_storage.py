"""Exercise storage ownership, active leases, disk-pressure guards, and recovery."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from leanflow_cli.runtime.storage import (
    StoragePolicy,
    StoragePressure,
    StorageSafetyError,
    cleanup_storage,
    ensure_storage_capacity,
    entries,
    forget_workspace,
    lease_entry,
    leases,
    maintenance,
    register_entry,
    register_workspace,
    storage_lock,
    storage_status,
    touch_entry,
    workspaces,
)
from leanflow_cli.runtime.storage import process_views as process_inventory
from leanflow_cli.runtime.storage.entries import MARKER, managed_entry_size
from leanflow_cli.runtime.storage.policy import GIB
from leanflow_cli.runtime.storage.process_views import ProcessViews
from leanflow_cli.runtime.storage.workspaces import _record


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Confine every test to its own private managed home."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("LEANFLOW_HOME", str(path))
    return path


def _entry(home: Path, name: str, *, stamp: float = 1, kind: str = "dependency") -> Path:
    """Create one small explicitly owned dependency or runtime entry."""
    if kind == "dependency":
        path = home / "cache/dependencies/v1" / (name * 64)
    elif kind == "staging":
        path = home / "cache/dependencies/v1" / f".staging-{name}"
    else:
        path = home / ("releases" if kind == "runtime-release" else "runtimes") / name
    path.mkdir(parents=True)
    (path / "data").write_bytes(b"x" * 4096)
    register_entry(path, kind=kind)
    payload = json.loads((path / MARKER).read_text())
    payload["last_used"] = stamp
    (path / MARKER).write_text(json.dumps(payload))
    return path


def _policy(*, target: float = 0, reserve: float = 0, automatic: bool = True) -> StoragePolicy:
    return StoragePolicy(target, reserve, automatic)


def test_defaults_and_validation() -> None:
    policy = StoragePolicy.from_config({})
    assert policy.cache_target_bytes == 20 * GIB
    assert policy.min_free_bytes == 10 * GIB
    assert policy.automatic_cleanup
    for section in (
        {"cache_target_gib": -1},
        {"min_free_gib": float("nan")},
        {"min_free_gib": "10"},
        {"cache_target_gib": True},
        {"automatic_cleanup": "false"},
    ):
        with pytest.raises(ValueError):
            StoragePolicy.from_config({"storage": section})


def test_lru_eviction_and_touch(home: Path) -> None:
    older = _entry(home, "a", stamp=1)
    newer = _entry(home, "b", stamp=2)
    one_size = managed_entry_size(newer)
    report = cleanup_storage(policy=_policy(target=one_size / GIB))
    assert not older.exists()
    assert newer.exists()
    assert report["removed"] == [str(older)]
    assert report["reclaimed_bytes"] > 0
    before = json.loads((newer / MARKER).read_text())
    touch_entry(newer)
    after = json.loads((newer / MARKER).read_text())
    assert after["created_at"] == before["created_at"]
    assert after["last_used"] > before["last_used"]


def test_active_lease_and_nested_lock_pin_entry(home: Path) -> None:
    entry = _entry(home, "a")
    with storage_lock(), storage_lock(), lease_entry(entry):
        status = cleanup_storage(policy=_policy())
        assert entry.exists()
        assert status["pinned_bytes"] > 0
    cleanup_storage(policy=_policy())
    assert not entry.exists()


def test_confirmed_dead_and_reused_pid_leases_are_reclaimed(home: Path, monkeypatch) -> None:
    entry = _entry(home, "a")
    with lease_entry(entry):
        slot = leases._lease_directory(entry, home)
        record = next(slot.iterdir())
        payload = json.loads(record.read_text())
        payload["pid"] = 99999999
        record.write_text(json.dumps(payload))
        assert not leases.entry_is_leased(entry)
    entry = _entry(home, "b")
    with lease_entry(entry):
        monkeypatch.setattr(leases, "_process_start", lambda pid: "different-launch")
        assert not leases.entry_is_leased(entry)
    cleanup_storage(policy=_policy())
    assert not entry.exists()


def test_uncertain_identity_and_invalid_lease_fail_closed(home: Path, monkeypatch) -> None:
    entry = _entry(home, "a")
    with lease_entry(entry):
        monkeypatch.setattr(leases, "_process_start", lambda pid: None)
        assert leases.entry_is_leased(entry)
        cleanup_storage(policy=_policy())
        assert entry.exists()
    slot = leases._lease_directory(entry, home)
    slot.mkdir(parents=True, exist_ok=True)
    (slot / "broken.json").write_text("not json")
    cleanup_storage(policy=_policy())
    assert entry.exists()


def test_unknown_marker_and_symlink_entries_are_preserved(home: Path, tmp_path: Path) -> None:
    unknown = _entry(home, "a")
    (unknown / MARKER).write_text('{"schema_version": 999}')
    outside = tmp_path / "proofs"
    outside.mkdir()
    (outside / "Proof.lean").write_text("verified proof")
    linked = home / "cache/dependencies/v1" / ("b" * 64)
    linked.symlink_to(outside, target_is_directory=True)
    unmarked = home / "runtimes/legacy"
    unmarked.mkdir(parents=True)
    (unmarked / "checkpoint.json").write_text("preserve")
    report = cleanup_storage(policy=_policy())
    assert unknown.exists() and linked.is_symlink() and unmarked.exists()
    assert (outside / "Proof.lean").read_text() == "verified proof"
    assert set(report["preserved_unknown"]) == {str(unknown), str(linked), str(unmarked)}


def test_current_install_and_in_progress_runtime_are_protected(home: Path) -> None:
    old = _entry(home, "old", kind="runtime")
    current = _entry(home, "current", kind="runtime")
    release = _entry(home, "source", kind="runtime-release")
    installing = _entry(home, "installing", kind="runtime")
    register_entry(
        installing, kind="runtime", metadata={"ready": False, "installer_pid": os.getpid()}
    )
    (home / "install-root").write_text(f"repo_root={release}/source\nvenv_dir={current}/venv\n")
    cleanup_storage(policy=_policy())
    assert not old.exists()
    assert current.exists() and release.exists() and installing.exists()


def test_unverifiable_install_metadata_pins_every_runtime(home: Path) -> None:
    runtime = _entry(home, "old", kind="runtime")
    (home / "install-root").write_text("truncated data")
    cleanup_storage(policy=_policy())
    assert runtime.exists()


def test_staging_is_cleaned_even_below_soft_target(home: Path) -> None:
    stage = _entry(home, "abandoned", kind="staging")
    report = cleanup_storage(policy=_policy(target=20))
    assert not stage.exists()
    assert str(stage) in report["removed"]


def test_sealed_directories_and_internal_links_delete_safely(home: Path, tmp_path: Path) -> None:
    entry = _entry(home, "a")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "important").write_text("retain")
    (entry / "external").symlink_to(outside, target_is_directory=True)
    nested = entry / "sealed"
    nested.mkdir()
    (nested / "data").write_text("owned")
    nested.chmod(0o500)
    entry.chmod(0o500)
    cleanup_storage(policy=_policy())
    assert not entry.exists()
    assert (outside / "important").read_text() == "retain"


def test_pressure_cleans_first_then_blocks_new_work(home: Path, monkeypatch) -> None:
    entry = _entry(home, "a")
    monkeypatch.setattr(maintenance, "_disk_free", lambda path: 0)
    with pytest.raises(StoragePressure) as error:
        ensure_storage_capacity(policy=_policy(reserve=10))
    assert not entry.exists()
    assert error.value.required_bytes == 10 * GIB
    assert "resume" in str(error.value)


def test_pressure_recovery_and_disabled_automatic_cleanup(home: Path, monkeypatch) -> None:
    entry = _entry(home, "a")
    monkeypatch.setattr(
        maintenance, "_disk_free", lambda path: 20 * GIB if not entry.exists() else 0
    )
    result = ensure_storage_capacity(policy=_policy(reserve=10, automatic=False))
    assert not entry.exists() and result["free_bytes"] == 20 * GIB
    untouched = _entry(home, "b")
    ensure_storage_capacity(policy=_policy(automatic=False))
    assert untouched.exists()


def _hold_entry(home: str, entry: str, ready, finish) -> None:
    """Hold a child-process storage lease until the parent completes its inspection."""
    os.environ["LEANFLOW_HOME"] = home
    with lease_entry(Path(entry)):
        ready.set()
        finish.wait(15)


def test_cross_process_lease_prevents_eviction(home: Path) -> None:
    entry = _entry(home, "a")
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    child = context.Process(target=_hold_entry, args=(str(home), str(entry), ready, finish))
    child.start()
    try:
        assert ready.wait(10)
        cleanup_storage(policy=_policy())
        assert entry.exists()
    finally:
        finish.set()
        child.join(15)
        if child.is_alive():
            child.terminate()
            child.join()
    assert child.exitcode == 0
    cleanup_storage(policy=_policy())
    assert not entry.exists()


def _workspace(tmp_path: Path) -> Path:
    """Build a private view with proofs, checkpoints, scratch, and library artifacts."""
    snapshot = tmp_path / "project"
    for relative in (".lake/packages/lib", ".lake/build", ".leanflow-job", "checkpoints"):
        (snapshot / relative).mkdir(parents=True)
    for relative in (
        "Proof.lean",
        ".lake/build/Own.olean",
        ".leanflow-job/result.json",
        "checkpoints/state.json",
    ):
        (snapshot / relative).write_text("durable result")
    (snapshot / ".lake/packages/lib/Lib.olean").write_bytes(b"cached dependency" * 200)
    return snapshot


def test_dead_workspace_cleanup_keeps_every_proof_checkpoint_and_build(
    home: Path, tmp_path: Path
) -> None:
    snapshot = _workspace(tmp_path)
    register_workspace(snapshot)
    live = cleanup_storage(policy=_policy())
    assert live["workspace_pinned_bytes"] > 0
    assert (snapshot / ".lake/packages").exists()
    record = _record(snapshot, home)
    payload = json.loads(record.read_text())
    payload["pid"] = 99999999
    record.write_text(json.dumps(payload))
    cleaned = cleanup_storage(policy=_policy())
    assert not (snapshot / ".lake/packages").exists()
    assert (snapshot / ".leanflow-dependencies-released").is_file()
    assert cleaned["reclaimed_bytes"] > 0
    for relative in (
        "Proof.lean",
        ".lake/build/Own.olean",
        ".leanflow-job/result.json",
        "checkpoints/state.json",
    ):
        assert (snapshot / relative).read_text() == "durable result"


def test_workspace_unknown_owner_or_links_are_never_pruned(home: Path, tmp_path: Path) -> None:
    snapshot = _workspace(tmp_path)
    register_workspace(snapshot)
    record = _record(snapshot, home)
    payload = json.loads(record.read_text())
    payload["pid"] = 99999999
    record.write_text(json.dumps(payload))
    (snapshot / ".leanflow-cache-owner").write_text('{"schema_version":999}')
    cleanup_storage(policy=_policy())
    assert (snapshot / ".lake/packages").exists()
    with pytest.raises(StorageSafetyError, match="linked"):
        alias = tmp_path / "alias"
        alias.symlink_to(snapshot, target_is_directory=True)
        register_workspace(alias)


def test_workspace_forget_stops_tracking_without_deleting_outputs(
    home: Path, tmp_path: Path
) -> None:
    snapshot = _workspace(tmp_path)
    register_workspace(snapshot)
    forget_workspace(snapshot)
    assert not _record(snapshot, home).exists()
    assert (snapshot / "Proof.lean").exists()
    assert (snapshot / ".lake/packages").exists()
    assert storage_status(policy=_policy())["workspace_bytes"] == 0


def test_linked_registry_and_unmanaged_lease_fail_closed(home: Path, tmp_path: Path) -> None:
    with pytest.raises(StorageSafetyError, match="unmanaged"):
        with lease_entry(tmp_path / "user-proof"):
            pass
    storage = home / "storage"
    storage.mkdir()
    (storage / "registry.lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(StorageSafetyError, match="linked"):
        cleanup_storage(policy=_policy())


def test_obsolete_managed_runtime_is_removed_below_cache_target(home: Path) -> None:
    old = _entry(home, "old", kind="runtime")
    release = _entry(home, "old-source", kind="runtime-release")
    cache = _entry(home, "a")
    report = cleanup_storage(policy=_policy(target=20))
    assert not old.exists() and not release.exists()
    assert cache.exists()
    assert set(report["removed"]) == {str(old), str(release)}


def test_confirmed_dead_unfinished_install_can_be_reclaimed(home: Path) -> None:
    dead = _entry(home, "dead-install", kind="runtime")
    register_entry(dead, kind="runtime", metadata={"ready": False, "installer_pid": 99999999})
    payload = json.loads((dead / MARKER).read_text())
    payload["installer_process_start"] = "old-start"
    (dead / MARKER).write_text(json.dumps(payload))
    cleanup_storage(policy=_policy(target=20))
    assert not dead.exists()


def test_admission_reuses_publication_size_without_scanning_artifacts(
    home: Path, monkeypatch
) -> None:
    entry = _entry(home, "a")

    def unexpected_scan(path: Path) -> int:
        pytest.fail(f"admission rescanned a published tree: {path}")

    monkeypatch.setattr(entries, "managed_entry_size", unexpected_scan)
    report = ensure_storage_capacity(policy=_policy(target=20))
    assert entry.exists()
    assert report["managed_bytes"] > 0


def test_workspace_admission_reuses_size_and_owner_probe(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    first = _workspace(tmp_path / "first")
    second = _workspace(tmp_path / "second")
    register_workspace(first)
    register_workspace(second)
    calls = []

    def unexpected_scan(path: Path) -> int:
        pytest.fail(f"admission rescanned a private view: {path}")

    original = workspaces._lease_active

    def recorded_probe(payload):
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(workspaces, "managed_entry_size", unexpected_scan)
    monkeypatch.setattr(workspaces, "_lease_active", recorded_probe)
    result = storage_status(policy=_policy())
    assert result["workspace_pinned_bytes"] > 0
    assert len(calls) == 1


def test_register_workspace_revokes_dead_owner_before_collection(
    home: Path, tmp_path: Path
) -> None:
    snapshot = _workspace(tmp_path)
    register_workspace(snapshot)
    record = _record(snapshot, home)
    payload = json.loads(record.read_text())
    payload["pid"] = 99999999
    record.write_text(json.dumps(payload))
    with storage_lock():
        register_workspace(snapshot)
        cleanup_storage(policy=_policy())
    assert (snapshot / ".lake/packages").exists()
    assert (snapshot / "Proof.lean").exists()


def _sleeping_child(executable: Path, *, cwd: Path) -> subprocess.Popen[str]:
    """Launch one isolated test process in its own session and await its ready signal."""
    child = subprocess.Popen(
        [str(executable), "-c", "import time; print('ready', flush=True); time.sleep(30)"],
        cwd=cwd,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None and child.stdout.readline() == "ready\n"
    return child


def _cleanup_after_reaped_child(
    child: subprocess.Popen[str], removed: tuple[Path, ...], *, timeout_s: float = 10
) -> None:
    """Retry only an observed uncertain host inventory after this test's child has been reaped."""
    assert child.returncode is not None
    original = process_inventory.process_views
    observed: ProcessViews | None = None

    def inspect() -> ProcessViews:
        nonlocal observed
        observed = original()
        return observed

    deadline = time.monotonic() + timeout_s
    with patch.object(process_inventory, "process_views", inspect):
        while True:
            observed = None
            report = cleanup_storage(policy=_policy())
            if not any(path.exists() for path in removed):
                return
            if observed is None or observed.complete or time.monotonic() >= deadline:
                break
            # Other xdist children can exit between ps and lsof. Unknown
            # inventories must retain storage; retry a fresh maintenance pass.
            time.sleep(0.05)
    raise AssertionError(
        "Storage remained after reaped-child cleanup: "
        + json.dumps(
            {
                "inventory_complete": None if observed is None else observed.complete,
                "entries": report["entries"],
                "cleanup_errors": report["cleanup_errors"],
                "preserved_unknown": report["preserved_unknown"],
            }
        )
    )


def test_orphan_child_cwd_protects_workspace_after_controller_death(
    home: Path, tmp_path: Path
) -> None:
    snapshot = _workspace(tmp_path)
    register_workspace(snapshot)
    record = _record(snapshot, home)
    payload = json.loads(record.read_text())
    payload["pid"] = 99999999
    record.write_text(json.dumps(payload))
    child = _sleeping_child(Path(sys.executable), cwd=snapshot)
    try:
        assert os.getpgid(child.pid) != os.getpgrp()
        report = cleanup_storage(policy=_policy())
        assert (snapshot / ".lake/packages").exists()
        assert report["workspace_pinned_bytes"] > 0
    finally:
        child.terminate()
        child.communicate(timeout=10)
    _cleanup_after_reaped_child(child, (snapshot / ".lake/packages",))
    assert not (snapshot / ".lake/packages").exists()
    assert (snapshot / "Proof.lean").exists()


def test_orphan_runtime_python_protects_runtime_and_its_source_release(
    home: Path, tmp_path: Path
) -> None:
    runtime = _entry(home, "orphan-runtime", kind="runtime")
    release = _entry(home, "source", kind="runtime-release")
    register_entry(runtime, kind="runtime", metadata={"source_root": str(release / "source")})
    binary = runtime / "venv/bin/python"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(sys.executable)
    child = _sleeping_child(binary, cwd=tmp_path)
    try:
        cleanup_storage(policy=_policy())
        assert runtime.exists() and release.exists()
    finally:
        child.terminate()
        child.communicate(timeout=10)
    _cleanup_after_reaped_child(child, (runtime, release))
    assert not runtime.exists() and not release.exists()


def test_reaped_child_cleanup_retries_only_incomplete_inventory(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _entry(home, "retry-runtime", kind="runtime")
    child = _sleeping_child(Path(sys.executable), cwd=tmp_path)
    child.terminate()
    child.communicate(timeout=10)
    seen: list[bool] = []

    def inspect() -> ProcessViews:
        seen.append(runtime.exists())
        return ProcessViews((), (), len(seen) > 1)

    monkeypatch.setattr(process_inventory, "process_views", inspect)
    _cleanup_after_reaped_child(child, (runtime,))
    assert seen == [True, True] and not runtime.exists()


def test_reaped_child_cleanup_still_fails_when_inventory_stays_unknown(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _entry(home, "unknown-retry-runtime", kind="runtime")
    child = _sleeping_child(Path(sys.executable), cwd=tmp_path)
    child.terminate()
    child.communicate(timeout=10)
    monkeypatch.setattr(process_inventory, "process_views", lambda: ProcessViews((), (), False))
    with pytest.raises(AssertionError, match="unverifiable process inventory"):
        _cleanup_after_reaped_child(child, (runtime,), timeout_s=0)
    assert runtime.exists()


def test_unknown_process_inventory_preserves_dead_views_and_runtimes(
    home: Path, tmp_path: Path, monkeypatch
) -> None:
    runtime = _entry(home, "unknown-runtime", kind="runtime")
    snapshot = _workspace(tmp_path)
    register_workspace(snapshot)
    record = _record(snapshot, home)
    payload = json.loads(record.read_text())
    payload["pid"] = 99999999
    record.write_text(json.dumps(payload))
    monkeypatch.setattr(process_inventory, "process_views", lambda: ProcessViews((), (), False))
    result = cleanup_storage(policy=_policy())
    assert runtime.exists() and (snapshot / ".lake/packages").exists()
    assert result["pinned_bytes"] > 0 and result["workspace_pinned_bytes"] > 0


def test_process_inventory_uses_path_boundaries() -> None:
    root = Path("/tmp/private-project")
    assert not ProcessViews(("/tmp/private-project-other",), (), True).uses(root)
    assert not ProcessViews((), ("lean /tmp/private-project-other/Main.lean",), True).uses(root)
    assert ProcessViews((), ("lean /tmp/private-project/Main.lean",), True).uses(root)
    assert ProcessViews((str(root / "nested"),), (), True).uses(root)


def test_macos_inventory_accepts_mandatory_cwd_field(monkeypatch) -> None:
    def capture(command, **kwargs):
        output = (
            f" 123 {os.getuid()} S python /tmp/project/Main.py\n"
            if command[0] == "ps"
            else "p123\nfcwd\nn/tmp/project\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(process_inventory.subprocess, "run", capture)
    views = process_inventory._posix_views()
    assert views.complete
    assert views.working_directories == ("/tmp/project",)


def test_macos_inventory_retains_when_a_listed_process_loses_cwd(monkeypatch) -> None:
    """Preserve uncertainty when a process exits after ps but remains an unreaped zombie."""

    def capture(command, **kwargs):
        output = (
            f"123 {os.getuid()} S python /tmp/other.py\n456 {os.getuid()} S python child\n"
            if command[0] == "ps"
            else "p123\nfcwd\nn/tmp/other\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    probes = []
    monkeypatch.setattr(process_inventory.subprocess, "run", capture)
    monkeypatch.setattr(
        process_inventory.os, "kill", lambda pid, signal: probes.append((pid, signal))
    )
    views = process_inventory._posix_views()
    assert probes == [(456, 0)]
    assert not views.complete
    assert views.uses(Path("/tmp/unrelated-runtime"))


def test_linux_login_helper_requires_privileged_service_parent(tmp_path: Path) -> None:
    """Do not let unreadable login daemons block all storage cleanup on SSH hosts."""
    helper, parent = tmp_path / "123", tmp_path / "122"
    helper.mkdir()
    parent.mkdir()
    (helper / "status").write_text("PPid:\t122\n")
    (helper / "comm").write_text("sshd\n")
    (parent / "comm").write_text("sshd\n")
    (parent / "status").write_text("Uid:\t0\t0\t0\t0\n")
    assert process_inventory._system_session_helper(helper)
    (parent / "status").write_text("Uid:\t1000\t1000\t1000\t1000\n")
    assert not process_inventory._system_session_helper(helper)
    (helper / "comm").write_text("python\n")
    (parent / "status").write_text("Uid:\t0\t0\t0\t0\n")
    assert not process_inventory._system_session_helper(helper)
