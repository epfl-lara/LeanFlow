---
name: lean-formalization
description: Native formalization workflow entry. Follow the formalize/draft specs, typed Lean tools, and queue-driven verification ladder.
---

# Native Lean Formalization

Primary specs:

- `epflemma_specs/workflows/formalize.md`
- `epflemma_specs/workflows/draft.md` when available through routing
- `epflemma_specs/workflows/search.md`

## Tool Order

1. `formalization_document_inspect` when `/formalize` provided a source `.tex` or `.pdf`
2. `lean_capabilities`
3. `lean_inspect`
4. `lean_search`
5. create or update the planner blueprint before deep proof work
6. `lean_proof_context`, `lean_auto_probe`, `lean_auto_search`, or `lean_auto_try` only when a drafted declaration is blocked and theorem-local automation is justified
7. draft the declaration or helper lemma
8. `patch` or `write_file` for managed Lean file edits; the queue manager verifies successful edits against the current gate
9. `apply_verified_patch` only when you specifically need a single atomic patch/checkpoint/verification result
10. `lean_verify` for final broader verification when the manager gate did not cover the requested scope
11. `lean_worker_dispatch` when the router recommends `proof-repair`, `axiom-eliminator`, or `sorry-filler-deep`

## Guardrails

- Keep names readable and codebase-consistent.
- Start from structured Lean state, not guessed missing imports or guessed theorem names.
- In managed queue workflows, prefer `patch`/`write_file` because the runner records the automatic post-edit `lean_incremental_check(check_target)` result and falls back to Lake only when needed. Use `apply_verified_patch` for compatibility or when its pre-edit checkpoint payload is specifically useful.
- Prefer focused `lean_verify` module checks when close to clean; reserve full-project verification for milestone checks.
- Prefer explicit intermediate lemmas over brittle proof scripts.
- For document formalization, add a source comment immediately above every generated declaration with the informal statement, source pointer, and useful proof/dependency notes.
- Keep the generated blueprint aligned with declaration names, split lemmas, source labels, and statement-fidelity decisions.
- Do not declare success while the requested scope still has diagnostics, open goals, warnings, or `sorry`.
- Surface missing assumptions or ambiguous math instead of hiding them.
