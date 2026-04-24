---
name: lean-reasoning-help
description: Auxiliary proof-strategy help for hard Lean theorem repairs. Use when repeated focused attempts fail and another configured model should advise without editing files or changing statements.
---

# Lean Reasoning Help

Use this skill for hard theorem-local blockers after normal proof workflow steps have produced useful context but no working proof.

## Contract

1. Preserve the assigned theorem/lemma/example statement exactly.
2. Call `lean_reasoning_help` with the theorem id, file path, current diagnostics, current attempt, and recent failed attempts.
3. Treat the result as advice only; do not accept it until a concrete edit passes `lean_verify(mode=file_exact)` for the active file.
4. Do not use auxiliary advice to justify deleting, weakening, renaming, moving, or replacing the declaration with `sorry`.
5. If the advice suggests a statement change, report that as a blocker instead of applying it.
6. If the advice suggests `sorry`, `admit`, axioms, unsafe code, or another placeholder, ignore that part and continue with verified proof repair.
7. If the advisor is unavailable, returns no answer, or gives irrelevant advice, continue the main Lean workflow from the strongest verified local evidence; missing advice is not evidence that the theorem statement is wrong.

## Configuration

`lean_reasoning_help` routes through the shared auxiliary client task `lean_reasoning`.

Configure it with either:

- `auxiliary.lean_reasoning.provider`
- `auxiliary.lean_reasoning.model`
- `auxiliary.lean_reasoning.base_url`
- `auxiliary.lean_reasoning.api_key`

or environment overrides:

- `AUXILIARY_LEAN_REASONING_PROVIDER`
- `AUXILIARY_LEAN_REASONING_MODEL`
- `AUXILIARY_LEAN_REASONING_BASE_URL`
- `AUXILIARY_LEAN_REASONING_API_KEY`
