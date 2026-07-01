---
name: lean-search
description: Native Lean search entry. Use the shared `lean_search` surface first across local-project and Mathlib/semantic modes, with provider-aware fallbacks and result provenance.
---

# Native Lean Search

The single search entry for LeanFlow: use the shared `lean_search` surface across
local-project and Mathlib/semantic modes, with provider-aware fallbacks and result
provenance.

Primary spec:

- `leanflow_specs/workflows/search.md`

## When To Use

Use search when the blocker is missing knowledge, not missing syntax:

- the next step depends on the local Lean project rather than generic Mathlib knowledge
- you do not know the local declaration name
- you need a Mathlib lemma with a certain shape
- the goal suggests an existing theorem rather than a fresh proof idea
- a proof keeps failing because the right fact has not been found yet

## Search Order

Prefer local context first, then widen to the library:

1. read the assigned file / declaration context (and queue hints, when present)
2. `lean_search mode=local` when the answer may already exist in the project, imports, or nearby files
3. `lean_search mode=semantic` for semantic theorem discovery across the library
4. `lean_search mode=type-pattern` when the goal shape matters most
5. `lean_search mode=natural-language` for broad Mathlib discovery when the exact shape is unclear
6. fall back to small `rg`-style project inspection only when the shared search surface returns nothing

## Local-Project Targets

When the relevant knowledge is project-local, aim the search at:

- earlier theorems in the same file that solve a similar goal shape
- existing helper lemmas in sibling modules
- imports that already expose the needed API
- project-specific notation, abbreviations, and wrapper definitions
- previous proofs that demonstrate the expected tactic or term style

## Mathlib / Semantic Heuristics

When the supporting fact likely lives in the library:

- try both short and namespaced forms such as `Nat.succ`, `List.map`, `abs`, `Submodule.span`
- search by conclusion keywords and algebraic objects, not only by the desired final theorem name
- if tactic-based search fails, search imports and nearby files for examples of the same construction
- when proof terms fail due to mismatched hypotheses, search again using the actual hypothesis types from the goal

## Guardrails

- Do not guess theorem names and loop on compiler failures when `lean_search` can settle the question faster.
- Do not duplicate a standard Mathlib result, or an existing project helper, under a new local name unless the project explicitly wants that wrapper.
- Do not drift into unrelated files once the relevant local pattern is found; when the workflow is file-scoped, search outward only as far as needed to unblock that file.
- Keep the final proof aligned with the local codebase style even when the supporting lemma comes from Mathlib.
- Return the smallest set of relevant matches with provenance.
