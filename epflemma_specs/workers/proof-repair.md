---
id: proof-repair
kind: worker
title: Proof Repair
summary: Compiler-guided repair worker for repeated type mismatch, unknown identifier, instance synthesis, timeout, and unsolved-goal blockers.
tools: [lean_inspect, lean_search, lean_verify]
route_actions: [delegate-proof-repair]
---

# Native Proof Repair Worker

Use for repeated compiler-style blockers with a small diff budget and frequent verification.
