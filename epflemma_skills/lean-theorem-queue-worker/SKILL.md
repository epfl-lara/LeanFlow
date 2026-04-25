---
name: lean-theorem-queue-worker
description: Native single-declaration worker entry. Obey the queue handoff exactly, use the shared Lean tools, and escalate through specialist workers only when the route calls for it.
---

# Native Lean Queue Worker

Use this skill when an external workflow manager has already chosen the next declaration to solve.

Primary specs:

- `epflemma_specs/workflows/prove.md`
- `epflemma_specs/workflows/search.md`
- `epflemma_specs/workers/proof-repair.md`
- `epflemma_specs/workers/proof-golfer.md`
- `epflemma_specs/workers/axiom-eliminator.md`
- `epflemma_specs/workers/sorry-filler-deep.md`

## Expected Manager Handoff

1. active Lean file
2. target declaration name and, when possible, line number
3. why this declaration is still pending
4. the last `N` failed attempts for this declaration and why they failed
5. search hints, blocker signature, verification gate, and recommended worker when available

## Worker Contract

1. Focus only on the assigned declaration until it is solved or a concrete blocker is proven.
2. Do not jump to later theorems in the file, even if they also contain `sorry`.
3. Treat previous failed attempts as negative guidance:
   - do not blindly repeat the same proof shape
   - explain when a new attempt differs materially from earlier failures
4. You may introduce helper lemmas, local intermediate facts, or small private supporting declarations when they make the assigned declaration easier to prove. This is optional, not required; use it when it genuinely breaks a hard proof into smaller verified steps, and keep every helper scoped to the assigned theorem's needs.
5. After each meaningful edit, re-check the assigned declaration with `lean_inspect` before making another large change.
6. For a managed file-scoped assigned theorem, the preferred edit path is `patch` or `write_file`; the queue manager runs the canonical file verification gate after successful edits. Use `apply_verified_patch(check_mode=file_exact)` only when you specifically need its atomic checkpoint plus verification payload.
7. Do not treat `lake build`, `grep`, `head`, or truncated output as proof that the assigned theorem is clean.
8. If the declaration becomes clean, stop and hand control back to the manager rather than continuing to the next theorem on your own.
9. Treat runtime step-budget warnings as real control signals. With only a few API steps left, prefer one concrete verification-backed edit or a concise blocker report over starting a broad new strategy.

## Queue Hygiene

1. If an earlier unresolved declaration is producing syntax, elaboration, or goal-state errors that prevent useful diagnostics for the current queue item, do not spend the turn solving that earlier declaration unless the manager assigned it to you.
2. Preserve the earlier declaration's current proof work before unblocking the file: comment the broken proof state or failed attempt in place, then close that earlier declaration's active proof body with a minimal `sorry` so the current assigned declaration can be inspected.
3. Never change, weaken, rename, move, or delete the earlier declaration statement while doing this. Only edit the proof body.
4. Treat this as a temporary queue-unblocking move, not success. Mention the preserved commented attempt and the inserted `sorry` in the handoff or failed-attempt summary so a later queue pass can resume from it.
5. Do not use this pattern to finish the assigned declaration. If the assigned declaration still needs `sorry`, report a blocker instead of claiming success.

## Search Strategy

1. Search the local project first with `lean_search mode=local`.
2. Search Mathlib next with `lean_search mode=semantic|type-pattern|natural-language` when the needed fact looks standard.
3. If search is exhausted or the blocker still looks automation-suited, call `lean_proof_context` before deeper automation.
4. Use `lean_auto_probe` first, then `lean_auto_search`, then `lean_auto_try` for one concrete candidate when theorem-local automation is justified.
5. Use `lean_multi_attempt` only when you have 2-6 specific short local tactic candidates at one proof location.
6. Do not send theorem-sized proof blocks, declaration headers, or candidates containing `sorry` to `lean_multi_attempt`.
7. If you have one full candidate proof, prefer `lean_auto_try` before editing; then use the managed edit path unless the atomic `apply_verified_patch` payload is specifically useful.
8. Invent helper lemmas or sublemmas when the direct proof is too large or repeated direct attempts fail. Prefer small statements that are easy to verify and directly feed the assigned declaration.
9. If repeated focused attempts fail while the theorem still looks solvable, call `lean_reasoning_help` with the statement, diagnostics, current attempt, and failed-attempt summary.
10. If `lean_reasoning_help` reports that the advisor is unavailable or returned no answer, continue with the strongest concrete edit, verification, worker dispatch, or blocker report you have.
11. If repeated searches keep returning no useful results, stop searching in that turn and switch to the strongest concrete edit, verification, worker dispatch, or blocker report you have.
12. If `lean_auto_try` reports a project-level backend/setup error such as an unsupported `set_option`, do not treat that as proof feedback for the assigned theorem and do not edit unrelated examples or solved declarations. Continue with the managed edit path or report the setup issue as a file-level blocker.

## Success Condition

The assigned declaration is successful only when:

- its proof has no `sorry`
- diagnostics for that declaration are clean
- there are no remaining goals for that declaration
- the attempted fix does not introduce a new local blocker around it
- and the manager-requested file check succeeds, either through the automatic post-edit gate or an explicit `lean_verify(mode=file_exact)`
- and any recommended specialist worker route has either been used or explicitly ruled out

## Failure Condition

Stop and report a blocker when:

- the same proof approach keeps failing for a known reason
- the declaration appears to require a missing lemma or changed statement
- the surrounding file state prevents isolated progress on the assigned declaration

When stopping with failure, summarize the blocker in terms the manager can store as the next failed attempt context.

If the API step budget is exhausted before you finish, the runner records the current theorem as a failed attempt and, when it has the original untruncated `sorry` slice, comments the current failed declaration above the theorem and restores that declaration to the safe baseline `sorry` body. That is not success and does not skip the theorem; the next queue cycle resumes the same item with the failed-attempt context.
