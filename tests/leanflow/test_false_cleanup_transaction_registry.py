"""Fail-closed false-cleanup transaction and quarantine registry tests."""

from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from leanflow_cli.workflows import decomposition_provenance, false_decomposition_cleanup, plan_state
from leanflow_cli.workflows import false_cleanup_transaction_registry as registry


def _transaction(tmp_path, label: str, state: str = "pending", *, current: bool = True) -> dict:
    """Return one record sealed by the production cleanup writer."""
    source = str((tmp_path / f"{label}.lean").absolute())
    graph_file = source
    helper = f"{label}_helper"
    parent = f"{label}_parent"
    helper_node_id = plan_state.node_id_for(helper, graph_file)
    parent_node_id = plan_state.node_id_for(parent, graph_file)
    helper_signature = "2" * 64
    promotion = {
        "promotion_id": hashlib.sha256(f"promotion:{label}".encode()).hexdigest(),
        "theorem": helper,
        "node_id": helper_node_id,
        "declaration_signature_sha256": helper_signature,
        "is_main_goal": False,
    }
    if current:
        promotion.update(
            {
                "operation_path": source,
                "graph_node_name": helper,
                "graph_node_file": graph_file,
            }
        )
        promotion["graph_identity_sha256"] = registry._graph_identity_sha256(promotion)
    restored = f"theorem {parent} : True := by\n  sorry\n"
    restored_slice = decomposition_provenance.declaration_slice(restored, parent)
    assert restored_slice is not None
    source_after = f"namespace {label}\n{restored}\nend {label}\n"
    prepared = {
        "version": 1,
        "state": "pending",
        "prepared_at": "2026-07-16T00:00:00+00:00",
        "file": source,
        "graph_file": graph_file,
        "helper": helper,
        "parent": parent,
        "helper_node_id": helper_node_id,
        "parent_node_id": parent_node_id,
        "promotion_id": promotion["promotion_id"],
        "promotion": promotion,
        "provenance_id": "b" * 64,
        "source_hash_kind": "sha256-raw-utf8-bytes",
        "source_before_sha256": "3" * 64,
        "source_after_sha256": registry._sha256_text(source_after),
        "source_after": source_after,
        "helper_declaration_sha256": "4" * 64,
        "helper_signature_sha256": helper_signature,
        "parent_current_declaration_sha256": "5" * 64,
        "parent_signature_sha256": restored_slice.signature_sha256,
        "parent_restored_declaration_sha256": registry._sha256_text(restored),
        "parent_restored_declaration": restored,
        "parent_restored_statement": registry._graph_statement(restored, parent),
        "ownership_basis": "decomposer-graph",
    }
    sealed = false_decomposition_cleanup._seal_transaction(prepared)
    if state == "committed":
        sealed["state"] = state
        sealed["committed_at"] = "2026-07-16T00:01:00+00:00"
        sealed.pop("source_after")
    elif state == "quarantined":
        sealed["state"] = state
        sealed["quarantined_at"] = "2026-07-16T00:01:00+00:00"
        sealed["reason"] = "source identity changed"
    elif state == "manual-retry-authorized":
        sealed["state"] = state
        sealed["quarantined_at"] = "2026-07-16T00:01:00+00:00"
        sealed["reason"] = "source identity changed"
        sealed["manual_retry_authorized_at"] = "2026-07-16T00:02:00+00:00"
        sealed["manual_retry_reason"] = "operator reconciled source identity"
    else:
        assert state == "pending"
    return sealed


def _reseal_transaction(transaction: dict) -> dict:
    """Reapply production cleanup seals after an intentional identity change."""
    candidate = deepcopy(transaction)
    candidate.pop("transaction_id", None)
    candidate.pop("immutable_fingerprint", None)
    candidate.pop("promotion_evidence_sha256", None)
    return false_decomposition_cleanup._seal_transaction(candidate)


def _v2_transaction(tmp_path, label: str, state: str = "pending") -> dict:
    """Return one transaction sealing a dependent source invalidation."""
    transaction = _transaction(tmp_path, label)
    graph_file = transaction["graph_file"]
    dependent = f"{label}_dependent"
    transaction["version"] = 2
    transaction["invalidated_dependents"] = [
        {
            "node_id": plan_state.node_id_for(dependent, graph_file),
            "name": dependent,
            "file": graph_file,
            "source_sha256": "6" * 64,
            "declaration_sha256": "7" * 64,
        }
    ]
    sealed = _reseal_transaction(transaction)
    if state == "committed":
        sealed["state"] = "committed"
        sealed["committed_at"] = "2026-07-16T00:01:00+00:00"
        sealed.pop("source_after")
    return sealed


