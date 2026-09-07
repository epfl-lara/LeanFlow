# Prover dashboard and job follow-up backlog

Recorded 2026-09-06 from Beck–Fiala campaign `prove-vscode-run-mtpu8tot-isdk`
and the preceding readiness checks. Updated after the completed Spencer campaign
with runtime fixes and VS Code extension 0.1.14. Research campaigns remain separate
from the disposable verification project.
Updated 2026-09-07 with the Spencer universal-bound verification measurements (J15).

Scope: the dedicated standard/research prover and its VS Code surfaces. Preserve
source protection, independent verification, durable budgets, and resumability.
This is a future-work backlog, not work required now. P1 and P2 indicate relative
priority for later improvements or investigations. Checked items have verified
fixes; unchecked items remain deferred until separately scheduled.

## Release verification

The 0.1.14 implementation adds independent progress publication every two
seconds, explicit operation start/end records, and a three-second live UI
refresh with sequence ordering and last-good-state retention. A long source
transaction no longer prevents worker progress from being published. The
snapshot still references the last committed source checkpoint; staged files
are visible without being treated as accepted proofs.

Regression coverage includes the reported D09 counts; verification/integration
transitions; frozen-source snapshots; interrupted operation and candidate
resume; a failed second edit preserving a prior committed edit; nested
kernel-inspection timeouts preserving the accepted plan; and cancellation at
the final session event retaining exact stop reasons and token accounting.

Independent validation: 7,266 Python tests, Black/Ruff/mypy, 243 extension tests,
production VSIX packaging and installation, real isolated Lean checks with
helper imports, real managed Loogle searches, and the installed dashboard.
The completed Spencer run is visible as 5 solved, 0 active, 0 awaiting checks,
135/2000 calls, and 1h32m elapsed. Old snapshots explicitly show missing metadata
as unknown rather than inventing provider/effort or historical stop reasons.

The disposable live research run `backlog-live-smoke-20260906-2` completed in
8m11s using 11/16 calls (outline 3, proposal 2, review 2, prover 4) on
GPT-6 Astra/xhigh. Its final Lake build passed. The installed dashboard detected
the externally started run without Refresh and recovered after an editor reload.
An initial fixture-only preflight failure occurred before any paid calls because
the newly created Lake configuration had not been prepared; warming that
configuration fixed setup without changing sandbox permissions.

This live check caught two additional UI cases, now regressed: a still-running
prover waiting synchronously for verification must count as Awaiting, and the
overall graph-validation operation must not count as an additional concrete Lean
check. Captured live-state replay confirms Active 0 / Awaiting 1 while the job
itself is still running. New completed snapshots no longer get a legacy-state
warning just because successful completion has no failure stop reason. Candidate
verification shares one active Lean deadline across elaboration and type
inspection after acquiring its slot. Queue waits use remaining campaign time,
observe cancellation, and publish a separate verifier-queue operation; contention
no longer consumes a proof's active check allowance.

The final Fable 5.1/max review found no blocking issue in the queue correction.
Two real submissions through the same verifier both passed; the second waited
67.77s for its slot, and both completed in 86.69s total. They used only `propext`.
A separate exact-candidate check also passed in 60.88s with matching protected
type. No model calls were used for these independent checks.

Loogle's repaired client writes and reuses the managed 376 MB index: cold build
plus query took 164.2s, the second process and query 3.45s, both returning
`Real.exp_add`. The REPL preflight live check resolves missing v4.33.1 to v4.33.0.
Trailing CLI preview options now produced a valid preview without creating any
run directory in the live smoke fixture.

### Remaining measurements and external limitations

- **J02**: phase timings and single-use reuse of an identical independently
  checked submission are implemented. Warm/batched skeleton validation and
  comparative time-to-first-prover measurements remain follow-up work. The
  submission cache invalidates on source, candidate, dependency, axiom,
  signature, toolchain or Lake manifest changes; final build remains mandatory.
  The hard campaign exposed excessive invalidation and repeated candidate checks;
  see **J15** for measured queue, verification and publication delays.
- **J04**: phrase/term ranking, provider interleaving and degradation reporting
  are implemented with offline regressions. A live Spencer query put the
  relevant partial-colouring paper first; the web provider returned a connection
  reset and arXiv supplied weak lower-ranked hits. Broader ranking evaluation
  and external web-provider reliability remain open; no retry cascade was added.
- **J10**: prompts and a one-call completion regression now encourage planning
  stages to finish when ready; the real smoke proposal and review both finished
  after two of three allowed calls. A full hard campaign is still needed to quantify
  the effect on repeated research and audits.

## Dashboard and launch

