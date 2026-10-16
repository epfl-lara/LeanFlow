"""Bind preparation retries and first author admission to one exact charged campaign."""

from __future__ import annotations

import json
import os

import pytest

from core.campaign_limits import CampaignLimits, LimitReached
from leanflow_cli.formalization import preparation_campaign as campaign

LIMITS = {"agent_calls": 3, "tokens": 100, "wall_time_s": 60}


@pytest.fixture
def source_record():
    return {"document_id": "fixture-document", "source_bundle": "a" * 64}


def _select(root, record, *, run_id="fixture-run", limits=None, resume=False, **changes):
    arguments = {
        "run_id": run_id,
        "limits": LIMITS if limits is None else limits,
        "backend": "codex",
        "selection_token": "fixture-selection",
        "source_draft_only": False,
        "effective_draft": False,
        "resume": resume,
        **changes,
    }
    with campaign.campaign_lock(root, run_id):
        return campaign.select_campaign(root, record, **arguments)


def _budget(root, binding):
    return CampaignLimits(campaign.campaign_budget(root, binding), binding["limits"])


def test_exact_resume_preserves_binding_ledger_bytes_and_charged_call(tmp_path, source_record):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    invocation = budget.reserve()
    budget.started_call(invocation)
    budget.finish(invocation, {"input_tokens": 11, "output_tokens": 7}, started=True)
    original = budget.path.read_bytes()
    resumed = _select(tmp_path, source_record, resume=True)
    assert resumed == binding == campaign.load_campaign(tmp_path, "fixture-run")
    assert budget.path.read_bytes() == original, "Selecting a retry must not rewrite accounting"
    restored = _budget(tmp_path, resumed)
    assert restored.state["invocations"] == {
        invocation: {"status": "finished", "usage": {"input_tokens": 11, "output_tokens": 7}}
    }
    assert restored.elapsed_before >= budget.elapsed_before


def test_fresh_runs_receive_distinct_confined_budgets(tmp_path, source_record):
    first = _select(tmp_path, source_record, run_id="first")
    second = _select(tmp_path, source_record, run_id="second")
    assert first["binding_id"] != second["binding_id"]
    first_path = campaign.campaign_budget(tmp_path, first)
    second_path = campaign.campaign_budget(tmp_path, second)
    assert first_path != second_path
    assert first_path.is_relative_to(tmp_path / ".leanflow/formalization-campaigns")
    assert second_path.is_relative_to(tmp_path / ".leanflow/formalization-campaigns")


def test_equivalent_normalized_limits_resume_the_existing_campaign(tmp_path, source_record):
    first = _select(tmp_path, source_record)
    _budget(tmp_path, first)
    second = _select(
        tmp_path,
        source_record,
        resume=True,
        limits={"agent_calls": "3.0", "tokens": 100.0, "wall_time_s": "60.000"},
    )
    assert second == first
    assert second["limits"] == LIMITS


@pytest.mark.parametrize("name", ["agent_calls", "tokens", "wall_time_s"])
def test_changed_limits_cannot_open_a_new_segment(tmp_path, source_record, name):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    original = budget.path.read_bytes()
    changed = {**LIMITS, name: LIMITS[name] + 1}
    with pytest.raises(ValueError, match="(?i)new campaign.*changing limits"):
        _select(tmp_path, source_record, limits=changed, resume=True)
    assert budget.path.read_bytes() == original
    assert campaign.load_campaign(tmp_path, "fixture-run") == binding


@pytest.mark.parametrize(
    "changes",
    [
        {"backend": "claude"},
        {"selection_token": "different-selection"},
        {"source_draft_only": True},
        {"effective_draft": True},
    ],
)
def test_resume_rejects_changed_campaign_identity_before_ledger_use(
    tmp_path, source_record, changes
):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    original = budget.path.read_bytes()
    with pytest.raises(ValueError):
        _select(tmp_path, source_record, resume=True, **changes)
    assert budget.path.read_bytes() == original


@pytest.mark.parametrize(
    "changes", [{"document_id": "different-document"}, {"source_bundle": "b" * 64}]
)
def test_resume_rejects_different_source_identity(tmp_path, source_record, changes):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    original = budget.path.read_bytes()
    with pytest.raises(ValueError):
        _select(tmp_path, {**source_record, **changes}, resume=True)
    assert budget.path.read_bytes() == original


