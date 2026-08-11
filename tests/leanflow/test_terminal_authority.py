"""Terminal source-and-graph authority lease tests."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from types import SimpleNamespace

from leanflow_cli.native import terminal_authority
from leanflow_cli.workflows import decomposition_provenance, plan_state


def test_terminal_guard_acquires_sorted_sources_before_graph(monkeypatch, tmp_path):
    calls: list[str] = []
    first = tmp_path / "A.lean"
    second = tmp_path / "B.lean"
    first.write_text("theorem a : True := by trivial\n", encoding="utf-8")
    second.write_text("theorem b : True := by trivial\n", encoding="utf-8")

    @contextlib.contextmanager
    def source_operation(path, *, canonical=False):
        assert canonical is True
        calls.append(f"enter-source:{Path(path).name}")
        try:
            yield SimpleNamespace(path=Path(path))
        finally:
            calls.append(f"exit-source:{Path(path).name}")

    @contextlib.contextmanager
    def blueprint_guard():
        calls.append("enter-graph")
        try:
            yield
        finally:
            calls.append("exit-graph")

    monkeypatch.setattr(
        terminal_authority.decomposition_provenance,
        "source_operation",
        source_operation,
    )
    monkeypatch.setattr(
        terminal_authority.file_locks,
        "acquire_namespace_lock",
        lambda path, **kwargs: calls.append(f"enter-runtime:{Path(path).name}")
        or {"success": True},
    )
    monkeypatch.setattr(
        terminal_authority.file_locks,
        "release_namespace_lock",
        lambda path, **kwargs: calls.append(f"exit-runtime:{Path(path).name}") or {"success": True},
    )
    monkeypatch.setattr(
        terminal_authority.decomposition_provenance,
        "read_source_bytes",
        lambda operation: operation.path.read_bytes(),
    )
    monkeypatch.setattr(
        terminal_authority.plan_state,
        "blueprint_commit_guard",
        blueprint_guard,
    )
    monkeypatch.setattr(
        terminal_authority.plan_state,
        "load_blueprint",
        lambda: SimpleNamespace(revision=7),
    )

    with terminal_authority.terminal_authority_guard([second, first, second]) as snapshot:
        assert snapshot.source_paths == (str(first), str(second))
        assert snapshot.source_bytes[str(first)].startswith(b"theorem a")
        assert snapshot.blueprint_revision == 7
        calls.append("commit")

    assert calls == [
        "enter-runtime:A.lean",
        "enter-runtime:B.lean",
        "enter-source:A.lean",
        "enter-source:B.lean",
        "enter-graph",
        "commit",
        "exit-graph",
        "exit-source:B.lean",
        "exit-source:A.lean",
        "exit-runtime:B.lean",
        "exit-runtime:A.lean",
    ]


def test_terminal_guard_blocks_cooperative_source_and_graph_writers(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "state"))
    source = tmp_path / "Main.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    started = {name: threading.Event() for name in ("source", "graph")}
    acquired = {name: threading.Event() for name in ("source", "graph")}

    def source_writer() -> None:
        started["source"].set()
        with decomposition_provenance.source_operation(source, canonical=True):
            acquired["source"].set()

    def graph_writer() -> None:
        started["graph"].set()
        with plan_state.blueprint_commit_guard():
            acquired["graph"].set()

    threads = [
        threading.Thread(target=source_writer, daemon=True),
        threading.Thread(target=graph_writer, daemon=True),
    ]
    with terminal_authority.terminal_authority_guard([source]) as snapshot:
        assert snapshot.source_bytes[str(source)].startswith(b"theorem demo")
        for thread in threads:
            thread.start()
        assert all(event.wait(timeout=1.0) for event in started.values())
        assert acquired["source"].wait(timeout=0.05) is False
        assert acquired["graph"].wait(timeout=0.05) is False

    for thread in threads:
        thread.join(timeout=1.0)
        assert thread.is_alive() is False
    assert all(event.is_set() for event in acquired.values())


def test_terminal_guard_blocks_runtime_file_writer_until_commit(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    source = tmp_path / "Main.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")

    with terminal_authority.terminal_authority_guard([source], runtime_owner_id="terminal-owner"):
        blocked = terminal_authority.file_locks.acquire_file_lock(
            str(source),
            owner_id="writer-owner",
            purpose="late write",
            strict=True,
            force=True,
        )
        assert blocked["success"] is False
        assert blocked["lock"]["owner_id"] == "terminal-owner"
        assert blocked["lock"]["kind"] == "namespace"
        forced_release = terminal_authority.file_locks.release_file_lock(
            str(source), owner_id="writer-owner", force=True, strict=True
        )
        assert forced_release["success"] is False

    acquired = terminal_authority.file_locks.acquire_file_lock(
        str(source), owner_id="writer-owner", purpose="resumed write", strict=True
    )
    assert acquired["success"] is True
    released = terminal_authority.file_locks.release_file_lock(
        str(source), owner_id="writer-owner", strict=True
    )
    assert released["success"] is True


def test_terminal_guard_resolves_symlink_before_runtime_reservation(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    source = tmp_path / "Main.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    alias = tmp_path / "Alias.lean"
    alias.symlink_to(source)

    with terminal_authority.terminal_authority_guard(
        [alias],
        runtime_owner_id="terminal-owner",
    ) as snapshot:
        assert snapshot.source_paths == (str(source.resolve()),)
        blocked = terminal_authority.file_locks.acquire_file_lock(
            str(source),
            owner_id="writer-owner",
            purpose="late real-path write",
            strict=True,
            force=True,
        )
        assert blocked["success"] is False
        assert blocked["lock"]["owner_id"] == "terminal-owner"


def test_terminal_namespace_blocks_late_project_file_reservation(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    late = project / "Late.lean"

    with terminal_authority.terminal_namespace_guard([project], runtime_owner_id="terminal-owner"):
        blocked = terminal_authority.file_locks.acquire_file_lock(
            str(late), owner_id="writer-owner", purpose="late source", strict=True
        )
        assert blocked["success"] is False
        assert blocked["lock"]["kind"] == "namespace"

    acquired = terminal_authority.file_locks.acquire_file_lock(
        str(late), owner_id="writer-owner", purpose="resumed source", strict=True
    )
    assert acquired["success"] is True
    terminal_authority.file_locks.release_file_lock(str(late), owner_id="writer-owner", strict=True)
