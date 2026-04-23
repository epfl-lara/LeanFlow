# GaussTest

Small Lean workflow project kept in-repo for EPFLemma manual and opt-in workflow testing.

This is a vendored snapshot of the local `GaussTest` proving project. It is intentionally not wired into the default test suite. The purpose is to keep a compact real Lean repo around for:

- `prove` and `autoprove` smoke runs
- future `formalize` and `autoformalize` workflow experiments
- workflow UX, resumability, and state/debugging checks against a nontrivial Lean project

Contents:

- `GaussTest/RealTheorems.lean`: analysis/algebra examples with several `sorry` goals
- `GaussTest/IMOMath.lean`: olympiad-style theorem stubs
- `examples_nam.txt` and `examples_f2f.txt`: extra prompt material for future workflow coverage

Typical local run:

```bash
lake update
epflemma project init
epflemma workflow prove GaussTest/RealTheorems.lean
```

When updating this snapshot, keep it small and Lean-workflow focused. Do not turn it into a default CI dependency.
