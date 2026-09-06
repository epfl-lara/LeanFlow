# Proposed Beck–Fiala input

Prepared for selection and environment checks only. No proof campaign has started.

The theorem declaration is copied verbatim from formal-conjectures commit 8323e878b83fcd7f4a448256069352a265460d75. Its proof remains `sorry`. The dataset category attribute and unused utility imports are omitted in this standalone project. The namespace, parameters, hypotheses, and conclusion are preserved.

`RUN_CONFIG.json` records the current configuration: four parallel provers,
200 calls per prover/negation pass, 50 per planning/review/research stage, 2,000
total calls, eight hours of active campaign time, and 20 minutes per provider
request or independent verification. Model selection is `openai-codex` with
`gpt-6-astra` at `xhigh` for all model roles.

The same limits are available in this project's local flag profile
`beck-fiala-research`. Select the Codex provider explicitly when using it.
The JSON file alone does not launch anything or change global defaults.

Lean 4.33.1 and Mathlib are pinned in the Lake files. Because upstream REPL has
no 4.33.1 tag, its 4.33.0 source commit is pinned and built with Lean 4.33.1.