def _v3_transaction(tmp_path, label: str, state: str = "pending") -> dict:
    """Return one transaction upgrading a committed version-1 cleanup."""
    transaction = _v2_transaction(tmp_path, label)
    transaction["version"] = 3
    transaction["invalidated_dependents"][0]["source_kind"] = "source_obligation"
    transaction["migration_from_transaction_id"] = "8" * 64
    sealed = _reseal_transaction(transaction)
    if state == "committed":
        sealed["state"] = "committed"
        sealed["committed_at"] = "2026-07-16T00:01:00+00:00"
        sealed.pop("source_after")
    return sealed


def _quarantine(label: str, state: str = "quarantined") -> dict:
    """Return one quarantine record with the production identity formula."""
    promotion_id = hashlib.sha256(label.encode()).hexdigest()
    provenance_id = hashlib.sha256(f"provenance:{label}".encode()).hexdigest()
    reason = f"ambiguous cleanup ownership for {label}"
    quarantine_id = hashlib.sha256(
        f"{promotion_id}\0{provenance_id}\0{reason}".encode()
    ).hexdigest()
    result = {
        "quarantine_id": quarantine_id,
        "state": state,
        "quarantined_at": "2026-07-16T00:00:00+00:00",
        "reason": reason,
        "promotion": {"promotion_id": promotion_id, "theorem": label},
        "provenance_id": provenance_id,
    }
    if state == "resolved":
        result["resolved_at"] = "2026-07-16T00:01:00+00:00"
        result["resolution_reason"] = "operator reconciled exact provenance"
    return result


def test_absent_and_current_or_legacy_committed_transactions_are_compatible(tmp_path):
    current = _transaction(tmp_path, "current", "committed")
    legacy = _transaction(tmp_path, "legacy", "committed", current=False)

    absent = registry.audit_false_cleanup_transaction_registry(None)
    audited = registry.audit_false_cleanup_transaction_registry([current, legacy])

    assert absent.ok is True
    assert absent.retained_registry is None
    assert audited.ok is True
    assert audited.pending == 0
    assert audited.ambiguous == 0
    assert audited.terminal == 2
    assert audited.retained_registry == [current, legacy]


@pytest.mark.parametrize("state", ["pending", "committed"])
def test_version_two_dependent_invalidations_are_authenticated(tmp_path, state: str):
    transaction = _v2_transaction(tmp_path, f"v2-{state}", state)

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.ambiguous == 0
    assert audited.records[0].disposition == ("terminal" if state == "committed" else "live")


@pytest.mark.parametrize("state", ["pending", "committed"])
def test_version_three_legacy_migrations_are_authenticated(tmp_path, state: str):
    transaction = _v3_transaction(tmp_path, f"v3-{state}", state)

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.ambiguous == 0
    assert audited.records[0].disposition == ("terminal" if state == "committed" else "live")


def test_version_three_migration_identity_tampering_is_ambiguous(tmp_path):
    transaction = _v3_transaction(tmp_path, "v3-tampered", "committed")
    transaction["migration_from_transaction_id"] = "not-a-sha"

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.ambiguous == 1
    assert "legacy-migration identity" in audited.records[0].reason


def test_version_three_authenticates_source_less_graph_artifacts(tmp_path):
    transaction = _v3_transaction(tmp_path, "v3-graph-artifact")
    graph_file = transaction["graph_file"]
    name = "v3_graph_artifact_planner"
    transaction["invalidated_dependents"].append(
        {
            "node_id": plan_state.node_id_for(name, graph_file),
            "name": name,
            "file": graph_file,
            "source_sha256": "",
            "declaration_sha256": "9" * 64,
            "source_kind": "graph_artifact",
        }
    )
    transaction = _reseal_transaction(transaction)

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.ambiguous == 0
    assert audited.records[0].disposition == "live"


def test_version_three_rejects_source_claim_on_graph_artifact(tmp_path):
    transaction = _v3_transaction(tmp_path, "v3-graph-artifact-source")
    transaction["invalidated_dependents"][0].update(
        {"source_kind": "graph_artifact", "source_sha256": "6" * 64}
    )
    transaction = _reseal_transaction(transaction)

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.ambiguous == 1
    assert "dependent graph identity" in audited.records[0].reason


