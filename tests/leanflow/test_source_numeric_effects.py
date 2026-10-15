"""Retain branches when helper arguments, scope hooks or external reads can change TeX constants."""

from __future__ import annotations

import pytest

from tests.leanflow.test_source_numeric_conditionals import _inventory, _statement


def _branches(prefix: str, comparison: str = r"\flag=0\relax") -> str:
    return (
        prefix
        + r"\ifnum"
        + comparison
        + " "
        + _statement("maybe")
        + r"\else "
        + _statement("alternative")
        + r"\fi"
    )


@pytest.mark.parametrize(
    "effect",
    [
        r"\def\close{\end{proof}}\begin{proof}\def\flag{1}\close",
        r"\newenvironment{custom}{\def\flag{1}}{}\def\open{\begin{custom}}\open",
        r"\def\execute#1{#1}\execute{\def\flag{1}}",
        r"\newcommand{\execute}[1]{#1}\execute{\def\flag{1}}",
        r"\def\execute#1{#1}\def\indirect{\execute{\def\flag{1}}}\indirect",
        r"\def\execute#1{#1}\execute{\UnknownEffect}",
        r"\def\execute#1{#1}\execute X",
        r"\def\execute#1X{#1}\execute{value}X",
        r"\newcommand{\execute}[1][default]{#1}\execute[value]",
        r"\def\open{$}\open\def\flag{1}",
        r"\def\close{$}\close",
        r"\def\endquote{\gdef\flag{1}\endlist}\begin{quote}text\end{quote}",
    ],
)
def test_hidden_scope_or_argument_effects_cannot_exclude_either_branch(tmp_path, effect):
    inventory = _inventory(tmp_path, _branches(r"\def\flag{0}" + effect))
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}
    assert {item["label"] for item in inventory["items"]} == {"maybe", "alternative"}


@pytest.mark.parametrize("loader", [r"\bibliography{refs}", r"\tableofcontents"])
def test_external_bibliography_or_toc_effects_cannot_supply_numeric_authority(tmp_path, loader):
    (tmp_path / "refs.bbl").write_text(r"\gdef\flag{1}")
    (tmp_path / "paper.toc").write_text(r"\gdef\flag{1}")
    inventory = _inventory(tmp_path, _branches(r"\def\flag{0}" + loader))
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}


@pytest.mark.parametrize(
    "definition,invocation",
    [
        (r"\def\endinput{}", r"\endinput"),
        (r"\def\begin#1{anything}", ""),
        (r"\def\end#1{anything}", ""),
        (r"\def\iftrue{anything}", r"\iftrue"),
        (r"\def\iffalse{anything}", r"\iffalse"),
        (r"\def\begingroup{\def\flag{1}}", r"\begingroup"),
        (r"\def\endgroup{}", r"\endgroup"),
        (r"\def\usepackage#1{anything}", r"\usepackage{color}"),
    ],
)
def test_redefined_scanner_controls_block_even_without_a_later_numeric_conditional(
    tmp_path, definition, invocation
):
    inventory = _inventory(tmp_path, definition + invocation + _statement("surviving"))
    assert any(row["code"] == "redefined_scanner_control" for row in inventory["diagnostics"])
    assert inventory["extraction_status"] == "blocked"
    assert [item["label"] for item in inventory["items"]] == ["surviving"]
    assert inventory["items"][0]["scope"] == "candidate"


@pytest.mark.parametrize("environment", ["comment", "verbatim", "Verbatim", "lstlisting", "minted"])
@pytest.mark.parametrize("redefinition", ["environment", "end_hook"])
def test_source_redefined_literal_blocks_are_not_silently_discarded(
    tmp_path, environment, redefinition
):
    definition = (
        rf"\newenvironment{{{environment}}}{{}}{{}}"
        if redefinition == "environment"
        else rf"\def\end{environment}{{}}"
    )
    inventory = _inventory(
        tmp_path,
        definition
        + rf"\begin{{{environment}}}"
        + _statement("surviving")
        + rf"\end{{{environment}}}",
    )
    assert any(row["code"] == "redefined_scanner_control" for row in inventory["diagnostics"])
    assert [item["label"] for item in inventory["items"]] == ["surviving"]
    assert inventory["items"][0]["scope"] == "candidate"


@pytest.mark.parametrize("open_,close", [("$", "$"), ("$$", "$$"), (r"\(", r"\)"), (r"\[", r"\]")])
def test_assignments_within_math_refuse_implicit_scope_guesses(tmp_path, open_, close):
    inventory = _inventory(tmp_path, _branches(r"\def\flag{0}" + open_ + r"\def\flag{1}" + close))
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}


def test_implicit_left_right_math_groups_cannot_be_guessed_for_assignments(tmp_path):
    inventory = _inventory(tmp_path, _branches(r"\def\flag{0}$\left(\def\flag{1}\right)$"))
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert len(inventory["items"]) == 2


@pytest.mark.parametrize("space", [" ", "\n", "\t "])
def test_control_word_whitespace_does_not_terminate_numeric_token_concatenation(tmp_path, space):
    source = _branches(r"\def\digit{1}", "12=\\digit" + space + r"2\relax")
    inventory = _inventory(tmp_path, source)
    assert not inventory["diagnostics"]
    assert [item["label"] for item in inventory["items"]] == ["maybe"]


@pytest.mark.parametrize(
    "helper,call",
    [
        (r"\def\execute#1{#1}", r"\execute{plain text}"),
        (r"\newcommand{\set}[1]{\left\{{#1}\right\}}", r"\set{\alpha + \flag}"),
        (r"\newcommand{\pair}[2]{(#1,#2)}", r"\pair{first}{second}"),
        (r"\newcommand{\supmark}[1]{$^{#1}$}", r"\supmark{\flag}"),
    ],
)
def test_plain_required_brace_helpers_receive_only_bounded_argument_effect_checks(
    tmp_path, helper, call
):
    inventory = _inventory(tmp_path, _branches(r"\def\flag{0}" + helper + call))
    assert not inventory["diagnostics"]
    assert [item["label"] for item in inventory["items"]] == ["maybe"]
    assert not inventory["audit_complete"]


def test_required_argument_and_effect_graph_budgets_fail_closed(tmp_path):
    helper = r"\def\execute#1{#1}\execute{" + "x" * 4097 + "}"
    inventory = _inventory(tmp_path, _branches(r"\def\flag{0}" + helper))
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    chain = "".join(
        rf"\def\helper{letter}{{\helper{next_letter}}}"
        for letter, next_letter in zip("abcdefghijklmnop", "bcdefghijklmnopq", strict=True)
    )
    chain += r"\def\helperq{plain}\helpera"
    inventory = _inventory(tmp_path, _branches(r"\def\flag{0}" + chain))
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
