"""Keep unfrozen file loading and saved macro I/O outside native lexical admission."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_reconciliation import (
    apply_reconciliation,
    assert_current_extraction,
    assert_reconciliation_reviewed,
    lexical_admission,
    reconciliation_requirements,
)


def _inventory(tmp_path: Path, text: str) -> dict:
    """Retain a real unfrozen include candidate beside the immutable selected source."""
    (tmp_path / "hidden.tex").write_text(
        r"\begin{theorem}\label{hidden}Hidden mathematics.\end{theorem}"
    )
    source = tmp_path / "paper.tex"
    source.write_text(text + r"\begin{theorem}\label{visible}Visible.\end{theorem}")
    before = source.read_bytes()
    value = inventory_source(tmp_path, source)
    assert source.read_bytes() == before
    return value


@pytest.mark.parametrize(
    "command",
    [
        r"\@input{hidden.tex}",
        r"\@@input hidden.tex ",
        r"\import{./}{hidden.tex}",
        r"\subimport{./}{hidden.tex}",
        r"\Import{./}{hidden.tex}",
        r"\SubImport{./}{hidden.tex}",
        r"\inputfrom{./}{hidden.tex}",
        r"\subinputfrom{./}{hidden.tex}",
        r"\includefrom{./}{hidden.tex}",
        r"\subincludefrom{./}{hidden.tex}",
        r"\InputIfFileExists{hidden.tex}{}{}",
        r"\IfFileExists{hidden.tex}{}{}",
        r"\includepdf{hidden.pdf}",
        r"\includestandalone{hidden}",
    ],
)
def test_unexpanded_file_commands_remain_hard_after_loader_uncertainty(
    tmp_path: Path, command: str
) -> None:
    inventory = _inventory(tmp_path, r"\documentclass{article}\makeatletter " + command)
    assert "hidden.tex" not in inventory["files"]
    effects = [
        row for row in lexical_admission(inventory) if row["code"] == "unsupported_tex_effect"
    ]
    assert effects
    assert all(row["blocking"] for row in effects)
    record = {
        "revision_id": "rev",
        "source_bundle": inventory["bundle_hash"],
        "inventory": inventory,
    }
    expected = {row["diagnostic_id"] for row in reconciliation_requirements(record)["diagnostics"]}
    assert not expected.intersection(row["id"] for row in effects)


@pytest.mark.parametrize(
    "definition,invocation",
    [
        (r"\def\later{\write18{forbidden}}", r"\later"),
        (r"\gdef\later{\openout0=output.dat}", r"\later"),
        (r"\edef\later{\input{hidden.tex}}", r"\later"),
        (r"\newcommand{\later}{\input{hidden.tex}}", r"\later"),
        (r"\DeclareRobustCommand{\later}{\directlua{forbidden}}", r"\later"),
        (r"\newcommand{\later}{\includegraphics{missing.pdf}}", r"\later"),
        (r"\newenvironment{later}{}{\input{hidden.tex}}", r"\begin{later}A.\end{later}"),
    ],
)
def test_early_macro_replacement_effects_survive_later_loader_trust_loss(
    tmp_path: Path, definition: str, invocation: str
) -> None:
    inventory = _inventory(tmp_path, definition + r"\documentclass{article}" + invocation)
    effects = [
        row for row in lexical_admission(inventory) if row["code"] == "unsupported_tex_effect"
    ]
    assert effects
    spans = [span for row in effects for span in row.get("spans", [row.get("span")]) if span]
    assert any(span["byte_start"] < len(definition.encode()) for span in spans)
    assert not (tmp_path / "output.dat").exists()


def test_unknown_at_command_is_reported_but_an_exact_local_math_definition_is_retained(
    tmp_path,
) -> None:
    inventory = _inventory(tmp_path, r"\@UnknownMathSymbol \def\@LocalMath{1}\@LocalMath ")
    references = {row["name"]: row for row in inventory["command_references"]}
    assert references["@UnknownMathSymbol"]["resolution"] == "unresolved"
    assert references["@LocalMath"]["resolution"] == "local_definition"
    assert any("@UnknownMathSymbol" in row["message"] for row in inventory["diagnostics"])


def test_definitely_inactive_macro_io_is_not_executed_or_admitted_as_a_definition(tmp_path) -> None:
    inventory = _inventory(tmp_path, r"\iffalse\def\later{\input{hidden.tex}}\fi ")
    assert not inventory["macro_definitions"]
    assert not lexical_admission(inventory)
    assert [row["label"] for row in inventory["items"]] == ["visible"]


@pytest.mark.parametrize(
    "version", [None, "provenance-scanner-v9", "provenance-scanner-v12", "provenance-scanner-v14"]
)
def test_old_or_unversioned_inventory_cannot_gain_new_native_lexical_authority(
    tmp_path, version
) -> None:
    inventory = _inventory(tmp_path, r"\documentclass{article}\UnknownMathSymbol ")
    inventory["extraction_version"] = version
    before = copy.deepcopy(inventory)
    blockers = [row for row in inventory["diagnostics"] if row["blocking"]]
    assert lexical_admission(inventory) == blockers
    record = {
        "revision_id": "rev",
        "source_bundle": inventory["bundle_hash"],
        "inventory": inventory,
    }
    assert not reconciliation_requirements(record)["diagnostics"]
    with pytest.raises(ValueError, match="retry_extraction"):
        apply_reconciliation(record, {"requirements_id": "old", "entries": []})
    assert inventory == before


def test_old_zero_diagnostic_tex_inventory_cannot_reach_fresh_empty_acceptance(tmp_path) -> None:
    inventory = _inventory(tmp_path, "")
    assert not inventory["diagnostics"]
    inventory["extraction_version"] = "provenance-scanner-v14"
    record = {
        "revision_id": "rev",
        "source_bundle": inventory["bundle_hash"],
        "inventory": inventory,
    }
    for operation in (
        lambda: assert_current_extraction(inventory),
        lambda: apply_reconciliation(record, None),
        lambda: assert_reconciliation_reviewed(record, []),
    ):
        with pytest.raises(ValueError, match="retry_extraction"):
            operation()
    assert_reconciliation_reviewed(record, [], historical=True)
    assert "source_reconciliation" not in record


def test_saved_review_replay_remains_historical_after_policy_upgrade(tmp_path, monkeypatch) -> None:
    from leanflow_cli.formalization import source_reconciliation

    inventory = _inventory(tmp_path, r"\documentclass{article}\UnknownMathSymbol ")
    record = {
        "revision_id": "rev",
        "source_bundle": inventory["bundle_hash"],
        "inventory": inventory,
    }
    requirements = reconciliation_requirements(record)
    apply_reconciliation(
        record,
        {
            "requirements_id": requirements["requirements_id"],
            "entries": [
                {
                    "diagnostic_id": row["diagnostic_id"],
                    "interpretation": "Literal reading.",
                    "reason": "Independent source check.",
                }
                for row in requirements["diagnostics"]
            ],
        },
    )
    reviews = [
        {
            "revision_id": "rev",
            "cleanup_verified": True,
            "accepted": True,
            "coherence_decision": "PASS",
            "coverage": [
                {"region_id": row["id"], "decision": "PASS", "rationale": "Source checked."}
                for row in inventory["regions"]
            ],
        }
    ]
    before = copy.deepcopy(record)
    monkeypatch.setattr(source_reconciliation, "EXTRACTION_VERSION", "future-policy")
    with pytest.raises(ValueError, match="retry_extraction"):
        assert_reconciliation_reviewed(record, reviews)
    assert_reconciliation_reviewed(record, reviews, historical=True)
    assert record == before
