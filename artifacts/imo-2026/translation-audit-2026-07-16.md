# IMO 2026 public Lean translation audit — 2026-07-16

## Outcome

The authoritative English statements are live at <https://www.imo-official.org/problems/2026/> and preserved locally in `official-problems-2026-07-16.html` with SHA-256 `198784ca80ae7b27041f295f4a24cd3371fdcef0d26bdf84e1ec5274206482d0`. A same-day re-fetch produced the same digest.

One exact public Lean 4 translation repository was found: [`jsm28/IMOLean`](https://github.com/jsm28/IMOLean), commit [`3fc62b66ec02aa8446f3a1461f540ed93a74caa3`](https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3), authored 2026-07-16T05:34:02Z. All six files parse with that repository's pinned environment. P1 is not source-faithful; P2–P6 pass an initial statement comparison, although P3–P5 leave the requested answer itself as `sorry`.

## What was searched

- GitHub public commit and code search using `IMO 2026`, exact official phrases, problem names, and expected Lean filenames; candidate repositories were opened or cloned before evaluation.
- [`leanprover-community/mathlib4`](https://github.com/leanprover-community/mathlib4) at `3b5afa97c31c95c69273cc3724eb50c78399405c`.
- [`openai/miniF2F`](https://github.com/openai/miniF2F) at `4e433ff5cadff23f9911a2bb5bbab2d351ce5554`.
- [`yangky11/miniF2F-lean4`](https://github.com/yangky11/miniF2F-lean4) at `5746b7d6c47855ce1294bed87329618ff7f1bc31`.
- [`internlm/Lean-Workbook`](https://huggingface.co/datasets/internlm/Lean-Workbook), Hub revision `2e066e310b2c6d2c27616927ae131f82901c8f1c`, last modified 2024-10-09, plus [`therewillbecode/lean-workbook`](https://github.com/therewillbecode/lean-workbook) at `e0b870f65a3ad6e6c6aeafb738bc53748c0c9782`.
- General public web search for exact statement phrases and same-day solution/transcription material.

Exact-token local searches for `IMO 2026`, `Confucius`, `Liu Bang`, `Xiang Yu`, `Shan-Yu`, and `Mulan` found no 2026 translation in the pinned mathlib, Lean Workbook Git repository, or either miniF2F repository. The Hugging Face Dataset Viewer returned zero rows for the distinctive token `Confucius`; its 2024 last-modified date also predates these problems.

The same-day Evan Chen commit [`dfb6de7`](https://github.com/vEnhance/web.evanchen.cc/commit/dfb6de7a588a72cb98c4dbfed56e515454b71f3a) only added links to draft solutions; the linked PDF/TeX artifacts were not present at inspection time. [`SignalPilot-Labs/AutoFyn@faca2e8`](https://github.com/SignalPilot-Labs/AutoFyn/commit/faca2e8e2eeb20a46227287a320a14a8fff6a1da) contains natural-language proof artifacts, not Lean statement translations. Neither was used as statement authority.

This is a time-bounded public search, not proof that no unindexed or newly published repository exists.

## Compilation check

Environment:

```text
lean-toolchain: leanprover/lean4:v4.32.0-rc1
mathlib: 3b5afa97c31c95c69273cc3724eb50c78399405c
IMOLean: 3fc62b66ec02aa8446f3a1461f540ed93a74caa3
```

Commands run from a clean shallow clone after `lake exe cache get`:

```sh
lake env lean IMO/IMO2026P1.lean
lake env lean IMO/IMO2026P2.lean
lake env lean IMO/IMO2026P3.lean
lake env lean IMO/IMO2026P4.lean
lake env lean IMO/IMO2026P5.lean
lake env lean IMO/IMO2026P6.lean
```

Every command exited 0. Each file emitted only expected `declaration uses 'sorry'` warnings. This verifies elaboration, not proof completeness or statement fidelity.

## Fidelity findings

| Problem | Result | Evidence |
| --- | --- | --- |
| P1 | **Reject unchanged.** | Official move replaces the pair by `gcd(x,y)` and `lcm(x,y)/gcd(x,y)`. [`IMO2026P1.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P1.lean) omits the division. When `x ∣ y`, its relation admits the unchanged board as a valid move, so the claimed termination theorem is false. |
| P2 | Initial pass. | [`IMO2026P2.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P2.lean) matches the rendered official midpoint, four strict-interior, three angle, circumcentre, and distance conditions. |
| P3 | Initial pass; answer incomplete. | [`IMO2026P3.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P3.lean) represents the finite marking/claiming game and a worst-case Liu strategy, but defines the requested value as `answer := sorry`. |
| P4 | Initial pass; answer incomplete. | [`IMO2026P4.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P4.lean) represents legal cuts, arbitrary retained halves, and finite winning strategies, but defines the winning-angle set as `answer := sorry`. |
| P5 | Initial pass; answer incomplete. | [`IMO2026P5.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P5.lean) matches the positive-real radical inequalities, but defines the function set as `answer := sorry`. |
| P6 | Initial pass. | [`IMO2026P6.lean`](https://github.com/jsm28/IMOLean/blob/3fc62b66ec02aa8446f3a1461f540ed93a74caa3/IMO/IMO2026P6.lean) uses an equivalent zero-based indexing and explicitly represents leastness, all prior gcd conditions, and positive additive periodicity. |

See `formalization/Blueprint.md` for the problem-by-problem completion plan and `formalization/IMO2026.lean` for the corrected P1 target begun locally.