- [x] **D01 · P1 · Show the actual post-review phase and check progress.**
  **Confirmed:** the reviewer accepted the proposal at 14:00:15 UTC, but the
  persisted phase stayed `reviewing` while helper compilation continued. At
  14:05, five of eight helper skeletons had compiled, yet all displayed agent
  jobs were completed. The UI looked idle.
  **Done when:** validation appears as active controller work with the current
  file, checks completed/total, start time, last progress, timeout, and explicit
  reason provers are waiting. Include protected-signature checks after skeleton
  compilation; do not imply that compiled skeletons are proved theorems.
  **Owner:** `planning_controller.py`, prover observer/state, `ProverWorkspace.tsx`.

- [x] **D02 · P1 · Display the complete campaign budget.**
  **Confirmed:** the proof workspace shows calls consumed and plan refinements,
  but its normalized metrics omit total calls, reservations, and elapsed time.
  **Done when:** show used/total calls, calls available for new work, outstanding
  reservations, active elapsed/limit, and remaining restart/decomposition/node
  allowances. Explain that the 50-call planning limit is per stage. Distinguish
  reserved capacity from actual spend. Unknown cost must remain unknown.
  **Owner:** `src/core/prover.ts`, `ProverWorkspace.tsx`, budget snapshot contract.

- [x] **D03 · P1 · Correct model, provider, and agent-capacity displays.**
  **Confirmed:** Live showed provider/model as `—` and `Agents 1`, while the
  dedicated controller had `gpt-6-astra` and research parallelism four. The
  generic card reads legacy live-status fields.
  **Done when:** display effective provider, model, reasoning effort, and context
  per role; distinguish one orchestrator from `0/4` active prover slots. Explain
  why slots are unused: planning, validation, dependencies, or budget reservation.
  **Owner:** `LiveView.tsx`, prover snapshot/observer.

- [x] **D04 · P1 · Expose the proposed DAG before publication.**
  **Confirmed:** the canonical DAG remained the single original root throughout
  proposal, semantic review, and skeleton validation. The eight-helper proposal
  was only available in job artifacts.
  **Done when:** users can inspect a separately labelled proposed/reviewed DAG
  and review result while the validated executable DAG remains authoritative.
  Show syntax-check failures on proposed nodes without marking them proved or
  scheduling them prematurely. Keep accepted progress visible during replanning.
  **Owner:** planning checkpoints, `src/core/prover.ts`, graph/tree views.

- [x] **D05 · P1 · Report staged file changes while checks run.**
  **Confirmed:** helper files and a Lake configuration edit existed on disk while
  the persisted dashboard still reported no recorded edits. Materialization
  accumulates changes before publishing its transaction.
  **Done when:** show tentative creations/edits, responsible controller job, and
  source/diff links as they happen; clearly mark pending validation, committed,
  or rolled back. Do not weaken the atomic source transaction to update the UI.
  **Owner:** `source_transaction.py`, `planning_controller.py`, change snapshots.

- [x] **D06 · P2 · Make activity and job artifacts useful without raw JSON.**
  **Observed UX gap:** `Last activity: api-response` does not explain useful
  work. Draft outlines, resources, and computation reports exist in job folders
  before the canonical plan changes.
  **Done when:** show short factual events such as “reused paper”, “computation
  rejected”, and “checking helper 5/8”, with durations and links to source
  artifacts/results. Separate draft notes from accepted PLAN content. Retain
  raw events and existing result-report links; do not expose hidden reasoning.
  **Additional confirmed failure at 14:12 UTC:** the run log rendered
  `plan_rejected: {}` and the activity event omitted its diagnostic. The reason
  could only be recovered from the next planning job's prompt.
  **Owner:** event shaping, logs view, job artifact inventory, plan view.

- [x] **D07 · P1 · Make saved profiles reliably usable in the extension.**
  **Confirmed; local workaround applied:** the prepared CLI profile included
  `LEANFLOW_PROVER_ALLOWED_AXIOMS`, which VS Code correctly rejects as
  terminal-only. Removing this redundant default from the project profile
  enabled launch without changing the effective standard axiom policy.
  **Done when:** saving/exporting a profile identifies its supported launch
  surfaces and flags incompatibilities before launch. Preview includes the
  effective dedicated prover configuration, including inherited profile values,
  and shows the actual assigned run ID. Keep the extension allowlist intact.
  **Owner:** profile catalog/export, `launch.ts`, launch preview and UI.

- [ ] **D08 · P1 · Detect externally resumed runs while the dashboard is idle.**
  **Confirmed:** after restarting through the VS Code terminal, the updated
  dashboard continued to show the old interrupted run. The run manager stops
  polling when no active owner exists and does not discover a later external
  start until Refresh is pressed. Refresh correctly attaches and restarts polling.
  **Reopened, 2026-09-06 at 21:43 UTC:** idle discovery exists, but a selected
  terminal run still masks the newly discovered live owner. After resuming as
  `spencer-universal-resume-20260906-1`, Live remained pinned to the predecessor's
  provider error even after Refresh. Reloading the window cleared the selection
  and exposed the new running campaign. Provide an explicit follow-current-run
  control and make the selected historical execution clear; test this transition
  after a run launched from this same dashboard has failed.
  **Done when:** an idle dashboard detects a new project owner through a bounded
  idle check or file notification, without requiring a fresh extension launch.
  **Owner:** `runManager.ts`, services and external-run discovery tests.

