---
name: lean-theorem-queue-worker
description: Solve one assigned Lean declaration at a time using manager-provided target scope and failed-attempt history.
---

# Lean Theorem Queue Worker

Use this skill when an external workflow manager has already chosen the next declaration to solve.

## Expected Manager Handoff

The manager should provide all of the following in the prompt:

1. active Lean file
2. target declaration name and, when possible, line number
3. why this declaration is still pending
4. the last `N` failed attempts for this declaration and why they failed
5. whether the worker should keep editing this declaration or only diagnose the blocker

## Worker Contract

1. Focus only on the assigned declaration until it is solved or a concrete blocker is proven.
2. Do not jump to later theorems in the file, even if they also contain `sorry`.
3. Treat previous failed attempts as negative guidance:
   - do not blindly repeat the same proof shape
   - explain when a new attempt differs materially from earlier failures
4. You may introduce local helper lemmas or intermediate proof steps when needed, but only if they directly unblock the assigned declaration.
5. After each meaningful edit, re-check the assigned declaration with Lean diagnostics/goals before making another large change.
6. For a file-scoped assigned theorem, the only acceptable final verification command is `lake env lean <file>` for that exact file.
7. Do not treat `lake build`, `grep`, `head`, or truncated output as proof that the assigned theorem is clean.
8. If the declaration becomes clean, stop and hand control back to the manager rather than continuing to the next theorem on your own.

## Search Strategy

1. Search the local project first for nearby helpers and style.
2. Search Mathlib next when the needed fact looks standard.
3. Only invent a new sublemma after those searches fail to produce the required statement.

## Success Condition

The assigned declaration is successful only when:

- its proof has no `sorry`
- diagnostics for that declaration are clean
- there are no remaining goals for that declaration
- the attempted fix does not introduce a new local blocker around it
- and the manager-requested file check succeeds when one is provided

## Failure Condition

Stop and report a blocker when:

- the same proof approach keeps failing for a known reason
- the declaration appears to require a missing lemma or changed statement
- the surrounding file state prevents isolated progress on the assigned declaration

When stopping with failure, summarize the blocker in terms the manager can store as the next failed attempt context.
