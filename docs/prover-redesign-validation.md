# Prover redesign validation

Validated on macOS on 2026-09-06 in the separate
`milikic/prover-redesign` worktree. The original checkout was left unchanged;
its pre-existing untracked `Refactor.md` was preserved.

## Repository gates

| Check | Result |
| --- | --- |
| Original checkout baseline | 6,761 passed, 105 skipped |
| Black | 687 files unchanged |
| Ruff | Passed |
| Mypy | Passed, 277 gated source files |
| Full Python suite | 6,934 passed, 106 skipped, 14 warnings |
| Extension tests | 154 passed |
| Extension TypeScript and build | Passed |
| Installable VSIX package | Built successfully |
| CLI help, flags JSON, installed-wrapper help | Passed |
| Exact-run prover CLI snapshot | Completed state, four calls, plan and changed-file data returned |

The Python warnings are existing deprecation/runtime warnings; the additional
skip is the opt-in Lean integration test. Protected Lean checks were also run
explicitly outside the default suite.

## End-to-end evidence

- **Live standard prover:** the configured Codex connection using `gpt-6-astra`
  solved `(2 : Nat) + 2 = 4` in four provider requests. The controller replaced
  only the original `sorry` with `rfl`, independently checked the candidate and
  its axiom profile, and passed the final Lake build. Recorded usage was 11,008
  input tokens and 572 output tokens; elapsed time was 71.693 seconds, including
  Lean startup and verification. Cost was reported as unavailable, not zero.
- **Research integration:** scripted model replies exercised fresh outline,
  graph construction and review, helper-library registration, helper proving,
  root proving, and a successful final Lake build. Lean verification and the
  build were real; this test does not measure model planning quality.
- **Negation:** an explicitly quantified false arithmetic statement inside a
  namespace was negated and independently proved in scratch. The original source
  stayed unchanged; the accepted axiom profile used only standard axioms.
- **Isolation:** real macOS checks rejected canonical-file writes, including
  direct paths, symlinks, hardlinks and Lean `#eval` IO. Warm checks reused the
  Lean process; bounded-output and descendant-timeout cleanup tests passed.
- **Research resources:** a live arXiv download was saved with provenance.
  Pinned dependency installation and rollback were tested with controlled Lake
  execution; no new third-party package was installed into the user's project.
- **Provider accounting:** a real local HTTP server returning 429 received
  exactly one physical request. Failed provider requests and resumed calls kept
  their original durable allocation. Live tests also exposed and fixed Codex
  request-field and streamed-output compatibility issues.

Claude Code reviewed the design and implementation. Its actionable findings
included helper-library registration, source rollback, cancellation propagation,
startup visibility, settings precedence, resume hole identity, and event privacy.
These were addressed and covered by focused tests.

The final review also checked recovery after helper compilation and negation
provider failures. Both now preserve the accepted candidate or the existing
negation job allocation for resume, with focused regression coverage.

## Limits of this evidence

This is integration and correctness evidence, not a benchmark showing lower
cost or higher proof success on hard mathematics. Top-down scheduling remains
experimental; bottom-up is the default. The extension was tested through its
build, unit tests, CLI contract and isolated browser fixtures, not an installed
VS Code end-to-end campaign.

Accepted proof commits have a crash journal. Multi-file plan materialization
rolls back ordinary failures but does not yet recover an abrupt process kill
between its file writes and state publication. Exact negation declines ambiguous
section-variable contexts. Linux isolation profiles are tested, while actual OS
execution was verified on macOS. See [the workflow reference](prover-workflow.md)
for configuration and supported boundaries.