- [x] **D09 · P1 · Count submitted proofs as awaiting verification/integration.**
  **User report, confirmed against live state on 2026-09-06:** SpencerResearch
  displayed `Solved 0 · Active 3 · Pending 2 · Awaiting verification 0`, although
  `prover_00005` had completed its Boolean-cube product proof after six calls.
  Independent submission checking accepted it at 19:03:34 UTC with standard
  axioms; integration was still pending. Its DAG node `n_rb_cube_product` retained
  status `running`. `graphProofState` classifies that stale node status as active
  even when the corresponding prover job is completed.
  **Done when:** persist and expose the transition from proving to submitted,
  verifying, and integrating. Count this node under Awaiting verification, with
  its current check/integration step visible, until final acceptance marks it
  solved. For the reported snapshot, show `Solved 0 · Active 2 · Pending 2 ·
  Awaiting verification 1`. A completed job alone must never imply a solved node.
  Keep card totals, graph badges, job rows, and node details consistent; cover
  successful submission, pending integration, rejected candidates returning to
  proving, conditional dependencies, and terminal checks with regression tests.
  **Owner:** runtime submission/integration state, observer, `proverGraph.ts`,
  `ProverWorkspace.tsx`. Related: D01 concerns controller-phase visibility;
  this item concerns individual proof lifecycle and aggregate counts.

## Controller and agent jobs

- [x] **J00 · P1 · Fix generated-helper import bookkeeping before retrying staging.**
  **Confirmed live blocker, reproduced without editing campaign files:** after
  the eight-helper proposal's semantic acceptance, materialization rolled back
  at 14:12:11 UTC with `protected source changed outside controller:
  LeanFlowProofs/FloatingGeometricProgress.lean`. A generated helper's baseline
  already contains its dependency imports. The later import pass adds those
  same modules to `SourceDocument.imports`, changing `render()` to include
  duplicate imports, while the file rewrite loop only rewrites previously
  existing documents. The controller therefore rejects its own generated file;
  no external edit is required. A read-only reproduction using this exact
  proposal is saved in `materialization-import-mismatch.json` in the evidence
  directory. Other generated helpers with dependencies are also exposed.
  **Wrong recovery classification:** the exception was sent to mathematical
  replanning. Job `orchestrator_00004` responded by dropping the composite helper
  and moving its work into the root, although that does not repair the bookkeeping.
  **Done when:** generated files and expected renderings agree throughout the
  transaction; imports are represented once. A multi-level generated-helper DAG
  passes staging, signature checks, and dispatch. Real external edits must still
  fail closed. Route controller consistency failures to infrastructure diagnosis,
  preserving the accepted mathematical plan and spent-call ledger, rather than
  spending new planning calls on a different decomposition.
  **Owner:** `planning_controller.py`, `source.py`, transaction/recovery tests.
  **Repair:** commit `4e69f84` represents dependency imports once, reconciles
  changed edges, recompiles affected helpers in dependency order, and restores
  previous compiled artifacts on rollback. Source conflicts and deadlines no
  longer become mathematical rejection events. Verified with 7,115 Python tests,
  180 extension tests, real Lean staging/reversal/rollback builds, and two
  Fable 5.1/max review passes whose material findings were addressed. The resumed
  campaign is `beckfiala-research-resume-20260906-2`. At 15:21:14 UTC, all eight
  helper skeletons and protected signatures had passed staging, the nine-node
  DAG was published, and four provers were dispatched. The updated extension's
  Live view confirmed the graph and all four active prover jobs. The campaign
  retained its earlier 50 spent calls; staging used no additional model calls.

- [x] **J01 · P1 · Reconcile helper-import guidance with controller behavior.**
  **Confirmed contract mismatch:** the outline, proposal, and accepted review
  require inlining closed helper proofs because the original target imports
  only Mathlib. The controller explicitly supports adding generated helper
  imports while preserving original source and checking protected signatures.
  This could cause needless duplication and large root proofs; that downstream
  effect has not yet been observed.
  **Done when:** planning, review, and prover contracts share the same precise
  source-edit/import policy. Prefer the supported module path when applicable;
  reserve local inlining for an actual import/dependency constraint. Verify
  original statements and final import closure independently.
  **Owner:** planning/session prompts, `planning_controller.py`, source protection.

