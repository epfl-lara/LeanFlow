"""Tests for bounded research-delivery activity identifiers."""

from __future__ import annotations

import json

from leanflow_cli.workflows import research_delivery_observability
from leanflow_cli.workflows.research_findings import delivery_key


def test_delivery_activity_details_preserve_normal_receipt_identity() -> None:
    """Keep exact stable identifiers for an ordinary foreground batch."""
    markers = [
        delivery_key("campaign.ds-288", "erdos_242"),
        delivery_key("campaign.em-289", "erdos_242"),
    ]

    details = research_delivery_observability.delivery_activity_details(markers)

    assert details["marker_count"] == 2
    assert details["delivery_job_ids"] == ["campaign.ds-288", "campaign.em-289"]
    assert details["delivery_target_symbols"] == ["erdos_242"]
    assert details["delivery_receipt_markers"] == markers
    assert len(details["delivery_ack_tokens"]) == 2
    assert details["delivery_identifiers_truncated"] is False


def test_delivery_activity_details_bound_pathological_identifiers() -> None:
    """Retain fixed-size correlation data when exact fields cannot stay hot."""
    oversized = "x" * 2_000
    markers = [
        delivery_key(f"{oversized}-{index}", oversized)
        for index in range(research_delivery_observability.DELIVERY_ACTIVITY_IDENTIFIER_CAP + 3)
    ]

    details = research_delivery_observability.delivery_activity_details(markers)
    encoded = json.dumps(details, ensure_ascii=False)

    assert details["marker_count"] == len(markers)
    assert len(details["delivery_ack_tokens"]) == (
        research_delivery_observability.DELIVERY_ACTIVITY_IDENTIFIER_CAP
    )
    assert details["delivery_job_ids"] == []
    assert details["delivery_receipt_markers"] == []
    assert details["delivery_identifiers_omitted"] == 3
    assert details["delivery_identifiers_truncated"] is True
    assert len(details["delivery_identifier_set_sha256"]) == 64
    assert len(encoded) < 4_000
