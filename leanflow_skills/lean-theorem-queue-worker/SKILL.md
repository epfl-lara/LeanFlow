---
name: lean-theorem-queue-worker
description: Native single-declaration queue entry. Obey the queue handoff exactly, use the shared Lean tools, and escalate through helper decomposition or reasoning help when local attempts stall.
---

# Native Lean Queue Worker

Use this skill when an external workflow manager has already chosen the next declaration to solve.

Primary specs:

- `leanflow_specs/workflows/prove.md`
- `leanflow_specs/workflows/search.md`

## Expected Manager Handoff

1. active Lean file
2. target declaration name and, when possible, line number
3. why this declaration is still pending
4. the last `N` failed attempts for this declaration and why they failed
5. search hints, blocker signature, and verification gate when available

## Worker Contract

1. Focus only on solving the assigned declaration until the manager's verification gate accepts it. Never end
   an unresolved assignment: a blocker report always carries a requested route (`decompose` | `negate` |
   `plan`) plus the evidence for it, and a blocker is never permission to end an unresolved theorem.
2. Do not jump to later theorems in the file, even if they also contain `sorry`.
3. Treat previous failed attempts as negative guidance:
   - do not blindly repeat the same proof shape
   - explain when a new attempt differs materially from earlier failures
4. Helper decomposition is a standard, first-class proving strategy, not a last resort: introduce helper lemmas, local intermediate facts, or small private supporting declarations whenever they make the assigned declaration easier to prove. After about two failed direct attempts, decomposing (for example via `lean_decompose_helpers`) is the expected next move, not another direct rewrite of the same proof shape. A newly inserted helper's `sorry` is normal work-in-progress during the turn; the sorry-free requirement applies at final acceptance of the assigned declaration, not to intermediate states. Keep every helper scoped to the assigned theorem's needs.
5. Queue edit scope protects declarations that already existed when this theorem was assigned. Do not edit, reorder, rename, delete, or solve pre-existing non-assigned declarations or future queue items, but adding and iterating on new helper declarations for this theorem is allowed.
6. Preserve existing theorem, lemma, and example statements exactly unless the user explicitly requested a refactor. This applies to the assigned declaration and to helper declarations after you create them; change proof bodies, not established statements.
7. After each meaningful edit, re-check the assigned declaration with `lean_inspect` or `lean_incremental_check(check_target)` before making another large change.
8. For a managed file-scoped assigned theorem, the preferred edit path is `patch` or `write_file`; the queue manager runs the LeanProbe queue-step verifier after successful edits and falls back to Lake if needed. Use `apply_verified_patch(check_mode=file_exact)` only when you specifically need its atomic checkpoint plus verification payload.
9. When ordinary diagnostics are not enough, call `lean_incremental_check(action=feedback, include_tactics=true)` for the assigned declaration. Use returned `tactics[*].goals`, `tactics[*].proof_state`, file-global message positions, and `feedback_lean` comments as the repair context.
10. Use Lean tools for normal managed queue verification so the manager can classify the assigned declaration. Terminal-based Lake checks are allowed as an emergency/manual fallback if the Lean tools themselves are broken.
11. Do not treat `lake build`, `grep`, `head`, or truncated output as proof that the assigned theorem is clean.
12. If the declaration becomes clean, stop and hand control back to the manager rather than continuing to the next theorem on your own.
13. Treat runtime step-budget warnings as real control signals. With only a few API steps left, prefer one concrete verification-backed edit or a concise blocker report over starting a broad new strategy. A decompose-and-insert helper batch counts as one meaningful edit, not several; switching strategy to decomposition is budgeted work, never budget waste.

## Plan-State Freshness

1. Start every assignment from the deterministic queue handoff and refresh the assigned declaration with `lean_inspect` or current Lean diagnostics. Those sources and the kernel gate are inventory and declaration truth.
2. A managed `plan.md` read exposes bounded, read-only generated sections. Do not edit or paginate that file: the hidden, user-owned historical Notes tail may contain stale sorry counts, helper inventory, copied declarations, and proof sketches. Structured planner state is persisted by the workflow manager.
3. Dependency-graph statuses are useful routing state, but stored graph statements and plan prose are snapshots. If either disagrees with the current queue assignment, Lean source, or kernel diagnostics, follow the current queue and Lean evidence.
4. Use generated Strategy, Frontier, Grounding, and Decision sections as route context. Do not reconstruct the queue or choose a declaration body from historical Notes.
5. Do not read raw `summary.json` or `blueprint.json`: they are machine snapshots that can contain large historical ledgers and stale stored bodies. Use the injected graph digest, completed research-finding handoff, queue assignment, and current Lean diagnostics.

