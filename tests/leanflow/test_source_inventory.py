"""Exercise full source recovery, exact provenance, and visible extraction uncertainty."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from leanflow_cli.formalization.source_adapters import compare_plastex
from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_provenance import digest


def _tex(tmp_path: Path, text: str, name: str = "main.tex") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _reconstruct(tmp_path: Path, spans: list[dict]) -> str:
    pieces = []
    for span in spans:
        raw = (tmp_path / span["file"]).read_bytes()
        assert digest(raw) == span["file_hash"]
        pieces.append(raw[span["byte_start"] : span["byte_end"]].decode())
    return "".join(pieces)


def _assert_regions_partition_sources(tmp_path: Path, inventory: dict) -> None:
    for occurrence in inventory["include_occurrences"]:
        if occurrence["status"] == "unresolved":
            continue
        regions = sorted(
            (
                region
                for region in inventory["regions"]
                if region.get("occurrence_id") == occurrence["id"]
            ),
            key=lambda region: region["byte_start"],
        )
        cursor = 0
        for region in regions:
            assert region["byte_start"] == cursor
            assert region["byte_end"] > cursor
            cursor = region["byte_end"]
        assert cursor == len((tmp_path / occurrence["file"]).read_bytes())


def test_stress_inventory_has_no_item_statement_proof_or_document_cap(tmp_path: Path) -> None:
    qualifier = " provided the final nontrivial qualifier holds."
    statement = "A statement with unicode α and β. " * 100 + qualifier
    proof = "An unabridged proof step. " * 1200 + "FINAL PROOF CLAUSE."
    source = _tex(
        tmp_path,
        "\n".join(
            rf"\begin{{theorem}}\label{{t:{index}}}{statement}\end{{theorem}}"
            rf"\begin{{proof}}{proof}\end{{proof}}"
            for index in range(165)
        ),
    )
    assert source.stat().st_size > 120_000
    inventory = inventory_source(tmp_path, source)
    assert len(inventory["items"]) == 165
    assert inventory["files"]["main.tex"]["text"] == source.read_text()
    for item in inventory["items"]:
        assert qualifier in item["text"]
        assert len(item["proof_text"]) > 20_000
        assert "FINAL PROOF CLAUSE." in item["proof_text"]
        assert _reconstruct(tmp_path, item["statement_spans"]) == item["text"]
        assert _reconstruct(tmp_path, item["proof_spans"]) == item["proof_text"]
    assert inventory["audit_complete"] is False
    json.dumps(inventory)
    _assert_regions_partition_sources(tmp_path, inventory)


def test_includes_custom_environments_local_packages_and_macro_context(tmp_path: Path) -> None:
    source = _tex(
        tmp_path,
        r"\documentclass{article}\usepackage{local}\input{macros}\begin{document}\input{sections/a}\end{document}",
    )
    _tex(tmp_path, r"\newtheorem{result}{Theorem}", "local.sty")
    _tex(tmp_path, r"\newcommand{\Set}[1]{\{#1\}}", "macros.tex")
    _tex(tmp_path, r"\input{b}", "sections/a.tex")
    _tex(
        tmp_path,
        r"\begin{result}[A result]\label{main}The set $\Set{α}$ exists.\end{result}\begin{proof}Use the definition.\end{proof}",
        "sections/b.tex",
    )
    inventory = inventory_source(tmp_path, source)
    assert set(inventory["files"]) == {
        "main.tex",
        "macros.tex",
        "local.sty",
        "sections/a.tex",
        "sections/b.tex",
    }
    (item,) = inventory["items"]
    assert item["label"] == "main"
    assert item["kind"] == "theorem"
    assert item["title"] == "A result"
    assert item["claim_status"] == "proved_in_source"
    assert len(item["context_refs"]) == 2
    assert not inventory["diagnostics"]
    assert _reconstruct(tmp_path, item["statement_spans"]) == item["text"]
    _assert_regions_partition_sources(tmp_path, inventory)


def test_cross_file_statement_and_proof_have_multiple_original_spans(tmp_path: Path) -> None:
    source = _tex(
        tmp_path,
        r"\begin{theorem}\label{multi}Every \input{middle} holds.\end{theorem}\begin{proof}\input{argument}\end{proof}",
    )
    _tex(tmp_path, "natural number α", "middle.tex")
    _tex(tmp_path, "The proof uses β.", "argument.tex")
    inventory = inventory_source(tmp_path, source)
    (item,) = inventory["items"]
    assert len(item["statement_spans"]) == 3
    assert len(item["proof_spans"]) == 3
    assert "Every natural number α holds." in item["text"]
    assert _reconstruct(tmp_path, item["statement_spans"]) == item["text"]
    _assert_regions_partition_sources(tmp_path, inventory)


def test_comments_literal_and_inactive_fake_statements_are_not_active_claims(
    tmp_path: Path,
) -> None:
    source = _tex(
        tmp_path,
        r"""
