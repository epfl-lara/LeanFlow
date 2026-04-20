---
name: lean-proof-loop
description: Run the standard OpenGauss Lean proof loop: inspect, diagnose, edit minimally, rebuild, and verify.
---

# Lean Proof Loop

Use this skill for guided proof work in Lean.

## Procedure

1. Identify the active Lean file and target declaration before editing.
2. Query diagnostics and goals first. Do not start by guessing a patch.
3. Make the smallest proof change that addresses the current blocker.
4. Re-run diagnostics or a build after each meaningful edit.
5. Treat the proof as verified only when there are no remaining goals, no blocking diagnostics, the explicit build succeeds, and the project has no remaining `sorry` outside dependencies.

## Guardrails

- Prefer local proof repair over broad refactors.
- Do not remove important theorem structure just to silence errors.
- Do not stop just because the active theorem looks clean if the wider project still has build errors or `sorry`.
- If proof goals move or split, describe the new state before continuing.
- If the session is compacted or resumed, trust the persisted handoff plus current Lean state over memory.
