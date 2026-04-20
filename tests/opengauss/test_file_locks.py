from __future__ import annotations

import json

from opengauss_cli.file_locks import acquire_file_lock, release_all_file_locks
from tools.file_tools import write_file_tool


def test_acquire_file_lock_blocks_other_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENGAUSS_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"

    first = acquire_file_lock(str(target), owner_id="agent-a", purpose="prove Main.lean")
    second = acquire_file_lock(str(target), owner_id="agent-b", purpose="other proof")

    assert first["success"] is True
    assert second["success"] is False
    assert "agent-a" in second["error"]


def test_write_file_tool_respects_foreign_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENGAUSS_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    result = json.loads(write_file_tool(str(target), "theorem demo : True := by trivial\n", owner_id="agent-b"))

    assert "locked" in result["error"]


def test_release_all_file_locks_clears_owner_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENGAUSS_HOME", str(tmp_path / "home"))
    target = tmp_path / "Main.lean"
    acquire_file_lock(str(target), owner_id="agent-a", purpose="active edit")

    payload = release_all_file_locks(owner_id="agent-a")
    retry = acquire_file_lock(str(target), owner_id="agent-b", purpose="next edit")

    assert payload["count"] == 1
    assert retry["success"] is True