% \begin{theorem}\label{comment}Fake.\end{theorem}
\begin{comment}\begin{theorem}Fake.\end{theorem}\end{comment}
\verb|\begin{theorem}Fake.\end{theorem}|
\iffalse \begin{theorem}\label{inactive}Fake.\end{theorem}
  \iftrue \begin{lemma}Nested fake.\end{lemma}\fi
\else \begin{theorem}\label{live}A 50\% result.\end{theorem}\fi
\newcommand{\latent}{\begin{theorem}Not invoked.\end{theorem}}
""",
    )
    inventory = inventory_source(tmp_path, source)
    assert [item["label"] for item in inventory["items"]] == ["live"]
    assert any(region["disposition"] == "excluded" for region in inventory["regions"])
    assert any(region["disposition"] == "unresolved" for region in inventory["regions"])
    _assert_regions_partition_sources(tmp_path, inventory)


def test_unknown_conditionals_and_macro_generated_structure_block(tmp_path: Path) -> None:
    source = _tex(
        tmp_path,
        r"\newcommand{\generated}{\begin{theorem}Generated.\end{theorem}}\generated\ifdraft\begin{lemma}Maybe.\end{lemma}\else\begin{lemma}Alternatively.\end{lemma}\fi",
    )
    inventory = inventory_source(tmp_path, source)
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}
    assert {diagnostic["code"] for diagnostic in inventory["diagnostics"]} >= {
        "unresolved_conditional",
        "macro_generated_structure",
    }
    assert inventory["extraction_status"] == "blocked"


def test_iff_and_boolean_declarations_do_not_open_conditionals(tmp_path: Path) -> None:
    source = _tex(
        tmp_path,
        r"\newif\ifcomments\commentsfalse\begin{theorem}$P\iff Q$.\end{theorem}",
    )
    inventory = inventory_source(tmp_path, source)
    assert not inventory["diagnostics"]
    assert inventory["items"][0]["scope"] == "included"
    assert len(inventory["regions"]) < 10
    _assert_regions_partition_sources(tmp_path, inventory)


def test_custom_boolean_branch_remains_unresolved_without_tex_execution(tmp_path: Path) -> None:
    source = _tex(
        tmp_path,
        r"\newif\ifcomments\commentsfalse\ifcomments\begin{lemma}Maybe.\end{lemma}\fi",
    )
    inventory = inventory_source(tmp_path, source)
    assert inventory["items"][0]["scope"] == "candidate"
    assert any(d["code"] == "unresolved_conditional" for d in inventory["diagnostics"])
    assert not any(d["code"] == "unclosed_conditional" for d in inventory["diagnostics"])


def test_standard_layout_and_math_commands_do_not_hide_unknown_commands(tmp_path: Path) -> None:
    source = _tex(
        tmp_path,
        r"\hypersetup{hidelinks}\setlist[itemize]{leftmargin=*}\theoremstyle{plain}"
        r"\twocolumn\onecolumn\linewidth\FloatBarrier\appendices\balance\begingroup"
        r"\begin{theorem}$\Pr(A)\impliedby B\npreceq C\sqsubseteq D\frown E\upharpoonright F\downarrow G\mathds{1}$.\end{theorem}"
        r"\endgroup\ActualUnknownCommand",
    )
    inventory = inventory_source(tmp_path, source)
    assert len(inventory["diagnostics"]) == 1
    assert "ActualUnknownCommand" in inventory["diagnostics"][0]["message"]


def test_includeonly_repeated_occurrences_and_duplicate_labels(tmp_path: Path) -> None:
    source = _tex(tmp_path, r"\includeonly{keep}\include{skip}\include{keep}\input{keep}")
    _tex(tmp_path, r"\begin{lemma}\label{same}Kept.\end{lemma}", "keep.tex")
    _tex(tmp_path, r"\begin{lemma}\label{skipped}Skipped.\end{lemma}", "skip.tex")
    inventory = inventory_source(tmp_path, source)
    assert len(inventory["items"]) == 2
    assert len({item["id"] for item in inventory["items"]}) == 2
    assert [item["label"] for item in inventory["items"]] == ["same", "same"]
    assert any(diagnostic["code"] == "duplicate_label" for diagnostic in inventory["diagnostics"])
    skipped = [region for region in inventory["regions"] if region["file"] == "skip.tex"]
    assert skipped and {region["disposition"] for region in skipped} == {"excluded"}
    _assert_regions_partition_sources(tmp_path, inventory)


def test_cycles_missing_includes_and_external_symlinks_do_not_disappear(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = _tex(project, r"\input{other}\input{missing}\input{escape}")
    _tex(project, r"\input{main}", "other.tex")
    external = _tex(tmp_path, r"\begin{theorem}Outside.\end{theorem}", "outside.tex")
    (project / "escape.tex").symlink_to(external)
    inventory = inventory_source(project, source)
    assert set(inventory["files"]) == {"main.tex", "other.tex"}
    assert {diagnostic["code"] for diagnostic in inventory["diagnostics"]} == {
        "include_cycle",
        "missing_include",
    }
    assert inventory["extraction_status"] == "blocked"


def test_label_identity_survives_prose_insertions_and_content_changes(tmp_path: Path) -> None:
    source = _tex(tmp_path, r"\begin{lemma}\label{stable}First version.\end{lemma}")
    first = inventory_source(tmp_path, source)
    source.write_text(
        "Unclassified introductory prose.\n" + source.read_text().replace("First", "Second")
    )
    second = inventory_source(tmp_path, source)
    assert first["document_id"] == second["document_id"]
    assert first["items"][0]["id"] == second["items"][0]["id"]
    assert first["items"][0]["content_hash"] != second["items"][0]["content_hash"]
    assert first["bundle_hash"] != second["bundle_hash"]
    assert any(region["disposition"] == "unresolved" for region in second["regions"])


def test_nested_statements_are_bounded_and_proofs_not_truncated(tmp_path: Path) -> None:
    source = _tex(
        tmp_path,
        r"\begin{theorem}\label{outer}Outer.\begin{lemma}\label{inner}Inner.\end{lemma}\begin{proof}Inner argument.\end{proof}Outer qualifier.\end{theorem}",
    )
    inventory = inventory_source(tmp_path, source)
    assert len(inventory["items"]) == 2
    assert "Outer qualifier." in inventory["items"][0]["text"]
    assert "Inner argument." not in inventory["items"][0]["text"]
    assert "Inner argument." in inventory["items"][1]["proof_text"]
    _assert_regions_partition_sources(tmp_path, inventory)


def test_ignored_tail_unknown_commands_and_dynamic_includeonly_are_visible(tmp_path: Path) -> None:
    source = _tex(
        tmp_path,
        r"\includeonly{\chosen}\begin{document}\begin{theorem}Uses \mystery.\end{theorem}\end{document}\begin{lemma}Ignored tail.\end{lemma}",
    )
    inventory = inventory_source(tmp_path, source)
    assert len(inventory["items"]) == 1
    assert {diagnostic["code"] for diagnostic in inventory["diagnostics"]} >= {
        "unresolved_includeonly",
        "unresolved_command",
    }
    assert any(
        reference["name"] == "mystery" and reference["resolution"] == "unresolved"
        for reference in inventory["command_references"]
    )
    assert inventory["regions"][-1]["disposition"] == "excluded"
    _assert_regions_partition_sources(tmp_path, inventory)


def _pdf(path: Path, texts: list[str]) -> None:
    """Write a minimal real PDF without adding optional fixture dependencies."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_ids = []
    for text in texts:
        page_id = len(objects) + 1
        page_ids.append(page_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] /Count {len(page_ids)} >>".encode()
    )
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(data)


