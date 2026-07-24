# Phase E — Evaluation Harness

The /prove redesign's evaluation harness (roadmap §4.13; audit Part B §5).
It gates promotion of the relentless-prover behavior. The frozen inventories
live in `corpus_manifest.json`; scoring reads `blueprint.json`, `summary.json`,
`journal.jsonl`, decision packets, coach coverage, campaign epochs, and the
dispatch ledger. Results append to `results.jsonl`.

## Suites

- **T1 Regression (every phase):** the demo projects
  (`testdata/workflow_projects/ProveDemo` IMOMath1–3, RealTheorems;
  `DocFormalizationDemo`) must stay green, and flags-off runs must be
  byte-identical on the hot path (extends Phase 0's shadow-compare into a
  permanent gate). `harness.t1_fixture_projects()` is the inventory.
- **T2 Capability:** 40 exact Lean 4 declarations: 20 from the pinned
  Google DeepMind miniF2F test file and 20 from pinned PutnamBench.
- **T3 Research-grade:** ten multi-hour campaigns: the isolated IMOMath3
  scope and nine solved declarations from Formal Conjectures pinned at
  `bench-v1-lean4.27.0`, including `erdos_865.variants.k2`.
- **Adversarial fixtures:** four local Lean files covering a false leaf,
  false decomposition, vacuity, and nonstandard-axiom temptation.

## Per-phase gates

| Phase | Gate |
|---|---|
| P1 | kill -9 at a random point, resume, zero verified-work loss, graph reconciles (10/10 drills — `harness.reconcile_drill`) |
| P2 | coach coverage 100%, zero strategy/verdict authority, surrendering model output rejected |
| P3 | adversarial fixtures + ledger: zero lost jobs across 100 dispatches |
| P4 | T2 uplift vs the Phase-2 baseline + fixtures (b)(c)(d) |
| P5 | T3 first runs: 100% terminal-artifact compliance |
| P6 | T3 with research mode on: give-up-termination rate 0 and unresolved-success exit rate 0 |

## Protocol

Frozen suites, pinned toolchain/mathlib per suite version. Every
phase-enable PR runs T1 + its gate tier with flags off AND on; results
append to `evals/results.jsonl` via `harness.append_result` (one JSON object
per line: suite, phase, flags, metrics, timestamp). Any T1 regression or a
T2 solve-rate drop >1σ blocks the flag default-flip.

`python -m pytest tests/leanflow/test_eval_harness.py` exercises the scorer
itself; live-run scoring is invoked with
`harness.score_terminal_artifacts(<workflow-state root>)` and
`harness.score_campaign_metrics(<workflow-state root>)`. Aggregate a frozen
suite with `harness.aggregate_campaign_metrics(reports)`. Promotion targets
are:

- voluntary-give-up termination rate: `0`
- unresolved-success exit rate: `0`
- coach coverage: `100%`
- reported route/proof-shape diversity, job launched/consumed/replaced counts,
  verified graph progress, and epoch rollover counts