def test_dependent_invalidation_tampering_is_ambiguous(tmp_path):
    transaction = _v2_transaction(tmp_path, "v2-tampered", "committed")
    transaction["invalidated_dependents"][0]["name"] = "replacement"

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.ambiguous == 1
    assert "dependent graph identity" in audited.records[0].reason


def test_version_one_cannot_smuggle_unsealed_dependent_invalidations(tmp_path):
    transaction = _transaction(tmp_path, "v1-smuggled", "committed")
    transaction["invalidated_dependents"] = []
    transaction["invalidated_dependents_sha256"] = registry._sha256_json([])

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.ambiguous == 1
    assert "version-2 dependent evidence" in audited.records[0].reason


@pytest.mark.parametrize("state", ["pending", "quarantined", "manual-retry-authorized"])
def test_every_unresolved_transaction_state_is_live_and_retained(tmp_path, state: str):
    transaction = _transaction(tmp_path, state, state)

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.ok is False
    assert audited.pending == 1
    assert audited.ambiguous == 0
    assert audited.terminal == 0
    assert audited.records[0].disposition == "live"
    assert audited.records[0].state == state
    assert audited.retained_registry[0] is transaction


@pytest.mark.parametrize("state", ["pending", "quarantined", "committed"])
def test_retry_authorization_provenance_survives_replay_states(tmp_path, state: str):
    """Authorization metadata remains authenticated after quarantine is cleared."""
    transaction = _transaction(tmp_path, f"authorized-{state}", state)
    transaction["manual_retry_authorized_at"] = "2026-07-16T00:02:00+00:00"
    transaction["manual_retry_reason"] = "operator authorized exact stored replay"

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.ambiguous == 0
    assert audited.records[0].state == state
    assert audited.records[0].disposition == ("terminal" if state == "committed" else "live")


def test_pending_reconciliation_metadata_is_authenticated(tmp_path):
    transaction = _transaction(tmp_path, "pending-reconciled")
    transaction["last_reconciliation_at"] = "2026-07-16T00:03:00+00:00"
    transaction["last_reconciliation_reason"] = "graph writer still owns the lease"

    audited = registry.audit_false_cleanup_transaction_registry([transaction])

    assert audited.records[0].disposition == "live"


@pytest.mark.parametrize(
    "corruption",
    [
        "non_mapping",
        "unknown_field",
        "unknown_state",
        "forged_fingerprint",
        "forged_promotion_evidence",
        "forged_replay_source",
        "forged_restored_parent",
        "committed_source_payload",
        "incomplete_reconciliation",
    ],
)
def test_malformed_or_forged_cleanup_evidence_remains_exact(tmp_path, corruption: str):
    transaction = _transaction(tmp_path, corruption, "committed")
    raw: object = transaction
    if corruption == "non_mapping":
        raw = ["unparsed", {"transaction_id": transaction["transaction_id"]}]
    elif corruption == "unknown_field":
        transaction["future_authority"] = True
    elif corruption == "unknown_state":
        transaction["state"] = "future-terminal"
    elif corruption == "forged_fingerprint":
        transaction["transaction_id"] = "f" * 64
    elif corruption == "forged_promotion_evidence":
        transaction["promotion"]["theorem"] = "another_helper"
    elif corruption == "forged_replay_source":
        transaction["state"] = "pending"
        transaction.pop("committed_at")
        transaction["source_after"] = "theorem forged : True := by trivial\n"
    elif corruption == "forged_restored_parent":
        transaction["parent_restored_declaration"] += "\n-- changed"
    elif corruption == "committed_source_payload":
        transaction["source_after"] = "theorem retained : True := by trivial\n"
    else:
        transaction["last_reconciliation_at"] = "2026-07-16T00:02:00+00:00"
    snapshot = deepcopy(raw)

    audited = registry.audit_false_cleanup_transaction_registry([raw])

    assert audited.ok is False
    assert audited.pending == 1
    assert audited.ambiguous == 1
    assert audited.terminal == 0
    assert audited.records[0].disposition == "ambiguous"
    assert audited.retained_registry[0] is raw
    assert raw == snapshot


def test_duplicate_transaction_or_promotion_identities_are_all_ambiguous(tmp_path):
    first = _transaction(tmp_path, "duplicate", "committed")
    duplicate_transaction = deepcopy(first)
    duplicate_promotion = deepcopy(first)
    duplicate_promotion["provenance_id"] = "e" * 64
    duplicate_promotion = _reseal_transaction(duplicate_promotion)

    transaction_audit = registry.audit_false_cleanup_transaction_registry(
        [first, duplicate_transaction]
    )
    promotion_audit = registry.audit_false_cleanup_transaction_registry(
        [first, duplicate_promotion]
    )

    assert transaction_audit.ambiguous == 2
    assert promotion_audit.ambiguous == 2
    assert all("duplicated" in record.reason for record in transaction_audit.records)
    assert all(
        "promotion identity is duplicated" in record.reason for record in promotion_audit.records
    )


