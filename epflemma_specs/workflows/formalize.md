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

## Tool Order

1. `lean_capabilities`
2. `lean_inspect`
3. `lean_search`
4. Draft or revise declarations in small steps
5. `lean_worker_dispatch` when the router recommends a worker
6. `lean_verify`

## Safety

- keep declaration headers stable unless the formalization task explicitly requires a redraft
- prefer explicit intermediate lemmas over brittle proof scripts
- do not declare success while remaining queue items still contain `sorry`, build errors, or open goals
