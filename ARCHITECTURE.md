# LeanFlow architecture

LeanFlow is a Lean-first orchestration and verification kernel around three
native agent runtimes: Codex, Claude Code and Lemex. Native runtimes own model
requests, tools, authentication, retries, sampling, history and compression.
LeanFlow owns workflow scheduling, global accounting, job isolation, independent
Lean acceptance, canonical source transactions and persistent evidence.

## Layering and entry points

`core/` depends on no higher layer. `agent/` and `tools/` build on core;
`leanflow_cli/` composes those pieces into workflows. Add cohesive leaf modules
rather than expanding legacy monoliths.

- `leanflow` → `leanflow_cli.main:main` and `workflow.py`.
- `prove` / `autoprove` → `leanflow_cli.workflows.prover.runtime`.
- `formalize`, `draft`, `review`, `refactor`, `golf` →
  `leanflow_cli.native.harness_workflow`.
- `leanflow-agent`, direct `run_agent` CLI use and old auxiliary request entry
  points return migration errors. The in-process `AIAgent` / `native_runner`
  engines, direct SDK adapters, credential readers, model routing, and custom
  compression are removed. Their independent Lean/state helpers remain in leaf
  modules with direct tests. Generic delegation no longer registers a tool; the
  selected native runtime owns its own agent execution.

Web retrieval and historical session search return bounded verbatim excerpts.
There are no hidden summarizer, skill-audit or smart-approval model calls.
The direct auxiliary theorem-advisor tools are removed from tool discovery.

## Native runtime boundary

`agent/runtime/harness/` owns typed requests/results, native command construction,
structured output parsing, cumulative-usage normalization, session resume,
process-tree cleanup and OS isolation. `settings.py` records per-invocation model
and reasoning metadata from Codex/Lemex startup diagnostics and Claude control/response
envelopes, keeping requested overrides separate. `metadata.py` owns bounded read-only
stdio metadata transport (`runtime/provider_protocol.py` preserves its editor imports);
`model_defaults.py` queries native model/list only for an unreported session effort,
labelled distinctly as a model default. No metadata query starts a model turn.
A complete native process invocation is
one LeanFlow agent call. Internal model requests and compaction remain native.
`observations.py` projects emitted message/reasoning blocks, plans and native tool
starts/results into separate `agent-*` observations, never independent acceptance
or additional call accounting. `redaction.py` removes exact environment credentials
and recognizable secrets before transcripts reach log files or callbacks. Prover
jobs persist complete observations under stable evidence IDs; the shared stream
carries bounded previews for `runs event` to expand on demand.

`leanflow_cli/runtime/runtime_provider.py` selects exactly `codex`, `claude` or
`lemex`. It does not read/copy credentials or translate external endpoint names.
`env_loader.py` preserves exported native variables and refuses to import legacy
provider credentials from LeanFlow dotenv. `config.py` detects old schema settings
and requires explicit migration without rewriting them.

`runtime/lemex_install.py` adopts an installed self-contained Lemex distribution
into a content-hashed immutable runtime, or installs a pinned source revision in
a private prefix. It preserves the global executable and native home. The native
resolver and doctor share that executable lookup.

Native tools run only in filtered project snapshots under macOS seatbelt or Linux
Bubblewrap; unsupported isolation fails closed. The outer OS sandbox is the
filesystem authority. On macOS the inner Codex/Lemex sandbox is disabled inside
that already enforced boundary because nested seatbelt creation is unsupported.
No unconstrained fallback is permitted. Native home/session/auth state remains
native; runtime configuration is protected from tool writes. Unrelated MCP,
hooks, plugins and project-local runtime settings cannot import extra authority.
General runs permit network access. `harness/network_policy.py` resolves clean-room
and offline settings into local-only research. `harness/restricted.py` disables
native research tools and prior native context; restricted invocations are ephemeral.
The model connection retains native authentication and defaults, while controller
bridge commands run with OS-enforced network denial. `harness/context_policy.py`
checks automatic context boundaries. Saved controller research provenance permits
clean-room checkpoint resumes and rejects online or legacy context reuse.
An explicit import-only prover
target reports how to select the actual source file
or the whole project; imported files are never silently added to its proof scope.

## Core primitives

