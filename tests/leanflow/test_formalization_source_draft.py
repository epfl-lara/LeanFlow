"""Keep source-uncertain previews inspectable without granting formalization authority."""

from __future__ import annotations

import copy
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import HarnessResult, IsolationPolicy
from core.campaign_limits import CampaignLimits
from leanflow_cli import workflow
from leanflow_cli.formalization import intake, proposal, source_draft, source_draft_recovery
from leanflow_cli.formalization import preparation_launch as launch
from leanflow_cli.formalization.evidence import digest, read_blob
from leanflow_cli.formalization.publication_io import PublicationCancelled, publication_lock
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    document_directory,
    load_document,
    load_receipt,
    save_receipt,
)
from leanflow_cli.formalization.service import (
    act_document,
    list_documents,
    resolve_artifact,
    show_document,
    snapshot,
)
from leanflow_cli.native import harness_source_draft, harness_workflow
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.workflow import parse_workflow_command
from tests.leanflow.test_source_inventory import _pdf


@pytest.fixture
def uncertain_record(tmp_path: Path) -> dict:
    """Freeze a real paired bundle whose executable TeX semantics are uncertain."""
    source = tmp_path / "paper.tex"
    source.write_text(
        r"\documentclass{article}\input{section}"
        r"\ifnum\unknown>0\relax"
        r"\begin{theorem}The first branch claims that zero equals zero.\end{theorem}"
        r"\else\begin{lemma}The second branch claims that one equals one.\end{lemma}\fi"
        r"\RareNotation{x}"
    )
    (tmp_path / "section.tex").write_text(
        r"\begin{definition}A natural number is a mathematical object.\end{definition}"
    )
    companion = tmp_path / "paper.pdf"
    _pdf(companion, ["Theorem 1. Zero equals zero. Lemma 2. One equals one."])
    return create_document(tmp_path, source, companion_pdf=companion)


def test_explicit_draft_retains_both_branches_and_all_source_uncertainty(
    uncertain_record: dict, tmp_path: Path
) -> None:
    inventory = uncertain_record["inventory"]
    before = copy.deepcopy(inventory)
    source_before = {
        name: (tmp_path / name).read_bytes() for name in uncertain_record["source_files"]
    }
    codes = {row["code"] for row in inventory["diagnostics"] if row["blocking"]}
    assert codes >= {
        "unresolved_loader_effect",
        "inherited_tex_meanings",
        "unresolved_conditional",
        "unresolved_command",
    }
    ordinary = source_draft.draft_policy(inventory)
    assert ordinary["strict"]
    selected = source_draft.draft_policy(inventory, explicit=True)
    assert selected["enabled"] and selected["draft_only"]
    assert not selected["strict"]
    assert selected["deferred"] == [row for row in inventory["diagnostics"] if row["blocking"]]
    assert inventory == before
    assert len(inventory["items"]) == 3
    assert all(item["scope"] == "candidate" for item in inventory["items"])
    assert any("first branch" in item["text"] for item in inventory["items"])
    assert any("second branch" in item["text"] for item in inventory["items"])
    assert not inventory["companion_pdf"]["correspondence_verified"]
    assert not inventory["audit_complete"] and inventory["extraction_status"] == "blocked"
    assert {name: (tmp_path / name).read_bytes() for name in source_before} == source_before


def test_explicit_clean_pair_is_still_unaccepted_draft_only(tmp_path: Path) -> None:
    source = tmp_path / "clean.tex"
    source.write_text(r"\begin{theorem}Zero equals zero.\end{theorem}")
    companion = tmp_path / "clean.pdf"
    _pdf(companion, ["Theorem 1. Zero equals zero."])
    record = create_document(tmp_path, source, companion_pdf=companion)
    policy = source_draft.draft_policy(record["inventory"], explicit=True)
    assert policy["enabled"] and policy["draft_only"]
    assert policy["strict"] == policy["deferred"] == []
    assert not source_draft.draft_policy(record["inventory"])["draft_only"]


def test_existing_companion_asset_deferral_remains_automatic_and_unaccepted(tmp_path: Path) -> None:
    source = tmp_path / "assets.tex"
    source.write_text(
        r"\begin{theorem}Zero equals zero.\end{theorem}"
        r"\includegraphics{missing.png}\addbibresource{missing.bib}"
    )
    companion = tmp_path / "assets.pdf"
    _pdf(companion, ["Theorem 1. Zero equals zero. Figure and references."])
    record = create_document(tmp_path, source, companion_pdf=companion)
    before = copy.deepcopy(record["inventory"])
    policy = source_draft.draft_policy(record["inventory"])
    assert policy["enabled"] and policy["draft_only"]
    assert not policy["strict"] and len(policy["deferred"]) == 2
    assert all(
        row["blocking"] and row["code"] == "missing_support_file" for row in policy["deferred"]
    )
    assert record["inventory"] == before


@pytest.mark.parametrize(
    "command",
    [
        "formalize --source-draft-only paper.tex",
        "/formalize paper.tex --source-draft-only",
        '/formalize --source-draft-only "Sources/paper with spaces.tex"',
    ],
)
def test_cli_source_draft_intent_is_separate_from_the_source_path(command: str) -> None:
    workflow = parse_workflow_command(command)
    assert workflow.workflow_kind == "formalize" and workflow.source_draft_only
    assert workflow.workflow_args == (
        "Sources/paper with spaces.tex" if "spaces" in command else "paper.tex"
    )
    assert "--source-draft-only" not in workflow.workflow_args


@pytest.mark.parametrize(
    "command",
    [
        "/prove --source-draft-only Main.lean",
        "/formalize --source-draft-only paper.tex --source-draft-only",
    ],
)
def test_cli_source_draft_intent_cannot_be_duplicated_or_applied_to_other_workflows(
    command: str,
) -> None:
    with pytest.raises(ValueError, match="source-draft-only"):
        parse_workflow_command(command)


@pytest.mark.parametrize("mutation", ["unpaired", "implicit", "hash_mismatch", "not_tex"])
def test_explicit_draft_requires_the_exact_explicitly_frozen_pdf_pair(
    uncertain_record: dict, mutation: str
) -> None:
    inventory = copy.deepcopy(uncertain_record["inventory"])
    if mutation == "unpaired":
        inventory.pop("companion_pdf")
    elif mutation == "implicit":
        inventory["companion_pdf"]["selection"] = "neighboring_filename"
    elif mutation == "hash_mismatch":
        inventory["companion_pdf"]["sha256"] = "different PDF bytes"
    else:
        inventory["source_kind"] = "pdf"
    before = copy.deepcopy(inventory)
    with pytest.raises(ValueError):
        source_draft.draft_policy(inventory, explicit=True)
    assert inventory == before
    assert source_draft.draft_policy(inventory)["strict"]


@pytest.mark.parametrize(
    "code",
    [
        "missing_include",
        "unclosed_environment",
        "source_read_error",
        "encoding_unconfirmed",
        "pdf_text_missing",
        "pdf_metadata_error",
        "include_cycle",
        "unknown_future_blocker",
    ],
)
def test_explicit_draft_never_defers_structural_read_or_pdf_failure(
    uncertain_record: dict, code: str
) -> None:
    inventory = copy.deepcopy(uncertain_record["inventory"])
    hard = {
        "id": "diagnostic-hard",
        "code": code,
        "blocking": True,
        "message": "A source input cannot be faithfully inventoried",
        "file": "paper.tex",
    }
    inventory["diagnostics"].append(hard)
    before = copy.deepcopy(inventory)
    policy = source_draft.draft_policy(inventory, explicit=True)
    assert policy["strict"] == [hard]
    assert inventory == before


@pytest.mark.parametrize(
    "code",
    [
        "unresolved_loader_effect",
        "inherited_tex_meanings",
        "unresolved_conditional",
        "unresolved_command",
    ],
)
def test_explicit_draft_does_not_defer_legacy_diagnostics_without_frozen_spans(
    uncertain_record: dict, code: str
) -> None:
    inventory = copy.deepcopy(uncertain_record["inventory"])
    malformed = next(row for row in inventory["diagnostics"] if row["code"] == code)
    malformed.pop("span", None)
    malformed.pop("spans", None)
    policy = source_draft.draft_policy(inventory, explicit=True)
    assert malformed in policy["strict"]
    assert malformed not in policy["deferred"]


@pytest.mark.parametrize("change", ["unknown_file", "hash", "negative", "past_end", "boolean"])
def test_explicit_draft_requires_authentic_in_bounds_diagnostic_coordinates(
    uncertain_record: dict, change: str
) -> None:
    inventory = copy.deepcopy(uncertain_record["inventory"])
    malformed = next(
        row for row in inventory["diagnostics"] if row["code"] == "unresolved_loader_effect"
    )
    span = malformed["span"]
    if change == "unknown_file":
        span["file"] = "unfrozen.tex"
    elif change == "hash":
        span["file_hash"] = "sha256:" + "f" * 64
    elif change == "negative":
        span["byte_start"] = -1
    elif change == "past_end":
        span["byte_end"] = inventory["files"]["paper.tex"]["size"] + 1
    else:
        span["byte_start"] = False
    policy = source_draft.draft_policy(inventory, explicit=True)
    assert malformed in policy["strict"]
    assert malformed not in policy["deferred"]


