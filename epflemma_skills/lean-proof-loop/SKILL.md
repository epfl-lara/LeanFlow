---
name: lean-proof-loop
description: Native proving workflow entry. Follow the prove/formalize specs, structured Lean tools, queue state, and router decisions instead of free-form proof guessing.
---

# Native Lean Proof Loop

Primary specs:

- `epflemma_specs/workflows/prove.md`
- `epflemma_specs/workflows/formalize.md`
- `epflemma_specs/workflows/search.md`

Treat the native workflow specs as the contract. This skill is the routing layer that points to them.

## Tool Order

1. `lean_capabilities`
2. `lean_inspect`
3. `lean_search` with the smallest relevant search mode. It may use local project search, local/public Loogle, LeanExplore, semantic providers, and Mathlib fallbacks behind one wrapper.
4. `lean_multi_attempt` when you have 2-6 short tactic candidates and want to screen them before editing; REPL power mode can make this much cheaper than patch/verify loops.
5. `lean_proof_context` or `lean_auto_search` when theorem-local context or automation search would reduce guessing. Use them opportunistically for automation-shaped goals or repeated blockers; do not force them when a direct edit is clearer.
6. `lean_decompose_helpers` when a hard theorem needs helper lemmas, intermediate invariants, or a proof split before a useful edit is clear. Prefer this over broad advice when the next step should be a checked sublemma plan.
7. `patch` or `write_file` for managed Lean file edits; the queue manager verifies successful edits against the required gate
8. `apply_verified_patch` only when you specifically need a single atomic patch/checkpoint/verification result
9. `lean_verify` only when inspecting existing state or doing a final broader verification not already covered by the manager gate
10. `lean_reasoning_help` when repeated focused attempts fail and another configured model may provide broad proof-strategy advice
11. `lean_worker_dispatch` when the route recommends a specialist worker

## Operating Rules

1. Trust the queue manager and `route_decision` over free-form exploration.
2. Use one assigned theorem goal at a time when a queue item is assigned; small helper declarations are allowed only when they directly support that assigned goal.
3. Treat failed-attempt history as negative guidance.
4. Keep work pinned to the requested file or project scope.
5. Use theorem-context and automation-search wrappers only after search exhaustion, repeated blockers, or an explicitly automation-suited route.
6. When the theorem is hard and direct proof search is turning into broad repeated searches, call `lean_decompose_helpers` for split advice before inserting placeholder comments or theorem-sized helper guesses. Use `ready_to_insert` helper skeletons as a plan, then prove them without lingering `sorry`.
7. Treat `lean_reasoning_help` output as advice only; if it is unavailable or returns no answer, continue the main proof workflow and report that the advisor was unavailable if relevant.
8. In managed queue workflows, prefer `patch`/`write_file` because the runner records the automatic post-edit `lean_incremental_check(check_target)` result and falls back to Lake only when needed. Use `apply_verified_patch` for compatibility or when its pre-edit checkpoint payload is specifically useful.
9. Preserve existing theorem, lemma, and example statements exactly unless the user explicitly requested a refactor. New helper declarations are allowed, but pre-existing future queue declarations are not part of the current turn.
10. Finish only after explicit verification of the requested scope.

## Verification Rules

- File-scoped theorem turns: iterate with `lean_inspect`, edit with the managed edit path, and accept success after the automatic post-edit `lean_incremental_check(check_target)` gate or an explicit equivalent succeeds for the assigned declaration. Use `lean_verify(mode=file_exact)` for final Lake sweeps, fallback, or explicit canonical verification.
- Queue edit scope protects pre-existing non-assigned declarations. Adding and refining new helper declarations for the assigned theorem is allowed; solving or rewriting future queued declarations is not.
- For stuck file-scoped proofs, request richer LeanProbe feedback with `lean_incremental_check(action=feedback, include_tactics=true)`. Read tactic goals/proof states and `feedback_lean` before changing strategy.
- Prefer Lean tools for managed queue verification. Terminal-based Lake checks are allowed as an emergency/manual fallback if the Lean tools themselves are broken.
- Module/project turns: prefer focused `lean_verify` module checks before a final project build.
- Do not treat `grep`, truncated terminal output, or a disappearing `sorry` as success.
