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
---

# Draft

Use this workflow to create or stabilize declaration skeletons before the main proving loop takes over.

## Inputs

- target file or module
- informal statement or partial Lean declaration
- local style constraints from nearby project code

## Tool Order

1. `lean_capabilities`
2. `lean_inspect`
3. `lean_search`
4. edit the declaration shape
5. `lean_verify`

## Exit Criteria

- imports are coherent
- signatures are stable
- the drafted declaration is ready for proving work or for the next queue pass
