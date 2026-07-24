from leanflow_cli.workflows import research_delivery_gate, research_findings


def test_missing_or_changed_scope_fails_closed_to_one_scan():
    state = {}

    assert research_delivery_gate.scan_required(state, scope="Main.lean::first") is True
    research_delivery_gate.mark_scanned(state, scope="Main.lean::first")
    assert research_delivery_gate.scan_required(state, scope="Main.lean::first") is False

    assert research_delivery_gate.scan_required(state, scope="Main.lean::second") is True


def test_completion_watermark_dirties_once_and_duplicate_stays_clean():
    state = {}
    scope = "Main.lean::demo"
    assert research_delivery_gate.scan_required(state, scope=scope)
    research_delivery_gate.mark_scanned(state, scope=scope)

    research_delivery_gate.mark_published(state, scope=scope, watermark=1)
    assert research_delivery_gate.scan_required(state, scope=scope)
    research_delivery_gate.mark_scanned(state, scope=scope)
    assert not research_delivery_gate.scan_required(state, scope=scope)

    research_delivery_gate.mark_published(state, scope=scope, watermark=1)
    assert not research_delivery_gate.scan_required(state, scope=scope)
    research_delivery_gate.request_scan(state, scope=scope)
    assert research_delivery_gate.scan_required(state, scope=scope)
    research_delivery_gate.mark_scanned(state, scope=scope)
    research_delivery_gate.mark_published(state, scope=scope, watermark=2)
    assert research_delivery_gate.scan_required(state, scope=scope)


def test_malformed_or_old_schema_remains_retryable():
    state = {
        research_delivery_gate.STATE_KEY: {
            "schema_version": 0,
            "scope": "Main.lean::demo",
            "dirty": False,
            "requested_watermark": 100,
            "scanned_watermark": 100,
        }
    }

    assert research_delivery_gate.scan_required(state, scope="Main.lean::demo") is True


def test_v1_clean_watermark_reopens_for_checked_candidate_priority():
    """Upgrade scans the durable suffix that old bounded FIFO could strand."""
    state = {
        research_delivery_gate.STATE_KEY: {
            "schema_version": 1,
            "scope": "242.lean::schinzel_generalization",
            "dirty": False,
            "requested_watermark": 709,
            "scanned_watermark": 709,
        }
    }

    assert (
        research_delivery_gate.scan_required(
            state,
            scope="242.lean::schinzel_generalization",
        )
        is True
    )


def test_direct_foreground_acknowledgement_reopens_current_assignment():
    state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "/tmp/Main.lean",
        }
    }
    scope = research_delivery_gate.current_assignment_scope(state)
    research_delivery_gate.mark_scanned(state, scope=scope)
    marker = research_findings.delivery_key("campaign.ds-001", "demo")
    prompt = research_findings.stage_foreground_delivery(
        state,
        target_symbol="demo",
        markers=[marker],
        prompt="bounded finding",
    )

    acknowledged = research_findings.acknowledge_foreground_deliveries(
        state,
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "Using it."},
        ],
    )

    assert acknowledged == (marker,)
    assert research_delivery_gate.scan_required(state, scope=scope)


def test_intermediate_chunk_acknowledgement_reopens_without_completed_marker():
    state = {
        "campaign_id": "campaign-chunks",
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "/tmp/Main.lean",
        },
    }
    scope = research_delivery_gate.current_assignment_scope(state)
    research_delivery_gate.mark_scanned(state, scope=scope)
    marker = research_findings.delivery_key("campaign.ds-huge", "demo")
    prompt = research_findings.stage_foreground_delivery(
        state,
        target_symbol="demo",
        markers=[marker],
        prompt="large finding\n" + ("proof evidence\n" * 7_000),
    )

    acknowledged = research_findings.acknowledge_foreground_deliveries(
        state,
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "Retained this chunk."},
        ],
    )

    assert acknowledged == ()
    assert research_delivery_gate.scan_required(state, scope=scope)
