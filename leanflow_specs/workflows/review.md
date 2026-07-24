---
id: review
kind: workflow
title: Review
summary: Read-only Lean review workflow for correctness, blockers, style risks, and readiness for the next proving cycle.
skills: [lean-diagnostics]
tools: [lean_capabilities, lean_inspect, lean_search, lean_axioms]
review_actions: [continue, decompose, plan, negate, re-state, park]
stop_conditions: [review-complete]
route_actions: [diagnostics]
phases: [phase-review]
---

# Native Review Spec

Review is read-only by default. Prioritize behavioural and proof-correctness findings over style commentary.

## When To Use

Use this workflow when the goal is to classify the current Lean state without becoming the main editing workflow:

- triaging why a proving run is stuck
- auditing correctness/style/axiom risks
- deciding whether the next command should be prove, refactor, golf, or stop
- leaving a clean handoff for the next cycle

## What Not To Use This Workflow For

Do not use this workflow for:

- primary proof repair
- declaration drafting or formalization
- checkpoint/commit creation
- broad code rewriting disguised as “review”

If the task is to change code until it verifies, use `prove`, `formalize`, `refactor`, or `golf` instead.

## Tool Order

1. `lean_capabilities`
   - read capability and degraded-mode state first so the review does not assume tools that are unavailable
2. `lean_inspect`
   - primary source for diagnostics, goals, blocker kind, queue items, and current verification state
3. `lean_search`
   - use when the blocker appears to be missing knowledge rather than wrong code
4. `lean_axioms`
   - use when the target compiles but proof acceptability is unclear because of axioms

Review is read-only by default. The job is classification and recommendation, not mainline editing.

## Output Contract

A useful review should answer:

- what is blocked
- why it is blocked
- what the next workflow should be
- what not to try again

Prioritize:

- correctness and build blockers
- open goals and `sorry`
- axiom risks
- only then style or golfing opportunities

## Next-Action Vocabulary

One list, aligned to the orchestrator's routes (see the `phase-review`
fragment — it is the canonical contract). Use one of these `next_action`
labels in the review output:

- `continue`
  - current proving path still looks viable (the `direct-prove` route)
- `decompose`
  - the goal needs stated helper lemmas before more attempts
- `plan`
  - the scope needs research/strategy before more prover budget
- `negate`
  - the statement smells false; a feasibility probe is due
- `re-state`
  - the declaration shape is the blocker (sub-lemmas only; main-statement
    changes need a human ACK)
- `park`
  - statement fidelity or required human approval prevents safe autonomous work;
    pause with a complete decision packet
  - difficulty, failed proof shapes, and exhausted route budgets are not parking reasons

Do not invent alternate action labels — `deep`, `repair`, `redraft`,
`golf`, `replan`, `falsify`, and `stop` are retired vocabulary. Actions
are suggestions the router consumes; the kernel gate stays the authority.

## Stop Conditions

The review is complete when it has produced:

- the current blocker classification
- the best next action
- the key evidence supporting that action

## Handoff Format

A concise review handoff should include:

- scope reviewed
- top blockers
- supporting evidence
- `next_action`
- concrete next command or route recommendation
