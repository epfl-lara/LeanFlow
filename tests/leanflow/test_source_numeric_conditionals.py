"""Admit literal numeric TeX branches without losing inactive bytes or guessing expansion effects."""

from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_provenance import SourceText, scan_tex
from tests.leanflow.test_source_inventory import _assert_regions_partition_sources


def _inventory(root: Path, source: str) -> dict:
    path = root / "paper.tex"
    path.write_text(source)
    inventory = inventory_source(root, path)
    assert path.read_bytes().decode() == source
    assert inventory["files"]["paper.tex"]["text"] == source
    _assert_regions_partition_sources(root, inventory)
    return inventory


def _statement(label: str) -> str:
    return rf"\begin{{theorem}}\label{{{label}}}{label} holds.\end{{theorem}}"


def test_exact_robust_fuzzy_extractor_preamble_retains_layout_uncertainty_after_loading(tmp_path):
    preamble = (
        "\\def\\lncs{0}\n"
        "\\def\\marginnotes{1}\n"
        "\\ifnum\\lncs=0\n"
        "\\documentclass[11pt]{article}\n"
        "\\usepackage{fullpage}\n"
        "\\else\n"
        "\\documentclass[runningheads]{llncs}\n"
        "\\pagestyle{headings}\n"
        "\\fi\n"
    )
    source = (
        preamble
        + r"\ifnum\lncs=1 "
        + _statement("inactive")
        + r"\else "
        + _statement("live")
        + r"\fi"
    )
    inventory = _inventory(tmp_path, source)
    assert {row["code"] for row in inventory["diagnostics"]} >= {
        "unresolved_loader_effect",
        "unresolved_conditional",
    }
    assert [item["label"] for item in inventory["items"]] == ["inactive", "live"]
    assert inventory["extraction_status"] == "blocked" and not inventory["audit_complete"]
    scan = scan_tex(SourceText("paper.tex", source.encode()))
    conditional = next(mark for mark in scan.marks if mark[0] == 33)
    assert conditional[2:] == ("supporting_context", "Conditional delimiter")
    assert source[conditional[0] : conditional[1]] == "\\ifnum\\lncs=0\n"
    assert "llncs" in scan.visible and "article" in scan.visible
    inactive_offset = source.index("runningheads")
    assert any(a <= inactive_offset < b and kind == "unresolved" for a, b, kind, _ in scan.marks)
    assert len(scan.visible) == len(source) and scan.visible.count("\n") == source.count("\n")


@pytest.mark.parametrize(
    "test,expected", [("2=2", True), ("2<3", True), ("2>3", False), ("-2<+1", True)]
)
def test_literal_numeric_relations_and_unicode_offsets_are_exact(tmp_path, test, expected):
    source = (
        "αβ\n"
        + r"\ifnum"
        + test
        + r"\relax "
        + _statement("then")
        + r"\else "
        + _statement("otherwise")
        + r"\fi"
    )
    inventory = _inventory(tmp_path, source)
    assert not inventory["diagnostics"]
    item = inventory["items"][0]
    assert item["label"] == ("then" if expected else "otherwise")
    for span in item["statement_spans"]:
        assert source.encode()[span["byte_start"] : span["byte_end"]].decode() in item["text"]


def test_direct_literal_redefinitions_and_local_scopes_do_not_leak(tmp_path):
    source = r"\def\flag{0}{\def\flag{1}\ifnum\flag=1\relax " + _statement("brace-local") + r"\fi}"
    source += (
        r"\begingroup\def\flag{2}\ifnum\flag=2\relax "
        + _statement("explicit-local")
        + r"\fi\endgroup"
    )
    source += (
        r"\begin{proof}\def\flag{3}\ifnum\flag=3\relax "
        + _statement("environment-local")
        + r"\fi\end{proof}"
    )
    source += r"\ifnum\flag=0\relax " + _statement("outer") + r"\fi"
    source += r"\def\flag{4}\ifnum\flag=4\relax " + _statement("redefined") + r"\fi"
    inventory = _inventory(tmp_path, source)
    assert not inventory["diagnostics"]
    assert [item["label"] for item in inventory["items"]] == [
        "brace-local",
        "explicit-local",
        "environment-local",
        "outer",
        "redefined",
    ]


def test_nested_conditions_and_inactive_assignments_preserve_full_inactive_regions(tmp_path):
    source = (
        r"\def\flag{0}\ifnum\flag=1\relax\def\flag{9}\iftrue "
        + _statement("never")
        + r"\fi\else\ifnum\flag<1\relax "
        + _statement("live")
        + r"\else "
        + _statement("also-never")
        + r"\fi\fi"
    )
    source += r"\ifnum\flag=0\relax " + _statement("still-zero") + r"\fi"
    inventory = _inventory(tmp_path, source)
    assert not inventory["diagnostics"]
    assert [item["label"] for item in inventory["items"]] == ["live", "still-zero"]
    assert any(region["disposition"] == "excluded" for region in inventory["regions"])


