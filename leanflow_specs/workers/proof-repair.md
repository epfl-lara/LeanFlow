---
id: proof-repair
kind: worker
title: Proof Repair
summary: Compiler-guided repair worker for repeated type mismatch, unknown identifier, instance synthesis, timeout, and unsolved-goal blockers.
tools: [lean_inspect, lean_incremental_check, lean_search, lean_proof_context, lean_verify]
route_actions: [delegate-proof-repair]
---

# Native Proof Repair Worker

Use for repeated compiler-style blockers with a small diff budget and frequent verification.

## When To Use

Use only when the active blocker is compiler-guided and has repeated after direct local fixes:

- type mismatch
- unknown identifier
- failed instance synthesis
- unsolved-goal compiler output
- tactic timeout or elaboration failure with a clear local source

Do not use this worker for theorem discovery or broad proof redesign. Search first; use `sorry-filler-deep` for deeper restructuring.

## Tool Order

1. `lean_inspect`
2. `lean_incremental_check(action=feedback, include_tactics=true)` when the local goal is unclear
3. `lean_search` only when a missing symbol or instance suggests a known fact
4. `lean_proof_context` when repeated compiler-guided attempts still leave a theorem-local blocker
5. small local edit, then `lean_incremental_check(action=check_target)`
6. `lean_verify` only for the final or explicit broader gate

## Operating Rules

- stay on the assigned file and declaration
- prefer the smallest diff that changes the blocker
- preserve theorem meaning and declaration headers
- after a real repair, hand control back to the kernel gate; after a blocker report,
  preserve its evidence and continue on the manager-selected route without drifting
  into broad cleanup

## Handoff

Report:

- blocker signature addressed
- exact change made
- verification result
- whether the blocker changed, persisted, or widened
