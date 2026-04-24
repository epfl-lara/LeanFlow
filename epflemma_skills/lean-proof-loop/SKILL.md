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
3. `lean_search` with the smallest relevant search mode
4. `lean_proof_context`, `lean_auto_probe`, `lean_auto_search`, or `lean_auto_try` only when the blocker/search state justifies theorem-local automation
5. `lean_multi_attempt` only for 2-6 short local tactic candidates at one proof location, never for full proof blocks or snippets containing `sorry`
6. `lean_reasoning_help` when repeated focused attempts fail and another configured model may provide proof-strategy advice
7. `apply_verified_patch` for Lean file edits whenever you have a concrete patch; it applies the patch and immediately runs the required check
8. `lean_verify` only when inspecting existing state or doing a final broader verification not already covered by `apply_verified_patch`
9. `lean_worker_dispatch` when the route recommends a specialist worker

## Operating Rules

1. Trust the queue manager and `route_decision` over free-form exploration.
2. Use one declaration at a time when a queue item is assigned.
3. Treat failed-attempt history as negative guidance.
4. Keep work pinned to the requested file or project scope.
5. Use theorem-context and automation wrappers only after search exhaustion, repeated blockers, or an explicitly automation-suited route.
6. Treat `lean_reasoning_help` output as advice only; if it is unavailable or returns no answer, continue the main proof workflow and report that the advisor was unavailable if relevant.
7. Prefer `apply_verified_patch` over raw `patch`/`write_file` for `.lean` edits so workflow state records whether the last edit is verified, check-failed, or patch-failed.
8. Finish only after explicit verification of the requested scope.

## Verification Rules

- File-scoped theorem turns: iterate with `lean_inspect`, then use `apply_verified_patch(check_mode=file_exact)` for proof edits; accept success only after that check or an explicit `lean_verify(mode=file_exact)` succeeds for the active file.
- Module/project turns: prefer focused `lean_verify` module checks before a final project build.
- Do not treat `grep`, truncated terminal output, or a disappearing `sorry` as success.
