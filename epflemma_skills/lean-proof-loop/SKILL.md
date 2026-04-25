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
5. `lean_proof_context`, `lean_auto_probe`, `lean_auto_search`, or `lean_auto_try` when theorem-local context or automation would reduce guessing. Use them opportunistically for automation-shaped goals, repeated blockers, or a concrete candidate proof; do not force them when a direct edit is clearer.
6. `lean_reasoning_help` when repeated focused attempts fail and another configured model may provide proof-strategy advice
7. `patch` or `write_file` for managed Lean file edits; the queue manager verifies successful edits against the required gate
8. `apply_verified_patch` only when you specifically need a single atomic patch/checkpoint/verification result
9. `lean_verify` only when inspecting existing state or doing a final broader verification not already covered by the manager gate
10. `lean_worker_dispatch` when the route recommends a specialist worker

## Operating Rules

1. Trust the queue manager and `route_decision` over free-form exploration.
2. Use one declaration at a time when a queue item is assigned.
3. Treat failed-attempt history as negative guidance.
4. Keep work pinned to the requested file or project scope.
5. Use theorem-context and automation wrappers only after search exhaustion, repeated blockers, or an explicitly automation-suited route.
6. Treat `lean_reasoning_help` output as advice only; if it is unavailable or returns no answer, continue the main proof workflow and report that the advisor was unavailable if relevant.
7. In managed queue workflows, prefer `patch`/`write_file` because the runner records the automatic post-edit verification result. Use `apply_verified_patch` for compatibility or when its pre-edit checkpoint payload is specifically useful.
8. Treat `lean_auto_try` backend/setup errors as tool or file-level blockers, not theorem proof failures; do not edit unrelated examples or solved declarations just to satisfy the automation backend.
9. Finish only after explicit verification of the requested scope.

## Verification Rules

- File-scoped theorem turns: iterate with `lean_inspect`, edit with the managed edit path, and accept success only after the automatic post-edit gate or an explicit `lean_verify(mode=file_exact)` succeeds for the active file.
- Module/project turns: prefer focused `lean_verify` module checks before a final project build.
- Do not treat `grep`, truncated terminal output, or a disappearing `sorry` as success.