def test_missing_run_binding_cannot_adopt_another_runs_paid_ledger(tmp_path, source_record):
    first = _select(tmp_path, source_record, run_id="paid-run")
    budget = _budget(tmp_path, first)
    invocation = budget.reserve()
    budget.started_call(invocation)
    budget.finish(invocation, None, started=True)
    original = budget.path.read_bytes()
    with pytest.raises(ValueError, match="(?i)missing campaign accounting.*new campaign"):
        _select(tmp_path, source_record, run_id="unknown-run", resume=True)
    assert budget.path.read_bytes() == original


@pytest.mark.parametrize(
    ("limits", "usage", "message"),
    [
        ({"agent_calls": 1, "tokens": None, "wall_time_s": None}, {}, "agent-call limit"),
        (
            {"agent_calls": 3, "tokens": 18, "wall_time_s": None},
            {"input_tokens": 11, "output_tokens": 7},
            "reported-token limit",
        ),
        (
            {"agent_calls": 3, "tokens": 100, "wall_time_s": None},
            None,
            "Token usage is unavailable",
        ),
    ],
)
def test_resume_retains_exhausted_or_unknown_usage(tmp_path, source_record, limits, usage, message):
    binding = _select(tmp_path, source_record, limits=limits)
    budget = _budget(tmp_path, binding)
    invocation = budget.reserve()
    budget.started_call(invocation)
    budget.finish(invocation, usage, started=True)
    resumed = _select(tmp_path, source_record, limits=limits, resume=True)
    restored = _budget(tmp_path, resumed)
    with pytest.raises(LimitReached, match=message):
        restored.reserve()
    assert list(restored.state["invocations"]) == [invocation]


@pytest.mark.parametrize(
    "relative",
    [
        "outside.json",
        ".leanflow/formalization-campaigns/another/budget.json",
        ".leanflow/native-workflows/other-run/limits.json",
    ],
)
def test_supplied_budget_path_cannot_select_an_arbitrary_ledger(tmp_path, source_record, relative):
    with pytest.raises(ValueError):
        _select(tmp_path, source_record, budget_path=tmp_path / relative)


def test_native_first_admission_can_bind_only_its_exact_limits_path(tmp_path, source_record):
    path = tmp_path / ".leanflow/native-workflows/fixture-run/limits.json"
    binding = _select(tmp_path, source_record, budget_path=path)
    assert campaign.campaign_budget(tmp_path, binding) == path
    _budget(tmp_path, binding)
    assert _select(tmp_path, source_record, resume=True, budget_path=path) == binding


def test_another_runs_valid_same_limits_ledger_cannot_replace_the_bound_budget(
    tmp_path, source_record
):
    first = _select(tmp_path, source_record, run_id="first-run")
    first_budget = _budget(tmp_path, first)
    invocation = first_budget.reserve()
    first_budget.started_call(invocation)
    first_budget.finish(invocation, {"input_tokens": 7}, started=True)
    second = _select(tmp_path, source_record, run_id="second-run")
    second_path = campaign.campaign_budget(tmp_path, second)
    copied = second_path.read_bytes()
    first_budget.path.write_bytes(copied)
    with pytest.raises(ValueError):
        _select(tmp_path, source_record, run_id="first-run", resume=True)
    assert first_budget.path.read_bytes() == copied
    assert second_path.read_bytes() == copied


def test_missing_bound_ledger_is_not_recreated_on_resume(tmp_path, source_record):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    budget.path.unlink()
    with pytest.raises(ValueError):
        _select(tmp_path, source_record, resume=True)
    assert not budget.path.exists()


@pytest.mark.parametrize("contents", ["", "{", "[]", '{"version": 1}'])
def test_corrupt_or_legacy_bound_ledger_is_not_replaced(tmp_path, source_record, contents):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    budget.path.write_text(contents)
    with pytest.raises(ValueError):
        _select(tmp_path, source_record, resume=True)
    assert budget.path.read_text() == contents


