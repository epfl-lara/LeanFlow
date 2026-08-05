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
   `plan`) plus the evidence for it, and a blocker is never permission to end an unresolved theorem. Put any
   retained counterexamples or ruled-out proof shapes under the exact heading `Negative evidence:` so the next
   checkpoint can preserve them without treating arbitrary prose as proof authority.
2. Do not jump to later theorems in the file, even if they also contain `sorry`.
3. Treat previous failed attempts as negative guidance:
   - do not blindly repeat the same proof shape
   - explain when a new attempt differs materially from earlier failures
4. Helper decomposition is a standard, first-class proving strategy, not a last resort: introduce helper lemmas, local intermediate facts, or small private supporting declarations whenever they make the assigned declaration easier to prove. After about two failed direct attempts or repeated bounded verification timeouts, call `lean_extract_have(action="inventory")` when substantial local proofs already exist, then extract one cohesive block or a bounded batch (at most four) with semantic `helper_names`; otherwise use `lean_decompose_helpers` to derive new subgoals. Prefer candidates with the largest reported context reduction, but keep logically related phases together and avoid splitting tiny facts. The extraction is one transaction: LeanFlow recovers exact contexts, verifies every helper and call site independently, and commits only the combined checked rewrite. Do not replay another unchanged broad check. When declaration size causes the timeout, prefer cohesive top-level helpers over more local `have` blocks so LeanProbe can cache and verify the phases independently. A newly inserted helper's `sorry` is normal work-in-progress during the turn; the sorry-free requirement applies at final acceptance of the assigned declaration, not to intermediate states. Keep every helper scoped to the assigned theorem's needs. When a helper does not use file-level section variables or instances, wrap it with the appropriate `omit ... in` or place it in a narrower section so verified helper banking does not accumulate predictable `unusedSectionVars` warning growth.
5. Queue edit scope protects declarations that already existed when this theorem was assigned. Do not edit, reorder, rename, delete, or solve pre-existing non-assigned declarations or future queue items, but adding and iterating on new helper declarations for this theorem is allowed.
6. Preserve existing theorem, lemma, and example statements exactly unless the user explicitly requested a refactor. This applies to the assigned declaration and to helper declarations after you create them; change proof bodies, not established statements.
7. After each meaningful edit, rely on the managed LeanProbe gate before making another large change. A hard Lean error restores the exact pre-edit source; a timeout-only result preserves the source and triggers the decomposition route rather than an unchanged replay.
8. For a managed file-scoped assigned theorem, the preferred edit path is `patch` or `write_file`; the queue manager runs LeanFlow's cached incremental queue-step verifier after successful edits and falls back to Lake only when the incremental backend is unavailable, crashes, or cannot rebuild its cache. A bounded target-check timeout rejects that attempt without starting a duplicate full-file check. Use `apply_verified_patch` only when you specifically need its atomic checkpoint plus verification payload; it uses the same cached incremental verifier by default. Request `check_mode=file_exact` only for an explicit canonical Lake check.
9. When ordinary diagnostics are not enough, call `lean_incremental_check(action=feedback, include_tactics=true)` for the assigned declaration. Use returned `tactics[*].goals`, `tactics[*].proof_state`, file-global message positions, and `feedback_lean` comments as the repair context.
10. Use LeanFlow's Lean tools for every managed queue verification so the manager can classify the assigned declaration and reuse the warm cache. Direct terminal `lean`, `lake env lean`, and `lake build` checks are rejected during theorem turns; report a broken incremental backend so the manager can run the canonical fallback. For a managed companion file, use `lean_incremental_check(action=check_file)`.
11. Do not treat `lake build`, `grep`, `head`, or truncated output as proof that the assigned theorem is clean.
12. If the declaration becomes clean, stop and hand control back to the manager rather than continuing to the next theorem on your own.
13. Treat runtime step-budget warnings as real control signals. With only a few API steps left, prefer one concrete verification-backed edit or a concise blocker report over starting a broad new strategy. A decompose-and-insert helper batch counts as one meaningful edit, not several; switching strategy to decomposition is budgeted work, never budget waste.
14. Do not confuse a knowledge prior with Lean evidence. “This seems too hard,” “I do not know the library lemma,” or an advisor/model's initial doubt is a reason to inspect, search, derive, decompose, and test—not to surrender or request another plan. Keep climbing the persistence ladder until a concrete kernel diagnostic, statement mismatch, resource failure, or genuinely exhausted set of materially distinct routes supplies evidence.
15. When a handoff contains a concrete next edit, construction, invariant, or verified route, attempt that exact step before requesting another route. You may abandon it only after recording the precise Lean rejection or new mathematical evidence that makes it unsuitable. Preserve target-local checked facts and the strongest verified route across rewrites; do not delete them merely to return to a familiar proof shape.
16. Treat a decomposer- or planner-generated helper differently from an established source theorem when
    concrete evidence shows its statement is false. If a boundary counterexample works, or the proof
    requires an indispensable parent hypothesis that the generated helper omitted, stop trying to invent
    that premise. Preserve the counterexample or missing-hypothesis evidence and request the `negate` route
    immediately. The manager's kernel-backed false-decomposition cleanup owns invalidating the helper,
    restoring its parent, and replanning the affected subtree.
17. When elaboration or arithmetic automation fails around a multiline `∑`, `∏`, or other big-operator expression, first parenthesize the complete summand or product body and check that smaller expression independently. Do this before changing the mathematical route: Lean's parser can otherwise associate trailing arithmetic outside the binder and make a correct `ring`/`nlinarith` step appear false.

## Plan-State Freshness

1. Start every assignment from the deterministic queue handoff and refresh the assigned declaration with `lean_inspect` or current Lean diagnostics. Those sources and the kernel gate are inventory and declaration truth. If the handoff says that the unchanged declaration already exhausted a bounded verification check without diagnostics, use that current evidence and the decomposition route instead of repeating the timed-out refresh.
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
3. Use `lean_multi_attempt` when you have 2-6 specific short local tactic candidates at one proof location. This is especially useful before patching small tactic ideas because REPL power mode may screen them quickly. Treat only `verified_attempts`/`target_verified=true` as a closing result; an empty-goal backend probe is provisional until LeanFlow reconstructs and exact-checks the complete assigned declaration.
4. If search is exhausted or the blocker still looks automation-suited, call `lean_proof_context` before deeper automation search.
5. Use `lean_auto_search` when theorem-local automation search is justified. This wrapper is an optional accelerator, not a mandatory step.
6. Do not send theorem-sized proof blocks, declaration headers, multi-line `have ... := by` proof blocks, or candidates containing `sorry` to `lean_multi_attempt`.
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
- a generated helper is false without a parent hypothesis it omitted; request `negate` with the smallest
  concrete counterexample instead of fabricating the missing premise
- the surrounding file state prevents isolated progress on the assigned declaration

When a proof shape fails, summarize the blocker in terms the manager can store as failed-attempt evidence,
request a distinct route (`decompose`, `plan`, or `negate`), and keep the assignment active. A blocker is
never permission to end an unresolved theorem.

If the API step budget is exhausted before you finish, the runner records the exact failed proof body and diagnostics in workflow state, the proof graph, and the dead-branch audit trail. Production Lean source stays on the best verified live state or its safe baseline; failed declarations are not copied into the source as comments. That is not success and does not skip the theorem: the next queue cycle resumes the same item with the failed-attempt context and must try a materially different route.
