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

1. `lean_capabilities`
2. `lean_inspect`
3. `lean_search`
4. `lean_proof_context`, `lean_auto_probe`, `lean_auto_search`, or `lean_auto_try` only when a drafted declaration is blocked and theorem-local automation is justified
5. draft the declaration or helper lemma
6. `apply_verified_patch` for Lean file edits, with `check_mode=file_exact` or `module` depending on the current formalization scope
7. `lean_verify` for final broader verification when `apply_verified_patch` did not cover the requested scope
8. `lean_worker_dispatch` when the router recommends `proof-repair`, `axiom-eliminator`, or `sorry-filler-deep`

## Guardrails

- Keep names readable and codebase-consistent.
- Start from structured Lean state, not guessed missing imports or guessed theorem names.
- Prefer `apply_verified_patch` over raw `patch`/`write_file` for `.lean` edits so each generated declaration or proof edit records verification status immediately.
- Prefer focused `lean_verify` module checks when close to clean; reserve full-project verification for milestone checks.
- Prefer explicit intermediate lemmas over brittle proof scripts.
- Do not declare success while the requested scope still has diagnostics, open goals, warnings, or `sorry`.
- Surface missing assumptions or ambiguous math instead of hiding them.
