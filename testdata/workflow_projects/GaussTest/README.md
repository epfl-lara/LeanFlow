# GaussTest

Small Lean workflow project kept in-repo for EPFLemma manual and opt-in workflow testing.

This is a vendored snapshot of the local `GaussTest` proving project. It is intentionally not wired into the default test suite. The purpose is to keep a compact real Lean repo around for:

- `prove`/`autoprove` smoke runs
- future `formalize`/`autoformalize` workflow experiments
- workflow UX, resumability, and state/debugging checks against a nontrivial Lean project

Contents:

- `GaussTest.lean`: default library entrypoint; imports the theorem files in project order
- `GaussTest/RealTheorems.lean`: real-analysis and Lipschitz examples
- `GaussTest/IMOMath1.lean`: smaller solved contest-style algebra, number theory, AIME, IMO, and Putnam examples
- `GaussTest/IMOMath2.lean`: larger contest-style examples, mostly solved, with one remaining proof-repair target
- `GaussTest/IMOMath3.lean`: harder Putnam-style theorem statements kept as `sorry` targets

The `lakefile.toml` default target is `GaussTest`, so `lake build` checks the sorted theorem collection by default.

Typical local run:

```bash
lake update
lake build
epflemma project init
epflemma workflow prove GaussTest/RealTheorems.lean
epflemma workflow prove GaussTest/IMOMath2.lean
epflemma workflow prove GaussTest/IMOMath3.lean
```

When updating this snapshot, keep it small and Lean-workflow focused. Add new contest problems to the sorted `IMOMath*.lean` files, keep `GaussTest.lean` aligned with files that should build by default, and do not turn this project into a default CI dependency.
