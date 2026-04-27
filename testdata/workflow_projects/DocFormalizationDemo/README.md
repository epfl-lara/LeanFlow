# DocFormalizationDemo

Small mathlib-based Lean project for EPFLemma document formalization testing.

The project is intentionally separate from `GaussTest`: `GaussTest` remains a
proof-repair fixture, while this project exercises the `/formalize` and
`/autoformalize` document pipeline.

Contents:

- `lakefile.toml`: Lean package configured with mathlib and REPL.
- `DocFormalizationDemo.lean`: library entrypoint.
- `DocFormalizationDemo/Setup.lean`: local coding-theory definitions that a planner can reuse.
- `docs/Hamming74SingleErrorCorrection.tex`: LaTeX document formalizing the Hamming(7,4) single-error-correction argument.

Typical run:

```bash
lake update
lake build
epflemma project init
epflemma workflow formalize docs/Hamming74SingleErrorCorrection.tex
```

The alias is equivalent:

```bash
epflemma workflow autoformalize docs/Hamming74SingleErrorCorrection.tex
```

Expected preflight artifacts after starting the workflow:

- `.epflemma/workflow-state/formalization/docs-Hamming74SingleErrorCorrection/context.md`
- `.epflemma/workflow-state/formalization/docs-Hamming74SingleErrorCorrection/blueprint.md`
- `.epflemma/workflow-state/formalization/docs-Hamming74SingleErrorCorrection/manifest.json`
- `DocFormalizationDemo/Formalization/Hamming74SingleErrorCorrection.lean`
