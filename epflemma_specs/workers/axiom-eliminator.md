---
id: axiom-eliminator
kind: worker
title: Axiom Eliminator
summary: Worker for checking and removing non-standard axioms or axiom-sensitive proof rewrites before a workflow is accepted.
tools: [lean_inspect, lean_axioms, lean_search, lean_verify]
route_actions: [delegate-axiom-eliminator]
---

# Native Axiom Eliminator Worker

Use when a proof is correct but the axiom profile is unacceptable.
