"""Retain live candidates from independently reproduced TeX skipping and expansion cases."""

from __future__ import annotations

import pytest

from leanflow_cli.formalization.source_draft import draft_policy
from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_provenance import SourceText, scan_tex
from tests.leanflow.test_source_environment_paths import paired
from tests.leanflow.test_source_inventory import _assert_regions_partition_sources
from tests.leanflow.test_source_numeric_conditionals import _inventory, _statement


@pytest.mark.parametrize(
    "skipped",
    [r"\def\ignored{\fi\bgroup}", r"\ifNotAConditional\fi"],
)
def test_inactive_tokens_never_consume_definitions_or_invent_if_prefix_nesting(tmp_path, skipped):
    inventory = _inventory(tmp_path, r"\iffalse" + skipped + _statement("live"))
    assert [item["label"] for item in inventory["items"]] == ["live"]
    assert any(region["disposition"] == "excluded" for region in inventory["regions"])


def test_known_inactive_conditionals_still_nest_and_track_newif_token_meaning(tmp_path):
    source = r"\newif\ifflag\iffalse\ifflag" + _statement("hidden") + r"\fi\fi"
    source += _statement("live")
    inventory = _inventory(tmp_path, source)
    assert [item["label"] for item in inventory["items"]] == ["live"]
    source = r"\newif\ifflag\def\ifflag{ignored}\iffalse\ifflag\fi" + _statement("live")
    inventory = _inventory(tmp_path, source)
    assert [item["label"] for item in inventory["items"]] == ["live"]


@pytest.mark.parametrize(
    "effect",
    [
        r"\def\theequation{9}\documentclass{article}",
        "\\def\\flag{0}\\def\\open{%$\n$}\\open\\def\\flag{1}x$",
        r"\newenvironment{foo}{}{}\def\endfoo{9}\renewenvironment{foo}{}{1}",
        r"\def\thefoo{9}\newtheorem{foo}{Claim}\begin{foo}",
    ],
)
def test_loader_comment_helpers_and_generated_controls_preserve_both_candidates(tmp_path, effect):
    operand = (
        "theequation"
        if "theequation" in effect
        else "endfoo" if "endfoo" in effect else "thefoo" if "thefoo" in effect else "flag"
    )
    value = "0" if operand == "flag" else "9"
    source = effect + rf"\ifnum\{operand}={value}\relax "
    source += _statement("right") + r"\else " + _statement("wrong") + r"\fi"
    if operand == "thefoo":
        source += r"\end{foo}"
    inventory = _inventory(tmp_path, source)
    assert {item["label"] for item in inventory["items"]} >= {"right", "wrong"}
    assert inventory["extraction_status"] == "blocked"
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}


@pytest.mark.parametrize("whitespace", ["\n\n", "\n \t\n", "\r\n\r\n", "\r\r"])
@pytest.mark.parametrize("position", ["before", "after_macro", "before_relation"])
def test_numeric_paragraph_tokens_never_join_decimal_operands(tmp_path, whitespace, position):
    condition = {
        "before": whitespace + r"1=1\relax",
        "after_macro": r"1=\digit" + whitespace + r"0\relax",
        "before_relation": r"1" + whitespace + r"=1\relax",
    }[position]
    source = r"\def\digit{1}\ifnum" + condition + _statement("right")
    source += r"\else " + _statement("wrong") + r"\fi"
    inventory = _inventory(tmp_path, source)
    assert {item["label"] for item in inventory["items"]} == {"right", "wrong"}
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_known_endinput_finishes_current_line_before_excluding_following_lines(tmp_path, newline):
    source = r"\endinput " + _statement("live") + newline + _statement("excluded")
    inventory = _inventory(tmp_path, source)
    assert [item["label"] for item in inventory["items"]] == ["live"]
    excluded_start = source.encode().index(_statement("excluded").encode())
    assert any(
        row["byte_start"] <= excluded_start < row["byte_end"] and row["disposition"] == "excluded"
        for row in inventory["regions"]
    )