- [ ] **J02 · P2 · Measure and reduce time to the first prover.**
  **Observed; optimization needs evaluation:** the outline took about 18m36s
  (21 calls), formal proposal 18m09s (12 calls), and review 2m28s (4 calls).
  Skeleton validation then ran sequential isolated Lean processes, each loading
  Mathlib. The system had spent over 45 minutes with no prover dispatched yet.
  **New baseline:** in `spencer-universal-resume-20260906-1`, accepted review to
  completed materialization/signature validation took 1,456.809s (24m17s), from
  22:02:36 to 22:26:53 UTC on 2026-09-06, for 11 helper skeletons and 12 protected
  declaration signatures. This deterministic phase used no model calls.
  **Done when:** timings separate model reasoning, tool work, imports, and
  validation. Benchmark compact stage handoffs and warm or batched skeleton
  checks against this baseline. Keep fresh review contexts, dependency order,
  import-cache invalidation, isolation, and independent final verification.
  **Owner:** planning/session telemetry, `verification.py`, check runtime.

- [x] **J03 · P1 · Give computation jobs an accurate capability contract.**
  **Confirmed:** both planning contexts attempted `from fractions import
  Fraction as Q`; the alias is rejected. Further requests failed on `In` and
  `BitAnd`. Four rejected computations consumed model turns before successful
  reformulations. Fraction itself is supported; the current error obscures the
  alias restriction.
  **Done when:** expose supported syntax/imports and working exact-arithmetic
  examples; return precise unsupported-construct diagnostics and retain learned
  tool constraints across stage handoffs. Consider safe language additions only
  with tests; retain the sandbox and distinguish experiments from proofs.
  **Owner:** `session_tools.py`, `empirical_compute_runtime.py`, tool prompts.

- [ ] **J04 · P2 · Improve research search relevance and result merging.**
  **Observed, with source-supported cause:** the first Beck–Fiala proof search
  placed unrelated algebra papers ahead of relevant results. The arXiv query
  includes a broad token disjunction, and bounded search appends arXiv results
  before web results, then truncates the combined list.
  **Done when:** evaluate phrase-aware queries, relevant-result ranking, and
  balanced provider merging on a small mathematical-search set. Display degraded
  providers and keep local resource reuse. Do not add an unbounded fallback loop.
  **Owner:** `session_research.py`, shared provider behavior only as separately scoped.

- [x] **J05 · P1 · Repair managed Loogle index compatibility.**
  **Confirmed in readiness testing:** the client sends `--write-index`; the
  installed Loogle rejects it. An interactive `Nat.add_comm` query worked, but
  cold startup took 107.3s. Research local Loogle is currently disabled.
  **Done when:** negotiate/pin compatible index flags and formats; exercise the
  real managed-index path, not only binary availability. Keep bounded fallback
  and avoid a private heavyweight index per parallel prover. A doctor check
  should detect the actual incompatible client path.
  **Owner:** Lean search/Loogle adapter; this shared-tool change needs its own scope.

- [x] **J06 · P1 · Give terminal states an exact stop reason.**
  **Confirmed by source audit; not a terminal event in this campaign:**
  `budget_exhausted` can mean a local job allowance ended with no admissible work,
  even when the overall campaign has calls remaining.
  **Done when:** persist a stable reason code, limiting scope, used/limit values,
  unresolved nodes, and permitted next action. Distinguish campaign calls/time,
  job budget, restart/decomposition limits, infrastructure errors, and certified
  disproof. Test parallel reservations and resume accounting at each boundary.
  **Owner:** `runtime.py`, observer/status mapping, dashboard.

  **2026-09-07 follow-through:** scheduler dead ends now use `blocked`, campaign
  time uses `timeout`, and only campaign call exhaustion uses `budget_exhausted`.
  The benchmark exports stop scope and pauses dead ends. Accepted local repair
  reopens within retained retry limits; rejected DAG drafts keep their critique
  and can continue beyond three attempts inside the global ceilings. Direction
  refinements charge only after acceptance/materialization; reviewer guidance
  explicitly classifies tactic/rewrite repair separately. Request-level cost
  provenance and coverage now survive resume and are durably checkpointed.

- [x] **J07 · P2 · Make timeout behavior and stalled-work detection explicit.**
  **Known limitation:** wall time is cooperative. In-flight tools/checks can
  outlive admission of new work; prover scratch checks still use a separate 60s
  limit despite the campaign's 1,200s request/verification setting.
  **Live reproduction, SpencerResearch:** root job `prover_00008` repeatedly
  received `check_timeout` between 19:15 and 19:26 UTC, including trivial probes
  importing generated helpers. An `import Mathlib`-only probe passed in 45s.
  The prover reported import/check startup as the immediate blocker and copied
  already-proved helper declarations into private scratch to obtain feedback;
  that scratch route returned Lean results, including a pass at 19:28:06.
  This does not establish root acceptance. Preserve these event logs as evidence
  when separating cold import startup from proof elaboration deadlines.
  **Done when:** expose each operation's effective deadline and cancellation
  state, distinguish slow active computation from no progress, and document
  bounded overrun. Evaluate configurable scratch timeouts and process-tree
  cancellation without discarding valid completed work.
  **Owner:** session/check runtime, timeout configuration, live progress.

