---
id: axiom-eliminator
kind: worker
title: Axiom Eliminator
summary: Worker for checking and removing non-standard axioms or axiom-sensitive proof rewrites before a workflow is accepted.
tools: [lean_inspect, lean_incremental_check, lean_axioms, lean_search, lean_proof_context, lean_verify]
route_actions: [delegate-axiom-eliminator]
---

# Native Axiom Eliminator Worker

Use when a proof is correct but the axiom profile is unacceptable.

## When To Use

Use only when the theorem already compiles or is very close, but `lean_axioms` shows non-standard axioms or an axiom-sensitive proof shape.

Do not use to fix ordinary compiler errors; use `proof-repair` first.

## Tool Order

1. `lean_inspect`
2. `lean_axioms`
3. `lean_search`
4. `lean_proof_context` when the current proof shape needs theorem-local context before an axiom-sensitive rewrite
5. local proof rewrite, then `lean_incremental_check(action=check_target)`
6. `lean_verify` only for the final or explicit broader gate

## Operating Rules

- preserve theorem meaning
- prefer removing custom axioms without introducing new ones elsewhere
- verify the target theorem again after each meaningful rewrite
- report both the old and new axiom profile

## Handoff

Report:

- original axiom list
- resulting axiom list
- any remaining `custom_axioms`
- verification result