## Queue Hygiene

1. If an earlier unresolved declaration is producing syntax, elaboration, or goal-state errors that prevent useful diagnostics for the current queue item, do not spend the turn solving that earlier declaration unless the manager assigned it to you.
2. Preserve the earlier declaration's current proof work before unblocking the file: comment the broken proof state or failed attempt in place, then close that earlier declaration's active proof body with a minimal `sorry` so the current assigned declaration can be inspected.
3. Never change, weaken, rename, move, or delete the earlier declaration statement while doing this. Only edit the proof body.
4. Treat this as a temporary queue-unblocking move, not success. Mention the preserved commented attempt and the inserted `sorry` in the handoff or failed-attempt summary so a later queue pass can resume from it.
5. Do not use this pattern to finish the assigned declaration. If the assigned declaration still needs `sorry`, report a blocker (with its requested route) instead of claiming success.

## Search Strategy

1. Search the local project first with `lean_search mode=local`.
2. Search Mathlib next with `lean_search mode=semantic|type-pattern|natural-language` when the needed fact looks standard. The wrapper may use local/public Loogle, LeanExplore, semantic providers, and rg fallbacks; trust provider provenance in the result.
3. Use `lean_multi_attempt` when you have 2-6 specific short local tactic candidates at one proof location. This is especially useful before patching small tactic ideas because REPL power mode may screen them quickly.
4. If search is exhausted or the blocker still looks automation-suited, call `lean_proof_context` before deeper automation search.
5. Use `lean_auto_search` when theorem-local automation search is justified. This wrapper is an optional accelerator, not a mandatory step.
6. Do not send theorem-sized proof blocks, declaration headers, or candidates containing `sorry` to `lean_multi_attempt`.
7. If you have one full candidate proof, use the managed edit path unless the atomic `apply_verified_patch` payload is specifically useful.
8. Invent helper lemmas or sublemmas when the direct proof is too large or repeated direct attempts fail. Prefer small statements that are easy to verify and directly feed the assigned declaration.
9. If the theorem is hard because the next useful edit is a sublemma/invariant split, call `lean_decompose_helpers` with the exact statement, current diagnostics/goals, current attempt, and failed-attempt summary. Use it before inserting placeholder comments, unchecked theorem-sized helper guesses, or broad speculative patches.
10. Treat `lean_decompose_helpers` output as a plan to execute: insert the helpers marked `ready_to_insert` now, prove each one, then assemble the assigned declaration from them. Keep failed skeleton diagnostics as blocker context rather than hiding them.
11. If repeated focused attempts fail while the theorem still looks solvable and the blocker is broad strategy/library navigation rather than a split plan, call `lean_reasoning_help` with the statement, diagnostics, current attempt, and failed-attempt summary.
12. If `lean_reasoning_help` reports that the advisor is unavailable or returned no answer, continue with the strongest concrete edit, verification, or blocker report you have.
13. If repeated searches keep returning no useful results, stop searching in that turn and switch to the strongest concrete edit, `lean_decompose_helpers` when a helper split is the likely next edit, verification, or blocker report you have.
14. Preserve accumulated proof work. Never use `git restore`, `git checkout`, `git reset`, or an equivalent bulk reversion to discard a partially verified declaration. Revise it with managed patches; only the deterministic manager may restore its captured safe baseline after recording the failed attempt.

## Success Condition

The assigned declaration is successful only when:

- its proof has no `sorry`
- diagnostics for that declaration are clean
- there are no remaining goals for that declaration
- the attempted fix does not introduce a new local blocker around it
- and the manager-requested check succeeds, either through the automatic post-edit `lean_incremental_check(check_target)` gate, an explicit incremental check, or a final/fallback `lean_verify(mode=file_exact)`
- and any recommended specialist worker route has either been used or explicitly ruled out

## Route-Change Conditions

Record failed-attempt evidence and request a distinct route when:

- the same proof approach keeps failing for a known reason
- the declaration appears to require a missing lemma or changed statement
- the surrounding file state prevents isolated progress on the assigned declaration

When a proof shape fails, summarize the blocker in terms the manager can store as failed-attempt evidence,
request a distinct route (`decompose`, `plan`, or `negate`), and keep the assignment active. A blocker is
never permission to end an unresolved theorem.

If the API step budget is exhausted before you finish, the runner records the current theorem as a failed attempt and, when it has the original untruncated `sorry` slice, comments the current failed declaration above the theorem and restores that declaration to the safe baseline `sorry` body. That is not success and does not skip the theorem; the next queue cycle resumes the same item with the failed-attempt context.
