"""Tests for bounded exact-source negation harness batches."""

from leanflow_cli.workflows import source_negation_batch, source_negation_harness


def _candidate(name: str, *, insert_at: int) -> source_negation_batch.BatchCandidateInput:
    alias = f"leanflowNegationPromotion_{name}"
    harness = source_negation_harness.build_source_negation_harness(
        alias=alias,
        negation_prop="True",
        candidate_name=name,
    )
    assert harness is not None
    return source_negation_batch.BatchCandidateInput(
        proof_declaration=name,
        candidate_name=name,
        alias=alias,
        insert_at=insert_at,
        harness=harness,
    )


def test_batch_harness_preserves_schedule_order_and_maps_generated_lines():
    harness = source_negation_batch.build_batch_harness(
        "lemma first : True := by trivial\nlemma second : True := by trivial\n",
        (_candidate("second", insert_at=2), _candidate("first", insert_at=1)),
    )

    assert [candidate.proof_declaration for candidate in harness.candidates] == [
        "second",
        "first",
    ]
    first_region = harness.candidates[1]
    second_region = harness.candidates[0]
    assert first_region.start_line == 2
    assert second_region.start_line == first_region.end_line + 2
    assert harness.source.index("leanflowNegationPromotion_first") < harness.source.index(
        "lemma second"
    )


def test_batch_harness_omits_commands_after_the_final_candidate():
    harness = source_negation_batch.build_batch_harness(
        "lemma candidate : True := by trivial\n" "theorem unrelated_slow_tail : True := by simp\n",
        (_candidate("candidate", insert_at=1),),
    )

    assert "lemma candidate" in harness.source
    assert "leanflowNegationPromotion_candidate" in harness.source
    assert "unrelated_slow_tail" not in harness.source


def test_batch_classifies_four_local_failures_from_one_exact_check():
    harness = source_negation_batch.build_batch_harness(
        "\n".join(f"lemma c{index} : True := by trivial" for index in range(4)) + "\n",
        tuple(_candidate(f"c{index}", insert_at=index + 1) for index in range(4)),
    )
    output = "\n".join(
        f"/tmp/check.lean:{candidate.tactic_start_line}:3: " "error: application type mismatch"
        for candidate in harness.candidates
    )

    verdicts = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": False,
            "retryable": False,
            "failure_kind": "lean_elaboration",
            "command": ["lake", "env", "lean", "/tmp/check.lean"],
            "output": output,
            "messages": [],
        },
        allowed_axioms=set(),
    )

    assert len(verdicts) == 4
    assert {verdict.disposition for verdict in verdicts} == {source_negation_batch.INCOMPATIBLE}
    assert {verdict.failure_kind for verdict in verdicts} == {
        "source_candidate_kernel_incompatible"
    }


def test_batch_classifies_recovered_failed_theorems_with_sorry_axioms():
    harness = source_negation_batch.build_batch_harness(
        "\n".join(f"lemma c{index} : True := by trivial" for index in range(4)) + "\n",
        tuple(_candidate(f"c{index}", insert_at=index + 1) for index in range(4)),
    )
    output = "\n".join(
        (
            f"/tmp/check.lean:{candidate.tactic_start_line}:3: error: type mismatch\n"
            f"'{candidate.alias}' depends on axioms: [sorryAx]"
        )
        for candidate in harness.candidates
    )

    verdicts = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": False,
            "retryable": False,
            "failure_kind": "lean_elaboration",
            "command": ["lake", "env", "lean", "/tmp/check.lean"],
            "output": output,
            "messages": [],
        },
        allowed_axioms=set(),
    )

    assert len(verdicts) == 4
    assert all(verdict.disposition == source_negation_batch.INCOMPATIBLE for verdict in verdicts)


def test_batch_attributes_parenthesized_lean_error_codes():
    harness = source_negation_batch.build_batch_harness(
        "lemma candidate : True := by trivial\n",
        (_candidate("candidate", insert_at=1),),
    )
    candidate = harness.candidates[0]

    verdict = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": False,
            "retryable": False,
            "failure_kind": "lean_elaboration",
            "command": ["lake", "env", "lean", "/tmp/check.lean"],
            "output": (
                f"/tmp/check.lean:{candidate.tactic_start_line}:3: "
                "error(lean.unknownIdentifier): unknown identifier\n"
                f"'{candidate.alias}' depends on axioms: [sorryAx]"
            ),
            "messages": [],
        },
        allowed_axioms=set(),
    )[0]

    assert verdict.disposition == source_negation_batch.INCOMPATIBLE


def test_batch_treats_proof_error_with_clean_axioms_as_conflicting_evidence():
    harness = source_negation_batch.build_batch_harness(
        "lemma candidate : True := by trivial\n",
        (_candidate("candidate", insert_at=1),),
    )
    candidate = harness.candidates[0]

    verdict = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": False,
            "retryable": False,
            "failure_kind": "lean_elaboration",
            "command": ["lake", "env", "lean", "/tmp/check.lean"],
            "output": (
                f"/tmp/check.lean:{candidate.tactic_start_line}:3: error: type mismatch\n"
                f"'{candidate.alias}' does not depend on any axioms"
            ),
            "messages": [],
        },
        allowed_axioms=set(),
    )[0]

    assert verdict.disposition == source_negation_batch.UNCERTAIN
    assert verdict.failure_kind == "source_batch_candidate_evidence_conflict"