def test_terminal_cap_never_evicts_live_or_ambiguous_transaction_evidence(tmp_path):
    terminals = [_transaction(tmp_path, f"terminal-{index}", "committed") for index in range(3)]
    live = _transaction(tmp_path, "live")
    ambiguous = {"unparsed": True}
    raw = [terminals[0], live, terminals[1], ambiguous, terminals[2]]

    audited = registry.audit_false_cleanup_transaction_registry(raw, terminal_history_cap=1)

    assert audited.retained_registry == [live, ambiguous, terminals[2]]
    assert audited.retained_registry[0] is live
    assert audited.retained_registry[1] is ambiguous
    assert audited.pending == 2
    assert audited.terminal == 3


def test_non_list_transaction_registry_is_preserved_as_ambiguous():
    raw = {"pending": ["opaque"]}

    audited = registry.audit_false_cleanup_transaction_registry(raw)

    assert audited.ok is False
    assert audited.retained_registry is raw
    assert audited.pending == 1
    assert audited.ambiguous == 1


def test_unresolved_and_resolved_quarantines_are_classified_without_coercion():
    unresolved = _quarantine("unresolved")
    resolved = _quarantine("resolved", "resolved")

    audited = registry.audit_false_cleanup_quarantine_registry([unresolved, resolved])

    assert audited.ok is False
    assert audited.pending == 1
    assert audited.ambiguous == 0
    assert audited.terminal == 1
    assert audited.records[0].disposition == "live"
    assert audited.records[1].disposition == "terminal"
    assert audited.retained_registry[0] is unresolved
    assert audited.retained_registry[1] is resolved


@pytest.mark.parametrize(
    "corruption",
    ["non_mapping", "unknown_field", "unknown_state", "forged_id", "missing_resolution"],
)
def test_malformed_quarantine_evidence_is_retained_exactly(corruption: str):
    quarantine = _quarantine(corruption, "resolved")
    raw: object = quarantine
    if corruption == "non_mapping":
        raw = ["opaque"]
    elif corruption == "unknown_field":
        quarantine["future"] = "authority"
    elif corruption == "unknown_state":
        quarantine["state"] = "dismissed"
    elif corruption == "forged_id":
        quarantine["quarantine_id"] = "f" * 64
    else:
        quarantine.pop("resolution_reason")
    snapshot = deepcopy(raw)

    audited = registry.audit_false_cleanup_quarantine_registry([raw])

    assert audited.ok is False
    assert audited.pending == 1
    assert audited.ambiguous == 1
    assert audited.terminal == 0
    assert audited.retained_registry[0] is raw
    assert raw == snapshot


def test_duplicate_quarantine_identity_is_ambiguous_and_exact():
    first = _quarantine("duplicate", "resolved")
    second = deepcopy(first)

    audited = registry.audit_false_cleanup_quarantine_registry([first, second])

    assert audited.ambiguous == 2
    assert audited.pending == 2
    assert audited.retained_registry[0] is first
    assert audited.retained_registry[1] is second
    assert all("duplicated" in record.reason for record in audited.records)


def test_quarantine_cap_only_evicts_authenticated_resolutions():
    resolved = [_quarantine(f"resolved-{index}", "resolved") for index in range(3)]
    unresolved = _quarantine("still-unresolved")
    ambiguous = {"quarantine_id": "not-authenticated"}
    raw = [resolved[0], unresolved, resolved[1], ambiguous, resolved[2]]

    audited = registry.audit_false_cleanup_quarantine_registry(raw, terminal_history_cap=1)

    assert audited.retained_registry == [unresolved, ambiguous, resolved[2]]
    assert audited.pending == 2
    assert audited.terminal == 3


def test_absent_and_non_list_quarantine_registries_preserve_exact_shape():
    absent = registry.audit_false_cleanup_quarantine_registry(None)
    raw = {"quarantines": "opaque"}
    malformed = registry.audit_false_cleanup_quarantine_registry(raw)

    assert absent.ok is True
    assert absent.retained_registry is None
    assert malformed.ok is False
    assert malformed.retained_registry is raw
    assert malformed.ambiguous == 1
