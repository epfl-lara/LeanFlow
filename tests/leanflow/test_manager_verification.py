"""Tests for the extracted manager_verification helpers + native_runner re-export (Phase 2).

These cover the cleanly-pure, closed-under-"calls" subset of the manager-verification /
verification-decision cluster moved out of native_runner: the last-verification record reader and
its tri-state outcome, the deterministic-check timeout readers, the manager retry key builder, the
auxiliary-override probe, and the advisory-review system-prompt selector. The re-export-identity
test pins every moved name to the same object on ``native_runner`` so existing callers (the many
verification helpers still living there) resolve them without a back-import.
"""

import os

from leanflow_cli.native import native_runner
from leanflow_cli.workflows import manager_verification as mv
from leanflow_cli.workflows.verification_providers import (
    AUTOFORMALIZER_VERIFICATION_TASK,
    BLUEPRINT_VERIFICATION_TASK,
)


def test_native_runner_reexports_are_identical():
    # Every re-exported name in native_runner must be the SAME object as in
    # manager_verification, so callers still living in native_runner resolve the extracted
    # helpers without a back-import.
    for name in mv.__all__:
        assert getattr(native_runner, name) is getattr(mv, name), name


def test_verification_outcome_tristate():
    # No record and no verification_ok flag -> unverified.
    assert mv._verification_outcome({}, {}) == "unverified"
    assert mv._verification_outcome(None, None) == "unverified"
    # A recorded last_verification drives the ok/failed result.
    ok_state = {"last_verification": {"ok": True}}
    assert mv._verification_outcome(ok_state, {}) == "ok"
    assert mv._verification_outcome({"last_verification": {"ok": False}}, {}) == "failed"
    # Falling back to the verification_ok flag when there is no record.
    assert mv._verification_outcome({}, {"verification_ok": True}) == "ok"
    assert mv._verification_outcome({}, {"verification_ok": False}) == "failed"


def test_last_verification_record_prefers_autonomy_then_live():
    autonomy = {"last_verification": {"ok": True, "scope": "file"}}
    live = {"last_verification": {"ok": False, "scope": "target"}}
    # Autonomy state wins when it carries a record.
    assert mv._last_verification_record(autonomy, live).get("ok") is True
    # Falls back to live_state when autonomy has nothing.
    assert mv._last_verification_record({}, live).get("ok") is False
    # Nothing recorded anywhere -> empty mapping.
    assert mv._last_verification_record({}, {}) == {}


def test_manager_feedback_retry_key_is_target_and_resolved_file():
    key = mv._manager_feedback_retry_key("Foo.bar", "/tmp/some/file.lean")
    assert key.endswith("::Foo.bar")
    assert "file.lean" in key
    # Empty file yields a leading separator; target is still appended.
    assert mv._manager_feedback_retry_key(" Foo ", "") == "::Foo"


def test_incremental_timeout_readers_honor_env(monkeypatch):
    monkeypatch.delenv("LEANFLOW_MANAGER_INCREMENTAL_PREPARE_TIMEOUT_S", raising=False)
    monkeypatch.delenv("LEANFLOW_MANAGER_INCREMENTAL_CHECK_TIMEOUT_S", raising=False)
    assert (
        mv._manager_incremental_prepare_timeout_s()
        == mv.MANAGER_INCREMENTAL_PREPARE_TIMEOUT_DEFAULT_S
    )
    assert (
        mv._manager_incremental_check_timeout_s() == mv.MANAGER_INCREMENTAL_CHECK_TIMEOUT_DEFAULT_S
    )
    monkeypatch.setenv("LEANFLOW_MANAGER_INCREMENTAL_PREPARE_TIMEOUT_S", "123")
    monkeypatch.setenv("LEANFLOW_MANAGER_INCREMENTAL_CHECK_TIMEOUT_S", "456")
    assert mv._manager_incremental_prepare_timeout_s() == 123
    assert mv._manager_incremental_check_timeout_s() == 456


def test_incremental_prepare_blocking_declaration_parses_named_prerequisite():
    error = (
        "failed to build env before target at no_witness_ten_sixty_one: "
        "line 335:47 error: maximum number of heartbeats has been reached"
    )
    assert mv._incremental_prepare_blocking_declaration(error) == "no_witness_ten_sixty_one"
    assert mv._incremental_prepare_blocking_declaration("provider unavailable") == ""


def test_verification_review_system_prompt_branches_on_task():
    blueprint = mv._verification_review_system_prompt(BLUEPRINT_VERIFICATION_TASK)
    autoform = mv._verification_review_system_prompt(AUTOFORMALIZER_VERIFICATION_TASK)
    assert "statement/source verifier" in blueprint
    assert "autoformalization verifier" in autoform
    assert blueprint != autoform


def test_verification_task_has_aux_overrides_reads_env(monkeypatch):
    task = "PROVE"
    for suffix in ("MODEL", "BASE_URL", "API_KEY", "REASONING_EFFORT"):
        monkeypatch.delenv(f"AUXILIARY_{task}_{suffix}", raising=False)
    # With no env override and no matching config, there is no override.
    assert mv._verification_task_has_aux_overrides(task) in (True, False)
    monkeypatch.setenv(f"AUXILIARY_{task}_MODEL", "gpt-test")
    assert mv._verification_task_has_aux_overrides(task) is True
    os.environ.pop(f"AUXILIARY_{task}_MODEL", None)