def test_resume_preserves_exhausted_active_wall_time(tmp_path, source_record):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    saved = json.loads(budget.path.read_text())
    saved["elapsed_s"] = LIMITS["wall_time_s"]
    budget.path.write_text(json.dumps(saved))
    resumed = _select(tmp_path, source_record, resume=True)
    restored = _budget(tmp_path, resumed)
    with pytest.raises(LimitReached, match="time limit"):
        restored.reserve()
    assert restored.elapsed_before >= LIMITS["wall_time_s"]


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_bound_ledger_alias_is_rejected_before_resume_reads_it(tmp_path, source_record, alias):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    original = budget.path.read_bytes()
    outside = tmp_path / "unowned-ledger.json"
    budget.path.rename(outside)
    if alias == "symlink":
        budget.path.symlink_to(outside)
    else:
        os.link(outside, budget.path)
    with pytest.raises(ValueError):
        _select(tmp_path, source_record, resume=True)
    assert outside.read_bytes() == original


def test_new_campaign_cannot_traverse_a_symlinked_control_directory(tmp_path, source_record):
    outside = tmp_path / "outside"
    outside.mkdir()
    control = tmp_path / ".leanflow"
    control.mkdir()
    (control / "formalization-campaigns").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        _select(tmp_path, source_record)
    assert not list(outside.iterdir())


def test_lost_binding_cannot_fall_back_to_the_surviving_paid_ledger(tmp_path, source_record):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    invocation = budget.reserve()
    budget.started_call(invocation)
    budget.finish(invocation, {"input_tokens": 3}, started=True)
    original = budget.path.read_bytes()
    binding_path = tmp_path / ".leanflow/native-workflows/fixture-run/campaign-accounting.json"
    binding_path.unlink()
    with pytest.raises(ValueError, match="(?i)missing campaign accounting.*new campaign"):
        _select(tmp_path, source_record, resume=True)
    assert not binding_path.exists()
    assert budget.path.read_bytes() == original


@pytest.mark.parametrize("contents", ["{", "[]", '{"schema_version": 0}'])
def test_invalid_binding_is_retained_and_cannot_create_a_replacement(
    tmp_path, source_record, contents
):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    original_ledger = budget.path.read_bytes()
    path = tmp_path / ".leanflow/native-workflows/fixture-run/campaign-accounting.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        _select(tmp_path, source_record, resume=True)
    assert path.read_text() == contents
    assert budget.path.read_bytes() == original_ledger


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("root", "/different-project"),
        ("run_id", "different-run"),
        ("budget_relative", "../outside-ledger.json"),
        ("binding_id", "0" * 64),
    ],
)
def test_changed_persisted_binding_is_rejected_before_accounting_use(
    tmp_path, source_record, name, replacement
):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    original_ledger = budget.path.read_bytes()
    path = tmp_path / ".leanflow/native-workflows/fixture-run/campaign-accounting.json"
    changed = {**binding, name: replacement}
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError):
        campaign.load_campaign(tmp_path, "fixture-run")
    with pytest.raises(ValueError):
        _select(tmp_path, source_record, resume=True)
    assert budget.path.read_bytes() == original_ledger


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_binding_file_alias_is_rejected(tmp_path, source_record, alias):
    _select(tmp_path, source_record)
    path = tmp_path / ".leanflow/native-workflows/fixture-run/campaign-accounting.json"
    original = path.read_bytes()
    outside = tmp_path / "unowned-binding.json"
    path.rename(outside)
    if alias == "symlink":
        path.symlink_to(outside)
    else:
        os.link(outside, path)
    with pytest.raises(ValueError):
        campaign.load_campaign(tmp_path, "fixture-run")
    assert outside.read_bytes() == original


def test_preflight_only_resume_admits_the_first_author_without_a_fake_checkpoint(
    tmp_path, source_record
):
    binding = _select(tmp_path, source_record)
    budget = _budget(tmp_path, binding)
    invocation = budget.reserve()
    budget.started_call(invocation)
    budget.finish(invocation, {"input_tokens": 3}, started=True)
    original_ledger = budget.path.read_bytes()
    directory = tmp_path / ".leanflow/native-workflows/fixture-run"
    state = directory / "state.json"
    with campaign.campaign_lock(tmp_path, "fixture-run"):
        assert campaign.admit_author(tmp_path, binding, resume=True, state_path=state) is False
    assert not state.exists()
    assert json.loads((directory / "author-admitted.json").read_text()) == {
        "schema_version": 1,
        "binding_id": binding["binding_id"],
        "run_id": "fixture-run",
    }
    assert budget.path.read_bytes() == original_ledger


