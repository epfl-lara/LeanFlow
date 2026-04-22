---
id: review
kind: workflow
title: Review
summary: Read-only Lean review workflow for correctness, blockers, style risks, and readiness for the next proving cycle.
skills: [lean-diagnostics]
tools: [lean_capabilities, lean_inspect, lean_search, lean_axioms]
review_actions: [continue, replan, redraft, falsify, stop]
stop_conditions: [review-complete]
route_actions: [diagnostics]
---

# Native Review Spec

Review is read-only by default. Prioritize behavioural and proof-correctness findings over style commentary.