@pytest.mark.parametrize("loader", [r"\documentclass{article}", r"\usepackage{amsmath}"])
def test_loader_uncertainty_disables_primitive_literal_definition_and_tail_masks(tmp_path, loader):
    source = loader + r"\begin{document}\iffalse " + _statement("branch") + r"\fi"
    source += r"\begin{comment}" + _statement("comment") + r"\end{comment}"
    source += r"\def\wrapper{" + _statement("replacement") + "}"
    source += r"\end{document}" + _statement("tail")
    source += r"\endinput " + "\n" + _statement("next-line")
    inventory = _inventory(tmp_path, source)
    assert {item["label"] for item in inventory["items"]} == {
        "branch",
        "comment",
        "replacement",
        "tail",
        "next-line",
    }
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}
    assert inventory["extraction_status"] == "blocked"


def test_unconditional_stray_document_end_is_strict_with_no_uncertain_candidates(tmp_path):
    source = r"\documentclass{article}\iftrue " + _statement("branch") + r"\fi"
    source += r"\end{document}" + _statement("tail")
    inventory = _inventory(tmp_path, source)
    assert inventory["items"] == []
    assert any(row["code"] == "unmatched_environment" for row in inventory["diagnostics"])
    assert any(
        row["code"] == "unmatched_environment"
        for row in draft_policy(paired(inventory), explicit=True)["strict"]
    )
    raw = source.encode()
    first, last = raw.index(b"tail holds."), raw.index(b"tail holds.") + len(b"tail holds.")
    covered = [
        row for row in inventory["regions"] if row["byte_start"] < last and first < row["byte_end"]
    ]
    assert covered and {row["disposition"] for row in covered} == {"unresolved"}


@pytest.mark.parametrize("literal", ["verb", "lstinline"])
def test_unknown_inline_meanings_cannot_hide_theorem_candidates(tmp_path, literal):
    source = r"\usepackage{color}" + "\\" + literal + "|" + _statement("live") + "|"
    inventory = _inventory(tmp_path, source)
    assert [item["label"] for item in inventory["items"]] == ["live"]
    assert inventory["items"][0]["scope"] == "candidate"


@pytest.mark.parametrize(
    "prefix",
    [
        r"{\newif\iffoo}\iffalse\iffoo\fi",
        r"\newif\iffoo{\def\iffoo{ordinary}}\iffalse\iffoo\fi\fi",
        r"\newif\ififfoo\iffalse\iffootrue\fi",
    ],
)
def test_current_scoped_conditional_token_meaning_controls_inactive_nesting(tmp_path, prefix):
    inventory = _inventory(tmp_path, prefix + _statement("live"))
    assert [item["label"] for item in inventory["items"]] == ["live"]


@pytest.mark.parametrize(
    "ambiguous",
    [
        r"\def\ignored{\fi\bgroup}",
        r"\newif\iffoo\fi\iffalse\iffoo\fi",
        "\\endinput\n\\fi\n",
        r"\verb|\fi|",
        r"\begin{comment}\fi\end{comment}",
    ],
)
def test_execution_uncertainty_never_consumes_skipped_control_tokens(tmp_path, ambiguous):
    source = r"\ifdim1pt<0pt" + ambiguous + _statement("live")
    inventory = _inventory(tmp_path, source)
    if ambiguous == r"\def\ignored{\fi\bgroup}":
        # Executing the definition consumes fi; skipping it exposes fi. The
        # bounded path parser does not interpret this conditional token stream.
        assert inventory["items"] == []
        codes = {row["code"] for row in inventory["diagnostics"]}
        assert {"unresolved_conditional", "unsupported_environment_conditional"} <= codes
        assert any(
            row["code"] == "unsupported_environment_conditional"
            for row in draft_policy(paired(inventory), explicit=True)["strict"]
        )
        raw = source.encode()
        first, last = raw.index(b"live holds."), raw.index(b"live holds.") + len(b"live holds.")
        covered = [
            row
            for row in inventory["regions"]
            if row["byte_start"] < last and first < row["byte_end"]
        ]
        assert covered and {row["disposition"] for row in covered} == {"unresolved"}
        assert (
            sum(min(last, row["byte_end"]) - max(first, row["byte_start"]) for row in covered)
            == last - first
        )
        return
    assert [item["label"] for item in inventory["items"]] == ["live"]
    assert inventory["extraction_status"] == "blocked"