@pytest.mark.parametrize(
    "definition",
    [
        r"\def\flag#1{0}",
        r"\def\flag{\flag}",
        r"\def\other{0}\def\flag{\other}",
        r"\newcommand{\flag}{0}",
        r"\gdef\flag{0}",
        r"\edef\flag{0}",
        r"\xdef\flag{0}",
        r"\def\flag{2147483648}",
    ],
)
def test_unsupported_recursive_parameterized_and_global_definitions_remain_uncertain(
    tmp_path, definition
):
    inventory = _inventory(
        tmp_path,
        definition
        + r"\ifnum\flag=0\relax "
        + _statement("maybe")
        + r"\else "
        + _statement("alternative")
        + r"\fi",
    )
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}
    assert len(inventory["items"]) == 2


@pytest.mark.parametrize(
    "effect",
    [
        r"\let\flag\other",
        r"\global\def\flag{1}",
        r"\catcode`\%=12",
        r"\PotentialMutation",
        r"\Unknown@Mutation",
        r"\newlength\flag",
        r"\def\mutate{\setlength\flag{1pt}}\mutate",
        r"\def\mutate{\def\flag{1}}\mutate",
        r"\def\mutate{\def\flag{1}}\def\indirect{\mutate}\indirect",
        r"\def\mutate{\UnknownBodyEffect}\mutate",
        r"\def\repeat{\repeat}\repeat",
        r"\input{macros}",
        r"\def\ifnum#1{anything}",
    ],
)
def test_effectful_or_unknown_expansion_cannot_hide_later_branches(tmp_path, effect):
    inventory = _inventory(
        tmp_path,
        r"\def\flag{0}"
        + effect
        + r"\ifnum\flag=0\relax "
        + _statement("maybe")
        + r"\else "
        + _statement("alternative")
        + r"\fi",
    )
    assert any(
        row["code"] in {"unresolved_conditional", "redefined_scanner_control"}
        for row in inventory["diagnostics"]
    )
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}


@pytest.mark.parametrize(
    "loader",
    [
        r"\usepackage{custom}",
        r"\RequirePackage{custom}",
        r"\documentclass{custom}",
        r"\usepackage[unknown]{color}",
        r"\documentclass[unknown]{article}",
        r"\usepackage{\packageName}",
        r"\def\load{\usepackage{color}}\load",
        r"\def\load{\documentclass{article}}\load",
    ],
)
def test_unsupported_external_loaders_and_helper_loaders_preserve_both_branches(tmp_path, loader):
    inventory = _inventory(
        tmp_path,
        r"\def\flag{0}"
        + loader
        + r"\ifnum\flag=0\relax "
        + _statement("maybe")
        + r"\else "
        + _statement("alternative")
        + r"\fi",
    )
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}
    assert {item["label"] for item in inventory["items"]} == {"maybe", "alternative"}


@pytest.mark.parametrize(
    "loader,filename",
    [
        (r"\usepackage{custom}", "custom.sty"),
        (r"\usepackage{color}", "color.sty"),
        (r"\RequirePackage{amsmath}", "amsmath.sty"),
        (r"\documentclass{article}", "article.cls"),
    ],
)
def test_local_package_or_class_effects_never_receive_standard_loader_authority(
    tmp_path, loader, filename
):
    local_source = r"\def\flag{1}"
    (tmp_path / filename).write_text(local_source)
    inventory = _inventory(
        tmp_path,
        r"\def\flag{0}"
        + loader
        + r"\ifnum\flag=0\relax "
        + _statement("maybe")
        + r"\else "
        + _statement("alternative")
        + r"\fi",
    )
    assert inventory["files"][filename]["text"] == local_source
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}
    assert len(inventory["items"]) == 2


def test_standard_loaders_never_grant_numeric_or_structural_authority(tmp_path):
    inventory = _inventory(
        tmp_path,
        r"\def\flag{0}\documentclass[11pt]{article}"
        r"\usepackage{fullpage, amsmath, amssymb, amsfonts, mathrsfs,verbatim,color}"
        r"\RequirePackage{amsthm}\ifnum\flag=0\relax "
        + _statement("live")
        + r"\else "
        + _statement("inactive")
        + r"\fi",
    )
    assert {row["code"] for row in inventory["diagnostics"]} >= {
        "unresolved_loader_effect",
        "unresolved_conditional",
    }
    assert [item["label"] for item in inventory["items"]] == ["live", "inactive"]
    assert inventory["extraction_status"] == "blocked" and not inventory["audit_complete"]
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}


