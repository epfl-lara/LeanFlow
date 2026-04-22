---
name: lean-refactor-golf
description: Native refactor/golf entry. Preserve theorem meaning, use structured verification, and escalate to `proof-golfer` only when the router calls for it.
---

# Native Lean Refactor / Golf

Primary specs:

- `epflemma_specs/workflows/refactor.md`
- `epflemma_specs/workflows/golf.md`

## Tool Order

1. `lean_inspect`
2. `lean_search` for nearby proof shapes
3. minimal cleanup or simplification edit
4. `lean_verify`
5. `lean_worker_dispatch` with `proof-golfer` only when the route explicitly recommends it

## Guardrails

- Do not trade maintainability for tiny cosmetic wins unless the user explicitly wants golfing.
- Stop if the shorter proof becomes materially harder to understand or debug.
- Do not change theorem statements or public interfaces unless the task explicitly requires it.
