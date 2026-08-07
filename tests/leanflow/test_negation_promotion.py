"""Authoritative negation-promotion gate tests."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace

import pytest

from leanflow_cli.lean import negation_probe
from leanflow_cli.workflows import (
    decomposition_provenance,
    false_decomposition_cleanup,
    negation_promotion,
    plan_state,
)
from leanflow_cli.workflows.plan_state import Blueprint, GraphEdge, GraphNode


def test_source_candidate_failure_classifier_is_an_explicit_whitelist():
    definitive = {
        negation_promotion.SOURCE_CANDIDATE_DECLARATION_MISSING,
        negation_promotion.SOURCE_CANDIDATE_KERNEL_INCOMPATIBLE,
        negation_promotion.SOURCE_CANDIDATE_AXIOMS_UNACCEPTABLE,
    }
    for failure_kind in definitive:
        assert negation_promotion.source_candidate_definitively_incompatible(
            negation_promotion.PromotionResult(
                False,
                "definitive candidate rejection",
                failure_kind=failure_kind,
            )
        )
    for failure_kind in (
        "source_unavailable",
        "source_lease_changed",
        "source_goal_reconstruction_unavailable",
        "source_state_elaboration_uncertain",
        "graph_transaction_changed",
    ):
        assert not negation_promotion.source_candidate_definitively_incompatible(
            negation_promotion.PromotionResult(
                False,
                "scope or runtime uncertainty",
                failure_kind=failure_kind,
            )
        )
    assert not negation_promotion.source_candidate_definitively_incompatible(
        negation_promotion.PromotionResult(
            False,
            "retryable even with a candidate-shaped kind",
            failure_kind=negation_promotion.SOURCE_CANDIDATE_KERNEL_INCOMPATIBLE,
            retryable=True,
        )
    )


def _seed_campaign(monkeypatch, tmp_path, *, target: str, file_label: str) -> None:
    """Record one immutable requested root before any simulated provider turn."""

    def seed(summary):
        summary["campaign"] = {
            "campaign_id": "test-campaign",
            "provider_turn_nonce": 0,
            "status": "running",
            negation_promotion._CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD: True,
        }

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, seed)
    registered = negation_promotion.record_requested_campaign_roots(
        [{"target_symbol": target, "active_file": file_label}],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )
    assert registered.ok, registered.reason


def _remove_campaign_roots() -> None:
    """Simulate a legacy campaign that predates immutable root registration."""

    def mutate(summary):
        campaign = dict(summary.get("campaign") or {})
        campaign.pop(negation_promotion._CAMPAIGN_ROOTS_FIELD, None)
        summary["campaign"] = campaign

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, mutate)


def _reopen_campaign_root_registration() -> None:
    """Reset a test campaign to its pre-provider registration boundary."""

    def mutate(summary):
        campaign = dict(summary.get("campaign") or {})
        campaign.pop(negation_promotion._CAMPAIGN_ROOTS_FIELD, None)
        campaign[negation_promotion._CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD] = True
        campaign["provider_turn_nonce"] = 0
        summary["campaign"] = campaign

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, mutate)


def _setup(monkeypatch, tmp_path, *, sublemma: bool = False):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    (tmp_path / ".leanflow").mkdir(exist_ok=True)
    (tmp_path / ".leanflow" / "project.yaml").write_text("name: demo\n", encoding="utf-8")
    source = tmp_path / "Demo.lean"
    source.write_text(
        "import Mathlib\n\ntheorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n",
        encoding="utf-8",
    )
    node_id = plan_state.node_id_for("bad", "Demo.lean")
    node = GraphNode(
        id=node_id,
        name="bad",
        file="Demo.lean",
        status="proving",
        generated_by="queue-sync",
    )
    anchor_id = plan_state.node_id_for("bad_root_anchor", "Demo.lean")
    anchor = GraphNode(
        id=anchor_id,
        kind="def",
        name="bad_root_anchor",
        file="Demo.lean",
        status="proved",
        generated_by="human",
    )
    root_edge = GraphEdge(source=node_id, target=anchor_id, kind="depends_on")
    if sublemma:
        parent = GraphNode(id="main", name="main", file="Demo.lean", status="split")
        blueprint = Blueprint(
            nodes=(parent, node, anchor),
            edges=(
                root_edge,
                GraphEdge(source=node_id, target="main", kind="split_of"),
            ),
        )
    else:
        blueprint = Blueprint(nodes=(node, anchor), edges=(root_edge,))
    plan_state.save_blueprint(blueprint)
    if not sublemma:
        _seed_campaign(monkeypatch, tmp_path, target="bad", file_label="Demo.lean")
    goal = negation_probe.build_negation_goal(str(source), "bad", cwd=str(tmp_path))
    assert isinstance(goal, negation_probe.NegationGoal)
    entry = {
        "key": "bad::Demo.lean",
        "theorem": "bad",
        "file": "Demo.lean",
        "negation": {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms": [],
            "axioms_ok": True,
        },
        "promotion_evidence": {
            "declaration_signature_sha256": hashlib.sha256(
                goal.original.encode("utf-8")
            ).hexdigest(),
            "source_revision_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "negation_name": goal.name,
            "negation_prop": goal.prop,
            "original_signature": goal.original,
            "proof_tactic": "decide",
        },
    }
    return source, node_id, entry


def _install_successful_source_check(monkeypatch, calls: list[str]) -> None:
    """Install an exact-source check that records every generated harness."""

    def exact_project_check(code, **_kwargs):
        calls.append(code)
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "ok": True,
            "output": f"'{alias.group(1)}' does not depend on any axioms",
            "messages": [],
        }

    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        exact_project_check,
    )


def _setup_tmp_alias_project(monkeypatch, tmp_path):
    """Create a portable stand-in for macOS /tmp -> /private/tmp."""
    canonical_tmp = tmp_path / "private" / "tmp"
    canonical_project = canonical_tmp / "project"
    canonical_project.mkdir(parents=True)
    tmp_alias = tmp_path / "tmp"
    tmp_alias.symlink_to(canonical_tmp, target_is_directory=True)
    alias_project = tmp_alias / "project"
    source = canonical_project / "Demo.lean"
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n\n"
        "private lemma not_bad : ¬ (∀ n : Nat, n < 5) := by\n"
        "  intro h\n"
        "  have := h 5\n"
        "  omega\n",
        encoding="utf-8",
    )
    (canonical_project / ".leanflow").mkdir()
    (canonical_project / ".leanflow" / "project.yaml").write_text("name: demo\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(canonical_project))
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    node_id = plan_state.node_id_for("bad", str(source))
    anchor_id = plan_state.node_id_for("bad_root_anchor", str(source))
    plan_state.save_blueprint(
        Blueprint(
            nodes=(
                GraphNode(
                    id=node_id,
                    name="bad",
                    file=str(source),
                    status="proving",
                    generated_by="queue-sync",
                ),
                GraphNode(
                    id=anchor_id,
                    kind="def",
                    name="bad_root_anchor",
                    file=str(source),
                    status="proved",
                    generated_by="human",
                ),
            ),
            edges=(GraphEdge(source=node_id, target=anchor_id, kind="depends_on"),),
        )
    )
    _remove_campaign_roots()
    _seed_campaign(monkeypatch, canonical_project, target="bad", file_label=str(source))
    return canonical_project, alias_project, source, node_id


def test_fresh_standard_negation_promotes_main_goal(monkeypatch, tmp_path):
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms": ["Classical.choice"],
            "axioms_ok": True,
        },
    )

    result = negation_promotion.promote_negation(
        entry,
        cwd=str(tmp_path),
        requested_target_symbol="bad",
        requested_active_file="Demo.lean",
    )

    assert result.ok is True
    assert result.is_main_goal is True
    assert plan_state.load_blueprint().node_by_id(node_id).status == "false"
    promotions = plan_state.load_summary()["negation_promotions"]
    assert promotions[-1]["node_id"] == node_id
    assert promotions[-1]["classification_basis"] == "requested_scope_manifest"
    assert promotions[-1]["scope_root_campaign_id"] == "test-campaign"
    assert promotions[-1]["scope_root_identity_sha256"]
    assert promotions[-1]["scope_root_theorem"] == "bad"
    assert promotions[-1]["scope_root_file"] == "Demo.lean"
    assert promotions[-1]["scope_root_node_id"] == node_id


def test_campaign_root_registration_refuses_legacy_campaign_without_nonce(monkeypatch, tmp_path):
    """A legacy campaign can never snapshot its current helper-laden queue as roots."""
    _source, _node_id, _entry = _setup(monkeypatch, tmp_path)
    _reopen_campaign_root_registration()

    def make_legacy(summary):
        campaign = dict(summary.get("campaign") or {})
        campaign.pop("provider_turn_nonce", None)
        summary["campaign"] = campaign

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, make_legacy)

    result = negation_promotion.record_requested_campaign_roots(
        [{"target_symbol": "bad", "active_file": "Demo.lean"}],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert "legacy campaign" in result.reason
    assert negation_promotion._CAMPAIGN_ROOTS_FIELD not in plan_state.load_summary()["campaign"]


@pytest.mark.parametrize("invalid_nonce", [None, "", False, "0", -1, 0.0])
def test_campaign_root_registration_requires_exact_fresh_integer_zero_nonce(
    monkeypatch,
    tmp_path,
    invalid_nonce,
):
    """Falsy and coercible nonce values cannot counterfeit pre-provider scope entry."""
    _setup(monkeypatch, tmp_path)
    _reopen_campaign_root_registration()

    def corrupt_nonce(summary):
        campaign = dict(summary.get("campaign") or {})
        campaign["provider_turn_nonce"] = invalid_nonce
        summary["campaign"] = campaign

    negation_promotion.update_json_file(
        plan_state.plan_state_paths().summary_json,
        corrupt_nonce,
    )

    result = negation_promotion.record_requested_campaign_roots(
        [{"target_symbol": "bad", "active_file": "Demo.lean"}],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert negation_promotion._CAMPAIGN_ROOTS_FIELD not in plan_state.load_summary()["campaign"]


@pytest.mark.parametrize("invalid_nonce", [None, "", False, "0", -1, 0.0])
def test_sealed_campaign_root_registry_rejects_malformed_provider_nonce(
    monkeypatch,
    tmp_path,
    invalid_nonce,
):
    """A valid registry hash cannot launder malformed provider provenance."""
    _setup(monkeypatch, tmp_path)

    def corrupt_nonce(summary):
        campaign = dict(summary.get("campaign") or {})
        campaign["provider_turn_nonce"] = invalid_nonce
        summary["campaign"] = campaign

    negation_promotion.update_json_file(
        plan_state.plan_state_paths().summary_json,
        corrupt_nonce,
    )

    ready, reason = negation_promotion.campaign_root_provider_gate()

    assert ready is False
    assert "provider provenance" in reason


def test_campaign_root_provider_gate_distinguishes_fresh_and_legacy_state(monkeypatch, tmp_path):
    """Fresh incomplete scope blocks providers while legacy campaigns keep running."""
    _setup(monkeypatch, tmp_path)
    assert negation_promotion.campaign_root_provider_gate()[0] is True

    _reopen_campaign_root_registration()
    ready, reason = negation_promotion.campaign_root_provider_gate()
    assert ready is False
    assert "not registered" in reason

    def make_legacy(summary):
        campaign = dict(summary.get("campaign") or {})
        campaign.pop(negation_promotion._CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD, None)
        summary["campaign"] = campaign

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, make_legacy)
    ready, reason = negation_promotion.campaign_root_provider_gate()
    assert ready is True
    assert "legacy" in reason


def test_campaign_root_registration_can_seal_authenticated_empty_scope(monkeypatch, tmp_path):
    """A scope with no negatable named theorem must not deadlock providers."""
    _setup(monkeypatch, tmp_path)
    _reopen_campaign_root_registration()

    result = negation_promotion.record_requested_campaign_roots(
        [],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )

    assert result.ok is True
    assert result.roots == ()
    campaign = plan_state.load_summary()["campaign"]
    registry = campaign[negation_promotion._CAMPAIGN_ROOTS_FIELD]
    assert registry["roots"] == []
    assert registry["registry_sha256"]
    assert campaign[negation_promotion._CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD] is False
    assert negation_promotion.campaign_root_provider_gate(campaign)[0] is True


def test_sealed_registry_without_new_campaign_nonce_has_no_root_authority(monkeypatch, tmp_path):
    """A self-consistent registry cannot retrofit authority onto legacy state."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)

    def remove_fresh_origin(summary):
        campaign = dict(summary.get("campaign") or {})
        campaign.pop("provider_turn_nonce", None)
        summary["campaign"] = campaign

    negation_promotion.update_json_file(
        plan_state.plan_state_paths().summary_json, remove_fresh_origin
    )
    ready, reason = negation_promotion.campaign_root_provider_gate()
    assert ready is False
    assert "provider provenance" in reason
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    result = negation_promotion.promote_negation(entry, cwd=str(tmp_path))

    assert result.ok is False
    assert result.is_main_goal is False
    assert "provider provenance" in result.reason
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"


