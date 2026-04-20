---
name: lean-diagnostics
description: Focus on Lean diagnostics, goal inspection, blocker explanation, and verification state.
---

# Lean Diagnostics

Use this skill when the main job is understanding the current Lean state rather than making large edits.

## Procedure

1. Inspect diagnostics for the active file.
2. Inspect goals for the current declaration when available.
3. Distinguish between hard blockers, open proof goals, and non-blocking warnings.
4. Summarize the current verification state in concrete terms, including whether the wider project still has build failures or `sorry`.

## Output

- Active file
- Target declaration
- Blocking diagnostics
- Open goals
- Project-wide remaining `sorry` or build blockers
- Whether the session is verified, in progress, or blocked