def test_standard_loader_without_bundle_context_cannot_presume_external_macro_purity():
    source = r"\def\flag{0}\usepackage{color}\ifnum\flag=0\relax yes\else no\fi"
    scan = scan_tex(SourceText("paper.tex", source.encode()))
    assert any(row["code"] == "unresolved_conditional" for row in scan.diagnostics)
    assert "yes" in scan.visible and "no" in scan.visible


def test_builtin_name_redefinition_cannot_supply_a_package_compatible_numeric_constant(tmp_path):
    inventory = _inventory(
        tmp_path,
        r"\def\em{0}\usepackage{amsmath}\ifnum\em=0\relax " + _statement("maybe") + r"\fi",
    )
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert inventory["items"][0]["scope"] == "candidate"


def test_uncertain_branch_assignment_never_becomes_a_literal_constant(tmp_path):
    source = (
        r"\def\flag{0}\ifUnknown\def\flag{1}\else\def\flag{2}\fi\ifnum\flag=0\relax "
        + _statement("maybe")
        + r"\else "
        + _statement("alternative")
        + r"\fi"
    )
    inventory = _inventory(tmp_path, source)
    assert sum(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"]) == 2
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}


@pytest.mark.parametrize(
    "condition",
    [
        r"0=0\UnknownExpansion",
        "0=0% hidden token joining\n1",
        r"0=\numexpr1-1\relax",
        "2147483648=0",
        r"\count0=0",
        "'3=3",
        "0=\u00a00",
    ],
)
def test_unsupported_numeric_operands_and_unknown_termination_stay_blocked(tmp_path, condition):
    inventory = _inventory(
        tmp_path,
        r"\ifnum"
        + condition
        + "\n"
        + _statement("maybe")
        + r"\else "
        + _statement("alternative")
        + r"\fi",
    )
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert {item["scope"] for item in inventory["items"]} == {"candidate"}


def test_literal_token_concatenation_is_not_misread_as_a_terminated_integer(tmp_path):
    source = (
        r"\def\digit{1}\ifnum1=0\digit\relax "
        + _statement("one")
        + r"\else "
        + _statement("wrong")
        + r"\fi"
    )
    inventory = _inventory(tmp_path, source)
    assert not inventory["diagnostics"]
    assert [item["label"] for item in inventory["items"]] == ["one"]


def test_standard_operator_font_and_bibliography_controls_do_not_waive_unknown_commands(tmp_path):
    source = r"\def\flag{0}\DeclareMathOperator*{\expe}{\mathbf E}\def\A{{\cal A}}\def\adv{{\A}}\def\bydef{\stackrel{\mbox{\tiny{def}}}{=}}\def\definitionclose{\hspace*{\fill}$\diamondsuit$}\adv\bydef\definitionclose\ifnum\flag=0\relax "
    source += r"\begin{theorem}$\expe X\bmod 2$ for \em text, \sf text, \S and \aa.\end{theorem}"
    source += r"\fi\begin{thebibliography}{1}\bibitem{x}\newblock Citation.\end{thebibliography}\ActualUnknownCommand"
    inventory = _inventory(tmp_path, source)
    assert {row["code"] for row in inventory["diagnostics"]} == {"unresolved_command"}
    assert all("ActualUnknownCommand" in row["message"] for row in inventory["diagnostics"])
    operator = next(row for row in inventory["macro_definitions"] if row["name"] == "expe")
    assert operator["command"] == "DeclareMathOperator" and operator["body"] == r"\mathbf E"
    span = operator["span"]
    assert (
        source.encode()[span["byte_start"] : span["byte_end"]].decode()
        == r"\DeclareMathOperator*{\expe}{\mathbf E}"
    )
    assert operator["id"] in inventory["items"][0]["context_refs"]


def test_group_depth_and_effect_graph_bounds_fail_closed(tmp_path):
    source = "{" * 129 + r"\ifnum0=0\relax " + _statement("maybe") + r"\fi" + "}" * 129
    inventory = _inventory(tmp_path, source)
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert inventory["items"][0]["scope"] == "candidate"


def test_other_conditionals_keep_the_existing_conservative_policy(tmp_path):
    inventory = _inventory(tmp_path, r"\ifdim1pt=1pt " + _statement("maybe") + r"\fi")
    assert any(row["code"] == "unresolved_conditional" for row in inventory["diagnostics"])
    assert inventory["items"][0]["scope"] == "candidate"
