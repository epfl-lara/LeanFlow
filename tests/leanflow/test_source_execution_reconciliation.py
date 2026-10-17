"""Keep recognized TeX execution and I/O primitives outside lexical source-reading admission."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from leanflow_cli.formalization.source_inventory import inventory_source
from leanflow_cli.formalization.source_reconciliation import (
    apply_reconciliation,
    lexical_admission,
    reconciliation_requirements,
)


@pytest.mark.parametrize(
    "command",
    [
        r"\write18{touch forbidden}",
        r"\immediate\write18{touch forbidden}",
        r"\read0 to\result",
        r"\readline0 to\result",
        r"\openin0=source.dat",
        r"\openout0=output.dat",
        r"\closein0",
        r"\closeout0",
        r"\directlua{os.execute('forbidden')}",
        r"\latelua{os.execute('forbidden')}",
        r"\luaexec{os.execute('forbidden')}",
        r"\luafunction0",
        r"\special{ps: execution}",
        r"\pdfextension literal {execution}",
        r"\pdfliteral{execution}",
        r"\pdfximage{external.pdf}",
        r"\shipout\box0",
        r"\dump",
        r"\message{terminal output}",
        r"\errmessage{terminal output}",
    ],
)
def test_recognized_tex_effect_never_becomes_an_eligible_lexical_reading(
    tmp_path: Path, command: str
) -> None:
    source = tmp_path / "paper.tex"
    original = (r"\UnknownMathematicalSymbol \begin{theorem}True.\end{theorem}" + command).encode()
    source.write_bytes(original)
    inventory = inventory_source(tmp_path, source)
    record = {
        "revision_id": "revision-source",
        "source_bundle": inventory["bundle_hash"],
        "inventory": inventory,
    }
    hard = lexical_admission(inventory)
    effects = [row for row in hard if row["code"] == "unsupported_tex_effect"]
    assert effects and all(row["blocking"] for row in effects)
    requirements = reconciliation_requirements(record)
    assert not {row["diagnostic_id"] for row in requirements["diagnostics"]}.intersection(
        row["id"] for row in effects
    )
    for effect in effects:
        assert effect["spans"]
        assert all(
            original[span["byte_start"] : span["byte_end"]].startswith(b"\\")
            for span in effect["spans"]
        )
    proposal = {
        "requirements_id": requirements["requirements_id"],
        "entries": [
            {
                "diagnostic_id": row["diagnostic_id"],
                "interpretation": "A literal mathematical reading.",
                "reason": "Requires ordinary independent source review.",
            }
            for row in requirements["diagnostics"]
        ],
    }
    candidate = copy.deepcopy(record)
    apply_reconciliation(candidate, proposal)
    assert {row["id"] for row in lexical_admission(candidate["inventory"])}.issuperset(
        row["id"] for row in effects
    )
    assert source.read_bytes() == original
    assert not (tmp_path / "output.dat").exists()


def test_unknown_mathematical_symbol_remains_a_pending_lexical_candidate(tmp_path: Path) -> None:
    source = tmp_path / "paper.tex"
    source.write_text(r"\UnknownMathematicalSymbol \writeSymbol \begin{theorem}True.\end{theorem}")
    inventory = inventory_source(tmp_path, source)
    assert lexical_admission(inventory) == []
    assert {row["code"] for row in inventory["diagnostics"]} == {"unresolved_command"}
    assert {
        row["name"] for row in inventory["command_references"] if row["resolution"] == "unresolved"
    } == {"UnknownMathematicalSymbol", "writeSymbol"}


def test_local_shadowing_does_not_certify_a_known_effect_primitive(tmp_path: Path) -> None:
    source = tmp_path / "paper.tex"
    original = (
        rb"\def\write#1{A symbolic operation.}\write18{body}\begin{theorem}True.\end{theorem}"
    )
    source.write_bytes(original)
    inventory = inventory_source(tmp_path, source)
    assert any(
        row["name"] == "write" and row["resolution"] == "local_definition"
        for row in inventory["command_references"]
    )
    assert any(row["code"] == "unsupported_tex_effect" for row in lexical_admission(inventory))
    assert source.read_bytes() == original