- `core/home.py`: sole `LEANFLOW_HOME` authority.
- `core/campaign_limits.py`: optional global invocation, reported-token and active
  time ledger for non-prover workflows; reserves calls atomically, retains
  interrupted charges and refuses unknown token usage under a token cap.
- `core/deadlines.py`: optional monotonic deadlines, remaining time and
  operation-local cancellation. `None` is unlimited; zero is exhausted.
- `core/error_redaction.py`: deterministic error sanitation without provider code.
- `core/file_copy.py`: isolated copy-on-write files and atomic directory publication;
  unsupported filesystems retain checkpoint space before ordinary copies.
- `core/state.py`: historical session storage.
- `core/model_tools.py`, `toolsets.py`, registry and top-level compatibility shims:
  deterministic tool discovery and registration.
- Process identity, filesystem, resource admission, verified-edit authority,
  runtime flags and atomic utility modules remain dependency-light leaves.

## Prover execution

```text
workflow resolution
  → prover.runtime: discovery or native-version resume
  → research: fresh outline → graph construction → independent review
  → deterministic theorem-DAG scheduler
  → agent_session: private snapshot + job-bound MCP + one native invocation
  → independent LeanProbe feedback and strict compiled kernel inspection
  → controller source transaction
  → requested-scope placeholder check + project Lake build
```

Only the controller changes canonical sources and PLAN/DAG state. Top-down
candidates remain untrusted until prerequisites close and strict acceptance
passes. Bottom-up scheduling checks prerequisites first. Model reports never
establish proof or disproof.

| Prover leaf | Responsibility |
| --- | --- |
| `config.py` | Three native backends, optional global limits and Lean policy |
| `research_policy.py` | Persist local-only policy and reject reuse of online or unclassified campaign history |
| `runtime.py`, `controller.py` | Campaign lifecycle, shared progress and scheduling |
| `allocation.py`, `role_limits.py`, `usage.py`, `live_progress.py` | Durable invocation admission, campaign session limits per role, usage and active elapsed time |
| `agent_session.py` | Native invocation and same-session submission feedback |
| `native_snapshot.py` | Atomic filtered copy-on-write snapshots; release finished-session dependency caches and restore pinned revisions on resume; no escaping symlinks or shared inodes |
| `native_bridge.py`, `mcp_relay.py` | Authenticated job capability; durable start/poll/cancel operations and acceptance receipts |
| `native_command.py` | Long commands confined to the snapshot with cancellation and process-tree cleanup |
| `session_tools.py`, `session_search.py`, `session_research.py`, `session_compute.py` | Deterministic role-scoped tools without model routing |
| `source.py`, source transaction modules | Exact hole replacement, protected bytes, journal recovery and conflict detection |
| `planning_controller.py`, proposal/review modules | Plan validation, helper materialization and branch repair |
| `check_process.py`, `check_worker.py`, `native_check.py` | Isolated warm LeanProbe with the sole global execution deadline |
| `lean_environment.py`, `submission_cache.py` | Local dependency freshness and controller-owned exact-input acceptance reuse |
| `verification.py`, `type_profile.py` | Independent exact-source compilation, complete axiom profiles and protected kernel types |
| `negation.py`, `negation_job.py`, `negation_submission.py` | Exact negation construction, feedback and independent certificate checks |
| `recovery.py`, `recovery_reports.py` | Evidence-preserving continuation and reconstruction under global limits |
| `libraries.py` | Pinned additive Lake dependency updates with rollback |

Submission operations belong to the controller, not the native process. Native
process completion drains pending operations before final response handling;
controller shutdown cancels and joins them. A disconnected MCP client does not
cancel its check. Receipts bind exact source hashes and job revisions.

Original proof source is immutable outside authorized holes. The controller
captures kernel target types before planning and compares them after proposed
imports. Local fixed definitions are included in dependency fingerprints.
Explicit missing-definition authorization permits only those holes. Compiler output is copied into a separate inspector workspace before trusted
inspection code is created; compiler descendants never receive write authority
over that workspace. Checker reads exclude private controller state, with exact
capabilities for the current scratch and retained artifact. Checked
compiled artifacts are retained for publication; source transactions hold the
controller lock until state commits. Concurrent external edits fail closed.

## Other native workflows

- `native/harness_context.py` loads selected skills and copies prepared source
  context into native job authority.
