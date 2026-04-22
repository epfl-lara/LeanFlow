---
id: refactor
kind: workflow
title: Refactor
summary: Lean proof refactoring with structure-preserving simplification, search-backed helper reuse, and explicit verification after each meaningful edit.
skills: [lean-refactor-golf]
tools: [lean_capabilities, lean_inspect, lean_search, lean_verify, lean_worker_dispatch]
workers: [proof-golfer]
stop_conditions: [verified, blocked]
route_actions: [refactor]
---

# Native Refactor Spec

Refactor keeps theorem meaning fixed and focuses on reusable local improvements.
