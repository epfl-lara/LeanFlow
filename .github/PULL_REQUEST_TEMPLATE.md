## Summary

<!-- Explain the user-visible or maintainer-visible outcome. -->

## Motivation

<!-- What problem does this solve, and why is this the right scope? -->

Related issue:

## Changes

<!-- List the important changes. Keep generated files and unrelated cleanup out of the PR. -->

-

## Verification

<!-- Include the exact checks you ran and any manual workflow or platform coverage. -->

```text
black .
ruff check .
mypy
python -m pytest -q
```

## Risk and Compatibility

<!-- Note public API, workflow-state, provider, cross-platform, migration, or rollback concerns. -->

## Checklist

- [ ] The change is scoped to Lean proving, formalization, verification, or supporting runtime behavior.
- [ ] New behavior has tests; coupled refactors have characterization coverage.
- [ ] Black, Ruff, mypy, and the full test suite pass.
- [ ] User-facing behavior and configuration are documented.
- [ ] `ARCHITECTURE.md` is updated if module ownership or a public surface changed.
- [ ] Workflow and worker contract changes are reflected in `leanflow_specs/` and the routing skill where applicable.
- [ ] No credentials, local paths, generated campaign artifacts, caches, or workflow logs are committed.
- [ ] Cross-platform and sandbox behavior were considered where relevant.