- `native/harness_workflow.py` owns resumable native execution, optional global
  limits, MCP submission, source review and final promotion.
- `native/harness_observer.py` publishes native process ownership, heartbeats,
  global accounting and bounded MCP activity through the existing status API.
- `native/harness_artifacts.py` freezes proposed Lean/blueprint bytes in a second
  copy built from canonical dependencies, independently builds and inspects
  declarations, preserves original types and non-proof definitions, and promotes
  only an accepted receipt after rechecking canonical identity and cancellation.
- `native/harness_formalization.py` runs a fresh native session for mathematical
  source fidelity, validates a strict PASS/BLOCK schema and stores review evidence
  bound to source/proposal hashes. It shares the campaign ledger.
- `formalization/` retains TeX/PDF intake, source extraction and blueprint setup.

Formalization permits literal theorem proof holes only. Construction placeholders
block acceptance. The independent reviewer checks complete source coverage and
meaning; the controller checks elaboration and allowed axioms. Approved drafts
return `draft_ready`; proof completion requires a separately started `prove` run.
Review workflows publish findings only.

## Project setup

- `workflows/project_setup/` is the shared project preparation boundary. `scaffold.py`
  classifies and adopts source folders with pinned new-project dependencies;
  `verification.py` prepares compatible REPL dependencies and checks the real
  verification protocol; `process.py` streams owned, cancellable subprocesses;
  `service.py` drives phases and preserves source/version boundaries; `state.py`
  stores home-owned receipts and invalidates stale readiness. `readiness.py` checks
  installed artifacts and source import locations without starting Lean; unchanged
  verified setups reuse the receipt, while explicit repair bypasses that cache.
- `workflows/project.py` owns project discovery and registration. Retired clone and
  power-mode setup helpers are removed; CLI aliases use the shared pipeline.
- `cli/project_command.py` routes setup/init/create through that pipeline, emits
  human or NDJSON progress, and maps signals to cooperative cancellation. CLI and
  interactive shell use the same implementation and readiness result.
- `runtime/project_operations.py` holds canonical-path shared workflow / exclusive
  setup locks outside the project. It also checks legacy controller locks and
  live process identities, protecting already running workflows. Prover, native
  workflows and formalization intake take shared ownership before project writes.
- VS Code `src/core/projectSetupTypes.ts`, `projectSetupProcess.ts` and
  `projectSetup.ts` define the receipt contract, streaming process boundary and
  workspace-scoped service. `workspaceUris.ts` selects supported local or remote
  filesystem URIs. `ProjectSetupCard.tsx` presents the same setup state in sidebar
  and dashboard, with retry, cancellation, bounded inline live logs and separate
  dependency repair. Registration is distinct
  from readiness; provider authentication remains under its native setup service.

## Cache ownership and freshness

`runtime/dependency_cache/{identity,checkout}.py` and its public facade own exact
dependency compatibility, trusted canonical publication and private writable CoW
materialization. Shared entries are sealed; no writable shared symlink or hardlink
is exposed to projects. Root proof artifacts never enter this store. Project
setup publishes only after readiness, and snapshots keep their existing clean-room
source filter. Missing compiled artifacts are never published as a ready entry.

`runtime/storage/{policy,leases,entries,maintenance,workspaces,process_views}.py`
owns managed entry limits, cross-process leases/transactions, safe LRU eviction,
orphan process protection and abandoned view recovery. Only marked managed entries
or exactly registered dependency views are collectible; proofs/checkpoints remain
outside the deletion scope. Defaults are a 20 GiB soft target and 10 GiB reserve.
`prover/storage_admission.py` guards dispatch and quiet controller polling;
storage pressure takes the existing durable cancellation/checkpoint path.
`cli/storage_command.py` exposes inspection, cleanup and explicit publication.

`runtime/runtime_install.py` hashes source inputs, claims a fresh versioned Python
environment and atomically switches the validated wrapper/install metadata.
CLI and detached controller entrypoints lease their interpreter/source for their
lifetime. The installer never upgrades a live environment or healthy MCP backend
in place. Old unmarked installations are preserved conservatively.

`vscode-extension/src/core/{storage,storageState}.ts` owns host-side storage actions
and metric validation; `webview/views/StorageCard.tsx` presents the shared policy
and cleanup action through the existing typed message boundary.

