# IMO 2026 LeanFlow formalization blueprint

## Source and pinned environment

- Primary source: <https://www.imo-official.org/problems/2026/>
- Byte-preserved source: `../official-problems-2026-07-16.html`
- Readable derivative: `../original-questions.md`
- Source retrieval time: `2026-07-16T08:09:07Z`
- Preserved HTML SHA-256: `198784ca80ae7b27041f295f4a24cd3371fdcef0d26bdf84e1ec5274206482d0`
- Baseline: [`jsm28/IMOLean@3fc62b6`](https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3)
- Lean: `leanprover/lean4:v4.32.0-rc1`
- Mathlib: `3b5afa97c31c95c69273cc3724eb50c78399405c`

The saved official HTML is authoritative. The Markdown derivative is for navigation only. The local project preserves the upstream namespaces and declaration structure so that review findings and later proofs map directly back to IMOLean.

## Canonical local modules

| Problem | Module | Local correction or bridge | Open formalization task | Review gate |
| --- | --- | --- | --- | --- |
| P1 | `IMO2026/P1.lean` | Uses the official second replacement `lcm(x,y) / gcd(x,y)`, fixing the false upstream transition relation. | Prove `IMO2026P1.result`. | **Passed:** `APPROVED WITH EXPLICIT BRIDGE`. |
| P2 | `IMO2026/P2.lean` | Documents that affine-independence arguments are well-formedness witnesses and `∠` is undirected. | Prove `IMO2026P2.result`. | **Passed:** `APPROVED WITH EXPLICIT BRIDGE`. |
| P3 | `IMO2026/P3.lean` | Keeps the full mark/claim/payoff model. Interior marks and realised Xiang streams are representation bridges to verify. | Determine `IMO2026P3.answer`, then prove `result`. | **Passed:** `APPROVED WITH EXPLICIT BRIDGE` in two-stage mode. |
| P4 | `IMO2026/P4.lean` | Keeps the finite adversarial triangle game. Radians and realised binary histories are representation bridges to verify. | Determine `IMO2026P4.answer`, then prove `result`. | **Passed:** `APPROVED WITH EXPLICIT BRIDGE` in two-stage mode. |
| P5 | `IMO2026/P5.lean` | Keeps the exact positive-real domain/codomain and both radical inequalities. | Determine `IMO2026P5.answer`, then prove `result`. | **Passed:** `APPROVED` in two-stage mode. |
| P6 | `IMO2026/P6.lean` | Documents the bridge `Lean a n = official a_(n+1)`. | Prove `IMO2026P6.result`. | **Passed:** `APPROVED WITH EXPLICIT BRIDGE`. |

## Hole semantics

This project is a source-translation workspace, not yet a theorem-only benchmark.

- P1, P2, and P6 have a concrete theorem statement and one proof hole each.
- P3, P4, and P5 are *determine* problems. Each intentionally has an answer-definition hole and a theorem-proof hole. Filling the answer definition is part of solving the olympiad problem; it must not be replaced by an unverified guess merely to obtain a theorem-only target.
- Consequently, P3-P5 are valid two-stage formalization tasks but are not concrete theorem-only benchmarks until their `answer` definitions are filled.
- Expected initial inventory: nine `sorry` occurrences and no `admit`, explicit `axiom`, or `opaque` declaration across the six modules.

## Pre-review verification snapshot

Recorded on 2026-07-16 from this directory:

- `lake update`: exit 0; manifest resolves mathlib exactly to `3b5afa97c31c95c69273cc3724eb50c78399405c`.
- `lake build`: exit 0; umbrella library and all six modules build.
- `lake env lean IMO2026/Pn.lean`: exit 0 independently for every `n = 1, ..., 6`.
- Trust scan: exactly nine `sorry` occurrences at the intended answer/proof holes; no `admit`, explicit `axiom`, or `opaque` declaration.
- P1 regression probe: Lean accepts a proof of `¬ ValidMove (fun _ : Fin 2026 => 2) (fun _ : Fin 2026 => 2)`, confirming that the corrected relation rejects the former all-2 no-op.

These checks establish elaboration and hole accounting only. They do not establish source fidelity or prove any IMO result.

## Source-fidelity checklist

### P1

Check the 2026 labeled positions, repeated initial values, positivity, distinct selected positions, unchanged nonselected positions, both gcd/lcm outputs including exact natural-number division, finite termination, exactly one surviving value greater than 1, and choice-independence of that value. In particular, verify that the all-2 board cannot make a no-op transition.

### P2

Check both midpoint identities, all four strict-interior conditions, all three angle equalities with exact vertices, the circumcentre of `AKL`, and `OM = ON`. Decide whether every affine-independence assumption follows from source nondegeneracy/interiority or materially strengthens the problem.

### P3

Check positive `n`, at-most-`n` marks by each player, order and distinctness of marks, cut-piece indexing, Liu-first alternating legal claims, information available to each player, adversarial quantifier order, Liu's total-length payoff, and `IsGreatest`. Verify that restricting marks to `(0,1)` and representing Xiang by a legal realised stream are explicit equivalence bridges, and check the game is nonvacuous.

### P4

Check `0 < θ < π`, arbitrary nondegenerate initial triangles, win-before-cut semantics, strict nonvertex side points, both retained halves, history-dependent Mulan strategy, universal Shan-Yu choices, and finite eventual exact-angle payoff. Verify the degrees/radians and realised-history bridges and rule out vacuity.

### P5

Check positive-real domain and codomain, universal positive `x,y`, both square-root inequalities, their orientation, and the fact that the answer hole ranges over exactly the same function type as the predicate set.

### P6

Check every term is an integer greater than 1, strict growth of each next term, gcd greater than 1 with every earlier term, leastness, positive `T,L`, and the all-index additive-periodicity conclusion. Verify the zero-based/one-based bridge without an off-by-one loss.

## Gate before proofs

1. `lake build` must succeed under the pinned toolchain and mathlib revision.
2. The trust scan must match the intentional nine-hole inventory and find no other escape hatches.
3. An independent GPT-5.6 Pro review must approve each local module or identify a concrete correction.
4. Any correction must be rebuilt and re-reviewed before proof work begins.

**Gate status:** passed on 2026-07-16 for all six modules under their declared task modes. See [`../gpt56-pro-local-review.md`](../gpt56-pro-local-review.md), SHA-256 `40b5b4ac4792a01f35e168db93bcbc8a0e28edde81574ccaf7bec439896aeb7b`.

No theorem proof or answer classification was started during statement preparation or review. P1/P2/P6 may now enter proof work; P3/P4/P5 must determine and re-review their concrete answers before their theorem proofs become fixed targets.
