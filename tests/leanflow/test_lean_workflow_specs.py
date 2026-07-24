from __future__ import annotations

import pytest

from leanflow_cli.lean.lean_workflow_specs import (
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
    "doctor",
}

SHIPPED_HELPER_SPECS = {"search"}

SHIPPED_PHASE_SPECS = {
    "phase-search",
    "phase-draft",
    "phase-review",
    "phase-negation",
    "phase-planning",
}

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


def test_prove_contract_treats_plan_notes_as_historical_not_inventory():
    prove = get_lean_spec("prove")

    assert prove is not None
    assert "never paginate it" in prove.content
    assert "user-owned historical context" in prove.content
    normalized = " ".join(prove.content.split())
    assert "current Lean source/kernel diagnostics outrank" in normalized
    assert "Do not read raw `summary.json` or `blueprint.json`" in prove.content


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

    assert ids >= SHIPPED_WORKFLOW_SPECS
    assert ids >= SHIPPED_WORKER_SPECS
    assert ids >= SHIPPED_HELPER_SPECS
    assert ids >= SHIPPED_PHASE_SPECS


def test_list_specs_filters_workflows_and_workers_disjointly():
    workflows = {record.spec_id for record in list_specs("workflow")}
    workers = {record.spec_id for record in list_specs("worker")}
    helpers = {record.spec_id for record in list_specs("helper")}
    phases = {record.spec_id for record in list_specs("phase")}

    assert workflows >= SHIPPED_WORKFLOW_SPECS
    assert workers >= SHIPPED_WORKER_SPECS
    assert helpers >= SHIPPED_HELPER_SPECS
    assert phases >= SHIPPED_PHASE_SPECS
    assert workflows.isdisjoint(workers)
    assert workflows.isdisjoint(helpers)
    assert workers.isdisjoint(helpers)
    assert phases.isdisjoint(workflows | workers | helpers)


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
        assert not unknown, f"workflow {record.spec_id} references unknown workers {unknown}"


def test_spec_content_does_not_leak_frontmatter_fence():
    for record in load_lean_specs().values():
        assert not record.content.startswith(
            "---"
        ), f"spec {record.spec_id} content still carries frontmatter fence"


def test_specs_for_skill_empty_or_missing_returns_empty_list():
    assert specs_for_skill("") == []
    assert specs_for_skill("no-such-skill-xyz") == []


# ---------------------------------------------------------------------------
# Phase 6 §6.9: phase fragments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec_id", sorted(SHIPPED_PHASE_SPECS))
def test_every_shipped_phase_fragment_resolves_with_contract(spec_id):
    record = get_lean_spec(spec_id)
    assert record is not None, f"phase fragment {spec_id!r} missing"
    assert record.kind == "phase"
    assert record.summary
    assert record.consumed_by, f"{spec_id!r} declares no consumers"
    assert record.deliverable_schema, f"{spec_id!r} declares no deliverable schema"
    # The schema is machine-consumable YAML describing a mapping.
    import yaml

    parsed = yaml.safe_load(record.deliverable_schema)
    assert isinstance(parsed, dict)
    assert record.content.strip(), f"{spec_id!r} has no body"


def test_phase_fragments_never_reach_skill_prompts():
    """Fragments embed via get_lean_spec by their consumers, never via the
    skill-prompt path — no fragment may declare skills."""
    for record in list_specs("phase"):
        assert record.skills == (), f"{record.spec_id} leaks into skill prompts"


def test_phase_review_vocabulary_matches_orchestrator_routes():
    """§6.9: ONE action vocabulary, aligned to the route enum."""
    from leanflow_cli.workflows.orchestrator import ROUTES

    record = get_lean_spec("phase-review")
    assert record is not None
    import yaml

    schema = yaml.safe_load(record.deliverable_schema)
    actions = {a.strip() for a in str(schema["action"]).split("|")}
    # `continue` is the reviewer's word for the direct-prove route (§6.9).
    assert actions - {"continue"} <= set(ROUTES)
    assert "continue" in actions
    assert "Difficulty and exhausted routes are not parking reasons" in record.content
    for retired in ("deep", "repair", "redraft", "golf", "replan", "falsify"):
        assert retired not in actions


