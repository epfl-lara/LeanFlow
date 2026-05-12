---
id: proof-repair
kind: worker
title: Proof Repair
summary: Compiler-guided repair worker for repeated type mismatch, unknown identifier, instance synthesis, timeout, and unsolved-goal blockers.
tools: [lean_inspect, lean_search, lean_proof_context, lean_verify]
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
2. `lean_search` only when a missing symbol or instance suggests a known fact
3. `lean_proof_context` when repeated compiler-guided attempts still leave a theorem-local blocker
4. small local edit
5. `lean_verify` at the narrowest truthful gate

## Operating Rules

- stay on the assigned file and declaration
- prefer the smallest diff that changes the blocker
- preserve theorem meaning and declaration headers
- stop after a real repair or a crisp blocker report; do not drift into broad cleanup

## Handoff

Report:

- blocker signature addressed
- exact change made
- verification result
- whether the blocker changed, persisted, or widened
