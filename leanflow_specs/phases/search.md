---
id: phase-search
kind: phase
title: Search Phase
summary: "Bounded search protocol shared by the prover pre-step and deep-search jobs: provider order, the empty-search budget, and a findings JSON deliverable."
consumed_by: [prover, planner, orchestrator]
deliverable_schema:
  findings:
    - claim: "one-line factual finding"
      source: "url, module path, or repo path"
      relevance: "why it bears on the goal"
      candidate_lemmas: ["Fully.Qualified.Name"]
  providers_tried: ["mode or provider names attempted, in order"]
  exhausted: "bool — whether search is exhausted for routing purposes"
---

# Search Phase

The one search contract. The prover's assignment pre-step, the planner's
mathlib/web lanes, and deep-search dispatch jobs all follow it; the
empty-search budget lives HERE, not in per-workflow prose.

## Provider order

Run the SUBSET of this order your toolset provides, preserving the order
— a Lean-only lane runs steps 1–4 and never reaches the web; a web-only
lane runs step 5 alone. Modes your toolset lacks are not failures: list
them in `providers_tried` as `unavailable:<mode>` so the router can tell
"not tried" from "tried and empty".

1. `lean_capabilities` once per scope — know which providers are live and
   read `degraded_reasons` before trusting any result set.
2. `lean_search(mode=local)` — the project and its dependencies first.
3. `lean_search(mode=semantic)` — local LeanExplore, hosted only when
   `LEANEXPLORE_API_KEY` is configured.
4. `lean_search(mode=type-pattern)` then `mode=natural-language` — widen
   only after narrower modes returned nothing usable.
5. Web (`web_search`/`web_fetch`, `repo_clone` for concrete proof
   developments) — research lanes and deep-search jobs only, never the
   inner prover loop.

## The empty-search budget

- Three consecutive searches without a usable result end searching for the
  turn: make the best concrete edit you have, or report a blocker WITH a
  requested route (`decompose` | `negate` | `plan`) and the search
  evidence.
- `repeated empty search loop detected` in `degraded_reasons` is a hard
  stop for the turn — do not rephrase the same query a fourth time.
- Every provider you tried goes into `providers_tried`, and whether search
  is now exhausted into `exhausted` — the router consumes `exhausted`, so
  omitting it hides a routing signal, and an empty result set without
  `providers_tried` destroys the evidence that searching happened.

## What not to do

- Do not ignore `degraded_reasons`; a degraded provider set means weaker
  ripgrep-style results, not "no results exist".
- Do not report `exhausted: true` after trying a single mode.
- Do not paste search transcripts into the deliverable — findings are
  distilled claims with sources, never raw output.
