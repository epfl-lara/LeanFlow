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

Search before prove. Prefer local and semantic providers first, then fall back to project or mathlib search.
