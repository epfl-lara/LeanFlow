# IMO 2026 LeanFlow statement project

This directory is the canonical local statement set to review before proof work begins.

- Official source: <https://www.imo-official.org/problems/2026/>
- Baseline formalization: [jsm28/IMOLean@3fc62b6](https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3)
- Lean toolchain: `leanprover/lean4:v4.32.0-rc1`
- Mathlib: `3b5afa97c31c95c69273cc3724eb50c78399405c`

The six modules preserve Joseph Myers's Apache-2.0 attribution, include the upstream [`LICENSE`](LICENSE), and retain IMOLean's standard-mode convention. P1 includes the missing `/ gcd` correction from [PR #1](https://github.com/jsm28/IMOLean/pull/1). P2 includes the representation note from [PR #2](https://github.com/jsm28/IMOLean/pull/2). P6 includes the indexing bridge and removes the unrelated real-number notation from [PR #3](https://github.com/jsm28/IMOLean/pull/3).

## Intentional proof tasks

- P1, P2, and P6 each retain one theorem proof as `by sorry`.
- P3, P4, and P5 each retain `answer := sorry` plus the theorem proof `by sorry`. Determining that answer is part of the original problem, not a missing translation definition.
- The independent local review is now approved for all six modules under these declared task modes; see [`../gpt56-pro-local-review.md`](../gpt56-pro-local-review.md). No proof work has started yet.

## Verification

```sh
lake update
lake exe cache get
lake build
rg "\\b(sorry|admit)\\b" --glob '*.lean'
```