def test_file_level_queue_label_cannot_be_recorded_as_a_theorem_root(monkeypatch, tmp_path):
    """Project/file scopes must expand declarations before closing the root gate."""
    _setup(monkeypatch, tmp_path)
    _reopen_campaign_root_registration()

    result = negation_promotion.record_requested_campaign_roots(
        [{"target_symbol": "Demo.lean", "active_file": "Demo.lean"}],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert "declaration is unavailable" in result.reason
    assert negation_promotion.campaign_root_provider_gate()[0] is False


def test_campaign_root_registration_supports_two_roots_in_one_file(monkeypatch, tmp_path):
    """Deterministic same-file leases remain reentrant and preserve both roots."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path)
    source.write_text(
        source.read_text(encoding="utf-8") + "\ntheorem other_root : True := by\n  sorry\n",
        encoding="utf-8",
    )
    blueprint = plan_state.load_blueprint()
    other_id = plan_state.node_id_for("other_root", "Demo.lean")
    plan_state.save_blueprint(
        blueprint.replace_node(
            GraphNode(
                id=other_id,
                name="other_root",
                file="Demo.lean",
                status="proving",
                generated_by="queue-sync",
            )
        )
    )
    _reopen_campaign_root_registration()
    source_operation = decomposition_provenance.source_operation
    leased_paths = []

    def count_source_lease(path, **kwargs):
        leased_paths.append(str(path))
        return source_operation(path, **kwargs)

    monkeypatch.setattr(decomposition_provenance, "source_operation", count_source_lease)

    result = negation_promotion.record_requested_campaign_roots(
        [
            {"target_symbol": "other_root", "active_file": "Demo.lean"},
            {"target_symbol": "bad", "active_file": "Demo.lean"},
        ],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )

    assert result.ok is True
    assert [root["theorem"] for root in result.roots] == ["bad", "other_root"]
    assert leased_paths == [str(source)]


def test_campaign_root_registration_rejects_semantic_alias_duplicate(monkeypatch, tmp_path):
    """Relative and absolute spellings cannot duplicate one mathematical root."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path)
    _reopen_campaign_root_registration()

    result = negation_promotion.record_requested_campaign_roots(
        [
            {"target_symbol": "bad", "active_file": "Demo.lean"},
            {"target_symbol": "bad", "active_file": str(source)},
        ],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert "duplicate" in result.reason


def test_campaign_root_registration_leases_canonical_files_in_stable_order(monkeypatch, tmp_path):
    """Alias spelling cannot invert multi-file lease order across processes."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path)
    other = tmp_path / "Other.lean"
    other.write_text("theorem other_root : True := by\n  sorry\n", encoding="utf-8")
    blueprint = plan_state.load_blueprint()
    other_id = plan_state.node_id_for("other_root", "Other.lean")
    plan_state.save_blueprint(
        blueprint.replace_node(
            GraphNode(
                id=other_id,
                name="other_root",
                file="Other.lean",
                status="proving",
                generated_by="queue-sync",
            )
        )
    )
    alias_dir = tmp_path / "aliases"
    alias_dir.mkdir()
    demo_alias = alias_dir / "z_demo.lean"
    other_alias = alias_dir / "a_other.lean"
    demo_alias.symlink_to(source)
    other_alias.symlink_to(other)
    _reopen_campaign_root_registration()
    source_operation = decomposition_provenance.source_operation
    leased: list[tuple[str, bool]] = []

    def record_source_lease(path, **kwargs):
        leased.append((str(path), bool(kwargs.get("canonical"))))
        return source_operation(path, **kwargs)

    monkeypatch.setattr(decomposition_provenance, "source_operation", record_source_lease)

    result = negation_promotion.record_requested_campaign_roots(
        [
            {"target_symbol": "bad", "active_file": str(demo_alias)},
            {"target_symbol": "other_root", "active_file": str(other_alias)},
        ],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )

    assert result.ok is True
    expected_paths = sorted((str(source.resolve()), str(other.resolve())))
    assert leased == [(path, True) for path in expected_paths]


def test_campaign_root_registration_detects_graph_drift_before_commit(monkeypatch, tmp_path):
    """A graph race cannot publish root authority into the campaign summary."""
    _source, _node_id, _entry = _setup(monkeypatch, tmp_path)
    _reopen_campaign_root_registration()
    blueprint = plan_state.load_blueprint()
    drifted = replace(blueprint, revision=blueprint.revision + 1)
    reads = iter((blueprint, drifted))
    monkeypatch.setattr(negation_promotion.plan_state, "load_blueprint", lambda: next(reads))

    result = negation_promotion.record_requested_campaign_roots(
        [{"target_symbol": "bad", "active_file": "Demo.lean"}],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert "dependency graph changed" in result.reason
    assert negation_promotion._CAMPAIGN_ROOTS_FIELD not in plan_state.load_summary()["campaign"]


def test_idempotent_root_registration_source_race_preserves_existing_registry(
    monkeypatch, tmp_path
):
    """A failed idempotent audit never deletes root authority it did not create."""
    _source, _node_id, _entry = _setup(monkeypatch, tmp_path)
    before = dict(plan_state.load_summary()["campaign"])
    original_assert = negation_promotion._assert_source_unchanged

    def fail_before_commit(operation, expected, *, stage):
        if stage == "immediately before campaign-root registry commit":
            raise negation_promotion._SourceLeaseChanged("injected source race")
        original_assert(operation, expected, stage=stage)

    monkeypatch.setattr(negation_promotion, "_assert_source_unchanged", fail_before_commit)

    result = negation_promotion.record_requested_campaign_roots(
        [{"target_symbol": "bad", "active_file": "Demo.lean"}],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert plan_state.load_summary()["campaign"] == before


def test_planner_prerequisite_without_split_edge_cannot_become_main_goal(monkeypatch, tmp_path):
    """Planner ownership plus a root mismatch is ambiguity, not terminal falsity."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    root_id = plan_state.node_id_for("campaign_root", "Demo.lean")
    blueprint = plan_state.load_blueprint()
    prerequisite = replace(blueprint.node_by_id(node_id), generated_by="planner")
    root = GraphNode(
        id=root_id,
        name="campaign_root",
        file="Demo.lean",
        status="proving",
        generated_by="queue-sync",
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(root, prerequisite),
            edges=(GraphEdge(source=root_id, target=node_id, kind="depends_on"),),
            revision=blueprint.revision,
        )
    )
    _remove_campaign_roots()
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    result = negation_promotion.promote_negation(
        entry,
        cwd=str(tmp_path),
        requested_target_symbol="bad",
        requested_active_file="Demo.lean",
    )

    assert result.ok is False
    assert "requested campaign root registry" in result.reason
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    assert plan_state.load_summary().get("negation_promotions", []) == []


def test_current_assignment_matching_unknown_helper_is_not_root_authority(monkeypatch, tmp_path):
    """The current queue target cannot launder missing helper metadata into main truth."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    root_id = plan_state.node_id_for("campaign_root", "Demo.lean")
    blueprint = plan_state.load_blueprint()
    unknown_helper = replace(blueprint.node_by_id(node_id), generated_by="queue-sync")
    root = GraphNode(
        id=root_id,
        name="campaign_root",
        file="Demo.lean",
        status="proving",
        generated_by="queue-sync",
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(root, unknown_helper),
            edges=(GraphEdge(source=root_id, target=node_id, kind="depends_on"),),
            revision=blueprint.revision,
        )
    )
    _remove_campaign_roots()
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    result = negation_promotion.promote_negation(
        entry,
        cwd=str(tmp_path),
        requested_target_symbol="bad",
        requested_active_file="Demo.lean",
    )

    assert result.ok is False
    assert "requested campaign root registry" in result.reason
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    assert plan_state.load_summary().get("negation_promotions", []) == []


def test_isolated_queue_sync_assignment_is_not_campaign_root_authority(monkeypatch, tmp_path):
    """Queue synchronization alone cannot authorize terminal disproof."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    blueprint = plan_state.load_blueprint()
    current = blueprint.node_by_id(node_id)
    plan_state.save_blueprint(Blueprint(nodes=(current,), revision=blueprint.revision))
    _remove_campaign_roots()
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    result = negation_promotion.promote_negation(
        entry,
        cwd=str(tmp_path),
        requested_target_symbol="bad",
        requested_active_file="Demo.lean",
    )

    assert result.ok is False
    assert "requested campaign root registry" in result.reason
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"


def test_startup_never_rehydrates_planner_prerequisite_as_campaign_disproof(monkeypatch, tmp_path):
    """A legacy main bit on a non-root planner node cannot stop the resumed root."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    promoted = negation_promotion.promote_negation(
        entry,
        cwd=str(tmp_path),
        requested_target_symbol="bad",
        requested_active_file="Demo.lean",
    )
    assert promoted.ok and promoted.is_main_goal

    root_id = plan_state.node_id_for("campaign_root", "Demo.lean")
    blueprint = plan_state.load_blueprint()
    prerequisite = replace(blueprint.node_by_id(node_id), generated_by="planner")
    root = GraphNode(
        id=root_id,
        name="campaign_root",
        file="Demo.lean",
        status="proving",
        generated_by="queue-sync",
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(root, prerequisite),
            edges=(GraphEdge(source=root_id, target=node_id, kind="depends_on"),),
            revision=blueprint.revision,
        )
    )
    _remove_campaign_roots()

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path),
        target_symbol="campaign_root",
        active_file="Demo.lean",
    )

    assert reconciled.terminal_disproof is False
    assert reconciled.promotion is None


def test_startup_current_unknown_assignment_cannot_rehydrate_legacy_main_bit(monkeypatch, tmp_path):
    """Resume reclassifies the current target from durable graph ownership."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    promoted = negation_promotion.promote_negation(entry, cwd=str(tmp_path))
    assert promoted.ok and promoted.is_main_goal

    root_id = plan_state.node_id_for("campaign_root", "Demo.lean")
    blueprint = plan_state.load_blueprint()
    unknown_helper = replace(blueprint.node_by_id(node_id), generated_by="queue-sync")
    root = GraphNode(
        id=root_id,
        name="campaign_root",
        file="Demo.lean",
        status="proving",
        generated_by="queue-sync",
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(root, unknown_helper),
            edges=(GraphEdge(source=root_id, target=node_id, kind="depends_on"),),
            revision=blueprint.revision,
        )
    )
    _remove_campaign_roots()

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path),
        target_symbol="bad",
        active_file="Demo.lean",
    )

    assert reconciled.terminal_disproof is False
    assert reconciled.promotion is None
    assert reconciled.quarantined == 1