def test_explicit_draft_cannot_turn_a_complete_author_proposal_into_acceptance(
    uncertain_record: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = uncertain_record
    snapshot = tmp_path / ".leanflow/author"
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "base_revision_id": record["revision_id"],
        "source_draft_only": True,
        "setup_notes": "The author claims these provisional mappings are complete.",
        "units": [
            {
                "id": "unit-provisional",
                "required_source_ids": [item["id"] for item in record["inventory"]["items"]],
                "declarations": [{"name": "True.intro", "imports": ["Init"]}],
                "representation_note": "Provisional author-supplied correspondence",
            }
        ],
        "coverage": [
            {
                "region_id": region["id"],
                "disposition": "item" if region.get("item_ids") else "supporting_context",
                "item_ids": region.get("item_ids", []),
                "reason": "The author claims all candidate regions were translated",
            }
            for region in record["inventory"]["regions"]
        ],
    }
    (scratch / "formalization-proposal.json").write_text(json.dumps(payload))
    monkeypatch.setattr(
        proposal,
        "resolve_layout",
        lambda *args: pytest.fail("The original source gate must reject before compilation"),
    )
    result = proposal.check_proposal(
        root=tmp_path,
        snapshot=snapshot,
        baseline=source_files(tmp_path),
        directory=tmp_path / ".leanflow/checks",
        document_id=record["document_id"],
        base_revision_id=record["revision_id"],
        selection_token=current_selection_token(tmp_path, record["document_id"]),
        allowed_axioms=(),
    )
    assert not result["accepted"] and result["status"] == "check_rejected"
    assert "Blocking extraction diagnostics" in result["error"]
    selected = load_document(tmp_path, record["document_id"])
    assert selected["author_base_revision"] == record["revision_id"]
    assert selected["inventory"] == record["inventory"] and selected["units"] == []
    assert not selected.get("lean_files")
    receipt = load_receipt(selected)
    assert receipt["status"] == "check_rejected" and receipt["handoff"] is None
    assert not (tmp_path / "formalization").exists()


@pytest.fixture
def preview_inputs(uncertain_record: dict, tmp_path: Path) -> tuple[dict, Path, dict]:
    """Keep author output in scratch while the selected original bundle stays canonical."""
    author = tmp_path / ".leanflow/author"
    module = author / "Main/Document/Setup.lean"
    module.parent.mkdir(parents=True)
    module.write_text(
        "namespace Main.Document\ndef provisionalSetup : Nat := 0\nend Main.Document\n"
    )
    (author / "Main/Document.lean").write_text("import Main.Document.Setup\n")
    notes = author / ".leanflow-job"
    notes.mkdir()
    (notes / "setup-notes.md").write_text("The natural-number representation is provisional.\n")
    (notes / "blockers.md").write_text("Both conditional branches remain candidates.\n")
    (notes / "formalization-proposal.json").write_text('{"accepted": true}')
    (author / "Main/Outside.lean").write_text("def unrelated : Bool := false\n")
    layout = {"owned_prefix": "Main/Document", "aggregator": "Main/Document.lean"}
    return uncertain_record, author, layout


def _freeze(root: Path, record: dict, author: Path, layout_info: dict, **overrides: Any) -> dict:
    """Exercise the controller preview boundary with current selection authority."""
    options = {
        "layout": layout_info,
        "run_id": "preview-run",
        "selection_token": current_selection_token(root, record["document_id"]),
        "model_report": '{"status":"done","accepted":true}',
        **overrides,
    }
    return source_draft.freeze_preview(root, record, author, **options)


def test_frozen_preview_is_visible_and_openable_without_authority(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path
) -> None:
    record, author, layout = preview_inputs
    revision_path = (
        document_directory(tmp_path, record["document_id"])
        / "revisions"
        / f"{record['revision_id']}.json"
    )
    revision_before = revision_path.read_bytes()
    sources_before = {name: (tmp_path / name).read_bytes() for name in record["source_files"]}
    frozen_module = (author / "Main/Document/Setup.lean").read_bytes()
    frozen = _freeze(tmp_path, record, author, layout)
    assert frozen["status"] == "unaccepted_source_uncertain"
    assert source_draft.load_preview(tmp_path, record) == frozen
    (author / "Main/Document/Setup.lean").write_text(
        "-- later model edits are not frozen evidence\n"
    )
    selected = load_document(tmp_path, record["document_id"])
    shown = snapshot(tmp_path, selected)
    assert shown["draft_preview"]["status"] == "unaccepted_source_uncertain"
    assert shown["can_prove"] is False and shown["handoff"] is None
    assert all(not item["lean_refs"] and item["review_status"] != "pass" for item in shown["items"])
    assert selected["revision_id"] == record["revision_id"] and selected["units"] == []
    assert not selected.get("lean_files") and not selected.get("receipt")
    assert load_receipt(selected) is None
    assert revision_path.read_bytes() == revision_before
    assert {name: (tmp_path / name).read_bytes() for name in sources_before} == sources_before
    assert not (tmp_path / "Main/Document/Setup.lean").exists()
    assert not (tmp_path / "formalization").exists()
    artifacts = [row for row in shown["artifacts"] if row["path"] not in record["source_files"]]
    assert {row["path"] for row in artifacts} >= {
        "Main/Document/Setup.lean",
        "Main/Document.lean",
        ".leanflow-job/setup-notes.md",
        ".leanflow-job/blockers.md",
        ".leanflow-job/formalization-proposal.json",
    }
    assert all(row["path"] != "Main/Outside.lean" for row in artifacts)
    proposal = next(
        row for row in artifacts if row["path"] == ".leanflow-job/formalization-proposal.json"
    )
    proposal_path = resolve_artifact(
        tmp_path, record["document_id"], record["revision_id"], proposal["id"]
    )
    assert Path(proposal_path["path"]).read_text() == '{"accepted": true}'
    assert selected.get("corrections", []) == []
    assert selected.get("correction_authorities", []) == []
    module = next(row for row in artifacts if row["path"] == "Main/Document/Setup.lean")
    opened = resolve_artifact(tmp_path, record["document_id"], record["revision_id"], module["id"])
    assert Path(opened["path"]).read_bytes() == frozen_module
    assert Path(opened["path"]).stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("operation", ["load", "show", "snapshot", "artifact"])
