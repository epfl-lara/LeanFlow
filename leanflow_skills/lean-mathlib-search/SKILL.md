---
name: lean-mathlib-search
description: Native Mathlib/search entry. Use the shared `lean_search` surface first, with provider-aware fallbacks and result provenance.
---

# Native Lean Search

Primary spec:

- `leanflow_specs/workflows/search.md`

## Search Order

1. `lean_search mode=local` when the answer may already exist in the project
2. `lean_search mode=semantic` for semantic theorem discovery
3. `lean_search mode=type-pattern` when the goal shape matters most
4. `lean_search mode=natural-language` for broad Mathlib discovery
5. fall back to small `rg`-style project inspection only when the shared search surface returns nothing

## Search Heuristics

- Try both short and namespaced forms such as `Nat.succ`, `List.map`, `abs`, `Submodule.span`.
- Search by conclusion keywords and algebraic objects, not only by the desired final theorem name.
- If tactic-based search fails, search imports and nearby files for examples of the same construction.
- When proof terms fail due to mismatched hypotheses, search again using the actual hypothesis types from the goal.

## Guardrails

- Do not guess theorem names and loop on compiler failures when `lean_search` can settle the question faster.
- Do not duplicate a standard Mathlib result under a project-local name unless the project explicitly wants that wrapper.
- Keep the final proof aligned with the local codebase style even when the supporting lemma comes from Mathlib.
