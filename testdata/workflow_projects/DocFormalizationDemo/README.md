# DocFormalizationDemo

Small mathlib-based Lean project for EPFLemma document formalization testing.

The project is intentionally separate from `GaussTest`: `GaussTest` remains a
proof-repair fixture, while this project exercises the `/formalize` and
`/autoformalize` document pipeline.

Source document:

- `docs/RecountingTheRationals.tex` is a locally authored LaTeX note based on
  Calkin and Wilf's paper, not the original article source. I looked for an
  original `.tex` source and found publisher/PDF sources, but not source TeX.
- Primary reference: Neil Calkin and Herbert S. Wilf, "Recounting the
  Rationals", The American Mathematical Monthly 107(4), 360-363, 2000,
  https://doi.org/10.1080/00029890.2000.12005205.
- Public PDF mirror: https://www.math.clemson.edu/~calkin/Papers/recountingmonthly.pdf.

Contents:

- `lakefile.toml`: Lean package configured with mathlib and REPL.
- `DocFormalizationDemo.lean`: minimal library entrypoint importing mathlib.
- `docs/RecountingTheRationals.tex`: realistic LaTeX source document to formalize, covering the Calkin-Wilf hyperbinary enumeration of the positive rationals.

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

Commit guard:

- The repository pre-commit hook protects this project, including
  `docs/RecountingTheRationals.tex`.
- Runtime workflow output should remain in ignored paths such as `.epflemma/`,
  `.lake/`, `.artifacts/`, or ignored LaTeX build files.
- Intentional fixture updates require
  `ALLOW_DOCFORMALIZATIONDEMO_COMMIT=1 git commit`.