def test_stale_source_revision_refuses_promotion(monkeypatch, tmp_path):
    source, node_id, entry = _setup(monkeypatch, tmp_path)
    source.write_text(source.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")

    result = negation_promotion.promote_negation(entry, cwd=str(tmp_path))

    assert result.ok is False
    assert "source revision changed" in result.reason
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"


def test_nonstandard_axiom_rerun_refuses_promotion(monkeypatch, tmp_path):
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": ["sorryAx"],
            "axioms_ok": True,
        },
    )

    result = negation_promotion.promote_negation(entry, cwd=str(tmp_path))

    assert result.ok is False
    assert "non-standard axioms" in result.reason
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"


def test_promoted_sublemma_invalidates_its_decomposition(monkeypatch, tmp_path):
    _source, node_id, entry = _setup(monkeypatch, tmp_path, sublemma=True)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    result = negation_promotion.promote_negation(entry, cwd=str(tmp_path))

    blueprint = plan_state.load_blueprint()
    assert result.ok is True
    assert result.is_main_goal is False
    assert blueprint.node_by_id(node_id).status == "false"
    assert blueprint.node_by_id("main").status == "conjectured"


def test_source_negation_promotes_sublemma_and_invalidates_decomposition(monkeypatch, tmp_path):
    source, node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n\n"
        "private lemma not_bad : ¬ (∀ n : Nat, n < 5) := by\n"
        "  intro h\n"
        "  have := h 5\n"
        "  omega\n",
        encoding="utf-8",
    )

    def scratch(code, **_kwargs):
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "ok": False,
            "messages": [
                {
                    "severity": "warning",
                    "message": (
                        f"'Demo.{alias.group(1)}' depends on axioms: "
                        "[propext, Classical.choice, Quot.sound]"
                    ),
                }
            ],
        }

    monkeypatch.setattr(negation_promotion, "lean_ephemeral_source_check", scratch)

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
    )

    blueprint = plan_state.load_blueprint()
    assert result.ok is True
    assert result.is_main_goal is False
    assert blueprint.node_by_id(node_id).status == "false"
    assert blueprint.node_by_id("main").status == "conjectured"
    assert result.evidence["proof_declaration"] == "not_bad"
    assert result.evidence["promotion_kind"] == "source_negation"
    assert negation_promotion.revalidate_promotion(result.evidence, cwd=str(tmp_path)).ok


@pytest.mark.parametrize(
    ("dependent_type", "proof"),
    (
        (
            "    let P : Prop := ∀ n : Nat, n < 5\n    ¬ P",
            "by\n  intro h\n  have := h 5\n  omega",
        ),
        (
            "    let P : Prop := ∀ n : Nat, n < 5\n    ¬ P",
            "fun h => by\n  have := h 5\n  omega",
        ),
        (
            "    have witness : True := True.intro\n    ¬ (∀ n : Nat, n < 5)",
            "by\n  intro h\n  have := h 5\n  omega",
        ),
        (
            "    have witness : True := True.intro\n    ¬ (∀ n : Nat, n < 5)",
            "fun h => by\n  have := h 5\n  omega",
        ),
    ),
    ids=("let-block", "let-term", "have-block", "have-term"),
)
def test_source_negation_accepts_dependent_statement_assignments(
    monkeypatch, tmp_path, dependent_type, proof
):
    """Type-level let/have assignments must not be mistaken for the proof body."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n\n"
        f"private lemma not_bad :\n{dependent_type} := {proof}\n",
        encoding="utf-8",
    )
    harnesses: list[str] = []
    _install_successful_source_check(monkeypatch, harnesses)

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
    )

    assert result.ok is True
    assert negation_promotion.revalidate_promotion(result.evidence, cwd=str(tmp_path)).ok
    assert len(harnesses) == 2


@pytest.mark.parametrize(
    ("preamble", "candidate_type"),
    (
        ("", "(∀ n : Nat, n < 5) → False"),
        ("abbrev Refutes (P : Prop) := P → False\n\n", "Refutes (∀ n : Nat, n < 5)"),
    ),
    ids=("arrow-false", "reducible-alias"),
)
def test_source_negation_leaves_proposition_equivalence_to_lean(
    monkeypatch, tmp_path, preamble, candidate_type
):
    """Equivalent negation spellings reach the exact Lean harness and revalidation."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        "import Mathlib\n\n"
        f"{preamble}"
        "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n\n"
        f"private lemma refutes_bad : {candidate_type} := by\n"
        "  intro h\n"
        "  have := h 5\n"
        "  omega\n",
        encoding="utf-8",
    )
    harnesses: list[str] = []
    _install_successful_source_check(monkeypatch, harnesses)

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="refutes_bad",
        cwd=str(tmp_path),
    )

    assert result.ok is True
    assert negation_promotion.revalidate_promotion(result.evidence, cwd=str(tmp_path)).ok
    assert len(harnesses) == 2


def test_source_negation_does_not_treat_placeholder_tokens_as_textual_authority(
    monkeypatch, tmp_path
):
    """Comments, strings, quotations, and escaped names cannot reject a proof."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n\n"
        "private lemma not_bad : ¬ (∀ n : Nat, n < 5) := by\n"
        '  let text := "sorry admit sorryAx"\n'
        "  let quotedName : Lean.Name := ``sorry\n"
        "  have «sorry» : True := by trivial\n"
        "  -- sorry\n"
        "  /- admit and sorryAx are data here -/\n"
        "  intro h\n"
        "  have := h 5\n"
        "  omega\n",
        encoding="utf-8",
    )
    harnesses: list[str] = []
    _install_successful_source_check(monkeypatch, harnesses)

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
    )

    assert result.ok is True
    assert len(harnesses) == 1


@pytest.mark.parametrize("placeholder", ("sorry", "admit"))
def test_source_negation_rejects_actual_placeholder_via_axiom_audit(
    monkeypatch, tmp_path, placeholder
):
    """A real placeholder is rejected by Lean's printed axioms, not raw text."""
    source, node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n\n"
        f"private lemma not_bad : ¬ (∀ n : Nat, n < 5) := by\n  {placeholder}\n",
        encoding="utf-8",
    )
    harnesses: list[str] = []

    def sorry_axiom_check(code, **_kwargs):
        harnesses.append(code)
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "ok": True,
            "output": f"'{alias.group(1)}' depends on axioms: [sorryAx]",
            "messages": [],
        }

    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        sorry_axiom_check,
    )

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert result.failure_kind == negation_promotion.SOURCE_CANDIDATE_AXIOMS_UNACCEPTABLE
    assert negation_promotion.source_candidate_definitively_incompatible(result) is True
    assert len(harnesses) == 1
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"


def test_source_negation_rejects_expected_revision_mismatch_before_lean(monkeypatch, tmp_path):
    """A lease-bound A/B mismatch is retryable and never reaches candidate Lean."""
    source, node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\nprivate lemma not_bad : ¬ (∀ n : Nat, n < 5) := by omega\n",
        encoding="utf-8",
    )
    initial_blueprint = plan_state.load_blueprint()
    monkeypatch.setattr(
        negation_promotion.negation_probe,
        "build_negation_goal",
        lambda *_args, **_kwargs: pytest.fail("revision mismatch reached goal reconstruction"),
    )
    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        lambda *_args, **_kwargs: pytest.fail("revision mismatch reached Lean"),
    )

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
        expected_source_revision_sha256="0" * 64,
    )

    assert result.ok is False
    assert result.retryable is True
    assert result.failure_kind == "source_revision_changed_before_candidate_check"
    assert negation_promotion.source_candidate_definitively_incompatible(result) is False
    assert plan_state.load_blueprint() == initial_blueprint
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"


def test_source_negation_batch_classifies_four_failures_with_one_lean_check(monkeypatch, tmp_path):
    """Failed aliases share one compile while retaining exact local diagnostics."""
    source = tmp_path / "Demo.lean"
    candidates = tuple(f"candidate_{index}" for index in range(4))
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n\n"
        + "".join(f"private lemma {name} : True := by trivial\n" for name in candidates),
        encoding="utf-8",
    )
    checked_sources: list[str] = []

    def exact_batch_check(code, **_kwargs):
        checked_sources.append(code)
        aliases = re.findall(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert len(aliases) == 4
        lines = code.splitlines()
        output: list[str] = []
        for alias in aliases:
            theorem_line = next(
                index
                for index, line in enumerate(lines, start=1)
                if line.startswith(f"theorem {alias}")
            )
            output.append(
                f"/tmp/leanflow-source-check.lean:{theorem_line + 1}:3: "
                "error: application type mismatch"
            )
            # Lean recovers failed theorem commands with sorryAx and continues.
            output.append(f"'{alias}' depends on axioms: [sorryAx]")
        return {
            "success": False,
            "retryable": False,
            "failure_kind": "lean_elaboration",
            "command": ["lake", "env", "lean", "/tmp/leanflow-source-check.lean"],
            "output": "\n".join(output),
            "messages": [],
        }

    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        exact_batch_check,
    )

    verdicts = negation_promotion.preflight_source_negation_candidates(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declarations=candidates,
        cwd=str(tmp_path),
        expected_source_revision_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )

    assert len(checked_sources) == 1
    assert [verdict.proof_declaration for verdict in verdicts] == list(candidates)
    assert all(
        verdict.failure_kind == negation_promotion.SOURCE_CANDIDATE_KERNEL_INCOMPATIBLE
        for verdict in verdicts
    )
    assert all(
        negation_promotion.source_candidate_definitively_incompatible(
            negation_promotion.PromotionResult(
                False,
                verdict.reason,
                failure_kind=verdict.failure_kind,
                retryable=verdict.retryable,
            )
        )
        for verdict in verdicts
    )


