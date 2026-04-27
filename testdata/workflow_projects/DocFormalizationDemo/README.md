# DocFormalizationDemo

Small mathlib-based Lean project for EPFLemma document formalization testing.

The project is intentionally separate from `GaussTest`: `GaussTest` remains a
proof-repair fixture, while this project exercises the `/formalize` and
`/autoformalize` document pipeline.

Contents:

- `lakefile.toml`: Lean package configured with mathlib and REPL.
- `DocFormalizationDemo.lean`: minimal library entrypoint importing mathlib.
- `docs/RecountingTheRationals.tex`: LaTeX source document to formalize, covering the Calkin-Wilf enumeration of the positive rationals.

Typical run:

```bash
lake update
lake build
epflemma project init
epflemma workflow formalize docs/RecountingTheRationals.tex
```

The alias is equivalent:

```bash
epflemma workflow autoformalize docs/RecountingTheRationals.tex
```

Expected preflight artifacts after starting the workflow:

- `.epflemma/workflow-state/formalization/docs-RecountingTheRationals/context.md`
- `.epflemma/workflow-state/formalization/docs-RecountingTheRationals/blueprint.md`
- `.epflemma/workflow-state/formalization/docs-RecountingTheRationals/manifest.json`
- `DocFormalizationDemo/Formalization/RecountingTheRationals.lean`

`DocFormalizationDemo/Formalization/RecountingTheRationals.lean` should not
exist in the clean base fixture. EPFLemma creates it when the formalization
workflow starts. There is no pre-written Lean formalization of the target in
this fixture.
