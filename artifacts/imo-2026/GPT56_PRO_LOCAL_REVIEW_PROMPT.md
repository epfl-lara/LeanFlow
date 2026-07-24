# GPT-5.6 Pro prompt: pre-proof review of the local IMO 2026 Lean project

Act as an independent senior Lean 4 formalization reviewer. Audit the canonical local IMO 2026 statement project before any proof work begins. This is a source-fidelity, elaboration, and task-design review. Do not attempt the olympiad proofs and do not fill any answer definition.

## Objective

For each local module P1-P6, determine whether it:

1. elaborates in the pinned local environment;
2. represents every qualifier in the authoritative English statement;
3. uses only explicit, defensible representation bridges;
4. has the intended hole structure for its problem type; and
5. avoids hidden weakening, strengthening, vacuity, off-by-one errors, or inconsistent game and termination semantics.

Treat every existing audit, comment, pull request, and blueprint assertion as a hypothesis to independently confirm or falsify.

## Authoritative and local inputs

- Official public page: <https://www.imo-official.org/problems/2026/>
- Byte-preserved official response: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/official-problems-2026-07-16.html`
- Preserved response SHA-256: `198784ca80ae7b27041f295f4a24cd3371fdcef0d26bdf84e1ec5274206482d0`
- Readable derivative: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/original-questions.md`
- Canonical project: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/formalization`
- Project blueprint: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/formalization/Blueprint.md`
- Project README: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/formalization/README.md`
- Baseline repository: <https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3>
- Prior audit to challenge: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/gpt56-pro-review.md`

The preserved official HTML is primary. Correct the Markdown derivative in your report if it disagrees with the rendered official source.

## Intended task modes

- P1, P2, and P6 should contain one concrete theorem statement with one proof `sorry`.
- P3, P4, and P5 are *determine* problems in IMOLean standard mode. Each intentionally contains two work items: an `answer` definition whose body is `sorry`, followed by a theorem whose proof is `sorry`. Review whether each answer's **type** and theorem relation correctly stage the classification task.
- Do not call P3-P5 theorem-only benchmark-ready while `answer` is unknown. Separately judge whether they are well-posed two-stage formalization tasks.
- Expected initial total: nine `sorry` occurrences. No `admit`, explicit `axiom`, or `opaque` declaration is intended.

## Required procedure

1. Verify `lean-toolchain`, `lakefile.toml`, and the resolved mathlib revision. Review this local project, not a moving upstream branch.
2. Inspect the complete official statements, not search snippets. Preserve direct URLs.
3. Read `IMO2026.lean`, `IMO2026/P1.lean` through `P6.lean`, the blueprint, and README completely.
4. Run from the canonical project directory:

   ```sh
   lake build
   for f in IMO2026/P1.lean IMO2026/P2.lean IMO2026/P3.lean IMO2026/P4.lean IMO2026/P5.lean IMO2026/P6.lean; do lake env lean "$f"; done
   rg -n "\\b(sorry|admit|axiom|opaque)\\b" --glob '*.lean'
   ```

   Record exit statuses and every trust-relevant diagnostic. Distinguish “elaborates with `sorry`” from “proved.”
5. Build a source-qualifier matrix for every problem, including object class, quantifier order/dependency, domains/codomains, positivity, distinctness, interiority, leastness, finiteness, equation/inequality orientation, indexing, and every requested conclusion.
6. For P1, independently verify the corrected quotient transition and test whether an all-2 position can still make a no-op move. Check that the infinite stuttering model is equivalent to finite termination plus terminal normal form.
7. For P2, verify angle vertices/orientation, strict interiors, midpoint and circumcentre semantics, and whether extra affine-independence arguments are merely representation witnesses.
8. For P3 and P4, audit strategy, adversary, information, legal-move, termination, payoff, and nonvacuity semantics. State every bridge precisely.
9. For P5, audit subtypes/coercions, both radicals, inequality orientations, and the answer type.
10. For P6, write out the exact zero-based/one-based substitution and check every recurrence and conclusion index.
11. Do not edit any Lean source, blueprint, prior report, or unrelated file. Your only permitted write is the report path below.

## Verdict scale

Assign exactly one statement verdict to each local module:

- `APPROVED`: source-faithful and ready for its declared proof or answer-plus-proof task;
- `APPROVED WITH EXPLICIT BRIDGE`: faithful after stating a precise representation/indexing bridge;
- `NEEDS REVISION`: a concrete source mismatch, semantic flaw, ill-typed task boundary, or undocumented material bridge remains;
- `BLOCKED`: source or environment could not be checked, with exact blocker evidence.

Also assign a separate task-mode label:

- `CONCRETE THEOREM TASK` for a theorem with a fixed proposition and proof hole only;
- `TWO-STAGE DETERMINE TASK` for an answer-definition hole plus a proof hole;
- `NOT READY` if neither task is soundly staged.

Compilation success alone must never imply approval.

## Required report

Write the complete review to:

`/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/gpt56-pro-local-review.md`

The report must contain:

1. an executive summary and explicit go/no-go recommendation for starting proofs;
2. authoritative-source and pinned-environment metadata;
3. exact compile/trust-scan commands and results;
4. one section per problem with verdict, task-mode label, source qualifiers, Lean clauses with file/line locators, bridges, omissions, vacuity/counterexample analysis, and the smallest correction if needed;
5. an exact inventory classifying every `sorry` as an answer hole or proof hole;
6. a prioritized remediation queue, empty only if the project is ready;
7. direct public source/repository/file links; and
8. a final checklist stating whether proof work may safely begin for each problem.

Use precise language. If evidence is inconclusive, say so instead of guessing. Preserve all unrelated dirty-worktree changes.
