from __future__ import annotations

import json
import multiprocessing
import os
import threading
from datetime import UTC, datetime, timedelta

import pytest

from leanflow_cli.runtime import file_locks
from leanflow_cli.runtime.file_locks import (
    acquire_file_lock,
    acquire_namespace_lock,
    describe_lock,
    ensure_file_lock,
    list_file_locks,
    release_all_file_locks,
    release_file_lock,
    release_namespace_lock,
    release_stale_file_locks,
)
from tools.implementations.file_tools import write_file_tool


def _multiprocess_contending_acquire(
    home, target, owner_id, start_event, finish_event, result_queue
):
    """Acquire one lease while keeping the child alive for conflict checks."""
    os.environ["LEANFLOW_HOME"] = home
    os.environ.pop("LEANFLOW_PROJECT_ROOT", None)
    if not start_event.wait(timeout=15):
        result_queue.put((owner_id, {"success": False, "error": "start timeout"}))
        return
    result = acquire_file_lock(target, owner_id=owner_id, purpose="process contention")
    result_queue.put((owner_id, result))
    finish_event.wait(timeout=15)


def _multiprocess_reentrant_acquire(home, target, result_queue):
    """Exercise a public registry update inside an outer registry transaction."""
    os.environ["LEANFLOW_HOME"] = home
    os.environ.pop("LEANFLOW_PROJECT_ROOT", None)
    with file_locks._file_lock_transaction(strict=True):
        result_queue.put(
            acquire_file_lock(
                target,
                owner_id="nested-process",
                purpose="reentrant transaction",
                strict=True,
            )
        )


def _multiprocess_probe_acquire(home, target, ready_event, done_event, result_queue):
    """Signal immediately before entering one cross-process registry transaction."""
    os.environ["LEANFLOW_HOME"] = home
    os.environ.pop("LEANFLOW_PROJECT_ROOT", None)
    ready_event.set()
    result_queue.put(acquire_file_lock(target, owner_id="probe-process", purpose="sidecar probe"))
    done_event.set()


def test_acquire_file_lock_blocks_other_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    first = acquire_file_lock(str(target), owner_id="agent-a", purpose="prove Main.lean")
    second = acquire_file_lock(str(target), owner_id="agent-b", purpose="other proof")

    assert first["success"] is True
    assert second["success"] is False
    assert "agent-a" in second["error"]


def test_write_file_tool_respects_foreign_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    result = json.loads(
        write_file_tool(str(target), "theorem demo : True := by trivial\n", owner_id="agent-b")
    )

    assert "locked" in result["error"]


def test_release_all_file_locks_clears_owner_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    payload = release_all_file_locks(owner_id="agent-a")
    retry = acquire_file_lock(str(target), owner_id="agent-b", purpose="next edit")

    assert payload["count"] == 1
    assert retry["success"] is True


def test_release_file_lock_fails_for_non_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    result = release_file_lock(str(target), owner_id="agent-b")

    assert result["success"] is False
    assert "agent-a" in result["error"]


def test_release_file_lock_with_force_allows_non_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    result = release_file_lock(str(target), owner_id="agent-b", force=True)

    assert result["success"] is True
    assert result["released"] is True

    retry = acquire_file_lock(str(target), owner_id="agent-b", purpose="now free")
    assert retry["success"] is True


def test_acquire_file_lock_with_force_overrides_existing_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="original")

    override = acquire_file_lock(str(target), owner_id="agent-b", purpose="takeover", force=True)

    assert override["success"] is True
    assert override["owner_id"] == "agent-b"

    lock = describe_lock(str(target))
    assert lock["owner_id"] == "agent-b"


def test_same_owner_can_reacquire_own_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    first = acquire_file_lock(str(target), owner_id="agent-a", purpose="first")
    second = acquire_file_lock(str(target), owner_id="agent-a", purpose="refresh")

    assert first["success"] is True
    assert second["success"] is True


