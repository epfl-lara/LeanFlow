# ProveDemo

Small Lean workflow project kept in-repo for LeanFlow manual and opt-in workflow testing.

This is a vendored snapshot of the local `ProveDemo` proving project. It is intentionally not wired into the default test suite. The purpose is to keep a compact real Lean repo around for:

- `prove`/`autoprove` smoke runs
- future `formalize`/`autoformalize` workflow experiments
- workflow UX, resumability, and state/debugging checks against a nontrivial Lean project

Contents:

- `ProveDemo.lean`: default library entrypoint; imports the theorem files in project order
- `ProveDemo/RealTheorems.lean`: real-analysis and Lipschitz examples
- `ProveDemo/IMOMath1.lean`: smaller contest-style algebra, number theory, AIME, IMO, and Putnam examples
- `ProveDemo/IMOMath2.lean`: larger contest-style examples, medium difficulty Putnam problems, and some harder AIME problems
- `ProveDemo/IMOMath3.lean`: harder Putnam-style theorem statements

The `lakefile.toml` default target is `ProveDemo`, so `lake build` checks the sorted theorem collection by default.

Typical local run:

```bash
lake update
lake build
leanflow project init
leanflow workflow prove ProveDemo/RealTheorems.lean
leanflow workflow prove ProveDemo/IMOMath2.lean
leanflow workflow prove ProveDemo/IMOMath3.lean
```

When updating this snapshot, keep it small and Lean-workflow focused. Add new contest problems to the sorted `IMOMath*.lean` files, keep `ProveDemo.lean` aligned with files that should build by default, and do not turn this project into a default CI dependency.