@pytest.mark.skipif(
    not shutil.which("pdftotext") or not shutil.which("pdfinfo"), reason="Poppler is unavailable"
)
def test_real_pdf_pages_boxes_and_unrepaired_blank_page(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _pdf(source, ["Theorem 1. Every zero equals zero.", ""])
    inventory = inventory_source(tmp_path, source)
    assert len(inventory["pages"]) == 2
    assert inventory["pages"][0]["boxes"]
    assert inventory["pages"][0]["width"] == 612
    assert inventory["pages"][0]["index"] == 0
    assert len(inventory["items"]) == 1
    assert inventory["items"][0]["scope"] == "candidate"
    assert {region["disposition"] for region in inventory["regions"]} == {"unresolved"}
    assert any(
        diagnostic["code"] == "pdf_text_missing" and diagnostic["page_index"] == 1
        for diagnostic in inventory["diagnostics"]
    )
    assert inventory["regions"][1]["repair_required"] is True
    assert inventory["extraction_status"] == "blocked"


def test_pdf_tool_unavailability_requires_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    _pdf(source, ["Theorem 1. A statement."])
    monkeypatch.setattr("leanflow_cli.formalization.source_pdf.shutil.which", lambda _: None)
    inventory = inventory_source(tmp_path, source)
    assert inventory["extraction_status"] == "blocked"
    assert inventory["regions"][0]["repair_required"]
    assert {diagnostic["code"] for diagnostic in inventory["diagnostics"]} == {
        "pdf_extractor_missing",
        "pdf_pages_unknown",
        "pdf_page_census_unverified",
    }


def test_optional_structural_adapter_disagreement_cannot_grant_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _tex(tmp_path, r"\begin{theorem}A statement.\end{theorem}")
    inventory = inventory_source(tmp_path, source)
    before = json.dumps(inventory, sort_keys=True)
    monkeypatch.setattr(
        "leanflow_cli.formalization.source_adapters.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps({"status": "parsed", "items": []}), ""
        ),
    )
    comparison = compare_plastex(tmp_path, inventory)
    assert comparison["count_agreement"] is False
    assert comparison["grants_coverage"] is False
    assert comparison["boundary_status"] == "unadjudicated"
    assert json.dumps(inventory, sort_keys=True) == before


