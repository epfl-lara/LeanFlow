"""Retry failed frozen PDF extraction without losing repaired revisions or physical pages."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from leanflow_cli.formalization.records import create_document, load_document, source_bytes
from leanflow_cli.formalization.service import act_document
from leanflow_cli.formalization.source_pdf import assert_pdf_review_ready


def _tools(monkeypatch: pytest.MonkeyPatch, *, count: int | None, extracted: int = 1) -> None:
    """Model independently successful metadata and failed or partial text extraction."""
    monkeypatch.setattr("leanflow_cli.formalization.source_pdf.shutil.which", lambda name: name)

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[0] == "pdfinfo":
            return subprocess.CompletedProcess(
                command,
                0 if count else 1,
                f"Pages: {count}\n" if count else "",
                "metadata unavailable",
            )
        if not extracted:
            return subprocess.CompletedProcess(command, 1, "", "text extraction failed")
        page = '<page width="612" height="792"><block><line><word xMin="1" yMin="2" xMax="3" yMax="4">Theorem.</word></line></block></page>'
        return subprocess.CompletedProcess(command, 0, "<html>" + page * extracted + "</html>", "")

    monkeypatch.setattr("leanflow_cli.formalization.source_pdf._run", run)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF frozen bytes, tools independently modeled")
    return source


def _transcribe(tmp_path: Path, record: dict, page: int) -> dict:
    act_document(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        {
            "type": "replace_page_transcription",
            "page_index": page,
            "text": f"Complete physical page {page + 1}, including all formulas.",
            "verified": True,
            "reason": "Compared the entire rendered frozen page",
        },
    )
    return load_document(tmp_path, record["document_id"])


def test_raw_transient_failure_retries_after_tools_become_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    monkeypatch.setattr("leanflow_cli.formalization.source_pdf.shutil.which", lambda _: None)
    first = create_document(tmp_path, source)
    assert create_document(tmp_path, source)["revision_id"] == first["revision_id"]
    _tools(monkeypatch, count=1)
    second = create_document(tmp_path, source)
    assert second["revision_id"] != first["revision_id"]
    assert second["parent_revision_id"] == first["revision_id"]
    assert second["inventory"]["page_inventory"]["verified"]
    assert (
        load_document(tmp_path, first["document_id"], first["revision_id"])["inventory"][
            "extraction_status"
        ]
        == "blocked"
    )


def test_user_repairs_require_explicit_frozen_retry_and_remain_in_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    _tools(monkeypatch, count=1, extracted=0)
    first = create_document(tmp_path, source)
    repaired = _transcribe(tmp_path, first, 0)
    _tools(monkeypatch, count=1)
    assert create_document(tmp_path, source)["revision_id"] == repaired["revision_id"]
    original = source.read_bytes()
    source.write_bytes(b"changed live document must not replace frozen input")
    snapshot = act_document(
        tmp_path,
        repaired["document_id"],
        repaired["revision_id"],
        {"type": "retry_extraction", "reason": "Poppler fixed; explicitly reopen my old repairs"},
    )
    current = load_document(tmp_path, repaired["document_id"])
    assert snapshot["revision_id"] == current["revision_id"] != repaired["revision_id"]
    assert current["parent_revision_id"] == repaired["revision_id"]
    assert source_bytes(current, "paper.pdf") == original
    assert not current["inventory"]["pages"][0].get("transcription")
    assert load_document(tmp_path, repaired["document_id"], repaired["revision_id"])["inventory"][
        "pages"
    ][0]["transcription"]["verified"]
    assert (
        not current["units"]
        and not current["checks"]
        and not current["reviews"]
        and current["handoff"] is None
    )


@pytest.mark.parametrize("extracted", [0, 1])
def test_missing_text_pages_are_repairable_and_global_failure_clears_only_after_all_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extracted: int
) -> None:
    source = _source(tmp_path)
    _tools(monkeypatch, count=2, extracted=extracted)
    record = create_document(tmp_path, source)
    assert len(record["inventory"]["pages"]) == 2
    assert record["inventory"]["pages"][1]["text"] == ""
    assert record["inventory"]["regions"][1]["repair_required"]
    partial = _transcribe(tmp_path, record, 0)
    assert any(
        row["blocking"]
        for row in partial["inventory"]["diagnostics"]
        if row["code"] in {"pdf_extraction_failed", "pdf_page_count_mismatch"}
    )
    complete = _transcribe(tmp_path, partial, 1)
    assert not any(
        row["blocking"]
        for row in complete["inventory"]["diagnostics"]
        if row["code"]
        in {
            "pdf_extraction_failed",
            "pdf_page_count_mismatch",
            "pdf_text_missing",
            "pdf_fidelity_unverified",
        }
    )
    assert_pdf_review_ready(complete["inventory"])


def test_unknown_physical_census_cannot_be_authorized_by_transcript_or_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    _tools(monkeypatch, count=None)
    record = _transcribe(tmp_path, create_document(tmp_path, source), 0)
    with pytest.raises(ValueError, match="physical page inventory"):
        assert_pdf_review_ready(record["inventory"])
    region = record["inventory"]["regions"][0]
    act_document(
        tmp_path,
        record["document_id"],
        record["revision_id"],
        {
            "type": "classify_region",
            "region_id": region["id"],
            "classification": "excluded",
            "reason": "This known page is outside scope",
        },
    )
    excluded = load_document(tmp_path, record["document_id"])
    with pytest.raises(ValueError, match="physical page inventory"):
        assert_pdf_review_ready(excluded["inventory"])
    assert any(
        row["blocking"]
        for row in excluded["inventory"]["diagnostics"]
        if row["code"] == "pdf_page_census_unverified"
    )
