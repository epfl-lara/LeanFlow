---
name: lean-proof-loop
description: Native proving workflow entry. Follow the prove/formalize specs, structured Lean tools, queue state, and router decisions instead of free-form proof guessing.
---

# Native Lean Proof Loop

Primary specs:

- `leanflow_specs/workflows/prove.md`
- `leanflow_specs/workflows/formalize.md`
- `leanflow_specs/workflows/search.md`

Treat the native workflow specs as the contract. This skill is the routing layer that points to them.

## Tool Order

When the manager handoff reports that the unchanged assigned declaration already exhausted a
bounded exact or incremental verification check without errors or placeholders, do not begin by
repeating `lean_inspect` or a broad file check. After two such timeouts, take the decomposition
route: first inventory substantial local proofs with `lean_extract_have(action="inventory")`, then
promote one cohesive block or a bounded semantically named batch; otherwise derive cohesive new helpers with `lean_decompose_helpers`. Verify helpers
independently with LeanProbe and retry the parent only after its body is materially smaller. This
timeout-recovery rule overrides the default order below.

1. `lean_capabilities`
2. `lean_inspect`
3. `lean_search` with the smallest relevant search mode. It may use local project search, local/public Loogle, LeanExplore, semantic providers, and Mathlib fallbacks behind one wrapper.
4. `lean_multi_attempt` when you have 2-6 short tactic candidates and want to screen them before editing; do not send declaration-sized or multi-line `have ... := by` proof blocks. REPL power mode can make this much cheaper than patch/verify loops. Treat only `verified_attempts`/`target_verified=true` as proof-closing evidence; raw empty-goal probe output is provisional.
5. `lean_proof_context` or `lean_auto_search` when theorem-local context or automation search would reduce guessing. Use them opportunistically for automation-shaped goals or repeated blockers; do not force them when a direct edit is clearer.
6. `lean_extract_have` after repeated timeouts: inventory active candidates, choose a cohesive high-value split, optionally assign semantic helper names, and transactionally promote up to four local proofs with independent helper/call-site checks
7. `lean_decompose_helpers` when a hard theorem needs new helper lemmas, intermediate invariants, or a proof split before a useful edit is clear. Prefer this over broad advice when the next step should be a checked sublemma plan.
8. `patch` or `write_file` for managed Lean file edits; the queue manager verifies successful edits against the required gate and restores the exact pre-edit source when Lean reports hard errors
9. `apply_verified_patch` only when you specifically need a single atomic patch/checkpoint/verification result
10. `lean_verify` only when inspecting existing state or doing a final broader verification not already covered by the manager gate
11. `lean_reasoning_help` when repeated focused attempts fail and another configured model may provide broad proof-strategy advice

## Operating Rules

1. Trust the queue manager and `route_decision` over free-form exploration.
2. Use one assigned theorem goal at a time when a queue item is assigned; small helper declarations are allowed only when they directly support that assigned goal.
3. Treat failed-attempt history as negative guidance.
4. Keep work pinned to the requested file or project scope.
5. Use theorem-context and automation-search wrappers only after search exhaustion, repeated blockers, or an explicitly automation-suited route.
6. Helper decomposition is a standard, first-class strategy: when the theorem is hard, after about two failed direct attempts, or after repeated bounded verification timeouts, inventory active local blocks with `lean_extract_have(action="inventory")`. Choose one cohesive candidate or a bounded batch of up to four, assign semantic `helper_names` when the defaults are not explanatory, and let the tool verify every extracted helper and rewritten call site before its single transactional commit. If no suitable local block exists, call `lean_decompose_helpers`, insert the `ready_to_insert` helper skeletons now, prove each helper, then assemble the assigned goal from them. Prefer top-level helpers over additional local `have` blocks when declaration size is causing the timeout, so LeanProbe can cache and verify each phase independently. A helper's `sorry` is normal work-in-progress during the turn; the sorry-free requirement applies at final acceptance, not to intermediate states. Use `omit ... in` or a narrower section for helpers that do not use file-level variables/instances, avoiding predictable `unusedSectionVars` warning growth.
7. Treat `lean_reasoning_help` output as advice only. Its deterministic guard removes terminal surrender recommendations and reframes blocker/open-problem assessments as route-change evidence. If it is unavailable or returns no answer, continue the main proof workflow and report that the advisor was unavailable if relevant.
8. In managed queue workflows, prefer `patch`/`write_file` because the runner records the automatic post-edit `lean_incremental_check(check_target)` result and falls back to Lake only when LeanProbe is unavailable, crashes, or cannot rebuild its cache. A bounded target-check timeout rejects that attempt without starting a duplicate full-file check. Use `apply_verified_patch` for compatibility or when its pre-edit checkpoint payload is specifically useful.
9. Preserve existing theorem, lemma, and example statements exactly unless the user explicitly requested a refactor. New helper declarations are allowed, but pre-existing future queue declarations are not part of the current turn.
10. Finish only after explicit verification of the requested scope.
11. A negative knowledge prior is not a blocker. If a proof, construction, or library fact initially seems beyond reach, respond by inspecting the exact goal, researching the missing fact, deriving a smaller invariant, and checking a concrete candidate. Continue through materially distinct routes until Lean or deterministic workflow evidence—not confidence—justifies changing course.
12. Concrete route advice creates an attempt obligation: apply its first edit or produce precise Lean evidence rejecting it before requesting another plan. Preserve the strongest kernel-verified route and target-local checked facts across compression and rewrites.
13. When elaboration or arithmetic automation fails around a multiline `∑`, `∏`, or other big-operator expression, first parenthesize the complete summand or product body and check that smaller expression independently. Do this before changing the mathematical route: Lean's parser can otherwise associate trailing arithmetic outside the binder and make a correct `ring`/`nlinarith` step appear false.

## Verification Rules

- File-scoped theorem turns: iterate with `lean_inspect`, edit with the managed edit path, and accept success after the automatic post-edit `lean_incremental_check(check_target)` gate or an explicit equivalent succeeds for the assigned declaration. Use `lean_verify(mode=file_exact)` for final Lake sweeps, fallback, or explicit canonical verification.
- Queue edit scope protects pre-existing non-assigned declarations. Adding and refining new helper declarations for the assigned theorem is allowed; solving or rewriting future queued declarations is not.
- For stuck file-scoped proofs, request richer LeanProbe feedback with `lean_incremental_check(action=feedback, include_tactics=true)`. Read tactic goals/proof states and `feedback_lean` before changing strategy.
- Use LeanFlow's Lean tools for every managed queue verification so the manager can classify the result and reuse the warm cache. Direct terminal Lean/Lake checks are rejected during theorem turns; report a broken incremental backend so the manager can run the canonical fallback.
- Module/project turns: prefer focused `lean_verify` module checks before a final project build.
- Do not treat `grep`, truncated terminal output, or a disappearing `sorry` as success.