def test_source_negation_batch_returns_compatible_only_as_preflight_evidence(monkeypatch, tmp_path):
    """A sibling success is selected for, but never performs, graph promotion."""
    source = tmp_path / "Demo.lean"
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n\n"
        "private lemma unrelated : True := by trivial\n"
        "private lemma not_bad : ¬ (∀ n : Nat, n < 5) := by\n"
        "  intro h\n  have := h 5\n  omega\n",
        encoding="utf-8",
    )

    def mixed_batch_check(code, **_kwargs):
        aliases = re.findall(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert len(aliases) == 2
        lines = code.splitlines()
        failed_line = next(
            index
            for index, line in enumerate(lines, start=1)
            if line.startswith(f"theorem {aliases[0]}")
        )
        return {
            "success": False,
            "retryable": False,
            "failure_kind": "lean_elaboration",
            "command": ["lake", "env", "lean", "/tmp/check.lean"],
            "output": (
                f"/tmp/check.lean:{failed_line + 1}:3: error: type mismatch\n"
                f"'{aliases[0]}' depends on axioms: [sorryAx]\n"
                f"'{aliases[1]}' does not depend on any axioms"
            ),
            "messages": [],
        }

    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        mixed_batch_check,
    )
    initial_blueprint = plan_state.load_blueprint()

    verdicts = negation_promotion.preflight_source_negation_candidates(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declarations=("unrelated", "not_bad"),
        cwd=str(tmp_path),
    )

    assert verdicts[0].disposition == "incompatible"
    assert verdicts[1].disposition == "compatible"
    assert verdicts[1].axioms == ()
    assert plan_state.load_blueprint() == initial_blueprint


def test_source_negation_batch_revision_mismatch_never_reaches_lean(monkeypatch, tmp_path):
    source = tmp_path / "Demo.lean"
    source.write_text(
        "theorem bad : True := by sorry\nlemma candidate : True := by trivial\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        lambda *_args, **_kwargs: pytest.fail("revision mismatch reached Lean"),
    )

    verdict = negation_promotion.preflight_source_negation_candidates(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declarations=("candidate",),
        cwd=str(tmp_path),
        expected_source_revision_sha256="0" * 64,
    )[0]

    assert verdict.disposition == "uncertain"
    assert verdict.retryable is True
    assert verdict.failure_kind == "source_revision_changed_before_candidate_check"


def test_source_negation_batch_source_change_after_check_is_retryable(monkeypatch, tmp_path):
    source = tmp_path / "Demo.lean"
    source.write_text(
        "theorem bad : True := by sorry\nlemma candidate : True := by trivial\n",
        encoding="utf-8",
    )

    def mutate_source(code, **_kwargs):
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        source.write_text(source.read_text(encoding="utf-8") + "\n-- raced\n", encoding="utf-8")
        return {
            "success": True,
            "retryable": False,
            "failure_kind": "",
            "output": f"'{alias.group(1)}' does not depend on any axioms",
            "messages": [],
        }

    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        mutate_source,
    )

    verdict = negation_promotion.preflight_source_negation_candidates(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declarations=("candidate",),
        cwd=str(tmp_path),
    )[0]

    assert verdict.disposition == "uncertain"
    assert verdict.retryable is True
    assert verdict.failure_kind == "source_lease_changed"


def test_source_negation_batch_rejects_duplicate_resolved_declaration_identity(
    monkeypatch, tmp_path
):
    """Qualified spellings cannot batch against the same short-name region."""
    source = tmp_path / "Demo.lean"
    source.write_text(
        "theorem bad : True := by sorry\nlemma candidate : True := by trivial\n",
        encoding="utf-8",
    )
    region = {
        "kind": "lemma",
        "name": "candidate",
        "line": 2,
        "end_line": 2,
        "text": "lemma candidate : True := by trivial",
    }
    monkeypatch.setattr(
        negation_promotion,
        "_exact_source_declaration_region",
        lambda *_args, **_kwargs: dict(region),
    )
    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        lambda *_args, **_kwargs: pytest.fail("ambiguous identities reached Lean"),
    )

    verdicts = negation_promotion.preflight_source_negation_candidates(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declarations=("Foo.candidate", "Bar.candidate"),
        cwd=str(tmp_path),
    )

    assert len(verdicts) == 2
    assert all(verdict.disposition == "uncertain" for verdict in verdicts)
    assert all(
        verdict.failure_kind == "source_candidate_declaration_ambiguous" for verdict in verdicts
    )


def test_source_negation_promotes_universal_target_from_specialized_counterexample(
    monkeypatch, tmp_path
):
    """Bridge a checked finite counterexample into the exact universal negation."""
    source, node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad : \u2200 n : Nat, n < 5 := by\n  sorry\n\n"
        "private lemma not_bad_at_five : \u00ac (5 < 5) := by\n"
        "  omega\n",
        encoding="utf-8",
    )
    harnesses: list[str] = []

    def exact_project_check(code, **_kwargs):
        harnesses.append(code)
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        bridge = (
            "intro leanflowTarget\n"
            "  apply not_bad_at_five\n"
            "  first\n"
            "  | exact leanflowTarget\n"
            "  | apply leanflowTarget"
        )
        if bridge not in code:
            return {
                "success": False,
                "failure_kind": "lean_elaboration",
                "messages": [
                    {
                        "severity": "error",
                        "message": (
                            "type mismatch: not_bad_at_five has type \u00ac (5 < 5) "
                            "but is expected to have type \u00ac (\u2200 n : Nat, n < 5)"
                        ),
                    }
                ],
            }
        return {
            "success": True,
            "ok": True,
            "output": f"'{alias.group(1)}' does not depend on any axioms",
            "messages": [],
        }

    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        exact_project_check,
    )

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad_at_five",
        cwd=str(tmp_path),
    )

    assert result.ok is True
    assert result.evidence["proof_tactic"] == (
        "intro leanflowTarget\n"
        "apply not_bad_at_five\n"
        "first\n"
        "| exact leanflowTarget\n"
        "| apply leanflowTarget"
    )
    assert negation_promotion.revalidate_promotion(result.evidence, cwd=str(tmp_path)).ok
    assert len(harnesses) == 2
    assert plan_state.load_blueprint().node_by_id(node_id).status == "false"


@pytest.mark.parametrize(
    ("error_location", "expected_definitive"),
    (("harness", True), ("source", False)),
)
def test_source_negation_caches_only_harness_local_elaboration_failure(
    monkeypatch, tmp_path, error_location, expected_definitive
):
    """A whole-source error outside the alias cannot reject the candidate."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\nprivate lemma not_bad : ¬ (∀ n : Nat, n < 5) := by omega\n",
        encoding="utf-8",
    )

    def exact_project_check(code, **_kwargs):
        lines = code.splitlines()
        alias_line = next(
            index
            for index, line in enumerate(lines, start=1)
            if line.startswith("theorem leanflowNegationPromotion_")
        )
        error_line = alias_line + 2 if error_location == "harness" else 1
        return {
            "success": False,
            "retryable": False,
            "failure_kind": "lean_elaboration",
            "output": (
                f"/tmp/leanflow-source-check.lean:{error_line}:3: error: "
                "type mismatch while applying source helper\n"
            ),
            "messages": [],
        }

    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        exact_project_check,
    )

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert negation_promotion.source_candidate_definitively_incompatible(result) is (
        expected_definitive
    )
    if expected_definitive:
        assert result.failure_kind == (negation_promotion.SOURCE_CANDIDATE_KERNEL_INCOMPATIBLE)
        assert result.retryable is False
    else:
        assert result.failure_kind == "lean_elaboration"
        assert result.retryable is True


def test_truncated_harness_diagnostics_cannot_continue_candidate_scan():
    """Unseen diagnostics prevent classifying an elaboration failure as local."""
    payload = {
        "success": False,
        "retryable": True,
        "failure_kind": "lean_elaboration",
        "output_truncated": True,
        "output": "/tmp/check.lean:12:3: error: harness mismatch",
        "messages": [],
    }

    assert (
        negation_promotion._failure_allows_candidate_scan_continuation(
            payload,
            start_line=10,
            end_line=15,
        )
        is False
    )


@pytest.mark.parametrize(
    ("diagnostics", "truncated", "expected"),
    (
        ("", False, True),
        ("/tmp/check.lean:12:3: error: harness timeout detail", False, True),
        ("/tmp/check.lean:25:3: error: source failure", False, False),
        ("error: unlocated source failure", False, False),
        ("", True, False),
    ),
    ids=("none", "harness-only", "outside-harness", "unlocated", "truncated"),
)
def test_timeout_continuation_requires_clean_harness_local_diagnostics(
    diagnostics, truncated, expected
):
    """Known global or incomplete timeout evidence must stop candidate fanout."""
    payload = {
        "success": False,
        "retryable": True,
        "failure_kind": "infrastructure_timeout",
        "output_truncated": truncated,
        "output": diagnostics,
        "messages": [],
    }

    assert (
        negation_promotion._failure_allows_candidate_scan_continuation(
            payload,
            start_line=10,
            end_line=15,
        )
        is expected
    )


def test_source_negation_harness_retains_let_bound_target_body(monkeypatch, tmp_path):
    """Never mistake a target result's first let binding for its proof body."""
    source, node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad (t : Nat) :\n"
        "    let n := 840 * t + 361\n"
        "    let x := 210 * t + 91\n"
        "    n < x := by\n"
        "  sorry\n\n"
        "private lemma not_bad_at_one :\n"
        "    ¬ (let n := 840 * 1 + 361\n"
        "       let x := 210 * 1 + 91\n"
        "       n < x) := by\n"
        "  norm_num\n",
        encoding="utf-8",
    )
    harnesses: list[str] = []

    def exact_project_check(code, **_kwargs):
        harnesses.append(code)
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        if "∀ (t : Nat), let n := 840 * t + 361" not in code:
            return {
                "success": False,
                "failure_kind": "lean_elaboration",
                "messages": [
                    {
                        "severity": "error",
                        "message": "unexpected token ')'; expected ':=' or '|'",
                    }
                ],
            }
        return {
            "success": True,
            "ok": True,
            "output": f"'{alias.group(1)}' does not depend on any axioms",
            "messages": [],
        }

    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        exact_project_check,
    )

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad_at_one",
        cwd=str(tmp_path),
    )

    assert result.ok is True
    assert negation_promotion.revalidate_promotion(result.evidence, cwd=str(tmp_path)).ok
    assert len(harnesses) == 2
    assert plan_state.load_blueprint().node_by_id(node_id).status == "false"


