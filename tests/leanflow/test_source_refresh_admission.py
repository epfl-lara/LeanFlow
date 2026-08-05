"""Tests for bounded source refresh after a managed patch anchor miss."""

from __future__ import annotations

from leanflow_cli.native import source_refresh_admission as admission


def test_patch_anchor_miss_reserves_one_active_file_read(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    pending = admission.patch_anchor_miss_reservation(
        function_name="apply_verified_patch",
        args={"path": str(active)},
        payload={
            "status": "patch_failed",
            "patch_applied": False,
            "message": "Could not apply hunk within its @@ anchor region",
        },
        target_symbol="demo",
        active_file=str(active),
        source_revision_sha256="source-sha",
    )

    assert pending == {
        "target_symbol": "demo",
        "active_file": str(active),
        "source_revision_sha256": "source-sha",
        "reason": admission.PATCH_ANCHOR_MISS_REASON,
    }
    assert admission.bounded_patch_anchor_read_matches(
        pending,
        function_name="read_file",
        args={"path": str(active), "offset": 1, "limit": 20},
        active_file=str(active),
    )


def test_non_anchor_patch_failure_does_not_reserve_read(tmp_path):
    active = tmp_path / "Main.lean"

    pending = admission.patch_anchor_miss_reservation(
        function_name="apply_verified_patch",
        args={"path": str(active)},
        payload={
            "status": "patch_failed",
            "patch_applied": False,
            "message": "An identical helper declaration already exists",
        },
        target_symbol="demo",
        active_file=str(active),
        source_revision_sha256="source-sha",
    )

    assert pending is None
