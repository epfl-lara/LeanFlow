---
id: sorry-filler-deep
kind: worker
title: Sorry Filler Deep
summary: Strategic deep worker for stubborn theorem queue items that need multi-step restructuring, bounded helper extraction, or repeated search-backed attempts.
tools: [lean_inspect, lean_search, lean_verify, lean_axioms]
route_actions: [delegate-sorry-filler-deep]
---

# Native Sorry Filler Deep Worker

Use after fast queue-worker attempts fail or search is exhausted. Stay within the active file and verification fence unless the workflow explicitly widens scope.

## When To Use

Use when the active declaration needs bounded multi-step restructuring:

- repeated local attempts did not change the blocker
- search is exhausted but the theorem still looks salvageable
- a helper lemma or alternate proof structure is needed

Do not use as the default first worker.

## Tool Order

1. `lean_inspect`
2. `lean_search`
3. bounded restructuring inside the active file
4. `lean_verify`
5. `lean_axioms` if the deeper rewrite raises an axiom-risk question

## Operating Rules

- stay within the active file unless the workflow explicitly widened scope
- preserve declaration headers unless the workflow explicitly allows redraft
- prefer a small number of coherent restructuring moves over many speculative edits
- leave a crisp handoff if the deep route still fails

## Handoff

Report:

- restructuring strategy attempted
- helper lemmas added, if any
- verification result
- whether the next action should be continue, redraft, or stop
