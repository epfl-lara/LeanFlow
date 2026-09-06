# Proposed next research campaign: Spencer without the logarithmic loss

Status: approved by the user on 2026-09-06. Input and effective configuration
verified; launch is being recorded in the project
`UNIVERSAL_BOUND_RUN_CONFIG.json`. Reviewed with Claude Fable 5.1 at maximum
effort on 2026-09-06.

## Target

For every family of n subsets of an n-element set, find a sign colouring whose
sum on each subset has absolute value at most K sqrt(n), for one universal
positive real K chosen independently of n and the family.

```lean
∃ K : ℝ, 0 < K ∧ ∀ (n : ℕ) (S : Fin n → Finset (Fin n)),
  ∃ χ : Fin n → ℝ, (∀ j, χ j = 1 ∨ χ j = -1) ∧
    ∀ i, |∑ j ∈ S i, χ j| ≤ K * Real.sqrt n
```

This is a new variant to prepare as its own protected declaration, with a fresh
run and budget. It removes the logarithmic factor from the completed local
random-colouring bound K sqrt(n log(n + 2)). It allows a loose constant rather
than prescribing 6, avoiding the sharp-constant estimates while retaining the
main partial-colouring argument. It is known mathematics; the challenge is its
Lean formalization.

The completed local result has five independently verified nodes, no remaining
sorry, a successful final Lake build, 135 model calls, and 1h32m31s elapsed. Its
four helper modules remain available as imports in the same SpencerResearch
project. The exponential-pair inequality, cube factorization and cube moment
bound are likely reusable; the old threshold lemma may be less directly useful.

## Source status and literature

At upstream commit `8323e878b83fcd7f4a448256069352a265460d75`, both the exact-six
and random-colouring statements in [Formal Conjectures' Spencer file](https://github.com/google-deepmind/formal-conjectures/blob/8323e878b83fcd7f4a448256069352a265460d75/FormalConjectures/Wikipedia/SixStandardDeviations.lean)
retain `sorry`. Its `research solved` annotation means mathematically known,
not that a Lean proof is present. The K sqrt(n) variant above is not itself an
existing upstream declaration. Bounded searches did not establish whether an
independent complete formalization exists elsewhere; no global novelty claim is
made.

Primary references to inspect during planning:

- [Spencer, Six standard deviations suffice (1985)](https://doi.org/10.1090/S0002-9947-1985-0784009-0), the original result.
- [Bansal, Constructive Algorithms for Discrepancy Minimization (2010)](https://arxiv.org/abs/1002.2259), an algorithmic treatment using the entropy method.
- [Cai et al., Revisit the Partial Coloring Method: Prefix Spencer and Sampling (2024)](https://arxiv.org/abs/2408.13756), a modern comparison of partial-colouring methods.

These are resource leads. The controller must select and read a concrete proof,
record its actual inequalities and hypotheses, then submit the planned interfaces
for a fresh review. Merely citing these papers is not an informal proof.

## Expected obligations to evaluate

1. A finite sign-cube tail-count bound, reusing the verified moment estimate.
2. A finite discretization of each row sum and an explicit entropy bound.
3. Subadditivity and a large-fibre argument for the joint discretization.
4. A Hamming-ball counting bound to find two sufficiently separated colourings.
5. A reusable partial-colouring lemma: freeze a fixed fraction of coordinates
   with bounded discrepancy. Quantify the remaining coordinate count and row
   count separately so repeated application is valid.
6. Iterate on uncoloured coordinates, prove termination and retain the sign
   condition for frozen coordinates.
7. Bound the sum of stage errors by a convergent geometric/logarithmic series.
8. Choose one universal K and close n = 0 and small n explicitly.

Do not solve the final goal by importing a theorem equivalent to it. Generated
`def`/`abbrev` helper nodes are supported by the current workflow; they remain
separate obligations with the same source and verification rules. The setting
`fill_definitions = false` protects user-supplied incomplete definitions, not the
ability to introduce helper definitions during a reviewed decomposition.

## Approved configuration

| Setting | Approved value |
| --- | --- |
| Mode and order | Research, bottom-up |
| Provider and models | openai-codex; gpt-6-astra for orchestrator, review and provers |
| Shared reasoning effort | xhigh |
| Parallel provers | 4 |
| Calls per prover / negation pass | 200, retained across local decomposition |
| Calls per planning / review / resource stage | 50; finish earlier when ready |
| Total campaign calls | 4,000, including all jobs and restarts |
| Campaign active wall time | 16 hours |
| Request / Lean-check cap | 1,200 seconds, capped by remaining campaign time |
| Extra passes per theorem | 2 |
| Wrong-direction plan refinements | 6 |
| Decomposition / node ceilings | 16 / 40 |
| Context and compression | 64k prover, 128k orchestrator; compression enabled |
| Axioms | propext, Classical.choice, Quot.sound only |
| Local Loogle | Use the repaired managed index; verify campaign-side availability before launch |

The increased caps apply to the new universal-bound campaign; the completed
random-colouring campaign retains its original limits and accounting.
The main extra cost is the finite entropy/partial-colouring infrastructure. If it
is too large for one campaign, preserve the partial-colouring lemma as a useful
standalone result rather than silently weakening the final target. The exact-six
statement remains a later target.

Alternative if a different area is preferred: the explicit-construction lower
bound R(5,5) ≥ 43 from the upstream Ramsey-number collection. It trades analytic
infrastructure for a substantial finite certificate and verification task; see
[the primary Ramsey-number construction paper](https://arxiv.org/abs/2212.12630).

The new frozen input is `Spencer/UniversalBound.lean`. LeanProbe elaborated its
statement and imports without errors, with its one intended `sorry`. The resolved
launch preview confirms all values above. A real LeanFlow research-mode Loogle
query returned `Real.exp_add` with no degraded search providers. The VS Code
catalog now accepts the runtime default `xhigh`; all 42 catalog tests passed.