def test_lock_records_owner_process_id(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    assert describe_lock(str(target))["process_id"] > 0


def test_list_file_locks_returns_empty_when_no_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    result = list_file_locks()

    assert result == []


def test_list_file_locks_returns_active_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    file_a = tmp_path / "A.lean"
    file_b = tmp_path / "B.lean"
    acquire_file_lock(str(file_a), owner_id="agent-a", purpose="edit A")
    acquire_file_lock(str(file_b), owner_id="agent-b", purpose="edit B")

    locks = list_file_locks()

    assert len(locks) == 2
    owners = {lock["owner_id"] for lock in locks}
    assert owners == {"agent-a", "agent-b"}


def test_describe_lock_returns_empty_for_unlocked_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Unlocked.lean"

    result = describe_lock(str(target))

    assert result == {}


def test_describe_lock_returns_entry_for_locked_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Locked.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="test lock")

    result = describe_lock(str(target))

    assert result["owner_id"] == "agent-a"
    assert result["purpose"] == "test lock"
    assert "expires_at" in result


def test_expired_lock_is_cleaned_up_on_next_acquire(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    acquire_file_lock(str(target), owner_id="agent-a", purpose="will expire", ttl_seconds=60)

    lock_file = tmp_path / "home" / "workflow-state" / "file_locks.json"
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
    normalized = str(target.resolve())
    payload["locks"][normalized].pop("process_id")
    payload["locks"][normalized]["expires_at"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()
    lock_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    retry = acquire_file_lock(str(target), owner_id="agent-b", purpose="after expiry")

    assert retry["success"] is True
    assert retry["owner_id"] == "agent-b"


def test_expired_process_backed_lock_remains_held_while_owner_is_alive(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="live owner", ttl_seconds=60)
    lock_file = tmp_path / "home" / "workflow-state" / "file_locks.json"
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
    normalized = str(target.resolve())
    payload["locks"][normalized]["expires_at"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()
    lock_file.write_text(json.dumps(payload), encoding="utf-8")

    retry = acquire_file_lock(str(target), owner_id="agent-b", purpose="competing edit")

    assert retry["success"] is False
    assert retry["lock"]["owner_id"] == "agent-a"


def test_dead_process_lock_is_cleaned_up_before_expiry(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="dead owner")
    monkeypatch.setattr(
        "leanflow_cli.runtime.file_locks._process_seems_alive", lambda process_id: False
    )

    retry = acquire_file_lock(str(target), owner_id="agent-b", purpose="resume")

    assert retry["success"] is True
    assert retry["owner_id"] == "agent-b"


def test_terminal_legacy_owner_lock_is_released_without_touching_unknown_owner(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    stale = tmp_path / "Stale.lean"
    unknown = tmp_path / "Unknown.lean"
    acquire_file_lock(str(stale), owner_id="dead-agent", purpose="old edit")
    acquire_file_lock(str(unknown), owner_id="unknown-agent", purpose="live elsewhere")
    lock_file = tmp_path / "home" / "workflow-state" / "file_locks.json"
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
    payload["locks"][str(stale.resolve())].pop("process_id")
    payload["locks"][str(unknown.resolve())].pop("process_id")
    lock_file.write_text(json.dumps(payload), encoding="utf-8")

    result = release_stale_file_locks(dead_owner_ids=["dead-agent"])

    assert result["released"] == [str(stale.resolve())]
    assert describe_lock(str(stale)) == {}
    assert describe_lock(str(unknown))["owner_id"] == "unknown-agent"


def test_ensure_file_lock_idempotent_for_own_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    first = ensure_file_lock(str(target), owner_id="agent-a", purpose="edit")
    second = ensure_file_lock(str(target), owner_id="agent-a", purpose="refresh")

    assert first["success"] is True
    assert second["success"] is True


def test_ensure_file_lock_fails_for_different_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    acquire_file_lock(str(target), owner_id="agent-a", purpose="held")
    result = ensure_file_lock(str(target), owner_id="agent-b")

    assert result["success"] is False
    assert "agent-a" in result["error"]


def test_release_all_file_locks_only_clears_own_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    file_a = tmp_path / "A.lean"
    file_b = tmp_path / "B.lean"
    acquire_file_lock(str(file_a), owner_id="agent-a", purpose="mine")
    acquire_file_lock(str(file_b), owner_id="agent-b", purpose="theirs")

    payload = release_all_file_locks(owner_id="agent-a")

    assert payload["count"] == 1
    remaining = list_file_locks()
    assert len(remaining) == 1
    assert remaining[0]["owner_id"] == "agent-b"


def test_acquire_lock_requires_nonempty_owner_id(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    result = acquire_file_lock(str(target), owner_id="")

    assert result["success"] is False
    assert "owner_id" in result["error"]


def test_simultaneous_threads_cannot_both_acquire_same_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    worker_count = 8
    start = threading.Barrier(worker_count)
    results = []

    def contend(index):
        start.wait(timeout=5)
        results.append(
            acquire_file_lock(str(target), owner_id=f"thread-{index}", purpose="thread contention")
        )

    threads = [threading.Thread(target=contend, args=(index,)) for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(result["success"] is True for result in results) == 1
    assert sum(result["success"] is False for result in results) == worker_count - 1


@pytest.mark.skipif(file_locks.fcntl is None, reason="requires POSIX flock")
def test_simultaneous_processes_cannot_both_acquire_same_file(tmp_path):
    context = multiprocessing.get_context("spawn")
    home = str(tmp_path / "home")
    target = str(tmp_path / "Main.lean")
    start_event = context.Event()
    finish_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_contending_acquire,
            args=(home, target, f"process-{index}", start_event, finish_event, result_queue),
        )
        for index in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start_event.set()
        results = [result_queue.get(timeout=15) for _process in processes]

        assert sum(result["success"] is True for _owner, result in results) == 1
        assert sum(result["success"] is False for _owner, result in results) == 1
    finally:
        finish_event.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)


@pytest.mark.skipif(file_locks.fcntl is None, reason="requires POSIX flock")
def test_registry_transaction_is_process_local_reentrant(tmp_path):
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_multiprocess_reentrant_acquire,
        args=(str(tmp_path / "home"), str(tmp_path / "Main.lean"), result_queue),
    )
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)

    assert process.exitcode == 0
    assert result_queue.get(timeout=5)["success"] is True


@pytest.mark.skipif(file_locks.fcntl is None, reason="requires POSIX flock")
def test_child_acquire_waits_for_registry_sidecar_flock(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    context = multiprocessing.get_context("spawn")
    ready_event = context.Event()
    done_event = context.Event()
    result_queue = context.Queue()
    sidecar = home / "workflow-state" / "file_locks.json.lock"
    sidecar.parent.mkdir(parents=True)
    process = context.Process(
        target=_multiprocess_probe_acquire,
        args=(
            str(home),
            str(tmp_path / "Main.lean"),
            ready_event,
            done_event,
            result_queue,
        ),
    )
    with sidecar.open("a", encoding="utf-8") as handle:
        file_locks.fcntl.flock(handle.fileno(), file_locks.fcntl.LOCK_EX)
        try:
            process.start()
            assert ready_event.wait(timeout=10)
            assert done_event.wait(timeout=0.25) is False
        finally:
            file_locks.fcntl.flock(handle.fileno(), file_locks.fcntl.LOCK_UN)
    assert done_event.wait(timeout=10)
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)

    assert process.exitcode == 0
    assert result_queue.get(timeout=5)["success"] is True


def test_namespace_acquire_rejects_foreign_descendant_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    child = project / "Main.lean"
    acquire_file_lock(str(child), owner_id="agent-a", purpose="active edit")

    reservation = acquire_namespace_lock(
        str(project), owner_id="terminal-b", purpose="terminal commit"
    )

    assert reservation["success"] is False
    assert reservation["lock"]["path"] == str(child.resolve())
    assert reservation["lock"]["owner_id"] == "agent-a"


def test_file_acquire_rejects_foreign_ancestor_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    acquire_namespace_lock(str(project), owner_id="terminal-a", purpose="terminal commit")

    reservation = acquire_file_lock(
        str(project / "Generated" / "Helper.lean"),
        owner_id="agent-b",
        purpose="new helper",
    )

    assert reservation["success"] is False
    assert reservation["lock"]["path"] == str(project.resolve())
    assert reservation["lock"]["kind"] == "namespace"


def test_namespace_acquire_rejects_foreign_ancestor_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    acquire_namespace_lock(str(project), owner_id="terminal-a", purpose="outer commit")

    reservation = acquire_namespace_lock(
        str(project / "Generated"), owner_id="terminal-b", purpose="nested commit"
    )

    assert reservation["success"] is False
    assert reservation["lock"]["path"] == str(project.resolve())


def test_namespace_boundary_does_not_block_similar_sibling(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    acquire_namespace_lock(str(project), owner_id="terminal-a", purpose="terminal commit")

    reservation = acquire_file_lock(
        str(tmp_path / "project-sibling" / "Main.lean"),
        owner_id="agent-b",
        purpose="unrelated edit",
    )

    assert reservation["success"] is True


def test_same_owner_can_strengthen_file_to_namespace_without_losing_child(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    child = project / "Main.lean"
    acquire_file_lock(str(child), owner_id="terminal-a", purpose="source")

    reservation = acquire_namespace_lock(
        str(project), owner_id="terminal-a", purpose="terminal commit"
    )
    blocked = acquire_file_lock(
        str(project / "Generated.lean"), owner_id="agent-b", purpose="late source"
    )

    assert reservation["success"] is True
    assert describe_lock(str(child))["owner_id"] == "terminal-a"
    assert blocked["success"] is False


def test_same_owner_file_refresh_does_not_downgrade_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    acquire_namespace_lock(str(project), owner_id="terminal-a", purpose="terminal commit")

    refreshed = acquire_file_lock(str(project), owner_id="terminal-a", purpose="source refresh")

    assert refreshed["success"] is True
    assert refreshed["kind"] == "namespace"
    assert describe_lock(str(project))["kind"] == "namespace"


def test_release_namespace_allows_new_descendant_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    acquire_namespace_lock(str(project), owner_id="terminal-a", purpose="terminal commit")

    released = release_namespace_lock(str(project), owner_id="terminal-a")
    reservation = acquire_file_lock(
        str(project / "Generated.lean"), owner_id="agent-b", purpose="new source"
    )

    assert released["success"] is True
    assert released["released"] is True
    assert reservation["success"] is True


def test_forced_namespace_acquire_cannot_evict_foreign_descendants(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    child = project / "Main.lean"
    acquire_file_lock(str(child), owner_id="agent-a", purpose="active edit")

    reservation = acquire_namespace_lock(
        str(project), owner_id="terminal-b", purpose="forced commit", force=True
    )

    assert reservation["success"] is False
    assert describe_lock(str(child))["owner_id"] == "agent-a"


def test_forced_file_acquire_cannot_evict_ancestor_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    acquire_namespace_lock(str(project), owner_id="terminal-a", purpose="terminal commit")

    reservation = acquire_file_lock(
        str(project / "Late.lean"), owner_id="agent-b", purpose="forced edit", force=True
    )

    assert reservation["success"] is False
    assert describe_lock(str(project))["kind"] == "namespace"
    assert describe_lock(str(project))["owner_id"] == "terminal-a"


def test_forced_file_acquire_cannot_evict_exact_path_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    source = tmp_path / "Main.lean"
    acquire_namespace_lock(str(source), owner_id="terminal-a", purpose="source authority")

    reservation = acquire_file_lock(
        str(source), owner_id="agent-b", purpose="forced edit", force=True
    )

    assert reservation["success"] is False
    assert describe_lock(str(source))["kind"] == "namespace"
    assert describe_lock(str(source))["owner_id"] == "terminal-a"


def test_forced_file_release_cannot_release_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    acquire_namespace_lock(str(project), owner_id="terminal-a", purpose="terminal commit")

    released = release_file_lock(str(project), owner_id="agent-b", force=True)

    assert released["success"] is False
    assert describe_lock(str(project))["kind"] == "namespace"
    assert describe_lock(str(project))["owner_id"] == "terminal-a"


@pytest.mark.skipif(file_locks.fcntl is None, reason="strict mode requires POSIX flock")
@pytest.mark.parametrize(
    "raw_registry",
    [
        b"{not-json",
        b"[]",
        b'{"version": 99, "locks": {}}',
        b'{"version": 1, "locks": []}',
    ],
    ids=[
        "malformed-json",
        "non-object",
        "unknown-version",
        "non-object-locks",
    ],
)
def test_strict_namespace_acquire_fails_closed_on_unknown_registry(
    monkeypatch, tmp_path, raw_registry
):
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    lock_file = home / "workflow-state" / "file_locks.json"
    lock_file.parent.mkdir(parents=True)
    lock_file.write_bytes(raw_registry)

    reservation = acquire_namespace_lock(
        str(tmp_path / "project"),
        owner_id="terminal-a",
        purpose="terminal commit",
        strict=True,
    )

    assert reservation["success"] is False
    assert reservation["registry_error"] is True
    assert raw_registry == lock_file.read_bytes()


@pytest.mark.skipif(file_locks.fcntl is None, reason="strict mode requires POSIX flock")
@pytest.mark.parametrize(
    "entry",
    ["unknown-entry", {"owner_id": "agent-a", "kind": "future-kind"}],
    ids=["non-object-entry", "unknown-entry-kind"],
)
def test_strict_namespace_acquire_rejects_unknown_registry_entry(monkeypatch, tmp_path, entry):
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    lock_file = home / "workflow-state" / "file_locks.json"
    lock_file.parent.mkdir(parents=True)
    payload = {
        "version": 1,
        "locks": {str((tmp_path / "Locked.lean").resolve()): entry},
    }
    raw_registry = json.dumps(payload).encode()
    lock_file.write_bytes(raw_registry)

    reservation = acquire_namespace_lock(
        str(tmp_path / "project"),
        owner_id="terminal-a",
        purpose="terminal commit",
        strict=True,
    )

    assert reservation["success"] is False
    assert reservation["registry_error"] is True
    assert raw_registry == lock_file.read_bytes()


@pytest.mark.skipif(file_locks.fcntl is None, reason="strict mode requires POSIX flock")
def test_strict_namespace_acquire_rejects_noncanonical_registry_path(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    lock_file = home / "workflow-state" / "file_locks.json"
    lock_file.parent.mkdir(parents=True)
    noncanonical = str(tmp_path / "project" / ".." / "Locked.lean")
    raw_registry = json.dumps(
        {"version": 1, "locks": {noncanonical: {"owner_id": "agent-a"}}}
    ).encode()
    lock_file.write_bytes(raw_registry)

    reservation = acquire_namespace_lock(
        str(tmp_path / "project"), owner_id="terminal-a", strict=True
    )

    assert reservation["success"] is False
    assert reservation["registry_error"] is True
    assert raw_registry == lock_file.read_bytes()


@pytest.mark.skipif(file_locks.fcntl is None, reason="strict mode requires POSIX flock")
def test_strict_namespace_release_fails_closed_if_registry_became_corrupt(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    project = tmp_path / "project"
    acquired = acquire_namespace_lock(
        str(project), owner_id="terminal-a", purpose="terminal commit", strict=True
    )
    assert acquired["success"] is True
    lock_file = home / "workflow-state" / "file_locks.json"
    corrupt_registry = b"{corrupt-after-acquire"
    lock_file.write_bytes(corrupt_registry)

    released = release_namespace_lock(str(project), owner_id="terminal-a", strict=True)

    assert released["success"] is False
    assert released["registry_error"] is True
    assert corrupt_registry == lock_file.read_bytes()


@pytest.mark.skipif(file_locks.fcntl is None, reason="strict mode requires POSIX flock")
def test_strict_namespace_release_fails_closed_if_registry_disappeared(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    project = tmp_path / "project"
    acquired = acquire_namespace_lock(str(project), owner_id="terminal-a", strict=True)
    assert acquired["success"] is True
    lock_file = home / "workflow-state" / "file_locks.json"
    lock_file.unlink()

    released = release_namespace_lock(str(project), owner_id="terminal-a", strict=True)

    assert released["success"] is False
    assert released["registry_error"] is True
    assert not lock_file.exists()


@pytest.mark.skipif(file_locks.fcntl is None, reason="strict mode requires POSIX flock")
def test_strict_release_all_fails_closed_if_registry_became_corrupt(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    acquired = acquire_file_lock(str(tmp_path / "Main.lean"), owner_id="terminal-a", strict=True)
    assert acquired["success"] is True
    lock_file = home / "workflow-state" / "file_locks.json"
    corrupt_registry = b"{corrupt-before-release-all"
    lock_file.write_bytes(corrupt_registry)

    released = release_all_file_locks(owner_id="terminal-a", strict=True)

    assert released["success"] is False
    assert released["registry_error"] is True
    assert corrupt_registry == lock_file.read_bytes()


def test_ordinary_file_acquire_remains_tolerant_of_corrupt_registry(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    lock_file = home / "workflow-state" / "file_locks.json"
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text("{not-json", encoding="utf-8")

    reservation = acquire_file_lock(
        str(tmp_path / "Main.lean"), owner_id="agent-a", purpose="advisory edit"
    )

    assert reservation["success"] is True
    assert json.loads(lock_file.read_text(encoding="utf-8"))["version"] == 1
