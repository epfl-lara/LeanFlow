"""Deliver actual unaccepted project folders without shrinking source coverage or granting approval."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from leanflow_cli.formalization import draft_delivery, source_draft
from leanflow_cli.formalization.clause_obligations import obligation_registry
from leanflow_cli.formalization.evidence import read_blob
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    document_directory,
    load_document,
    load_receipt,
)
from leanflow_cli.formalization.service import act_document, resolve_artifact, snapshot


@pytest.fixture
def delivery_inputs(tmp_path, monkeypatch):
    """Prepare a real frozen source and an actual controller-allocated scratch project."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    paper = tmp_path / "paper.tex"
    paper.write_text(
        r"\begin{definition}A natural number is an object.\end{definition}"
        "\n"
        r"\begin{theorem}Zero equals zero.\end{theorem}"
    )
    record = create_document(tmp_path, paper)
    item = record["inventory"]["items"][0]
    act_document(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        {
            "type": "record_obligation",
            "item_id": item["id"],
            "source_refs": copy.deepcopy(item["statement_spans"]),
            "description": "The stated object has the required natural-number domain.",
            "reason": "Keep the source domain explicit during source translation review.",
        },
    )
    record = load_document(tmp_path, record["document_id"])
    project = tmp_path / ".leanflow/native-workflows/delivery-run/project"
    module = project / "Main/Document/Statements.lean"
    module.parent.mkdir(parents=True)
    module.write_text("def Paper.object : Nat := 0\ntheorem Paper.result : 0 = 0 := by sorry\n")
    (project / "Main/Document.lean").write_text("import Main.Document.Statements\n")
    (project / "Main.lean").write_text("import Main.Document\n")
    (project / "lakefile.toml").write_text('name = "paper"\n[[lean_lib]]\nname = "Main"\n')
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.19.0\n")
    (project / ".leanflow-job").mkdir()
    ids = [row["id"] for row in record["inventory"]["items"]]
    clause = next(iter(obligation_registry(record)))
    proposal = {
        "accepted": True,
        "publication_authority": True,
        "units": [
            {
                "id": "statements",
                "required_source_ids": ids,
                "declarations": [
                    {"name": "Paper.object", "file": "Main/Document/Statements.lean"},
                    {"name": "Paper.result", "file": "Main/Document/Statements.lean"},
                ],
                "clause_dispositions": [
                    {
                        "obligation_id": clause,
                        "disposition": "unresolved",
                        "declaration_names": ["Paper.object"],
                        "rationale": "The source domain reading remains provisional for independent review.",
                    }
                ],
            }
        ],
        "coverage": [
            {
                "region_id": row["id"],
                "disposition": "unresolved",
                "item_ids": row.get("item_ids", []),
                "reason": "Retained for source review.",
            }
            for row in record["inventory"]["regions"]
        ],
        # Deliberately not admissible to apply_corrections: these remain raw author notes.
        "corrections": [{"id": "raw-typo", "authority_refs": [], "accepted": True}],
    }
    _write_proposal(project, proposal)
    return (
        record,
        project,
        {"owned_prefix": "Main/Document", "aggregator": "Main/Document.lean"},
        proposal,
    )


def _write_proposal(project: Path, proposal: dict) -> None:
    (project / ".leanflow-job/formalization-proposal.json").write_text(json.dumps(proposal))


def _retain(root: Path, inputs: tuple, **overrides) -> dict:
    record, project, layout, _ = inputs
    census = draft_delivery.validate_best_effort(project, record, layout=layout)
    options = dict(
        layout=layout,
        run_id="delivery-run",
        selection_token=current_selection_token(root, record["document_id"]),
        model_report="All statements were authored; source review remains unresolved.",
        check_report={
            "complete": census["complete"],
            "census": census,
            "pending_proofs": ["Paper.result"],
        },
        review_notes={
            "decision": "BLOCK",
            "notes": "Keep the explicit source-domain ambiguity visible.",
        },
        compiled=True,
    )
    options.update(overrides)
    return draft_delivery.retain_best_effort(root, record, project, **options)


