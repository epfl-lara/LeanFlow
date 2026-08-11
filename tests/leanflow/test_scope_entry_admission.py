"""Tests for provider-to-first-tool foreground Lean priority."""

from __future__ import annotations

from types import SimpleNamespace

from leanflow_cli.native import scope_entry_admission


class _Lease:
    """Record deterministic lease release in focused tests."""

    def __init__(self) -> None:
        self.releases = 0

    def release(self) -> bool:
        self.releases += 1
        return True


def test_scope_entry_lease_is_armed_only_with_background_capacity(monkeypatch, tmp_path):
    """Keep sequential research free of unnecessary priority markers."""
    calls = []
    monkeypatch.setattr(
        scope_entry_admission,
        "reserve_project_foreground_priority_lease",
        lambda *args, **kwargs: calls.append((args, kwargs)) or _Lease(),
    )
    agent = SimpleNamespace()

    assert (
        scope_entry_admission.arm(
            agent,
            project_root=str(tmp_path),
            background_workers=0,
        )
        is None
    )
    assert calls == []


def test_rearming_scope_entry_releases_the_older_unconsumed_lease(monkeypatch, tmp_path):
    """Let a refreshed scope own exactly one process-local reservation."""
    first = _Lease()
    second = _Lease()
    leases = iter((first, second))
    monkeypatch.setattr(
        scope_entry_admission,
        "reserve_project_foreground_priority_lease",
        lambda *args, **kwargs: next(leases),
    )
    agent = SimpleNamespace()

    assert (
        scope_entry_admission.arm(
            agent,
            project_root=str(tmp_path),
            background_workers=2,
        )
        is first
    )
    assert (
        scope_entry_admission.arm(
            agent,
            project_root=str(tmp_path),
            background_workers=2,
        )
        is second
    )

    assert first.releases == 1
    assert second.releases == 0
