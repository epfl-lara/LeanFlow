# Campaign results

One directory per finished campaign, holding only measurements — no proof
sources. The proofs themselves are evidence of a different kind and live with
the run that produced them; what is kept here is what a later run needs in
order to be compared against this one.

Each directory contains:

- `summary.json` — the arm's full frozen `ProverConfig`, the LeanFlow commit and
  a digest over the frozen runtime file set, aggregate totals, and the
  per-split / per-category breakdown.
- `cells.json` — one record per cell: problem identity (including the
  `statement_sha256` that ties it to `lean_proof_bench_v2.csv`), status,
  independent kernel-verification outcome, token and call counts, wall clock,
  final node counts, and every execution attempt.
- `metrics.csv` — the runner's own live export, copied verbatim at the end of
  the run.

`summary.json` records `leanflow_commit` because arms are only comparable
against runs of the same prover. Two runs at different commits can still be
reported together, but the difference has to be stated rather than assumed
away — see the note on `negation_api_calls` below.

## `astra-top-split`

The eighteen Lean-IMO-Bench problems LEAP did not solve, run top-down with
`gpt-6-astra`: orchestrator, review and research at `xhigh`, prover and
negation passes at `low`. 200 calls per prover pass, 50 per planning stage,
2000 per cell, 4 parallel provers, 8 h wall clock, 1200 s per Lean check.
Internet access off; each cell started from the pre-formalized statement and
`sorry` alone.

All eighteen were proved and independently verified: statements byte-identical
to the published CSV, sorry-free, kernel-accepted, and closing on the three
standard axioms only. 3708 API calls, 109.0 M input and 1.59 M output tokens,
15.5 h of summed cell time, 107 helper lemmas.

Two caveats belong with the numbers. Twelve of the eighteen ran before
`search_project` could reach the project's own documentation, so they proved
their theorems without local library search. And the frozen runtime predates
`negation_api_calls`, so its refutation passes were bounded by the prover
budget rather than by their own; a later run at a newer commit is not spending
calls the same way.