def test_actual_project_delivery_preserves_raw_corrections_and_source_without_approval(
    tmp_path,
    delivery_inputs,
):
    record, project, _, proposal = delivery_inputs
    sources = {name: (tmp_path / name).read_bytes() for name in record["source_files"]}
    preview = _retain(tmp_path, delivery_inputs)
    assert preview["status"] == "unaccepted_formalization"
    assert preview["reason"] == "review_ambiguities" and preview["delivery_status"] == "complete"
    assert all(
        preview[key] is False for key in ("accepted", "publication_authority", "proof_authority")
    )
    assert preview["delivery"]["counts"]["unresolved_known_clauses"] == 1
    assert preview["base_revision_id"] == record["revision_id"]
    assert source_draft.load_preview(tmp_path, record) == preview
    census_id = preview["delivery"]["census_artifact_id"]
    artifact = resolve_artifact(tmp_path, record["document_id"], record["revision_id"], census_id)
    census = json.loads(Path(artifact["path"]).read_bytes())
    assert census["raw_proposed_corrections"] == proposal["corrections"]
    assert census["raw_proposal"]["accepted"] is True
    assert census["accepted"] is False and census["complete"] is True
    project_artifact = resolve_artifact(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        preview["delivery"]["project_artifact_id"],
    )
    assert project_artifact["kind"] == "project" and Path(project_artifact["path"]) == project
    assert Path(project_artifact["path"]).is_dir()
    shown = snapshot(tmp_path, load_document(tmp_path, record["document_id"]))
    assert shown["can_prove"] is False and shown["handoff"] is None
    assert load_receipt(record) is None and not record.get("lean_files")
    assert {name: (tmp_path / name).read_bytes() for name in sources} == sources
    assert len([row for row in shown["artifacts"] if row["kind"] == "project"]) == 1


@pytest.mark.parametrize("missing", ["item", "region", "clause", "module", "config"])
def test_partial_drafts_remain_openable_but_never_claim_complete(
    tmp_path, delivery_inputs, missing
):
    record, project, layout, proposal = delivery_inputs
    if missing == "item":
        proposal["units"][0]["required_source_ids"].pop()
    elif missing == "region":
        proposal["coverage"].pop()
    elif missing == "clause":
        proposal["units"][0]["clause_dispositions"] = []
    elif missing == "module":
        (project / "Main/Document/Statements.lean").unlink()
    else:
        (project / "lean-toolchain").unlink()
    _write_proposal(project, proposal)
    census = draft_delivery.validate_best_effort(project, record, layout=layout)
    assert census["complete"] is False
    preview = _retain(tmp_path, delivery_inputs)
    assert preview["delivery_status"] == "incomplete"
    assert (
        resolve_artifact(
            tmp_path,
            record["document_id"],
            record["revision_id"],
            preview["delivery"]["project_artifact_id"],
        )["kind"]
        == "project"
    )


def test_slice_projection_cannot_shrink_full_draft_denominator(tmp_path, delivery_inputs):
    record, project, layout, proposal = delivery_inputs
    record["full_inventory"] = copy.deepcopy(record["inventory"])
    record["inventory"]["items"] = record["inventory"]["items"][:1]
    record["inventory"]["regions"] = record["inventory"]["regions"][:1]
    proposal["units"][0]["required_source_ids"] = [record["inventory"]["items"][0]["id"]]
    proposal["coverage"] = proposal["coverage"][:1]
    _write_proposal(project, proposal)
    census = draft_delivery.validate_best_effort(project, record, layout=layout)
    assert census["complete"] is False
    assert census["counts"]["source_items"] == len(record["full_inventory"]["items"]) == 2
    assert census["missing_source_ids"] and census["missing_region_ids"]


def test_project_resolution_rejects_modified_sources_but_frozen_files_remain_openable(
    tmp_path,
    delivery_inputs,
):
    record, project, _, _ = delivery_inputs
    preview = _retain(tmp_path, delivery_inputs)
    frozen = next(
        row for row in preview["artifacts"] if row["path"] == "Main/Document/Statements.lean"
    )
    (project / frozen["path"]).write_text("def changed : Nat := 1\n")
    with pytest.raises(ValueError, match="changed"):
        resolve_artifact(
            tmp_path,
            record["document_id"],
            record["revision_id"],
            preview["delivery"]["project_artifact_id"],
        )
    file = resolve_artifact(tmp_path, record["document_id"], record["revision_id"], frozen["id"])
    assert "Paper.result" in Path(file["path"]).read_text()


