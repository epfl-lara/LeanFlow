---
name: lean-formalization
description: Translate mathematical intent into Lean declarations and proofs in small verifiable steps.
---

# Lean Formalization

Use this skill for `formalize`, `autoformalize`, and declaration drafting work.

## Procedure

1. Extract the mathematical target and required objects.
2. Choose a Lean representation that matches the local codebase style.
3. Draft declarations, imports, and signatures before attempting the full proof.
4. Verify each step with diagnostics and a build instead of writing a large speculative block.
5. If the formalization needs multiple lemmas, leave a clear dependency order and drive the project toward zero build errors and zero `sorry`.

## Guardrails

- Keep names readable and codebase-consistent.
- Prefer explicit intermediate lemmas over brittle proof scripts.
- Do not declare success while other Lean files in the project still fail to build or still contain `sorry`.
- Surface missing assumptions or ambiguous math instead of hiding them.
