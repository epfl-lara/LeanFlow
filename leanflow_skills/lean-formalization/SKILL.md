---
name: lean-formalization
description: Native formalization workflow entry. Follow the formalize/draft specs, typed Lean tools, and queue-driven verification ladder.
---

# Native Lean Formalization

Primary specs:

- `leanflow_specs/workflows/formalize.md`
- `leanflow_specs/workflows/draft.md` when available through routing
- `leanflow_specs/workflows/search.md`

## Tool Order

1. `formalization_document_inspect` when `/formalize` provided a source `.tex`, `.pdf`, or TeX project directory
2. `lean_capabilities`
3. `lean_inspect`
4. `lean_search`
5. create or update the planner blueprint before deep proof work
6. satisfy the document formalization handoff verifier before the managed prover queue starts
7. `lean_proof_context` or `lean_auto_search` only when a drafted declaration is blocked and theorem-local automation search is justified
8. draft the declaration or helper lemma
9. `patch` or `write_file` for managed Lean file edits; the queue manager verifies successful edits against the current gate
10. `apply_verified_patch` only when you specifically need a single atomic patch/checkpoint/verification result
11. `lean_verify` for final broader verification when the manager gate did not cover the requested scope

## Guardrails

- Keep names readable and codebase-consistent.
- Start from structured Lean state, not guessed missing imports or guessed theorem names.
- In managed queue workflows, prefer `patch`/`write_file` because the runner records the automatic post-edit `lean_incremental_check(check_target)` result and falls back to Lake only when LeanProbe is unavailable, crashes, or cannot rebuild its cache. A bounded target-check timeout rejects that attempt without starting a duplicate full-file check. Use `apply_verified_patch` for compatibility or when its pre-edit checkpoint payload is specifically useful.
- Prefer focused `lean_verify` module checks when close to clean; reserve full-project verification for milestone checks.
- Prefer explicit intermediate lemmas over brittle proof scripts.
- Every generated Lean file must begin with imports. Do not put module doc comments, file overviews, namespaces, or declarations above imports.
- For document formalization, update the nearby `Blueprint.md` before writing the main Lean draft. Replace `_pending_` source inventory entries with declaration names, dependencies, split lemmas, statement-fidelity reviews, and proof/prover notes.
- For each source theorem or lemma, put a compact source-aware proof sketch in the Lean doc comment immediately above the generated declaration. Use clear labels such as `Source proof`, `Proof sketch`, or `Prover notes`; do not leave the prover to rediscover the paper proof from scratch. The generated supplemental blueprint skill carries the durable `Blueprint.md` reference for prover turns.
- In the planner draft, leave theorem/lemma/example proofs as `by sorry`; do not fill proofs during `/formalize`, even if they look easy. The managed prover queue should solve them one declaration at a time after statement/source verification is approved.
- Verify planner draft readiness with `lean_inspect` and `lean_verify` (module or file_exact), plus the document formalization handoff verifier. Do not use terminal Lake commands as the normal readiness check.
- Before handoff to the prover queue, let the runner's independent statement/source verification pass review the blueprint and Lean draft. The reviewer must approve or correct the planned declarations, source locators, theorem statements, and prover notes before the handoff verifier passes.
- The statement/source verifier must check concrete fidelity axes, not just plausibility. Every source entry should record `Source qualifiers`, `Lean coverage`, and `Scope changes`. Qualifiers include mathematical object class, quantifier order, parameter domain, output codomain, equality/image condition, side conditions, and follow-on claims. Every explicit qualifier must appear in the Lean theorem, be covered by a companion declaration, or be recorded as an intentional scope change.
- Do not treat a simpler Lean encoding as full coverage for a source claim about a richer object class or representation. Add a bridge declaration, or mark the coverage as partial and record the representation change in the blueprint.
- During the main source-fidelity draft, it is acceptable to work in one generated Lean file if that helps preserve source context and verifier feedback. After statement/source review passes, perform the runner-requested final organization pass: decide whether a multi-file layout improves the project, split into files such as `Basic.lean`, `Constructions.lean`, and `Theorems.lean` only when useful, update `Main.lean` as the aggregator, keep the blueprint declaration/source mappings intact, and run `lean_verify(mode=project)`.
- To make the handoff verifier pass after review: replace scaffold root imports with direct Mathlib/project dependencies, add the generated target module to the root project module so plain `lake build` checks it, keep the blueprint import plan identical to the target Lean imports, ensure the draft has no hard Lean diagnostics, and record `Statement verification status: approved` for each source theorem/lemma inventory entry.
- During proof repair, consult the nearby `Blueprint.md` and original `.tex`/`.pdf` source for the paper's proof strategy before inventing a proof.
- Keep the generated blueprint aligned with declaration names, split lemmas, source labels, and statement-fidelity decisions.
- Do not declare success while the requested scope still has diagnostics, open goals, warnings, or `sorry`.
- Surface missing assumptions or ambiguous math instead of hiding them.