`scripts/lean_imo_campaign/storage.py` keeps campaigns on the exact installed
interpreter under active PID leases, records global dependency identity in
source-only baselines, restores private cell views and releases them only after
durable result evidence. Historical replay validates retained or reconstructed
inputs before dispatch; no private Python environment is cloned per campaign.

Native runtimes own prompt caching, session history and compaction. LeanFlow
resumes the explicit native session and preserves its home; it does not inject
cache keys or ephemeral sessions. The job MCP namespace persists across controller
restarts, and tool schemas/configuration are ordered consistently. Reported cache
reads, writes and reasoning subtotals survive normalization without being counted
twice. Claude accounting uses its complete cumulative `modelUsage` totals rather
than the partial main-loop `usage` field. Missing totals or resume baselines remain
unknown.

Lean acceptance reuse is separate: only the controller can populate it, and only
with an independently accepted result. Keys bind candidate bytes, protected types,
axiom policy, managed import closure and the canonical Lean environment. A matching
result is consumed once; disk receipts cannot restore this in-memory cache.
Dependency changes invalidate reuse and restart affected warm Lean workers.
Generic workflow receipts also bind the canonical environment before verification
and recheck it before publication.

The environment generation covers unmanaged Lean sources, compiled dependencies,
toolchain and loader configuration. It uses local filesystem identity, size and
nanosecond modification/change timestamps rather than rereading large library
binaries. This assumes filesystem changes are visible through normal local stat
metadata. Dependency writers must serialize updates with publication; changed
source libraries still require their normal Lake rebuild. Independent final
verification remains the completion gate.

## Accounting and persistence

There is no default per-response, per-job or Lean-check timeout. Unset global
calls/tokens/time are unlimited; zero is exhausted. Process creation reserves a
call before dispatch and refunds only a proven non-start. Native-reported token
usage is normalized; absent usage is unknown. A token ceiling prevents later
admission and can overshoot during a running invocation.

Active elapsed time includes queues, tools and verification, survives resumes and
is shared across parallel jobs. Heartbeats persist it during silent native work.
Offline time is excluded. Cancellation/deadlines terminate owned process trees,
stop controller operations and prevent source promotion.

Prover state remains `.leanflow/workflow-state/prover/<run-id>/`: plan, graph,
protected baselines/checkpoints, jobs, transaction journals, events, receipts and
addressed guidance. Runtime-private ledgers and accepted evidence stay outside
agent write authority. Version-2 resume retains spent limits; legacy direct API
conversations require a new native run.

Other workflows hold an exclusive controller lock under
`.leanflow/native-workflows/<run-id>/` with native session
identity, `state.json`, `limits.json`, proposal checks, accepted source and source
review receipts. Canonical conflicts reject resume/publication. All durable JSON
uses atomic replacement.

## CLI, Lean services and tool modules

`cli/` owns command handling, doctor, flags and exact-run observer APIs. `workflow.py`
resolves scope, skills, backend and the `LEANFLOW_NATIVE_*` environment. Preview
performs no provisioning or paid launch. `flags/` describes supported controls;
editor profiles cannot inject unlisted environment settings.

`lean/` owns diagnostics, goals, declaration inspection, search, LeanProbe/LSP
integration and backend compatibility. `tools/implementations`, `tools/utilities`,
`tools/mcp` and `tools/environments` retain deterministic tool and transport
services. The native job's MCP schemas are explicitly selected; registry breadth
does not grant extra job authority. Existing Lean command defaults in standalone
editor utilities are separate from the native campaign execution deadline.

`run_metrics`, `run_stream`, `run_snapshot`, `run_history` and provenance leaves
bind observations to exact run IDs. Immutable evidence and terminal exit identity
are required for complete metrics; missing usage remains unscored. A digest
supports corruption detection, not protection against the filesystem owner.

## VS Code Extension

`vscode-extension/` is a workspace extension over the public CLI contracts; it
does not read or reinterpret LeanFlow's persistence files directly:

- `src/core/cli.ts` owns JSON command execution, strict output parsing, and the
  workflow launch boundary. The host passes only catalogued
  `LEANFLOW_*` overrides and a minted run id. Native credentials are injected
  separately immediately before spawn, never included in persisted overrides.
