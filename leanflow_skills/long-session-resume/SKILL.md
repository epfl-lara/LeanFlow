---
name: long-session-resume
description: Resume compacted or checkpointed LeanFlow sessions without repeating work.
---

# Long Session Resume

Use this skill when a managed workflow was compacted, resumed, rolled back, or checkpoint-restored.

## Procedure

1. Read the latest persisted checkpoint or snapshot summary.
2. Compare it against the current Lean file, diagnostics, goals, and build state.
3. Identify what is already complete, what changed since the handoff, and the next concrete action.
4. Continue from the current verified state instead of replaying old work blindly.

## Guardrails

- Treat persisted handoffs as authoritative summaries, not perfect truth.
- Prefer the live Lean state when it conflicts with old transcript details.
- Record a fresh checkpoint after a meaningful resumed milestone.