- [x] **J08 · P2 · Finish operational configuration and recovery gaps.**
  **Known readiness limitations; re-verify separately:** role models share one
  provider and reasoning-effort setting; transient provider failure requires
  manual resume; automatic REPL patch-version selection requested a nonexistent
  `v4.33.1` tag until this project received a tested source pin.
  **Done when:** expose supported per-role settings honestly, offer a clear
  same-run resume path that retains spent budgets, and resolve compatible REPL
  source pins with an actual preflight. Do not equate Claude Code login with
  LeanFlow Anthropic credentials or claim that Fable is live-validated there.
  **Owner:** provider/configuration, resume UX, Lean dependency initialization.

- [x] **J09 · P1 · Reject misplaced preview options before launching work.**
  **Confirmed during repair:** placing `--dry-run --json` after `workflow prove`
  and its target passes them into the workflow's remainder arguments. The CLI
  starts execution instead of previewing it. The unintended resume attempt was
  stopped during checks with the shared counter still at 50 calls.
  **Done when:** recognize preview options unambiguously or reject their invalid
  position before any run is created. Test both supported option positions and
  malformed placement. Until fixed, put them before the workflow name:
  `leanflow workflow --dry-run --json prove Main.lean`.
  **Owner:** CLI workflow argument parsing and launch-contract tests.

- [ ] **J10 · P2 · End planning stages when their deliverable is ready.**
  **Observed in SpencerResearch, 2026-09-06 18:32 UTC:** initial research job
  `orchestrator_00001` had written a complete finite-potential outline and a
  five-helper proposal by calls 20–26. Its notes explicitly said the mathematical
  plan was complete enough for delegation, but at call 37 it was still fetching
  sources and auditing notes; canonical PLAN/DAG remained unpublished and no
  prover had started. This is a latency/efficiency concern, not a budget violation.
  **Source audit:** non-prover final responses may end immediately, so early
  completion is not programmatically blocked. However, the per-request reminder
  says to continue concrete work and mentions reporting on the final permitted
  call without explicitly encouraging an earlier completed deliverable. This is
  a possible contributor, not a demonstrated sole cause.
  **Done when:** planning/review/research guidance treats the allowance as a
  ceiling, submits the requested complete JSON once the assigned uncertainty is
  resolved, and avoids repeated audits without a named unresolved question.
  Characterize early successful completion and verify it in a real campaign;
  preserve the final-call reporting safeguard and independent review gate.
  **Owner:** `agent_session.py`, role-specific orchestration guidance, planning
  stage metrics. Evidence: SpencerResearch run `prove-vscode-run-mtq4h466-l8ib`,
  `jobs/orchestrator_00001/PLAN_job.md` and `events.jsonl`.

- [x] **J11 · P1 · Remove quadratic log-redaction delay.**
  A long identifier in tool feedback caused the assignment-redaction regex to
  retry at every suffix. Profiling attributed 19.47s of a 20.4s test to that
  regex. A match-preserving start boundary eliminates the repeated scans;
  redaction and large-feedback regressions passed together in 0.24s. Full tool
  evidence remains a valid JSON artifact with a bounded context preview.

- [ ] **J12 · P2 · Future work: evaluate minimal prover contexts with controlled expansion.**
  **User decision, 2026-09-06:** keep the current program-generated encouragement
  and informal `PLAN_job.md` sketch behavior. Record context extraction as a future
  experiment; do not replace the current scratch construction as part of this note.
  **Done when:** construct the smallest practical initial context for the assigned
  goal, preserving required definitions, instances, notation, namespaces, section
  variables and universes. Let the prover discover missing material through the
  supplied DAG and project/library searches, then request additional declarations
  or imports through a controller-validated operation. Validate dependency closure,
  reject cycles and target self-use, preserve the original target's kernel type and
  user-protected source, and independently enforce the configured axiom policy.
  Report an explicit fallback to the containing-source scratch when extraction
  cannot preserve the environment safely. Compare context size, token usage,
  initialization/check latency and proof success on representative projects before
  changing the default; a smaller file alone is not evidence of faster proving.
  **Owner:** `source.py`, `job_controller.py`, `session_tools.py`, materialization
  and independent verification. Status: design backlog, not implemented here.

