"""Keep superseded source censuses historical even when they recorded no diagnostics."""

from __future__ import annotations

import copy

import pytest

from leanflow_cli.formalization import freshness, intake
from leanflow_cli.formalization.proof_receipts import current_proof_receipt
from leanflow_cli.formalization.records import create_document, load_document, load_receipt
from leanflow_cli.formalization.service import show_document
from tests.leanflow import test_formalization_handoff as handoff_tests

reviewed_project = handoff_tests.reviewed


def test_zero_diagnostic_old_inventory_blocks_paid_author_admission(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(r"\begin{theorem}True.\end{theorem}")
    record = create_document(tmp_path, source)
    inventory = copy.deepcopy(record["inventory"])
    inventory["extraction_version"] = "provenance-scanner-v14"
    assert inventory["diagnostics"] == []
    before = copy.deepcopy(inventory)
    blockers = intake.author_blocking_diagnostics(inventory)
    assert blockers and any("retry_extraction" in row["message"] for row in blockers)
    assert inventory == before


def test_old_published_zero_diagnostic_receipt_is_saved_but_never_current(request, monkeypatch):
    monkeypatch.setattr(handoff_tests, "EXTRACTION_VERSION", "provenance-scanner-v14")
    root, _ = request.getfixturevalue("reviewed_project")
    record = load_document(root, "paper")
    receipt = load_receipt(record)
    assert not record["inventory"]["diagnostics"] and not record.get("source_reconciliation")
    monkeypatch.setattr(
        freshness,
        "environment_identity",
        lambda _: pytest.fail("Old source census must fail before expensive currentness checks"),
    )
    current = freshness.published_acceptance_freshness(root, record, receipt)
    assert not current["current"] and current["status"] == "acceptance_stale"
    assert "retry_extraction" in current["reason"]
    with pytest.raises(ValueError, match="retry_extraction"):
        current_proof_receipt(root, record, receipt)
    historical = show_document(root, "paper", verify_current=False)
    assert historical["currentness"] == "historical"
    assert not historical["can_prove"] and historical["handoff"] is None
    assert load_receipt(record) == receipt
