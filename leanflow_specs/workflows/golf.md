---
id: golf
kind: workflow
title: Golf
summary: Lean proof improvement workflow for directness, brevity, and maintainability without sacrificing correctness or introducing axioms.
skills: [lean-refactor-golf]
tools: [lean_capabilities, lean_inspect, lean_search, lean_multi_attempt, lean_verify, lean_axioms]
workers: []
stop_conditions: [verified, blocked]
route_actions: [golf]
---

# Native Golf Spec

Golf only runs after a proof compiles. Preserve readability and check axioms before accepting a shortened proof.

## When To Use

Use this workflow when a proof already compiles and the goal is directness, brevity, clarity, or lighter proof search burden.

Typical uses:

- replacing `apply ...; exact ...` with direct terms
- collapsing boilerplate tactic wrappers
- simplifying a local proof without changing theorem meaning
- removing accidental axiom or automation inflation introduced during proving

## What Not To Use This Workflow For

Do not use this workflow for:

- active compiler blockers or open `sorry`
- statement redesign or declaration redrafting
- broad strategy refactors across multiple helpers
- read-only quality review

Use `prove` first when the theorem does not compile, `formalize` when the statement is still evolving, `refactor` for structural strategy changes, and `review` for read-only auditing.

## Tool Order

1. `lean_capabilities`
   - check provider availability before planning search-backed optimization
2. `lean_inspect`
   - confirm the target proof already compiles and capture the current local state
3. `lean_search`
   - search for shorter or more direct local/library proof shapes before inventing them
4. `lean_multi_attempt`
   - use only for 2-6 concrete local simplification candidates at one proof position
   - do not send theorem-sized proof blocks, statement changes, or candidates containing `sorry`
5. apply one local simplification batch
   - keep theorem meaning fixed
   - prefer small, reversible edits
6. `lean_verify`
   - verify the simplified proof immediately
7. `lean_axioms`
   - use when a shorter proof might worsen the axiom profile
## Golf Policy

Prefer candidates in this order:

- more direct proof shape
- lower inference/search burden
- clearer proof structure
- shorter code

Avoid “wins” that:

- make the proof shorter but harder to maintain
- move to heavier automation for a tiny line-count gain
- hide important intermediate names
- worsen the axiom profile

## Verification Ladder

1. `lean_inspect` after each local simplification
2. `lean_verify(mode=file_exact)` for file-scoped acceptance
3. broader verification only when the workflow scope requires it

Golf is successful only when the new proof is still correct and the simplification is a real improvement, not just fewer characters.

## Stop Conditions

Stop when:

- the targeted proof has been meaningfully simplified and still verifies
- the remaining opportunities are marginal or reduce clarity
- the workflow uncovers a structural issue that really belongs in `refactor`

## Handoff Format

If golfing remains unfinished, record:

- proof targeted
- simplifications already applied
- best remaining candidate
- verification result
- whether the next step is continue, refactor, or stop