def test_source_negation_promotion_and_revalidation_use_cold_full_module_floor(
    monkeypatch, tmp_path
):
    """Both authoritative source paths outlive the generic scratch deadline."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\nprivate lemma not_bad : ¬ (∀ n : Nat, n < 5) := by omega\n",
        encoding="utf-8",
    )
    original = source.read_bytes()
    timeouts: list[int] = []

    def exact_project_check(code, *, cwd, timeout_s):
        assert cwd == str(tmp_path)
        assert source.read_bytes() == original
        timeouts.append(timeout_s)
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "ok": True,
            "output": f"'{alias.group(1)}' does not depend on any axioms",
            "messages": [],
        }

    monkeypatch.delenv("LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S", raising=False)
    monkeypatch.setattr(negation_probe, "probe_timeout_s", lambda: 120)
    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        exact_project_check,
    )

    promoted = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
    )
    assert promoted.ok is True
    assert negation_promotion.revalidate_promotion(promoted.evidence, cwd=str(tmp_path)).ok
    assert timeouts == [300, 300]
    assert source.read_bytes() == original


def test_source_negation_cold_timeout_is_retryable_without_authority_mutation(
    monkeypatch, tmp_path
):
    """An exhausted cold check remains a resumable pause, never proof authority."""
    source, node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\nprivate lemma not_bad : ¬ (∀ n : Nat, n < 5) := by omega\n",
        encoding="utf-8",
    )
    original_source = source.read_bytes()
    original_blueprint = plan_state.load_blueprint()
    timeouts: list[int] = []

    def timed_out(_code, *, cwd, timeout_s):
        assert cwd == str(tmp_path)
        assert source.read_bytes() == original_source
        timeouts.append(timeout_s)
        return {
            "success": False,
            "ok": False,
            "timed_out": True,
            "retryable": True,
            "failure_kind": "infrastructure_timeout",
            "messages": [],
        }

    monkeypatch.delenv("LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S", raising=False)
    monkeypatch.setattr(negation_probe, "probe_timeout_s", lambda: 120)
    monkeypatch.setattr(negation_promotion, "lean_ephemeral_source_check", timed_out)

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert result.retryable is True
    assert result.scan_may_continue is True
    assert result.failure_kind == "infrastructure_timeout"
    assert timeouts == [300]
    assert source.read_bytes() == original_source
    assert plan_state.load_blueprint() == original_blueprint
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    assert not plan_state.load_summary().get("negation_promotions")


def test_authoritative_source_check_records_the_first_lean_error(monkeypatch, tmp_path):
    """Persist a bounded actionable diagnostic, not only the broad failure kind."""
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "diagnostic-test")
    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        lambda *_args, **_kwargs: {
            "success": False,
            "failure_kind": "lean_elaboration",
            "error": "warning before the bounded error prefix",
            "output": (
                "/tmp/check.lean:1:1: warning: declaration uses 'sorry'\n"
                "/tmp/check.lean:9:68: error: unexpected token ')'; expected ':=' or '|'\n"
                "/tmp/check.lean:10:1: error: unknown constant `neg_bad`\n"
            ),
            "messages": [],
        },
    )
    monkeypatch.setattr(
        negation_promotion,
        "append_workflow_activity",
        lambda event, _message, **details: events.append((event, details)),
    )

    result = negation_promotion._run_authoritative_source_check(
        "theorem bad : False := by trivial",
        cwd=str(tmp_path),
        theorem="bad",
    )

    assert result["failure_detail"] == (
        "/tmp/check.lean:9:68: error: unexpected token ')'; expected ':=' or '|'"
    )
    completed = next(
        details for event, details in events if event == "negation-promotion-kernel-check-completed"
    )
    assert completed["failure_detail"] == result["failure_detail"]


def test_source_negation_exact_project_harness_omits_later_declarations(monkeypatch, tmp_path):
    """Candidate checks must not elaborate unrelated declarations after the alias."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        "import Mathlib\n\n"
        "theorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n\n"
        "private lemma not_bad : ¬ (∀ n : Nat, n < 5) := by\n"
        "  intro h\n"
        "  have := h 5\n"
        "  omega\n\n"
        "/-- A doc containing `<` that the project parser accepts. -/\n"
        "@[category research open]\n"
        "theorem next_declaration : True := by\n"
        "  trivial\n",
        encoding="utf-8",
    )
    original = source.read_bytes()
    harnesses = []

    def exact_project_check(code, *, cwd, timeout_s):
        assert cwd == str(tmp_path)
        assert timeout_s >= 10
        assert source.read_bytes() == original
        harnesses.append(code)
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "ok": True,
            "output": f"'{alias.group(1)}' does not depend on any axioms",
            "messages": [],
        }

    monkeypatch.setattr(
        negation_promotion,
        "lean_ephemeral_source_check",
        exact_project_check,
    )

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
    )

    assert result.ok is True
    assert len(harnesses) == 1
    harness = harnesses[0]
    assert "theorem leanflowNegationPromotion_" in harness
    assert "/-- A doc containing" not in harness
    assert "next_declaration" not in harness
    assert source.read_bytes() == original


def test_source_negation_rejects_sorry_axiom(monkeypatch, tmp_path):
    source, node_id, _entry = _setup(monkeypatch, tmp_path)
    source.write_text(
        source.read_text(encoding="utf-8") + "\nlemma not_bad : ¬ (∀ n : Nat, n < 5) := by omega\n",
        encoding="utf-8",
    )

    def scratch(code, **_kwargs):
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "messages": [
                {
                    "severity": "warning",
                    "message": f"'{alias.group(1)}' depends on axioms: [sorryAx]",
                }
            ],
        }

    monkeypatch.setattr(negation_promotion, "lean_ephemeral_source_check", scratch)

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label="Demo.lean",
        proof_declaration="not_bad",
        cwd=str(tmp_path),
    )

    assert result.ok is False
    assert "non-standard axioms" in result.reason
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"


def test_repeated_source_promotion_preserves_ambiguous_tmp_alias_evidence(monkeypatch, tmp_path):
    canonical_project, alias_project, source, _node_id = _setup_tmp_alias_project(
        monkeypatch, tmp_path
    )

    def scratch(code, **_kwargs):
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "messages": [
                {
                    "severity": "warning",
                    "message": f"'{alias.group(1)}' does not depend on any axioms",
                }
            ],
        }

    journal_events = []
    activity_events = []
    monkeypatch.setattr(negation_promotion, "lean_ephemeral_source_check", scratch)
    monkeypatch.setattr(
        negation_promotion.plan_state,
        "append_journal_event",
        lambda event: journal_events.append(dict(event)),
    )
    monkeypatch.setattr(
        negation_promotion,
        "append_workflow_activity",
        lambda event_type, message, **details: activity_events.append(
            (event_type, message, details)
        ),
    )

    first = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label=str(alias_project / "Demo.lean"),
        proof_declaration="not_bad",
        cwd=str(alias_project),
    )
    revision_after_first = plan_state.load_blueprint().revision

    def seed_legacy_alias_duplicate(summary):
        existing = dict(summary["negation_promotions"][0])
        existing.pop("canonical_file", None)
        existing.pop("promotion_id", None)
        existing["file"] = str(alias_project / "Demo.lean")
        existing["key"] = f"{alias_project / 'Demo.lean'}::bad"
        existing["promoted_at"] = "2099-01-01T00:00:00+00:00"
        summary["negation_promotions"].append(existing)

    negation_promotion.update_json_file(
        plan_state.plan_state_paths().summary_json, seed_legacy_alias_duplicate
    )
    second = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label=str(source),
        proof_declaration="not_bad",
        cwd=str(canonical_project),
    )

    promotions = plan_state.load_summary()["negation_promotions"]
    assert first.ok is True and first.already_promoted is False
    assert second.ok is False and second.already_promoted is False
    assert "requires reconciliation" in second.reason
    assert len(promotions) == 2
    assert promotions[0]["file"] == str(source.resolve())
    assert promotions[0]["canonical_file"] == str(source.resolve())
    assert promotions[0]["key"] == f"{source.resolve()}::bad"
    assert promotions[0]["promotion_id"] == first.evidence["promotion_id"]
    assert promotions[1]["file"] == str(alias_project / "Demo.lean")
    assert "promotion_id" not in promotions[1]
    assert plan_state.load_blueprint().revision == revision_after_first
    assert [event["event"] for event in journal_events] == ["negation-promoted"]
    assert [event[0] for event in activity_events] == [
        "negation-promotion-kernel-check-started",
        "negation-promotion-kernel-check-completed",
        "negation-promoted",
        "negation-promotion-kernel-check-started",
        "negation-promotion-kernel-check-completed",
    ]


def test_startup_migration_preserves_legacy_tmp_alias_duplicates(monkeypatch, tmp_path):
    canonical_project, alias_project, source, node_id = _setup_tmp_alias_project(
        monkeypatch, tmp_path
    )
    source_revision = hashlib.sha256(source.read_bytes()).hexdigest()
    legacy = {
        "key": f"{alias_project / 'Demo.lean'}::bad",
        "theorem": "bad",
        "file": str(alias_project / "Demo.lean"),
        "source_revision_sha256": source_revision,
        "declaration_signature_sha256": "same-signature",
        "negation_name": "not_bad",
        "negation_prop": "¬ (∀ n : Nat, n < 5)",
        "proof_tactic": "exact not_bad",
        "proof_declaration": "not_bad",
        "axioms": [],
        "promotion_kind": "source_negation",
        "promoted_at": "2026-01-01T00:00:00+00:00",
        "node_id": node_id,
        "is_main_goal": True,
    }
    canonical_duplicate = {
        **legacy,
        "key": f"{source.resolve()}::bad",
        "file": str(source.resolve()),
        "promoted_at": "2026-01-01T00:10:00+00:00",
    }

    def seed(summary):
        summary["negation_promotions"] = [legacy, canonical_duplicate]

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, seed)
    journal_events = []
    activity_events = []
    monkeypatch.setattr(
        negation_promotion.plan_state,
        "append_journal_event",
        lambda event: journal_events.append(dict(event)),
    )
    monkeypatch.setattr(
        negation_promotion,
        "append_workflow_activity",
        lambda *args, **kwargs: activity_events.append((args, kwargs)),
    )

    migrated = negation_promotion.migrate_promotion_summary(cwd=str(canonical_project))
    first_summary = plan_state.load_summary()
    repeated = negation_promotion.migrate_promotion_summary(cwd=str(canonical_project))
    second_summary = plan_state.load_summary()

    assert migrated["records_before"] == 2
    assert migrated["records_after"] == 2
    assert migrated["duplicates_removed"] == 0
    assert migrated["records_canonicalized"] == 0
    assert repeated["duplicates_removed"] == 0
    assert repeated["records_canonicalized"] == 0
    assert first_summary == second_summary
    assert second_summary["negation_promotions"] == [legacy, canonical_duplicate]
    audit = negation_promotion._audit_active_promotions(second_summary["negation_promotions"])
    assert audit.ok is False
    assert audit.ambiguous == 2
    assert journal_events == []
    assert activity_events == []


def test_startup_migration_preserves_incomplete_axiom_evidence(monkeypatch, tmp_path):
    source, node_id, _entry = _setup(monkeypatch, tmp_path)
    complete = {
        "key": f"{source.resolve()}::bad",
        "theorem": "bad",
        "file": str(source),
        "source_revision_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "declaration_signature_sha256": "same-signature",
        "negation_prop": "¬ (∀ n : Nat, n < 5)",
        "proof_tactic": "decide",
        "axioms": [],
        "node_id": node_id,
        "is_main_goal": True,
    }
    missing_axiom_result = dict(complete)
    missing_axiom_result.pop("axioms")

    def seed(summary):
        summary["negation_promotions"] = [complete, missing_axiom_result]

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, seed)

    migrated = negation_promotion.migrate_promotion_summary(cwd=str(tmp_path))

    assert migrated["records_after"] == 2
    assert migrated["duplicates_removed"] == 0
    assert len(plan_state.load_summary()["negation_promotions"]) == 2


