---
id: search
kind: helper
title: Search
summary: Unified Lean search helper that prefers MCP/LSP providers and falls back to native project and mathlib ripgrep search with provider provenance.
skills: [lean-mathlib-search, lean-project-search]
tools: [lean_capabilities, lean_search]
route_actions: [search]
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

1. `lean_capabilities`
   - check whether semantic providers are available before assuming LSP-backed or MCP-backed search exists
2. `lean_search(mode=local)`
   - first choice when the answer may already be in the current project, imports, or nearby files
3. `lean_search(mode=semantic)`
   - use for library-level discovery by meaning
4. `lean_search(mode=type-pattern)`
   - use when the goal shape matters more than words
5. `lean_search(mode=natural-language)`
   - broad fallback for theorem discovery when the exact shape is unclear

## What Not To Do

- do not guess theorem names repeatedly when search can settle it faster
- do not treat search as proof verification
- do not ignore `degraded_reasons`; when semantic providers are unavailable, expect weaker `rg`-style fallback results

## Handoff

When search does not resolve the blocker, record:

- modes already tried
- providers attempted
- top candidate lemmas or declarations
- whether search is now exhausted for router purposes