@pytest.mark.parametrize(
    "kind", ["wrong_run", "project_alias", "source_alias", "source_hardlink", "source_edit"]
)
def test_retention_rejects_unallocated_or_modified_project_inputs(tmp_path, delivery_inputs, kind):
    record, project, _, _ = delivery_inputs
    if kind == "wrong_run":
        inputs = (record, project.parent / "other-project", delivery_inputs[2], delivery_inputs[3])
        inputs[1].mkdir()
    else:
        inputs = delivery_inputs
        if kind == "project_alias":
            project.rename(project.parent / "real-project")
            project.symlink_to(project.parent / "real-project", target_is_directory=True)
        elif kind in {"source_alias", "source_hardlink"}:
            path = project / "Main/Document/Statements.lean"
            target = tmp_path / "external.lean"
            target.write_bytes(path.read_bytes())
            path.unlink()
            if kind == "source_alias":
                path.symlink_to(target)
            else:
                os.link(target, path)
        else:
            (project / "paper.tex").write_text("The author changed the original source.")
    with pytest.raises(ValueError):
        _retain(tmp_path, inputs)
    assert source_draft.load_preview(tmp_path, record) is None


def test_dependency_and_build_outputs_do_not_reroll_project_identity(tmp_path, delivery_inputs):
    record, project, _, _ = delivery_inputs
    preview = _retain(tmp_path, delivery_inputs)
    (project / ".lake").mkdir()
    (project / ".lake/large-output.olean").write_text("compiled output changes freely")
    (project / ".lake/packages").symlink_to(tmp_path, target_is_directory=True)
    resolved = resolve_artifact(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        preview["delivery"]["project_artifact_id"],
    )
    assert resolved["path"] == str(project)


def test_project_artifact_cannot_be_forged_from_a_blob(tmp_path, delivery_inputs):
    record, _, _, _ = delivery_inputs
    preview = _retain(tmp_path, delivery_inputs)
    blob = next(row for row in preview["artifacts"] if row["kind"] == "lean")
    forged = {**blob, "kind": "project"}
    with pytest.raises(ValueError):
        draft_delivery.resolve_project_artifact(tmp_path, preview, forged)
    assert read_blob(document_directory(tmp_path, record["document_id"]) / "blobs", blob["sha256"])


def test_author_compile_claim_cannot_complete_unchecked_project(tmp_path, delivery_inputs):
    preview = _retain(tmp_path, delivery_inputs, compiled=False)
    assert preview["delivery_status"] == "incomplete"
    assert preview["delivery"]["coverage_complete"] is True
    assert preview["delivery"]["compiled"] is False


@pytest.mark.parametrize(
    "change", ["source", "selection", "project_during_freeze", "proposal_during_freeze"]
)
def test_retention_rechecks_current_source_selection_and_project_bytes(
    tmp_path,
    delivery_inputs,
    monkeypatch,
    change,
):
    record, project, _, _ = delivery_inputs
    if change == "source":
        (tmp_path / "paper.tex").write_text("A new source version must not attach to this draft.")
    elif change == "selection":
        act_document(
            tmp_path,
            record["document_id"],
            record["revision_id"],
            {
                "type": "record_obligation",
                "item_id": record["inventory"]["items"][1]["id"],
                "source_refs": copy.deepcopy(record["inventory"]["items"][1]["statement_spans"]),
                "description": "The second source statement is a separate proof obligation.",
                "reason": "A newer trusted user action must invalidate the previous selection.",
            },
        )
    else:
        freeze = source_draft.freeze_artifacts

        def freeze_then_edit(*args, **kwargs):
            result = freeze(*args, **kwargs)
            target = (
                "Main.lean"
                if change == "project_during_freeze"
                else ".leanflow-job/formalization-proposal.json"
            )
            (project / target).write_text("-- concurrent edit\n")
            return result

        monkeypatch.setattr(source_draft, "freeze_artifacts", freeze_then_edit)
    with pytest.raises(ValueError):
        _retain(tmp_path, delivery_inputs)
    assert source_draft.load_preview(tmp_path, record) is None


def test_malformed_clause_stays_incomplete_and_raw_notes_remain_preserved(
    tmp_path, delivery_inputs
):
    record, project, layout, proposal = delivery_inputs
    proposal["units"][0]["clause_dispositions"][0]["disposition"] = {"fake": "represented"}
    _write_proposal(project, proposal)
    census = draft_delivery.validate_best_effort(project, record, layout=layout)
    assert census["complete"] is False and census["missing_clause_ids"]
    assert census["raw_proposal"] == proposal


