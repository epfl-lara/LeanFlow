"""Keep explicit TeX/PDF association distinct from correspondence, source repair, and coverage."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from leanflow_cli.cli.formalization_command import (
    handle_formalization,
    register_formalization_parser,
)
from leanflow_cli.formalization import intake, source_pdf
from leanflow_cli.formalization.formalization_documents import (
    _select_formalization_document,
)
from leanflow_cli.formalization.records import create_document, load_document, source_bytes
from leanflow_cli.formalization.service import act_document, inspect_document
from leanflow_cli.formalization.source_companion import refresh_companion_refs
from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_provenance import digest

STATEMENT = "Every selected process sends at least one message before deciding."


@pytest.fixture
def sources(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    tex, pdf = tmp_path / "paper.tex", tmp_path / "rendered.pdf"
    tex.write_text(r"\begin{theorem}" + STATEMENT + r"\end{theorem}")
    pdf.write_bytes(b"Frozen original PDF container")

    def extract(source: Path, relative: str, document_id: str) -> dict:
        raw = source.read_bytes()
        texts = ["Theorem 1. " + STATEMENT, "Unrelated appendix material."]
        if b"mismatch" in raw:
            texts = ["An entirely different document."]
        return {
            "files": {relative: {"sha256": digest(raw), "encoding": "binary", "size": len(raw)}},
            "pages": [
                {"index": index, "label": str(index + 1), "text": text, "boxes": []}
                for index, text in enumerate(texts)
            ],
            "items": [],
            "regions": [
                {
                    "id": f"page-{index}",
                    "file": relative,
                    "file_hash": digest(raw),
                    "page_index": index,
                    "byte_start": 0,
                    "byte_end": len(raw),
                    "coordinate_space": "pdf_page",
                    "disposition": "unresolved",
                    "item_ids": [],
                    "reason": "Page audit",
                }
                for index in range(len(texts))
            ],
            "diagnostics": [],
        }

    monkeypatch.setattr("leanflow_cli.formalization.source_inventory.inventory_pdf", extract)
    return tex, pdf


def test_pair_is_explicit_frozen_and_never_grants_correspondence(sources, tmp_path: Path) -> None:
    tex, pdf = sources
    unpaired = create_document(tmp_path, tex)
    assert set(unpaired["source_files"]) == {tex.name}
    assert not unpaired["inventory"]["pages"]
    shown = inspect_document(tmp_path, tex, companion_pdf=pdf)
    record = load_document(tmp_path, shown["document_id"])
    assert record["inventory"]["source_kind"] == "latex"
    assert record["inventory"]["companion_pdf"]["correspondence_verified"] is False
    assert source_bytes(record, pdf.name) == pdf.read_bytes()
    refs = shown["items"][0]["source_refs"]
    assert refs[0]["file"] == tex.name and refs[0]["precision"] == "exact"
    assert refs[1]["file"] == pdf.name and refs[1]["precision"] == "approximate"
    assert refs[1]["page_index"] == 0 and STATEMENT.rstrip(".").lower() == refs[1]["text"].lower()
    assert refs[1]["method"] == "text_match"
    assert all(
        r["disposition"] == "unresolved"
        for r in record["inventory"]["regions"]
        if r["file"] == pdf.name
    )
    repeated = create_document(tmp_path, tex)
    assert repeated["revision_id"] == record["revision_id"]
    pdf.write_bytes(b"mismatched changed PDF")
    changed = create_document(tmp_path, tex)
    assert changed["revision_id"] != record["revision_id"]
    assert source_bytes(record, pdf.name) != source_bytes(changed, pdf.name)
    ref = inspect_document(tmp_path, tex)["items"][0]["source_refs"][1]
    assert ref["precision"] == "unavailable" and ref["file"] == pdf.name
    assert "page_index" not in ref and "line_start" not in ref


def test_companion_does_not_repair_missing_tex_assets(sources, tmp_path: Path) -> None:
    tex, pdf = sources
    tex.write_text(tex.read_text() + r"\includegraphics{missing.png}\bibliography{missing}")
    inventory = inventory_source(tmp_path, tex, companion_pdf=pdf)
    assert [d["code"] for d in inventory["diagnostics"]] == ["missing_support_file"] * 2
    assert inventory["extraction_status"] == "blocked"


def test_companion_path_must_be_project_contained_and_non_symlink(sources, tmp_path: Path) -> None:
    tex, pdf = sources
    link = tmp_path / "alias.pdf"
    link.symlink_to(pdf)
    with pytest.raises(ValueError, match="symlink"):
        create_document(tmp_path, tex, companion_pdf=link)
    with pytest.raises(ValueError, match="inside"):
        create_document(tmp_path, tex, companion_pdf=tmp_path.parent / "external.pdf")


def test_companion_references_recomputed_after_user_boundary_repair(
    sources, tmp_path: Path
) -> None:
    tex, pdf = sources
    tex.write_text(tex.read_text() + "\nAn unmatched different claim.")
    shown = inspect_document(tmp_path, tex, companion_pdf=pdf)
    updated = act_document(
        tmp_path,
        shown["document_id"],
        shown["revision_id"],
        {
            "type": "update_boundary",
            "item_id": shown["items"][0]["id"],
            "source_refs": [{"file": tex.name, "line_start": 2, "line_end": 2}],
        },
    )
    assert updated["items"][0]["source_refs"][1]["precision"] == "unavailable"


def test_cli_pair_option_and_launch_intake_preserve_association(
    sources, tmp_path: Path, monkeypatch, capsys
) -> None:
    tex, pdf = sources
    parser = argparse.ArgumentParser()
    register_formalization_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "formalization",
            "--project",
            str(tmp_path),
            "inspect",
            tex.name,
            "--companion-pdf",
            pdf.name,
            "--json",
        ]
    )
    assert handle_formalization(args) == 0
    shown = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(intake.lean_build, "validate_build_inputs", lambda *a, **kw: {})
    monkeypatch.setattr(
        intake,
        "resolve_layout",
        lambda *args: {
            "accepted": True,
            "library": "Main",
            "owned_prefix": "Main/Doc",
            "module_prefix": "Main.Doc",
            "aggregator": "Main/Doc.lean",
        },
    )
    selection = _select_formalization_document(tmp_path, tmp_path, tex.name)
    context = intake.prepare_intake(tmp_path, selection)
    assert context.metadata["revision_id"] == shown["revision_id"]
    extracted = context.extracted_text_path.read_text()
    assert "COMPANION PDF rendered.pdf — physical page 1" in extracted
    assert "Unrelated appendix material." in extracted


@pytest.mark.parametrize("page_count", [3, 4])
def test_bbox_failure_falls_back_to_all_physical_pages_without_invented_boxes(
    tmp_path: Path, monkeypatch, page_count: int
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf source")
    monkeypatch.setattr(source_pdf.shutil, "which", lambda name: name)

    def run(command: list[str]) -> subprocess.CompletedProcess:
        if command[0] == "pdfinfo":
            return subprocess.CompletedProcess(command, 0, f"Pages: {page_count}\n", "")
        if "-bbox-layout" in command:
            return subprocess.CompletedProcess(
                command, -6, "partial unusable output", "coordinate extractor crashed"
            )
        assert "-layout" in command
        return subprocess.CompletedProcess(
            command, 0, "Theorem 1. Every result holds.\f\fLast page.\f", ""
        )

    monkeypatch.setattr(source_pdf, "_run", run)
    inventory = inventory_source(tmp_path, pdf)
    assert len(inventory["pages"]) == page_count
    assert [p["text"] for p in inventory["pages"]] == [
        "Theorem 1. Every result holds.",
        "",
        "Last page.",
    ] + ([""] if page_count == 4 else [])
    assert all(
        p["boxes"] == [] and p["mapping_method"] == "poppler_layout" for p in inventory["pages"][:3]
    )
    if page_count == 4:
        assert inventory["pages"][3]["mapping_method"] == "unavailable"
        assert inventory["regions"][3]["repair_required"]
    assert inventory["items"][0]["statement_spans"][0]["mapping_method"] == "poppler_layout"
    warnings = {d["code"]: d for d in inventory["diagnostics"]}
    assert warnings["pdf_layout_fallback"]["blocking"] is False
    assert {
        d["page_index"] for d in inventory["diagnostics"] if d["code"] == "pdf_text_missing"
    } == ({1, 3} if page_count == 4 else {1})
    assert ("pdf_page_count_mismatch" in warnings) == (page_count == 4)


def test_math_only_claims_do_not_acquire_spurious_prose_matches(sources, tmp_path: Path) -> None:
    tex, pdf = sources
    tex.write_text(
        r"\begin{theorem}$$\text{Every selected process sends at least one message before deciding.}$$\end{theorem}"
    )
    inventory = inventory_source(tmp_path, tex, companion_pdf=pdf)
    assert inventory["items"][0]["pdf_refs"][0]["mapping_precision"] == "unavailable"


def test_repeated_prose_retains_ambiguous_pages_and_original_unicode_offsets(
    sources, tmp_path: Path
) -> None:
    tex, pdf = sources
    inventory = inventory_source(tmp_path, tex, companion_pdf=pdf)
    inventory["pages"][0]["text"] = "İ introduction. " + STATEMENT
    inventory["pages"][1]["text"] = "Another copy: " + STATEMENT
    refresh_companion_refs(inventory)
    refs = inventory["items"][0]["pdf_refs"]
    assert [ref["page_index"] for ref in refs] == [0, 1]
    assert all(ref["mapping_precision"] == "approximate" for ref in refs)
    assert all(ref["text"] == STATEMENT.rstrip(".") for ref in refs)
