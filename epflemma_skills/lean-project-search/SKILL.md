---
name: lean-project-search
description: Native local-project search entry. Use `lean_search` local mode and the queue context to stay inside the relevant dependency cone.
---

# Native Lean Project Search

Use this skill when the next step depends on the local Lean project rather than generic Mathlib knowledge.

Primary spec:

- `epflemma_specs/workflows/search.md`

## Tool Order

1. read the assigned file / declaration context
2. `lean_search mode=local`
3. inspect nearby modules only if the local search or queue hints point there
4. return the smallest set of relevant matches with provenance

## Search Targets

- Earlier theorems in the same file that solve a similar goal shape
- Existing helper lemmas in sibling modules
- Imports that already expose the needed API
- Project-specific notation, abbreviations, and wrapper definitions
- Previous proofs that demonstrate the expected tactic or term style

## Guardrails

- Do not drift into unrelated files once the relevant local pattern is found.
- Do not duplicate an existing project helper just because its name was not obvious at first glance.
- When the workflow is file-scoped, search outward only as far as needed to unblock that file.