def test_startup_migration_preserves_exact_final4_shaped_helper(monkeypatch, tmp_path):
    """Startup storage migration cannot rewrite leased legacy helper evidence."""
    source, node_id, _entry = _setup(monkeypatch, tmp_path, sublemma=True)
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\nprivate lemma not_bad : ¬ (∀ n : Nat, n < 5) := by omega\n",
        encoding="utf-8",
    )
    goal = negation_probe.build_negation_goal(str(source), "bad", cwd=str(tmp_path))
    assert isinstance(goal, negation_probe.NegationGoal)
    legacy = {
        "axioms": ["propext", "Classical.choice", "Quot.sound"],
        "canonical_file": str(source),
        "declaration_signature_sha256": hashlib.sha256(goal.original.encode("utf-8")).hexdigest(),
        "file": str(source),
        "is_main_goal": False,
        "key": f"{source}::bad",
        "negation_name": goal.name,
        "negation_prop": goal.prop,
        "node_id": node_id,
        "promoted_at": "2026-07-14T19:53:12+00:00",
        "promotion_id": "0" * 64,
        "promotion_kind": "source_negation",
        "proof_declaration": "not_bad",
        "proof_tactic": "exact not_bad",
        "source_revision_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "theorem": "bad",
    }
    legacy["promotion_id"] = negation_promotion._legacy_promotion_id(legacy, tmp_path)

    def seed(summary):
        summary["negation_promotions"] = [legacy]
        summary["negation_promotion_transactions"] = []

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, seed)

    migrated = negation_promotion.migrate_promotion_summary(cwd=str(tmp_path))

    assert migrated == {
        "records_before": 1,
        "records_after": 1,
        "records_canonicalized": 0,
        "duplicates_removed": 0,
    }
    assert plan_state.load_summary()["negation_promotions"] == [legacy]
    audit = negation_promotion._audit_active_promotions([legacy])
    assert audit.reconcilable == 1
    assert audit.ambiguous == 0


def test_startup_routes_reconcilable_helper_to_cleanup_before_pending_audit(monkeypatch, tmp_path):
    """A unique legacy helper reaches leased recovery before it can pause startup."""
    source, _node_id, entry = _setup(monkeypatch, tmp_path, sublemma=True)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    monkeypatch.setattr(
        false_decomposition_cleanup,
        "reconcile_false_decompositions",
        lambda *args, **kwargs: false_decomposition_cleanup.CleanupReconciliation(),
    )
    promoted = negation_promotion.promote_negation(entry, cwd=str(tmp_path))
    current = dict(promoted.evidence or {})
    legacy = {
        field: current[field]
        for field in (
            "axioms",
            "canonical_file",
            "declaration_signature_sha256",
            "file",
            "is_main_goal",
            "key",
            "negation_name",
            "negation_prop",
            "node_id",
            "promoted_at",
            "promotion_id",
            "proof_tactic",
            "source_revision_sha256",
            "theorem",
        )
    }
    if "promotion_kind" in current:
        legacy["promotion_kind"] = current["promotion_kind"]
    legacy["file"] = str(source)
    legacy["canonical_file"] = str(source)
    legacy["key"] = f"{source}::{legacy['theorem']}"
    legacy["promotion_id"] = negation_promotion._legacy_promotion_id(legacy, tmp_path)

    def seed(summary):
        summary["negation_promotions"] = [legacy]
        summary["negation_promotion_transactions"] = []

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, seed)
    observed = []

    def capture(promotions, **kwargs):
        observed.extend(dict(item) for item in promotions)
        assert callable(kwargs["validate_promotion"])
        return false_decomposition_cleanup.CleanupReconciliation()

    monkeypatch.setattr(
        false_decomposition_cleanup,
        "reconcile_false_decompositions",
        capture,
    )

    reconciled = negation_promotion.reconcile_promotions_on_startup(cwd=str(tmp_path))

    assert observed == [legacy]
    assert reconciled.promotion_pending == 1
    assert "reconcilable negation promotion" in reconciled.promotion_reasons[0]


def test_startup_upgrades_uniquely_proven_legacy_promotion_under_lease(monkeypatch, tmp_path):
    """A real pre-binding record gains authority only after source and graph proof."""
    source, _node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    promoted = negation_promotion.promote_negation(entry, cwd=str(tmp_path))
    assert promoted.ok

    def downgrade(summary):
        current = dict(summary["negation_promotions"][0])
        legacy = {
            field: current[field]
            for field in (
                "axioms",
                "canonical_file",
                "declaration_signature_sha256",
                "file",
                "is_main_goal",
                "key",
                "negation_name",
                "negation_prop",
                "node_id",
                "promoted_at",
                "promotion_id",
                "proof_tactic",
                "source_revision_sha256",
                "theorem",
            )
        }
        if "promotion_kind" in current:
            legacy["promotion_kind"] = current["promotion_kind"]
        if "proof_declaration" in current:
            legacy["proof_declaration"] = current["proof_declaration"]
        legacy["file"] = str(source)
        legacy["canonical_file"] = str(source)
        legacy["promotion_id"] = negation_promotion._legacy_promotion_id(legacy, tmp_path)
        summary["negation_promotions"] = [legacy]
        summary["negation_promotion_transactions"] = []

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, downgrade)
    negation_promotion.migrate_promotion_summary(cwd=str(tmp_path))

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )

    assert reconciled.terminal_disproof is True
    upgraded = plan_state.load_summary()["negation_promotions"][0]
    assert upgraded["operation_path"] == str(source)
    assert upgraded["graph_node_name"] == "bad"
    assert upgraded["graph_node_file"] == "Demo.lean"
    assert upgraded["graph_identity_sha256"]
    assert upgraded["promotion_id"] != negation_promotion._legacy_promotion_id(upgraded, tmp_path)
    transactions = plan_state.load_summary()["negation_promotion_transactions"]
    assert len(transactions) == 1
    assert transactions[0]["state"] == "committed"
    assert transactions[0]["transaction_id"] == upgraded["promotion_id"]
    assert (
        negation_promotion._audit_promotion_transactions(transactions).records[0].disposition
        == "terminal"
    )


def test_promotion_identity_keeps_distinct_revision_and_signature(monkeypatch, tmp_path):
    source, _node_id, _entry = _setup(monkeypatch, tmp_path)
    events = []
    monkeypatch.setattr(
        negation_promotion.plan_state,
        "append_journal_event",
        lambda event: events.append(dict(event)),
    )
    monkeypatch.setattr(negation_promotion, "append_workflow_activity", lambda *a, **k: None)
    campaign = plan_state.load_summary()["campaign"]
    root = campaign[negation_promotion._CAMPAIGN_ROOTS_FIELD]["roots"][0]
    base = {
        "theorem": "bad",
        "file": "Demo.lean",
        "source_revision_sha256": "a" * 64,
        "declaration_signature_sha256": root["declaration_signature_sha256"],
        "negation_name": "not_bad",
        "negation_prop": "¬ (∀ n : Nat, n < 5)",
        "proof_tactic": "decide",
        "axioms": [],
    }

    results = []
    for promotion in (
        base,
        {**base, "source_revision_sha256": "b" * 64},
        {
            **base,
            "source_revision_sha256": "b" * 64,
            "declaration_signature_sha256": "c" * 64,
        },
    ):
        with decomposition_provenance.source_operation(source) as operation:
            results.append(
                negation_promotion._commit_promotion(
                    theorem_id="bad",
                    file_label="Demo.lean",
                    promotion={
                        **promotion,
                        "operation_path": str(operation.path),
                    },
                    project_root=tmp_path,
                    operation=operation,
                    source_bytes=decomposition_provenance.read_source_bytes(operation),
                )
            )

    promotions = plan_state.load_summary()["negation_promotions"]
    assert all(result.ok and not result.already_promoted for result in results[:2])
    assert results[2].ok is False
    assert "requested-root declaration identity changed" in results[2].reason
    assert len(promotions) == 2
    assert len({promotion["promotion_id"] for promotion in promotions}) == 2
    assert [event["event"] for event in events] == ["negation-promoted"] * 2


