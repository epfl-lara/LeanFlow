"""Preserve user repairs, reconcile TeX uncertainty, and keep disposable views private."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from leanflow_cli.formalization.conflict_resolution import (
    publication_conflicts,
    resolve_publication_conflict,
)
from leanflow_cli.formalization.evidence import put_blob
from leanflow_cli.formalization.formalization_documents import (
    prepare_formalization_document_context,
)
from leanflow_cli.formalization.proposal import check_proposal
from leanflow_cli.formalization.publication_journal import load_registry, save_registry
from leanflow_cli.formalization.publication_registry_loss import PublicationAuthorityLoss
from leanflow_cli.formalization.records import (
    DocumentSelectionConflict,
    create_document,
    current_selection_token,
    document_directory,
    load_document,
    new_revision,
    rehydrate_document,
    save_revision,
)
from leanflow_cli.formalization.service import (
    act_document,
    inspect_document,
    resolve_artifact,
    show_document,
)
from leanflow_cli.native.harness_artifacts import source_files


def _source(root: Path, text: str) -> Path:
    source = root / "paper.tex"
    source.write_text(text)
    return source


def _layout(root: Path, document: str) -> dict:
    return {
        "accepted": True,
        "library": "Main",
        "owned_prefix": "Main/Doc",
        "aggregator": "Main/Doc.lean",
        "module_prefix": "Main.Doc",
    }


def test_user_added_statement_cannot_be_overwritten_by_an_inflight_author(tmp_path: Path) -> None:
    base = create_document(
        tmp_path, _source(tmp_path, "\\begin{theorem}True.\\end{theorem}\nA missed statement.\n")
    )
    candidate = new_revision(base)
    candidate.update(author_base_revision=base["revision_id"], submission_id="author-attempt")
    candidate = save_revision(Path(base["_directory"]), candidate)
    viewed = show_document(tmp_path, base["document_id"])
    repaired = act_document(
        tmp_path,
        base["document_id"],
        candidate["revision_id"],
        {
            "type": "add_item",
            "title": "Missed statement",
            "kind": "statement",
            "source_refs": [
                {"artifact_id": viewed["artifacts"][0]["id"], "line_start": 2, "line_end": 2}
            ],
        },
    )
    selected = load_document(tmp_path, base["document_id"])
    assert "author_base_revision" not in selected and "submission_id" not in selected
    result = check_proposal(
        root=tmp_path,
        snapshot=tmp_path / "author",
        baseline={},
        directory=tmp_path / ".leanflow/check",
        document_id=base["document_id"],
        base_revision_id=base["revision_id"],
        selection_token=current_selection_token(tmp_path, base["document_id"]),
        allowed_axioms=(),
    )
    assert not result["accepted"] and "inventory changed" in result["error"]
    after = show_document(tmp_path, base["document_id"])
    assert after["revision_id"] == repaired["revision_id"]
    assert any(item["title"] == "Missed statement" for item in after["items"])


def test_common_latex_and_duplicate_labels_do_not_block_intake(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.lean_build.validate_build_inputs", lambda *a, **kw: {}
    )
    _source(
        tmp_path,
        r"\title{Paper\thanks{Support}}\date{\today}\textsc{Introduction}"
        "\n"
        r"\begin{theorem}\label{same}$\mathbf{x},\dots$ are vectors.\end{theorem}"
        "\n"
        r"\begin{theorem}\label{same}Second occurrence.\end{theorem}",
    )
    monkeypatch.setattr("leanflow_cli.formalization.intake.resolve_layout", _layout)
    context = prepare_formalization_document_context(
        project_root=tmp_path, cwd=tmp_path, workflow_args="paper.tex"
    )
    diagnostics = context.metadata["inventory"]["diagnostics"]
    assert diagnostics and all(not row["blocking"] for row in diagnostics)
    assert diagnostics[0]["code"] == "duplicate_label"
    assert len({item["id"] for item in context.metadata["inventory"]["items"]}) == 2


def test_eligible_command_readings_remain_pending_and_can_be_explicitly_reconciled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.lean_build.validate_build_inputs", lambda *a, **kw: {}
    )
    source = _source(tmp_path, r"\newnotation{A real mathematical statement.}")
    monkeypatch.setattr("leanflow_cli.formalization.intake.resolve_layout", _layout)
    context = prepare_formalization_document_context(
        project_root=tmp_path, cwd=tmp_path, workflow_args="paper.tex"
    )
    manifest = json.loads(context.manifest_path.read_text())
    assert manifest["source_reconciliation_requirements"]["diagnostics"]
    assert any(row["blocking"] for row in context.metadata["inventory"]["diagnostics"])
    assert source.read_text() == r"\newnotation{A real mathematical statement.}"
    initial = inspect_document(tmp_path, source)
    diagnostic = next(row for row in initial["diagnostics"] if row["code"] == "unresolved_command")
    assert diagnostic["can_resolve_command"] and diagnostic["source_refs"]
    action = {
        "type": "resolve_command",
        "diagnostic_id": diagnostic["id"],
        "interpretation": "This command renders its complete argument as mathematical prose.",
        "reason": "Compared every occurrence with the rendered source",
        "verified": True,
    }
    for invalid in ({"verified": False}, {"interpretation": ""}, {"reason": ""}):
        with pytest.raises(ValueError):
            act_document(
                tmp_path, initial["document_id"], initial["revision_id"], {**action, **invalid}
            )
    repaired = act_document(tmp_path, initial["document_id"], initial["revision_id"], action)
    diagnostic = repaired["diagnostics"][0]
    assert not diagnostic["blocking"] and diagnostic["resolution"]["verified"]
    assert diagnostic["resolution"]["source_spans"]
    assert repaired["counts"]["unresolved"] > 0 and not repaired["can_prove"]
    record = load_document(tmp_path, initial["document_id"])
    assert any(region.get("source_interpretations") for region in record["inventory"]["regions"])
    monkeypatch.setattr("leanflow_cli.formalization.intake.resolve_layout", _layout)
    context = prepare_formalization_document_context(
        project_root=tmp_path, cwd=tmp_path, workflow_args="paper.tex"
    )
    assert context.metadata["revision_id"] == repaired["revision_id"]


def test_command_resolution_cannot_waive_missing_include(tmp_path: Path) -> None:
    initial = inspect_document(tmp_path, _source(tmp_path, r"\input{missing}"))
    diagnostic = next(row for row in initial["diagnostics"] if row["blocking"])
    with pytest.raises(ValueError, match="Only an uncertain TeX command"):
        act_document(
            tmp_path,
            initial["document_id"],
            initial["revision_id"],
            {
                "type": "resolve_command",
                "diagnostic_id": diagnostic["id"],
                "interpretation": "Ignore it",
                "reason": "I checked",
                "verified": True,
            },
        )
    assert show_document(tmp_path, initial["document_id"])["revision_id"] == initial["revision_id"]


def test_conditional_interpretation_retains_both_branches_and_unapproved_coverage(tmp_path: Path):
    source = _source(
        tmp_path,
        r"\ifnum\unknownflag=0\relax"
        r"\begin{theorem}\label{first}First alternative.\end{theorem}"
        r"\else\begin{theorem}\label{second}Second alternative.\end{theorem}\fi",
    )
    original = source.read_bytes()
    initial = inspect_document(tmp_path, source)
    before = load_document(tmp_path, initial["document_id"])
    diagnostic = next(
        row for row in initial["diagnostics"] if row["code"] == "unresolved_conditional"
    )
    assert diagnostic["can_resolve_conditional"] and diagnostic["source_refs"]
    action = {
        "type": "resolve_conditional",
        "diagnostic_id": diagnostic["id"],
        "interpretation": "Both alternatives remain in scope; their rendered meanings require review.",
        "reason": "Checked the frozen source and every alternative",
        "verified": True,
    }
    for invalid in ({"verified": False}, {"reason": ""}, {"interpretation": ""}):
        with pytest.raises(ValueError):
            act_document(
                tmp_path, initial["document_id"], initial["revision_id"], {**action, **invalid}
            )
    repaired = act_document(tmp_path, initial["document_id"], initial["revision_id"], action)
    after = load_document(tmp_path, initial["document_id"])
    assert source.read_bytes() == original and after["source_files"] == before["source_files"]
    assert [(item["id"], item["scope"]) for item in after["inventory"]["items"]] == [
        (item["id"], item["scope"]) for item in before["inventory"]["items"]
    ]

    def immutable(region):
        return {
            key: value
            for key, value in region.items()
            if key not in {"disposition", "reason", "source_interpretations"}
        }

    assert [immutable(region) for region in after["inventory"]["regions"]] == [
        immutable(region) for region in before["inventory"]["regions"]
    ]
    assert [
        region["id"]
        for region in after["inventory"]["regions"]
        if region["disposition"] == "excluded"
    ] == [
        region["id"]
        for region in before["inventory"]["regions"]
        if region["disposition"] == "excluded"
    ]
    resolved = next(row for row in repaired["diagnostics"] if row["id"] == diagnostic["id"])
    assert resolved["blocking"] is False and resolved["resolution"]["actor"] == "user"
    assert repaired["can_prove"] is False and repaired["counts"]["unresolved"] > 0
    with pytest.raises(ValueError, match="revision changed"):
        act_document(tmp_path, initial["document_id"], initial["revision_id"], action)


@pytest.mark.parametrize(
    "code", ["unresolved_loader_effect", "inherited_tex_meanings", "missing_support_file"]
)
def test_conditional_interpretation_cannot_waive_other_source_effects(tmp_path: Path, code: str):
    from leanflow_cli.formalization.diagnostic_actions import resolve_conditional

    record = {"inventory": {"diagnostics": [{"id": "diagnostic", "code": code}]}}
    with pytest.raises(ValueError, match="Only an uncertain TeX conditional"):
        resolve_conditional(record, {"diagnostic_id": "diagnostic", "verified": True})


def test_private_cache_cleanup_never_writes_intake_or_lean_viewer_to_portable_tree(
    tmp_path: Path, monkeypatch
) -> None:
    # Simulate an existing local publication whose project-wide ownership
    # index survives removal of the disposable private document cache.
    save_registry(tmp_path, load_registry(tmp_path))
    monkeypatch.setattr(
        "leanflow_cli.formalization.intake.lean_build.validate_build_inputs", lambda *a, **kw: {}
    )
    source = _source(tmp_path, r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, source)
    private = Path(record["_directory"])
    (tmp_path / "Main.lean").write_text("theorem t : True := by sorry\n")
    record["lean_files"] = {
        "Main.lean": put_blob(private / "blobs", (tmp_path / "Main.lean").read_bytes())
    }
    record = save_revision(private, record)
    portable = document_directory(tmp_path, record["document_id"], published=True)
    shutil.copytree(private, portable)
    shutil.rmtree(private)
    before = source_files(tmp_path)
    view = show_document(tmp_path, record["document_id"])
    artifact = next(row for row in view["artifacts"] if row["path"] == "Main.lean")
    opened = resolve_artifact(
        tmp_path, record["document_id"], record["revision_id"], artifact["id"]
    )
    assert Path(opened["path"]).is_relative_to(private)
    monkeypatch.setattr("leanflow_cli.formalization.intake.resolve_layout", _layout)
    context = prepare_formalization_document_context(
        project_root=tmp_path, cwd=tmp_path, workflow_args="paper.tex"
    )
    assert context.context_path.is_relative_to(private)
    assert not (portable / "viewer").exists() and not (portable / "intake").exists()
    assert source_files(tmp_path) == before


def test_new_portable_selection_is_visible_conflict_until_explicit_selection(
    tmp_path: Path,
) -> None:
    record = create_document(tmp_path, _source(tmp_path, r"\begin{theorem}True.\end{theorem}"))
    private = Path(record["_directory"])
    portable = document_directory(tmp_path, record["document_id"], published=True)
    shutil.copytree(private, portable)
    newer = new_revision(record)
    newer["status"] = "published_elsewhere"
    newer = save_revision(portable, newer)
    with pytest.raises(DocumentSelectionConflict):
        load_document(tmp_path, record["document_id"])
    view = show_document(tmp_path, record["document_id"])
    assert view["status"] == "selection_conflict" and not view["can_prove"]
    assert view["selection_conflict"]["published_revision_id"] == newer["revision_id"]
    selection = {"type": "select_revision", "revision_id": newer["revision_id"]}
    with pytest.raises(PublicationAuthorityLoss):
        act_document(tmp_path, record["document_id"], record["revision_id"], selection)
    preserved = {
        path.relative_to(portable): path.read_bytes()
        for path in portable.rglob("*")
        if path.is_file()
    }
    observed = publication_conflicts(tmp_path)["transactions"][0]
    assert observed["unavailable_registry"]
    acknowledgment = resolve_publication_conflict(
        tmp_path,
        {
            **{
                name: observed[name]
                for name in ("transaction_id", "observation_id", "destinations", "retained_slots")
            },
            "choice": "keep_current",
            "reason": "Inspected the imported portable selection; any mathematical authority requires fresh acceptance",
        },
    )
    assert acknowledgment["requires_new_acceptance"]
    assert load_registry(tmp_path)["selected_revision"] is None
    assert preserved == {
        path.relative_to(portable): path.read_bytes()
        for path in portable.rglob("*")
        if path.is_file()
    }
    selected = act_document(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        selection,
    )
    assert selected["revision_id"] == newer["revision_id"] and not selected["selection_conflict"]
    assert not selected["can_prove"]
    historical = act_document(
        tmp_path,
        record["document_id"],
        newer["revision_id"],
        {"type": "select_revision", "revision_id": record["revision_id"]},
    )
    assert (
        historical["revision_id"] == record["revision_id"] and not historical["selection_conflict"]
    )


def test_command_reconciliation_preserves_claims_and_reopens_context_audit(tmp_path: Path) -> None:
    initial = inspect_document(
        tmp_path,
        _source(
            tmp_path,
            "\\mystery{Unclassified context.}\n\\begin{conjecture}\\mystery{Open claim.}\\end{conjecture}",
        ),
    )
    diagnostic = initial["diagnostics"][0]
    assert len(diagnostic["source_refs"]) == 2
    region = next(row for row in initial["regions"] if row["classification"] == "unresolved")
    classified = act_document(
        tmp_path,
        initial["document_id"],
        initial["revision_id"],
        {
            "type": "classify_region",
            "region_id": region["id"],
            "classification": "non_mathematical",
            "reason": "Initial context interpretation",
        },
    )
    authorized = act_document(
        tmp_path,
        initial["document_id"],
        classified["revision_id"],
        {
            "type": "authorize_proof",
            "item_id": initial["items"][0]["id"],
            "authorized": True,
            "reason": "Attempt the source claim",
        },
    )
    repaired = act_document(
        tmp_path,
        initial["document_id"],
        authorized["revision_id"],
        {
            "type": "resolve_command",
            "diagnostic_id": diagnostic["id"],
            "interpretation": "Renders its complete mathematical argument",
            "reason": "Checked both occurrences",
            "verified": True,
        },
    )
    assert len(repaired["items"]) == len(initial["items"])
    assert repaired["items"][0]["proof_authorization"] is None
    assert (
        next(row for row in repaired["regions"] if row["id"] == region["id"])["classification"]
        == "unresolved"
    )
    item = load_document(tmp_path, initial["document_id"])["inventory"]["items"][0]
    assert item["source_interpretations"][0]["interpretation"]


def test_rehydrating_historical_evidence_preserves_newer_portable_selection(tmp_path: Path) -> None:
    base = create_document(tmp_path, _source(tmp_path, r"\begin{theorem}True.\end{theorem}"))
    private = Path(base["_directory"])
    portable = document_directory(tmp_path, base["document_id"], published=True)
    shutil.copytree(private, portable)
    candidate = new_revision(base)
    candidate["status"] = "newer"
    newer = save_revision(portable, candidate)
    shutil.rmtree(private)
    historic = load_document(tmp_path, base["document_id"], base["revision_id"])
    rehydrate_document(historic, private)
    assert load_document(tmp_path, base["document_id"])["revision_id"] == newer["revision_id"]


@pytest.mark.parametrize("corruption", [None, "filename", "conflict"])
def test_portable_review_cache_rehydration_preserves_immutable_decisions(
    tmp_path: Path, corruption: str | None
) -> None:
    record = create_document(tmp_path, _source(tmp_path, r"\begin{theorem}True.\end{theorem}"))
    private = Path(record["_directory"])
    portable = document_directory(tmp_path, record["document_id"], published=True)
    shutil.copytree(private, portable)
    key = "a" * 64
    value = {
        "cache_key": key if corruption != "filename" else "b" * 64,
        "status": "review_rejected",
    }
    (portable / "review-cache").mkdir()
    (portable / "review-cache" / f"{key}.json").write_text(json.dumps(value))
    if corruption == "conflict":
        (private / "review-cache").mkdir()
        (private / "review-cache" / f"{key}.json").write_text(
            json.dumps({**value, "status": "review_passed"})
        )
    remote = {**record, "_directory": str(portable)}
    if corruption:
        with pytest.raises(ValueError, match="(?:cache identity|review evidence disagree)"):
            rehydrate_document(remote, private)
    else:
        rehydrate_document(remote, private)
        assert json.loads((private / "review-cache" / f"{key}.json").read_text()) == value
