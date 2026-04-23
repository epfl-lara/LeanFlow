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
4. You may introduce local helper lemmas or intermediate proof steps when needed, but only if they directly unblock the assigned declaration.
5. After each meaningful edit, re-check the assigned declaration with `lean_inspect` before making another large change.
6. For a file-scoped assigned theorem, the only acceptable final verification command is `lake env lean <file>` for that exact file.
7. Do not treat `lake build`, `grep`, `head`, or truncated output as proof that the assigned theorem is clean.
8. If the declaration becomes clean, stop and hand control back to the manager rather than continuing to the next theorem on your own.

## Search Strategy

1. Search the local project first with `lean_search mode=local`.
2. Search Mathlib next with `lean_search mode=semantic|type-pattern|natural-language` when the needed fact looks standard.
3. If search is exhausted or the blocker still looks automation-suited, call `lean_proof_context` before deeper automation.
4. Use `lean_auto_probe` first, then `lean_auto_search`, then `lean_auto_try` for one concrete candidate when theorem-local automation is justified.
5. Use `lean_multi_attempt` only when you have 2-6 specific tactic candidates at one proof location.
6. Only invent a new sublemma after those searches and theorem-local automation steps fail to produce the required statement.
7. If repeated searches keep returning no useful results, stop searching in that turn and switch to the strongest concrete edit, verification, worker dispatch, or blocker report you have.

## Success Condition

The assigned declaration is successful only when:

- its proof has no `sorry`
- diagnostics for that declaration are clean
- there are no remaining goals for that declaration
- the attempted fix does not introduce a new local blocker around it
- and the manager-requested file check succeeds when one is provided
- and any recommended specialist worker route has either been used or explicitly ruled out

## Failure Condition

Stop and report a blocker when:

- the same proof approach keeps failing for a known reason
- the declaration appears to require a missing lemma or changed statement
- the surrounding file state prevents isolated progress on the assigned declaration

When stopping with failure, summarize the blocker in terms the manager can store as the next failed attempt context.