- `src/core/cliHealth.ts` distinguishes installation failures from temporary CLI
  failures, coalesces checks and retries with bounded backoff. `services.ts`
  serializes full refreshes and retains catalogs and readiness during transient
  failures; `webview/CliNotice.tsx` keeps job navigation available while reconnecting.
- `src/core/providerCredentials.ts` stores explicit native keys and the Lemex
  endpoint in VS Code SecretStorage, scoped by extension host authority; a missing
  override preserves the native environment and login. `providerSetup.ts` owns
  password prompts, native login terminals, and credential-free status checks via
  `leanflow provider --json --check` (`runtime/provider_setup.py`). Native homes
  and auth files are neither copied nor modified. `ProvidersView.tsx` exposes the
  setup actions without handling secret values; remote setup executes on the
  workspace host. Presence checks do not validate paid API access.
- `runtime/provider_models.py` reads native model catalogs and resolved defaults
  through Codex/Lemex app-server metadata and Claude's SDK control messages.
  `runtime/provider_protocol.py` bounds and reaps these metadata-only subprocesses;
  it sends no user prompt or inference request. Provider checks run concurrently.
  `src/core/providerModelMetadata.ts` bounds and redacts catalog fields at the
  editor boundary. `src/core/providerModels.ts` mirrors role inheritance and
  validates model-specific reasoning choices; `NativeModelControls.tsx` renders
  those choices without a second hardcoded model or effort list. Unknown capability
  metadata stays distinct from a model that reports no configurable reasoning.
  The extension caches the snapshot until setup refresh or credential changes.
- `src/core/agentActivity.ts` selects a bounded narrative for an exact run/job.
  `webview/views/AgentActivity.tsx` shows it beside each prover job, with emitted
  reasoning optionally visible and complete output expanded through the existing
  per-event evidence cache. Missing reasoning is labelled as unreported.
  Agent-log navigation uses the recorded agent identity and clears stale search
  filters. Launch displays the resolved default backend from provider metadata.
- `src/core/workflowTerminal.ts` prefers a private tmux server per job with an
  inherited launch environment and retained exit status; it falls back to a regular
  integrated terminal before dispatch, or after a confirmed failed session creation.
  Ambiguous dispatch is observed without launching a second copy. No credentials
  enter tmux arguments/config files. Reattachment never starts another workflow.
- `src/core/runManager.ts` persists a run identity before launch and reconciles
  exact runtime status, immutable results and owned tmux pane exits. It releases
  finished private panes only after persisting their outcome, keeps cached history
  through read failures, and follows external live ids independently of the newest
  history stream. Every Stop uses the CLI's identity-validated `runs stop`.
- `src/core/runCatalog.ts` merges tracked jobs, verified external owners and saved
  history without duplicate ids or worker streams. `RunMenu.tsx`, `SidebarView.tsx`
  and `runsTree.ts` share that catalog; exact selection applies to details and logs.
  `services.ts` coordinates clicked-job navigation across both webview surfaces.
- `src/core/runOwnership.ts` is the pure owner-adoption and terminal-state
  policy; `runStatus.ts` shares outcome labels with the browser. `src/core/runSelection.ts` prevents stopped tracked rows from masking
  an active project's live status in both the host and browser webview, and
  identifies verified external owners for bounded live polling.
  `src/core/projectDiscovery.ts` makes nested-manifest selection explicit and
  refuses ambiguous parent workspaces; `project.ts` performs the filesystem and
  symbolic-link checks before adopting that root.
  `src/core/launchPaths.ts` contains manual target and path-like skill containment
  checks, including realpath checks for symbolic-link escapes.
