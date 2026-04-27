## Workflow Example Projects

This directory contains Lean projects that we keep in-repo for manual and opt-in workflow testing.

These projects are not part of the default pytest or CI path. They exist so we can exercise real EPFLemma workflows against small, known Lean repos without relying on user-local state.

Current contents:

- `GaussTest/`: small mathlib-based proving project with `sorry` targets and extra text prompts for future workflow expansion.
- `DocFormalizationDemo/`: small mathlib-based document formalization project with a LaTeX source paper.

Typical usage:

```bash
cd testdata/workflow_projects/GaussTest
lake update
epflemma project init
epflemma workflow prove GaussTest/RealTheorems.lean
```

Document formalization usage:

```bash
cd testdata/workflow_projects/DocFormalizationDemo
lake update
lake build
epflemma project init
epflemma workflow formalize docs/Hamming74SingleErrorCorrection.tex
```

Commit guard:

- tracked files under `testdata/workflow_projects/GaussTest` are protected by the repo pre-commit hook
- tracked files under `testdata/workflow_projects/DocFormalizationDemo` are protected by the repo pre-commit hook
- workflow attempts can still accumulate in ignored project-local state such as `.epflemma/` and `.lake/`
- to intentionally refresh the canonical fixture, use `ALLOW_GAUSSTEST_COMMIT=1 git commit`
- to intentionally refresh the document fixture, use `ALLOW_DOCFORMALIZATIONDEMO_COMMIT=1 git commit`
