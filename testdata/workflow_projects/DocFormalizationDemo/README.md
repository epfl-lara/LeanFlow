# DocFormalizationDemo

Small mathlib-based Lean project for LeanFlow document formalization testing.

The project is intentionally separate from `ProveDemo`: `ProveDemo` remains a
proof-repair fixture, while this project exercises the `/formalize` and
`/autoformalize` document pipeline.

Source documents:

- `docs/PythagoreanPolynomialParametrization/pyth.tex` is the TeX source for
  Sophie Frisch and Leonid Vaserstein, "Parametrization of Pythagorean triples
  by a single triple of polynomials", J. Pure Appl. Algebra 212(1), 271-274,
  2008. arXiv: https://arxiv.org/abs/0706.0290.
- `docs/QuantizingPythagoreanTriples/Pythagore2.tex` is the TeX source for
  Hugo Mathevet, Sophie Morier-Genoud, and Valentin Ovsienko, "Quantizing
  Pythagorean triples", arXiv: https://arxiv.org/abs/2602.20536.

Contents:

- `lakefile.toml`: Lean package configured with mathlib and REPL.
- `DocFormalizationDemo.lean`: minimal library entrypoint.
- `docs/PythagoreanPolynomialParametrization/`: real TeX source project for an
  integer-valued-polynomial parametrization theorem for Pythagorean triples.
- `docs/QuantizingPythagoreanTriples/`: real TeX source project for a
  q-deformed Pythagorean triple construction.

Typical run:

```bash
lake update
lake build
leanflow project init
leanflow workflow formalize docs/PythagoreanPolynomialParametrization/pyth.tex
```

Or try the q-deformation source:

```bash
leanflow workflow formalize docs/QuantizingPythagoreanTriples/Pythagore2.tex
```

Expected preflight artifacts after starting the workflow:

- `.leanflow/workflow-state/formalization/.../context.md`
- `.leanflow/workflow-state/formalization/.../manifest.json`
- `DocFormalizationDemo/<DocumentName>/Blueprint.md`
- `DocFormalizationDemo/<DocumentName>/Main.lean`

Document-specific generated modules should not exist in the clean base fixture.
LeanFlow creates them when a formalization workflow starts. The blueprint lives
beside the generated Lean files so planner and prover turns can reread it
easily. There is no pre-written Lean formalization of either target in this
fixture.

Commit guard:

- The repository pre-commit hook protects this project, including `docs/`.
- Runtime workflow output should remain in ignored paths such as `.leanflow/`,
  `.lake/`, `.artifacts/`, or ignored LaTeX build files.
- Intentional fixture updates require
  `ALLOW_DOCFORMALIZATIONDEMO_COMMIT=1 git commit`.
