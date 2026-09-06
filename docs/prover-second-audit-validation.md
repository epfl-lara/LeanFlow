# Prover redesign: second verification pass

Validated on 2026-09-06 in `milikic/prover-redesign`, starting from `a0c46bb`.
This pass combined independent Claude Code review, additional primary-source
research, failing regression tests, full repository checks, and real Lean runs.
The original checkout and its untracked `Refactor.md` were preserved.

## Outcome

| Check | Result |
| --- | --- |
| Black | 695 files unchanged |
| Ruff | Passed |
| Mypy | 278 gated source files passed |
| Full Python suite | 7,014 passed, 109 skipped, 14 existing warnings |
| Additional focused suite with real Lean enabled | 77 passed |
| Extension tests | 159 passed |
| Extension TypeScript, production build, VSIX packaging | Passed |
| Live model proof: `putnam_2007_b1` | Independently accepted; final Lake build passed |
| Production resume after final-gate repair | Completed; no additional model calls |
| Real four-file top-down chain | Completed in four scripted sessions; no retries |

The full Python suite excludes opt-in Lean project tests; these were also run
explicitly against a built local project. OS execution was verified on macOS.
The extension was not installed into the user's running editor during this pass.

## Live theorem and independent acceptance

An isolated copy of `putnam_2007_b1` from
`testdata/workflow_projects/ProveDemo/ProveDemo/IMOMath3.lean` was supplied to the
actual bounded standard prover using the configured Codex connection and
`gpt-6-astra`. The theorem states that a nonconstant integer polynomial with
nonnegative coefficients satisfies the specified divisibility condition exactly
at the positive integer `n = 1`.

The prover used six requests from a 40-request ceiling, with 33,170 reported input
tokens and 2,891 output tokens. It found the evaluation-divisibility lemma,
repaired its Lean proof from compiler feedback, and submitted a complete proof.
The controller independently accepted it. The final build initially failed because
the sandbox's writable build directory did not yet exist. A regression reproduced
that setup failure; the fix creates and validates the directory before granting
access. Production resume completed under a new run ID without further model
calls. Cumulative recorded campaign time was 295.338 seconds; cost was unknown.

Because the type-profile implementation evolved while the live proof ran, the
finished proof was checked again against the final implementation using a freshly
captured profile from the original saved baseline. This independent check passed:
original type and fixed local dependencies matched, and the only axioms were
`Classical.choice`, `Quot.sound`, and `propext`. The audit made no canonical source
changes and used no model calls.

Evidence is retained locally under `/tmp/leanflow-prover-second-audit/`:
`live-putnam-result.json`, `live-putnam-resume.log`, `putnam-final-recheck.json`,
and `putnam-current-gate.json`. The fixture path is in `putnam-root.txt`.
The original run ID is `second-audit-live-putnam`; its resumed execution is
`second-audit-live-putnam-resumed`.

A separate real-Lean controller test used four original modules in a dependency
chain. Scripted model replies supplied correct top-down candidates. It reproduced
both incomplete promotion and stale imported artifacts, then completed all four
nodes after the fixes with exactly four sessions and a successful final build.
Its evidence is `promotion-live-result.json`. This checks scheduling and Lean
integration, not model planning quality.

## Verified corrections

- **Statement integrity:** proof-hole replacements cannot append declarations or
  unscoped commands. Before planning, the controller captures actual kernel types
  and referenced fixed local definition bodies. Materialization and acceptance
  reject import-induced meaning changes. Fresh compilation and axiom inspection
  reject a stale warm-checker verdict after an imported dependency changes.
- **Recovery:** planning resumes reuse finished outline/design/review reports and
  interrupted allocations. Bounded multi-file journals and immutable source
  checkpoints recover interrupted materialization without overwriting outside
  edits. Accepted proof installation uses exclusive temporary files and guarded
  rollback. Source conflicts have actionable typed status.
- **Parallel work:** finished sibling candidates survive cancellation or provider
  failure and can be verified on resume without another model pass. Stale results
  cannot reset newer proved nodes. Preparation errors release reservations;
  repeated accounting cannot double-charge them.
- **Scheduling:** top-down promotion continues until all newly ready candidates
  have been checked. Accepted original modules, as well as generated helpers,
  refresh their importable artifacts before dependent proofs use them.
- **Bounded progress:** renamed ancestor restatements are rejected. Changed notes
  or a claimed promising direction alone no longer renew a pass; concrete edits
  inside protected scratch holes can retain a partial proof for another bounded
  pass. This is an edit-progress check, not certification of an unfinished proof.
- **Efficiency and context:** idle loops no longer rewrite PLAN/DAG/source
  snapshots; metric changes reuse immutable source checkpoints. Idle source scans
  run at a coarse interval while acceptance retains strict checks. Admission
  includes tool schemas and the remaining-budget note. Repeated identical writes
  do not reset the search repetition guard. Search omission is explicit, and large
  tool results stay valid JSON with the full evidence saved in a readable artifact.
- **Guidance and UI:** job-addressed messages arriving before session startup are
  delivered and remain pinned across compaction and resume. The extension waits
  for acknowledgement and preserves rejected drafts. Standard mode targets its
  active prover by default, and stale legacy launch settings cannot silently
  change the selected mode. Resume preserves saved target scope and rejects an
  explicit mismatch.

Characterization and regression coverage is in `tests/leanflow/test_prover_*`,
including the new recovery, progress, promotion, source-integrity, type-profile,
source-search and storage suites. Meaningful failures were reproduced before their
fixes; the complete gate was run after integration.

## Independent review and literature

Claude Code completed a read-only review with `claude-opus-5[1m]`, using the
existing effort configuration without an override. Its process exited normally,
reported `is_error=false` and `subtype=success`, and recorded no permission denials.
The session ID is `ace5592b-865d-40e5-8e72-8386c08f652b`; the raw report is
`/tmp/leanflow-prover-second-audit/claude-review.json`.

Claude reported ten findings. The material findings concerned top-down promotion,
malformed negation submissions, source-conflict status, idle persistence,
finished sibling candidates, resume scope, restored graph bounds, and the restart
progress predicate. These were checked against the source and covered by
regressions; the cost-label and documentation findings were also corrected.
Claude's review itself was static and observed concurrent edits. The real Lean
and provider evidence above comes from the subsequent local verification.

The [additional literature audit](prover-second-audit-research.md) reopens the
three requested papers and examines three related papers plus official Codex and
Kilo sources. It supports preserving compiler feedback and partial proofs without
adding another advisory layer. A formal parent-sketch witness remains a possible
bounded extension; the current DAG review is not a formal certificate that each
proposed dependency set discharges its parent.

## Limits

This is correctness and integration evidence, not a benchmark of pass rates or
cost savings. Bottom-up remains the default; top-down remains experimental despite
the verified chain test. Live planning quality, representative hard-problem
performance, Linux kernel execution, arbitrary external dependency installation,
and an installed VS Code Extension Host campaign remain unmeasured here.
Duplicate short declaration names in different namespaces of one file and
ambiguous ambient contexts for negation still fail closed. Exact type identity
checks may conservatively reject an equivalent statement elaborated differently.
