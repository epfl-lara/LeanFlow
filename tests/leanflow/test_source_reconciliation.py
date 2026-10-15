"""Keep lexical source proposals separate from original evidence and independent approval."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_provenance import digest
from leanflow_cli.formalization.source_reconciliation import (
    MAX_INTERPRETATION_CHARACTERS,
    apply_reconciliation,
    assert_reconciliation_reviewed,
    compact_requirements,
    lexical_admission,
    reconciliation_requirements,
)


def record(tmp_path: Path) -> dict:
    source = tmp_path / "main.tex"
    source.write_text(
        r"\documentclass{article}\begin{document}\iftrue "
        r"\begin{theorem}\label{one}\UnresolvedSymbol is positive.\end{theorem}"
        r"\else\begin{theorem}\label{two}An alternative holds.\end{theorem}\fi\end{document}"
    )
    inventory = inventory_source(tmp_path, source)
    return {
        "revision_id": "rev-base",
        "source_bundle": inventory["bundle_hash"],
        "inventory": inventory,
    }


def proposal(value: dict) -> dict:
    requirements = reconciliation_requirements(value)
    return {
        "requirements_id": requirements["requirements_id"],
        "entries": [
            {
                "diagnostic_id": row["diagnostic_id"],
                "interpretation": "Read the literal source and retain both conditional alternatives.",
                "reason": "Independent complete-source review must determine this lexical reading.",
            }
            for row in requirements["diagnostics"]
        ],
    }


def reviews(value: dict) -> list[dict]:
    return [
        {
            "revision_id": value["revision_id"],
            "cleanup_verified": True,
            "accepted": True,
            "coherence_decision": "PASS",
            "coverage": [
                {"region_id": row["id"], "decision": "PASS", "rationale": "Exact source checked"}
                for row in value["inventory"]["regions"]
            ],
        }
    ]


def test_ordinary_latex_admits_a_pending_lexical_proposal_without_pdf_or_approval(tmp_path) -> None:
    value = record(tmp_path)
    original = copy.deepcopy(value["inventory"])
    assert lexical_admission(original) == []
    assert original["extraction_status"] == "blocked"
    apply_reconciliation(value, proposal(value))
    assert value["full_inventory"] == original
    assert value["inventory"]["items"] == original["items"]
    assert value["inventory"]["regions"] == original["regions"]
    assert {row["label"] for row in value["inventory"]["items"]} == {"one", "two"}
    assert value["source_reconciliation"]["verified"] is False
    assert value["source_reconciliation"]["actor"] == "author_proposal"
    assert all(row["blocking"] for row in original["diagnostics"])
    with pytest.raises(ValueError, match="fresh independently"):
        assert_reconciliation_reviewed(value, [])
    assert_reconciliation_reviewed(value, reviews(value))


def test_compact_requirements_retain_all_meanings_and_the_full_identity_binding(tmp_path) -> None:
    value = record(tmp_path)
    apply_reconciliation(value, proposal(value))
    full = value["source_reconciliation"]
    before = copy.deepcopy(full)
    compact = compact_requirements(full)
    assert compact["requirements_id"] == full["requirements_id"]
    assert compact["entries"] == full["entries"]
    assert compact["diagnostics"] == full["diagnostics"]
    assert compact["affected_region_count"] == len(full["affected_region_ids"])
    assert compact["candidate_source_count"] == len(full["candidate_source_ids"])
    assert compact["complete_membership_evidence_id"] == "source-reconciliation-requirements"
    assert "affected_region_ids" not in compact and "candidate_source_ids" not in compact
    assert full == before


@pytest.mark.parametrize(
    "code",
    [
        "unsupported_tex_effect",
        "unsupported_expansion",
        "unsupported_tex_preprocessing",
        "redefined_scanner_control",
        "unparsed_definition",
        "unmatched_environment",
        "unclosed_conditional",
        "source_read_error",
        "unresolved_includeonly",
        "missing_support_file",
        "pdf_text_fidelity_unverified",
        "environment_structure_limit",
    ],
)
def test_hard_effects_assets_and_structure_never_receive_lexical_waivers(tmp_path, code) -> None:
    value = record(tmp_path)
    diagnostic = copy.deepcopy(value["inventory"]["diagnostics"][0])
    diagnostic.update(id="hard", code=code)
    value["inventory"]["diagnostics"].append(diagnostic)
    assert lexical_admission(value["inventory"]) == [diagnostic]
    supplied = proposal(value)
    supplied["entries"].append(
        {"diagnostic_id": "hard", "interpretation": "Ignore it", "reason": "No"}
    )
    with pytest.raises(ValueError):
        apply_reconciliation(value, supplied)


@pytest.mark.parametrize(
    "tamper",
    ["span", "hash", "revision", "source", "census", "duplicate", "missing", "user", "long"],
)
def test_proposal_requires_exact_current_complete_frozen_bindings(tmp_path, tamper) -> None:
    value = record(tmp_path)
    supplied = proposal(value)
    if tamper == "span":
        value["inventory"]["diagnostics"][0]["span"]["byte_end"] += 1
    elif tamper == "hash":
        value["inventory"]["files"]["main.tex"]["sha256"] = "different"
    elif tamper == "revision":
        value["revision_id"] = "rev-new"
    elif tamper == "source":
        value["source_bundle"] = "replacement"
    elif tamper == "census":
        value["inventory"]["regions"].pop()
    elif tamper == "duplicate":
        supplied["entries"].append(supplied["entries"][0])
    elif tamper == "missing":
        supplied["entries"].pop()
    elif tamper == "user":
        supplied["entries"][0]["verified"] = True
    else:
        supplied["entries"][0]["interpretation"] = "x" * (MAX_INTERPRETATION_CHARACTERS + 1)
    with pytest.raises(ValueError):
        apply_reconciliation(value, supplied)


@pytest.mark.parametrize(
    "tamper",
    [
        "missing",
        "duplicate",
        "block",
        "coherence",
        "cleanup",
        "revision",
        "note",
        "after_source",
        "narrowed",
        "malformed",
    ],
)
def test_no_partial_stale_or_unclosed_review_resolves_the_source_reading(tmp_path, tamper) -> None:
    value = record(tmp_path)
    apply_reconciliation(value, proposal(value))
    evidence = reviews(value)
    if tamper == "missing":
        evidence[0]["coverage"].pop()
    elif tamper == "duplicate":
        evidence[0]["coverage"].append(evidence[0]["coverage"][0])
    elif tamper == "block":
        evidence[0]["coverage"][0]["decision"] = "BLOCK"
    elif tamper == "coherence":
        evidence[0]["coherence_decision"] = "BLOCK"
    elif tamper == "cleanup":
        evidence[0]["cleanup_verified"] = False
    elif tamper == "revision":
        evidence[0]["revision_id"] = "other"
    elif tamper == "note":
        value["source_reconciliation"]["verified"] = True
    elif tamper == "narrowed":
        value["inventory"]["regions"].pop()
        evidence = reviews(value)
    elif tamper == "malformed":
        value["source_reconciliation"] = ["not a certificate"]
    else:
        value["full_inventory"]["items"][0]["text"] += " Changed qualifier"
    with pytest.raises(ValueError):
        assert_reconciliation_reviewed(value, evidence)


def test_pdf_only_and_unbound_conditional_structure_remain_hard(tmp_path) -> None:
    value = record(tmp_path)
    inventory = value["inventory"]
    inventory["source_kind"] = "pdf"
    assert lexical_admission(inventory) == inventory["diagnostics"]
    inventory["source_kind"] = "latex"
    diagnostic = inventory["diagnostics"][0]
    diagnostic["code"] = "unresolved_environment_structure"
    assert diagnostic in lexical_admission(inventory)
    diagnostic.update(
        independent_conditional_paths=True,
        violation_witnesses=[{"source_spans": [diagnostic["span"]]}],
        violation_witnesses_truncated=False,
    )
    assert diagnostic not in lexical_admission(inventory)
    diagnostic["violation_witnesses_truncated"] = True
    assert diagnostic in lexical_admission(inventory)


@pytest.mark.parametrize("count,truncated", [(1024, False), (1025, True)])
def test_structural_reconciliation_retains_complete_witnesses_up_to_its_bound(
    tmp_path: Path, count: int, truncated: bool
) -> None:
    source = tmp_path / "many.tex"
    source.write_text(
        r"\usepackage{unknown}\begin{document}"
        + "".join(rf"\iftrue\end{{unknown{index}}}\fi" for index in range(count))
        + r"\end{document}"
    )
    inventory = inventory_source(tmp_path, source)
    diagnostic = next(
        row for row in inventory["diagnostics"] if row["code"] == "unresolved_environment_structure"
    )
    assert diagnostic["rejected_state_transitions"] == count
    assert len(diagnostic["violation_witnesses"]) == min(count, 1024)
    assert diagnostic["violation_witnesses_truncated"] is truncated
    assert (diagnostic in lexical_admission(inventory)) is truncated
    assert diagnostic["blocking"] is True


def bibliography_record(tmp_path: Path, *, prefix: str = "%", inline: str | None = None) -> dict:
    """Freeze a real literal TeX fixture with companion metadata but no claimed visual review."""
    source = tmp_path / "paper.tex"
    if inline is None:
        inline = (
            "\\begin{thebibliography}{AB}\n"
            "\\bibitem{AB}A supplied original reference.\n"
            "\\end{thebibliography}\n"
        )
    source.write_text(
        "\\documentclass{article}\n"
        + prefix
        + "\\bibliography{missing-database}\n"
        + "\\begin{lemma}A holds.\\end{lemma}\n"
        + inline
    )
    inventory = inventory_source(tmp_path, source)
    pdf = b"%PDF-frozen-context-not-a-rendering-claim"
    inventory["files"]["paper.pdf"] = {
        "sha256": digest(pdf),
        "encoding": "binary",
        "size": len(pdf),
    }
    inventory["companion_pdf"] = {
        "file": "paper.pdf",
        "sha256": digest(pdf),
        "selection": "explicit",
        "correspondence_verified": False,
    }
    return {
        "revision_id": "rev-source",
        "source_bundle": inventory["bundle_hash"],
        "inventory": inventory,
    }


def test_commented_missing_bibliography_requires_explicit_frozen_inline_reading(tmp_path) -> None:
    value = bibliography_record(tmp_path)
    original = copy.deepcopy(value["inventory"])
    missing = next(row for row in original["diagnostics"] if row["code"] == "missing_support_file")
    assert missing not in lexical_admission(original)
    requirements = reconciliation_requirements(value)
    bound = next(
        row for row in requirements["diagnostics"] if row["diagnostic_id"] == missing["id"]
    )
    support = bound["commented_support_evidence"]
    assert support["interpretation_kind"] == "commented-reference-with-supplied-inline-bibliography"
    raw = (tmp_path / "paper.tex").read_bytes()
    commented, inline = support["commented_reference_span"], support["inline_bibliography_span"]
    assert raw[commented["byte_start"] : commented["byte_end"]].startswith(b"%\\bibliography")
    assert b"\\bibitem{AB}" in raw[inline["byte_start"] : inline["byte_end"]]
    apply_reconciliation(value, proposal(value))
    assert value["full_inventory"] == original
    assert missing["blocking"] is True
    assert "paper.bbl" not in value["inventory"]["files"]
    with pytest.raises(ValueError):
        assert_reconciliation_reviewed(value, [])


@pytest.mark.parametrize(
    "case",
    [
        "active",
        "escaped_percent",
        "no_inline",
        "commented_inline",
        "macro_inline",
        "no_pdf",
        "stale_pdf",
        "stale_text",
        "size_limit",
    ],
)
def test_real_missing_or_unbound_bibliography_context_stays_hard(tmp_path, case) -> None:
    prefix = "" if case == "active" else r"\%" if case == "escaped_percent" else "%"
    inline = "" if case == "no_inline" else None
    if case == "commented_inline":
        inline = (
            "%\\begin{thebibliography}{AB}\n%\\bibitem{AB}Reference.\n%\\end{thebibliography}\n"
        )
    elif case == "macro_inline":
        inline = "\\newcommand{\\hidden}{\n\\begin{thebibliography}{AB}\n\\bibitem{AB}Reference.\n\\end{thebibliography}}\n"
    value = bibliography_record(tmp_path, prefix=prefix, inline=inline)
    inventory = value["inventory"]
    missing = next(row for row in inventory["diagnostics"] if row["code"] == "missing_support_file")
    if case == "no_pdf":
        inventory.pop("companion_pdf")
    elif case == "stale_pdf":
        inventory["companion_pdf"]["sha256"] = "replacement"
    elif case == "stale_text":
        inventory["files"]["paper.tex"]["text"] += "New input"
    elif case == "size_limit":
        missing["support_unavailable_reason"] = "size_limit"
    assert missing in lexical_admission(inventory)


def test_commented_bibliography_does_not_waive_a_genuine_missing_graphic(tmp_path) -> None:
    value = bibliography_record(tmp_path)
    diagnostic = copy.deepcopy(
        next(
            row
            for row in value["inventory"]["diagnostics"]
            if row["code"] == "missing_support_file"
        )
    )
    diagnostic.update(
        id="missing-real-figure",
        support_command="includegraphics",
        support_target="actual-missing-figure",
    )
    value["inventory"]["diagnostics"].append(diagnostic)
    assert lexical_admission(value["inventory"]) == [diagnostic]
