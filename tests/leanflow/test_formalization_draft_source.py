"""Allow useful companion-backed drafts while unresolved source assets still block acceptance."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from agent.runtime.harness import HarnessResult, IsolationPolicy
from leanflow_cli.formalization import intake, proposal
from leanflow_cli.formalization.formalization_documents import (
    FormalizationDocumentError,
    prepare_formalization_document_context,
)
from leanflow_cli.formalization.records import (
    create_document,
    current_selection_token,
    load_document,
)
from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.native import harness_workflow
from leanflow_cli.native.harness_artifacts import source_files
from tests.leanflow.test_source_inventory import _pdf


@pytest.fixture
def paired_record(tmp_path: Path) -> dict:
    source = tmp_path / "paper.tex"
    source.write_text(
        r"\begin{theorem}Every natural number equals itself.\end{theorem}"
        r"\includegraphics{missing.png}\bibliography{missing}\addbibresource{other.bib}"
    )
    pdf = tmp_path / "paper.pdf"
    _pdf(pdf, ["Theorem 1. Every natural number equals itself. Figure and references."])
    return create_document(tmp_path, source, companion_pdf=pdf)


def test_author_deferral_preserves_all_original_source_diagnostics_and_coverage(
    paired_record: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = paired_record["inventory"]
    original = copy.deepcopy(inventory)
    assert {row["support_command"] for row in inventory["diagnostics"]} == {
        "includegraphics",
        "bibliography",
        "addbibresource",
    }
    assert intake.author_blocking_diagnostics(inventory) == []
    assert inventory == original
    monkeypatch.setattr(intake.lean_build, "validate_build_inputs", lambda *a, **kw: {})
    monkeypatch.setattr(
        intake,
        "resolve_layout",
        lambda *args: {
            "accepted": True,
            "library": "Main",
            "owned_prefix": "Main/Document",
            "aggregator": "Main/Document.lean",
        },
    )
    context = prepare_formalization_document_context(
        project_root=tmp_path, cwd=tmp_path, workflow_args="paper.tex"
    )
    frozen = json.loads(context.manifest_path.read_text())["inventory"]
    assert frozen == original
    assert all(row["blocking"] and "resolution" not in row for row in frozen["diagnostics"])
    assert not frozen["audit_complete"] and frozen["extraction_status"] == "blocked"
    assert frozen["companion_pdf"]["correspondence_verified"] is False
    guidance = context.context_path.read_text()
    assert "DRAFT PREPARATION ONLY" in guidance
    assert 'return exactly {"status":"blocked","reason":' in guidance
    assert "Do not submit a falsely complete proposal" in guidance
    assert all(row["id"] in guidance for row in frozen["diagnostics"])
    assert not context.target_lean_path.exists()


@pytest.mark.parametrize("mutation", ["unpaired", "implicit", "hash_mismatch", "legacy_diagnostic"])
def test_author_deferral_requires_explicit_frozen_pair_and_structured_diagnostic(
    paired_record: dict, mutation: str
) -> None:
    inventory = copy.deepcopy(paired_record["inventory"])
    if mutation == "unpaired":
        inventory.pop("companion_pdf")
    elif mutation == "implicit":
        inventory["companion_pdf"]["selection"] = "neighboring_filename"
    elif mutation == "hash_mismatch":
        inventory["companion_pdf"]["sha256"] = "different PDF"
    else:
        for row in inventory["diagnostics"]:
            row.pop("support_command")
    assert len(intake.author_blocking_diagnostics(inventory)) == 3
    assert not intake.draft_source_guidance(inventory)


@pytest.mark.parametrize(
    "extra",
    [r"\input{missing}", r"\include{missing}", r"\UnknownClaim{x}", r"\begin{lemma}Unclosed"],
)
def test_companion_does_not_defer_includes_macros_or_parse_failures(
    paired_record: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    source = tmp_path / "paper.tex"
    source.write_text(source.read_text() + extra)
    record = create_document(tmp_path, source)
    assert intake.author_blocking_diagnostics(record["inventory"])
    monkeypatch.setattr(
        intake, "preflight_dependencies", lambda *args: pytest.fail("Source repair must stop first")
    )
    with pytest.raises(FormalizationDocumentError, match="Source extraction needs repair"):
        prepare_formalization_document_context(
            project_root=tmp_path, cwd=tmp_path, workflow_args="paper.tex"
        )
    with pytest.raises(ValueError, match="Repair source inventory"):
        harness_workflow.run_workflow(
            {
                "LEANFLOW_PROJECT_ROOT": str(tmp_path),
                "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
                "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
                "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
            }
        )


def test_companion_extraction_failures_are_never_deferred(paired_record: dict) -> None:
    inventory = copy.deepcopy(paired_record["inventory"])
    failure = {"code": "pdf_text_missing", "blocking": True, "message": "Unreadable page"}
    inventory["diagnostics"].append(failure)
    assert intake.author_blocking_diagnostics(inventory) == [failure]


def test_scanner_version_refreshes_old_diagnostics_without_modifying_sources(
    paired_record: dict, tmp_path: Path
) -> None:
    from leanflow_cli.formalization.records import document_directory, save_revision

    original = (tmp_path / "paper.tex").read_bytes()
    legacy = copy.deepcopy(paired_record)
    legacy["inventory"]["extraction_version"] = "provenance-scanner-v3"
    for row in legacy["inventory"]["diagnostics"]:
        row.pop("support_command")
        row.pop("support_target")
    saved = save_revision(document_directory(tmp_path, legacy["document_id"]), legacy)
    refreshed = create_document(tmp_path, tmp_path / "paper.tex")
    assert saved["revision_id"] != refreshed["revision_id"]
    assert not intake.author_blocking_diagnostics(refreshed["inventory"])
    assert (tmp_path / "paper.tex").read_bytes() == original


def test_author_cannot_override_source_blockers_with_complete_proposal_coverage(
    paired_record: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = paired_record
    snapshot = tmp_path / ".leanflow/author"
    scratch = snapshot / ".leanflow-job"
    scratch.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "base_revision_id": record["revision_id"],
        "setup_notes": "The author claims the missing assets do not matter.",
        "units": [
            {
                "id": "unit-claim",
                "required_source_ids": [item["id"] for item in record["inventory"]["items"]],
                "declarations": [{"name": "True.intro", "imports": ["Init"]}],
                "representation_note": "Complete claim requested by the author",
            }
        ],
        "coverage": [
            {
                "region_id": region["id"],
                "disposition": "item" if region.get("item_ids") else "supporting_context",
                "item_ids": region.get("item_ids", []),
                "reason": "Author claims to have covered the complete region",
            }
            for region in record["inventory"]["regions"]
        ],
    }
    (scratch / "formalization-proposal.json").write_text(json.dumps(payload))
    monkeypatch.setattr(
        proposal,
        "resolve_layout",
        lambda *args: pytest.fail("Source gate must run before compiler"),
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
    assert selected["inventory"]["diagnostics"] == record["inventory"]["diagnostics"]
    assert not (tmp_path / "formalization").exists()


@pytest.mark.parametrize(
    "final_response",
    [
        '{"status":"blocked","reason":"Missing original figure assets"}',
        '{"status":"done","accepted":true}',
        "Useful draft saved.",
    ],
)
@pytest.mark.parametrize("explicit_submit", [False, True])
def test_native_draft_only_turn_stops_without_retry_or_promotion(
    paired_record: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    final_response: str,
    explicit_submit: bool,
) -> None:
    record = paired_record
    (tmp_path / "Main.lean").write_text("-- existing root\n")
    monkeypatch.setattr(intake, "preflight_dependencies", lambda *args: {})
    canonical = (tmp_path / "paper.tex").read_bytes()
    calls = []
    bridges = []
    original_bridge = harness_workflow.NativeBridge

    def bridge_factory(**kwargs):
        bridge = original_bridge(**kwargs)
        bridges.append(bridge)
        return bridge

    def prepare(root: Path, snapshot: Path) -> None:
        shutil.copytree(root, snapshot, ignore=shutil.ignore_patterns(".leanflow"))

    def native(request, **kwargs):
        assert not calls, "Known source blockers cannot charge another automatic author turn"
        calls.append(request)
        assert "DRAFT PREPARATION ONLY" in request.prompt
        request.on_started()
        target = request.cwd / "Main/Document/Setup.lean"
        target.parent.mkdir(parents=True)
        target.write_text("def provisionalSetup : Nat := 0\n")
        (request.cwd / ".leanflow-job/blockers.md").write_text("Missing original figure assets.")
        if explicit_submit:
            rejected = bridges[-1].callbacks["submit_files"]("The author claims acceptance")
            assert rejected["accepted"] is False and rejected["status"] == "source_blocked"
            assert rejected["error_code"] == "source_repair_required"
        return HarnessResult(
            "completed",
            final_response,
            "draft",
            {},
        )

    monkeypatch.setattr(harness_workflow, "prepare_snapshot", prepare)
    monkeypatch.setattr(harness_workflow, "NativeBridge", bridge_factory)
    monkeypatch.setattr(harness_workflow, "run_harness", native)
    monkeypatch.setattr(
        harness_workflow,
        "build_runtime_isolation",
        lambda backend, cwd, **kwargs: IsolationPolicy(cwd),
    )
    monkeypatch.setattr(harness_workflow, "release_snapshot_dependencies", lambda root: None)
    monkeypatch.setattr(
        proposal,
        "check_proposal",
        lambda **kwargs: pytest.fail("Known source blockers must not attempt Lean acceptance"),
    )
    monkeypatch.setattr(
        "leanflow_cli.formalization.review.review_revision",
        lambda *args, **kwargs: pytest.fail("Unresolved source must not enter review"),
    )
    result = harness_workflow.run_workflow(
        {
            "LEANFLOW_PROJECT_ROOT": str(tmp_path),
            "LEANFLOW_NATIVE_WORKFLOW_KIND": "formalize",
            "LEANFLOW_FORMALIZATION_DOCUMENT_ID": record["document_id"],
            "LEANFLOW_FORMALIZATION_REVISION": record["revision_id"],
        }
    )
    assert result["status"] == "blocked" and result["source_draft_only"] is True
    if '"status":"blocked"' in final_response:
        assert result["error"] == "Missing original figure assets"
    else:
        assert "Unresolved source assets" in result["error"]
    assert result["source_diagnostics"] == [row["id"] for row in record["inventory"]["diagnostics"]]
    assert len(calls) == 1
    assert (calls[0].cwd / "Main/Document/Setup.lean").exists()
    assert not (tmp_path / "Main/Document/Setup.lean").exists()
    assert (tmp_path / "Main.lean").read_text() == "-- existing root\n"
    assert (tmp_path / "paper.tex").read_bytes() == canonical
    assert not (tmp_path / "formalization").exists()
    selected = load_document(tmp_path, record["document_id"])
    assert selected["revision_id"] == record["revision_id"]
    assert selected["inventory"] == record["inventory"]
    assert inventory_source(tmp_path, tmp_path / "paper.tex")["extraction_status"] == "blocked"
