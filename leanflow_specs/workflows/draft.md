---
id: draft
kind: workflow
title: Draft
summary: Draft Lean declarations, signatures, imports, and proof skeletons that are ready for the native proving/formalization loop.
aliases: []
skills:
  - lean-formalization
tools:
  - lean_capabilities
  - lean_inspect
  - lean_search
  - lean_verify
workers: []
review_actions:
  - inspect
  - draft
stop_conditions:
  - signatures-stable
  - imports-resolved
route_actions:
  - draft
phases: [phase-draft]
---

# Draft

Draft Lean declarations that are ready for the proving loop. The stub
shape, placement, naming, and graph-emission rules are the `phase-draft`
fragment's contract (`leanflow_specs/phases/draft.md`) — the same rules
the planner, decomposer, and multi-direction paths enforce mechanically.

## Inputs

- the informal claim or source material to formalize
- the target file and any surrounding declarations

## Tool Order

1. `lean_capabilities`
2. `lean_inspect` on the target file
3. `lean_search` for names, shapes, and prior art
4. write the declaration per the `phase-draft` stub contract
5. `lean_verify` (sorry warnings pass; errors do not)

## Exit Criteria

- imports coherent, signatures stable
- every stub obeys the `phase-draft` shape (one sorry-bodied declaration)
- the declaration is ready for proving or the next queue pass
