"""Exercise correction contracts without treating synthetic inspector rows as kernel acceptance."""

from __future__ import annotations

import copy

import pytest

from leanflow_cli.formalization import corrections
from leanflow_cli.formalization.actions import apply_action
from leanflow_cli.formalization.clause_obligations import obligation_registry
from leanflow_cli.formalization.lean_policy import evaluate_policy
from leanflow_cli.formalization.records import create_document
from tests.leanflow.test_formalization_lean_policy import _row

_EXPLANATION = (
    "The exact frozen equal-weight distribution context supports this local arithmetic repair."
)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    """Freeze a toy discrepancy and supply explicit synthetic compiled-policy fixtures."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    paper = tmp_path / "paper.tex"
    paper.write_text(
        r"\begin{definition}Two equally weighted outcomes form a probability distribution; "
        r"each has probability one third.\end{definition}"
        "\n"
        r"\begin{theorem}The resulting construction satisfies its stated bound.\end{theorem}"
    )
    record = create_document(tmp_path, paper)
    item = record["inventory"]["items"][0]
    refs = copy.deepcopy(item["statement_spans"])
    apply_action(
        record,
        {
            "type": "record_obligation",
            "item_id": item["id"],
            "source_refs": refs,
            "description": "The printed equal weights sum to one.",
            "reason": _EXPLANATION,
        },
    )
    clause = next(iter(obligation_registry(record)))
    refs = copy.deepcopy(obligation_registry(record)[clause]["source_refs"])
    action = {
        "type": "record_correction_authority",
        "authority_id": "local-source-repairs",
        "version": 1,
        "kind": "bounded_policy",
        "source_bundle": record["source_bundle"],
        "source_item_ids": [item["id"]],
        "clause_ids": [clause],
        "statement": "Permit uniquely source-supported local mathematical repairs backed by checked facts and fresh review.",
        "enabled": True,
    }
    authority = corrections.record_correction_authority(record, action)
    proposal = {
        "id": "equal-weight-normalization",
        "kind": "source_supported_mathematical",
        "source_item_id": item["id"],
        "clause_ids": [clause],
        "original": {
            "source_refs": refs,
            "literal_reading": "Two equal weights of one third sum to one.",
        },
        "adopted": {
            "reading": "The two equal normalized weights are one half each.",
            "declaration_names": ["Paper.adopted"],
        },
        "rationale": _EXPLANATION,
        "source_context_refs": refs,
        "authority_refs": [authority["id"]],
        "evidence": [
            {
                "goal_name": "Paper.localGoal",
                "proof_name": "Paper.localFact",
                "purpose": _EXPLANATION,
            }
        ],
    }
    record.update(
        environment_id="fixture-environment",
        policy_id="fixture-policy",
        units=[
            {
                "id": "unit",
                "required_source_ids": [item["id"]],
                "declarations": [{"name": "Paper.adopted"}],
                "clause_dispositions": [
                    {
                        "obligation_id": clause,
                        "disposition": "represented_via_correction",
                        "correction_id": proposal["id"],
                        "declaration_names": ["Paper.adopted"],
                        "rationale": _EXPLANATION,
                    }
                ],
            }
        ],
    )
    goal_shape = {
        "type_constant_name": None,
        "type_head_forall": False,
        "proposition_body_head_forall": False,
        "proposition_body_is_prop": True,
    }
    proof_shape = {
        "type_constant_name": "Paper.localGoal",
        "type_head_forall": False,
        "proposition_body_head_forall": None,
        "proposition_body_is_prop": None,
    }
    common = {
        "pending": False,
        "direct_sorry": False,
        "axioms": [],
        "body": "synthetic complete arithmetic body",
    }
    checks = evaluate_policy(
        [
            _row("Paper.adopted"),
            _row(
                "Paper.localGoal",
                **common,
                kind="definition",
                is_prop=False,
                type="Expr.sort 0",
                direct_dependencies=[],
                correction_shape=goal_shape,
            ),
            _row(
                "Paper.localFact",
                **common,
                type="Expr.const Paper.localGoal []",
                direct_dependencies=["Paper.localGoal"],
                correction_shape=proof_shape,
            ),
        ],
        (),
    )
    checks["kernel_replay"] = {
        "scope": "referenced-dependency-closure",
        "axiom_hygiene_scope": "noncompiler-import-closure",
    }
    record["checks"] = checks
    return record, proposal, action


def _bind(inputs):
    record, proposal, _ = inputs
    corrections.apply_corrections(record, [proposal])
    corrections.bind_correction_evidence(record, record["checks"])
    return record


def _review(record):
    dossier = {
        "revision_id": record["revision_id"],
        "corrections": record["corrections"],
        "correction_evidence": record["checks"]["correction_evidence"],
    }
    row = record["corrections"][0]
    value = {
        "revision_id": record["revision_id"],
        "accepted": True,
        "cleanup_verified": True,
        "status": "completed",
        "correction_verdicts": [
            {
                "correction_id": row["id"],
                "content_id": row["content_id"],
                "evidence_id": record["checks"]["correction_evidence"][0]["evidence_id"],
                "authority_refs": row["authority_refs"],
                "decision": "PASS",
                "rationale": _EXPLANATION,
                "source_support": _EXPLANATION,
                "competing_alternatives": "Unequal weights contradict the explicit equal-weight specification in the frozen source context.",
                "issues": [],
            }
        ],
    }
    return dossier, value


def test_proposal_preserves_original_denominator_and_grants_no_authority(inputs):
    record, proposal, _ = inputs
    inventory, files = copy.deepcopy(record["inventory"]), copy.deepcopy(record["source_files"])
    rows = corrections.apply_corrections(record, [proposal])
    assert rows[0]["actor"] == "author_proposal" and rows[0]["status"] == "proposed"
    assert record["inventory"] == inventory and record["source_files"] == files
    corrections.bind_correction_evidence(record, record["checks"])
    assert "correction_evidence" not in record
    with pytest.raises(ValueError, match="independent statement review"):
        corrections.assert_corrections_accepted(record, [])
    dossier, value = _review(record)
    assert corrections.validate_correction_verdicts(dossier, value) == []
    corrections.assert_corrections_accepted(record, [value])
    assert record["checks"]["declarations"][0]["pending"] is True


@pytest.mark.parametrize("field", ["accepted", "authority", "verified", "content_id"])
def test_author_cannot_supply_correction_approval(inputs, field):
    record, proposal, _ = inputs
    with pytest.raises(ValueError, match="authored correction"):
        corrections.apply_corrections(record, [{**proposal, field: True}])


@pytest.mark.parametrize(
    "damage",
    [
        "bundle",
        "item",
        "clause",
        "span",
        "foreign_span",
        "context",
        "authority",
        "duplicate",
        "no_fact",
    ],
)
def test_proposal_requires_exact_frozen_scope_and_applicable_authority(inputs, damage):
    record, proposal, _ = inputs
    row = copy.deepcopy(proposal)
    if damage == "bundle":
        record["source_bundle"] = "0" * 64
    elif damage == "item":
        row["source_item_id"] = record["inventory"]["items"][1]["id"]
    elif damage == "clause":
        row["clause_ids"] = ["unknown-clause"]
    elif damage == "span":
        row["original"]["source_refs"][0]["file_hash"] = "sha256:" + "0" * 64
    elif damage == "foreign_span":
        other = record["inventory"]["items"][1]["statement_spans"][0]
        row["original"]["source_refs"][0].update(
            byte_start=other["byte_start"], byte_end=other["byte_end"]
        )
    elif damage == "context":
        row["source_context_refs"] = []
    elif damage == "authority":
        row["authority_refs"] = ["invented-authority"]
    elif damage == "no_fact":
        row["evidence"] = []
    with pytest.raises(ValueError):
        corrections.apply_corrections(record, [row, row] if damage == "duplicate" else [row])


def test_revoked_or_replaced_authority_cannot_be_reused(inputs):
    record, proposal, action = inputs
    corrections.apply_corrections(record, [proposal])
    previous = corrections.correction_context_identity(record)
    corrections.record_correction_authority(record, {**action, "version": 2, "enabled": False})
    assert corrections.correction_context_identity(record) != previous
    with pytest.raises(ValueError, match="current applicable"):
        corrections.apply_corrections(record, [proposal])
    with pytest.raises(ValueError, match="trusted user decision"):
        corrections.record_correction_authority(record, {**action, "version": 4})


def test_user_interpretation_requires_its_own_versioned_decision(inputs):
    record, proposal, action = inputs
    proposal["kind"] = "user_interpretation"
    with pytest.raises(ValueError, match="not enabled"):
        corrections.apply_corrections(record, [proposal])
    authority = corrections.record_correction_authority(
        record, {**action, "authority_id": "chosen-model", "kind": "interpretation"}
    )
    proposal["authority_refs"] = [authority["id"]]
    corrections.apply_corrections(record, [proposal])


def test_overlapping_corrections_must_be_merged(inputs):
    record, proposal, _ = inputs
    with pytest.raises(ValueError, match="Overlapping corrections"):
        corrections.apply_corrections(record, [proposal, {**proposal, "id": "another-reading"}])


@pytest.mark.parametrize(
    "damage", ["literal", "wrong_id", "wrong_names", "missing_clause", "second_unit"]
)
def test_correction_metadata_cannot_bypass_actual_unit_clause_dispositions(inputs, damage):
    record, proposal, _ = inputs
    corrections.apply_corrections(record, [proposal])
    unit = record["units"][0]
    row = unit["clause_dispositions"][0]
    if damage == "literal":
        row["disposition"] = "represented"
    elif damage == "wrong_id":
        row["correction_id"] = "another-correction"
    elif damage == "wrong_names":
        row["declaration_names"] = ["Paper.localFact"]
    elif damage == "missing_clause":
        unit["clause_dispositions"] = []
    else:
        other = copy.deepcopy(unit)
        other.update(id="another-unit", clause_dispositions=[])
        record["units"].append(other)
    with pytest.raises(ValueError, match="Every corrected clause"):
        corrections.bind_correction_evidence(record, record["checks"])


def test_encoding_repair_does_not_demand_equivalence_to_wrong_lean_encoding(inputs):
    record, proposal, _ = inputs
    proposal.update(kind="encoding_repair", evidence=[])
    record = _bind(inputs)
    assert record["checks"]["correction_evidence"][0]["evidence"] == []
    with pytest.raises(ValueError, match="independent statement review"):
        corrections.assert_corrections_accepted(record, [])


@pytest.mark.parametrize("kind", ["encoding_repair", "equivalent_reformulation"])
def test_source_preserving_repairs_need_no_new_user_permission(inputs, kind):
    record, proposal, _ = inputs
    record["correction_authorities"] = []
    proposal.update(kind=kind, authority_refs=[])
    if kind == "encoding_repair":
        proposal["evidence"] = []
    record = _bind(inputs)
    dossier, value = _review(record)
    assert corrections.validate_correction_verdicts(dossier, value) == []
    corrections.assert_corrections_accepted(record, [value])


@pytest.mark.parametrize("kind", ["source_supported_mathematical", "user_interpretation"])
def test_substantive_repair_acceptance_still_requires_scoped_authority(inputs, kind):
    record, proposal, _ = inputs
    proposal.update(kind=kind, authority_refs=[])
    with pytest.raises(ValueError, match="authority references"):
        corrections.apply_corrections(record, [proposal])


def test_no_corrections_need_no_compiler_or_environment_fields():
    checks = {}
    assert corrections.bind_correction_evidence({"source_bundle": "a" * 64}, checks) == []
    assert checks == {"correction_evidence": []}


def test_pdf_text_window_cannot_escape_item_on_the_same_physical_page():
    span = {
        "file": "paper.pdf",
        "coordinate_space": "pdf_page",
        "page_index": 1,
        "byte_start": 0,
        "byte_end": 200,
        "text_start": 10,
        "text_end": 50,
    }
    assert corrections._contains(span, {**span, "text_start": 20, "text_end": 40})
    assert not corrections._contains(span, {**span, "text_start": 51, "text_end": 60})


@pytest.mark.parametrize(
    "damage",
    [
        "expected_goal",
        "hypothesis",
        "hidden_forall",
        "goal_not_prop",
        "pending",
        "sorry",
        "unsafe",
        "native",
        "library_sorry",
        "cycle",
        "adopted_dependency",
        "missing_replay",
        "unmapped",
    ],
)
def test_strict_closed_evidence_rejects_dishonest_or_incomplete_warrants(inputs, damage):
    record, proposal, _ = inputs
    corrections.apply_corrections(record, [proposal])
    checks = record["checks"]
    goal, proof = checks["declarations"][1:]
    if damage == "expected_goal":
        proof["correction_shape"]["type_constant_name"] = "True"
    elif damage == "hypothesis":
        proof["correction_shape"]["type_head_forall"] = True
    elif damage == "hidden_forall":
        goal["correction_shape"]["proposition_body_head_forall"] = True
    elif damage == "goal_not_prop":
        goal["correction_shape"]["proposition_body_is_prop"] = False
    elif damage in {"pending", "unsafe"}:
        proof[damage] = True
    elif damage in {"sorry", "native"}:
        proof["axioms"] = ["sorryAx" if damage == "sorry" else "Lean.ofReduceBool"]
        checks["allowed_axioms"] = proof["axioms"]
    elif damage == "library_sorry":
        proof["direct_dependencies"].append("Library.bad")
        checks["library_declarations"] = [
            {"name": "Library.bad", "axioms": ["sorryAx"], "direct_dependencies": []}
        ]
    elif damage == "cycle":
        goal["direct_dependencies"] = [proof["name"]]
    elif damage == "adopted_dependency":
        proof["direct_dependencies"].append("Paper.adopted")
    elif damage == "missing_replay":
        checks.pop("kernel_replay")
    else:
        record["units"] = []
    with pytest.raises(ValueError):
        corrections.bind_correction_evidence(record, checks)
    assert "correction_evidence" not in checks


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "invented",
        "content",
        "evidence",
        "authority",
        "revision",
        "thin_rationale",
        "thin_source",
        "thin_alternatives",
        "unresolved",
    ],
)
def test_same_review_requires_exact_correction_judgments(inputs, damage):
    record = _bind(inputs)
    dossier, value = _review(record)
    row = value["correction_verdicts"][0]
    if damage == "missing":
        value.pop("correction_verdicts")
    elif damage == "duplicate":
        value["correction_verdicts"] *= 2
    elif damage in {"invented", "content", "evidence", "authority", "revision"}:
        if damage == "revision":
            value["revision_id"] = "other-revision"
        else:
            field = {
                "invented": "correction_id",
                "content": "content_id",
                "evidence": "evidence_id",
                "authority": "authority_refs",
            }[damage]
            row[field] = ["other"] if damage == "authority" else "other"
    elif damage.startswith("thin_"):
        field = {
            "thin_rationale": "rationale",
            "thin_source": "source_support",
            "thin_alternatives": "competing_alternatives",
        }[damage]
        row[field] = "yes"
    else:
        row["issues"] = ["An additional unapproved independence assumption remains necessary."]
    with pytest.raises(ValueError):
        corrections.validate_correction_verdicts(dossier, value)


@pytest.mark.parametrize(
    "damage",
    [
        "definition",
        "reading",
        "authority",
        "environment",
        "policy",
        "verdict",
        "cleanup",
        "revision",
        "none",
    ],
)
def test_changed_inputs_or_missing_fresh_review_invalidate_correction_acceptance(inputs, damage):
    record = _bind(inputs)
    _, value = _review(record)
    if damage == "definition":
        record["checks"]["declarations"][1]["body_hash"] = "changed-body"
    elif damage == "reading":
        record["corrections"][0]["adopted"]["reading"] += " with an extra hypothesis"
    elif damage == "authority":
        corrections.record_correction_authority(
            record, {**inputs[2], "version": 2, "enabled": False}
        )
    elif damage == "environment":
        record["environment_id"] = "changed-environment"
    elif damage == "policy":
        record["policy_id"] = "changed-policy"
    elif damage == "verdict":
        value["correction_verdicts"][0]["decision"] = "BLOCK"
    elif damage == "cleanup":
        value["cleanup_verified"] = False
    elif damage == "revision":
        value["revision_id"] = "previous-revision"
    else:
        value["correction_verdicts"] = []
    with pytest.raises(ValueError):
        corrections.assert_corrections_accepted(record, [value])


def test_true_local_fact_alone_cannot_approve_a_source_correction(inputs):
    record, _, _ = inputs
    record["checks"]["declarations"][1]["body"] = "Expr.const True []"
    record = _bind(inputs)
    with pytest.raises(ValueError, match="independent statement review"):
        corrections.assert_corrections_accepted(record, [])
    dossier, value = _review(record)
    value["correction_verdicts"][0].update(
        decision="BLOCK", issues=["True provides no evidence for the claimed normalization repair."]
    )
    assert corrections.validate_correction_verdicts(dossier, value) == [
        record["corrections"][0]["id"]
    ]
    with pytest.raises(ValueError, match="requires changes"):
        corrections.assert_corrections_accepted(record, [value])


def test_mutually_supporting_corrections_cannot_justify_each_other(inputs):
    record, proposal, action = inputs
    item = record["inventory"]["items"][0]
    apply_action(
        record,
        {
            "type": "record_obligation",
            "item_id": item["id"],
            "source_refs": proposal["original"]["source_refs"],
            "description": "The resulting normalized object has exactly two equal outcomes.",
            "reason": _EXPLANATION,
        },
    )
    clauses = list(obligation_registry(record))
    authority = corrections.record_correction_authority(
        record, {**action, "version": 2, "clause_ids": clauses}
    )
    proposal["authority_refs"] = [authority["id"]]
    other = copy.deepcopy(proposal)
    other.update(id="another-correction", clause_ids=[clauses[1]])
    other["adopted"]["declaration_names"] = ["Paper.otherAdopted"]
    other["evidence"][0].update(goal_name="Paper.otherGoal", proof_name="Paper.otherFact")
    unit = record["units"][0]
    unit["declarations"].append({"name": "Paper.otherAdopted"})
    unit["clause_dispositions"].append(
        {
            "obligation_id": clauses[1],
            "disposition": "represented_via_correction",
            "correction_id": other["id"],
            "declaration_names": ["Paper.otherAdopted"],
            "rationale": _EXPLANATION,
        }
    )
    corrections.apply_corrections(record, [proposal, other])
    adopted, goal, proof = copy.deepcopy(record["checks"]["declarations"])
    adopted.update(
        kind="definition",
        pending=False,
        direct_sorry=False,
        axioms=[],
        is_prop=False,
        body="complete local data",
        direct_dependencies=[],
    )
    second_adopted = {**adopted, "name": "Paper.otherAdopted"}
    second_goal = {**goal, "name": "Paper.otherGoal"}
    second_proof = {
        **proof,
        "name": "Paper.otherFact",
        "type": "Expr.const Paper.otherGoal []",
        "correction_shape": {**proof["correction_shape"], "type_constant_name": "Paper.otherGoal"},
        "direct_dependencies": ["Paper.otherGoal", "Paper.adopted"],
    }
    proof["direct_dependencies"].append("Paper.otherAdopted")
    replay = record["checks"]["kernel_replay"]
    checks = evaluate_policy([adopted, goal, proof, second_adopted, second_goal, second_proof], ())
    checks["kernel_replay"] = replay
    with pytest.raises(ValueError, match="circular justification"):
        corrections.bind_correction_evidence(record, checks)
    assert "correction_evidence" not in checks