def test_source_symlink_alias_is_rejected_even_inside_root(tmp_path: Path) -> None:
    source = _tex(tmp_path, r"\begin{lemma}A statement.\end{lemma}")
    link = tmp_path / "alias.tex"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        inventory_source(tmp_path, link)


@pytest.mark.parametrize("relative", ["../outside.tex", "/absolute.tex"])
def test_structural_adapter_rejects_nonrelative_bundle_paths(tmp_path: Path, relative: str) -> None:
    source = _tex(tmp_path, r"\begin{lemma}A statement.\end{lemma}")
    inventory = inventory_source(tmp_path, source)
    inventory["files"][relative] = inventory["files"].pop(source.name)
    with pytest.raises(ValueError, match="project-relative"):
        compare_plastex(tmp_path, inventory)


@pytest.mark.integration
@pytest.mark.parametrize("corpus", ["custom", "repeat_include", "macro_generated"])
def test_real_optional_plastex_corpus_comparison(tmp_path: Path, corpus: str) -> None:
    """Compare explicit miniature gold counts without treating parser agreement as coverage."""
    interpreter = os.environ.get("LEANFLOW_PLASTEX_TEST_PYTHON")
    if not interpreter:
        pytest.skip("Set LEANFLOW_PLASTEX_TEST_PYTHON to an isolated plasTeX interpreter")
    preamble = r"\documentclass{article}\usepackage{amsthm}\newtheorem{claim}{Proposition}"
    statement = r"\begin{claim}\label{claim:first}For each natural number $n$, $n=n$.\end{claim}"
    if corpus == "repeat_include":
        _tex(tmp_path, statement, "part.tex")
        body = r"\input{part}\input{part}"
        expected_scanner, expected_adapter = 2, 2
    elif corpus == "macro_generated":
        preamble += r"\newcommand{\makeclaim}{" + statement + "}"
        body = r"\makeclaim"
        expected_scanner, expected_adapter = 0, 1
    else:
        body = "% \\begin{claim}Commented claim.\\end{claim}\n" + statement
        expected_scanner, expected_adapter = 1, 1
    source = _tex(tmp_path, preamble + r"\begin{document}" + body + r"\end{document}")
    inventory = inventory_source(tmp_path, source)
    comparison = compare_plastex(tmp_path, inventory, python_executable=interpreter)
    assert comparison["status"] == "parsed", comparison
    assert len(inventory["items"]) == expected_scanner
    assert len(comparison["items"]) == expected_adapter
    assert comparison["grants_coverage"] is False and inventory["audit_complete"] is False
    if corpus == "macro_generated":
        assert inventory["extraction_status"] == "blocked"
        assert comparison["count_agreement"] is False
    else:
        assert comparison["count_agreement"] is True
