# Evaluation Harness

This harness measures regression safety, proof capability, and research-mode
persistence. Frozen benchmark inventories live in `corpus_manifest.json`.
Scoring reads the workflow's `blueprint.json`, `summary.json`, `journal.jsonl`,
decision packets, coach coverage, campaign epochs, and dispatch ledger.
Generated results append to the untracked `evals/results.jsonl`.

## Suites

- **T1 Regression:** the demo projects
  (`testdata/workflow_projects/ProveDemo` IMOMath1–3, RealTheorems;
  `DocFormalizationDemo`) must stay green, and flags-off runs must be
  byte-identical on the hot path. `harness.t1_fixture_projects()` is the
  inventory.
- **T2 Capability:** 40 exact Lean 4 declarations: 20 from the pinned
  Google DeepMind miniF2F test file and 20 from pinned PutnamBench.
- **T3 Research-grade:** ten multi-hour campaigns: the isolated IMOMath3
  scope and nine solved declarations from Formal Conjectures pinned at
  `bench-v1-lean4.27.0`, including `erdos_865.variants.k2`.
- **T4 Lean-IMO-Bench:** 60 IMO-style declarations (30 Basic, 30 Advanced)
  vendored from IMO-LeanProofBench into
  `testdata/workflow_projects/LeanIMOBench`, one standalone module per problem.
  Scored per problem against LEAP's published baseline (42/60 overall; Basic
  25/30, Advanced 17/30). Clean-room only: LEAP's formal proofs of these exact
  theorems are public, so a scored run must keep solution research disabled.
- **Adversarial fixtures:** four local Lean files covering a false leaf,
  false decomposition, vacuity, and nonstandard-axiom temptation.

## Promotion Criteria

- T4 runs are clean-room, one attempt per problem, with no reference
  solutions on disk; otherwise the LEAP comparison is not reportable
- forced-stop/resume drills lose no verified work and reconcile the proof graph
- persistence-coach coverage is 100%, with no strategy or verification authority
- adversarial fixtures cannot pass through false statements or forbidden axioms
- background dispatch loses no completed jobs
- research runs produce complete terminal artifacts
- voluntary-give-up and unresolved-success rates are both zero

## Protocol

Each suite version pins its toolchain and mathlib revision. Compare flags-off
and flags-on runs against the same frozen corpus. `harness.append_result`
writes one JSON object per line with the suite, experiment label, flags,
metrics, and timestamp. Any T1 regression or statistically meaningful T2
solve-rate drop blocks promotion.

`python -m pytest tests/leanflow/test_eval_harness.py` exercises the scorer
itself; live-run scoring is invoked with
`harness.score_terminal_artifacts(<workflow-state root>)` and
`harness.score_campaign_metrics(<workflow-state root>)`. Aggregate a frozen
suite with `harness.aggregate_campaign_metrics(reports)`.
