from __future__ import annotations

import pytest

from epflemma_cli.lean.lean_workflow_specs import (
    get_lean_spec,
    list_specs,
    load_lean_specs,
    specs_for_skill,
    validate_lean_specs,
)

SHIPPED_WORKFLOW_SPECS = {
    "prove",
    "formalize",
    "draft",
    "review",
    "refactor",
    "golf",
    "checkpoint",
    "doctor",
}

SHIPPED_HELPER_SPECS = {"search"}

SHIPPED_WORKER_SPECS = {
    "proof-repair",
    "proof-golfer",
    "axiom-eliminator",
    "sorry-filler-deep",
}


def test_validate_lean_workflow_specs_has_no_contract_errors():
    assert validate_lean_specs() == []


def test_core_workflow_specs_expose_aliases_without_active_worker_contracts():
    prove = get_lean_spec("prove")
    formalize = get_lean_spec("formalize")

    assert prove is not None
    assert formalize is not None
    assert "autoprove" in prove.aliases
    assert "autoformalize" in formalize.aliases
    assert prove.workers == ()
    assert formalize.workers == ()
    assert "lean_decompose_helpers" in prove.tools
    assert "lean_reasoning_help" in prove.tools


def test_prove_contract_recommends_helper_decomposition_for_hard_theorems():
    prove = get_lean_spec("prove")

    assert prove is not None
    assert "lean_decompose_helpers" in prove.content
    assert "structured sublemma split" in prove.content
    assert "decomposition blocker" in prove.content


def test_specs_for_skill_returns_native_workflow_links():
    spec_ids = {record.spec_id for record in specs_for_skill("lean-proof-loop")}

    assert "prove" in spec_ids
    assert "formalize" in spec_ids


@pytest.mark.parametrize("spec_id", sorted(SHIPPED_WORKFLOW_SPECS))
def test_every_shipped_workflow_spec_resolves_with_correct_kind(spec_id):
    record = get_lean_spec(spec_id)
    assert record is not None, f"workflow spec {spec_id!r} missing"
    assert record.kind == "workflow"
    assert record.summary, f"workflow spec {spec_id!r} has empty summary"


@pytest.mark.parametrize("spec_id", sorted(SHIPPED_WORKER_SPECS))
def test_every_shipped_worker_spec_resolves_with_correct_kind(spec_id):
    record = get_lean_spec(spec_id)
    assert record is not None, f"worker spec {spec_id!r} missing"
    assert record.kind == "worker"
    assert record.summary, f"worker spec {spec_id!r} has empty summary"


def test_list_specs_without_filter_returns_all_shipped_entries():
    ids = {record.spec_id for record in list_specs()}

    assert SHIPPED_WORKFLOW_SPECS <= ids
    assert SHIPPED_WORKER_SPECS <= ids
    assert SHIPPED_HELPER_SPECS <= ids


def test_list_specs_filters_workflows_and_workers_disjointly():
    workflows = {record.spec_id for record in list_specs("workflow")}
    workers = {record.spec_id for record in list_specs("worker")}
    helpers = {record.spec_id for record in list_specs("helper")}

    assert SHIPPED_WORKFLOW_SPECS <= workflows
    assert SHIPPED_WORKER_SPECS <= workers
    assert SHIPPED_HELPER_SPECS <= helpers
    assert workflows.isdisjoint(workers)
    assert workflows.isdisjoint(helpers)
    assert workers.isdisjoint(helpers)


def test_list_specs_unknown_kind_returns_empty():
    assert list_specs("bogus-kind") == []


def test_get_lean_spec_alias_lookup_matches_canonical_spec():
    via_alias = get_lean_spec("autoprove")
    via_canonical = get_lean_spec("prove")

    assert via_alias is not None
    assert via_canonical is not None
    assert via_alias.spec_id == via_canonical.spec_id


def test_get_lean_spec_returns_none_for_unknown():
    assert get_lean_spec("nonexistent-spec-xyz") is None
    assert get_lean_spec("") is None


def test_workflow_specs_reference_only_known_workers():
    specs = load_lean_specs()
    worker_ids = {record.spec_id for record in specs.values() if record.kind == "worker"}
    for record in specs.values():
        if record.kind != "workflow":
            continue
        unknown = [w for w in record.workers if w not in worker_ids]
        assert not unknown, (
            f"workflow {record.spec_id} references unknown workers {unknown}"
        )


def test_spec_content_does_not_leak_frontmatter_fence():
    for record in load_lean_specs().values():
        assert not record.content.startswith("---"), (
            f"spec {record.spec_id} content still carries frontmatter fence"
        )


def test_specs_for_skill_empty_or_missing_returns_empty_list():
    assert specs_for_skill("") == []
    assert specs_for_skill("no-such-skill-xyz") == []
