---
name: lean-proof-loop
description: Run the standard EPFLemma Lean proof loop: inspect, diagnose, edit minimally, rebuild, and verify.
---

# Lean Proof Loop

Use this skill for guided proof work in Lean.

## Procedure

1. Identify the active Lean file and target declaration before editing.
2. Query diagnostics and goals first. Do not start by guessing a patch.
3. Build a concrete todo list from real blockers in the requested scope: declarations with `sorry`, declarations with Lean errors, then declarations with warnings that still need cleanup.
4. When a queue manager assigns one declaration, treat that declaration as the entire task until it is solved or a concrete blocker is recorded.
5. If failed attempts for the current declaration are provided, use them as negative guidance and avoid repeating the same proof shape without a clear reason.
6. Work through blockers one declaration at a time. Do not jump around or declare the file done after fixing only the first theorem.
7. Make the smallest proof change that addresses the current blocker.
8. Re-run diagnostics or a build after each meaningful edit.
9. Treat the workflow as verified only when the requested scope is clean:
   - if the user gave one Lean file, that file has no remaining `sorry`, errors, open goals, or warnings
   - if the user did not give a file, the project has no remaining `sorry`, errors, open goals, or warnings outside dependencies
   - the explicit verification build succeeds

## Guardrails

- Prefer local proof repair over broad refactors.
- Use `lean-lsp` diagnostics/goals for most iterations.
- Avoid repeated `lake env lean <file>` loops. Prefer a focused `lake build <Module>` when the file is close to clean, and reserve full-project `lake build` for milestone verification.
- Keep the active file pinned to the requested workflow target. Do not drift to unrelated declarations discovered later in chat history or helper files.
- When a queue manager hands you a specific declaration, stop after that declaration is resolved or blocked and hand control back instead of continuing to the next theorem automatically.
- Do not remove important theorem structure just to silence errors.
- Do not stop just because the current theorem looks clean if later theorems in the same requested file still fail.
- Do not stop just because `sorry` disappeared if errors or warnings still remain.
- If proof goals move or split, describe the new state before continuing.
- If the session is compacted or resumed, trust the persisted handoff plus current Lean state over memory.