def test_batch_compatible_alias_is_only_non_authoritative_scheduling_evidence():
    harness = source_negation_batch.build_batch_harness(
        "lemma incompatible : True := by trivial\nlemma compatible : True := by trivial\n",
        (
            _candidate("incompatible", insert_at=1),
            _candidate("compatible", insert_at=2),
        ),
    )
    failed, succeeded = harness.candidates
    verdicts = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": False,
            "retryable": False,
            "failure_kind": "lean_elaboration",
            "command": ["lake", "env", "lean", "/tmp/check.lean"],
            "output": (
                f"/tmp/check.lean:{failed.tactic_start_line}:3: error: type mismatch\n"
                f"'{succeeded.alias}' does not depend on any axioms"
            ),
            "messages": [],
        },
        allowed_axioms=set(),
    )

    assert verdicts[0].disposition == source_negation_batch.INCOMPATIBLE
    assert verdicts[1].disposition == source_negation_batch.COMPATIBLE
    assert verdicts[1].axioms == ()


def test_batch_rejects_nonstandard_axioms_without_promoting():
    harness = source_negation_batch.build_batch_harness(
        "lemma candidate : True := by trivial\n",
        (_candidate("candidate", insert_at=1),),
    )
    alias = harness.candidates[0].alias

    verdict = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": True,
            "retryable": False,
            "failure_kind": "",
            "output": f"'{alias}' depends on axioms: [sorryAx]",
            "messages": [],
        },
        allowed_axioms={"Classical.choice"},
    )[0]

    assert verdict.disposition == source_negation_batch.INCOMPATIBLE
    assert verdict.failure_kind == "source_candidate_axioms_unacceptable"


def test_batch_never_caches_unlocated_or_outside_source_errors():
    harness = source_negation_batch.build_batch_harness(
        "lemma candidate : True := by trivial\n",
        (_candidate("candidate", insert_at=1),),
    )
    for output in (
        "error: unlocated project failure",
        "/tmp/check.lean:1:1: error: original source is broken",
    ):
        verdict = source_negation_batch.classify_batch_check(
            harness,
            {
                "success": False,
                "retryable": False,
                "failure_kind": "lean_elaboration",
                "command": ["lake", "env", "lean", "/tmp/check.lean"],
                "output": output,
                "messages": [],
            },
            allowed_axioms=set(),
        )[0]

        assert verdict.disposition == source_negation_batch.UNCERTAIN
        assert verdict.retryable is True


def test_batch_never_attributes_an_imported_file_error_to_an_alias_line():
    harness = source_negation_batch.build_batch_harness(
        "lemma candidate : True := by trivial\n",
        (_candidate("candidate", insert_at=1),),
    )

    verdict = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": False,
            "retryable": False,
            "failure_kind": "lean_elaboration",
            "command": ["lake", "env", "lean", "/tmp/harness.lean"],
            "output": (
                f"/project/Imported.lean:{harness.candidates[0].tactic_start_line}:3: "
                "error: imported declaration failed"
            ),
            "messages": [],
        },
        allowed_axioms=set(),
    )[0]

    assert verdict.disposition == source_negation_batch.UNCERTAIN
    assert verdict.retryable is True


def test_batch_treats_header_and_axiom_command_errors_as_uncertain():
    harness = source_negation_batch.build_batch_harness(
        "lemma candidate : True := by trivial\n",
        (_candidate("candidate", insert_at=1),),
    )
    candidate = harness.candidates[0]
    for line in (candidate.start_line, candidate.axiom_line):
        verdict = source_negation_batch.classify_batch_check(
            harness,
            {
                "success": False,
                "retryable": False,
                "failure_kind": "lean_elaboration",
                "command": ["lake", "env", "lean", "/tmp/check.lean"],
                "output": f"/tmp/check.lean:{line}:3: error: command failed",
                "messages": [],
            },
            allowed_axioms=set(),
        )[0]

        assert verdict.disposition == source_negation_batch.UNCERTAIN
        assert verdict.retryable is True


def test_batch_requires_one_unambiguous_axiom_profile():
    harness = source_negation_batch.build_batch_harness(
        "lemma candidate : True := by trivial\n",
        (_candidate("candidate", insert_at=1),),
    )
    alias = harness.candidates[0].alias

    verdict = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": True,
            "retryable": False,
            "failure_kind": "",
            "output": (
                f"'{alias}' does not depend on any axioms\n"
                f"'{alias}' depends on axioms: [sorryAx]"
            ),
            "messages": [],
        },
        allowed_axioms=set(),
    )[0]

    assert verdict.disposition == source_negation_batch.UNCERTAIN
    assert verdict.retryable is True


def test_batch_timeout_keeps_every_candidate_retryable():
    harness = source_negation_batch.build_batch_harness(
        "lemma first : True := by trivial\nlemma second : True := by trivial\n",
        (_candidate("first", insert_at=1), _candidate("second", insert_at=2)),
    )

    verdicts = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": False,
            "retryable": True,
            "timed_out": True,
            "failure_kind": "infrastructure_timeout",
            "output": "",
            "messages": [],
        },
        allowed_axioms=set(),
    )

    assert len(verdicts) == 2
    assert all(verdict.disposition == source_negation_batch.UNCERTAIN for verdict in verdicts)
    assert all(verdict.retryable for verdict in verdicts)


def test_batch_truncated_output_keeps_every_candidate_retryable():
    harness = source_negation_batch.build_batch_harness(
        "lemma first : True := by trivial\nlemma second : True := by trivial\n",
        (_candidate("first", insert_at=1), _candidate("second", insert_at=2)),
    )

    verdicts = source_negation_batch.classify_batch_check(
        harness,
        {
            "success": False,
            "retryable": False,
            "output_truncated": True,
            "failure_kind": "lean_elaboration",
            "output": "partial diagnostics",
            "messages": [],
        },
        allowed_axioms=set(),
    )

    assert all(verdict.disposition == source_negation_batch.UNCERTAIN for verdict in verdicts)
    assert all(verdict.retryable for verdict in verdicts)