@pytest.mark.parametrize("crash_stage", ["pending-persisted", "graph-persisted"])
def test_incomplete_promotion_transaction_replays_after_restart(monkeypatch, tmp_path, crash_stage):
    """A crash on either side of the graph write must replay one promotion."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    def crash(stage):
        if stage == crash_stage:
            raise RuntimeError(f"injected crash at {stage}")

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        negation_promotion.promote_negation(entry, cwd=str(tmp_path))

    crashed = plan_state.load_summary()
    assert crashed.get("negation_promotions", []) == []
    assert crashed["negation_promotion_transactions"][-1]["state"] == "pending"
    expected_crashed_status = "proving" if crash_stage == "pending-persisted" else "false"
    assert plan_state.load_blueprint().node_by_id(node_id).status == expected_crashed_status

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", lambda _stage: None)
    recovered = negation_promotion.recover_promotion_transactions(cwd=str(tmp_path))

    assert recovered["committed"] == 1
    assert recovered["quarantined"] == 0
    assert plan_state.load_blueprint().node_by_id(node_id).status == "false"
    summary = plan_state.load_summary()
    assert len(summary["negation_promotions"]) == 1
    assert summary["negation_promotion_transactions"][-1]["state"] == "committed"


def test_stale_incomplete_promotion_rolls_back_false_graph_node(monkeypatch, tmp_path):
    """A stale transaction cannot leave graph falsity behind after restart."""
    source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    def crash_after_graph(stage):
        if stage == "graph-persisted":
            raise RuntimeError("injected crash after graph")

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", crash_after_graph)
    with pytest.raises(RuntimeError, match="injected crash"):
        negation_promotion.promote_negation(entry, cwd=str(tmp_path))
    assert plan_state.load_blueprint().node_by_id(node_id).status == "false"

    source.write_text(source.read_text(encoding="utf-8") + "\n-- stale\n", encoding="utf-8")
    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", lambda _stage: None)
    recovered = negation_promotion.recover_promotion_transactions(cwd=str(tmp_path))

    assert recovered["committed"] == 0
    assert recovered["quarantined"] == 1
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    summary = plan_state.load_summary()
    assert summary.get("negation_promotions", []) == []
    assert summary["negation_promotion_transactions"][-1]["state"] == "quarantined"
    assert "source revision changed" in summary["negation_promotion_quarantine"][-1]["reason"]


def test_startup_revalidates_only_exact_main_goal_as_terminal(monkeypatch, tmp_path):
    """A current main disproof rehydrates exit truth; a sublemma never does."""
    _source, _node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    promoted = negation_promotion.promote_negation(entry, cwd=str(tmp_path))
    assert promoted.is_main_goal is True

    main = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )
    assert main.terminal_disproof is True
    assert main.promotion["theorem"] == "bad"

    def make_sublemma(summary):
        summary["negation_promotions"][0]["is_main_goal"] = False

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, make_sublemma)
    sublemma = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )
    assert sublemma.terminal_disproof is False


def test_startup_revalidates_registered_root_independent_of_current_rotation(monkeypatch, tmp_path):
    """Current root B and later split metadata cannot hide requested root A."""
    source, node_id, entry = _setup(monkeypatch, tmp_path)
    source.write_text(
        source.read_text(encoding="utf-8") + "\ntheorem other_root : True := by\n  sorry\n",
        encoding="utf-8",
    )
    goal = negation_probe.build_negation_goal(str(source), "bad", cwd=str(tmp_path))
    assert isinstance(goal, negation_probe.NegationGoal)
    entry["promotion_evidence"].update(
        {
            "source_revision_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "declaration_signature_sha256": hashlib.sha256(
                goal.original.encode("utf-8")
            ).hexdigest(),
            "negation_prop": goal.prop,
        }
    )
    blueprint = plan_state.load_blueprint()
    other_id = plan_state.node_id_for("other_root", "Demo.lean")
    plan_state.save_blueprint(
        blueprint.replace_node(
            GraphNode(
                id=other_id,
                name="other_root",
                file="Demo.lean",
                status="proving",
                generated_by="queue-sync",
            )
        )
    )
    _reopen_campaign_root_registration()
    registered = negation_promotion.record_requested_campaign_roots(
        [
            {"target_symbol": "bad", "active_file": "Demo.lean"},
            {"target_symbol": "other_root", "active_file": "Demo.lean"},
        ],
        campaign_id="test-campaign",
        cwd=str(tmp_path),
    )
    assert registered.ok
    blueprint = plan_state.load_blueprint()
    plan_state.save_blueprint(
        Blueprint(
            nodes=blueprint.nodes,
            edges=(
                *blueprint.edges,
                GraphEdge(source=node_id, target=other_id, kind="split_of"),
            ),
            revision=blueprint.revision,
        )
    )
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    promoted = negation_promotion.promote_negation(entry, cwd=str(tmp_path))
    assert promoted.ok and promoted.is_main_goal

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path),
        target_symbol="other_root",
        active_file="Demo.lean",
    )

    assert reconciled.terminal_disproof is True
    assert reconciled.promotion["theorem"] == "bad"


def test_startup_without_target_retains_ambiguous_same_file_promotion(monkeypatch, tmp_path):
    """A forged same-file record remains visible and blocks terminal truth."""
    _source, _node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    promoted = negation_promotion.promote_negation(entry, cwd=str(tmp_path))
    assert promoted.ok

    def add_ambiguous_record(summary):
        other = dict(summary["negation_promotions"][0])
        other["theorem"] = "other_main"
        other["key"] = f"{other['file']}::other_main"
        other.pop("promotion_id", None)
        summary["negation_promotions"].append(
            negation_promotion._canonicalize_promotion_record(other, tmp_path)
        )

    negation_promotion.update_json_file(
        plan_state.plan_state_paths().summary_json, add_ambiguous_record
    )

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="", active_file="Demo.lean"
    )

    assert reconciled.terminal_disproof is False
    assert reconciled.quarantined == 0
    assert reconciled.promotion_pending == 1
    assert len(plan_state.load_summary()["negation_promotions"]) == 2
    assert "theorem and graph identities differ" in reconciled.promotion_reasons[0]


def test_startup_propagates_final_false_cleanup_pause_state(monkeypatch, tmp_path):
    """Promotion reconciliation exposes durable source/graph ambiguity to native startup."""
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_promotion,
        "recover_promotion_transactions",
        lambda **kwargs: {"committed": 0, "quarantined": 0},
    )
    monkeypatch.setattr(
        false_decomposition_cleanup,
        "reconcile_false_decompositions",
        lambda *args, **kwargs: false_decomposition_cleanup.CleanupReconciliation(
            pending=2,
            quarantined=1,
            reasons=("graph revision changed after source persistence",),
        ),
    )

    reconciled = negation_promotion.reconcile_promotions_on_startup(cwd=str(tmp_path))

    assert reconciled.cleanup_pending == 2
    assert reconciled.cleanup_quarantined == 1
    assert reconciled.cleanup_reasons == ("graph revision changed after source persistence",)


def test_startup_quarantines_stale_committed_main_promotion(monkeypatch, tmp_path):
    """Changed source invalidates durable disproof and reopens proving work."""
    source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    assert negation_promotion.promote_negation(entry, cwd=str(tmp_path)).ok
    source.write_text(source.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")

    def seed_stale_report(summary):
        summary["final_report"] = {"status": "disproved", "detail": "old evidence"}

    negation_promotion.update_json_file(
        plan_state.plan_state_paths().summary_json, seed_stale_report
    )

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )

    assert reconciled.terminal_disproof is False
    assert reconciled.quarantined == 1
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    summary = plan_state.load_summary()
    assert summary["negation_promotions"] == []
    assert summary["negation_promotion_quarantine"][-1]["theorem"] == "bad"
    assert "final_report" not in summary


def test_startup_quarantines_stale_declaration_signature(monkeypatch, tmp_path):
    """A matching new source revision cannot launder an old declaration identity."""
    source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    assert negation_promotion.promote_negation(entry, cwd=str(tmp_path)).ok
    source.write_text(
        source.read_text(encoding="utf-8").replace("n < 5", "n < 6"), encoding="utf-8"
    )

    def accept_only_new_revision(summary):
        stale = dict(summary["negation_promotions"][0])
        stale["source_revision_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        summary["negation_promotions"] = [
            negation_promotion._canonicalize_promotion_record(stale, tmp_path)
        ]

    negation_promotion.update_json_file(
        plan_state.plan_state_paths().summary_json, accept_only_new_revision
    )

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )

    assert reconciled.terminal_disproof is False
    assert reconciled.quarantined == 1
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    quarantine = plan_state.load_summary()["negation_promotion_quarantine"][-1]
    assert "declaration signature changed" in quarantine["reason"]


def test_later_split_edge_cannot_demote_immutable_requested_root(monkeypatch, tmp_path):
    """Mutable decomposition metadata cannot erase scope-entry root authority."""
    source, node_id, entry = _setup(monkeypatch, tmp_path)
    source_before = source.read_bytes()
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "tactic": "decide",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    assert negation_promotion.promote_negation(entry, cwd=str(tmp_path)).is_main_goal
    blueprint = plan_state.load_blueprint()
    parent = GraphNode(id="new-parent", name="parent", file="Demo.lean", status="split")
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, parent),
            edges=(*blueprint.edges, GraphEdge(source=node_id, target=parent.id, kind="split_of")),
            revision=blueprint.revision,
        )
    )

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )

    assert reconciled.terminal_disproof is True
    assert reconciled.quarantined == 0
    assert reconciled.decompositions_cleaned == 0
    assert source.read_bytes() == source_before


def test_stale_redundant_promotion_does_not_undo_prior_false_evidence(monkeypatch, tmp_path):
    """An empty graph delta means another promotion already owned falsity."""
    _source, node_id, _entry = _setup(monkeypatch, tmp_path)
    blueprint = plan_state.load_blueprint()
    plan_state.save_blueprint(blueprint.invalidate_false_subtree(node_id))

    changed = negation_promotion._restore_transaction_graph(
        {
            "node_id": node_id,
            "graph_before_statuses": {},
            "graph_after_statuses": {},
        }
    )

    assert changed is False
    assert plan_state.load_blueprint().node_by_id(node_id).status == "false"


@pytest.mark.parametrize("stage", ["graph-persisted", "committed"])
def test_source_change_at_transaction_hook_rolls_back_authority(monkeypatch, tmp_path, stage):
    """A source write on either side of finalization cannot leave false truth."""
    source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    def mutate_source(hook_stage):
        if hook_stage == stage:
            source.write_text(source.read_text(encoding="utf-8") + "\n-- raced\n", encoding="utf-8")

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", mutate_source)

    result = negation_promotion.promote_negation(entry, cwd=str(tmp_path))

    assert result.ok is False
    assert "source" in result.reason
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    summary = plan_state.load_summary()
    assert summary.get("negation_promotions", []) == []
    assert summary["negation_promotion_transactions"][-1]["state"] == "quarantined"


def test_ancestor_swap_during_lean_rerun_cannot_promote(monkeypatch, tmp_path):
    """Replacing a leased source ancestor is detected before graph selection."""
    source, _old_node_id, entry = _setup(monkeypatch, tmp_path)
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    nested_source = source_dir / "Demo.lean"
    source.rename(nested_source)
    entry["file"] = "src/Demo.lean"
    goal = negation_probe.build_negation_goal(str(nested_source), "bad", cwd=str(tmp_path))
    assert isinstance(goal, negation_probe.NegationGoal)
    entry["promotion_evidence"].update(
        {
            "source_revision_sha256": hashlib.sha256(nested_source.read_bytes()).hexdigest(),
            "declaration_signature_sha256": hashlib.sha256(
                goal.original.encode("utf-8")
            ).hexdigest(),
            "negation_prop": goal.prop,
        }
    )
    node_id = plan_state.node_id_for("bad", "src/Demo.lean")
    current = plan_state.load_blueprint()
    plan_state.save_blueprint(
        Blueprint(
            nodes=(GraphNode(id=node_id, name="bad", file="src/Demo.lean", status="proving"),),
            revision=current.revision,
        )
    )

    def swap_ancestor(*_args, **_kwargs):
        source_dir.rename(tmp_path / "src-old")
        source_dir.mkdir()
        nested_source.write_text(
            "import Mathlib\n\ntheorem bad : ∀ n : Nat, n < 5 := by\n  sorry\n",
            encoding="utf-8",
        )
        return {"verdict": "negation_proved", "axioms": [], "axioms_ok": True}

    monkeypatch.setattr(negation_probe, "run_negation_attempt", swap_ancestor)

    result = negation_promotion.promote_negation(entry, cwd=str(tmp_path))

    assert result.ok is False
    assert "source" in result.reason
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    assert plan_state.load_summary().get("negation_promotions", []) == []


def test_alias_swap_does_not_reassign_pinned_promotion(monkeypatch, tmp_path):
    """A caller alias may move, but durable evidence remains on its pinned target."""
    canonical_project, alias_project, source, _node_id = _setup_tmp_alias_project(
        monkeypatch, tmp_path
    )
    alias_root = tmp_path / "tmp"
    alternate = tmp_path / "alternate"
    (alternate / "project").mkdir(parents=True)
    (alternate / "project" / "Demo.lean").write_text(source.read_text(), encoding="utf-8")

    def scratch(code, **_kwargs):
        alias_root.unlink()
        alias_root.symlink_to(alternate, target_is_directory=True)
        alias = re.search(r"theorem (leanflowNegationPromotion_[A-Fa-f0-9]+)", code)
        assert alias is not None
        return {
            "success": True,
            "messages": [
                {
                    "severity": "warning",
                    "message": f"'{alias.group(1)}' does not depend on any axioms",
                }
            ],
        }

    monkeypatch.setattr(negation_promotion, "lean_ephemeral_source_check", scratch)

    result = negation_promotion.promote_source_negation(
        theorem_id="bad",
        file_label=str(alias_project / "Demo.lean"),
        proof_declaration="not_bad",
        cwd=str(alias_project),
    )

    assert result.ok is True
    assert result.evidence["operation_path"] == str(source)
    assert result.evidence["file"] == str(source)


def test_recovery_quarantines_reassigned_graph_node_without_mutating_it(monkeypatch, tmp_path):
    """Pending evidence cannot restore or recommit a node reassigned after crash."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    def crash(stage):
        if stage == "graph-persisted":
            raise RuntimeError("crash")

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="crash"):
        negation_promotion.promote_negation(
            entry,
            cwd=str(tmp_path),
            requested_target_symbol="bad",
            requested_active_file="Demo.lean",
        )
    blueprint = plan_state.load_blueprint()
    node = blueprint.node_by_id(node_id)
    assert node is not None and node.status == "false"
    plan_state.save_blueprint(
        blueprint.replace_node(replace(node, name="reassigned", file="Other.lean"))
    )
    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", lambda _stage: None)

    recovered = negation_promotion.recover_promotion_transactions(cwd=str(tmp_path))

    assert recovered["committed"] == 0
    assert recovered["quarantined"] == 1
    reassigned = plan_state.load_blueprint().node_by_id(node_id)
    assert reassigned is not None
    assert reassigned.name == "reassigned"
    assert reassigned.status == "false"
    assert plan_state.load_summary().get("negation_promotions", []) == []


