from __future__ import annotations

from epflemma_cli.lean_workflow_specs import (
    get_lean_spec,
    specs_for_skill,
    validate_lean_specs,
)


def test_validate_lean_workflow_specs_has_no_contract_errors():
    assert validate_lean_specs() == []


def test_core_workflow_specs_expose_alias_and_worker_contracts():
    prove = get_lean_spec("prove")
    formalize = get_lean_spec("formalize")

    assert prove is not None
    assert formalize is not None
    assert "autoprove" in prove.aliases
    assert "autoformalize" in formalize.aliases
    assert "proof-repair" in prove.workers
    assert "axiom-eliminator" in formalize.workers


def test_specs_for_skill_returns_native_workflow_links():
    spec_ids = {record.spec_id for record in specs_for_skill("lean-proof-loop")}

    assert "prove" in spec_ids
    assert "formalize" in spec_ids
