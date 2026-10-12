# Workflow example projects

This directory contains Lean projects for manual and opt-in workflow testing.

Their Lean workflows are not run by default pytest or CI. Use them to exercise
real workflows independently of an existing user project. Run in a copy when
you need to preserve the original fixture's proof holes.

Current contents:

- [ProveDemo](ProveDemo/README.md): a small mathlib-based proving project with `sorry` targets.
- [DocFormalizationDemo](DocFormalizationDemo/README.md): a mathlib-based formalization project with LaTeX source papers.
- `LeanIMOBench/`: the 60-problem Lean-IMO-Bench fixture (30 Basic, 30 Advanced),
  one standalone module per problem so each is proved and scored independently.
  See its [README](LeanIMOBench/README.md) for setup and clean-room requirements.

Typical usage:

```bash
cd testdata/workflow_projects/ProveDemo
lake update
leanflow project init
leanflow workflow prove ProveDemo/RealTheorems.lean
```

Benchmark usage (one problem at a time):

```bash
cd testdata/workflow_projects/LeanIMOBench
lake update
lake build                         # all 60 statements must elaborate
leanflow project init
python3 scripts/list_problems.py --split Advanced --unsolved-by-leap
./scripts/run_problem.sh PB-Advanced-012
```

Document formalization usage:

```bash
cd testdata/workflow_projects/DocFormalizationDemo
lake update
lake build
leanflow project init
leanflow workflow formalize docs/PythagoreanPolynomialParametrization/pyth.tex
leanflow workflow formalize docs/QuantizingPythagoreanTriples/Pythagore2.tex
```

## Fixture protection

When the repository hook is installed, it protects intentional statement and
fixture content from accidental commits. Runtime attempts belong in ignored
paths such as `.leanflow/`, `.lake/`, and LaTeX build outputs.

- tracked files under `testdata/workflow_projects/ProveDemo` are protected by the repo pre-commit hook
- tracked files under `testdata/workflow_projects/DocFormalizationDemo` are protected by the repo pre-commit hook
- to intentionally refresh the canonical fixture, use `ALLOW_PROVEDEMO_COMMIT=1 git commit`
- to intentionally refresh the document fixture, use `ALLOW_DOCFORMALIZATIONDEMO_COMMIT=1 git commit`
- tracked files under `testdata/workflow_projects/LeanIMOBench` are protected by the repo pre-commit hook
- to intentionally refresh the benchmark fixture, regenerate with `scripts/build_benchmark.py` and use `ALLOW_LEANIMOBENCH_COMMIT=1 git commit`