def test_trusted_theorem_exclusion_stays_in_full_audit_without_fake_lean_mapping(
    tmp_path,
    delivery_inputs,
):
    record, project, layout, proposal = delivery_inputs
    theorem = record["inventory"]["items"][1]
    act_document(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        {
            "type": "record_obligation",
            "item_id": theorem["id"],
            "source_refs": copy.deepcopy(theorem["statement_spans"]),
            "description": "The excluded theorem retains its separate source-clause audit identity.",
            "reason": "Exclusion must retain the original source rather than invent a Lean declaration.",
        },
    )
    record = load_document(tmp_path, record["document_id"])
    reason = "This theorem is explicitly out of the requested source scope for this test."
    act_document(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        {
            "type": "set_scope",
            "item_id": theorem["id"],
            "scope": "excluded",
            "reason": reason,
        },
    )
    record = load_document(tmp_path, record["document_id"])
    proposal["units"][0]["required_source_ids"].remove(theorem["id"])
    proposal["units"][0]["declarations"] = proposal["units"][0]["declarations"][:1]
    _write_proposal(project, proposal)
    census = draft_delivery.validate_best_effort(project, record, layout=layout)
    assert census["complete"] is True
    assert census["counts"]["source_items"] == 2
    assert census["counts"]["required_source_items"] == census["counts"]["mapped_source_items"] == 1
    assert census["counts"]["excluded_source_items"] == 1
    assert census["counts"]["known_clauses"] == 2
    assert census["counts"]["excluded_known_clauses"] == 1
    assert census["excluded_items"] == [
        {"source_id": theorem["id"], "scope": "excluded", "actor": "user", "reason": reason}
    ]
    assert theorem["id"] not in census["mapped_source_ids"]
    preview = _retain(tmp_path, (record, project, layout, proposal))
    assert preview["delivery_status"] == "complete"


def test_same_campaign_resume_retains_old_version_and_opens_latest_project(
    tmp_path, delivery_inputs
):
    record, project, _, proposal = delivery_inputs
    first = _retain(tmp_path, delivery_inputs, compiled=False)
    directory = document_directory(tmp_path, record["document_id"])
    old_version = directory / f"draft-previews/versions/{first['preview_id']}.json"
    old_bytes = old_version.read_bytes()
    old_module = next(
        row for row in first["artifacts"] if row["path"] == "Main/Document/Statements.lean"
    )
    old_lean = read_blob(directory / "blobs", old_module["sha256"])
    module = project / "Main/Document/Statements.lean"
    module.write_bytes(old_lean + b"\n-- Resumed author completed the remaining setup.\n")
    proposal["setup_notes"] = "The resumed attempt retained the same campaign and source."
    _write_proposal(project, proposal)
    second = _retain(tmp_path, delivery_inputs)
    assert first["delivery_status"] == "incomplete" and second["delivery_status"] == "complete"
    assert first["preview_id"] != second["preview_id"] and first["run_id"] == second["run_id"]
    assert old_version.read_bytes() == old_bytes
    assert read_blob(directory / "blobs", old_module["sha256"]) == old_lean
    assert len(list((directory / "draft-previews/versions").glob("*.json"))) == 2
    assert source_draft.load_run_preview(tmp_path, record, "delivery-run") == second
    assert (
        source_draft.load_run_preview(
            tmp_path, record, "delivery-run", preview_id=first["preview_id"]
        )
        == first
    )
    assert source_draft.load_preview(tmp_path, record) == second
    opened = resolve_artifact(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        second["delivery"]["project_artifact_id"],
    )
    assert Path(opened["path"]) == project
    assert b"Resumed author" in (Path(opened["path"]) / old_module["path"]).read_bytes()


def test_previous_full_record_format_is_archived_before_latest_pointer_migration(
    tmp_path, delivery_inputs
):
    record, project, _, _ = delivery_inputs
    first = _retain(tmp_path, delivery_inputs, compiled=False)
    directory = document_directory(tmp_path, record["document_id"])
    old_version = directory / f"draft-previews/versions/{first['preview_id']}.json"
    # Characterize the previous shipped full-record location before the versioned pointer.
    (directory / "draft-previews/delivery-run.json").write_bytes(old_version.read_bytes())
    old_version.unlink()
    (project / "Main.lean").write_text("import Main.Document\n-- resumed setup\n")
    latest = _retain(tmp_path, delivery_inputs)
    assert json.loads(old_version.read_bytes()) == first
    assert source_draft.load_run_preview(tmp_path, record, "delivery-run") == latest
    assert (
        source_draft.load_run_preview(
            tmp_path, record, "delivery-run", preview_id=first["preview_id"]
        )
        == first
    )


