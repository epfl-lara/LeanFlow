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
    initial = inspect_document(tmp_path, _source(tmp_path))
    private = document_directory(tmp_path, initial["document_id"])
    portable = document_directory(tmp_path, initial["document_id"], published=True)
    shutil.copytree(private, portable)
    shutil.rmtree(private)
    shown = show_document(tmp_path, initial["document_id"])
    assert shown["revision_id"] == initial["revision_id"]
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
