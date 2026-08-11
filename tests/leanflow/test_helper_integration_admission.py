"""Tests for continuous checked-helper foreground admission."""

from __future__ import annotations

from types import SimpleNamespace

from agent.execution.admission_handoff import (
    clear_initial_foreground_lease,
    replace_initial_foreground_lease,
)
from leanflow_cli.native import helper_integration_admission


class _Lease:
    """Record marker publication and release ordering."""

    def __init__(self, name: str, ordering: list[str]) -> None:
        self.name = name
        self.marker_path = name
        self.expires_at = 1000.0
        self.ordering = ordering
        self.releases = 0

    def release(self) -> bool:
        self.ordering.append(f"release:{self.name}")
        self.releases += 1
        return True


def test_tiny_positive_lease_refreshes_continuously_without_expiry_gap(monkeypatch) -> None:
    """Clamp a sub-tick lease and preserve overlap across repeated refreshes."""
    clock = [0.0]
    leases = []

    class _TimedLease:
        def __init__(self, name: str, expires_at: float) -> None:
            self.marker_path = name
            self.expires_at = expires_at
            self.released = False

        def release(self) -> bool:
            self.released = True
            return True

    class _DormantThread:
        def __init__(self, *, target, name: str, daemon: bool) -> None:
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self) -> None:
            return None

        def join(self, timeout: float | None = None) -> None:
            del timeout

    def reserve(_project_root, seconds, *, reason):
        del reason
        if leases:
            assert any(not lease.released and lease.expires_at > clock[0] for lease in leases)
        lease = _TimedLease(f"marker-{len(leases)}", clock[0] + seconds)
        leases.append(lease)
        return lease

    monkeypatch.setenv("LEANFLOW_HELPER_INTEGRATION_ADMISSION_LEASE_S", "0.001")
    monkeypatch.setattr(
        helper_integration_admission,
        "reserve_project_foreground_priority_lease",
        reserve,
    )
    monkeypatch.setattr(helper_integration_admission.threading, "Thread", _DormantThread)

    reservation = helper_integration_admission.HelperIntegrationAdmissionReservation.start(
        candidate_id="short-lease-helper",
        project_root="/tmp/project",
        reason="continuity regression",
        refresh_interval_s=10.0,
    )

    assert reservation is not None
    assert reservation.lease_seconds == helper_integration_admission._MIN_CONTINUOUS_LEASE_S
    assert 0.0 < reservation.refresh_interval_s < reservation.lease_seconds
    assert reservation.refresh_interval_s <= reservation.lease_seconds / 3.0

    for expected_refresh_count in range(1, 4):
        clock[0] += reservation.refresh_interval_s
        active_before = [
            lease for lease in leases if not lease.released and lease.expires_at > clock[0]
        ]
        assert len(active_before) == 1
        assert reservation.refresh() is True
        active_after = [
            lease for lease in leases if not lease.released and lease.expires_at > clock[0]
        ]
        assert len(active_after) == 1
        assert active_after[0].expires_at > clock[0] + reservation.refresh_interval_s
        assert reservation.refresh_count == expected_refresh_count

    assert reservation.release(reason="test cleanup") is True


def test_zero_lease_environment_still_disables_reservation(monkeypatch) -> None:
    """Preserve zero as the explicit opt-out while clamping positive values."""
    monkeypatch.setenv("LEANFLOW_HELPER_INTEGRATION_ADMISSION_LEASE_S", "0")
    monkeypatch.setattr(
        helper_integration_admission,
        "reserve_project_foreground_priority_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected marker")),
    )

    assert (
        helper_integration_admission.HelperIntegrationAdmissionReservation.start(
            candidate_id="disabled-helper",
            project_root="/tmp/project",
            reason="disabled",
        )
        is None
    )


def test_refresh_publishes_replacement_before_releasing_prior_marker(monkeypatch) -> None:
    """Close the refresh boundary against a background check/acquire race."""
    ordering: list[str] = []
    leases = iter((_Lease("first", ordering), _Lease("second", ordering)))

    def reserve(*_args, **_kwargs):
        lease = next(leases)
        ordering.append(f"reserve:{lease.name}")
        return lease

    monkeypatch.setattr(
        helper_integration_admission,
        "reserve_project_foreground_priority_lease",
        reserve,
    )
    reservation = helper_integration_admission.HelperIntegrationAdmissionReservation.start(
        candidate_id="rhcp-helper",
        project_root="/tmp/project",
        reason="ready helper",
        lease_seconds=60.0,
        refresh_interval_s=3600.0,
    )
    assert reservation is not None
    assert reservation._thread is not None and reservation._thread.daemon

    assert reservation.refresh() is True
    assert ordering[:3] == ["reserve:first", "reserve:second", "release:first"]
    assert reservation.refresh_count == 1
    assert reservation.release(reason="test cleanup") is True
    assert ordering[-1] == "release:second"


