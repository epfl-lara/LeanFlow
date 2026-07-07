# Phase E — Evaluation Harness

The /prove redesign's evaluation harness (roadmap §4.13; audit Part B §5).
It gates every later enable-flag: without it, "improves hard-problem
capability" is unfalsifiable. It is deliberately small — a scorer over the
artifacts the redesign already produces (`blueprint.json`, `summary.json`,
`journal.jsonl`, decision packets) plus fixture inventories and a results
log; cross-checking `theorem_outcomes` against the graph joins with the
Phase 2 gate.

## Suites

- **T1 Regression (every phase):** the demo projects
  (`testdata/workflow_projects/ProveDemo` IMOMath1–3, RealTheorems;
  `DocFormalizationDemo`) must stay green, and flags-off runs must be
  byte-identical on the hot path (extends Phase 0's shadow-compare into a
  permanent gate). `harness.t1_fixture_projects()` is the inventory.
- **T2 Capability (Phases 2/4/5/6):** frozen ~40-problem set — miniF2F-hard
  slice + PutnamBench slice + a decomposition-required set. *Problem-set
  curation is an open task; the scorer below is ready for its artifacts.*
- **T3 Research-grade (Phases 4–6):** ~10 multi-hour tasks (re-derive recent
  mathlib results against a pre-dating snapshot; unformalized textbook
  theorems; open-flavored finite checks). Terminal-artifact compliance must
  be 100%: every run ends proved | disproved | documented.
- **Adversarial fixtures (Phases 3–4):** false-lemma, false-decomposition,
  vacuous-statement, axiom-temptation sets. *Fixture authoring lands with
  Phase 3.*

## Per-phase gates

| Phase | Gate |
|---|---|
| P1 | kill -9 at a random point, resume, zero verified-work loss, graph reconciles (10/10 drills — `harness.reconcile_drill`) |
| P2 | dark-launch nudge log human-rated ≥70% helpful, 0 verdict-adjacent |
| P3 | adversarial fixture (a) + ledger: zero lost jobs across 100 dispatches |
| P4 | T2 uplift vs the Phase-2 baseline + fixtures (b)(c)(d) |
| P5 | T3 first runs: 100% terminal-artifact compliance |
| P6 | T3 with research mode on: give-up-termination rate 0 |

## Protocol

Frozen suites, pinned toolchain/mathlib per suite version. Every
phase-enable PR runs T1 + its gate tier with flags off AND on; results
append to `evals/results.jsonl` via `harness.append_result` (one JSON object
per line: suite, phase, flags, metrics, timestamp). Any T1 regression or a
T2 solve-rate drop >1σ blocks the flag default-flip.

`python -m pytest tests/leanflow/test_eval_harness.py` exercises the scorer
itself; live-run scoring is invoked with
`harness.score_terminal_artifacts(<workflow-state root>)`.