- `src/core/prover.ts` validates exact-run snapshots and produces bounded DAG
  tree rows. `src/core/proverGraph.ts` lays out unique theorem nodes and directed
  dependency edges, including shared/disconnected components, and derives live
  proof states without equating candidates with verified results.
  `webview/views/ProverGraph.tsx` renders the default interactive graph with
  zoom, search, keyboard selection, and a status legend; geometry stays stable
  when proof statuses change. `webview/views/ProverWorkspace.tsx` shows the plan, dependencies,
  jobs, usage, file links/diffs, and queued guidance; `ProverSettings.tsx` exposes
  catalogued launch controls, grouped by role with independent model and reasoning
  effort selectors. `GlobalLimits.tsx` keeps global calls, reported tokens, and
  active time visible above role settings, converts entered minutes to seconds,
  and preserves explicit unbounded overrides and profile restoration.
  `webview/InfoHelp.tsx` supplies accessible disclosure controls with help rendered
  outside scrolling panels; `helpPosition.ts` clamps it to the viewport. `help.ts`
  shares term definitions across launch and live views. `src/core/limitDisplay.ts`
  identifies optional limits for consistent ∞ displays, preserving zero and unknown
  usage. Agent-call counters count native processes; activity events remain distinct.
  The host uses `runs prover` / `prover-message` / `runs event`,
  validates ownership and paths, and does not substitute another run's artifacts.
  `proverProgress.ts` and `proverOperations.ts` derive budget, capacity, operation,
  proposal and stop-reason views from the same snapshot. `proverModelSettings.ts`
  resolves saved role inheritance and per-invocation evidence without substituting
  current provider defaults for missing historical model names. `ProverModelHistory`
  displays confirmed values, evidence sources and settings changes across resumes. `proverCache.ts` and
  `proverService.ts` cache exact-run CLI reads with file-stat change detection;
  `idleDiscovery.ts` detects external owners even when no run is selected.
  `profileSurfaces.ts` checks profile compatibility before launch.
  The workspace delegates to focused `ProverBudget`, `ProverController`,
  `ProverDag`, `ProverPlan`, `ProverJobs`, `ProverChanges`, and `ProverGuidance`
  views; `proverFormat.ts` owns their formatting.
- `src/core/eventBuffer.ts` deduplicates and bounds host-side event tails. When
  eviction occurs the host sends an explicit reset rather than an append, so a
  long-running workflow cannot grow the webview's retained stream without
  bound.
- `src/core/storageSecurity.ts` contains the realpath and no-symlink checks used
  for profile reads, writes, replacement, deletion, and path opening.
- `src/core/experiments.ts`, `experimentIsolation.ts`, and
  `experimentMatrix.ts` own research sweeps. A sweep freezes its launch request
  and profile catalog, records a clean Git baseline, randomizes execution order,
  and runs each condition in a private detached local clone with a private
  dependency/build tree. The source checkout is never reset or cleaned.
- `src/core/experimentScoring.ts` accepts only versioned, exact CLI metrics with
  immutable final evidence. Missing, partial, or mismatched evidence produces
  an explicit unscored cell; terminal cells are not silently rerun.
- `src/webview/stats.ts` owns descriptive sample statistics, Student-t intervals,
  and Welch comparisons. The UI reports absent variance as absent, not zero.

The extension host is the trust boundary. Every webview message is validated at
runtime before it reaches filesystem or process APIs; modules included in the
webview bundle may import host types but not host runtime code. Workspace Trust
and virtual-workspace declarations fail closed because LeanFlow executes the
workspace's local toolchain and requires a real filesystem checkout.
Clipboard copies also pass through this boundary to VS Code's clipboard API,
with bounded text and visible success or failure feedback. Interaction tests in
`test/webviewButtons.test.mjs` exercise every webview button definition;
`test/webviewHost.test.mjs` checks every host action with disposable files and
inert process, credential, and editor services.

## Runtime contracts and development

Skills and workflow specs are packaged runtime guidance. Native jobs receive the
selected skill and equivalent MCP/native tool names. Deterministic source and
verification gates take precedence over model assertions.

Retired module entrypoints return actionable migration errors; shared helpers are
tested directly in their owning modules. New features use the native harness boundary. Public native backend names,
result/session/usage contracts, job-bound receipts, the single state-home
authority and exact source acceptance are load-bearing interfaces.

Add characterization tests before changing coupled code, keep new leaf modules
in the mypy gate, and run Black, Ruff, mypy and the full suite. See `AGENTS.md`.

The repository experiment tooling in `scripts/lean_imo_campaign/` uses native
Codex/Lemex conditions, immutable runtime snapshots and explicit optional global
limits. Version-2 manifests reject legacy API-budget experiments. Its dispatch,
recovery and immutable evidence remain separate from product workflow scheduling;
see `docs/lean-imo-campaign.md`.