- [ ] **J13 · P2 · Future work: integrate verified extraction of completed local have proofs.**
  **Source audit, 2026-09-06:** the broader tool registry already exposes
  `lean_extract_have` with candidate inventory and bounded transactional extraction.
  It recovers helper context through Mathlib `extract_goal`, checks the copied proof
  with LeanProbe and an axiom gate, checks the replacement, and applies a verified
  patch. The dedicated prover's `SessionTools` does not expose this operation.
  **User decision, 2026-09-06:** do not implement this now or relax the prover's
  body-only editing contract. Only completed local `have` proofs may be extracted.
  Save verified sublemmas in the same Lean file, make them immutable to the prover,
  and retain them as solved declarations in LeanProbe's cache. The agent then
  continues working only inside its assigned proof body.
  **Done when:** adapt this mechanical refactoring for substantial, already-proved
  local blocks, without a speculative decomposer or advisor agent. Recover local
  parameters, hypotheses, lets and universes through Lean; verify each helper and
  the rewritten target together, preserving the target statement and configured
  axiom policy. An unfinished parent must remain marked unfinished. Any future
  extraction must be a controller-owned verified transaction that saves and freezes
  same-file helper declarations, updates source ranges and dependency bookkeeping,
  and refreshes the protected scratch baseline without expanding agent write
  authority. Simply hoisting a helper in today's scratch remains disallowed. Keep
  the original source-protection and body-only submission contract, cycle checks,
  rollback and existing job budget. Keep extraction bounded and optional; measure
  whether stable helper declarations reduce repeated elaboration before automating it.
  **Owner:** `tools/implementations/lean_have_extraction.py`, `session_tools.py`,
  source publication and DAG bookkeeping. Status: integration proposal; no new
  prover tool or extraction invocation is introduced by this note.

- [ ] **J14 · P1 · Recover bounded planning from a transient provider disconnect.**
  **Observed, 2026-09-06 at 21:30:18 UTC:** Spencer universal-bound campaign
  `prove-vscode-run-mtqbk9pg-bp0u` stopped in its first outline job after request
  15 with `peer closed connection without sending complete message body
  (incomplete chunked read)`. The campaign had spent 15/4000 calls and 377.901
  seconds; neither its job nor campaign allowance was exhausted. No prover had
  started. Sources and event logs survived, but no informal plan was committed.
  `agent_session.py` deliberately returns `provider_error` immediately; planning
  currently ends the campaign on that outcome. J08's manual resume support does
  not provide automatic recovery here.
  **Done when:** classify recoverable transport interruption separately from
  authentication/configuration failures, admit a bounded controller recovery with
  preserved context and resources, charge every attempted request, and retain
  cumulative job/campaign limits. Show recovery and the eventual terminal cause
  accurately. Test interrupted streaming, repeated failure, cancellation and
  remaining-budget exhaustion; do not introduce an unbounded retry loop.
  **Owner:** bounded session and planning controller, resume/recovery status.

