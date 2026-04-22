---
id: checkpoint
kind: workflow
title: Checkpoint
summary: Save a workflow milestone only after explicit verification and auxiliary sanity checks such as axiom inspection.
skills: [lean-diagnostics]
tools: [lean_capabilities, lean_inspect, lean_verify, lean_axioms]
stop_conditions: [checkpoint-written, blocked]
route_actions: [diagnostics]
---

# Native Checkpoint Spec

Checkpoint after the active scope is explicitly verified and the current queue state is persisted.
