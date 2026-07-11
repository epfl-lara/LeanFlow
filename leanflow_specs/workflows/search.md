---
id: search
kind: helper
title: Search
summary: Unified Lean search helper that prefers MCP/LSP providers and falls back to native project and mathlib ripgrep search with provider provenance.
skills: [lean-search]
tools: [lean_capabilities, lean_search]
route_actions: [search]
phases: [phase-search]
---

# Native Search Spec

Search before proving, formalizing, refactoring, or golfing.

## When To Use

Use search when the blocker is missing knowledge, not missing syntax:

- you do not know the local declaration name
- you need a Mathlib lemma with a certain shape
- the goal suggests an existing theorem rather than a fresh proof idea
- the current proof keeps failing because the right fact has not been found

## Tool Usage

The provider order, the empty-search budget, and the findings deliverable
are the `phase-search` fragment's contract (`leanflow_specs/phases/search.md`)
— one contract for this helper, the prover pre-step, and deep-search jobs.
Follow it exactly; do not ignore `degraded_reasons`.

## Handoff

Report per the `phase-search` deliverable schema: findings with sources,
`providers_tried` in order, and whether search is now `exhausted` for
routing purposes.
