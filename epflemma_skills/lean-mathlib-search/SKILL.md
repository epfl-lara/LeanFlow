---
name: lean-mathlib-search
description: Search Mathlib efficiently for theorem names, APIs, and proof patterns before guessing a proof.
---

# Lean Mathlib Search

Use this skill when a proof likely depends on existing Mathlib lemmas, tactics, or naming conventions.

## Procedure

1. Start with Lean/LSP navigation and project imports to confirm the namespace and expected object types.
2. Search Mathlib by likely theorem names, namespace fragments, notation, and conclusion shape before inventing a new lemma.
3. When a search hit looks promising, inspect its exact statement and required hypotheses before using it in a proof.
4. Prefer reusing established Mathlib lemmas over re-proving standard facts locally.
5. If Mathlib lacks the exact shape you need, record the closest hits and derive only the missing bridge lemma.

## Search Heuristics

- Try both short and namespaced forms such as `Nat.succ`, `List.map`, `abs`, `Submodule.span`.
- Search by conclusion keywords and algebraic objects, not only by the desired final theorem name.
- If tactic-based search fails, search imports and nearby files for examples of the same construction.
- When proof terms fail due to mismatched hypotheses, search again using the actual hypothesis types from the goal.

## Guardrails

- Do not guess theorem names and loop on compiler failures when Mathlib search can settle the question faster.
- Do not duplicate a standard Mathlib result under a project-local name unless the project explicitly wants that wrapper.
- Keep the final proof aligned with the local codebase style even when the supporting lemma comes from Mathlib.
