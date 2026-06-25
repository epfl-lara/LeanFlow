---
name: lean-refactor-golf
description: Native refactor/golf routing entry. Load the linked workflow specs as the contract, preserve theorem meaning, and keep optimization inside the direct Lean tool surface.
---

# Native Lean Refactor / Golf

This skill is the routing layer for Lean proof refactoring and golfing. Treat the linked workflow specs as the operational contract for tool order, verification gates, and stop conditions.

Primary specs:

- `leanflow_specs/workflows/refactor.md`
- `leanflow_specs/workflows/golf.md`

## Routing Rules

- Use `refactor.md` for structure-preserving proof cleanup, helper layout, and reusable proof-shape improvements.
- Use `golf.md` for already-compiling proofs that need local directness, brevity, clarity, or lighter proof search burden.
- Preserve theorem meaning, theorem statements, and public interfaces unless the task explicitly asks for a semantic refactor.
