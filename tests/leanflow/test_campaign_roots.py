"""Native immutable campaign-root setup tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from leanflow_cli.native import campaign_roots
from leanflow_cli.workflows import (
    campaign_epoch,
    campaign_root_registry,
    negation_promotion,
    plan_state,
)


def _fresh_campaign(monkeypatch, tmp_path, *, run_id: str = "root-setup") -> dict:
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", run_id)
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    state: dict = {}
    campaign_epoch.ensure_campaign(state)
    return state


def test_explicit_file_roots_are_materialized_and_sealed_once(monkeypatch, tmp_path):
    source = tmp_path / "A.lean"
    source.write_text(
        "theorem root_a : True := by\n  sorry\n\ndef helper_def : Nat := by\n  sorry\n",
        encoding="utf-8",
    )
    state = _fresh_campaign(monkeypatch, tmp_path)

    setup = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    )

    assert setup.ok is True
    assert setup.registered is True
    assert [root["target_symbol"] for root in setup.roots] == ["root_a"]
    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("root_a", str(source)))
    assert node is not None
    assert node.status == "stated"
    assert node.generated_by == "queue-sync"
    assert negation_promotion.campaign_root_provider_gate()[0] is True

    # A sealed campaign never recomputes its initial source revision on resume.
    source.unlink()
    resumed = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    )
    assert resumed.ok is True
    assert resumed.reason == "requested campaign roots are registered"


def test_project_input_seals_only_files_selected_by_native_scope(monkeypatch, tmp_path):
    first = tmp_path / "A.lean"
    second = tmp_path / "B.lean"
    first.write_text("theorem root_a : True := by\n  sorry\n", encoding="utf-8")
    second.write_text("theorem root_b : True := by\n  sorry\n", encoding="utf-8")
    state = _fresh_campaign(monkeypatch, tmp_path, run_id="scoped-roots")

    files = campaign_roots.source_files_for_scope(
        project_root=tmp_path,
        project_files=(first,),
    )
    setup = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=files,
    )

    assert setup.ok is True
    assert [root["target_symbol"] for root in setup.roots] == ["root_a"]
    registry = plan_state.load_summary()["campaign"][negation_promotion._CAMPAIGN_ROOTS_FIELD]
    assert [root["theorem"] for root in registry["roots"]] == ["root_a"]
    assert (
        plan_state.load_blueprint().node_by_id(plan_state.node_id_for("root_b", str(second)))
        is None
    )


def test_only_def_and_anonymous_example_seal_empty_no_authority(monkeypatch, tmp_path):
    source = tmp_path / "EmptyRoots.lean"
    source.write_text(
        "def unfinished : Nat := by\n  sorry\n\nexample : True := by\n  sorry\n",
        encoding="utf-8",
    )
    state = _fresh_campaign(monkeypatch, tmp_path, run_id="empty-roots")

    setup = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    )

    assert setup.ok is True
    assert setup.roots == ()
    registry = plan_state.load_summary()["campaign"][negation_promotion._CAMPAIGN_ROOTS_FIELD]
    assert registry["roots"] == []
    assert negation_promotion.campaign_root_provider_gate()[0] is True


def test_marker_absent_legacy_campaign_skips_source_enumeration(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "legacy-roots")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)
    monkeypatch.delenv("LEANFLOW_NEGATION_PROBE", raising=False)
    state: dict = {}
    campaign_epoch.ensure_campaign(state)
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")

    setup = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(tmp_path / "does-not-exist.lean",),
    )

    assert setup.ok is True
    assert setup.legacy is True
    assert not plan_state.load_summary()["campaign"].get(negation_promotion._CAMPAIGN_ROOTS_FIELD)


def test_failed_registry_commit_rolls_back_only_nodes_created_by_attempt(monkeypatch, tmp_path):
    source = tmp_path / "Race.lean"
    source.write_text("theorem raced : True := by\n  sorry\n", encoding="utf-8")
    state = _fresh_campaign(monkeypatch, tmp_path, run_id="root-race")
    monkeypatch.setattr(
        negation_promotion,
        "record_requested_campaign_roots",
        lambda *args, **kwargs: negation_promotion.CampaignRootRegistration(
            False, "dependency graph changed before campaign-root registry commit"
        ),
    )

    setup = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    )

    assert setup.ok is False
    assert "dependency graph changed" in setup.reason
    assert plan_state.load_blueprint().nodes == ()
    assert negation_promotion.campaign_root_provider_gate()[0] is False


def test_journal_failure_after_registry_commit_never_rolls_graph_back(monkeypatch, tmp_path):
    source = tmp_path / "Committed.lean"
    source.write_text("theorem committed : True := by\n  sorry\n", encoding="utf-8")
    state = _fresh_campaign(monkeypatch, tmp_path, run_id="root-commit")
    monkeypatch.setattr(
        plan_state,
        "append_journal_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("journal unavailable")),
    )

    setup = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    )

    assert setup.ok is True
    assert (
        plan_state.load_blueprint().node_by_id(plan_state.node_id_for("committed", str(source)))
        is not None
    )
    assert negation_promotion.campaign_root_provider_gate()[0] is True


def test_revision_conflict_is_clean_setup_failure(monkeypatch, tmp_path):
    source = tmp_path / "Conflict.lean"
    source.write_text("theorem conflict : True := by\n  sorry\n", encoding="utf-8")
    state = _fresh_campaign(monkeypatch, tmp_path, run_id="root-conflict")
    monkeypatch.setattr(
        plan_state,
        "save_blueprint",
        lambda _blueprint: (_ for _ in ()).throw(
            plan_state.PlanStateRevisionConflict("concurrent graph writer")
        ),
    )

    setup = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    )

    assert setup.ok is False
    assert "concurrent graph writer" in setup.reason


def test_strict_audit_accepts_registered_root_and_empty_scope(monkeypatch, tmp_path):
    source = tmp_path / "Audit.lean"
    source.write_text("theorem audited : True := by\n  sorry\n", encoding="utf-8")
    state = _fresh_campaign(monkeypatch, tmp_path, run_id="root-audit-valid")
    setup = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    )

    audit = campaign_roots.audit_campaign_root_registry(plan_state.load_summary()["campaign"])

    assert setup.ok is True
    assert (
        campaign_roots.audit_campaign_root_registry
        is campaign_root_registry.audit_campaign_root_registry
    )
    assert audit.ok is True
    assert audit.campaign_id == state["campaign_id"]
    assert [root["theorem"] for root in audit.roots] == ["audited"]


@pytest.mark.parametrize(
    "corruption",
    [
        "non_mapping_root",
        "unknown_root_kind",
        "unknown_registry_field",
        "missing_registry_version",
        "duplicate_root",
    ],
)
def test_corrupt_sealed_registry_is_never_filtered_or_rewritten(
    monkeypatch, tmp_path, corruption: str
):
    source = tmp_path / "Corrupt.lean"
    source.write_text("theorem corrupt : True := by\n  sorry\n", encoding="utf-8")
    state = _fresh_campaign(monkeypatch, tmp_path, run_id=f"root-{corruption}")
    first = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    )
    assert first.ok is True

    def corrupt(summary):
        campaign = dict(summary["campaign"])
        registry = deepcopy(campaign[negation_promotion._CAMPAIGN_ROOTS_FIELD])
        if corruption == "non_mapping_root":
            registry["roots"].append(["opaque-root"])
        elif corruption == "unknown_root_kind":
            registry["roots"][0]["kind"] = "theorem"
        elif corruption == "unknown_registry_field":
            registry["future"] = "authority"
        elif corruption == "missing_registry_version":
            registry.pop("version")
        else:
            registry["roots"].append(deepcopy(registry["roots"][0]))
            registry["registry_sha256"] = negation_promotion._campaign_root_registry_sha256(
                registry["roots"]
            )
        campaign[negation_promotion._CAMPAIGN_ROOTS_FIELD] = registry
        summary["campaign"] = campaign

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, corrupt)
    corrupt_campaign = deepcopy(plan_state.load_summary()["campaign"])

    resumed = campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    )

    assert resumed.ok is False
    assert campaign_roots.audit_campaign_root_registry(corrupt_campaign).ok is False
    assert plan_state.load_summary()["campaign"] == corrupt_campaign


def test_forged_root_identity_and_malformed_nonce_fail_closed(monkeypatch, tmp_path):
    source = tmp_path / "Forged.lean"
    source.write_text("theorem forged : True := by\n  sorry\n", encoding="utf-8")
    state = _fresh_campaign(monkeypatch, tmp_path, run_id="root-forged")
    assert campaign_roots.initialize_campaign_roots(
        campaign_id=state["campaign_id"],
        project_root=tmp_path,
        source_files=(source,),
    ).ok
    campaign = deepcopy(plan_state.load_summary()["campaign"])
    campaign["provider_turn_nonce"] = True
    root = campaign[negation_promotion._CAMPAIGN_ROOTS_FIELD]["roots"][0]
    root["root_identity_sha256"] = "f" * 64
    campaign[negation_promotion._CAMPAIGN_ROOTS_FIELD]["registry_sha256"] = (
        negation_promotion._campaign_root_registry_sha256([root])
    )

    audit = campaign_roots.audit_campaign_root_registry(campaign)

    assert audit.ok is False
    assert "provider" in audit.reason