def test_deferring_specs_deliver_their_fragments():
    """A spec that defers to a fragment must carry it into the prompt: the
    prover/search/draft/review skill prompts embed the fragments their
    specs point at (finding: pointing at an invisible contract is worse
    than inlining it)."""
    from leanflow_cli.runtime.skill_core import build_skill_prompt

    for spec_id, phase_id in (
        ("prove", "phase-search"),
        ("prove", "phase-draft"),
        ("search", "phase-search"),
        ("draft", "phase-draft"),
        ("review", "phase-review"),
    ):
        record = get_lean_spec(spec_id)
        assert record is not None and phase_id in record.phases, (spec_id, phase_id)

    prompt = build_skill_prompt("lean-proof-loop")
    assert "[PHASE SPEC: phase-search]" in prompt
    assert "[PHASE SPEC: phase-draft]" in prompt
    # Deduped: prove and formalize share the skill; fragments appear once.
    assert prompt.count("[PHASE SPEC: phase-search]") == 1


def test_phases_field_validates_against_known_fragments(tmp_path, monkeypatch):
    import leanflow_cli.lean.lean_workflow_specs as specs_mod

    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "w.md").write_text(
        "---\nid: w\nkind: workflow\ntitle: W\nsummary: s\nphases: [phase-ghost]\n---\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(specs_mod, "SPEC_ROOT", tmp_path)
    specs_mod.load_lean_specs.cache_clear()
    try:
        errors = "\n".join(specs_mod.validate_lean_specs())
        assert "w: unknown phase fragment 'phase-ghost'" in errors
    finally:
        specs_mod.load_lean_specs.cache_clear()


def test_validator_flags_broken_phase_fragments(tmp_path, monkeypatch):
    import leanflow_cli.lean.lean_workflow_specs as specs_mod

    phases = tmp_path / "phases"
    phases.mkdir()
    (phases / "bad.md").write_text(
        "---\nid: phase-bad\nkind: phase\ntitle: Bad\nsummary: s\n"
        "consumed_by: [martian]\n---\n\nbody\n",
        encoding="utf-8",
    )
    (phases / "empty.md").write_text(
        "---\nid: phase-empty\nkind: phase\ntitle: Empty\nsummary: s\n---\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(specs_mod, "SPEC_ROOT", tmp_path)
    specs_mod.load_lean_specs.cache_clear()
    try:
        errors = "\n".join(specs_mod.validate_lean_specs())
        assert "unknown phase consumer 'martian'" in errors
        assert "phase-bad: phase fragment declares no deliverable_schema" in errors
        assert "phase-empty: phase fragment declares no consumed_by" in errors
    finally:
        specs_mod.load_lean_specs.cache_clear()


def test_duplicate_spec_ids_are_refused_loudly(tmp_path, monkeypatch):
    """A later file must never silently shadow an earlier spec id."""
    import leanflow_cli.lean.lean_workflow_specs as specs_mod

    (tmp_path / "workflows").mkdir()
    (tmp_path / "phases").mkdir()
    for where in ("workflows", "phases"):
        (tmp_path / where / "search.md").write_text(
            "---\nid: search\nkind: "
            + ("workflow" if where == "workflows" else "phase")
            + "\ntitle: S\nsummary: s\n---\n\nbody\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(specs_mod, "SPEC_ROOT", tmp_path)
    specs_mod.load_lean_specs.cache_clear()
    try:
        with pytest.raises(ValueError, match="Duplicate Lean spec id 'search'"):
            specs_mod.load_lean_specs()
    finally:
        specs_mod.load_lean_specs.cache_clear()
