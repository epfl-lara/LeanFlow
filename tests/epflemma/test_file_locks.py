from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

from epflemma_cli.runtime.file_locks import (
    acquire_file_lock,
    describe_lock,
    ensure_file_lock,
    list_file_locks,
    release_all_file_locks,
    release_file_lock,
)
from tools.implementations.file_tools import write_file_tool


def test_acquire_file_lock_blocks_other_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    first = acquire_file_lock(str(target), owner_id="agent-a", purpose="prove Main.lean")
    second = acquire_file_lock(str(target), owner_id="agent-b", purpose="other proof")

    assert first["success"] is True
    assert second["success"] is False
    assert "agent-a" in second["error"]


def test_write_file_tool_respects_foreign_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    result = json.loads(write_file_tool(str(target), "theorem demo : True := by trivial\n", owner_id="agent-b"))

    assert "locked" in result["error"]


def test_release_all_file_locks_clears_owner_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    payload = release_all_file_locks(owner_id="agent-a")
    retry = acquire_file_lock(str(target), owner_id="agent-b", purpose="next edit")

    assert payload["count"] == 1
    assert retry["success"] is True


def test_release_file_lock_fails_for_non_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    result = release_file_lock(str(target), owner_id="agent-b")

    assert result["success"] is False
    assert "agent-a" in result["error"]


def test_release_file_lock_with_force_allows_non_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    result = release_file_lock(str(target), owner_id="agent-b", force=True)

    assert result["success"] is True
    assert result["released"] is True

    retry = acquire_file_lock(str(target), owner_id="agent-b", purpose="now free")
    assert retry["success"] is True


def test_acquire_file_lock_with_force_overrides_existing_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="original")

    override = acquire_file_lock(str(target), owner_id="agent-b", purpose="takeover", force=True)

    assert override["success"] is True
    assert override["owner_id"] == "agent-b"

    lock = describe_lock(str(target))
    assert lock["owner_id"] == "agent-b"


def test_same_owner_can_reacquire_own_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    first = acquire_file_lock(str(target), owner_id="agent-a", purpose="first")
    second = acquire_file_lock(str(target), owner_id="agent-a", purpose="refresh")

    assert first["success"] is True
    assert second["success"] is True


def test_list_file_locks_returns_empty_when_no_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    result = list_file_locks()

    assert result == []


def test_list_file_locks_returns_active_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    file_a = tmp_path / "A.lean"
    file_b = tmp_path / "B.lean"
    acquire_file_lock(str(file_a), owner_id="agent-a", purpose="edit A")
    acquire_file_lock(str(file_b), owner_id="agent-b", purpose="edit B")

    locks = list_file_locks()

    assert len(locks) == 2
    owners = {lock["owner_id"] for lock in locks}
    assert owners == {"agent-a", "agent-b"}


def test_describe_lock_returns_empty_for_unlocked_file(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Unlocked.lean"

    result = describe_lock(str(target))

    assert result == {}


def test_describe_lock_returns_entry_for_locked_file(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Locked.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="test lock")

    result = describe_lock(str(target))

    assert result["owner_id"] == "agent-a"
    assert result["purpose"] == "test lock"
    assert "expires_at" in result


def test_expired_lock_is_cleaned_up_on_next_acquire(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    acquire_file_lock(str(target), owner_id="agent-a", purpose="will expire", ttl_seconds=60)

    lock_file = tmp_path / "home" / "workflow-state" / "file_locks.json"
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
    normalized = str(target.resolve())
    payload["locks"][normalized]["expires_at"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()
    lock_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    retry = acquire_file_lock(str(target), owner_id="agent-b", purpose="after expiry")

    assert retry["success"] is True
    assert retry["owner_id"] == "agent-b"


def test_ensure_file_lock_idempotent_for_own_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    first = ensure_file_lock(str(target), owner_id="agent-a", purpose="edit")
    second = ensure_file_lock(str(target), owner_id="agent-a", purpose="refresh")

    assert first["success"] is True
    assert second["success"] is True


def test_ensure_file_lock_fails_for_different_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    acquire_file_lock(str(target), owner_id="agent-a", purpose="held")
    result = ensure_file_lock(str(target), owner_id="agent-b")

    assert result["success"] is False
    assert "agent-a" in result["error"]


def test_release_all_file_locks_only_clears_own_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
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
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    result = acquire_file_lock(str(target), owner_id="")

    assert result["success"] is False
    assert "owner_id" in result["error"]
