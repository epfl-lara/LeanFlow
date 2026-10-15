"""Verify immutable revisions, optimistic repairs, frozen navigation, and PDF coverage."""

from __future__ import annotations

import argparse
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from leanflow_cli.cli.formalization_command import (
    handle_formalization,
    register_formalization_parser,
)
from leanflow_cli.formalization.evidence import digest
from leanflow_cli.formalization.proposal_schema import apply_proposal
from leanflow_cli.formalization.records import (
    create_document,
    document_directory,
    load_document,
    new_revision,
    save_receipt,
    save_revision,
    source_artifact_id,
    source_bytes,
)
from leanflow_cli.formalization.service import (
    act_document,
    inspect_document,
    list_documents,
    resolve_artifact,
    show_document,
)
from tests.leanflow.test_source_inventory import _pdf


def _source(
    root: Path, text: str = r"\begin{theorem}\label{zero}Zero equals zero.\end{theorem}"
) -> Path:
    source = root / "paper.tex"
    source.write_text(text)
    return source


def _action(snapshot: dict, **values: object) -> dict:
    return {
        "type": "set_scope",
        "item_id": snapshot["items"][0]["id"],
        "scope": "excluded",
        "reason": "Deferred to a separate document",
        **values,
    }


def _assume_current_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep revision/byte-drift fixtures independent of compiler-freshness integration tests."""
    monkeypatch.setattr(
        "leanflow_cli.formalization.freshness.published_acceptance_freshness",
        lambda *_, **__: {"current": True, "status": "current", "reason": ""},
    )


def test_historical_editor_projection_never_runs_currentness_or_grants_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.formalization import freshness, proof_receipts

    record = create_document(tmp_path, _source(tmp_path))

    def expensive(*args, **kwargs):
        raise AssertionError("Historical reads must not launch currentness checking")

    monkeypatch.setattr(freshness, "published_acceptance_freshness", expensive)
    monkeypatch.setattr(proof_receipts, "current_proof_receipt", expensive)
    monkeypatch.setattr(
        "leanflow_cli.formalization.service_snapshot.assert_source_current", expensive
    )
    shown = show_document(tmp_path, record["document_id"], verify_current=False)
    assert shown["currentness"] == "historical"
    assert shown["acceptance_freshness"]["current"] is False
    assert shown["can_prove"] is False and shown["handoff"] is None
    assert shown["source_currentness"] == "unchecked"


def test_committed_action_ack_does_not_wait_for_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = create_document(tmp_path, _source(tmp_path))
    item_id = record["inventory"]["items"][0]["id"]

    def slow_snapshot(*args, **kwargs):
        raise AssertionError("Committed edit must be acknowledged before any view refresh")

    monkeypatch.setattr("leanflow_cli.formalization.service.snapshot", slow_snapshot)
    result = act_document(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        {"type": "set_scope", "item_id": item_id, "scope": "included", "reason": "Keep claim"},
        acknowledge_only=True,
    )
    assert result["committed"] is True
    assert result["document_id"] == record["document_id"]
    assert result["revision_id"] != record["revision_id"]
    assert load_document(tmp_path, record["document_id"])["revision_id"] == result["revision_id"]


@pytest.mark.parametrize("operation", ["show", "list", "inspect", "artifact", "history"])
def test_historical_views_preserve_staged_receipts_without_recovery_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from leanflow_cli.formalization.records import load_receipt
    from tests.leanflow.test_formalization_receipts import _commit_receipt

    source = _source(tmp_path)
    record = create_document(tmp_path, source)
    staged, _ = _commit_receipt(tmp_path, record)
    private = Path(record["_directory"])
    before = {
        path.relative_to(private): path.read_bytes()
        for path in private.rglob("*")
        if path.is_file() and "viewer" not in path.parts
    }

    def no_recovery(*args, **kwargs):
        raise AssertionError("Historical browsing must not audit or repair publication authority")

    monkeypatch.setattr("leanflow_cli.formalization.receipt_recovery.load_registry", no_recovery)
    if operation == "show":
        result = show_document(tmp_path, record["document_id"], verify_current=False)
        assert result["currentness"] == "historical" and not result["can_prove"]
    elif operation == "list":
        assert list_documents(tmp_path, verify_current=False)["documents"]
    elif operation == "inspect":
        assert (
            inspect_document(tmp_path, source, verify_current=False)["revision_id"]
            == record["revision_id"]
        )
    elif operation == "artifact":
        artifact = source_artifact_id(record, "paper.tex")
        assert (
            Path(
                resolve_artifact(tmp_path, record["document_id"], record["revision_id"], artifact)[
                    "path"
                ]
            ).read_bytes()
            == source.read_bytes()
        )
    else:
        parser = argparse.ArgumentParser()
        register_formalization_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(
            [
                "formalization",
                "--project",
                str(tmp_path),
                "history",
                record["document_id"],
                "--expected-revision",
                record["revision_id"],
                "--json",
            ]
        )
        assert handle_formalization(args) == 0
    assert load_receipt(record) == staged
    assert all((private / name).read_bytes() == content for name, content in before.items())
    with pytest.raises(AssertionError, match="must not audit"):
        load_document(tmp_path, record["document_id"])


def test_saved_revision_history_is_paged_without_loading_old_receipts(tmp_path: Path) -> None:
    from leanflow_cli.formalization.service_history import revision_history

    record = create_document(tmp_path, _source(tmp_path))
    revisions = document_directory(tmp_path, record["document_id"]) / "revisions"
    for index in range(125):
        (revisions / f"rev-history-{index:04}.json").write_text("Unavailable historical body")
    shown = show_document(tmp_path, record["document_id"], verify_current=False)
    assert len(shown["revisions"]) == 50
    assert shown["revisions"][0]["revision_id"] == record["revision_id"]
    assert shown["revision_history"] == {"offset": 0, "next_offset": 50, "total": 126}
    remaining = revision_history(
        tmp_path, record["document_id"], record["revision_id"], offset=50, limit=100
    )
    names = [row["revision_id"] for row in [*shown["revisions"], *remaining["revisions"]]]
    assert len(names) == len(set(names)) == 126
    assert remaining["next_offset"] is None
    assert all(row["status"] == "historical" for row in remaining["revisions"])


def test_fast_document_picker_returns_summaries_without_loading_full_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = create_document(tmp_path, _source(tmp_path))

    def no_snapshot(*args, **kwargs):
        raise AssertionError("Document picker must not load full source-region projections")

    monkeypatch.setattr("leanflow_cli.formalization.service.snapshot", no_snapshot)
    shown = list_documents(tmp_path, verify_current=False)["documents"][0]
    assert shown["document_id"] == record["document_id"]
    assert shown["items"] == [] and shown["regions"] == []
    assert shown["counts"]["items"] == 1
    assert not shown["can_prove"] and shown["handoff"] is None


@pytest.mark.parametrize(
    "status", ["published", "accepted_staged", "review_rejected", "check_error"]
)
def test_fast_picker_reports_authenticated_saved_receipt_status_without_currentness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    from leanflow_cli.formalization.records import save_receipt

    record = create_document(tmp_path, _source(tmp_path))
    save_receipt(record, {"revision_id": record["revision_id"], "status": status})

    def no_currentness(*args, **kwargs):
        raise AssertionError("Saved picker status must not inspect current acceptance")

    monkeypatch.setattr("leanflow_cli.formalization.service.snapshot", no_currentness)
    monkeypatch.setattr(
        "leanflow_cli.formalization.freshness.published_acceptance_freshness", no_currentness
    )
    shown = list_documents(tmp_path, verify_current=False)["documents"][0]
    assert shown["status"] == status
    assert shown["currentness"] == "historical" and not shown["can_prove"]
    assert shown["handoff"] is None


def test_fast_picker_does_not_trust_an_edited_receipt_status(tmp_path: Path) -> None:
    from leanflow_cli.formalization.records import save_receipt

    record = create_document(tmp_path, _source(tmp_path))
    saved = save_receipt(
        record, {"revision_id": record["revision_id"], "status": "review_rejected"}
    )
    path = Path(record["_directory"]) / "receipts" / (record["revision_id"] + ".json")
    path.write_text(json.dumps({**saved, "status": "published"}))
    with pytest.raises(ValueError, match="differs from its immutable event"):
        list_documents(tmp_path, verify_current=False)


def test_frozen_artifact_navigation_does_not_audit_or_recover_proof_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = create_document(tmp_path, _source(tmp_path))
    artifact_id = source_artifact_id(record, "paper.tex")

    def no_authority_audit(*args, **kwargs):
        raise AssertionError("Frozen browsing must not audit publication authority")

    monkeypatch.setattr("leanflow_cli.formalization.publication.load_registry", no_authority_audit)
    opened = resolve_artifact(tmp_path, record["document_id"], record["revision_id"], artifact_id)
    assert Path(opened["path"]).read_bytes() == (tmp_path / "paper.tex").read_bytes()
    with pytest.raises(AssertionError, match="must not audit"):
        resolve_artifact(
            tmp_path, record["document_id"], record["revision_id"], "proof-lean-" + "a" * 64
        )


def test_editor_keeps_native_lexical_readings_distinct_from_hard_source_repairs(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        r"\documentclass{article}\begin{theorem}\UnknownSymbol is positive.\end{theorem}"
        r"\includegraphics{missing-figure}",
    )
    shown = inspect_document(tmp_path, source, verify_current=False)
    pending = [row for row in shown["diagnostics"] if row["native_interpretation_required"]]
    assert pending and all(row["blocking"] for row in pending)
    missing = next(row for row in shown["diagnostics"] if row["code"] == "missing_support_file")
    assert missing["blocking"] and not missing["native_interpretation_required"]
    assert not shown["can_prove"]


def _reconciled_display_record(tmp_path: Path) -> dict:
    """Store a complete candidate and its original source census for historical display tests."""
    from leanflow_cli.formalization.records import save_revision
    from leanflow_cli.formalization.source_reconciliation import apply_reconciliation
    from tests.leanflow.test_source_reconciliation import proposal

    value = create_document(
        tmp_path,
        _source(tmp_path, r"\documentclass{article}\begin{theorem}A claim.\end{theorem}"),
    )
    value.update(
        workflow_protocol=2,
        proposal_phase="complete",
        author_base_revision=value["revision_id"],
        slice_source_ids=[item["id"] for item in value["inventory"]["items"]],
    )
    apply_reconciliation(value, proposal(value))
    for region in value["inventory"]["regions"]:
        region.update(
            disposition="item" if region.get("item_ids") else "supporting_context",
            reason="Retain the complete source region in the proposed interpretation",
        )
    value["inventory"]["audit_complete"] = True
    value["units"] = [
        {
            "id": "unit-complete",
            "required_source_ids": value["slice_source_ids"],
            "declarations": [],
            "representation_note": "Display saved review evidence without proof authority",
        }
    ]
    return save_revision(Path(value["_directory"]), value)


def test_historical_published_readings_show_saved_review_without_clearing_original_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import copy

    from leanflow_cli.formalization.records import save_receipt
    from leanflow_cli.formalization.service_snapshot import snapshot
    from tests.leanflow.test_source_reconciliation import reviews

    record = _reconciled_display_record(tmp_path)
    original = copy.deepcopy(record)
    save_receipt(
        record,
        {"revision_id": record["revision_id"], "status": "published", "reviews": reviews(record)},
    )

    def no_currentness_audit(*args, **kwargs):
        raise AssertionError("A saved review label must not check currentness or recover authority")

    monkeypatch.setattr(
        "leanflow_cli.formalization.freshness.published_acceptance_freshness",
        no_currentness_audit,
    )
    monkeypatch.setattr(
        "leanflow_cli.formalization.preparation.assert_preparation_current", no_currentness_audit
    )
    shown = snapshot(tmp_path, record, verify_current=False)
    readings = [row for row in shown["diagnostics"] if row.get("pending_independent_review")]
    assert readings
    assert all(row["native_interpretation_status"] == "saved_reviewed" for row in readings)
    assert all(not row["native_interpretation_required"] for row in readings)
    assert all(row.get("pending_independent_review") for row in record["inventory"]["diagnostics"])
    assert all(row["blocking"] for row in record["full_inventory"]["diagnostics"])
    assert record == original
    assert shown["currentness"] == "historical" and not shown["can_prove"]
    assert shown["handoff"] is None


@pytest.mark.parametrize(
    "tamper", ["missing", "duplicate", "stale", "cleanup", "coherence", "slice", "staged"]
)
def test_incomplete_or_nonpublished_lexical_reviews_stay_pending_in_historical_display(
    tmp_path: Path, tamper: str
) -> None:
    from leanflow_cli.formalization.records import save_receipt
    from leanflow_cli.formalization.service_snapshot import snapshot
    from tests.leanflow.test_source_reconciliation import reviews

    record = _reconciled_display_record(tmp_path)
    evidence = reviews(record)
    status = "published"
    if tamper == "missing":
        evidence[0]["coverage"].pop()
    elif tamper == "duplicate":
        evidence[0]["coverage"].append(evidence[0]["coverage"][0])
    elif tamper == "stale":
        evidence[0]["revision_id"] = "rev-stale"
    elif tamper == "cleanup":
        evidence[0]["cleanup_verified"] = False
    elif tamper == "coherence":
        evidence[0]["coherence_decision"] = "BLOCK"
    elif tamper == "slice":
        record["proposal_phase"] = "representative_slice"
    else:
        status = "accepted_staged"
    save_receipt(
        record, {"revision_id": record["revision_id"], "status": status, "reviews": evidence}
    )
    shown = snapshot(tmp_path, record, verify_current=False)
    readings = [row for row in shown["diagnostics"] if row["native_interpretation_required"]]
    assert readings
    assert all(row["native_interpretation_status"] == "pending" for row in readings)
    assert not shown["can_prove"] and shown["handoff"] is None


def test_editor_exposes_registered_clauses_without_treating_dispositions_as_approval(
    tmp_path: Path,
) -> None:
    from leanflow_cli.formalization.service_snapshot import snapshot
    from tests.leanflow.test_formalization_clause_obligations import _record

    record = _record(tmp_path, "Both algorithms run in polynomial time.", "omitted")
    shown = snapshot(tmp_path, record, verify_current=False)
    clause = shown["items"][0]["known_obligations"][0]
    assert clause["description"] == "Both algorithms run in polynomial time."
    assert clause["source_refs"][0]["artifact_id"] in {row["id"] for row in shown["artifacts"]}
    assert clause["dispositions"][0]["disposition"] == "omitted"
    assert clause["verdicts"] == []
    assert shown["items"][0]["review_status"] == "pending"
    assert not shown["can_prove"]


def test_intake_freezes_bytes_without_creating_lean_or_blueprint(tmp_path: Path) -> None:
    source = _source(tmp_path)
    first = inspect_document(tmp_path, source)
    second = inspect_document(tmp_path, source)
    assert first["revision_id"] == second["revision_id"]
    assert first["items"][0]["review_status"] == "pending"
    assert first["can_prove"] is False
    assert not list(tmp_path.rglob("*.lean"))
    assert not list(tmp_path.rglob("Blueprint.md"))
    frozen = resolve_artifact(
        tmp_path, first["document_id"], first["revision_id"], first["artifacts"][0]["id"]
    )
    source.write_text("Live source changed.")
    assert "Zero equals zero" in Path(frozen["path"]).read_text()
    assert show_document(tmp_path, first["document_id"])["source_stale"] is True
    assert list_documents(tmp_path)["documents"][0]["document_id"] == first["document_id"]


@pytest.mark.parametrize(
    ("status", "error_fields", "field", "expected"),
    [
        (
            "check_rejected",
            {"checks": {"error": "Invalid declaration"}},
            "elaboration_status",
            "rejected",
        ),
        ("review_error", {"error": "Reviewer unavailable"}, "review_status", "error"),
        (
            "check_error",
            {"checks": {"error": "Compiler unavailable"}},
            "elaboration_status",
            "error",
        ),
    ],
)
def test_receipt_failures_are_actionable_editor_states(
    tmp_path: Path, status: str, error_fields: dict, field: str, expected: str
) -> None:
    record = create_document(tmp_path, _source(tmp_path))
    save_receipt(record, {"revision_id": record["revision_id"], "status": status, **error_fields})
    shown = show_document(tmp_path, record["document_id"])
    assert shown["status"] == status
    assert shown["receipt_error"] in {
        "Invalid declaration",
        "Reviewer unavailable",
        "Compiler unavailable",
    }
    assert shown["items"][0][field] == expected
    assert not shown["can_prove"]


def test_same_content_paths_have_distinct_artifact_identities(tmp_path: Path) -> None:
    (tmp_path / "a.tex").write_text("Context.")
    (tmp_path / "b.tex").write_text("Context.")
    source = _source(tmp_path, r"\input{a}\input{b}")
    result = inspect_document(tmp_path, source)
    artifacts = [
        artifact for artifact in result["artifacts"] if artifact["path"] in {"a.tex", "b.tex"}
    ]
    assert artifacts[0]["sha256"] == artifacts[1]["sha256"]
    assert artifacts[0]["id"] != artifacts[1]["id"]
    for artifact in artifacts:
        resolved = resolve_artifact(
            tmp_path, result["document_id"], result["revision_id"], artifact["id"]
        )
        assert Path(resolved["path"]).read_text() == "Context."


def test_stale_action_rejected_and_explicit_history_selection_supported(tmp_path: Path) -> None:
    initial = inspect_document(tmp_path, _source(tmp_path))
    edited = act_document(
        tmp_path, initial["document_id"], initial["revision_id"], _action(initial)
    )
    assert edited["revision_id"] != initial["revision_id"]
    with pytest.raises(ValueError, match="revision changed"):
        act_document(tmp_path, initial["document_id"], initial["revision_id"], _action(initial))
    restored = act_document(
        tmp_path,
        initial["document_id"],
        edited["revision_id"],
        {"type": "select_revision", "revision_id": initial["revision_id"]},
    )
    assert restored["revision_id"] == initial["revision_id"]
    assert restored["items"][0]["scope"] == "included"


def test_concurrent_actions_cannot_both_apply_to_the_same_revision(tmp_path: Path) -> None:
    initial = inspect_document(tmp_path, _source(tmp_path))
    barrier = Barrier(2)

    def edit(reason: str) -> str:
        barrier.wait()
        try:
            return act_document(
                tmp_path,
                initial["document_id"],
                initial["revision_id"],
                _action(initial, reason=reason),
            )["revision_id"]
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["First actor", "Second actor"]))
    assert results.count("conflict") == 1


def test_old_accepted_receipt_never_approves_a_new_selected_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assume_current_environment(monkeypatch)
    record = create_document(tmp_path, _source(tmp_path))
    directory = Path(record["_directory"])
    receipt = {
        "revision_id": record["revision_id"],
        "status": "published",
        "reviews": [
            {
                "verdicts": [
                    {
                        "source_ids": [record["inventory"]["items"][0]["id"]],
                        "decision": "PASS",
                        "rationale": "Faithful",
                    }
                ]
            }
        ],
        "handoff": {
            "revision_id": record["revision_id"],
            "obligations": [{"id": "o1", "file": "Main.lean", "declaration": "zero"}],
        },
    }
    save_receipt(record, receipt)
    approved = show_document(tmp_path, record["document_id"])
    assert approved["can_prove"]
    rejected = new_revision(record)
    rejected["status"] = "rejected"
    save_revision(directory, rejected)
    current = show_document(tmp_path, record["document_id"])
    assert not current["can_prove"]
    assert current["items"][0]["review_status"] == "pending"
    assert current["handoff"] is None
    assert current["status"] == "rejected"


@pytest.mark.parametrize("second", ["PASS", "BLOCK", None])
def test_split_source_review_needs_every_unit_and_preserves_rejections(
    tmp_path: Path, second: str | None
) -> None:
    record = create_document(tmp_path, _source(tmp_path))
    item = record["inventory"]["items"][0]["id"]
    record = new_revision(record)
    record["units"] = [
        {
            "id": name,
            "required_source_ids": [item],
            "declarations": [],
            "representation_note": "Split conjunct",
        }
        for name in ("part-a", "part-b")
    ]
    directory = Path(record["_directory"])
    record = save_revision(directory, record)

    def verdict(unit: str, decision: str) -> dict:
        return {
            "unit_id": unit,
            "source_ids": [item],
            "decision": decision,
            "rationale": "Checked this conjunct",
            "issues": ["Repair second conjunct"] if decision == "BLOCK" else [],
        }

    reviews = [{"verdicts": [verdict("part-a", "PASS")]}]
    if second:
        reviews.append({"verdicts": [verdict("part-b", second)]})
    # A final repeated PASS for A must not erase missing or failed B.
    reviews.append({"verdicts": [verdict("part-a", "PASS")]})
    receipt = {
        "revision_id": record["revision_id"],
        "status": "awaiting_review",
        "reviews": reviews,
    }
    save_receipt(record, receipt)
    result = show_document(tmp_path, record["document_id"])["items"][0]
    expected = {"PASS": "pass", "BLOCK": "correction_needed", None: "pending"}[second]
    assert result["review_status"] == expected
    if second == "BLOCK":
        assert "Repair second conjunct" in result["feedback"]


def test_explicit_item_and_region_scope_exclusion_can_pass_proposal_coverage(
    tmp_path: Path,
) -> None:
    source = _source(
        tmp_path,
        r"\begin{theorem}First claim.\end{theorem}"
        + "\n"
        + r"\begin{theorem}Second claim.\end{theorem}",
    )
    initial = inspect_document(tmp_path, source)
    item_id = initial["items"][0]["id"]
    edited = act_document(
        tmp_path, initial["document_id"], initial["revision_id"], _action(initial)
    )
    for region in edited["regions"]:
        if item_id in region["item_ids"]:
            edited = act_document(
                tmp_path,
                edited["document_id"],
                edited["revision_id"],
                {
                    "type": "classify_region",
                    "region_id": region["id"],
                    "classification": "excluded",
                    "reason": "First claim deliberately deferred",
                },
            )
    record = load_document(tmp_path, edited["document_id"])
    proposal = {
        "schema_version": 1,
        "base_revision_id": record["revision_id"],
        "units": [
            {
                "id": "second",
                "required_source_ids": [record["inventory"]["items"][1]["id"]],
                "declarations": [{"name": "second", "file": "Main.lean"}],
                "representation_note": "Only included claim",
            }
        ],
        "coverage": [
            {
                "region_id": region["id"],
                "disposition": region["disposition"],
                "item_ids": region["item_ids"],
                "reason": region["reason"],
            }
            for region in record["inventory"]["regions"]
        ],
    }
    assert apply_proposal(record, proposal)["inventory"]["audit_complete"]


def test_published_zero_obligation_handoff_remains_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assume_current_environment(monkeypatch)
    record = create_document(tmp_path, _source(tmp_path))
    receipt = {
        "revision_id": record["revision_id"],
        "status": "published",
        "handoff": {
            "revision_id": record["revision_id"],
            "obligations": [],
            "status": "no_proof_obligations",
        },
    }
    save_receipt(record, receipt)
    result = show_document(tmp_path, record["document_id"])
    assert result["handoff"]["status"] == "no_proof_obligations"
    assert result["can_prove"] is False


def test_proof_authorization_requires_user_decision_and_invalidates_acceptance(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, r"\begin{conjecture}An open claim.\end{conjecture}")
    initial = inspect_document(tmp_path, source)
    record = load_document(tmp_path, initial["document_id"])
    save_receipt(record, {"revision_id": record["revision_id"], "status": "review_passed"})
    action = {
        "type": "authorize_proof",
        "item_id": initial["items"][0]["id"],
        "authorized": True,
        "reason": "I want to attempt this open problem",
    }
    authorized = act_document(tmp_path, initial["document_id"], initial["revision_id"], action)
    assert authorized["revision_id"] != initial["revision_id"]
    assert authorized["items"][0]["proof_authorization"] == {
        "actor": "user",
        "authorized": True,
        "reason": action["reason"],
    }
    assert authorized["status"] == "inventory_pending" and not authorized["can_prove"]
    assert authorized["items"][0]["review_status"] == "pending"
    revoked = act_document(
        tmp_path,
        initial["document_id"],
        authorized["revision_id"],
        {**action, "authorized": False, "reason": "Keep this open problem outside proof work"},
    )
    assert revoked["items"][0]["proof_authorization"]["authorized"] is False
    for changes in ({"authorized": "true"}, {"reason": ""}):
        with pytest.raises(ValueError):
            act_document(
                tmp_path, initial["document_id"], revoked["revision_id"], {**action, **changes}
            )


def test_reviewed_claim_status_comes_from_content_not_environment_title(tmp_path: Path) -> None:
    source = _source(
        tmp_path, r"\begin{conjecture}The historical conjecture has been proved.\end{conjecture}"
    )
    record = create_document(tmp_path, source)
    save_receipt(
        record,
        {
            "revision_id": record["revision_id"],
            "status": "review_passed",
            "reviews": [
                {
                    "claims": [
                        {
                            "source_id": record["inventory"]["items"][0]["id"],
                            "status": "proved",
                            "rationale": "The text reports the completed proof despite the historical title",
                        }
                    ]
                }
            ],
        },
    )
    item = show_document(tmp_path, record["document_id"])["items"][0]
    assert item["claim_status"] == "proved" and item["extracted_claim_status"] == "conjectured"
    assert item["claim_status_rationale"] and item["proof_authorization"] is None


def test_changing_claim_boundary_revokes_proof_consent(tmp_path: Path) -> None:
    source = _source(
        tmp_path,
        "\\begin{conjecture}An open claim.\\end{conjecture}\nA different claim.\n",
    )
    initial = inspect_document(tmp_path, source)
    authorized = act_document(
        tmp_path,
        initial["document_id"],
        initial["revision_id"],
        {
            "type": "authorize_proof",
            "item_id": initial["items"][0]["id"],
            "authorized": True,
            "reason": "Attempt this exact claim",
        },
    )
    updated = act_document(
        tmp_path,
        initial["document_id"],
        authorized["revision_id"],
        {
            "type": "update_boundary",
            "item_id": initial["items"][0]["id"],
            "source_refs": [
                {
                    "artifact_id": initial["artifacts"][0]["id"],
                    "line_start": 2,
                    "line_end": 2,
                }
            ],
        },
    )
    assert updated["items"][0]["proof_authorization"] is None
    assert updated["revision_id"] != authorized["revision_id"]


def test_published_snapshot_disables_handoff_on_code_drift_or_pending_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assume_current_environment(monkeypatch)
    record = create_document(tmp_path, _source(tmp_path))
    code = tmp_path / "Main.lean"
    code.write_text("theorem zero : True := by sorry\n")
    original = code.read_bytes()
    receipt = {
        "revision_id": record["revision_id"],
        "status": "published",
        "handoff": {
            "revision_id": record["revision_id"],
            "files": {"Main.lean": digest(original)},
            "obligations": [{"id": "o1", "file": "Main.lean", "declaration": "zero"}],
        },
    }
    save_receipt(record, receipt)
    assert show_document(tmp_path, record["document_id"])["can_prove"]
    code.write_bytes(original + b"-- external edit\n")
    changed = show_document(tmp_path, record["document_id"])
    assert changed["lean_stale"] and not changed["can_prove"] and changed["handoff"] is None
    code.write_bytes(original)

    def pending(root: Path) -> None:
        raise RuntimeError("Publication requires recovery")

    monkeypatch.setattr(
        "leanflow_cli.formalization.publication.assert_no_pending_publication", pending
    )
    blocked = show_document(tmp_path, record["document_id"])
    assert not blocked["can_prove"] and "Publication requires recovery" in blocked["diagnostics"]


def test_portable_evidence_survives_private_deletion_and_can_be_revised(tmp_path: Path) -> None:
    from leanflow_cli.formalization.conflict_resolution import (
        publication_conflicts,
        resolve_publication_conflict,
    )

    initial = inspect_document(tmp_path, _source(tmp_path))
    private = document_directory(tmp_path, initial["document_id"])
    portable = document_directory(tmp_path, initial["document_id"], published=True)
    shutil.copytree(private, portable)
    shutil.rmtree(private)
    shown = show_document(tmp_path, initial["document_id"])
    assert shown["revision_id"] == initial["revision_id"]
    # Portable evidence remains readable, but importing authority into a fresh
    # workspace needs an explicit loss/trust observation before mutating it.
    observed = publication_conflicts(tmp_path)["transactions"][0]
    resolve_publication_conflict(
        tmp_path,
        {
            **{
                name: observed[name]
                for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
            },
            "choice": "keep_current",
            "reason": "Inspected imported evidence; any accepted translation will require a fresh review",
        },
    )
    opened = resolve_artifact(
        tmp_path, initial["document_id"], initial["revision_id"], initial["artifacts"][0]["id"]
    )
    assert Path(opened["path"]).is_relative_to(private)
    assert not (portable / "viewer").exists()
    assert (
        inspect_document(tmp_path, tmp_path / "paper.tex")["revision_id"] == initial["revision_id"]
    )
    edited = act_document(
        tmp_path, initial["document_id"], initial["revision_id"], _action(initial)
    )
    assert edited["revision_id"] != initial["revision_id"]
    assert source_bytes(load_document(tmp_path, initial["document_id"]), "paper.tex")
    assert (private / "revisions" / f"{initial['revision_id']}.json").is_file()


def test_revision_blob_artifact_and_symlink_tampering_are_rejected(tmp_path: Path) -> None:
    result = inspect_document(tmp_path, _source(tmp_path))
    record = load_document(tmp_path, result["document_id"])
    with pytest.raises(ValueError):
        show_document(tmp_path, "../../outside")
    with pytest.raises(ValueError, match="does not belong"):
        resolve_artifact(tmp_path, result["document_id"], result["revision_id"], "../outside")
    directory = Path(record["_directory"])
    view = directory / "viewer"
    view.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        resolve_artifact(
            tmp_path, result["document_id"], result["revision_id"], result["artifacts"][0]["id"]
        )
    view.unlink()
    revision = directory / "revisions" / f"{record['revision_id']}.json"
    data = json.loads(revision.read_text())
    data["status"] = "published"
    revision.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="identity"):
        show_document(tmp_path, result["document_id"])


def test_partial_tex_selection_leaves_residual_unresolved_and_update_releases_old_boundary(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, "First definition.\nSecond unclassified statement.\n")
    initial = inspect_document(tmp_path, source)
    ref = {"artifact_id": initial["artifacts"][0]["id"], "line_start": 1, "line_end": 1}
    added = act_document(
        tmp_path,
        initial["document_id"],
        initial["revision_id"],
        {"type": "add_item", "title": "First", "kind": "definition", "source_refs": [ref]},
    )
    assert any(region["classification"] == "unresolved" for region in added["regions"])
    assert any(region["classification"] == "represented" for region in added["regions"])
    moved = act_document(
        tmp_path,
        added["document_id"],
        added["revision_id"],
        {
            "type": "update_boundary",
            "item_id": added["items"][0]["id"],
            "source_refs": [{**ref, "line_start": 2, "line_end": 2}],
        },
    )
    first = next(
        region for region in moved["regions"] if region["source_refs"][0]["byte_start"] == 0
    )
    assert first["classification"] == "unresolved"


@pytest.mark.skipif(
    not shutil.which("pdftotext") or not shutil.which("pdfinfo"), reason="Poppler unavailable"
)
def test_partial_pdf_selection_leaves_residual_unresolved(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _pdf(source, ["First statement. Second statement."])
    initial = inspect_document(tmp_path, source)
    initial = act_document(
        tmp_path,
        initial["document_id"],
        initial["revision_id"],
        {
            "type": "replace_page_transcription",
            "page_index": 0,
            "text": "First statement. Second statement.",
            "verified": True,
            "reason": "Checked the complete page, including all mathematical content",
        },
    )
    added = act_document(
        tmp_path,
        initial["document_id"],
        initial["revision_id"],
        {
            "type": "add_item",
            "title": "First",
            "kind": "statement",
            "source_refs": [
                {
                    "artifact_id": initial["artifacts"][0]["id"],
                    "page_index": 0,
                    "text": "First statement.",
                }
            ],
        },
    )
    assert any(region["classification"] == "unresolved" for region in added["regions"])
    assert any(region["classification"] == "represented" for region in added["regions"])
    assert all(
        ref["precision"] != "exact" for region in added["regions"] for ref in region["source_refs"]
    )
    with pytest.raises(ValueError, match="uniquely match"):
        act_document(
            tmp_path,
            added["document_id"],
            added["revision_id"],
            {
                "type": "add_item",
                "title": "Invented",
                "source_refs": [
                    {
                        "artifact_id": initial["artifacts"][0]["id"],
                        "page_index": 0,
                        "text": "Not in source",
                    }
                ],
            },
        )


@pytest.mark.skipif(
    not shutil.which("pdftotext") or not shutil.which("pdfinfo"), reason="Poppler unavailable"
)
def test_blank_pdf_requires_verified_transcription_or_explicit_scope_exclusion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.pdf"
    _pdf(source, [""])
    initial = inspect_document(tmp_path, source)
    with pytest.raises(ValueError, match="verified transcription"):
        act_document(
            tmp_path,
            initial["document_id"],
            initial["revision_id"],
            {
                "type": "classify_region",
                "region_id": initial["regions"][0]["id"],
                "classification": "supporting_context",
                "reason": "Acknowledged",
            },
        )
    with pytest.raises(ValueError, match="explicit verification"):
        act_document(
            tmp_path,
            initial["document_id"],
            initial["revision_id"],
            {
                "type": "transcribe_page",
                "page_index": 0,
                "text": "Complete page.",
                "reason": "Manual transcription",
                "verified": False,
            },
        )
    repaired = act_document(
        tmp_path,
        initial["document_id"],
        initial["revision_id"],
        {
            "type": "transcribe_page",
            "page_index": 0,
            "text": "Complete page.",
            "reason": "Checked all formulas against the frozen page",
            "verified": True,
        },
    )
    assert not any(row["blocking"] for row in repaired["diagnostics"])
    assert repaired["diagnostics"][0]["resolution"] == "verified_transcription"
    assert repaired["regions"][0]["classification"] == "unresolved"
    assert not repaired["can_prove"]


def test_source_artifact_mismatch_and_mapping_escape_cannot_modify_revision(tmp_path: Path) -> None:
    record = create_document(tmp_path, _source(tmp_path))
    with pytest.raises(ValueError, match="identity"):
        act_document(
            tmp_path,
            record["document_id"],
            record["revision_id"],
            {
                "type": "add_item",
                "title": "Wrong",
                "source_refs": [
                    {
                        "artifact_id": source_artifact_id(record, "paper.tex"),
                        "file": "different.tex",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ],
            },
        )
    with pytest.raises(ValueError, match="project-relative"):
        act_document(
            tmp_path,
            record["document_id"],
            record["revision_id"],
            {
                "type": "set_mapping",
                "item_id": record["inventory"]["items"][0]["id"],
                "lean_refs": [{"file": "../escaped.lean", "name": "Foo"}],
                "rationale": "Bad path",
            },
        )
    assert load_document(tmp_path, record["document_id"])["revision_id"] == record["revision_id"]


def test_cli_protocol_supports_editor_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _source(tmp_path)
    parser = argparse.ArgumentParser()
    register_formalization_parser(parser.add_subparsers())
    args = parser.parse_args(
        ["formalization", "--project", str(tmp_path), "inspect", "paper.tex", "--json"]
    )
    assert handle_formalization(args) == 0
    snapshot = json.loads(capsys.readouterr().out)
    args = parser.parse_args(
        [
            "formalization",
            "--project",
            str(tmp_path),
            "action",
            snapshot["document_id"],
            "--expected-revision",
            snapshot["revision_id"],
            "--action-json=" + json.dumps(_action(snapshot)),
            "--json",
        ]
    )
    assert handle_formalization(args) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["scope"] == "excluded"
