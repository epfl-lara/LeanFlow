"""Tests for continuous post-edit verification admission."""

from __future__ import annotations

from types import SimpleNamespace

from leanflow_cli.native import verification_batch_admission


class _Reservation:
    """Record replacement and exact-release behavior for one batch."""

    def __init__(self, candidate_id: str, ordering: list[str]) -> None:
        self.candidate_id = candidate_id
        self.active = True
        self.ordering = ordering

    def release(self, *, reason: str) -> bool:
        self.ordering.append(f"release:{self.candidate_id}:{reason}")
        self.active = False
        return True

    def snapshot(self) -> dict[str, object]:
        return {"candidate_id": self.candidate_id, "released": not self.active}


def test_overlapping_batches_share_marker_until_last_callback(monkeypatch) -> None:
    """Keep one marker continuous regardless of callback completion order."""
    ordering: list[str] = []

    def start(*, candidate_id, **_kwargs):
        reservation = _Reservation(candidate_id, ordering)
        ordering.append(f"start:{reservation.candidate_id}")
        return reservation

    monkeypatch.setattr(
        verification_batch_admission.HelperIntegrationAdmissionReservation,
        "start",
        start,
    )
    monkeypatch.setattr(
        verification_batch_admission.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="batch"),
    )
    agent = SimpleNamespace()

    first = verification_batch_admission.begin(
        agent,
        project_root="/tmp/project",
        background_workers=2,
        expected_invocation_key="first-call",
        reason="first",
    )
    second = verification_batch_admission.begin(
        agent,
        project_root="/tmp/project",
        background_workers=2,
        expected_invocation_key="second-call",
        reason="second",
    )

    assert first is not None and second is not None
    assert first is second
    assert first.pending_batches == 2
    assert ordering == ["start:verified-patch-batch"]

    assert (
        verification_batch_admission.complete_one(
            agent,
            expected_invocation_key="first-call",
            reason="first callback completed",
        )
        is True
    )
    assert first.pending_batches == 1
    assert first.reservation.active is True
    assert ordering == ["start:verified-patch-batch"]

    assert (
        verification_batch_admission.complete_one(
            agent,
            expected_invocation_key="second-call",
            reason="last callback completed",
        )
        is True
    )
    assert verification_batch_admission.current(agent) is None
    assert ordering[-1] == ("release:verified-patch-batch:last callback completed")


def test_shutdown_release_consumes_all_pending_batches(monkeypatch) -> None:
    """Stop the refresher even when callback accounting remains pending."""
    ordering: list[str] = []
    reservation = _Reservation("verified-patch-new", ordering)
    group = verification_batch_admission.VerificationBatchAdmission(
        batch_id="verified-patch-new",
        reservation=reservation,
        pending_invocations={"first": 1, "second": 1},
    )
    agent = SimpleNamespace(
        _native_post_edit_verification_admission_reservation=group,
    )

    assert (
        verification_batch_admission.release(
            agent,
            reason="runner shutdown",
        )
        is True
    )
    assert reservation.active is False
    assert group.pending_batches == 0
    assert verification_batch_admission.current(agent) is None
    assert ordering == ["release:verified-patch-new:runner shutdown"]


def test_unmatched_callback_cannot_consume_another_patch_marker() -> None:
    """Bind completion to the invocation that joined the batch."""
    ordering: list[str] = []
    reservation = _Reservation("verified-patch-target", ordering)
    group = verification_batch_admission.VerificationBatchAdmission(
        batch_id="verified-patch-target",
        reservation=reservation,
        pending_invocations={"target-patch": 1},
    )
    agent = SimpleNamespace(
        _native_post_edit_verification_admission_reservation=group,
    )

    assert (
        verification_batch_admission.complete_one(
            agent,
            expected_invocation_key="support-patch",
            reason="unrelated callback",
        )
        is False
    )
    assert group.pending_batches == 1
    assert reservation.active is True
    assert verification_batch_admission.current(agent) is group

    verification_batch_admission.release(agent, reason="test cleanup")


def test_zero_background_capacity_does_not_publish_marker(monkeypatch) -> None:
    """Skip the transaction marker when no research worker can contend."""
    monkeypatch.setattr(
        verification_batch_admission.HelperIntegrationAdmissionReservation,
        "start",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected marker")),
    )

    assert (
        verification_batch_admission.begin(
            SimpleNamespace(),
            project_root="/tmp/project",
            background_workers=0,
            expected_invocation_key="call",
            reason="no contention",
        )
        is None
    )
