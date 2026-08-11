---
id: phase-negation
kind: phase
title: Negation Phase
summary: "The feasibility-probe protocol: scratch-only negation attempts, kernel-standard acceptance including axiom guards, and a strict verdict vocabulary."
consumed_by: [orchestrator]
deliverable_schema:
  verdict: "negation_proved | inconclusive — top level of the probe RESULT"
  plausible:
    counterexample_text: "small counterexample or empirical evidence, if any"
  negation:
    verdict: "negation_proved | inconclusive (the field the router reads)"
    axioms_ok: "bool — the refutation clears the allowed-axiom set"
---

# Negation Phase

Before burning prover budget on a resisting statement, probe whether it
is FALSE. A kernel-verified refutation is a concrete research result of
equal rank to a proof.

## Protocol

1. Scratch only: probes run through LeanProbe against scratch stubs —
   never edits to project files.
2. Try small counterexamples first (`decide`-able instances, boundary
   values), then a direct `¬P` proof attempt.
3. Acceptance is kernel-standard: the SAME axiom guards as a proof — a
   refutation via `native_decide` or a custom axiom does not count
   (`axioms_ok: false`).
4. `negation.verdict: negation_proved` requires the kernel to have
   accepted the `¬P` proof with `negation.axioms_ok: true` — the router
   reads exactly those two fields. Anything weaker is `inconclusive` —
   say so.

The deliverable schema is the probe RESULT shape; the persisted
`summary.json.negation_probes` entries additionally carry `theorem`,
`file`, and `key` (the exact storage key the router matches against the
current assignment).

## Consequences (the router's business, not yours)

- A proved negation of a SUB-lemma invalidates its decomposition subtree
  and triggers `re-state`.
- A proved negation of the MAIN goal escalates the scope toward a
  `disproved` resolution.
- The probe itself only reports; promotion to graph `false` happens
  through the negation-promotion path, never directly.
