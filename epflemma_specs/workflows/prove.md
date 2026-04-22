---
id: prove
kind: workflow
title: Prove
summary: Queue-driven autonomous theorem proving with LSP-first inspection, native search fallbacks, worker escalation, and strict verification gates.
aliases: [autoprove]
skills: [lean-proof-loop, lean-theorem-queue-worker]
tools: [lean_capabilities, lean_inspect, lean_search, lean_verify, lean_sorries, lean_axioms, lean_worker_dispatch]
workers: [proof-repair, axiom-eliminator, sorry-filler-deep]
review_actions: [continue, replan, redraft, falsify, stop]
stop_conditions: [verified, blocked, interrupted, stalled]
route_actions: [queue-worker, final-sweep, delegate-proof-repair, delegate-axiom-eliminator, delegate-sorry-filler-deep]
---

# Native Prove Spec

Use `/prove` or `/autoprove` for the same autonomous workflow.

## Tool Order

1. `lean_capabilities`
2. `lean_inspect`
3. `lean_search`
4. `lean_worker_dispatch` when the router recommends a worker
5. `lean_verify`

## Verification Ladder

- Per-edit: native diagnostics/goals from `lean_inspect`
- File gate: `lean_verify(mode=file_exact)`
- Milestone/project gate: `lean_verify(mode=project)`

## Escalation

- `proof-repair` for repeated compiler-style blockers
- `axiom-eliminator` for axiom-sensitive situations
- `sorry-filler-deep` for stubborn theorem queue items after repeated failures or exhausted search

## Stop Conditions

- verified scope
- hard blocker recorded
- interrupted workflow
- stalled progress with no new plan