def test_known_executing_definition_does_not_expose_its_conditional_replacement(tmp_path):
    source = r"\def\ignored{\fi\bgroup}" + _statement("live")
    inventory = _inventory(tmp_path, source)
    assert [item["label"] for item in inventory["items"]] == ["live"]
    assert not any(
        row["code"] == "unsupported_environment_conditional" for row in inventory["diagnostics"]
    )
    assert scan_tex(SourceText("paper.tex", source.encode())).conditional_events == []


def test_known_skipping_records_the_exposed_fi_instead_of_consuming_definition_arguments(tmp_path):
    source = r"\iffalse\def\ignored{\fi\bgroup}" + _statement("live")
    inventory = _inventory(tmp_path, source)
    assert [item["label"] for item in inventory["items"]] == ["live"]
    scan = scan_tex(SourceText("paper.tex", source.encode()))
    assert any(
        kind == "fi" and source[start:end] == r"\fi"
        for start, end, kind, _ in scan.conditional_events
    )
    assert not any(
        row["code"] == "unsupported_environment_conditional" for row in inventory["diagnostics"]
    )


@pytest.mark.parametrize(
    "declaration",
    [
        r"\newenvironment{wrapper}{x}{\fi}",
        r"\def\wrapper\fi#1{x}",
        r"\newcommand{\wrapper}[1][\fi]{x}",
    ],
)
def test_unknown_execution_checks_conditional_balance_in_the_entire_declaration(
    tmp_path, declaration
):
    source = r"\newif\ifchoice\ifchoice" + declaration + r"\else\fi"
    inventory = _inventory(tmp_path, source + _statement("later"))
    assert inventory["items"] == []
    assert any(
        row["code"] == "unsupported_environment_conditional"
        for row in draft_policy(paired(inventory), explicit=True)["strict"]
    )
    _assert_regions_partition_sources(tmp_path, inventory)


@pytest.mark.parametrize(
    "declaration",
    [
        r"\newcommand{\qed}{\ifmmode x\else y\fi}",
        r"\newenvironment{wrapper}{\ifmmode x\else y\fi}{\iftrue z\fi}",
    ],
)
def test_self_balanced_definition_controls_do_not_refuse_all_uncertain_candidates(
    tmp_path, declaration
):
    source = r"\newif\ifchoice\ifchoice" + declaration + r"\else\fi"
    inventory = _inventory(tmp_path, source + _statement("later"))
    assert [item["label"] for item in inventory["items"]] == ["later"]
    assert inventory["items"][0]["scope"] == "candidate"
    assert not any(
        row["code"] == "unsupported_environment_conditional" for row in inventory["diagnostics"]
    )


def test_plain_tex_unclosed_proof_remains_strict_when_conditional_path_mode_is_active(tmp_path):
    source = r"\newif\ifchoice\ifchoice\fi\proof X"
    inventory = _inventory(tmp_path, source)
    assert any(
        row["code"] == "unclosed_environment" and "plain TeX proof" in row["message"]
        for row in draft_policy(paired(inventory), explicit=True)["strict"]
    )


def test_unparsed_definition_never_preserves_stale_integer_authority(tmp_path):
    source = r"\def\flag{0}\def \flag{1}\ifnum\flag=0\relax "
    source += _statement("wrong") + r"\else " + _statement("right") + r"\fi"
    inventory = _inventory(tmp_path, source)
    assert {item["label"] for item in inventory["items"]} == {"wrong", "right"}
    assert {row["code"] for row in inventory["diagnostics"]} >= {
        "unparsed_definition",
        "unresolved_conditional",
    }


def test_cr_only_comment_line_does_not_hide_following_live_source(tmp_path):
    inventory = _inventory(
        tmp_path, "% ignored " + _statement("comment") + "\r" + _statement("live")
    )
    assert [item["label"] for item in inventory["items"]] == ["live"]


def test_caret_preprocessing_is_refused_before_any_branch_or_endinput_exclusion(tmp_path):
    first = r"\def\flag{0}^^5cdef^^5cflag{1}\ifnum\flag=0\relax\endinput\fi"
    source = first + "\n\\documentclass{article}" + _statement("live-hidden")
    inventory = _inventory(tmp_path, source)
    assert [item["label"] for item in inventory["items"]] == ["live-hidden"]
    assert inventory["items"][0]["scope"] == "candidate"
    diagnostic = next(
        row for row in inventory["diagnostics"] if row["code"] == "unsupported_tex_preprocessing"
    )
    span = diagnostic["span"]
    assert source.encode()[span["byte_start"] : span["byte_end"]] == b"^^"
    assert inventory["extraction_status"] == "blocked"
    assert not any(region["disposition"] == "excluded" for region in inventory["regions"])


