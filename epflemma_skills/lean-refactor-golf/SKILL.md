---
name: lean-refactor-golf
description: Simplify, refactor, or golf Lean proofs while preserving meaning and verifiability.
---

# Lean Refactor Golf

Use this skill for `refactor`, `golf`, and proof cleanup tasks.

## Procedure

1. Confirm the proof currently builds or identify the exact failure first.
2. Preserve theorem meaning and public interfaces.
3. Reduce duplication and unnecessary tactic noise before chasing terseness.
4. Rebuild after each simplification pass.

## Guardrails

- Do not trade maintainability for tiny cosmetic wins unless the user explicitly wants golfing.
- Stop if the shorter proof becomes materially harder to understand or debug.