def test_historical_preview_browsing_uses_only_frozen_evidence(
    preview_inputs: tuple[dict, Path, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    record, author, layout = preview_inputs
    frozen = _freeze(tmp_path, record, author, layout)

    def no_currentness(*args, **kwargs):
        raise AssertionError(
            "Historical preview browsing must not recover receipts or hash live sources"
        )

    monkeypatch.setattr(source_draft, "load_document", no_currentness)
    monkeypatch.setattr(source_draft, "read_regular_bytes", no_currentness)
    if operation == "load":
        assert source_draft.load_preview(tmp_path, record, verify_current=False) == frozen
    elif operation == "artifact":
        artifact = frozen["artifacts"][0]
        opened = resolve_artifact(
            tmp_path, record["document_id"], record["revision_id"], artifact["id"]
        )
        assert digest(Path(opened["path"]).read_bytes()) == artifact["sha256"]
    else:
        shown = (
            show_document(tmp_path, record["document_id"], verify_current=False)
            if operation == "show"
            else snapshot(tmp_path, record, verify_current=False)
        )
        assert shown["draft_preview"] == frozen
        assert shown["currentness"] == "historical" and shown["source_currentness"] == "unchecked"
        assert not shown["can_prove"] and shown["handoff"] is None


def test_historical_preview_preserves_saved_bytes_after_live_source_changes(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path
) -> None:
    record, author, layout = preview_inputs
    frozen = _freeze(tmp_path, record, author, layout)
    source = tmp_path / "paper.tex"
    source.write_text(source.read_text() + "\nChanged live source.\n")
    assert source_draft.load_preview(tmp_path, record) is None
    assert source_draft.load_preview(tmp_path, record, verify_current=False) == frozen
    shown = show_document(tmp_path, record["document_id"], verify_current=False)
    assert shown["draft_preview"] == frozen and not shown["can_prove"]


def test_modified_preview_metadata_is_not_displayed_as_current_evidence(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path
) -> None:
    record, author, layout = preview_inputs
    _freeze(tmp_path, record, author, layout)
    path = document_directory(tmp_path, record["document_id"]) / "draft-previews/preview-run.json"
    value = json.loads(path.read_text())
    value["status"] = "published"
    path.write_text(json.dumps(value))
    try:
        loaded = source_draft.load_preview(tmp_path, record)
    except ValueError:
        loaded = None
    assert loaded is None
    assert load_document(tmp_path, record["document_id"])["units"] == []
    assert load_receipt(load_document(tmp_path, record["document_id"])) is None


@pytest.mark.parametrize("target", ["pointer", "run"])
def test_malformed_preview_does_not_hide_source_document_or_break_document_listing(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, target: str
) -> None:
    record, author, layout = preview_inputs
    _freeze(tmp_path, record, author, layout)
    name = f"selected-{record['revision_id']}" if target == "pointer" else "preview-run"
    path = document_directory(tmp_path, record["document_id"]) / f"draft-previews/{name}.json"
    path.write_text("{malformed preview JSON")
    shown = show_document(tmp_path, record["document_id"])
    assert shown["document_id"] == record["document_id"]
    assert shown["draft_preview"] is None and shown["draft_preview_error"]
    assert shown["can_prove"] is False and shown["handoff"] is None
    assert {row["path"] for row in shown["artifacts"]} == set(record["source_files"])
    assert shown["items"] and shown["regions"] and shown["diagnostics"]
    listed = list_documents(tmp_path)
    assert len(listed["documents"]) == 1
    assert listed["documents"][0]["document_id"] == record["document_id"]
    assert listed["documents"][0]["draft_preview_error"]


def test_original_source_hardlinks_do_not_prevent_unaccepted_preview_retention(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path
) -> None:
    record, author, layout = preview_inputs
    aliases = tmp_path / "original-source-aliases"
    aliases.mkdir()
    original = {name: (tmp_path / name).read_bytes() for name in record["source_files"]}
    for name in original:
        os.link(tmp_path / name, aliases / Path(name).name)
        assert (tmp_path / name).stat().st_nlink == 2
    frozen = _freeze(tmp_path, record, author, layout)
    assert frozen["accepted"] is False and frozen["proof_authority"] is False
    assert source_draft.load_preview(tmp_path, record) == frozen
    for name, content in original.items():
        assert (tmp_path / name).read_bytes() == content
        assert (aliases / Path(name).name).read_bytes() == content
        assert (tmp_path / name).stat().st_nlink == 2


@pytest.mark.parametrize("change", ["token", "revision"])
def test_a_preview_is_not_attached_after_selection_changes(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, change: str
) -> None:
    record, author, layout = preview_inputs
    _freeze(tmp_path, record, author, layout)
    if change == "token":
        act_document(
            tmp_path,
            record["document_id"],
            record["revision_id"],
            {"type": "select_revision", "revision_id": record["revision_id"]},
        )
        selected = load_document(tmp_path, record["document_id"])
    else:
        with (tmp_path / "paper.tex").open("a") as handle:
            handle.write("\nAdditional source context.\n")
        selected = create_document(tmp_path, tmp_path / "paper.tex")
        assert selected["revision_id"] != record["revision_id"]
    assert source_draft.load_preview(tmp_path, selected) is None
    shown = snapshot(tmp_path, selected)
    assert not shown.get("draft_preview") and not shown["can_prove"]
    assert all(row["path"] in selected["source_files"] for row in shown["artifacts"])


@pytest.mark.parametrize("change", ["token", "source", "revision"])
def test_preview_freezing_rejects_stale_source_or_selection_authority(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, change: str
) -> None:
    record, author, layout = preview_inputs
    token = current_selection_token(tmp_path, record["document_id"])
    if change == "token":
        act_document(
            tmp_path,
            record["document_id"],
            record["revision_id"],
            {"type": "select_revision", "revision_id": record["revision_id"]},
        )
    elif change == "source":
        (tmp_path / "paper.tex").write_text("Changed source bytes.\n")
    else:
        act_document(
            tmp_path,
            record["document_id"],
            record["revision_id"],
            {
                "type": "authorize_proof",
                "item_id": record["inventory"]["items"][0]["id"],
                "authorized": False,
                "reason": "A new user decision requires a separate selected revision",
            },
        )
    with pytest.raises(ValueError):
        _freeze(tmp_path, record, author, layout, selection_token=token)
    assert not list(
        document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json")
    )


def test_source_edit_during_artifact_freezing_prevents_preview_selection(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, author, layout = preview_inputs
    original = source_draft.freeze_artifacts
    user_edit = b"The user changed the canonical source while draft artifacts were frozen.\n"

    def freeze_then_edit(*args, **kwargs):
        artifacts = original(*args, **kwargs)
        (tmp_path / "paper.tex").write_bytes(user_edit)
        return artifacts

    monkeypatch.setattr(source_draft, "freeze_artifacts", freeze_then_edit)
    with pytest.raises(ValueError):
        _freeze(tmp_path, record, author, layout)
    assert (tmp_path / "paper.tex").read_bytes() == user_edit
    assert not list(
        document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json")
    )


@pytest.mark.parametrize("kind", ["symlink", "ancestor_symlink", "hardlink", "fifo"])
def test_preview_freezing_rejects_owned_output_aliases_and_nonregular_inputs(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, kind: str
) -> None:
    record, author, layout = preview_inputs
    target = author / "Main/Document/Setup.lean"
    original = target.read_bytes()
    target.unlink()
    outside = tmp_path / "retained-author-output.lean"
    outside.write_bytes(original)
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "ancestor_symlink":
        target.parent.rmdir()
        target.parent.symlink_to(outside.parent, target_is_directory=True)
    elif kind == "hardlink":
        os.link(outside, target)
    else:
        os.mkfifo(target)
    with pytest.raises(ValueError):
        _freeze(tmp_path, record, author, layout)
    assert outside.read_bytes() == original
    assert not list(
        document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json")
    )


@pytest.mark.parametrize("kind", ["file", "total", "count", "escaped_layout"])
def test_preview_freezing_has_finite_artifact_and_report_bounds(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, kind: str
) -> None:
    record, author, layout = preview_inputs
    overrides = {}
    if kind == "file":
        (author / "Main/Document/Setup.lean").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    elif kind == "total":
        for index in range(5):
            (author / f"Main/Document/Large{index}.lean").write_bytes(b"x" * (2 * 1024 * 1024))
    elif kind == "count":
        for index in range(129):
            (author / f"Main/Document/Part{index}.lean").write_text("-- provisional\n")
    else:
        overrides["layout"] = {"owned_prefix": "../outside", "aggregator": "../outside.lean"}
    with pytest.raises(ValueError):
        _freeze(tmp_path, record, author, layout, **overrides)
    assert not list(
        document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json")
    )


@pytest.mark.parametrize("character", ["x", "α"])
def test_oversized_model_report_retains_lean_preview_with_marked_bounded_summary(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, character: str
) -> None:
    record, author, layout = preview_inputs
    report = character * (128 * 1024) + '\n{"accepted": true, "status": "done"}\n'
    encoded = report.encode()
    frozen = _freeze(tmp_path, record, author, layout, model_report=report)
    assert frozen["accepted"] is False and frozen["proof_authority"] is False
    report_artifact = next(
        row for row in frozen["artifacts"] if row["id"] == frozen["model_report_artifact_id"]
    )
    content = read_blob(
        document_directory(tmp_path, record["document_id"]) / "blobs", report_artifact["sha256"]
    )
    text = content.decode()
    assert len(content) <= 64 * 1024 + 1024
    assert "truncated" in text.lower() and "unaccepted" in text.lower()
    assert str(len(encoded)) in text and digest(encoded) in text
    assert report_artifact["truncated"] is True
    assert report_artifact["original_size_bytes"] == len(encoded)
    assert report_artifact["original_sha256"] == digest(encoded)
    assert any(row["path"] == "Main/Document/Setup.lean" for row in frozen["artifacts"])
    shown = show_document(tmp_path, record["document_id"])
    assert shown["draft_preview"] and not shown["can_prove"]
    assert load_document(tmp_path, record["document_id"])["units"] == []


@pytest.mark.parametrize("entrypoint", ["freeze", "intake", "native"])
@pytest.mark.parametrize("receipt_status", ["published", "accepted_staged"])
def test_source_drafting_refuses_to_shadow_a_selected_published_receipt_before_author_work(
    preview_inputs: tuple[dict, Path, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    receipt_status: str,
) -> None:
    record, author, layout = preview_inputs
    (tmp_path / "paper.tex").write_text(r"\begin{theorem}Zero equals zero.\end{theorem}")
    record = create_document(tmp_path, tmp_path / "paper.tex")
    published = save_receipt(
        record,
        {
            "schema_version": 1,
            "document_id": record["document_id"],
            "revision_id": record["revision_id"],
            "status": receipt_status,
            "checks": {"accepted": True},
            "reviews": [],
            "handoff": {"revision_id": record["revision_id"]},
        },
    )

    def forbidden(*args, **kwargs):
        pytest.fail("A published selection must be rejected before project setup or author work")

    monkeypatch.setattr(intake, "preflight_dependencies", forbidden)
    monkeypatch.setattr(harness_workflow, "run_harness", forbidden)
    monkeypatch.setattr(harness_workflow, "prepare_snapshot", forbidden)
    with pytest.raises(ValueError):
        if entrypoint == "freeze":
            _freeze(tmp_path, record, author, layout)
        elif entrypoint == "intake":
            from leanflow_cli.formalization.formalization_documents import (
                prepare_formalization_document_context,
            )

            prepare_formalization_document_context(
                project_root=tmp_path,
                cwd=tmp_path,
                workflow_args="paper.tex",
                source_draft_only=True,
            )
        else:
            harness_workflow.run_workflow(
                {
                    "LEANFLOW_PROJECT_ROOT": str(tmp_path),
                    "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
                    "LEANFLOW_WORKFLOW_RUN_ID": "published-draft-attempt",
                    "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
                    "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
                    "LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY": "true",
                }
            )
    assert load_receipt(load_document(tmp_path, record["document_id"])) == published
    assert not list(
        document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json")
    )
    assert not list((tmp_path / ".leanflow/native-workflows").glob("*/limits.json"))


def _completed_campaign(root: Path, record: dict, author: Path, run_id: str) -> tuple[dict, Path]:
    """Create persisted charged-and-quiescent author state without launching any native process."""
    campaign = root / ".leanflow/native-workflows" / run_id
    campaign.mkdir(parents=True)
    shutil.copytree(author, campaign / "project")
    for name in record["source_files"]:
        target = campaign / "project" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / name).read_bytes())
    state = {
        "version": 2,
        "run_id": run_id,
        "backend": "codex",
        "workflow": "formalize",
        "baseline": {name: data.hex() for name, data in source_files(root).items()},
        "formalization_selection_token": current_selection_token(root, record["document_id"]),
        "formalization_document_id": record["document_id"],
        "formalization_base_revision": record["revision_id"],
        "source_draft_intent": True,
        "source_draft_only": True,
        "source_draft_completed": True,
        "source_draft_quiescent": True,
        "source_draft_layout": {
            "owned_prefix": "Main/Document",
            "aggregator": "Main/Document.lean",
        },
        "native_status": "completed",
        "status": "blocked",
        "terminal": True,
        "final_response": '{"status":"blocked","reason":"Unverified source semantics"}',
    }
    state_path = campaign / "state.json"
    state_path.write_text(json.dumps(state))
    budget = CampaignLimits(
        campaign / "limits.json", {"agent_calls": 1, "tokens": 1, "wall_time_s": 1}
    )
    invocation = budget.reserve()
    budget.started_call(invocation)
    budget.finish(invocation, {"input_tokens": 1, "output_tokens": 1}, started=True)
    exhausted = json.loads((campaign / "limits.json").read_text())
    exhausted["elapsed_s"] = 10.0
    (campaign / "limits.json").write_text(json.dumps(exhausted))
    environment = {
        "LEANFLOW_PROJECT_ROOT": str(root),
        "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
        "LEANFLOW_NATIVE_RESUME_RUN_ID": run_id,
        "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
        "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
        "LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY": "true",
        "LEANFLOW_PROVER_AGENT_CALLS": "1",
        "LEANFLOW_PROVER_TOKENS": "1",
        "LEANFLOW_PROVER_WALL_TIME_S": "1",
    }
    return environment, state_path


def _forbid_author_relaunch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prohibit inference and project discovery while a completed draft is inspected or retained."""

    def forbidden(*args, **kwargs):
        pytest.fail("Completed draft recovery cannot reserve or launch author/setup work")

    monkeypatch.setattr(CampaignLimits, "reserve", forbidden)
    monkeypatch.setattr(intake, "preflight_dependencies", forbidden)
    monkeypatch.setattr(harness_workflow, "run_harness", forbidden)
    monkeypatch.setattr(harness_workflow, "prepare_snapshot", forbidden)
    monkeypatch.setattr(harness_workflow, "NativeBridge", forbidden)


def test_run_preview_identity_is_independent_of_the_latest_same_base_preview(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, author, layout = preview_inputs
    first = _freeze(tmp_path, record, author, layout, run_id="draft-a")
    environment, state_path = _completed_campaign(tmp_path, record, author, "draft-a")
    state = json.loads(state_path.read_text())
    state["draft_preview"] = first
    state_path.write_text(json.dumps(state))
    (author / "Main/Document/Setup.lean").write_text("def aDifferentProvisionalSetup : Nat := 1\n")
    second = _freeze(tmp_path, record, author, layout, run_id="draft-b")
    assert first["preview_id"] != second["preview_id"]
    assert source_draft.load_preview(tmp_path, record) == second
    assert source_draft.load_run_preview(tmp_path, record, "draft-a") == first
    assert source_draft.load_run_preview(tmp_path, record, "draft-b") == second
    ledger_before = (state_path.parent / "limits.json").read_bytes()
    _forbid_author_relaunch(monkeypatch)
    resumed = harness_workflow.run_workflow(environment)
    assert resumed["draft_preview"] == first
    assert resumed["draft_preview"]["run_id"] == "draft-a"
    assert source_draft.load_preview(tmp_path, record) == second
    assert (state_path.parent / "limits.json").read_bytes() == ledger_before


def test_retained_run_preview_remains_historical_when_selection_authority_changes(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, author, layout = preview_inputs
    first = _freeze(tmp_path, record, author, layout, run_id="draft-a")
    environment, state_path = _completed_campaign(tmp_path, record, author, "draft-a")
    state = json.loads(state_path.read_text())
    state["draft_preview"] = first
    state_path.write_text(json.dumps(state))
    act_document(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        {"type": "select_revision", "revision_id": record["revision_id"]},
    )
    assert source_draft.load_run_preview(tmp_path, record, "draft-a") == first
    assert not source_draft.preview_is_current(tmp_path, record, first)
    _forbid_author_relaunch(monkeypatch)
    resumed = harness_workflow.run_workflow(environment)
    assert resumed["draft_preview"] == first and resumed["draft_preview_attached"] is False
    assert not show_document(tmp_path, record["document_id"])["can_prove"]


def test_an_old_campaign_projection_cannot_adopt_another_runs_preview_when_its_own_recovery_fails(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, author, layout = preview_inputs
    other = _freeze(tmp_path, record, author, layout, run_id="draft-b")
    environment, state_path = _completed_campaign(tmp_path, record, author, "draft-a")
    state = json.loads(state_path.read_text())
    state.update(draft_preview=other, draft_preview_attached=True, source_draft_quiescent=False)
    state_path.write_text(json.dumps(state))
    _forbid_author_relaunch(monkeypatch)
    resumed = harness_workflow.run_workflow(environment)
    assert not resumed.get("draft_preview") and not resumed.get("draft_preview_attached")
    assert resumed.get("draft_retention_error")
    assert source_draft.load_run_preview(tmp_path, record, "draft-a") is None
    assert source_draft.load_run_preview(tmp_path, record, "draft-b") == other


def test_completed_resume_launch_uses_history_when_original_sources_and_project_setup_are_broken(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, author, _ = preview_inputs
    environment, state_path = _completed_campaign(tmp_path, record, author, "draft-launch-history")
    for name in record["source_files"]:
        (tmp_path / name).unlink()
    (tmp_path / "lakefile.toml").write_text("not a supported Lake project declaration\n")
    (tmp_path / "lean-toolchain").write_text("missing/compiler/toolchain\n")
    (tmp_path / ".leanflow/project.yaml").write_text("paths: [unfinished\n")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    for name in (
        "LEANFLOW_FORMALIZATION_BUDGET_PATH",
        "LEANFLOW_FORMALIZATION_CONTEXT",
        "LEANFLOW_FORMALIZATION_MANIFEST",
        "LEANFLOW_FORMALIZATION_EXTRACTED_TEXT",
    ):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail(
            "Completed history launch must not require source intake, configuration limits or project preparation"
        )

    monkeypatch.setattr(
        workflow,
        "resolve_runtime_provider",
        lambda **kwargs: {"provider": "codex", "model": "fixture"},
    )
    monkeypatch.setattr(workflow, "prepare_formalization_document_context", forbidden)
    monkeypatch.setattr(workflow, "discover_leanflow_project", forbidden)
    monkeypatch.setattr(workflow, "plan_state_enabled", lambda: False)
    monkeypatch.setattr(launch, "_select_formalization_document", forbidden)
    monkeypatch.setattr(launch, "create_document", forbidden)
    monkeypatch.setattr(launch, "load_config", forbidden)
    monkeypatch.setattr(launch, "discover_preparation_plan", forbidden)
    monkeypatch.setattr(launch, "prepare_document_project", forbidden)
    monkeypatch.setattr("leanflow_cli.config.load_config", forbidden)
    monkeypatch.setattr(CampaignLimits, "reserve", forbidden)
    plan = workflow.resolve_workflow_request(
        "/formalize --source-draft-only paper.tex", active_cwd=tmp_path
    )
    assert plan.project.root == tmp_path and plan.formalization_document is None
    assert plan.child_env["LEANFLOW_NATIVE_RESUME_RUN_ID"] == "draft-launch-history"
    assert plan.child_env["LEANFLOW_FORMALIZATION_DOCUMENT_ID"] == record["document_id"]
    assert plan.child_env["LEANFLOW_FORMALIZATION_REVISION"] == record["revision_id"]
    assert "LEANFLOW_FORMALIZATION_BUDGET_PATH" not in plan.child_env
    assert "LEANFLOW_FORMALIZATION_CONTEXT" not in plan.child_env
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*.json")
    } == before
    assert state_path.exists() and not (tmp_path / ".leanflow/formalization-campaigns").exists()


@pytest.mark.parametrize("stopped_status", ["limit_reached", "deadline_exceeded", "cancelled"])
def test_completed_quiescent_author_can_resume_bounded_retention_after_termination(
    preview_inputs: tuple[dict, Path, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stopped_status: str,
) -> None:
    record, author, _ = preview_inputs
    environment, state_path = _completed_campaign(tmp_path, record, author, "draft-recovery")
    state = json.loads(state_path.read_text())
    state.update(
        status=stopped_status,
        draft_retention_error="Completed author output was not retained before termination",
        explicit_cancel_requested=stopped_status == "cancelled",
    )
    state_path.write_text(json.dumps(state))
    ledger_before = (state_path.parent / "limits.json").read_bytes()
    _forbid_author_relaunch(monkeypatch)
    resumed = harness_workflow.run_workflow(environment)
    assert resumed["draft_preview"]["run_id"] == "draft-recovery"
    assert resumed["draft_preview"]["accepted"] is False
    assert resumed["draft_preview_attached"] is True
    assert not resumed.get("draft_retention_error")
    assert (
        source_draft.load_run_preview(tmp_path, record, "draft-recovery")
        == resumed["draft_preview"]
    )
    assert (state_path.parent / "limits.json").read_bytes() == ledger_before
    assert len(json.loads(ledger_before)["invocations"]) == 1
    assert not show_document(tmp_path, record["document_id"])["can_prove"]


def test_noncompleted_resume_rejects_changed_effective_draft_mode_without_an_author_call(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, author, _ = preview_inputs
    environment, state_path = _completed_campaign(tmp_path, record, author, "draft-mode-change")
    state = json.loads(state_path.read_text())
    state.update(source_draft_only=False, source_draft_completed=False, native_status="cancelled")
    state_path.write_text(json.dumps(state))
    ledger_before = (state_path.parent / "limits.json").read_bytes()
    _forbid_author_relaunch(monkeypatch)
    with pytest.raises(ValueError):
        harness_workflow.run_workflow(environment)
    assert (state_path.parent / "limits.json").read_bytes() == ledger_before
    assert not list(
        document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json")
    )


def test_unknown_author_shutdown_prevents_local_preview_recovery_without_reroll(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, author, _ = preview_inputs
    environment, state_path = _completed_campaign(
        tmp_path, record, author, "draft-shutdown-unknown"
    )
    state = json.loads(state_path.read_text())
    state["source_draft_quiescent"] = False
    state_path.write_text(json.dumps(state))
    ledger_before = (state_path.parent / "limits.json").read_bytes()
    _forbid_author_relaunch(monkeypatch)
    result = harness_workflow.run_workflow(environment)
    assert not result.get("draft_preview") and result.get("draft_retention_error")
    assert "project" in result["draft_retention_error"]
    assert (state_path.parent / "limits.json").read_bytes() == ledger_before
    assert not list(
        document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json")
    )


def test_a_fresh_interrupt_during_completed_author_recovery_cancels_only_local_retention(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, author, _ = preview_inputs
    environment, state_path = _completed_campaign(tmp_path, record, author, "draft-recovery-stop")
    ledger_before = (state_path.parent / "limits.json").read_bytes()
    previous_handler = signal.getsignal(signal.SIGINT)
    original_freeze = source_draft.freeze_preview
    interruptions = []

    def stop_before_freezing(*args, **kwargs):
        assert signal.getsignal(signal.SIGINT) != previous_handler
        interruptions.append(True)
        signal.raise_signal(signal.SIGINT)
        return original_freeze(*args, **kwargs)

    monkeypatch.setattr(source_draft, "freeze_preview", stop_before_freezing)
    monkeypatch.setattr(source_draft_recovery, "freeze_preview", stop_before_freezing)
    _forbid_author_relaunch(monkeypatch)
    result = harness_workflow.run_workflow(environment)
    assert interruptions == [True]
    assert result["status"] == "cancelled" and result["explicit_cancel_requested"] is True
    assert not result.get("draft_preview") and result.get("draft_retention_error")
    assert signal.getsignal(signal.SIGINT) == previous_handler
    assert (state_path.parent / "limits.json").read_bytes() == ledger_before
    assert not list(
        document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json")
    )


def _queued_preview(
    root_value: str, document_id: str, author_value: str, mode: str, channel: Connection
) -> None:
    """Wait on real project ownership and report cancellation before the holder releases it."""
    import fcntl
    import threading

    from leanflow_cli.formalization import publication_io

    root, author = Path(root_value), Path(author_value)
    record = load_document(root, document_id)
    token = current_selection_token(root, document_id)
    cancelled = threading.Event()
    original_flock = fcntl.flock
    announced = False

    def flock(descriptor: int, operation: int) -> Any:
        nonlocal announced
        if not announced:
            channel.send("waiting")
            announced = True
        return original_flock(descriptor, operation)

    publication_io.fcntl.flock = flock
    listener = None
    if mode == "cancel":

        def receive() -> None:
            assert channel.recv() == "cancel"
            cancelled.set()

        listener = threading.Thread(target=receive)
        listener.start()
    started = time.monotonic()
    try:
        source_draft.freeze_preview(
            root,
            record,
            author,
            layout={"owned_prefix": "Main/Document", "aggregator": "Main/Document.lean"},
            run_id="queued-preview",
            selection_token=token,
            model_report="Unaccepted queued preview",
            cancelled=cancelled.is_set if mode == "cancel" else None,
            deadline=started + 0.2 if mode == "deadline" else None,
        )
        channel.send({"status": "unexpectedly_retained"})
    except PublicationCancelled:
        channel.send({"status": "cancelled", "elapsed": time.monotonic() - started})
    finally:
        if listener is not None:
            listener.join(2)
        channel.close()


@pytest.mark.parametrize("mode", ["cancel", "deadline"])
def test_queued_preview_retention_exits_while_an_unrelated_process_keeps_publication_lock(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, mode: str
) -> None:
    record, author, _ = preview_inputs
    directory = document_directory(tmp_path, record["document_id"])
    blobs_before = {path.name: path.read_bytes() for path in (directory / "blobs").iterdir()}
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(
        target=_queued_preview,
        args=(str(tmp_path), record["document_id"], str(author), mode, remote),
    )
    try:
        with publication_lock(tmp_path):
            process.start()
            remote.close()
            assert local.poll(15) and local.recv() == "waiting"
            if mode == "cancel":
                local.send("cancel")
            assert local.poll(
                2
            ), "Draft cancellation must not wait for the unrelated holder to exit"
            result = local.recv()
            assert result["status"] == "cancelled" and result["elapsed"] < 1
            process.join(2)
            assert process.exitcode == 0
            assert not list(directory.glob("draft-previews/*.json"))
            assert {
                path.name: path.read_bytes() for path in (directory / "blobs").iterdir()
            } == blobs_before
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        local.close()


def _freeze_replaced_fifo(
    root_value: str, document_id: str, author_value: str, path_value: str, channel: Connection
) -> None:
    """Pause after regular-file checks and reject a competing named-pipe substitution."""
    root, author, target = Path(root_value), Path(author_value), Path(path_value)
    record = load_document(root, document_id)
    original_open = os.open

    def opening(value, flags, *args, **kwargs):
        if Path(value) == target:
            channel.send("before_open")
            assert channel.recv() == "continue"
        return original_open(value, flags, *args, **kwargs)

    os.open = opening
    try:
        _freeze(
            root,
            record,
            author,
            {"owned_prefix": "Main/Document", "aggregator": "Main/Document.lean"},
        )
        channel.send("unexpectedly_retained")
    except (OSError, ValueError, RuntimeError) as error:
        channel.send({"rejected": str(error)})
    finally:
        channel.close()


@pytest.mark.parametrize("input_kind", ["source", "owned_output"])
def test_competing_source_or_owned_output_fifo_substitution_cannot_block_preview_retention(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path, input_kind: str
) -> None:
    record, author, _ = preview_inputs
    path = tmp_path / "paper.tex" if input_kind == "source" else author / "Main/Document/Setup.lean"
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(
        target=_freeze_replaced_fifo,
        args=(str(tmp_path), record["document_id"], str(author), str(path), remote),
    )
    process.start()
    remote.close()
    try:
        assert local.poll(15) and local.recv() == "before_open"
        path.unlink()
        os.mkfifo(path)
        local.send("continue")
        assert local.poll(2), "Preview retention blocked on a substituted FIFO without a writer"
        result = local.recv()
        assert isinstance(result, dict) and result.get("rejected")
        process.join(2)
        assert process.exitcode == 0
        assert not list(
            document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json")
        )
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        local.close()


@pytest.mark.parametrize(
    "response",
    [
        '{"status":"blocked","reason":"The loader semantics remain unverified"}',
        '{"status":"done","accepted":true}',
        "The provisional translation is complete.",
    ],
)
@pytest.mark.parametrize("source_kind", ["uncertain", "clean"])
@pytest.mark.parametrize("stop_case", ["ordinary", "manual_cancel"])
def test_explicit_native_draft_charges_one_author_turn_and_resume_only_reads_preview(
    uncertain_record: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    source_kind: str,
    stop_case: str,
) -> None:
    record = uncertain_record
    if source_kind == "clean":
        (tmp_path / "paper.tex").write_text(r"\begin{theorem}Zero equals zero.\end{theorem}")
        record = create_document(tmp_path, tmp_path / "paper.tex")
        assert not any(row["blocking"] for row in record["inventory"]["diagnostics"])
    (tmp_path / "Main.lean").write_text("-- preserved project root\n")
    source_before = {name: (tmp_path / name).read_bytes() for name in record["source_files"]}
    canonical_before = source_files(tmp_path)
    layout = {
        "accepted": True,
        "library": "Main",
        "owned_prefix": "Main/Document",
        "module_prefix": "Main.Document",
        "aggregator": "Main/Document.lean",
    }
    monkeypatch.setattr(intake, "preflight_dependencies", lambda *args: layout)
    monkeypatch.setattr(
        "leanflow_cli.formalization.lean_layout.generation_layout", lambda *args: layout
    )
    requests = []
    bridges = []
    original_bridge = harness_workflow.NativeBridge
    original_freeze = source_draft.freeze_preview
    frozen_calls = []

    def bridge_factory(**kwargs):
        bridge = original_bridge(**kwargs)
        bridges.append(bridge)
        return bridge

    def freeze_after_bridge_close(*args, **kwargs):
        assert bridges and bridges[-1].closed
        assert not bridges[-1].thread.is_alive()
        assert not any(thread.is_alive() for thread in bridges[-1].threads)
        frozen_calls.append(True)
        return original_freeze(*args, **kwargs)

    def prepare(root: Path, target: Path) -> None:
        shutil.copytree(root, target, ignore=shutil.ignore_patterns(".leanflow"))

    def native(request, **kwargs):
        assert not requests, "An unchanged uncertain source must not reserve another author turn"
        requests.append(request)
        assert "DRAFT PREPARATION ONLY" in request.prompt
        assert "Keep both conditional branches" in request.prompt
        request.on_started()
        module = request.cwd / "Main/Document/Setup.lean"
        module.parent.mkdir(parents=True)
        module.write_text("def provisionalSetup : Nat := 0\n")
        (request.cwd / "Main/Document.lean").write_text("import Main.Document.Setup\n")
        (request.cwd / ".leanflow-job/blockers.md").write_text(
            "The loader semantics remain unverified.\n"
        )
        rejection = bridges[-1].callbacks["submit_files"]('{"accepted":true}')
        assert not rejection["accepted"] and rejection["status"] == "source_blocked"
        if stop_case == "manual_cancel":
            signal.raise_signal(signal.SIGINT)
        return HarnessResult(
            "completed", response, "draft-native-session", {}, cleanup_verified=True
        )

    def forbidden(*args, **kwargs):
        pytest.fail("A source-uncertain draft cannot invoke acceptance or review")

    monkeypatch.setattr(harness_workflow, "NativeBridge", bridge_factory)
    monkeypatch.setattr(source_draft, "freeze_preview", freeze_after_bridge_close)
    monkeypatch.setattr(source_draft_recovery, "freeze_preview", freeze_after_bridge_close)
    monkeypatch.setattr(harness_workflow, "prepare_snapshot", prepare)
    monkeypatch.setattr(harness_workflow, "run_harness", native)
    monkeypatch.setattr(
        harness_workflow,
        "build_runtime_isolation",
        lambda backend, cwd, **kwargs: IsolationPolicy(cwd),
    )
    monkeypatch.setattr(harness_workflow, "release_snapshot_dependencies", lambda *args: None)
    monkeypatch.setattr(proposal, "check_proposal", forbidden)
    monkeypatch.setattr("leanflow_cli.formalization.review.review_revision", forbidden)
    environment = {
        "LEANFLOW_PROJECT_ROOT": str(tmp_path),
        "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
        "LEANFLOW_WORKFLOW_RUN_ID": "source-draft-native",
        "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
        "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
        "LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY": "true",
    }
    result = harness_workflow.run_workflow(environment)
    assert result["status"] == ("cancelled" if stop_case == "manual_cancel" else "blocked")
    assert result["source_draft_only"]
    assert result["source_draft_completed"] and result["terminal"]
    assert result["source_draft_quiescent"]
    assert len(requests) == 1
    assert frozen_calls == ([] if stop_case == "manual_cancel" else [True])
    campaign = tmp_path / ".leanflow/native-workflows/source-draft-native"
    ledger_before = (campaign / "limits.json").read_bytes()
    assert len(json.loads(ledger_before)["invocations"]) == 1
    assert not json.loads((campaign / "accepted.json").read_text())["accepted"]
    selected = load_document(tmp_path, record["document_id"])
    assert selected["revision_id"] == record["revision_id"] and selected["units"] == []
    assert selected["inventory"] == record["inventory"]
    assert load_receipt(selected) is None
    shown = snapshot(tmp_path, selected)
    assert not shown["can_prove"] and shown["handoff"] is None
    if stop_case == "manual_cancel":
        assert shown["draft_preview"] is None
        assert result["explicit_cancel_requested"] is True
        assert result.get("draft_retention_error")
    else:
        assert shown["draft_preview"]["run_id"] == "source-draft-native"
        assert any(
            artifact["path"] == "Main/Document/Setup.lean" for artifact in shown["artifacts"]
        )
    monkeypatch.setattr(intake, "preflight_dependencies", forbidden)
    monkeypatch.setattr(harness_workflow, "prepare_snapshot", forbidden)
    resumed = harness_workflow.run_workflow(
        {**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": "source-draft-native"}
    )
    assert resumed["draft_preview"]["run_id"] == "source-draft-native"
    assert len(requests) == 1 and frozen_calls == [True]
    if stop_case == "ordinary":
        assert resumed == result
    assert (campaign / "limits.json").read_bytes() == ledger_before
    assert source_files(tmp_path) == canonical_before
    assert {name: (tmp_path / name).read_bytes() for name in source_before} == source_before


@pytest.mark.parametrize(
    "backend,event",
    [
        ("codex", {"type": "turn.completed"}),
        ("lemex", {"type": "turn.completed"}),
        ("claude", {"type": "result", "subtype": "success", "is_error": False}),
    ],
)
def test_successful_native_terminal_event_spends_turn_without_claiming_process_closure(
    tmp_path: Path, backend: str, event: dict
) -> None:
    state = {"status": "running", "source_draft_completed": False, "source_draft_quiescent": True}
    path = tmp_path / "state.json"
    harness_source_draft.observe_draft_terminal(backend, event, state, path)
    assert state["source_draft_terminal_observed"] is True
    assert state["source_draft_quiescent"] is False
    assert state["source_draft_completed"] is False
    assert json.loads(path.read_text()) == state


@pytest.mark.parametrize(
    "backend,event",
    [
        ("codex", {"type": "turn.failed", "error": {"message": "No successful result"}}),
        ("lemex", {"type": "turn.failed"}),
        ("claude", {"type": "result", "subtype": "success", "is_error": True}),
        ("claude", {"type": "result", "subtype": "error_during_execution", "is_error": False}),
        ("claude", {"type": "result", "subtype": "error_during_execution", "is_error": True}),
        ("claude", {"type": "turn.completed"}),
        ("codex", {"type": "result", "subtype": "success", "is_error": False}),
    ],
)
def test_failed_or_other_provider_events_do_not_mark_a_successful_author_terminal(
    tmp_path: Path, backend: str, event: dict
) -> None:
    state = {"status": "running", "source_draft_completed": False, "source_draft_quiescent": False}
    before = copy.deepcopy(state)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    original = path.read_bytes()
    harness_source_draft.observe_draft_terminal(backend, event, state, path)
    assert state == before and path.read_bytes() == original


@pytest.mark.parametrize(
    "backend,cleanup_outcome",
    [
        ("codex", "throws"),
        ("lemex", "throws"),
        ("claude", "throws"),
        ("codex", "throws_observed"),
        ("claude", "throws_observed"),
        ("codex", "deadline_verified"),
        ("codex", "deadline_unverified"),
        ("codex", "completed_unverified"),
    ],
)
def test_successful_terminal_then_harness_cleanup_crash_cannot_reroll_author_on_resume(
    uncertain_record: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    cleanup_outcome: str,
) -> None:
    record = uncertain_record
    (tmp_path / "Main.lean").write_text("-- preserved canonical project\n")
    original_sources = {name: (tmp_path / name).read_bytes() for name in record["source_files"]}
    baseline = source_files(tmp_path)
    layout = {
        "accepted": True,
        "library": "Main",
        "owned_prefix": "Main/Document",
        "module_prefix": "Main.Document",
        "aggregator": "Main/Document.lean",
    }
    monkeypatch.setattr(intake, "preflight_dependencies", lambda *args: layout)
    monkeypatch.setattr(
        "leanflow_cli.formalization.lean_layout.generation_layout", lambda *args: layout
    )
    if backend == "lemex":
        monkeypatch.setattr(
            "leanflow_cli.runtime.lemex_install.resolve_lemex_executable", lambda: None
        )
        monkeypatch.setattr(
            "leanflow_cli.runtime.lemex_install.managed_distribution_root", lambda: None
        )
    bridges = []
    requests = []
    releases = []
    original_bridge = harness_workflow.NativeBridge
    run_id = f"terminal-crash-{backend}"
    campaign = tmp_path / ".leanflow/native-workflows" / run_id
    state_path = campaign / "state.json"

    def bridge_factory(**kwargs):
        bridge = original_bridge(**kwargs)
        bridges.append(bridge)
        return bridge

    def prepare(root: Path, target: Path) -> None:
        shutil.copytree(root, target, ignore=shutil.ignore_patterns(".leanflow"))

    def native_then_crash(request, **kwargs):
        assert not requests
        requests.append(request)
        request.on_started()
        module = request.cwd / "Main/Document/Setup.lean"
        module.parent.mkdir(parents=True)
        module.write_text("def outputBeforeCleanupCrash : Nat := 0\n")
        event = (
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "terminal-before-crash",
            }
            if backend == "claude"
            else {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
        )
        kwargs["on_event"](event)
        observed = json.loads(state_path.read_text())
        assert observed["source_draft_terminal_observed"] is True
        assert not observed.get("source_draft_completed")
        assert observed["source_draft_quiescent"] is False
        if cleanup_outcome == "throws":
            raise RuntimeError("Simulated native cleanup crash before HarnessResult returned")
        observed_result = HarnessResult(
            "completed" if cleanup_outcome == "completed_unverified" else "deadline_exceeded",
            '{"status":"blocked","reason":"The source interpretation remains uncertain"}',
            "terminal-before-deadline",
            {"input_tokens": 1, "output_tokens": 1},
            cleanup_verified=cleanup_outcome == "deadline_verified",
        )
        if cleanup_outcome == "throws_observed":
            from agent.runtime.harness.models import NativeCleanupUnverified

            raise NativeCleanupUnverified(
                "Cleanup failed after observed terminal usage", result=observed_result
            )
        return observed_result

    def release(*args):
        releases.append(True)
        assert cleanup_outcome == "deadline_verified"

    monkeypatch.setattr(harness_workflow, "NativeBridge", bridge_factory)
    monkeypatch.setattr(harness_workflow, "prepare_snapshot", prepare)
    monkeypatch.setattr(harness_workflow, "run_harness", native_then_crash)
    monkeypatch.setattr(
        harness_workflow,
        "build_runtime_isolation",
        lambda backend, cwd, **kwargs: IsolationPolicy(cwd),
    )
    monkeypatch.setattr(harness_workflow, "release_snapshot_dependencies", release)
    environment = {
        "LEANFLOW_PROJECT_ROOT": str(tmp_path),
        "LEANFLOW_NATIVE_PROVIDER": backend,
        "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
        "LEANFLOW_WORKFLOW_RUN_ID": run_id,
        "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
        "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
        "LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY": "true",
    }
    initial = harness_workflow.run_workflow(environment)
    assert initial["source_draft_terminal_observed"] is True
    if cleanup_outcome == "deadline_verified":
        assert initial["source_draft_quiescent"] is True and releases == [True]
        assert initial["draft_preview"]["accepted"] is False
        assert not initial.get("draft_retention_error") and not initial.get("cache_cleanup_error")
    else:
        assert initial["source_draft_quiescent"] is False and releases == []
        if cleanup_outcome == "throws":
            assert not initial.get("source_draft_completed")
        assert not initial.get("draft_preview") and initial.get("draft_retention_error")
        assert initial.get("cache_cleanup_error")
    assert len(bridges) == 1 and bridges[0].closed
    assert not bridges[0].thread.is_alive()
    assert not any(thread.is_alive() for thread in bridges[0].threads)
    ledger_before = (campaign / "limits.json").read_bytes()
    invocations = list(json.loads(ledger_before)["invocations"].values())
    assert len(invocations) == 1 and invocations[0]["status"] == "finished"
    if cleanup_outcome == "throws":
        assert invocations[0]["usage"] is None
    else:
        assert invocations[0]["usage"] == {"input_tokens": 1, "output_tokens": 1}
    _forbid_author_relaunch(monkeypatch)
    for _ in range(2):
        resumed = harness_workflow.run_workflow(
            {**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": run_id}
        )
        assert resumed["terminal"] and resumed["source_draft_terminal_observed"]
        if cleanup_outcome == "deadline_verified":
            assert (
                resumed["source_draft_quiescent"]
                and resumed["draft_preview"] == initial["draft_preview"]
            )
        else:
            assert not resumed["source_draft_quiescent"]
            if cleanup_outcome == "throws":
                assert not resumed.get("source_draft_completed")
            assert not resumed.get("draft_preview") and resumed.get("draft_retention_error")
        assert (campaign / "limits.json").read_bytes() == ledger_before
        assert bool(
            list(document_directory(tmp_path, record["document_id"]).glob("draft-previews/*.json"))
        ) == (cleanup_outcome == "deadline_verified")
    assert len(requests) == 1 and len(bridges) == 1
    assert (
        campaign / "project/Main/Document/Setup.lean"
    ).read_text() == "def outputBeforeCleanupCrash : Nat := 0\n"
    assert source_files(tmp_path) == baseline
    assert {name: (tmp_path / name).read_bytes() for name in original_sources} == original_sources


def _read_completed_preview_under_unrelated_lock(
    root_value: str, run_id: str, channel: Connection
) -> None:
    """Read existing unaccepted run history without recovering or waiting on canonical publication."""
    from leanflow_cli.formalization import publication

    root = Path(root_value)
    state = json.loads((root / ".leanflow/native-workflows" / run_id / "state.json").read_text())

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Existing draft history cannot perform canonical recovery, authoring or retention"
        )

    publication.recover_pending_publication = forbidden
    publication.assert_no_pending_publication = forbidden
    harness_workflow.run_harness = forbidden
    harness_workflow.prepare_snapshot = forbidden
    intake.preflight_dependencies = forbidden
    CampaignLimits.reserve = forbidden
    source_draft_recovery.freeze_preview = forbidden
    source_draft.freeze_preview = forbidden
    environment = {
        "LEANFLOW_PROJECT_ROOT": str(root),
        "LEANFLOW_NATIVE_PROVIDER": "codex",
        "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
        "LEANFLOW_NATIVE_RESUME_RUN_ID": run_id,
        "LEANFLOW_FORMALIZATION_DOCUMENT_ID": state["formalization_document_id"],
        "LEANFLOW_FORMALIZATION_REVISION": state["formalization_base_revision"],
        "LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY": "true",
    }
    channel.send("started")
    started = time.monotonic()
    try:
        result = harness_workflow.run_workflow(environment)
        channel.send({"result": result, "elapsed": time.monotonic() - started})
    except BaseException as error:
        channel.send({"error": str(error)})
    finally:
        channel.close()


def test_existing_completed_preview_resume_does_not_wait_for_unrelated_canonical_publication(
    preview_inputs: tuple[dict, Path, dict], tmp_path: Path
) -> None:
    from core.utils import atomic_json_write
    from leanflow_cli.runtime.project_operations import project_activity

    record, author, layout = preview_inputs
    frozen = _freeze(tmp_path, record, author, layout, run_id="draft-existing-history")
    _, state_path = _completed_campaign(tmp_path, record, author, "draft-existing-history")
    state = json.loads(state_path.read_text())
    state.update(
        draft_preview=source_draft.load_run_preview(tmp_path, record, "draft-existing-history"),
        draft_preview_attached=True,
    )
    atomic_json_write(state_path, state)
    (state_path.parent / "controller.lock").touch()
    with project_activity(tmp_path):
        pass
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    process = context.Process(
        target=_read_completed_preview_under_unrelated_lock,
        args=(str(tmp_path), "draft-existing-history", remote),
    )
    try:
        with publication_lock(tmp_path):
            process.start()
            remote.close()
            assert local.poll(15) and local.recv() == "started"
            assert local.poll(2), "Reading an existing immutable preview waited on unrelated P"
            payload = local.recv()
            assert "error" not in payload, payload
            assert payload["elapsed"] < 1
            assert payload["result"]["draft_preview"] == frozen
            process.join(2)
            assert process.exitcode == 0
            assert {
                path.relative_to(tmp_path).as_posix(): path.read_bytes()
                for path in tmp_path.rglob("*")
                if path.is_file()
            } == before
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        local.close()


def _launch_unverified_preterminal_actor(
    root_value: str, environment: dict[str, str], channel: Connection
) -> None:
    """Leave one real fixture actor alive when its controller is killed before native callbacks."""
    root = Path(root_value)
    state_path = (
        root / ".leanflow/native-workflows" / environment["LEANFLOW_WORKFLOW_RUN_ID"] / "state.json"
    )
    layout = {
        "accepted": True,
        "library": "Main",
        "owned_prefix": "Main/Document",
        "module_prefix": "Main.Document",
        "aggregator": "Main/Document.lean",
    }
    patch = pytest.MonkeyPatch()
    patch.setattr(intake, "preflight_dependencies", lambda *args: layout)
    patch.setattr("leanflow_cli.formalization.lean_layout.generation_layout", lambda *args: layout)

    def prepare(project: Path, target: Path) -> None:
        shutil.copytree(project, target, ignore=shutil.ignore_patterns(".leanflow"))

    def launch_actor(request, **kwargs):
        state = json.loads(state_path.read_text())
        assert state["native_cleanup_unverified"] is True
        assert state["native_process_cleanup_verified"] is False
        assert not state.get("source_draft_terminal_observed")
        module = request.cwd / "Main/Document/Setup.lean"
        module.parent.mkdir(parents=True)
        module.write_text("def outputBeforeAnyNativeCallback : Nat := 0\n")
        actor = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        channel.send({"actor_pid": actor.pid})
        # No started/event/result callback has happened; the parent now kills this controller.
        while True:
            time.sleep(0.1)

    patch.setattr(harness_workflow, "prepare_snapshot", prepare)
    patch.setattr(harness_workflow, "run_harness", launch_actor)
    patch.setattr(
        harness_workflow,
        "build_runtime_isolation",
        lambda backend, cwd, **kwargs: IsolationPolicy(cwd),
    )
    try:
        harness_workflow.run_workflow(environment)
    except BaseException as error:
        channel.send({"error": str(error)})
    finally:
        patch.undo()
        channel.close()


@pytest.mark.skipif(os.name != "posix", reason="The crash fixture requires POSIX process ownership")
def test_killed_preterminal_controller_preserves_live_actor_inputs_and_blocks_all_resume_setup(
    uncertain_record: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = uncertain_record
    run_id = "preterminal-controller-crash"
    campaign = tmp_path / ".leanflow/native-workflows" / run_id
    environment = {
        "LEANFLOW_PROJECT_ROOT": str(tmp_path),
        "LEANFLOW_NATIVE_PROVIDER": "codex",
        "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
        "LEANFLOW_WORKFLOW_RUN_ID": run_id,
        "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
        "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
        "LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY": "true",
    }
    source_before = source_files(tmp_path)
    context = multiprocessing.get_context("spawn")
    local, remote = context.Pipe()
    controller = context.Process(
        target=_launch_unverified_preterminal_actor,
        args=(str(tmp_path), environment, remote),
    )
    actor_pid = None
    try:
        controller.start()
        remote.close()
        assert local.poll(15), "The controller did not reach native admission"
        payload = local.recv()
        assert "error" not in payload, payload
        actor_pid = payload["actor_pid"]
        os.kill(actor_pid, 0)
        controller.kill()
        controller.join(5)
        assert controller.exitcode == -signal.SIGKILL
        os.kill(actor_pid, 0)
        state = json.loads((campaign / "state.json").read_text())
        assert state["native_cleanup_unverified"] is True
        assert state["native_process_cleanup_verified"] is False
        assert not state.get("source_draft_terminal_observed")
        assert not state.get("source_draft_completed")
        assert not state.get("draft_preview")
        before = {
            path.relative_to(tmp_path).as_posix(): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        ledger_before = (campaign / "limits.json").read_bytes()
        assert len(json.loads(ledger_before)["invocations"]) == 1
        assert (campaign / "project/Main/Document/Setup.lean").read_text() == (
            "def outputBeforeAnyNativeCallback : Nat := 0\n"
        )
        resume = {**environment, "LEANFLOW_NATIVE_RESUME_RUN_ID": run_id}
        _forbid_author_relaunch(monkeypatch)

        def forbidden(*args, **kwargs):
            pytest.fail("Unverified preterminal history must fail before intake or project setup")

        for name in (
            "_select_formalization_document",
            "create_document",
            "load_config",
            "discover_preparation_plan",
            "prepare_document_project",
        ):
            monkeypatch.setattr(launch, name, forbidden)
        monkeypatch.setattr(harness_workflow, "release_snapshot_dependencies", forbidden)
        for _ in range(2):
            with pytest.raises(
                ValueError, match="closure is unverified.*retain its private inputs"
            ):
                harness_workflow.run_workflow(resume)
            with pytest.raises(
                ValueError, match="closure is unverified.*retain its private inputs"
            ):
                launch.prepare_formalization_launch(
                    tmp_path,
                    "paper.tex",
                    {"provider": "codex"},
                    environment=resume,
                    source_draft_only=True,
                )
            os.kill(actor_pid, 0)
            assert (campaign / "limits.json").read_bytes() == ledger_before
            assert {
                path.relative_to(tmp_path).as_posix(): path.read_bytes()
                for path in tmp_path.rglob("*")
                if path.is_file()
            } == before
        assert source_files(tmp_path) == source_before
    finally:
        if controller.is_alive():
            controller.kill()
            controller.join(5)
        if actor_pid is not None:
            try:
                os.kill(actor_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        local.close()


@pytest.mark.parametrize("live_changed", [False, True])
def test_recovered_preview_binds_frozen_evidence_even_when_author_edited_its_source_copy(
    preview_inputs, tmp_path, monkeypatch, live_changed
):
    """Retain historical artifacts without treating the editable author input as source fidelity."""
    record, author, _ = preview_inputs
    environment, state_path = _completed_campaign(tmp_path, record, author, "edited-author-source")
    original = (tmp_path / "paper.tex").read_bytes()
    (state_path.parent / "project/paper.tex").write_text("Author-edited private TeX copy")
    if live_changed:
        (tmp_path / "paper.tex").write_text("Current source changed after the charged turn")
    _forbid_author_relaunch(monkeypatch)
    result = harness_workflow.run_workflow(environment)
    assert result["draft_preview"]["accepted"] is False
    assert result["draft_preview_attached"] is (not live_changed)
    assert result["draft_preview"]["source_bundle"] == record["source_bundle"]
    assert not result.get("draft_retention_error")
    frozen = read_blob(Path(record["_directory"]) / "blobs", record["source_files"]["paper.tex"])
    assert frozen == original
    assert not result["draft_preview"]["proof_authority"]
    assert load_receipt(load_document(tmp_path, record["document_id"])) is None


def test_successful_recovery_replaces_the_old_deferred_cancellation_status(
    preview_inputs, tmp_path, monkeypatch
):
    record, author, _ = preview_inputs
    environment, state_path = _completed_campaign(tmp_path, record, author, "deferred-status")
    state = json.loads(state_path.read_text())
    state.update(
        status="cancelled",
        error="Explicit user cancellation deferred local preview retention; resume to retry without another author",
    )
    state_path.write_text(json.dumps(state))
    _forbid_author_relaunch(monkeypatch)
    result = harness_workflow.run_workflow(environment)
    assert result["status"] == "blocked" and result["draft_preview"]
    assert "saved" in result["error"] and "deferred" not in result["error"]


def test_cancellation_after_durable_preview_save_does_not_report_deferred_retention(
    preview_inputs, tmp_path, monkeypatch
):
    import threading

    record, author, _ = preview_inputs
    environment, state_path = _completed_campaign(tmp_path, record, author, "cancel-after-save")
    state = json.loads(state_path.read_text())
    cancelled = threading.Event()
    original = source_draft_recovery.freeze_preview

    def save_then_cancel(*args, **kwargs):
        preview = original(*args, **kwargs)
        cancelled.set()
        return preview

    monkeypatch.setattr(source_draft_recovery, "freeze_preview", save_then_cancel)
    result = source_draft_recovery.retain_completed_draft(
        tmp_path, state_path.parent, state, environment, cancelled=cancelled.is_set
    )
    assert result["draft_preview"] and result["status"] == "blocked"
    assert result["explicit_cancel_requested"] is True
    assert "deferred" not in result.get("error", "")
    assert (
        source_draft.load_run_preview(tmp_path, record, state["run_id"]) == result["draft_preview"]
    )
