# GPT-5.6 Pro prompt: independent review of IMO 2026 Lean translations

Act as an independent senior Lean 4 formalization reviewer. Audit the six IMO 2026 translations in `jsm28/IMOLean` for source fidelity and benchmark readiness. This is a review task, not a proof attempt.

## Objective

For each of Problems 1–6, determine whether the pinned Lean declaration:

1. elaborates in its declared environment;
2. faithfully represents every qualifier in the authoritative English problem;
3. makes only explicit, defensible representation changes;
4. states a concrete theorem that a proving agent can actually attempt; and
5. avoids hidden weakening, strengthening, vacuity, or inconsistent game/termination semantics.

Do not trust the existing local audit. Treat every prior finding, including the suspected P1 defect, as a hypothesis to independently confirm or falsify.

## Authoritative and comparison inputs

- Authoritative public page: <https://www.imo-official.org/problems/2026/>
- Byte-preserved official response: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/official-problems-2026-07-16.html`
- Preserved response SHA-256: `198784ca80ae7b27041f295f4a24cd3371fdcef0d26bdf84e1ec5274206482d0`
- Readable derivative transcription: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/original-questions.md`
- Existing audit, to challenge rather than assume: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/translation-audit-2026-07-16.md`
- Existing blueprint: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/formalization/Blueprint.md`
- Local proposed P1 replacement and other anchors: `/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/formalization/IMO2026.lean`
- Pinned upstream repository: <https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3>
- Existing local checkout of that exact commit: `/Users/lmilikic/Documents/Codex/2026-07-15/hey-can-you-add-a-new/work/IMOLean`

The official HTML is primary. The Markdown transcription is only a convenience and must be corrected in your report if it disagrees with the rendered official source.

## Required procedure

1. Verify the upstream checkout is exactly commit `3fc62b66ec02aa8446f3a1461f540ed93a74caa3`. Do not review a moving branch.
2. Inspect the complete official problem statements, not search-result snippets. Preserve direct URLs in the report.
3. Read every upstream file `IMO/IMO2026P1.lean` through `IMO/IMO2026P6.lean` completely.
4. Record the pinned Lean toolchain and mathlib revision.
5. Run the narrow compilation check for every file, recording exit status and all warnings relevant to trust (`sorry`, `admit`, extra axioms, or failures).
6. Perform a source-qualifier matrix for every problem. At minimum check:
   - mathematical object class;
   - quantifier order and dependency;
   - parameter domains and codomains;
   - positivity, distinctness, interior, leastness, and finiteness conditions;
   - equality/inequality orientation;
   - indexing conventions and any bridge needed;
   - termination, strategy, adversary, information, legal-move, and payoff semantics where applicable;
   - every requested follow-on conclusion; and
   - whether a `determine` problem has an explicit answer rather than an unconstrained placeholder.
7. Look for vacuity and counterexamples introduced by the encoding. For any claimed mismatch, give a concrete witness or a precise logical explanation.
8. Separately review the local proposed P1 replacement in `formalization/IMO2026.lean`: say whether it fixes the upstream issue and whether it is equivalent to, stronger than, or weaker than the official statement.
9. Do not edit the upstream checkout or any existing LeanFlow artifact. Your only permitted write is the final report path below.

## Verdict scale

Assign exactly one verdict to each upstream problem:

- `APPROVED`: source-faithful and concrete enough for a proving benchmark;
- `APPROVED WITH EXPLICIT BRIDGE`: faithful modulo a representation/indexing bridge that you state precisely;
- `PARTIAL`: useful encoding, but a requested answer, qualifier, or bridge is absent;
- `INCORRECT`: materially different, false for an encoding-specific reason, or vacuous;
- `BLOCKED`: source or environment could not be checked, with exact blocker evidence.

Compilation success alone must never imply `APPROVED`.

## Required report

Write the complete review to:

`/Users/lmilikic/Desktop/LeanFlow/artifacts/imo-2026/gpt56-pro-review.md`

The report must contain:

1. an executive summary and overall recommendation;
2. authoritative source and pinned-environment metadata;
3. exact compile commands and results;
4. one section per problem with:
   - verdict and severity;
   - source qualifiers;
   - corresponding Lean clauses with file/line locators;
   - omissions or scope changes;
   - concrete counterexample/logical failure when relevant;
   - benchmark-readiness judgment; and
   - the smallest recommended correction;
5. a separate assessment of the local corrected P1 target;
6. a prioritized remediation queue for LeanFlow; and
7. direct public source/repository/file links.

Use precise language: distinguish “elaborates with `sorry`” from “proved”, “question mechanics encoded” from “answer encoded”, and “initially plausible” from “source-verified”. If evidence is inconclusive, say so rather than guessing.

You are not alone in this workspace. Preserve all unrelated dirty-worktree changes and do not modify, revert, stage, or commit them.
