"""Keep conditional environment candidates distinct from structural certainty and draft authority."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leanflow_cli.formalization import source_environment_paths, source_provenance
from leanflow_cli.formalization.source_draft import draft_policy
from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_provenance import digest
from leanflow_cli.formalization.source_tex import TexBundle

PREFIX = r"\newif\ifchoice "
HARD = {"unmatched_environment", "unclosed_environment", "environment_structure_limit"}


def inventory(tmp_path: Path, text: str) -> dict:
    """Inventory a generic source fixture without running a TeX interpreter."""
    source = tmp_path / "paper.tex"
    source.write_text(PREFIX + text)
    return inventory_source(tmp_path, source)


def choices(begin: bool) -> str:
    """Describe alternative list delimiters without claiming repeated guards are stable."""
    command = "begin" if begin else "end"
    return rf"\ifchoice\{command}{{itemize}}\else\{command}{{enumerate}}\fi "


def paired(value: dict) -> dict:
    """Attach matching frozen companion metadata for the pure draft-policy check."""
    value = copy.deepcopy(value)
    raw = b"%PDF-fixture"
    value["files"]["paper.pdf"] = {
        "sha256": digest(raw),
        "encoding": "binary",
        "size": len(raw),
    }
    value["companion_pdf"] = {
        "file": "paper.pdf",
        "sha256": digest(raw),
        "selection": "explicit",
    }
    return value


def test_separate_unknown_list_choices_recover_outer_construction_without_correlating_guards(
    tmp_path,
):
    result = inventory(
        tmp_path,
        r"\newtheorem{constr}{Construction}\begin{document}"
        r"\begin{constr}\label{generic}A construction. "
        + choices(True)
        + r"\item First. "
        + choices(True)
        + r"\item Nested. "
        + choices(False)
        + choices(False)
        + r"\end{constr}\end{document}",
    )
    (item,) = result["items"]
    assert item["label"] == "generic" and item["scope"] == "candidate"
    assert result["extraction_status"] == "blocked" and result["audit_complete"] is False
    assert not any(row["code"] in HARD for row in result["diagnostics"])
    (uncertainty,) = [
        row for row in result["diagnostics"] if row["code"] == "unresolved_environment_structure"
    ]
    assert uncertainty["blocking"] and uncertainty["independent_conditional_paths"]
    assert uncertainty["violation_witnesses"]
    for span in uncertainty["spans"]:
        raw = (tmp_path / span["file"]).read_bytes()
        assert span["file_hash"] == digest(raw)
        assert raw[span["byte_start"] : span["byte_end"]]
        assert span["occurrence_id"]


def test_balanced_branch_local_statements_keep_both_candidates_without_false_misnest(tmp_path):
    result = inventory(
        tmp_path,
        r"\ifchoice\begin{theorem}\label{left}Left.\end{theorem}"
        r"\else\begin{lemma}\label{right}Right.\end{lemma}\fi",
    )
    assert {item["label"] for item in result["items"]} == {"left", "right"}
    assert {item["scope"] for item in result["items"]} == {"candidate"}
    codes = {row["code"] for row in result["diagnostics"]}
    assert "unresolved_conditional" in codes
    assert not codes & (HARD | {"unresolved_environment_structure"})


def test_loader_invalidates_an_already_recorded_known_outer_guard(tmp_path):
    result = inventory(
        tmp_path,
        r"\def\flag{0}\ifnum\flag=0\relax\documentclass{article}"
        r"\begin{theorem}\label{before_else}First.\end{theorem}"
        r"\else\begin{lemma}\label{after_else}Alternative.\end{lemma}\fi",
    )
    assert {item["label"] for item in result["items"]} == {"before_else", "after_else"}
    assert {item["scope"] for item in result["items"]} == {"candidate"}
    assert any(row["code"] == "unresolved_loader_effect" for row in result["diagnostics"])


def test_rejected_transition_counts_do_not_claim_complete_path_or_witness_enumeration(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(source_environment_paths, "MAX_WITNESSES", 1)
    result = inventory(tmp_path, choices(True) + choices(False))
    uncertainty = next(
        row for row in result["diagnostics"] if row["code"] == "unresolved_environment_structure"
    )
    assert len(uncertainty["violation_witnesses"]) == 1
    assert uncertainty["rejected_state_transitions"] > 1
    assert uncertainty["violation_witnesses_truncated"]
    assert uncertainty["violation_witnesses_deduplicated"]


def test_mutation_between_repeated_guards_never_removes_bad_path_evidence(tmp_path):
    result = inventory(
        tmp_path,
        r"\begin{theorem}\label{outer}"
        + choices(True)
        + r"\def\flag{1}"
        + choices(False)
        + r"\end{theorem}",
    )
    assert result["items"][0]["scope"] == "candidate"
    assert any(
        row["code"] == "unresolved_environment_structure" and row["violation_witnesses"]
        for row in result["diagnostics"]
    )
    assert any(row["code"] == "unresolved_conditional" for row in result["diagnostics"])


@pytest.mark.parametrize(
    "body",
    [
        r"\begin{theorem}Broken.\end{lemma}",
        r"\ifchoice\begin{theorem}Broken.\end{lemma}" r"\else\begin{lemma}Broken.\end{theorem}\fi",
        r"\ifchoice\begin{theorem}Unclosed.\else\begin{lemma}Unclosed.\fi",
    ],
)
def test_no_complete_balanced_path_preserves_strict_structural_failure(tmp_path, body):
    result = inventory(tmp_path, body)
    assert result["items"] == []
    assert any(row["code"] in HARD for row in result["diagnostics"])
    assert not any(
        row["code"] == "unresolved_environment_structure" for row in result["diagnostics"]
    )
    assert any(row["code"] in HARD for row in draft_policy(paired(result), explicit=True)["strict"])


@pytest.mark.parametrize(
    "body",
    [
        r"\ifchoice\begin{lemma}First half.\else Second half.\end{lemma}\fi",
        choices(True) + choices(False) + r"\begin{lemma}Claim.\end{lemma}\end{stray}",
    ],
)
def test_failed_path_analysis_never_falls_back_to_a_linear_stitched_candidate(tmp_path, body):
    result = inventory(tmp_path, body)
    assert result["items"] == []
    assert any(row["code"] in HARD for row in draft_policy(paired(result), explicit=True)["strict"])


@pytest.mark.parametrize(
    "body",
    [
        r"\ifcase0\begin{lemma}First.\end{lemma}" r"\or\begin{theorem}Second.\end{theorem}\else\fi",
        r"\ifcase0\begin{lemma}Unchosen.\end{lemma}\fi",
        r"\or\begin{lemma}No selector.\end{lemma}",
    ],
)
def test_case_conditionals_are_strict_without_a_binary_or_numeric_chooser(tmp_path, body):
    result = inventory(tmp_path, body)
    assert result["items"] == []
    assert any(
        row["code"] == "unsupported_environment_conditional"
        for row in draft_policy(paired(result), explicit=True)["strict"]
    )


def test_known_inactive_case_tokens_do_not_supply_structure_or_require_a_chooser(tmp_path):
    result = inventory(
        tmp_path,
        r"\iffalse\ifcase0\begin{lemma}Excluded.\or\end{lemma}\fi\fi"
        r"\begin{theorem}\label{visible}Visible.\end{theorem}",
    )
    assert [item["label"] for item in result["items"]] == ["visible"]
    assert not any(
        row["code"] == "unsupported_environment_conditional" for row in result["diagnostics"]
    )


@pytest.mark.parametrize("same_branch", [False, True])
def test_union_of_proof_candidates_never_claims_automatic_same_path_attachment(
    tmp_path, same_branch
):
    statement = r"\begin{lemma}\label{claim}Claim.\end{lemma}"
    proof = r"\begin{proof}Candidate proof bytes.\end{proof}"
    body = (
        r"\ifchoice" + statement + proof + r"\else Other.\fi"
        if same_branch
        else r"\ifchoice" + statement + r"\else" + proof + r"\fi"
    )
    result = inventory(tmp_path, body)
    (item,) = result["items"]
    assert item["claim_status"] == "statement_only" and item["scope"] == "candidate"
    assert item["proof_spans"] == [] and item["proof_text"] == ""
    assert item["proof_attachment_uncertain"]
    assert item["proof_candidate_spans"]
    assert "same-path" in item["proof_attachment_note"]
    raw = (tmp_path / "paper.tex").read_bytes()
    assert any(
        b"Candidate proof bytes" in raw[span["byte_start"] : span["byte_end"]]
        for span in item["proof_candidate_spans"]
    )
    first = raw.index(b"Candidate proof bytes.")
    last = first + len(b"Candidate proof bytes.")
    covered = [
        region
        for region in result["regions"]
        if region["byte_start"] < last and first < region["byte_end"]
    ]
    assert covered and {region["disposition"] for region in covered} == {"unresolved"}
    assert (
        sum(min(last, region["byte_end"]) - max(first, region["byte_start"]) for region in covered)
        == last - first
    )


def test_ordinary_nonconditional_proof_attachment_is_preserved(tmp_path):
    result = inventory(
        tmp_path,
        r"\begin{lemma}Claim.\end{lemma}\begin{proof}Argument.\end{proof}",
    )
    (item,) = result["items"]
    assert item["claim_status"] == "proved_in_source" and item["proof_spans"]
    assert not item.get("proof_attachment_uncertain")


def test_removed_loader_options_cannot_silently_drop_conditional_delimiters(tmp_path):
    result = inventory(
        tmp_path,
        r"\usepackage[\iftrue\fi]{custom}\begin{lemma}No projection.\end{lemma}",
    )
    assert result["items"] == []
    (diagnostic,) = [
        row for row in result["diagnostics"] if row["code"] == "unmapped_environment_conditional"
    ]
    (span,) = diagnostic["spans"]
    raw = (tmp_path / span["file"]).read_bytes()
    assert span["file_hash"] == digest(raw) and span["occurrence_id"]
    assert raw[span["byte_start"] : span["byte_end"]] == rb"\iftrue"
    assert diagnostic in draft_policy(paired(result), explicit=True)["strict"]


def test_includeonly_excluded_occurrences_legitimately_have_no_projected_delimiters(tmp_path):
    (tmp_path / "ignored.tex").write_text(r"\iftrue\begin{lemma}Ignored.\end{lemma}\fi")
    (tmp_path / "kept.tex").write_text(r"\begin{lemma}\label{kept}Kept.\end{lemma}")
    result = inventory(tmp_path, r"\includeonly{kept}\include{ignored}\include{kept}")
    assert [item["label"] for item in result["items"]] == ["kept"]
    assert not any(
        row["code"] == "unmapped_environment_conditional" for row in result["diagnostics"]
    )


@pytest.mark.parametrize("explicit,companion", [(False, True), (False, False), (True, True)])
def test_environment_uncertainty_requires_explicit_draft_and_exact_pdf_pair(
    tmp_path, explicit, companion
):
    result = inventory(
        tmp_path,
        r"\begin{theorem}Claim." + choices(True) + choices(False) + r"\end{theorem}",
    )
    if companion:
        result = paired(result)
    policy = draft_policy(result, explicit=explicit)
    rows = policy["deferred"] if explicit and companion else policy["strict"]
    assert any(row["code"] == "unresolved_environment_structure" for row in rows)
    assert policy["draft_only"] is bool(explicit and companion)


def test_explicit_draft_without_pdf_or_without_authentic_spans_is_refused(tmp_path):
    result = inventory(
        tmp_path,
        r"\begin{theorem}Claim." + choices(True) + choices(False) + r"\end{theorem}",
    )
    with pytest.raises(ValueError, match="explicitly paired frozen PDF"):
        draft_policy(result, explicit=True)
    result = paired(result)
    uncertainty = next(
        row for row in result["diagnostics"] if row["code"] == "unresolved_environment_structure"
    )
    uncertainty.pop("spans")
    assert uncertainty in draft_policy(result, explicit=True)["strict"]


@pytest.mark.parametrize(
    "limit",
    [
        "frontier",
        "conditional_depth",
        "environment_depth",
        "tokens",
        "history_references",
        "history_nodes",
        "metadata",
    ],
)
def test_parser_resource_cap_stays_strict_even_with_a_frozen_pdf(tmp_path, monkeypatch, limit):
    if limit == "frontier":
        monkeypatch.setattr(source_environment_paths, "MAX_FRONTIER", 1)
        body = choices(True) + choices(False)
    elif limit == "conditional_depth":
        monkeypatch.setattr(source_environment_paths, "MAX_CONDITIONAL_DEPTH", 2)
        body = r"\ifchoice " * 3 + "Claim." + r"\fi " * 3
    elif limit == "environment_depth":
        monkeypatch.setattr(source_environment_paths, "MAX_ENVIRONMENT_DEPTH", 2)
        body = r"\ifchoice" + r"\begin{theorem}" * 3 + r"\end{theorem}" * 3 + r"\else\fi"
    elif limit == "tokens":
        monkeypatch.setattr(source_environment_paths, "MAX_TOKENS", 2)
        body = r"\ifchoice Text.\else Other.\fi"
    elif limit == "history_references":
        monkeypatch.setattr(source_environment_paths, "MAX_HISTORY_REFERENCES", 8)
        body = r"\ifchoice " + r"\begin{theorem}Claim.\end{theorem}" * 16 + r"\else\fi"
    elif limit == "history_nodes":
        monkeypatch.setattr(source_environment_paths, "MAX_HISTORY_NODES", 4)
        body = r"\ifchoice " + r"\begin{theorem}Claim.\end{theorem}" * 8 + r"\else\fi"
    else:
        monkeypatch.setattr(source_provenance, "MAX_CONDITIONAL_EVENTS", 2)
        body = r"\ifchoice Text.\else Other.\fi"
    result = inventory(tmp_path, body)
    assert result["items"] == []
    assert any(row["code"] == "environment_structure_limit" for row in result["diagnostics"])
    assert any(
        row["code"] == "environment_structure_limit"
        for row in draft_policy(paired(result), explicit=True)["strict"]
    )


def test_nested_include_occurrences_keep_exact_original_byte_spans(tmp_path):
    part = tmp_path / "part.tex"
    part.write_text(r"\begin{lemma}\label{included}An included claim.\end{lemma}")
    result = inventory(tmp_path, r"\ifchoice\input{part}\else\input{part}\fi")
    assert len(result["items"]) == 2
    assert len({item["occurrence_id"] for item in result["items"]}) == 2
    for item in result["items"]:
        (span,) = item["statement_spans"]
        assert span["file"] == "part.tex" and span["file_hash"] == digest(part.read_bytes())
        assert part.read_bytes()[span["byte_start"] : span["byte_end"]].decode() == item["text"]
        assert item["scope"] == "candidate"


def test_many_independent_balanced_choices_merge_without_exponential_frontier(tmp_path):
    result = inventory(
        tmp_path,
        r"\begin{theorem}\label{many}"
        + (r"\ifchoice Text.\else Other text.\fi " * 200)
        + r"\end{theorem}",
    )
    assert [item["label"] for item in result["items"]] == ["many"]
    assert not any(row["code"] in HARD for row in result["diagnostics"])


@pytest.mark.parametrize("placement", ["sequential", "long_branch"])
def test_two_thousand_environments_keep_linear_bounded_histories(tmp_path, placement):
    statements = r"\begin{lemma}A generic claim.\end{lemma}" * 2000
    body = (
        r"\ifchoice Left.\else Right.\fi " + statements
        if placement == "sequential"
        else r"\ifchoice " + statements + r"\else Other.\fi"
    )
    source = tmp_path / "paper.tex"
    source.write_text(PREFIX + body)
    bundle = TexBundle(tmp_path, source, "generic-large-source")
    bundle.expand(source, source.name)
    blocks = source_environment_paths.conditional_blocks(bundle, bundle.expanded())
    assert blocks is not None and len(blocks) == 2000
    assert {block["environment"] for block in blocks} == {"lemma"}
    assert all(block["uncertain"] for block in blocks)
    assert not any(row["code"] == "environment_structure_limit" for row in bundle.diagnostics)


def test_alternative_closers_have_deterministic_candidate_order(tmp_path):
    text = r"\begin{lemma}\label{alternative}\ifchoice\end{lemma}\else\end{lemma}\fi"
    first = inventory(tmp_path, text)
    second = inventory(tmp_path, text)
    assert first == second
    assert len(first["items"]) == 2
    ends = [item["statement_spans"][-1]["byte_end"] for item in first["items"]]
    assert ends == sorted(ends)


def test_real_python_hash_seeds_produce_identical_inventory_json(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(
        PREFIX
        + r"\begin{theorem}\label{stable}"
        + (choices(True) + choices(False)) * 15
        + r"\end{theorem}"
    )
    script = (
        "import json,sys; from pathlib import Path; "
        "from leanflow_cli.formalization.source_inventory import inventory_source; "
        "source=Path(sys.argv[1]); "
        "print(json.dumps(inventory_source(source.parent,source),sort_keys=True))"
    )
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", script, str(source)],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "PYTHONHASHSEED": seed},
            timeout=15,
        )
        for seed in ("1", "8675309")
    ]
    assert outputs[0] == outputs[1]