- [ ] **J15 · P1 · Reduce queued and repeated independent verification work.**
  **User decision, 2026-09-07:** record for future fixes; do not interrupt the
  current campaign or change its verification policy as part of this note.
  **Measured:** Spencer universal-bound run `spencer-universal-resume-20260906-1`,
  events on 2026-09-06 from 22:32 to 23:08 UTC. Four prover slots share one
  independent verifier. These are observed wall times, not estimates of Lean CPU
  time or a general performance benchmark.

  | Submission | Queue wait | Independent check after admission | LeanProbe portion |
  | --- | ---: | ---: | ---: |
  | Geometric-tail mean (`prover_00006`) | 0.008s | 867.22s (14m27s) | 285.698s (4m46s) |
  | Cube tails (`prover_00004`) | 850.202s (14m10s) | about 462.57s (7m43s) | 166.359s (2m46s) |

  Geometric-tail publication then took 543.375s (9m03s), including a wait for the
  shared verifier lock; do not attribute this whole interval to compilation.
  Weighted-fibre submission (`prover_00007`) waited 1,334.006s (22m14s) before
  admission. Rounded bins submitted earlier but remained queued after that job
  was admitted, showing that admission is not ordered by arrival.

  **Confirmed implementation causes:** `verification.py` holds one
  `threading.Lock` across LeanProbe and compiled kernel-profile checks. Publishing
  a module takes the same lock. `type_profile.py` compiles the candidate in a
  fresh process, then launches another process to inspect its exported kernel
  type and axioms. Publication compiles the accepted source again and closes all
  of this verifier's warm workspaces. Workers are keyed by project and workspace,
  so prover scratch acceptance does not imply a warm independent checker.
  The roughly 581.5s beyond LeanProbe for geometric-tail verification belongs to
  the subsequent compiled-profile stage and wrapper overhead; compilation versus
  inspection is not separately timed in the successful result yet.

  `submission_cache.py` fingerprints every managed source document. Publishing
  the unrelated geometric-tail lemma invalidated cube-tail acceptance, and the
  controller started another independent check at 22:55:33 UTC. Preserve checks
  against actual imported dependencies and protected inputs when narrowing this
  cache; merely deleting the document fingerprint would be unsafe.

  **Host observation, not an isolated cause:** the 24 GiB Mac had approximately
  22 GiB of swap occupied during diagnosis. Measure active paging and individual
  checker memory before assigning a share of the delay to memory pressure or
  increasing verification concurrency.

  **Progress, 2026-09-07 (applies to runs launched or resumed after this code):**
  the controller's own submission-time check is now the acceptance check
  whenever it was closed. `submission_cache.py` fingerprints the exact candidate
  plus the transitive import closure of its file (parsed from real `import`
  commands, failing closed to every managed source on an unparseable header)
  instead of every managed document, so publishing an unrelated helper no longer
  forces "Independently checking candidate". Conditional (top-down skeleton)
  submissions are still never cached; their full check happens at promotion.
  New runs record `signature_scheme: module`: `type_profile.py` compiles the
  exact source under its real module name, retains the bytes in the
  controller-private `artifacts/` directory of the run with a SHA-256, inspects
  that same copy through a shadow package root, and `_accept_locked` publishes
  it via `LeanVerifier.install_module` only when the installed render is
  byte-identical to the checked source; any mismatch recompiles as before.
  Runs without the key keep the `legacy` scheme (fixed profile module name, so
  their recorded fingerprints stay comparable) and still recompile on
  publication. Per accepted node this removes one LeanProbe elaboration, one
  exact-source compile and one kernel inspection in the bottom-up case, and the
  publication compile in both orders for new module-scheme runs. Resumed legacy
  runs retain their original fingerprint scheme and publication compilation.
  Import layouts the conservative scanner cannot recognize invalidate against
  every managed source, rather than silently dropping dependencies.
  **Immediate timeout recovery, 2026-09-07:** resume-2 exhausted its shared
  1,200-second verification deadline during kernel inspection, after spending
  533.19 seconds on a redundant controller LeanProbe pass. Closed candidates now
  go directly through fresh exact-source compilation and trusted kernel
  type/axiom inspection. Prover feedback and skeleton diagnostics still use
  LeanProbe. The inspector enumerates the compiled module's own declarations
  instead of copying the full imported constant map, and marks its process-lived
  import environment persistent to avoid unnecessary reference-count updates.
  Compilation and inspection have separate live operation labels and recorded
  durations, including timeout results, while sharing the existing deadline.
  Legacy fingerprints, source isolation, axiom policy and final build remain
  required. Recovery evidence and regression logs are under
  `.leanflow/experiments/20260907-verification-recovery/`.
  The retained rounded-row-mean proof passed the repaired live gate in 444.5s
  (280.3s compilation, 164.2s inspection), then published in 183.6s. It is the
  sixth integrated helper. A subsequent controlled restart also fixed a
  repeat-resume bug that relabelled retained candidates as retries, and
  interruption during promotion now preserves the candidate. Older interrupted
  checks can recover their completed job's submission only if no verdict was
  recorded for that check; every recovered candidate is checked afresh.
  Not addressed here: the single verifier lock, fair admission, and a
  memory-bounded verifier pool.

  **Done when:**
  - Cache acceptance against the complete relevant source/import closure, target
    type, candidate, axiom policy and toolchain; unrelated helper publication
    must not force a repeat check. Relevant changes must still invalidate it.
  - Reuse trusted compiled artifacts and unaffected warm sessions where their
    inputs remain identical. Avoid duplicate elaboration/compilation without
    weakening source isolation, type/axiom checks or the final project build.
  - Provide fair admission and evaluate a memory-bounded verifier pool. Benchmark
    four-prover campaigns with cold and warm caches; do not assume four concurrent
    verifiers improve throughput on this machine.
  - Report queue wait, worker startup/imports, LeanProbe, exact-source compilation,
    kernel inspection and publication separately, including cache miss reasons.
    The dashboard must distinguish waiting, checking and publishing. Its outer
    operation time can exceed 20 minutes because the 1,200s active-check allowance
    starts after admission; queue time still consumes the campaign wall budget.
  - Regress unchanged-candidate reuse, unrelated and relevant dependency edits,
    import-cache invalidation, concurrent publication, queue cancellation and
    budget exhaustion. Verify representative real proofs and compare timings
    against this recorded baseline before claiming the issue is fixed.

  **Owner:** `verification.py`, `type_profile.py`, `submission_cache.py`,
  `check_process.py`, source publication, progress metrics and prover dashboard.
  Related: J02 covers initial skeleton/signature validation before prover launch.

