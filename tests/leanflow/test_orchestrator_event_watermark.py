"""Regression tests for safe-boundary orchestrator event coalescing."""

from __future__ import annotations

from leanflow_cli.workflows import orchestrator_event_watermark as watermark


def test_only_reviewed_read_and_search_tools_are_safe_boundaries():
    """Read/search callbacks are allowed while mutators and unknown tools fail closed."""
    for function_name in (
        "read_file",
        "search_files",
        "lean_search",
        "lean_inspect",
        "web_search",
        "web_fetch",
        "list_file_locks",
    ):
        assert watermark.is_safe_post_tool_boundary(function_name)

    for function_name in (
        "acquire_file_lock",
        "release_file_lock",
        "lean_worker_dispatch",
        "delegate_task",
        "repo_clone",
        "web_download",
        "apply_verified_patch",
        "lean_incremental_check",
        "lean_verify",
        "patch",
        "terminal",
        "write_file",
        "future_unreviewed_tool",
        "",
    ):
        assert not watermark.is_safe_post_tool_boundary(function_name)


def test_duplicate_sources_coalesce_into_one_monotonic_prefix():
    state: dict[str, object] = {}

    assert (
        watermark.publish_once(
            state,
            scope="Demo.lean::demo",
            source="finding:ds-1",
            reason="deep search done",
        )
        == 1
    )
    assert (
        watermark.publish_once(
            state,
            scope="Demo.lean::demo",
            source="finding:ds-1",
            reason="same ledger result rediscovered",
        )
        == 1
    )
    assert (
        watermark.publish_once(
            state,
            scope="Demo.lean::demo",
            source="finding:em-2",
            reason="empirical search done",
        )
        == 2
    )

    capture = watermark.claim_pending(state, scope="Demo.lean::demo")
    assert capture is not None
    assert capture.watermark == 2
    assert capture.reasons == ("deep search done", "empirical search done")
    assert watermark.claim_pending(state, scope="Demo.lean::demo") is None


def test_acknowledge_advances_only_through_captured_watermark():
    state: dict[str, object] = {}
    scope = "Demo.lean::demo"
    watermark.publish_once(state, scope=scope, source="finding:ds-1", reason="first")
    capture = watermark.claim_pending(state, scope=scope)
    assert capture is not None and capture.watermark == 1

    # This completion races with the consultation. It must remain pending
    # after the consultation acknowledges its earlier atomic snapshot.
    watermark.publish_once(state, scope=scope, source="finding:em-2", reason="second")
    watermark.acknowledge(state, scope=scope, capture=capture)

    assert watermark.has_pending(state, scope=scope)
    next_capture = watermark.claim_pending(state, scope=scope)
    assert next_capture is not None
    assert next_capture.watermark == 2
    assert next_capture.reasons == ("second",)


def test_failed_consult_release_retries_without_republishing():
    state: dict[str, object] = {}
    scope = "Demo.lean::demo"
    assert (
        watermark.publish_once(
            state,
            scope=scope,
            source="finding:ds-1",
            reason="deep search done",
        )
        == 1
    )
    capture = watermark.claim_pending(state, scope=scope)
    assert capture is not None

    watermark.release(state, scope=scope, capture=capture)

    retry = watermark.claim_pending(state, scope=scope)
    assert retry == capture
    assert (
        watermark.publish_once(
            state,
            scope=scope,
            source="finding:ds-1",
            reason="rediscovered",
        )
        == 1
    )


def test_assignment_change_drops_only_the_old_scope_notification_state():
    state: dict[str, object] = {}
    watermark.publish_once(
        state,
        scope="Demo.lean::first",
        source="finding:ds-1",
        reason="old target",
    )

    watermark.synchronize_scope(state, scope="Demo.lean::second")

    assert not watermark.has_pending(state, scope="Demo.lean::second")
    assert (
        watermark.publish_once(
            state,
            scope="Demo.lean::second",
            source="finding:ds-1",
            reason="inherited finding for new target",
        )
        == 1
    )


def test_foreground_grace_is_one_shot_target_scoped_and_releasable():
    """One research boundary reserves the next foreground opportunity."""
    state: dict[str, object] = {}
    first_scope = "Demo.lean::first"

    assert watermark.arm_foreground_grace(state, scope=first_scope) is True
    assert watermark.foreground_grace_active(state, scope=first_scope) is True
    assert watermark.arm_foreground_grace(state, scope=first_scope) is False
    assert watermark.release_foreground_grace(state, scope=first_scope) is True
    assert watermark.foreground_grace_active(state, scope=first_scope) is False
    assert watermark.release_foreground_grace(state, scope=first_scope) is False

    assert watermark.arm_foreground_grace(state, scope=first_scope) is True
    watermark.synchronize_scope(state, scope="Demo.lean::second")
    assert watermark.foreground_grace_active(state, scope="Demo.lean::second") is False
    assert watermark.arm_foreground_grace(state, scope="Demo.lean::second") is True