def test_candidate_replacement_overlaps_markers_and_exact_cleanup(monkeypatch) -> None:
    """Keep old priority until the new candidate marker is visible."""
    ordering: list[str] = []
    leases = iter((_Lease("old", ordering), _Lease("new", ordering)))

    def reserve(*_args, **_kwargs):
        lease = next(leases)
        ordering.append(f"reserve:{lease.name}")
        return lease

    monkeypatch.setattr(
        helper_integration_admission,
        "reserve_project_foreground_priority_lease",
        reserve,
    )
    monkeypatch.setattr(
        helper_integration_admission,
        "_refresh_interval",
        lambda _seconds: 3600.0,
    )
    agent = SimpleNamespace()

    first = helper_integration_admission.ensure(
        agent,
        candidate_id="rhcp-old",
        project_root="/tmp/project",
        background_workers=2,
        reason="old helper",
    )
    second = helper_integration_admission.ensure(
        agent,
        candidate_id="rhcp-new",
        project_root="/tmp/project",
        background_workers=2,
        reason="new helper",
    )

    assert first is not None and second is not None
    assert ordering[:3] == ["reserve:old", "reserve:new", "release:old"]
    assert (
        helper_integration_admission.release(
            agent,
            reason="wrong candidate",
            expected_candidate_id="rhcp-old",
        )
        is False
    )
    assert helper_integration_admission.current(agent) is second
    assert helper_integration_admission.release(agent, reason="resolved") is True
    assert ordering[-1] == "release:new"


def test_no_matching_candidate_stops_refresher_and_releases_marker(monkeypatch) -> None:
    """Leave no process-local marker after durable candidate retirement."""
    ordering: list[str] = []
    lease = _Lease("active", ordering)
    monkeypatch.setattr(
        helper_integration_admission,
        "reserve_project_foreground_priority_lease",
        lambda *_args, **_kwargs: ordering.append("reserve:active") or lease,
    )
    monkeypatch.setattr(
        helper_integration_admission,
        "_refresh_interval",
        lambda _seconds: 3600.0,
    )
    agent = SimpleNamespace()
    reservation = helper_integration_admission.ensure(
        agent,
        candidate_id="rhcp-helper",
        project_root="/tmp/project",
        background_workers=1,
        reason="ready helper",
    )
    assert reservation is not None and reservation.active

    assert (
        helper_integration_admission.ensure(
            agent,
            candidate_id="",
            project_root="/tmp/project",
            background_workers=1,
            reason="no matching candidate",
        )
        is None
    )

    assert helper_integration_admission.current(agent) is None
    assert reservation.active is False
    assert lease.releases == 1


def test_first_tool_batch_consumes_only_one_shot_lease(monkeypatch) -> None:
    """Preserve helper priority after inspections consume scope-entry priority."""
    ordering: list[str] = []
    helper_lease = _Lease("helper-window", ordering)
    initial_lease = _Lease("scope-entry", ordering)
    monkeypatch.setattr(
        helper_integration_admission,
        "reserve_project_foreground_priority_lease",
        lambda *_args, **_kwargs: helper_lease,
    )
    monkeypatch.setattr(
        helper_integration_admission,
        "_refresh_interval",
        lambda _seconds: 3600.0,
    )
    agent = SimpleNamespace()
    reservation = helper_integration_admission.ensure(
        agent,
        candidate_id="rhcp-helper",
        project_root="/tmp/project",
        background_workers=2,
        reason="ready helper",
    )
    assert reservation is not None
    replace_initial_foreground_lease(agent, initial_lease)

    assert clear_initial_foreground_lease(agent) is True
    assert initial_lease.releases == 1
    assert helper_lease.releases == 0
    assert helper_integration_admission.current(agent) is reservation
    assert reservation.active is True

    helper_integration_admission.release(agent, reason="test cleanup")
