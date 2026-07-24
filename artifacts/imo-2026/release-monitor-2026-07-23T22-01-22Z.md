# IMO 2026 release-monitor check

Retrieved at `2026-07-23T22:01:22Z`.

## Official sources checked

- [IMO 2026 problems archive](https://www.imo-official.org/problems/2026/) — the authoritative English archive remains publicly available and lists exactly Problems 1–6. The live HTML response SHA-256 was `7f9af1d0faaaa3c4d7dd3c27c6f35ae347fa39ba4c39f15e91a1184d1a78c6da`; it is not compared byte-for-byte because the archive's generated MathJax markup is known to vary between retrievals.
- [IMO 2026 edition page](https://www.imo-official.org/editions/2026/) — confirms the 67th IMO in Shanghai on July 10–21 and links the archive as the edition's problems source.
- [Official Shanghai host/organizer site](https://www.imo2026.com/index.htm) — identifies the Chinese Mathematical Society and Shanghai Municipal Education Commission, but its public pages still contain no separate question paper.
- [IMO annual regulations](https://www.imo-official.org/assets/documents/imo-annual-regulations.pdf) — organizer/event-context cross-check only; not a problem-statement source.

The original authoritative response remains preserved in `official-problems-2026-07-16.html` and `official-problems-2026-07-23.html`; `original-questions.md` remains the readable derivative. Browser review of the live archive found no material change to the six statements, so no replacement capture was created.

## Aligned secondary sources

- [IMOLean commit `3fc62b6`](https://github.com/jsm28/IMOLean/commit/3fc62b66ec02aa8446f3a1461f540ed93a74caa3) — a public Lean 4 formalization cross-check that adds six 2026 modules. It is not an authority, and the local P1 transition correction remains necessary.
- [Mathematics Stack Exchange P3 discussion](https://math.stackexchange.com/questions/5143966/imo-2026-problem-3-liu-bang-and-xiang-yu-stick-cutting-game) — independent public transcription/solution discussion for P3 only; cross-check, not authority.

No newer official national-olympiad statement publication or established formalization repository with a distinct, independently authoritative 2026 problem set was found in this bounded check.

## LeanFlow validation

Ran `lake build` in `formalization/` with its pinned Lean/Mathlib environment: **passed** (8646 jobs).

| Measure | Count | Status |
| --- | ---: | --- |
| Released exercises | 6 | Public in the official archive |
| Faithful Lean statement modules | 6 | Source-fidelity review remains approved in `formalization/Blueprint.md` |
| Validated modules | 6 | Included in successful project build |
| Translation blockers | 0 | No statement refresh required |
| Open proof/answer holes | 9 | P1/P2/P6 proofs; P3/P4/P5 answer plus proof |

This is a release-monitor and statement-validation result, not a claim that any IMO problem has been proved in Lean.