- [ ] **J16 · P1 · Refresh a stale Lake configuration cache from the controller, not from sandboxed checks.**
  **Observed, 2026-09-07 at 01:43:58 UTC:** Spencer universal-bound run
  `spencer-universal-resume-20260906-1` stopped with `environment_error` after
  every sandboxed Lean process (LeanProbe workers, the exact-source profile
  compile and the publication compile) failed with
  `operation not permitted ... .lake/config/2/lakefile.olean.lock`. The trigger
  was external: `lake -d <project> env lean` had been run from another directory
  whose `lean-toolchain` resolved to 4.32.0-rc1, and that Lake rewrote the
  project's `.lake/config/2/lakefile.olean` (Mathlib's compiled configuration)
  with its own `leanHash`. The 4.33.1 Lake inside the sandbox then treated the
  cache as stale, needed the lock to rebuild it, and the sandbox denied the
  write; the editor's unsandboxed `lake serve` rebuilt the cache at 01:48 UTC.
  `verification.py::_lake_config_cache` still grants only the pre-`.lake/config`
  layout (`.lake/lakefile.olean*`), so nothing inside a check can recover, and
  granting `.lake/config` writes to candidate compiles would let a proof
  metaprogram plant a configuration olean that the next trusted `lake` loads.
  **Immediate recovery safeguards, 2026-09-07:** recognize this exact Lake lock
  denial, including nested compile/inspection reports, as infrastructure failure
  before scheduling a mathematical retry. Persist the candidate before stopping.
  Resume can recover an older completed submission hidden by an empty retry when
  its last independent gate failed for infrastructure reasons; it must pass a
  fresh independent check, and all spent calls and attempts remain charged.
  Emit `proof_integrated` only after the source/state transaction is durable.
  Spencer readiness checks used the project cwd and explicitly pinned 4.33.1;
  both the sandboxed version command and LeanProbe preflight passed. Evidence:
  `.leanflow/experiments/20260907-resume-readiness/`. Automatic cache repair below
  remains future work; candidate sandbox permissions were not broadened.
  **Independent Fable 5.1/max follow-up:** confirmed the legacy Spencer resume
  has no launch blocker. Reproduced and fixed its remaining concrete findings:
  a later resume must never retry an integrated node; superseded pending jobs
  become interrupted when their candidate is retained; root-level profile
  modules create their shadow directory before linking published children;
  transient profile symlinks now live outside durable run evidence so a hard
  kill cannot make resume cloning reject that evidence. The resumed live run
  exposed a related dashboard bug: pending resumptions counted as active provers.
  Extension 0.1.16 excludes them from activity and running clocks.
  **Done when:** the controller refreshes the Lake configuration cache
  unsandboxed (for example `lake env lean --version` in the project) at
  preflight and once more when a check reports this exact lock denial, then
  retries that stage instead of ending the campaign; the retained candidate and
  budget accounting stay unchanged; the terminal cause names the external
  toolchain when detection is possible. Never grant `.lake/config` writes to
  sandboxed candidate processes. Test with a deliberately stale cache.
  **Owner:** `check_process.py` preflight, `verification.py`, campaign stop
  reasons. Related: J15 evidence directory.

## Evidence and verification discipline

Runtime files above are under `leanflow_cli/workflows/prover/`; extension files
are under `vscode-extension/`. Shared computation code is under `tools/utilities/`.

Current campaign evidence:

- `testdata/workflow_projects/BeckFialaResearch/.leanflow/workflow-state/prover/prove-vscode-run-mtpu8tot-isdk/`
  contains `state.json`, `PLAN.md`, `DAG.json`, and job events/results.
- `.leanflow/experiments/20260906-readiness-selection/beckfiala-monitor.json`
  records observations made while the campaign runs.
- [Spencer universal-bound events](../testdata/workflow_projects/SpencerResearch/.leanflow/workflow-state/prover/spencer-universal-resume-20260906-1/events.jsonl)
  retain operation timestamps and submission results for J02/J15. In that run,
  `jobs/prover_00004/` and `jobs/prover_00006/` retain the corresponding candidates
  and job evidence; live `state.json` is a changing snapshot, not a frozen baseline.
- The same evidence directory contains `beckfiala-launch.json`,
  `loogle-diagnostic.json`, `loogle-query.json`, and
  `materialization-import-mismatch.json`.
- [Budget contract](prover-budget-contract.md) and
  [readiness record](prover-readiness-selection.md) cover the earlier audit.

Interactive graph support already exists in commit `91f89a7` (extension 0.1.11).
Do not reopen “add a graph” as missing work; D04 concerns which planning state the
graph can display. Existing plan, event, result, source, and diff features should
be improved rather than treated as absent.

Before each fix, reproduce its specific gap and check for intervening changes.
Use focused regression tests for new behavior. Recheck the relevant dashboard
state with a real run. Performance work must preserve verification guarantees.
Keep active campaign source and generated proof files out of unrelated commits.
