# IMO 2026 release capture

Retrieved at `2026-07-16T08:09:07Z` from the public IMO archive while the 67th IMO was in progress. `official-problems-2026-07-16.html` preserves the authoritative response byte-for-byte.

- Official questions: <https://www.imo-official.org/problems/2026/>
- Official IMO status/news: <https://www.imo-official.org/>
- Official host/organizer site: <https://www.imo2026.com/>
- Saved response SHA-256: `198784ca80ae7b27041f295f4a24cd3371fdcef0d26bdf84e1ec5274206482d0`

`original-questions.md` is a readable transcription. Formulae were transcribed from the archive's rendered MathJax SVGs and cross-checked against the saved source; the HTML capture remains the primary preservation artifact.

The saved response was fetched again on 2026-07-16 and had the same SHA-256. A full-page browser rendering was also inspected. That inspection found and corrected a P2 mistranscription in the readable Markdown; the preserved HTML itself was unchanged.

Last rechecked: `2026-07-23T22:01:22Z`. The official archive continued to expose all six problems. The host/organizer site continued to identify the Chinese Mathematical Society and Shanghai Municipal Education Commission as organizers, but did not publish a separate question document. No material statement change was observed against the saved 2026-07-23 archive capture; the live response's MathJax-generated markup is not byte-stable between requests.

The official IMO archive is the sole authority. The host site is an organizer cross-check only; at retrieval it published event news/programme, not a separate question document. A same-day public transcription of Problem 3 agrees on the length-one stick, both players' at-most-`n` marks, and the target constant `c`, but it is not treated as authority or as a solution reference: <https://bbs.wenxuecity.com/znjy/7942803.html>.

`formalization/` is now a self-contained, pinned Lean project containing all six canonical local modules. It corrects the baseline P1 transition, documents the P2 and P6 bridges, and intentionally retains the answer-definition holes for the three *determine* problems P3-P5. The project builds with exactly nine expected `sorry` occurrences and no other trust escape hatch found by the local scan.

- [`formalization/Blueprint.md`](formalization/Blueprint.md) records the source-fidelity checklist, task modes, and pre-proof gate.
- [`GPT56_PRO_LOCAL_REVIEW_PROMPT.md`](GPT56_PRO_LOCAL_REVIEW_PROMPT.md) is the independent review prompt for the corrected local project.
- [`gpt56-pro-local-review.md`](gpt56-pro-local-review.md) is the completed independent review. It gives a **GO** for all six modules under their declared modes, with P3-P5 explicitly treated as two-stage answer-plus-proof tasks.
- [`translation-audit-2026-07-16.md`](translation-audit-2026-07-16.md) and [`gpt56-pro-review.md`](gpt56-pro-review.md) preserve the earlier audit of the uncorrected upstream snapshot.
