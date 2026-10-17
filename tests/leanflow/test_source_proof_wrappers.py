"""Retain literal custom source proofs without inferring execution or automatic proof authority."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_provenance import SourceText, scan_tex


def inventory(tmp_path: Path, text: str) -> dict:
    source = tmp_path / "paper.tex"
    source.write_text(text)
    return inventory_source(tmp_path, source)


def test_literal_comment_wrapper_keeps_complete_proof_as_provisional_source_evidence(
    tmp_path,
) -> None:
    definition = "\\newenvironment{myproof}{\\begin{proof}%\\small\n}{\\end{proof}}"
    proof = r"\begin{myproof}Use the full λ hypothesis. FINAL CLAUSE.\end{myproof}"
    value = inventory(tmp_path, definition + r"\begin{lemma}\label{one}A holds.\end{lemma}" + proof)
    item = value["items"][0]
    assert item["proof_candidate_text"] == proof
    assert item["proof_spans"] == []
    assert item["claim_status"] == "statement_only"
    assert item["proof_attachment_uncertain"] is True
    wrapper = item["proof_wrapper_definitions"][0]
    assert wrapper["original_definition"] == definition
    assert wrapper["argument_count"] == 0
    raw = (tmp_path / "paper.tex").read_bytes()
    span = wrapper["source_span"]
    assert raw[span["byte_start"] : span["byte_end"]].decode() == definition
    original_hash = item["content_hash"]
    changed = inventory(
        tmp_path,
        definition
        + r"\begin{lemma}\label{one}A holds.\end{lemma}"
        + proof.replace("FINAL CLAUSE", "DIFFERENT CLAUSE"),
    )
    assert changed["items"][0]["id"] == item["id"]
    assert changed["items"][0]["content_hash"] != original_hash


def test_proofof_label_binds_only_unique_literal_target_and_preserves_title_argument(
    tmp_path,
) -> None:
    proof = r"\begin{proofof}{\ref{first}}An exact source argument.\end{proofof}"
    value = inventory(
        tmp_path,
        r"\newenvironment{proofof}[1]{\begin{proof}[Proof of {#1}]}{\end{proof}}"
        r"\begin{lemma}\label{first}First holds.\end{lemma}"
        r"\begin{lemma}\label{second}Second holds.\end{lemma}" + proof,
    )
    first, second = value["items"]
    assert first["proof_candidate_text"] == proof
    assert "proof_candidate_text" not in second
    assert first["proof_wrapper_definitions"][0]["argument_count"] == 1


@pytest.mark.parametrize(
    "definition",
    [
        r"\newenvironment{myproof}{\begin{proof}\UnknownEffect}{\end{proof}}",
        r"\newenvironment{myproof}{\begin{proof}}{\end{proof}\gdef\flag{1}}",
        r"\newenvironment{myproof}[1][default]{\begin{proof}}{\end{proof}}",
        r"\newenvironment{myproof}[2]{\begin{proof}}{\end{proof}}",
        r"\newenvironment{myproof}{\begin{proof}}{\end{other}}",
        r"\newenvironment{myproof}{\begin{proof}[\UnknownEffect]}{\end{proof}}",
        r"\newenvironment{myproof}{\begin{proof}}{\end{proof}}\renewenvironment{myproof}{}{}",
        r"\newenvironment{proof}{}{}\newenvironment{myproof}{\begin{proof}}{\end{proof}}",
    ],
)
def test_effectful_ambiguous_or_unbounded_wrappers_are_not_attached(tmp_path, definition) -> None:
    value = inventory(
        tmp_path,
        definition + r"\begin{lemma}A holds.\end{lemma}\begin{myproof}Argument.\end{myproof}",
    )
    assert "proof_wrapper_definitions" not in value["items"][0]


def test_unsafe_invocation_argument_is_not_evaluated_or_attached(tmp_path) -> None:
    value = inventory(
        tmp_path,
        r"\newenvironment{proofof}[1]{\begin{proof}[Proof of #1]}{\end{proof}}"
        r"\begin{lemma}A holds.\end{lemma}"
        r"\begin{proofof}{\gdef\flag{1}}Argument.\end{proofof}",
    )
    assert "proof_candidate_text" not in value["items"][0]


def test_inactive_definition_cleanup_preserves_exact_skipped_token_events() -> None:
    source = r"\newif\ifflag\iffalse\ifflag hidden\fi\else live\fi"
    scan = scan_tex(SourceText("paper.tex", source.encode()))
    assert scan.conditional_events == [
        (13, 21, "if", False),
        (21, 28, "if", False),
        (35, 38, "fi", None),
        (38, 43, "else", None),
        (48, 51, "fi", None),
    ]
    assert [row["name"] for row in scan.definitions] == ["ifflag", "flagtrue", "flagfalse"]
    assert "hidden" not in scan.visible
    assert "live" in scan.visible
    source = r"\iffalse\def\x{\fi\bgroup}\begin{theorem}live\end{theorem}"
    scan = scan_tex(SourceText("paper.tex", source.encode()))
    assert scan.definitions == []
    assert scan.conditional_events == [(0, 8, "if", False), (15, 18, "fi", None)]
    assert r"\bgroup}" in scan.visible