@pytest.mark.parametrize("prefix", ["% ^^5c\n", r"\begin{verbatim}^^5c\end{verbatim}"])
def test_caret_notation_never_receives_partial_decode_or_localized_trust(tmp_path, prefix):
    inventory = _inventory(tmp_path, prefix + r"\iffalse " + _statement("candidate") + r"\fi")
    assert [item["label"] for item in inventory["items"]] == ["candidate"]
    assert any(row["code"] == "unsupported_tex_preprocessing" for row in inventory["diagnostics"])


@pytest.mark.parametrize("prefix", [r"\documentclass{article}", r"\catcode`\%=12"])
def test_percent_after_unknown_tokenizer_effects_remains_a_visible_candidate(tmp_path, prefix):
    source = prefix + "\n%" + _statement("live-hidden")
    inventory = _inventory(tmp_path, source)
    assert [item["label"] for item in inventory["items"]] == ["live-hidden"]
    assert inventory["items"][0]["scope"] == "candidate"
    assert inventory["extraction_status"] == "blocked"


@pytest.mark.parametrize(
    "prefix",
    [
        r"\documentclass{article}\def\unused{\includeonly{}}",
        r"\documentclass{article}\iffalse\includeonly{}\fi",
        r"\includeonly{}\includeonly{child}",
        r"{\includeonly{}}",
        r"\includeonly{}\usepackage{amsmath}",
    ],
)
def test_uncertain_includeonly_never_excludes_live_child_source(tmp_path, prefix):
    (tmp_path / "child.tex").write_text(_statement("live-hidden"))
    inventory = _inventory(tmp_path, prefix + r"\include{child}")
    assert [item["label"] for item in inventory["items"]] == ["live-hidden"]
    assert any(row["code"] == "unresolved_includeonly" for row in inventory["diagnostics"])
    assert all(
        occurrence["status"] == "included"
        for occurrence in inventory["include_occurrences"]
        if occurrence["file"] == "child.tex"
    )


def test_includeonly_is_not_retroactively_applied_to_an_earlier_include(tmp_path):
    (tmp_path / "child.tex").write_text(_statement("live-hidden"))
    inventory = _inventory(tmp_path, r"\include{child}\includeonly{}")
    assert [item["label"] for item in inventory["items"]] == ["live-hidden"]
    assert any(row["code"] == "unresolved_includeonly" for row in inventory["diagnostics"])


def test_include_inherits_environment_meanings_and_revokes_resumed_parent_authority(tmp_path):
    child = r"\def\flag{0}\begin{mutate}\ifnum\flag=0\relax "
    child += _statement("wrong") + r"\else " + _statement("right") + r"\fi\end{mutate}"
    (tmp_path / "child.tex").write_text(child)
    parent = r"\newenvironment{mutate}{\def\flag{1}}{}\input{child}\iffalse "
    parent += _statement("parent-candidate") + r"\fi"
    inventory = _inventory(tmp_path, parent)
    assert {item["label"] for item in inventory["items"]} == {"wrong", "right", "parent-candidate"}
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}
    assert (tmp_path / "child.tex").read_text() == child


def test_excluded_then_included_occurrence_never_reuses_fresh_child_authority(tmp_path):
    child = r"\iffalse " + _statement("candidate") + r"\fi"
    (tmp_path / "child.tex").write_text(child)
    root = tmp_path / "paper.tex"
    root.write_text(r"\includeonly{}\include{child}\input{child}")
    inventory = inventory_source(tmp_path, root)
    assert [item["label"] for item in inventory["items"]] == ["candidate"]
    assert inventory["items"][0]["scope"] == "candidate"
    assert any(
        row["code"] == "inherited_tex_meanings" and row["file"] == "child.tex"
        for row in inventory["diagnostics"]
    )
    _assert_regions_partition_sources(tmp_path, inventory)
