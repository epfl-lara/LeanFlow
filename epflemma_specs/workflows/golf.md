---
id: golf
kind: workflow
title: Golf
summary: Lean proof improvement workflow for directness, brevity, and maintainability without sacrificing correctness or introducing axioms.
skills: [lean-refactor-golf]
tools: [lean_capabilities, lean_inspect, lean_search, lean_verify, lean_worker_dispatch, lean_axioms]
workers: [proof-golfer, axiom-eliminator]
stop_conditions: [verified, blocked]
route_actions: [delegate-proof-golfer, delegate-axiom-eliminator]
---

# Native Golf Spec

Golf only runs after a proof compiles. Preserve readability and check axioms before accepting a shortened proof.
