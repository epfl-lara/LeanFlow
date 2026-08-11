---
name: lean-diagnostics
description: Native diagnostics/review/doctor entry. Use structured capability, inspection, and verification state instead of ad hoc summaries.
---

# Native Lean Diagnostics

Primary specs:

- `leanflow_specs/workflows/review.md`
- `leanflow_specs/workflows/doctor.md`
- `leanflow_specs/workflows/search.md`

## Tool Order

1. `lean_capabilities`
2. `lean_inspect`
3. `lean_sorries`
4. `lean_axioms` when axiom risk is relevant
5. `lean_verify` only when an explicit verification check is needed

## Output

- Active file
- Target declaration
- Blocking diagnostics
- Open goals
- Capability degradations
- Route action when present
- Project-wide remaining `sorry` or build blockers
- Whether the session is verified, in progress, or blocked