def test_first_author_marker_prevents_another_launch_if_checkpoint_was_lost(
    tmp_path, source_record
):
    binding = _select(tmp_path, source_record)
    state = tmp_path / ".leanflow/native-workflows/fixture-run/state.json"
    with campaign.campaign_lock(tmp_path, "fixture-run"):
        assert campaign.admit_author(tmp_path, binding, resume=False, state_path=state) is False
    assert not state.exists()
    with campaign.campaign_lock(tmp_path, "fixture-run"):
        with pytest.raises(ValueError):
            campaign.admit_author(tmp_path, binding, resume=True, state_path=state)


def test_matching_author_checkpoint_resumes_the_actual_existing_author(tmp_path, source_record):
    binding = _select(tmp_path, source_record)
    state = tmp_path / ".leanflow/native-workflows/fixture-run/state.json"
    with campaign.campaign_lock(tmp_path, "fixture-run"):
        assert campaign.admit_author(tmp_path, binding, resume=False, state_path=state) is False
    state.write_text(
        json.dumps({"run_id": "fixture-run", "campaign_binding_id": binding["binding_id"]})
    )
    with campaign.campaign_lock(tmp_path, "fixture-run"):
        assert campaign.admit_author(tmp_path, binding, resume=True, state_path=state) is True


def test_legacy_checkpoint_without_positive_author_marker_cannot_resume(tmp_path, source_record):
    binding = _select(tmp_path, source_record)
    directory = tmp_path / ".leanflow/native-workflows/fixture-run"
    state = directory / "state.json"
    state.write_text(
        json.dumps({"run_id": "fixture-run", "campaign_binding_id": binding["binding_id"]})
    )
    original = state.read_bytes()
    with campaign.campaign_lock(tmp_path, "fixture-run"):
        with pytest.raises(ValueError):
            campaign.admit_author(tmp_path, binding, resume=True, state_path=state)
    assert not (directory / "author-admitted.json").exists()
    assert state.read_bytes() == original


@pytest.mark.parametrize(
    "changes",
    [{"schema_version": 0}, {"run_id": "other-run"}, {"binding_id": "0" * 64}],
)
def test_mismatched_author_marker_cannot_admit_or_resume_a_process(
    tmp_path, source_record, changes
):
    binding = _select(tmp_path, source_record)
    directory = tmp_path / ".leanflow/native-workflows/fixture-run"
    state = directory / "state.json"
    state.write_text(
        json.dumps({"run_id": "fixture-run", "campaign_binding_id": binding["binding_id"]})
    )
    marker = directory / "author-admitted.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "binding_id": binding["binding_id"],
                "run_id": "fixture-run",
                **changes,
            }
        )
    )
    original = marker.read_bytes()
    with campaign.campaign_lock(tmp_path, "fixture-run"):
        with pytest.raises(ValueError):
            campaign.admit_author(tmp_path, binding, resume=True, state_path=state)
    assert marker.read_bytes() == original


@pytest.mark.parametrize("changes", [{"run_id": "other-run"}, {"campaign_binding_id": "0" * 64}])
def test_author_checkpoint_must_match_the_bound_run(tmp_path, source_record, changes):
    binding = _select(tmp_path, source_record)
    state = tmp_path / ".leanflow/native-workflows/fixture-run/state.json"
    with campaign.campaign_lock(tmp_path, "fixture-run"):
        assert campaign.admit_author(tmp_path, binding, resume=False, state_path=state) is False
    state.write_text(
        json.dumps(
            {"run_id": "fixture-run", "campaign_binding_id": binding["binding_id"], **changes}
        )
    )
    original = state.read_bytes()
    with campaign.campaign_lock(tmp_path, "fixture-run"):
        with pytest.raises(ValueError):
            campaign.admit_author(tmp_path, binding, resume=True, state_path=state)
    assert state.read_bytes() == original
