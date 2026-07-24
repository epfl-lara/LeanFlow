"""Tests for exact-revision negative resume-gate rejection reuse."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace

import pytest

from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows import resume_gate_rejection_cache as cache
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file


def _enabled_project(monkeypatch, tmp_path):
    """Return an enabled project with a stable fake import environment."""
    project = tmp_path / "Project"
    project.mkdir()
    active = project / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))
    monkeypatch.setattr(
        cache.lean_axiom_batch,
        "import_environment_fingerprint",
        lambda root: "e" * 64,
    )
    return project, active


def _identity(project, active, *, target="demo", allowed=("propext",)):
    """Capture one valid enabled axiom-policy identity."""
    identity = cache.capture_identity(
        active_file=str(active),
        target_symbol=target,
        project_root=project,
        profile_enabled=True,
        allowed_axioms=allowed,
    )
    assert identity is not None
    return identity


def _rejection_payload(active, *, target="demo", blocker="sorryAx"):
    """Return a completed exact check rejected solely by axiom policy."""
    manager_check = {
        "ok": False,
        "mode": "incremental_target",
        "target": target,
        "output": f"axiom guard: {target} depends on {blocker}",
        "has_errors": True,
        "axiom_profile_checked": True,
        "axiom_profile_blockers": [blocker],
        "axiom_violation": [blocker],
        "incremental": {
            "success": True,
            "ok": True,
            "action": "check_target",
            "target": target,
            "file": str(active),
            "has_errors": False,
            "has_sorry": False,
            "timed_out": False,
            "cancelled": False,
        },
    }
    verification = {
        "scope": f"target:{target}",
        "ok": False,
        "tool": "lean_incremental_check",
        "target": target,
        "active_file": str(active),
        "errors": 1,
        "sorry": 0,
        "axiom_profile_checked": True,
        "axiom_profile_blockers": [blocker],
    }
    return manager_check, verification


def test_capture_identity_canonicalizes_and_fingerprints_every_authority(monkeypatch, tmp_path):
    project, active = _enabled_project(monkeypatch, tmp_path)

    identity = cache.capture_identity(
        active_file="Main.lean",
        target_symbol="demo",
        project_root=project / ".",
        profile_enabled=True,
        allowed_axioms=("propext", "Classical.choice", "propext"),
    )

    assert identity is not None
    assert identity.project_root == str(project.resolve())
    assert identity.canonical_active_file == str(active.resolve())
    assert identity.target_symbol == "demo"
    assert identity.source_sha256 == hashlib.sha256(active.read_bytes()).hexdigest()
    assert len(identity.source_sha256) == 64
    assert identity.import_environment_sha256 == "e" * 64
    assert identity.verifier_contract_version == cache.VERIFIER_CONTRACT_VERSION
    assert identity.allowed_axioms == ("Classical.choice", "propext")
    assert identity.axiom_policy_sha256 == cache.axiom_policy_fingerprint(
        profile_enabled=True,
        allowed_axioms=("propext", "Classical.choice"),
    )


def test_axiom_policy_fingerprint_is_order_independent_but_policy_sensitive():
    enabled = cache.axiom_policy_fingerprint(
        profile_enabled=True,
        allowed_axioms=("propext", "Classical.choice"),
    )

    assert enabled == cache.axiom_policy_fingerprint(
        profile_enabled=True,
        allowed_axioms=("Classical.choice", "propext", "propext"),
    )
    assert enabled != cache.axiom_policy_fingerprint(
        profile_enabled=False,
        allowed_axioms=("Classical.choice", "propext"),
    )
    assert enabled != cache.axiom_policy_fingerprint(
        profile_enabled=True,
        allowed_axioms=("Classical.choice", "propext", "sorryAx"),
    )


def test_identity_capture_environment_exception_fails_closed(monkeypatch, tmp_path):
    project, active = _enabled_project(monkeypatch, tmp_path)

    def unavailable(root):
        raise LookupError("import environment unavailable")

    monkeypatch.setattr(
        cache.lean_axiom_batch,
        "import_environment_fingerprint",
        unavailable,
    )

    assert (
        cache.capture_identity(
            active_file=active,
            target_symbol="demo",
            project_root=project,
            profile_enabled=True,
            allowed_axioms=("propext",),
        )
        is None
    )


def test_completed_rejection_is_durable_exact_negative_authority(monkeypatch, tmp_path):
    project, active = _enabled_project(monkeypatch, tmp_path)
    identity = _identity(project, active)
    manager_check, verification = _rejection_payload(active)

    recorded = cache.remember_completed_rejection(
        identity,
        manager_check=manager_check,
        verification=verification,
    )

    assert recorded is not None
    assert recorded.blocker_axioms == ("sorryAx",)
    assert cache.matching_rejection(identity) == recorded
    persisted = read_json_file(plan_state.plan_state_paths().summary_json)
    raw = persisted[cache.SUMMARY_KEY][0]
    assert raw["negative_authority_only"] is True
    assert raw["rejection_kind"] == "disallowed_axioms"
    assert "accepted" not in raw
    assert "proved" not in raw


def test_every_identity_change_forces_a_cache_miss(monkeypatch, tmp_path):
    project, active = _enabled_project(monkeypatch, tmp_path)
    identity = _identity(project, active)
    manager_check, verification = _rejection_payload(active)
    assert (
        cache.remember_completed_rejection(
            identity,
            manager_check=manager_check,
            verification=verification,
        )
        is not None
    )
    summary = read_json_file(plan_state.plan_state_paths().summary_json)
    changed_policy = ("Classical.choice", "propext")
    mismatches = (
        replace(identity, project_root=str((tmp_path / "Other").resolve())),
        replace(identity, canonical_active_file=str((project / "Other.lean").resolve())),
        replace(identity, target_symbol="other"),
        replace(identity, source_sha256="a" * 64),
        replace(identity, import_environment_sha256="b" * 64),
        replace(identity, verifier_contract_version="resume-exact-target-axiom-policy-v2"),
        replace(
            identity,
            allowed_axioms=changed_policy,
            axiom_policy_sha256=cache.axiom_policy_fingerprint(
                profile_enabled=True,
                allowed_axioms=changed_policy,
            ),
        ),
        replace(
            identity,
            axiom_profile_enabled=False,
            axiom_policy_sha256=cache.axiom_policy_fingerprint(
                profile_enabled=False,
                allowed_axioms=identity.allowed_axioms,
            ),
        ),
    )

    assert all(item.valid for item in mismatches)
    assert all(cache.matching_rejection(item, summary) is None for item in mismatches)


@pytest.mark.parametrize(
    "unsafe_kind",
    [
        "exception",
        "timeout",
        "cancelled",
        "retryable",
        "profile_unavailable",
        "profile_timeout",
        "target_mismatch",
        "nested_target_mismatch",
        "file_mismatch",
        "profile_incomplete",
        "clean_profile",
        "kernel_error",
        "local_sorry",
    ],
)
def test_unsafe_or_nonmathematical_results_are_never_cached(
    monkeypatch,
    tmp_path,
    unsafe_kind,
):
    project, active = _enabled_project(monkeypatch, tmp_path)
    identity = _identity(project, active)
    manager_check, verification = _rejection_payload(active)
    checked = copy.deepcopy(manager_check)
    record = copy.deepcopy(verification)
    incremental = checked["incremental"]

    if unsafe_kind == "exception":
        checked["error"] = "axiom inspection raised an exception"
    elif unsafe_kind == "timeout":
        incremental["timed_out"] = True
    elif unsafe_kind == "cancelled":
        incremental["cancelled"] = True
    elif unsafe_kind == "retryable":
        incremental["retryable"] = True
    elif unsafe_kind == "profile_unavailable":
        checked["axiom_profile_blockers"] = ["axiom-profile-unavailable"]
        checked["axiom_violation"] = ["axiom-profile-unavailable"]
        record["axiom_profile_blockers"] = ["axiom-profile-unavailable"]
    elif unsafe_kind == "profile_timeout":
        checked["axiom_profile_blockers"] = ["axiom-profile-timeout"]
        checked["axiom_violation"] = ["axiom-profile-timeout"]
        record["axiom_profile_blockers"] = ["axiom-profile-timeout"]
    elif unsafe_kind == "target_mismatch":
        checked["target"] = "other"
        incremental["target"] = "other"
    elif unsafe_kind == "nested_target_mismatch":
        incremental["target"] = "other"
    elif unsafe_kind == "file_mismatch":
        incremental["file"] = str(project / "Other.lean")
    elif unsafe_kind == "profile_incomplete":
        checked["axiom_profile_checked"] = False
    elif unsafe_kind == "clean_profile":
        checked["axiom_profile_blockers"] = []
        checked["axiom_violation"] = []
        record["axiom_profile_blockers"] = []
    elif unsafe_kind == "kernel_error":
        incremental["ok"] = False
        incremental["has_errors"] = True
    else:
        incremental["has_sorry"] = True

    assert (
        cache.remember_completed_rejection(
            identity,
            manager_check=checked,
            verification=record,
        )
        is None
    )
    summary = read_json_file(plan_state.plan_state_paths().summary_json)
    assert cache.SUMMARY_KEY not in summary


def test_allowlisted_blocker_is_not_a_policy_rejection(monkeypatch, tmp_path):
    project, active = _enabled_project(monkeypatch, tmp_path)
    identity = _identity(project, active, allowed=("propext", "sorryAx"))
    manager_check, verification = _rejection_payload(active)

    assert (
        cache.remember_completed_rejection(
            identity,
            manager_check=manager_check,
            verification=verification,
        )
        is None
    )


def test_source_race_and_environment_change_are_not_persisted(monkeypatch, tmp_path):
    project, active = _enabled_project(monkeypatch, tmp_path)
    manager_check, verification = _rejection_payload(active)
    source_identity = _identity(project, active)
    active.write_text("theorem demo : True := by\n  exact True.intro\n", encoding="utf-8")

    assert (
        cache.remember_completed_rejection(
            source_identity,
            manager_check=manager_check,
            verification=verification,
        )
        is None
    )

    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    fingerprints = iter(("a" * 64, "b" * 64))
    monkeypatch.setattr(
        cache.lean_axiom_batch,
        "import_environment_fingerprint",
        lambda root: next(fingerprints),
    )
    environment_identity = _identity(project, active)
    assert (
        cache.remember_completed_rejection(
            environment_identity,
            manager_check=manager_check,
            verification=verification,
        )
        is None
    )
    assert cache.SUMMARY_KEY not in read_json_file(plan_state.plan_state_paths().summary_json)


def test_records_are_bounded_globally_and_per_target(monkeypatch, tmp_path):
    project, active = _enabled_project(monkeypatch, tmp_path)

    for index in range(cache.GLOBAL_RECORD_CAP + 5):
        target = f"demo_{index}"
        identity = _identity(project, active, target=target)
        manager_check, verification = _rejection_payload(active, target=target)
        assert (
            cache.remember_completed_rejection(
                identity,
                manager_check=manager_check,
                verification=verification,
            )
            is not None
        )
    summary = read_json_file(plan_state.plan_state_paths().summary_json)
    assert len(summary[cache.SUMMARY_KEY]) == cache.GLOBAL_RECORD_CAP

    for index in range(cache.PER_TARGET_RECORD_CAP + 3):
        active.write_text(
            f"theorem repeated : True := by\n  trivial -- revision {index}\n",
            encoding="utf-8",
        )
        identity = _identity(project, active, target="repeated")
        manager_check, verification = _rejection_payload(active, target="repeated")
        assert (
            cache.remember_completed_rejection(
                identity,
                manager_check=manager_check,
                verification=verification,
            )
            is not None
        )
    records = read_json_file(plan_state.plan_state_paths().summary_json)[cache.SUMMARY_KEY]
    repeated = [item for item in records if item["identity"]["target_symbol"] == "repeated"]
    assert len(records) <= cache.GLOBAL_RECORD_CAP
    assert len(repeated) == cache.PER_TARGET_RECORD_CAP


def test_plan_state_merge_cannot_regress_cache_owned_summary_key(monkeypatch, tmp_path):
    _enabled_project(monkeypatch, tmp_path)
    stale = plan_state.load_summary()
    marker = [{"negative_authority_only": True}]
    update_json_file(
        plan_state.plan_state_paths().summary_json,
        lambda summary: summary.update({cache.SUMMARY_KEY: marker}),
    )

    plan_state.save_summary(stale)

    assert read_json_file(plan_state.plan_state_paths().summary_json)[cache.SUMMARY_KEY] == marker