def test_interrupted_selected_pointer_update_keeps_previous_version_readable(
    tmp_path, delivery_inputs, monkeypatch
):
    record, project, _, _ = delivery_inputs
    first = _retain(tmp_path, delivery_inputs, compiled=False)
    (project / "Main/Document/Statements.lean").write_text(
        "def Paper.object : Nat := 0\ntheorem Paper.result : 0 = 0 := by sorry\n-- resumed\n"
    )
    persist = source_draft.durable_json

    def fail_selected(path, value):
        if path.name == f"selected-{record['revision_id']}.json":
            raise OSError("Injected interruption before selected-pointer replacement")
        persist(path, value)

    monkeypatch.setattr(source_draft, "durable_json", fail_selected)
    with pytest.raises(OSError, match="Injected interruption"):
        _retain(tmp_path, delivery_inputs)
    assert source_draft.load_preview(tmp_path, record) == first
    assert (
        source_draft.load_run_preview(tmp_path, record, "delivery-run")["delivery_status"]
        == "complete"
    )
    monkeypatch.setattr(source_draft, "durable_json", persist)
    latest = _retain(tmp_path, delivery_inputs)
    assert source_draft.load_preview(tmp_path, record) == latest


@pytest.mark.parametrize("corruption", ["version_bytes", "pointer_identity", "pointer_run"])
def test_versioned_delivery_loader_rejects_changed_evidence_or_pointer(
    tmp_path, delivery_inputs, corruption
):
    record, _, _, _ = delivery_inputs
    preview = _retain(tmp_path, delivery_inputs)
    directory = document_directory(tmp_path, record["document_id"])
    pointer = directory / "draft-previews/delivery-run.json"
    if corruption == "version_bytes":
        version = directory / f"draft-previews/versions/{preview['preview_id']}.json"
        value = json.loads(version.read_bytes())
        value["delivery_status"] = "incomplete"
        version.write_text(json.dumps(value))
    else:
        value = json.loads(pointer.read_bytes())
        value["preview_id" if corruption == "pointer_identity" else "run_id"] = "different"
        pointer.write_text(json.dumps(value))
    with pytest.raises((ValueError, OSError)):
        source_draft.load_run_preview(tmp_path, record, "delivery-run")


@pytest.mark.parametrize("change", ["lean", "proposal", "missing_report"])
def test_stale_compilation_before_retention_delivers_incomplete_current_draft(
    tmp_path,
    delivery_inputs,
    change,
):
    record, project, layout, proposal = delivery_inputs
    report = {
        "complete": True,
        "census": draft_delivery.validate_best_effort(project, record, layout=layout),
    }
    if change == "lean":
        module = project / "Main/Document/Statements.lean"
        module.write_text(
            module.read_text() + "\n-- User edited the open draft during advisory review.\n"
        )
    elif change == "proposal":
        proposal["setup_notes"] = (
            "A new provisional source interpretation was added after compilation."
        )
        _write_proposal(project, proposal)
    else:
        report = {"compiled": True}
    preview = _retain(tmp_path, delivery_inputs, check_report=report)
    assert preview["delivery_status"] == "incomplete"
    assert preview["delivery"]["coverage_complete"] is True
    assert preview["delivery"]["compiled"] is False
    assert (
        resolve_artifact(
            tmp_path,
            record["document_id"],
            record["revision_id"],
            preview["delivery"]["project_artifact_id"],
        )["kind"]
        == "project"
    )


@pytest.mark.parametrize("change", ["lean", "proposal"])
def test_post_freeze_edits_cannot_publish_compiled_delivery_pointer(
    tmp_path,
    delivery_inputs,
    monkeypatch,
    change,
):
    record, project, _, proposal = delivery_inputs
    freeze = draft_delivery.freeze_delivery_artifacts

    def freeze_then_edit(*args, **kwargs):
        result = freeze(*args, **kwargs)
        if change == "lean":
            (project / "Main.lean").write_text("-- An editor changed this after report freezing.\n")
        else:
            proposal["setup_notes"] = "The proposal changed during delivery freezing."
            _write_proposal(project, proposal)
        return result

    monkeypatch.setattr(draft_delivery, "freeze_delivery_artifacts", freeze_then_edit)
    with pytest.raises(ValueError, match="changed"):
        _retain(tmp_path, delivery_inputs)
    assert source_draft.load_preview(tmp_path, record) is None