def test_recovery_rejects_relative_absolute_alias_duplicate(monkeypatch, tmp_path):
    """Recovery requires one semantic theorem/file node, not one lexical spelling."""
    source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    def crash(stage):
        if stage == "graph-persisted":
            raise RuntimeError("crash")

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="crash"):
        negation_promotion.promote_negation(entry, cwd=str(tmp_path))
    blueprint = plan_state.load_blueprint()
    duplicate = GraphNode(
        id="alias-duplicate",
        name="bad",
        file=str(source),
        status="proving",
    )
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, duplicate),
            edges=blueprint.edges,
            revision=blueprint.revision,
        )
    )
    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", lambda _stage: None)

    recovered = negation_promotion.recover_promotion_transactions(cwd=str(tmp_path))

    assert recovered["committed"] == 0
    assert recovered["quarantined"] == 1
    current = plan_state.load_blueprint()
    assert current.node_by_id(node_id).status == "proving"
    assert current.node_by_id("alias-duplicate").status == "proving"


def test_startup_retains_tampered_main_classification_as_ambiguous(monkeypatch, tmp_path):
    """Changing helper evidence into a main disproof cannot produce terminal truth."""
    _source, _node_id, entry = _setup(monkeypatch, tmp_path, sublemma=True)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    promoted = negation_promotion.promote_negation(entry, cwd=str(tmp_path))
    assert promoted.ok and not promoted.is_main_goal

    def tamper(summary):
        summary["negation_promotions"][0]["is_main_goal"] = True

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, tamper)

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )

    assert reconciled.terminal_disproof is False
    assert reconciled.quarantined == 0
    assert reconciled.promotion_pending == 1
    assert len(plan_state.load_summary()["negation_promotions"]) == 1
    assert "classification is contradictory" in reconciled.promotion_reasons[0]
    assert len(plan_state.load_summary().get("negation_promotions", [])) == 1


def test_startup_rejects_symlink_at_durable_operation_path(monkeypatch, tmp_path):
    """Startup never follows a symlink substituted at a stored source identity."""
    source, _node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    assert negation_promotion.promote_negation(entry, cwd=str(tmp_path)).ok
    original = tmp_path / "Original.lean"
    source.rename(original)
    source.symlink_to(original)

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )

    assert reconciled.terminal_disproof is False
    assert reconciled.quarantined == 1
    assert reconciled.promotion_pending == 1
    assert len(plan_state.load_summary().get("negation_promotions", [])) == 1


def test_tampered_rollback_plan_cannot_rewrite_unrelated_graph_node(monkeypatch, tmp_path):
    """Transaction-id reuse cannot authorize drifted rollback status writes."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    blueprint = plan_state.load_blueprint()
    victim = GraphNode(id="victim", name="victim", file="Demo.lean", status="proving")
    plan_state.save_blueprint(
        Blueprint(
            nodes=(*blueprint.nodes, victim),
            edges=blueprint.edges,
            revision=blueprint.revision,
        )
    )
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    def crash(stage):
        if stage == "graph-persisted":
            raise RuntimeError("crash")

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", crash)
    with pytest.raises(RuntimeError, match="crash"):
        negation_promotion.promote_negation(
            entry,
            cwd=str(tmp_path),
            requested_target_symbol="bad",
            requested_active_file="Demo.lean",
        )

    def tamper(summary):
        transaction = summary["negation_promotion_transactions"][-1]
        transaction["promotion"]["graph_before_statuses"]["victim"] = "verified"

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, tamper)
    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", lambda _stage: None)

    recovered = negation_promotion.recover_promotion_transactions(cwd=str(tmp_path))

    assert recovered["committed"] == 0
    assert recovered["quarantined"] == 0
    assert recovered["pending"] == 1
    current = plan_state.load_blueprint()
    assert current.node_by_id(node_id).status == "false"
    assert current.node_by_id("victim").status == "proving"
    pending = plan_state.load_summary()["negation_promotion_transactions"]
    assert len(pending) == 1
    assert pending[0]["state"] == "pending"


def test_quarantine_graph_race_retains_replay_authority(monkeypatch, tmp_path):
    """A writer re-falsifying after rollback cannot race summary authority removal."""
    source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )
    assert negation_promotion.promote_negation(entry, cwd=str(tmp_path)).ok
    source.write_text(source.read_text(encoding="utf-8") + "\n-- stale\n", encoding="utf-8")

    def race(stage):
        if stage != "quarantine-graph-persisted":
            return
        blueprint = plan_state.load_blueprint()
        plan_state.save_blueprint(blueprint.invalidate_false_subtree(node_id))

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", race)

    reconciled = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )

    assert reconciled.terminal_disproof is False
    assert reconciled.promotion_pending == 1
    assert plan_state.load_blueprint().node_by_id(node_id).status == "false"
    summary = plan_state.load_summary()
    assert len(summary.get("negation_promotions", [])) == 1
    assert len(summary["negation_promotion_quarantine_pending"]) == 1

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", lambda _stage: None)
    resumed = negation_promotion.reconcile_promotions_on_startup(
        cwd=str(tmp_path), target_symbol="bad", active_file="Demo.lean"
    )

    assert resumed.terminal_disproof is False
    assert resumed.promotion_pending == 0
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    assert plan_state.load_summary().get("negation_promotions", []) == []


def test_graph_write_between_persistence_and_finalize_cannot_promote(monkeypatch, tmp_path):
    """A cooperative graph writer cannot cross the promotion summary boundary."""
    _source, node_id, entry = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        negation_probe,
        "run_negation_attempt",
        lambda *args, **kwargs: {
            "verdict": "negation_proved",
            "axioms": [],
            "axioms_ok": True,
        },
    )

    def race(stage):
        if stage != "graph-persisted":
            return
        blueprint = plan_state.load_blueprint()
        extra = GraphNode(id="concurrent", name="concurrent", file="Demo.lean", status="stated")
        plan_state.save_blueprint(
            Blueprint(
                nodes=(*blueprint.nodes, extra),
                edges=blueprint.edges,
                revision=blueprint.revision,
            )
        )

    monkeypatch.setattr(negation_promotion, "_promotion_transaction_hook", race)

    result = negation_promotion.promote_negation(entry, cwd=str(tmp_path))

    assert result.ok is False
    assert "graph changed" in result.reason
    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(node_id).status == "proving"
    assert blueprint.node_by_id("concurrent") is not None
    assert plan_state.load_summary().get("negation_promotions", []) == []


def test_transaction_retention_never_evicts_pending_authority():
    """Unauthenticated terminal-looking rows remain unresolved evidence."""
    pending = [{"transaction_id": f"pending-{index}", "state": "pending"} for index in range(75)]
    terminal = [{"transaction_id": f"done-{index}", "state": "committed"} for index in range(80)]

    retained = negation_promotion._retained_promotion_transactions(
        [*terminal[:40], *pending, *terminal[40:]]
    )

    assert {item["transaction_id"] for item in retained if item["state"] == "pending"} == {
        f"pending-{index}" for index in range(75)
    }
    assert sum(item["state"] == "committed" for item in retained) == 80


def test_pending_state_includes_every_live_promotion_transaction(monkeypatch, tmp_path):
    """The provider barrier sees raw pending writes as well as quarantines."""
    _setup(monkeypatch, tmp_path)

    def seed(summary):
        summary["negation_promotion_quarantine_pending"] = [
            {"reason": "graph rollback requires reconciliation"}
        ]
        summary["negation_promotion_transactions"] = [
            {"transaction_id": "tx-b", "state": "pending"},
            {"transaction_id": "tx-a", "state": "pending"},
            {"transaction_id": "tx-unknown", "state": "future-in-flight"},
            {"transaction_id": "tx-missing-state"},
            {"transaction_id": "tx-done", "state": "committed"},
        ]

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, seed)

    pending, reasons = negation_promotion._promotion_pending_state()

    assert pending == 6
    assert reasons == (
        "graph rollback requires reconciliation",
        "ambiguous negation-promotion transaction tx-b: record lacks a valid transaction identity",
        "ambiguous negation-promotion transaction tx-a: record lacks a valid transaction identity",
        "ambiguous negation-promotion transaction tx-unknown: record lacks a valid transaction identity",
        "ambiguous negation-promotion transaction tx-missing-state: record lacks a valid transaction identity",
        "ambiguous negation-promotion transaction tx-done: record lacks a valid transaction identity",
    )


def test_finalize_keeps_every_active_promotion_beyond_history_cap(monkeypatch, tmp_path):
    """Adding promotion 51 cannot orphan the graph authority of promotion 1."""
    source, _node_id, _entry = _setup(monkeypatch, tmp_path)
    base = {
        "file": str(source),
        "canonical_file": str(source),
        "operation_path": str(source),
        "source_revision_sha256": "revision",
        "declaration_signature_sha256": "signature",
        "negation_prop": "False",
        "proof_tactic": "decide",
        "axioms": [],
        "node_id": "node",
        "graph_node_name": "node",
        "graph_node_file": "Demo.lean",
        "graph_identity_sha256": "graph",
        "is_main_goal": True,
    }
    existing = [
        negation_promotion._canonicalize_promotion_record(
            {**base, "theorem": f"theorem_{index}"}, tmp_path
        )
        for index in range(55)
    ]

    def seed(summary):
        summary["negation_promotions"] = existing

    negation_promotion.update_json_file(plan_state.plan_state_paths().summary_json, seed)
    added = negation_promotion._canonicalize_promotion_record(
        {**base, "theorem": "new_theorem"}, tmp_path
    )
    negation_promotion._finalize_promotion_transaction(
        {
            "transaction_id": added["promotion_id"],
            "state": "pending",
            "promotion": added,
        },
        project_root=tmp_path,
    )

    promotions = plan_state.load_summary()["negation_promotions"]
    assert len(promotions) == 56
    assert promotions[0]["theorem"] == "theorem_0"
    assert promotions[-1]["theorem"] == "new_theorem"
