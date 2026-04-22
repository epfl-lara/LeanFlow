---
id: formalize
kind: workflow
title: Formalize
summary: Autonomous Lean formalization that drafts declarations in small verifiable steps and then proves them through the same queue-driven proving engine.
aliases: [autoformalize]
skills: [lean-formalization, lean-proof-loop, lean-theorem-queue-worker]
tools: [lean_capabilities, lean_inspect, lean_search, lean_verify, lean_sorries, lean_axioms, lean_worker_dispatch]
workers: [proof-repair, axiom-eliminator, sorry-filler-deep]
review_actions: [continue, replan, redraft, falsify, stop]
stop_conditions: [verified, blocked, interrupted, stalled]
route_actions: [queue-worker, final-sweep, delegate-proof-repair, delegate-axiom-eliminator, delegate-sorry-filler-deep]
---

# Native Formalize Spec

Use `/formalize` or `/autoformalize` for the same autonomous workflow.

## When To Use

Use this workflow when the input is mathematical intent that must be turned into Lean declarations and verified proofs.

Typical inputs:

- an informal theorem statement
- a request to formalize a definition plus first lemmas
- a partially drafted Lean file that still needs formalization and proof completion

## What Not To Use This Workflow For

Do not use this workflow for:

- proof-only repair where the statement shape is already stable
- read-only review
- save-point/checkpoint work
- pure tactic shortening after the declarations already compile

Use `prove`, `review`, `checkpoint`, or `golf` for those cases. Use `draft` only when the task is limited to declaration skeletons or signatures with no expectation of completing the proving loop.

## Tool Order

1. `lean_capabilities`
   - read the actual available diagnostics, search, and worker surface first
2. `lean_inspect`
   - inspect the target file, existing declarations, queue items, and blocker kind
   - use this before inserting more declarations into a broken file
3. `lean_search`
   - search local project facts, imports, and Mathlib before inventing names or structures
   - use it both for statement design and proof search
4. draft or revise declarations in small verifiable steps
   - prefer one declaration or one local helper at a time
   - keep imports and dependencies minimal and explicit
5. `lean_worker_dispatch`
   - use only when the route points to `proof-repair`, `axiom-eliminator`, or `sorry-filler-deep`
6. `lean_verify`
   - use the narrowest truthful verification gate for the current step
7. `lean_sorries` / `lean_axioms`
   - use when remaining `sorry` inventory or axiom profile is the real blocker

## Formalization Policy

Formalization is not complete when the declaration header merely parses. The workflow should move from informal intent to:

- declarations with stable names and signatures
- compilable Lean code
- proved statements
- verified scope with no residual `sorry`

Prefer:

- explicit intermediate lemmas over brittle monolithic tactics
- local project naming and import patterns over fresh ad hoc style
- small draft-and-verify increments over large speculative file rewrites

Avoid:

- drifting into unrelated helper files unless the workflow explicitly widened scope
- repeated header rewrites when the blocker is actually proof search or compilation
- declaring victory after only drafting statements without proving them

## Header Stability And Redraft

Headers should stay stable once they are good enough to support focused proving work.

Redraft is justified when:

- the statement is ill-typed
- the dependencies are wrong
- the generated shape clearly blocks the proof
- the workflow or route decision explicitly calls for a redraft

Redraft is not the default answer to an ordinary proof blocker. If the statement already expresses the intended theorem, prefer proof repair over header churn.

## Verification Ladder

Formalization uses the same verification ladder as proving:

1. `lean_inspect` for per-edit diagnostics and goals
2. `lean_verify(mode=file_exact)` for file-scoped theorem acceptance
3. `lean_verify(mode=module)` for focused milestone checks
4. `lean_verify(mode=project)` before declaring project-scoped formalization complete

Completion requires:

- the formalized declarations compile
- diagnostics are clean in the requested scope
- there are no open goals
- there are no remaining `sorry` in the requested scope

## Blocker Taxonomy

- statement-design blocker
  - wrong dependencies, malformed signature, or missing imports
  - route: redraft in small steps, then re-inspect
- compiler-style blocker
  - route: direct local fix, then `proof-repair` if repeated
- search blocker
  - route: `lean_search` before changing the theorem shape again
- axiom-risk blocker
  - route: `lean_axioms`, then `axiom-eliminator` if necessary
- stuck formalization queue item
  - route: bounded escalation to `sorry-filler-deep`

## Worker Escalation

- `proof-repair`
  - use when the drafted declaration shape is acceptable but proof compilation keeps failing in the same way
- `axiom-eliminator`
  - use when the formalized result compiles but the axiom profile is not acceptable
- `sorry-filler-deep`
  - use when the current declaration needs multi-step restructuring after repeated local attempts

## Stop Conditions

Stop only when:

- the requested formalization scope is verified
- a concrete blocker has been recorded and the next action is a handoff or redraft, not another speculative proof attempt
- the workflow was interrupted
- progress is stalled and the route decision already identifies the correct next step

## Handoff Format

When handing off unfinished formalization work, include:

- declarations added or revised
- declaration still blocked
- current blocker kind
- verification gate most recently passed
- search already attempted
- whether a redraft or worker escalation is recommended
