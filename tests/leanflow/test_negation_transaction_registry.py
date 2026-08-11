"""Fail-closed negation transaction registry tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from leanflow_cli.workflows import negation_promotion, plan_state
from leanflow_cli.workflows import negation_transaction_registry as registry


def _sealed_promotion(tmp_path, label: str) -> dict:
    """Return one structurally authenticated promotion without live I/O."""
    source = str((tmp_path / f"{label}.lean").absolute())
    graph_file = f"{label}.lean"
    node_id = plan_state.node_id_for(label, graph_file)
    promotion = {
        "key": f"{source}::{label}",
        "theorem": label,
        "file": source,
        "canonical_file": source,
        "operation_path": source,
        "source_revision_sha256": "1" * 64,
        "declaration_signature_sha256": "2" * 64,
        "negation_name": f"not_{label}",
        "negation_prop": f"Not {label}",
        "proof_tactic": "decide",
        "axioms": [],
        "promoted_at": "2026-07-16T00:00:00+00:00",
        "node_id": node_id,
        "graph_node_name": label,
        "graph_node_file": graph_file,
        "is_main_goal": False,
        "classification_basis": "decomposition_helper",
        "scope_root_campaign_id": "",
        "scope_root_identity_sha256": "",
        "scope_root_theorem": "",
        "scope_root_file": "",
        "scope_root_node_id": "",
        "graph_before_statuses": {node_id: "proving"},
        "graph_after_statuses": {node_id: "false"},
        "graph_changed_node_identities": {node_id: {"name": label, "file": graph_file}},
        "graph_before_revision": 7,
        "graph_expected_revision": 8,
    }
    promotion["graph_identity_sha256"] = registry._sha256_json(
        registry._graph_identity_payload(promotion)
    )
    promotion["classification_identity_sha256"] = registry._sha256_json(
        registry._classification_identity_payload(promotion)
    )
    promotion["rollback_plan_sha256"] = registry._sha256_json(
        registry._rollback_plan_payload(promotion)
    )
    identity = registry._promotion_identity_payload(promotion)
    assert identity is not None
    promotion["promotion_id"] = registry._sha256_json(identity)
    return promotion


def _reseal_promotion(promotion: dict) -> None:
    """Recompute every structural seal after an adversarial payload rewrite."""
    promotion["graph_identity_sha256"] = registry._sha256_json(
        registry._graph_identity_payload(promotion)
    )
    promotion["classification_identity_sha256"] = registry._sha256_json(
        registry._classification_identity_payload(promotion)
    )
    promotion["rollback_plan_sha256"] = registry._sha256_json(
        registry._rollback_plan_payload(promotion)
    )
    identity = registry._promotion_identity_payload(promotion)
    assert identity is not None
    promotion["promotion_id"] = registry._sha256_json(identity)


def _transaction(tmp_path, label: str, state: str = "committed") -> dict:
    """Return one valid transaction in any supported durable state."""
    promotion = _sealed_promotion(tmp_path, label)
    transaction = {
        "transaction_id": promotion["promotion_id"],
        "state": state,
        "prepared_at": "2026-07-16T00:00:00+00:00",
        "promotion": promotion,
    }
    if state == "committed":
        transaction["committed_at"] = "2026-07-16T00:01:00+00:00"
    elif state == "quarantined":
        transaction["reason"] = "stale evidence"
        transaction["quarantined_at"] = "2026-07-16T00:01:00+00:00"
    elif state == "consumed-by-false-decomposition-cleanup":
        transaction["committed_at"] = "2026-07-16T00:01:00+00:00"
        transaction["cleanup_transaction_id"] = "3" * 64
    return transaction


def _legacy_promotion(tmp_path, label: str) -> dict:
    """Return one final4-shaped pre-graph promotion for startup upgrade."""
    source = str((tmp_path / f"{label}.lean").absolute())
    return {
        "axioms": ["propext", "Classical.choice", "Quot.sound"],
        "canonical_file": source,
        "declaration_signature_sha256": "2" * 64,
        "file": source,
        "is_main_goal": False,
        "key": f"{source}::{label}",
        "negation_name": f"neg_{label}",
        "negation_prop": f"Not {label}",
        "node_id": f"n{'3' * 8}",
        "promoted_at": "2026-07-15T08:36:36+00:00",
        # A fixture path rebase can make this legacy storage id stale. The
        # leased startup migration recomputes it before authority is granted.
        "promotion_id": "4" * 64,
        "promotion_kind": "source_negation",
        "proof_declaration": f"not_{label}",
        "proof_tactic": f"exact not_{label}",
        "source_revision_sha256": "1" * 64,
        "theorem": label,
    }


def _quarantine(tmp_path, label: str) -> dict:
    """Return one authenticated flattened promotion-quarantine record."""
    promotion = _sealed_promotion(tmp_path, label)
    return {
        **promotion,
        "reason": "fresh rerun no longer proves the negation",
        "quarantined_at": "2026-07-16T00:02:00+00:00",
        "transaction_id": promotion["promotion_id"],
    }


def test_absent_and_supported_terminal_registries_preserve_compatibility(tmp_path):
    absent = registry.audit_negation_transaction_registry(None)
    terminal = [
        _transaction(tmp_path, "committed"),
        _transaction(tmp_path, "quarantined", "quarantined"),
        _transaction(
            tmp_path,
            "consumed",
            "consumed-by-false-decomposition-cleanup",
        ),
    ]

    audited = registry.audit_negation_transaction_registry(terminal)

    assert absent.ok is True
    assert absent.retained_registry is None
    assert audited.ok is True
    assert audited.pending == 0
    assert audited.ambiguous == 0
    assert audited.terminal == 3
    assert audited.retained_registry == terminal


def test_synthetic_valid_shape_matches_existing_promotion_seal_authority(tmp_path):
    promotion = _sealed_promotion(tmp_path, "seal-compatible")

    assert negation_promotion._promotion_identity_seals_are_authenticated(promotion, tmp_path)
    assert (
        registry.audit_negation_transaction_registry(
            [
                {
                    "transaction_id": promotion["promotion_id"],
                    "state": "pending",
                    "prepared_at": "2026-07-16T00:00:00+00:00",
                    "promotion": promotion,
                }
            ]
        )
        .records[0]
        .disposition
        == "live"
    )


def test_live_transaction_is_retained_and_blocks_terminal_outcome(tmp_path):
    transaction = _transaction(tmp_path, "pending", "pending")

    audited = registry.audit_negation_transaction_registry([transaction])

    assert audited.ok is False
    assert audited.pending == 1
    assert audited.ambiguous == 0
    assert audited.records[0].disposition == "live"
    assert audited.retained_registry[0] is transaction
    assert audited.reasons == (
        f"live negation-promotion transaction {transaction['transaction_id']}",
    )


def test_non_mapping_entry_is_retained_exactly_and_blocks_terminal_outcome(tmp_path):
    terminal = _transaction(tmp_path, "terminal")
    corrupt = ["raw", {"unparsed": True}]
    raw = [terminal, corrupt]

    audited = registry.audit_negation_transaction_registry(raw)

    assert audited.ok is False
    assert audited.pending == 1
    assert audited.ambiguous == 1
    assert audited.retained_registry[0] is terminal
    assert audited.retained_registry[1] is corrupt
    assert raw == [terminal, corrupt]


@pytest.mark.parametrize(
    "corrupt",
    [
        "missing_state",
        "unknown_state",
        "unknown_field",
        "unknown_promotion_kind",
        "unknown_promotion_field",
        "forged_transaction_id",
        "forged_graph_seal",
        "missing_commit_provenance",
    ],
)
def test_malformed_or_forged_terminal_record_never_becomes_terminal(tmp_path, corrupt: str):
    transaction = _transaction(tmp_path, corrupt)
    if corrupt == "missing_state":
        transaction.pop("state")
    elif corrupt == "unknown_state":
        transaction["state"] = "future-terminal"
    elif corrupt == "unknown_field":
        transaction["future"] = "terminal"
    elif corrupt == "unknown_promotion_kind":
        transaction["promotion"]["promotion_kind"] = "oracle_negation"
    elif corrupt == "unknown_promotion_field":
        transaction["promotion"]["future"] = "authority"
    elif corrupt == "forged_transaction_id":
        transaction["transaction_id"] = "f" * 64
    elif corrupt == "forged_graph_seal":
        transaction["promotion"]["graph_identity_sha256"] = "f" * 64
    else:
        transaction.pop("committed_at")
    corrupt_snapshot = deepcopy(transaction)

    audited = registry.audit_negation_transaction_registry([transaction])

    assert audited.ok is False
    assert audited.pending == 1
    assert audited.ambiguous == 1
    assert audited.terminal == 0
    assert audited.records[0].disposition == "ambiguous"
    assert audited.retained_registry[0] is transaction
    assert transaction == corrupt_snapshot


def test_duplicate_terminal_identity_is_unverifiable_and_both_records_are_retained(tmp_path):
    first = _transaction(tmp_path, "duplicate")
    second = deepcopy(first)

    audited = registry.audit_negation_transaction_registry([first, second])

    assert audited.ok is False
    assert audited.pending == 2
    assert audited.ambiguous == 2
    assert audited.terminal == 0
    assert audited.retained_registry[0] is first
    assert audited.retained_registry[1] is second
    assert all("duplicated" in record.reason for record in audited.records)


@pytest.mark.parametrize(
    "forgery",
    [
        "theorem_graph_mismatch",
        "non_deterministic_node_id",
        "unknown_before_status",
        "non_false_after_status",
        "revision_gap",
        "source_without_declaration",
    ],
)
def test_self_sealed_semantic_forgery_remains_ambiguous(tmp_path, forgery: str):
    transaction = _transaction(tmp_path, forgery)
    promotion = transaction["promotion"]
    if forgery == "theorem_graph_mismatch":
        promotion["graph_node_name"] = "different"
        promotion["node_id"] = plan_state.node_id_for("different", promotion["graph_node_file"])
        old_id = next(iter(promotion["graph_before_statuses"]))
        new_id = promotion["node_id"]
        promotion["graph_before_statuses"] = {new_id: promotion["graph_before_statuses"][old_id]}
        promotion["graph_after_statuses"] = {new_id: promotion["graph_after_statuses"][old_id]}
        promotion["graph_changed_node_identities"] = {
            new_id: {
                "name": "different",
                "file": promotion["graph_node_file"],
            }
        }
    elif forgery == "non_deterministic_node_id":
        old_id = promotion["node_id"]
        promotion["node_id"] = "n00000000"
        promotion["graph_before_statuses"] = {
            promotion["node_id"]: promotion["graph_before_statuses"][old_id]
        }
        promotion["graph_after_statuses"] = {
            promotion["node_id"]: promotion["graph_after_statuses"][old_id]
        }
        promotion["graph_changed_node_identities"] = {
            promotion["node_id"]: promotion["graph_changed_node_identities"][old_id]
        }
    elif forgery == "unknown_before_status":
        promotion["graph_before_statuses"][promotion["node_id"]] = "future"
    elif forgery == "non_false_after_status":
        promotion["graph_after_statuses"][promotion["node_id"]] = "proved"
    elif forgery == "revision_gap":
        promotion["graph_expected_revision"] += 4
    else:
        promotion["promotion_kind"] = "source_negation"
    _reseal_promotion(promotion)
    transaction["transaction_id"] = promotion["promotion_id"]
    snapshot = deepcopy(transaction)

    audited = registry.audit_negation_transaction_registry([transaction])

    assert audited.ok is False
    assert audited.records[0].disposition == "ambiguous"
    assert audited.retained_registry[0] is transaction
    assert transaction == snapshot


def test_retention_cap_applies_only_to_authenticated_terminal_history(tmp_path):
    terminal = [_transaction(tmp_path, f"terminal-{index}") for index in range(55)]
    live = _transaction(tmp_path, "live", "pending")
    corrupt = {"state": "committed", "transaction_id": "not-a-hash"}
    raw = [*terminal[:27], live, corrupt, *terminal[27:]]

    audited = registry.audit_negation_transaction_registry(raw, terminal_history_cap=50)

    retained = audited.retained_registry
    assert audited.pending == 2
    assert audited.ambiguous == 1
    assert audited.terminal == 55
    assert len(retained) == 52
    assert live in retained
    assert corrupt in retained
    assert all(record in retained for record in terminal[-50:])
    assert all(record not in retained for record in terminal[:5])


def test_invalid_registry_container_is_preserved_without_normalization():
    raw = {"records": ["opaque"]}

    audited = registry.audit_negation_transaction_registry(raw)

    assert audited.ok is False
    assert audited.retained_registry is raw
    assert audited.pending == 1
    assert audited.ambiguous == 1


def test_active_promotion_registry_accepts_every_current_sealed_record_without_cap(tmp_path):
    promotions = [_sealed_promotion(tmp_path, f"active-{index}") for index in range(75)]
    source_promotion = promotions[-1]
    source_promotion["promotion_kind"] = "source_negation"
    source_promotion["proof_declaration"] = "not_active_74"
    _reseal_promotion(source_promotion)

    audited = registry.audit_negation_promotions(promotions)

    assert audited.ok is True
    assert audited.active == 75
    assert audited.reconcilable == 0
    assert audited.ambiguous == 0
    assert audited.unresolved == 0
    assert audited.retained_registry is promotions
    assert audited.retained_indexes == tuple(range(75))
    assert audited.authenticated_indexes == tuple(range(75))
    assert audited.selectable_indexes == tuple(range(75))
    assert audited.unique_authenticated_index(source_promotion["promotion_id"]) == 74
    assert audited.unique_selectable_index(source_promotion["promotion_id"]) == 74
    assert negation_promotion._promotion_identity_seals_are_authenticated(
        source_promotion, tmp_path
    )


def test_final4_shaped_legacy_promotion_is_lossless_reconcilable_not_terminal(tmp_path):
    legacy = _legacy_promotion(tmp_path, "erdos_242.variants.witness_construction")
    raw = [legacy]

    audited = registry.audit_negation_promotions(raw)

    assert audited.ok is False
    assert audited.active == 0
    assert audited.reconcilable == 1
    assert audited.ambiguous == 0
    assert audited.unresolved == 1
    assert audited.retained_registry is raw
    assert audited.retained_registry[0] is legacy
    assert audited.authenticated_indexes == ()
    assert audited.selectable_indexes == (0,)
    assert audited.unique_authenticated_index(legacy["promotion_id"]) is None
    assert audited.unique_selectable_index(legacy["promotion_id"]) == 0
    assert "leased source/graph upgrade" in audited.reasons[0]


def test_duplicate_final4_shaped_promotions_remain_ambiguous_and_lossless(tmp_path):
    legacy = _legacy_promotion(tmp_path, "erdos_242.variants.witness_construction")
    duplicate = deepcopy(legacy)
    raw = [legacy, duplicate]
    snapshot = deepcopy(raw)

    audited = registry.audit_negation_promotions(raw)

    assert audited.ok is False
    assert audited.active == 0
    assert audited.reconcilable == 0
    assert audited.ambiguous == 2
    assert audited.selectable_indexes == ()
    assert audited.unique_selectable_index(legacy["promotion_id"]) is None
    assert audited.retained_registry is raw
    assert raw == snapshot
    assert all("duplicated" in record.reason for record in audited.records)


def test_active_promotion_registry_preserves_every_malformed_raw_element(tmp_path):
    valid = _sealed_promotion(tmp_path, "valid-active")
    non_mapping = ["opaque", {"future": True}]
    unknown = _sealed_promotion(tmp_path, "unknown-active")
    unknown["future_authority"] = True
    forged = _sealed_promotion(tmp_path, "forged-active")
    forged["rollback_plan_sha256"] = "f" * 64
    malformed_legacy = _legacy_promotion(tmp_path, "legacy-unknown")
    malformed_legacy["future_authority"] = "opaque"
    raw = [valid, non_mapping, unknown, forged, malformed_legacy]
    snapshot = deepcopy(raw)

    audited = registry.audit_negation_promotions(raw)

    assert audited.ok is False
    assert audited.active == 1
    assert audited.reconcilable == 0
    assert audited.ambiguous == 4
    assert audited.retained_registry is raw
    assert all(audited.retained_registry[index] is item for index, item in enumerate(raw))
    assert raw == snapshot
    assert audited.authenticated_indexes == (0,)
    assert audited.selectable_indexes == (0,)


def test_duplicate_active_promotion_identity_is_not_a_mutation_target(tmp_path):
    first = _sealed_promotion(tmp_path, "duplicate-active")
    second = deepcopy(first)
    raw = [first, second]

    audited = registry.audit_negation_promotions(raw)

    assert audited.ok is False
    assert audited.active == 0
    assert audited.reconcilable == 0
    assert audited.ambiguous == 2
    assert audited.matching_indexes(first["promotion_id"]) == (0, 1)
    assert audited.unique_authenticated_index(first["promotion_id"]) is None
    assert audited.unique_selectable_index(first["promotion_id"]) is None
    assert audited.retained_registry is raw
    assert all("duplicated" in record.reason for record in audited.records)


def test_active_promotion_invalid_container_is_preserved_exactly():
    absent = registry.audit_negation_promotions(None)
    raw = {"active": ["opaque"]}

    audited = registry.audit_negation_promotions(raw)

    assert absent.ok is True
    assert absent.retained_registry is None
    assert audited.ok is False
    assert audited.retained_registry is raw
    assert audited.ambiguous == 1
    assert audited.unresolved == 1


def test_promotion_quarantine_caps_only_authenticated_terminal_history(tmp_path):
    terminal = [_quarantine(tmp_path, f"quarantine-{index}") for index in range(55)]
    opaque = ["opaque-quarantine"]
    raw = [*terminal[:27], opaque, *terminal[27:]]

    audited = registry.audit_negation_promotion_quarantine(
        raw,
        terminal_history_cap=50,
    )

    retained = audited.retained_registry
    assert audited.ok is False
    assert audited.terminal == 55
    assert audited.ambiguous == 1
    assert len(retained) == 51
    assert opaque in retained
    assert all(record in retained for record in terminal[-50:])
    assert all(record not in retained for record in terminal[:5])
    assert retained[audited.retained_indexes.index(27)] is opaque
    assert audited.unique_authenticated_index(terminal[-1]["promotion_id"]) == 55


@pytest.mark.parametrize(
    "corruption",
    [
        "non_mapping",
        "unknown_envelope_field",
        "missing_reason",
        "forged_promotion_seal",
        "mismatched_transaction",
        "legacy_unsealed_promotion",
    ],
)
def test_promotion_quarantine_preserves_malformed_envelopes_exactly(
    tmp_path,
    corruption: str,
):
    if corruption == "non_mapping":
        item = ["opaque", {"reason": "unparsed"}]
    elif corruption == "legacy_unsealed_promotion":
        legacy = _legacy_promotion(tmp_path, "legacy-quarantine")
        item = {
            **legacy,
            "reason": "legacy stale evidence",
            "quarantined_at": "2026-07-16T00:02:00+00:00",
            "transaction_id": legacy["promotion_id"],
        }
    else:
        item = _quarantine(tmp_path, corruption)
        if corruption == "unknown_envelope_field":
            item["future_terminal"] = True
        elif corruption == "missing_reason":
            item.pop("reason")
        elif corruption == "forged_promotion_seal":
            item["classification_identity_sha256"] = "f" * 64
        else:
            item["transaction_id"] = "f" * 64
    raw = [item]
    snapshot = deepcopy(raw)

    audited = registry.audit_negation_promotion_quarantine(raw)

    assert audited.ok is False
    assert audited.terminal == 0
    assert audited.ambiguous == 1
    assert audited.retained_registry is raw
    assert audited.retained_registry[0] is item
    assert raw == snapshot


def test_duplicate_promotion_quarantine_identity_is_retained_and_ambiguous(tmp_path):
    first = _quarantine(tmp_path, "duplicate-quarantine")
    second = deepcopy(first)
    raw = [first, second]

    audited = registry.audit_negation_promotion_quarantine(raw, terminal_history_cap=0)

    assert audited.ok is False
    assert audited.terminal == 0
    assert audited.ambiguous == 2
    assert audited.retained_registry is raw
    assert audited.retained_indexes == (0, 1)
    assert audited.matching_indexes(first["promotion_id"]) == (0, 1)
    assert audited.unique_authenticated_index(first["promotion_id"]) is None
    assert all("duplicated" in record.reason for record in audited.records)


def test_absent_and_invalid_promotion_quarantine_containers_are_lossless():
    absent = registry.audit_negation_promotion_quarantine(None)
    raw = {"quarantined": ["opaque"]}
    invalid = registry.audit_negation_promotion_quarantine(raw)

    assert absent.ok is True
    assert absent.retained_registry is None
    assert invalid.ok is False
    assert invalid.retained_registry is raw
    assert invalid.ambiguous == 1
