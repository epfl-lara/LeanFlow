# LeanFlow Architecture

This document is the maintainer map for LeanFlow's current runtime. It records
the package boundaries, principal execution paths, compatibility surfaces, and
invariants that must survive implementation changes. User-facing behavior is
documented in `README.md` and `docs/product-reference.md`.

## Design Boundaries

LeanFlow is a Lean-first automation kernel. Its dependency direction is:

```text
core
├── agent
├── tools
└── leanflow_cli
```

- `core/` owns dependency-light runtime primitives and must not import from the
  higher layers.
- `agent/` owns provider conversations, prompt assembly, context management,
  tool execution, and accounting.
- `tools/` owns model-callable capabilities and deterministic safety guards.
- `leanflow_cli/` composes the agent and tools into Lean workflows, persistent
  project state, provider routing, and shell commands.

New behavior belongs in the smallest cohesive leaf module. Do not add new
responsibilities to `run_agent.py` or
`leanflow_cli/native/native_runner.py` when a focused collaborator can own
them.

## Entry Points

- `leanflow` → `leanflow_cli.main:main`
- `leanflow-agent` → `leanflow_agent:main`
- `run_agent.AIAgent` → the provider conversation loop used by managed
  workflows
- `leanflow_cli.workflows.prover.runtime` → the dedicated standard/research
  `prove` / `autoprove` controller
- `leanflow_cli.native.native_runner` → existing formalization, drafting,
  review, and editing workflows

The shell resolves a shared provider environment and launches a managed child
process. Proving composes `AIAgent`'s provider adapters without entering its legacy
conversation loop; `agent_session.py` owns a separate admitted-request loop.
Other workflows retain `native_runner`, its queue compatibility modules, existing
verification guards, and checkpoint behavior.

## Repository Layout

```text
LeanFlow/
├── core/                 # shared runtime kernel and compatibility authorities
├── agent/                # model conversation collaborators
├── tools/                # model-callable tools and deterministic guards
├── leanflow_cli/         # CLI, Lean services, workflows, runtimes, persistence
├── leanflow_skills/      # packaged prompt-time Lean guidance
├── leanflow_specs/       # packaged workflow and worker contracts
├── evals/                # frozen evaluation harness and adversarial fixtures
├── vscode-extension/     # editor front end over the CLI's JSON surfaces
├── testdata/             # deterministic Lean workflow fixtures
├── tests/                # unit, integration, installer, and contract tests
├── run_agent.py          # AIAgent compatibility surface and core loop
├── leanflow_agent.py     # leanflow-agent entry shim
└── README.md             # product overview and quick start
```

Local campaigns and their logs belong under ignored project state or an
external results repository, never in this source tree. The repository-level
`artifacts/` directory is ignored for this reason.

## Core

`core/` contains primitives that are shared across the agent, tool, and CLI
layers:

- `home.py` is the only authority for `LEANFLOW_HOME` and `~/.leanflow`.
- `state.py` owns the SQLite conversation/session store.
- `model_tools.py` and `toolsets.py` expose tool discovery and toolset
  selection.
- `process_identity.py` provides token-backed process, process-group, and
  session ownership checks.
- `provider_availability.py` and `provider_capacity.py` coordinate provider
  recovery and bounded background actors.
- `project_resource_admission.py` coordinates resource-heavy Lean work.
- `runtime_modes.py` centralizes process-scoped runtime flags.
- `verified_edit_authority.py` carries single-use, hash-bound authorization
  between managed orchestration and atomic patch tools when prior Lean evidence
  proves one exact source transition.
- `filesystem.py`, `time.py`, `constants.py`, and `utils.py` provide shared
  dependency-light utilities.

The top-level `model_tools.py`, `toolsets.py`, and `utils.py` modules are
compatibility shims over `core.*`.

## Agent Runtime

`run_agent.AIAgent` retains the public conversation-loop surface. Its
collaborators are grouped by responsibility:

- `agent/providers/` — primary and auxiliary provider routing, Codex Responses,
  Anthropic adaptation, isolated auxiliary calls, retries, and model metadata
- `agent/prompting/` — system prompts, prompt caching, reasoning normalization,
  and provider-response normalization
- `agent/compression/` — conversation persistence, context compression, and
  provider-aware summary handoff
- `agent/execution/` — tool batches, interrupts, command safety, skill
  commands, resource handoff, and model-facing projection of successful tool
  payloads while the manager and audit log retain the complete raw result
- `agent/accounting/` — token/cost accounting, redaction, and error logging
- `agent/display/` — terminal rendering and structured log formatting
- `agent/runtime/` — managed-run contracts, trajectory capture, and workflow
  events

`agent/execution/collaborator_resolvers.py` preserves lazy construction and
test patch points for these collaborators. Changes to a collaborator must
retain the corresponding `AIAgent` wrapper or compatibility property unless
the public surface is intentionally migrated.

## Tool Runtime

Tools self-register through `tools/registry.py`. Their schemas and normalized
results flow through `core.model_tools` and `tools/response.py`.

- `tools/implementations/` contains model-callable Lean, file, terminal,
  document, web, repository, delegation, memory, skill, and empirical tools.
  `lean_have_extraction.py` owns the transactional local-`have` promotion tool;
  its source parser lives in `leanflow_cli/lean/lean_have_extraction.py`.
- `tools/utilities/` contains deterministic guards and reusable implementation
  support, including process ownership, transcript protection, repository
  research policy, scratch-terminal policy, verified patch parsing, helper
  admission, daemon-backed wall-clock boundaries for blocking backends, and
  bounded authoritative source context for Lean advisors.
- `tools/mcp/` contains MCP configuration, schema shaping, transport, sampling,
  and managed-server lifecycle behavior.
- `tools/environments/` contains the local, SSH, Singularity, Daytona, and
  persistent-shell execution backends.

Tool discovery depends on import-time registration. Adding a tool requires
updating every applicable discovery list and toolset, plus tests that prove the
tool is reachable through the public registry.

## CLI and Workflow Runtime

`leanflow_cli/` is organized by responsibility:

- `main.py` and `cli/` own argument parsing, shell commands, status rendering,
  doctor checks, MCP bootstrap, and expert-help configuration.
  `cli/flags_command.py` and `cli/runs_command.py` are stable JSON surfaces for
  external observers; the only mutating run operation is `runs stop`, which
  delegates to verified process-identity interruption. `cli/run_metrics.py` is
  the stable metrics facade. `cli/run_stream.py` verifies hot/retained streams
  and aggregates usage; `cli/run_snapshot.py` seals launch and post-quiescence
  result artifacts plus a separate corruption-detection digest; and
  `cli/run_history.py` reconstructs list summaries across hot, final, and
  retained evidence. They never combine a selected stream with project-global
  latest state. `cli/run_provenance.py` assembles identities from focused
  project/source (`run_source_identity.py`), Lean/Lake build configuration
  (`run_build_identity.py`), runtime/config/skill
  (`run_runtime_identity.py`), interpreter/import/provider-package identity
  (`run_python_identity.py`), terminal and ambient-process controls
  (`run_terminal_identity.py`), digest-only persistent prompt inputs
  (`run_prompt_identity.py`), and canonical validation
  (`run_evidence_validation.py`) leaves. Exact metrics
  require a terminal run stream, matching immutable stream hash/count and exit
  evidence, a canonical redacted launch-environment digest, and complete
  content-addressed source, ignored project configuration/guidance, selected
  skill, LeanFlow/Python runtime, behavior-config, toolchain, and dependency
  provenance. The digest detects accidental edits but is not a hostile-owner
  signature. Retained streams go through the strict archive audit; `runs list`
  also binds hot/final integrity and result-limit truncation, and any missing
  coverage fails closed. A catalog-free state is complete-empty only when no
  retained archive, shard, or temporary evidence exists.
  `runs_command.py` remains the argparse/JSON/terminal facade and scopes and
  restores any `LEANFLOW_PROJECT_ROOT` it sets for an explicit
  `--project`, because the module outlives one command inside a test session or
  a shell.
- `flags/` declares the `LEANFLOW_*` knob catalog: name, type, default, group,
  provenance, and whether a knob is worth an ablation. It is descriptive only —
  no runtime reads it to decide behavior. The `research` profile is derived from
  `workflows/research_mode.py` rather than restated, and
  `tests/leanflow/test_flag_catalog.py` fails on a catalogued name no runtime
  module reads.
- `workflow.py` resolves workflow requests, providers, toolsets, and the
  `LEANFLOW_NATIVE_*` child-process environment contract. `preview=True`
  resolves the same plan without provisioning side effects (local Loogle warmup,
  formalization document intake) so `--dry-run` and editor forms can resolve a
  plan repeatedly; `launch_plan_payload` serializes it with credentials redacted.
- `runtime/` owns provider credentials/routing, file locks, sandbox execution,
  branding, environment loading, and built-in skill discovery.
- `lean/` owns diagnostics, goals, declaration inspection, incremental checks,
  automation, proof context, premise search, axiom checks, ephemeral
  validation, target-owning verification-path resolution, and the typed
  `LeanBackend` facade. Tactic-hole portfolios route
  through `lean_attempt_screening.py`, which prepares the target environment
  once and exact-checks bounded candidates with LeanProbe before any positional
  LSP fallback.
- `formalization/` owns source-document extraction, TeX discovery, generated
  Lean shaping, and the statement-review handoff.
- `workflows/prover/` owns the active bounded proof workflow; see its leaf map
  below. Other modules in `workflows/` retain native queues, compatibility
  planning/research helpers, verification transactions, and persistence.
- `native/` owns the existing non-prover managed workflow process,
  startup/resume reconciliation, source transactions, checkpoints, and shutdown.
  Its former proof-queue and advisory modules remain compatibility code; they
  are not the new prover's scheduler or budget authority.

### Bounded prover leaf map

| Module | Responsibility |
| --- | --- |
| `workflows/prover/config.py` | Per-role model/context settings, finite campaign/pass limits, and the optional no-internet boundary (local source search and computation remain available) |
| `workflows/prover/models.py` | Typed nodes, prerequisite DAG validation, revisions and fingerprints |
| `workflows/prover/source.py` | Comment-aware hole discovery, frozen source, scratch projection, exact replacements and typed source-consistency failures |
| `workflows/prover/source_transaction.py` | Proof and multi-file materialization journals, exact before/after images, and conflict-preserving recovery |
| `workflows/prover/scheduler.py` | Deterministic DFS selection, concurrency leases and shared-dependency deduplication |
| `workflows/prover/planning.py` | Structured plan proposals, immutable original statements and generated helper placement |
| `workflows/prover/runtime.py` | Sole source/plan/DAG authority, job admission, completion handling, recovery and resume lineage |
| `workflows/prover/entrypoint.py` | CLI startup, controller locking, terminal startup-failure publication and new-run resume cloning |
| `workflows/prover/job_controller.py` | Job workspaces, durable handoffs, request reservation, submission feedback, and separately accounted resource jobs |
| `workflows/prover/planning_controller.py` | Fresh planning/review stages, helper-materialization transactions with removable dependency imports, source-conflict recovery without mathematical replanning, and concurrent resource batches |
| `workflows/prover/materialization_imports.py` | Dependency-ordered recompilation of changed or missing helper artifacts before signature checks; journaled restoration of previous artifacts on rollback |
| `workflows/prover/store.py` | Atomic snapshots, PLAN/DAG publication, baselines, events and guidance inbox |
| `workflows/prover/live_progress.py` | Independent progress lock, bounded operation lifecycle, two-second heartbeat and metadata snapshots that retain the last committed source checkpoint |
| `workflows/prover/stop_reason.py` | Separate job, scheduler and campaign stop reasons with remaining capacity and unresolved obligations |
| `workflows/prover/submission_cache.py` | Single-use, in-memory reuse of parent-verified closed submissions only when exact candidate, mutable sources, dependency trust, protected type and axiom policy remain unchanged |
| `workflows/prover/check_failures.py` | Nested check-result classification that preserves accepted plans when compilation or kernel inspection fails for infrastructure reasons |
| `workflows/prover/observer.py` | Existing CLI activity/live-status bridge and terminal exit mapping |
| `workflows/prover/agent_session.py` | One scratch job, durable request admission and response usage, persistence encouragement and structured result |
| `workflows/prover/allocation.py` | Wait outside the controller lock for parallel allocations; reconcile usage without dequeuing proof results or making a worker wait on itself |
| `workflows/prover/usage.py` | Exact-model cost estimates, provider cost provenance, request coverage, and cumulative resume telemetry independent of observer flush timing |
| `workflows/prover/session_transport.py` | Shared provider adapters, one request per admission, no hidden retry/recovery loop |
| `workflows/prover/session_context.py` | Deterministic compaction retaining the contract, assignment and proof notes |
| `workflows/prover/session_guidance.py` | Selected skill contracts and durable addressed inbox delivery between requests |
| `workflows/prover/session_tools.py` | Role-specific read/scratch/Lean/research tools; no generic source-write or terminal authority |
| `workflows/prover/session_search.py` | Bounded project search and clean-room result filtering |
| `workflows/prover/session_research.py` | Direct bounded web/resource retrieval with provenance, relevance ranking, balanced provider merging, explicit degradation and no hidden model summaries |
| `workflows/prover/resource_handoff.py` | Bounded downloaded-resource catalogs and exact read grants across private job stages |
| `workflows/prover/check_process.py` | OS-isolated warm worker RPC and controller-owned restricted commands |
| `workflows/prover/check_sandbox.py` | Platform sandbox profiles, permitted runtime paths and restricted process environment |
| `workflows/prover/check_worker.py` | LeanProbe feedback inside the protected process |
| `workflows/prover/verification.py` | Independent candidate/type/axiom acceptance, importable module refresh and final Lake gate |
| `workflows/prover/type_profile.py` | Isolated exact-source compilation and trusted inspection of kernel types and local definition dependencies |
| `workflows/prover/negation.py` | Exact negated-target construction and certificate support |
| `workflows/prover/negation_job.py` | Separate bounded negation pass and independent certificate acceptance |
| `workflows/prover/libraries.py` | Additive helper-library registration and pinned Lake dependency installation with rollback |
| `flags/prover_catalog.py` | Public prover setting descriptions and defaults |
| `cli/prover_status.py` | Exact-run snapshot reads and durable guidance submission |

New prover leaf modules are in the mypy gate. The shared
`core/toolsets.py` registry entry `leanflow-prover-session` is empty deliberately:
only the role-specific schemas supplied by the session runtime are exposed.

The larger coordination modules remain intentionally coupled where tests patch
their module attributes. Extracting behavior from them requires
characterization tests and an explicit dependency seam first.

## VS Code Extension

`vscode-extension/` is a workspace extension over the public CLI contracts; it
does not read or reinterpret LeanFlow's persistence files directly:

- `src/core/cli.ts` owns JSON command execution, strict output parsing, and the
  detached workflow spawn boundary. The host passes only catalogued
  `LEANFLOW_*` overrides and a minted run id.
- `src/core/runManager.ts` persists a run record before spawn, binds it to its
  project root and process identity, and reconciles restored runs only from an
  exact run-id status or immutable terminal result. A restored run is stopped
  through `leanflow runs stop`, which revalidates the recorded process.
- `src/core/runOwnership.ts` is the pure owner-adoption and terminal-state
  policy. `src/core/runSelection.ts` prevents stopped tracked rows from masking
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
  catalogued launch controls. The host uses `runs prover` / `prover-message` / `runs event`,
  validates ownership and paths, and does not substitute another run's artifacts.
  `proverProgress.ts` and `proverOperations.ts` derive budget, capacity, operation,
  proposal and stop-reason views from the same snapshot. `proverCache.ts` and
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

## Runtime Contracts

The packaged Markdown under `leanflow_skills/` and `leanflow_specs/` is runtime
input, not supplementary prose:

- skills select concise prompt-time behavior for the active workflow
- workflow specs define tool order, verification gates, route actions, and stop
  conditions
- worker and phase specs define specialist responsibilities

These files ship in the wheel. A change to a workflow contract must update its
spec, the routing skill when applicable, and the relevant deterministic tests.

## Principal Execution Paths

### Proving

```text
leanflow workflow prove
  → workflow/provider resolution and lean-bounded-prover identity
  → prover.runtime: frozen source discovery or lineage-preserving resume
  → research only: fresh outline → graph construction → review → skeleton gate
  → deterministic prerequisite DAG scheduler
  → private agent_session with admitted request ledger and role-specific tools
  → OS-isolated warm LeanProbe feedback
  → controller-owned independent axiom/source check and exact hole replacement
  → requested-scope placeholder check and project Lake build
```

Only the controller commits canonical source and shared PLAN/DAG state. Default
bottom-up jobs use completed prerequisites. Experimental top-down output remains
an untrusted candidate until every planned dependency closes and strict checking
accepts it. Model success messages never change trusted proof status.

Source transactions retain the controller lock across independent Lean acceptance.
Worker usage and operation publication take a separate progress lock and serialize
the last committed DAG/source checkpoint, never in-flight source documents. Live
`verifying`/`integrating` overlays affect display only; the scheduler marks a node
proved after the canonical transaction commits. Snapshot sequence numbers increase
for both controller commits and metadata updates. Proposed graphs and staged diffs
remain labelled as pending until compilation and protected-type checks pass.
Scratch checks inherit the configured verification timeout capped by the job's
remaining deadline; queueing, cold startup, imports and elaboration share that cap.

### Formalization

```text
leanflow workflow formalize
  → source extraction and TeX/PDF preflight
  → declaration blueprint
  → buildable Lean statement draft
  → source-fidelity review
  → explicit handoff to prove
```

Formalization intentionally leaves theorem bodies as `sorry` after statement
approval. The subsequent `prove` workflow owns proof completion.

### Research Mode

Research mode separates informal planning, graph construction, semantic review,
resource questions, and proving into fresh sessions. The orchestrator can use
bounded paper/web retrieval, exact arithmetic experiments, and reviewed immutable
library requests; Lean search belongs to provers. The scheduler runs bounded
concurrent prover jobs and resource-question batches. Recovery replans affected
nodes without rewriting original claims or proved nodes; queued proof results
wait for the active controller planning phase to return before acceptance.

There is no standing advisor, model-based manager, generic terminal tool, or
budget-refreshing local decomposition loop. Per-pass and total admitted requests,
restarts, direction changes, structural recoveries, node count, context, and time
have separate finite settings. See `docs/prover-workflow.md` for current limits.
Up to two persisted `research_job` admissions per prover workspace can launch
separately bounded resource agents, with the same cap across resumes. Admissions
are reserved before dispatch, and each allocation consumes campaign capacity
without resetting the parent pass. Controller-derived resource grants retain
evidence from both children within the existing bounded catalog. Rejected
submissions receive independent feedback inside their existing session. The
accepted reviewer classification distinguishes direction
refinements from decomposition; deterministic campaign limits remain independent.

## Persistence and Resumability

User-level state resolves through `LEANFLOW_HOME` (normally `~/.leanflow`).
Prover runs use `.leanflow/workflow-state/prover/<run-id>/`:

- `PLAN.md`, `DAG.json`, and `state.json` describe current plan, graph and jobs.
- `source.json`, immutable `source-checkpoints/`, and `baselines/` preserve protected source and reviewable diffs. State points to one coherent source generation.
- `source-transaction.json` journals accepted proof installation and multi-file
  helper/import/configuration materialization until state commits. Resume retains
  interrupted candidates for rechecking and recovers only recorded source images;
  outside edits produce a source conflict. Unchanged source, DAG and PLAN payloads
  are not rewritten during metric updates or idle polling.
- Controller `events.jsonl` is separate from each `jobs/<job-id>/events.jsonl`.
- Jobs keep `Scratch.lean`, `PLAN_job.md`, candidate/report files, and resources.
- Admission ledgers and scratch baselines live outside job write access under
  `jobs/.runtime/<job-id>/`.
- `inbox.jsonl` stores user guidance for controller and addressed job request
  boundaries; per-job delivery offsets and bounded addressed guidance survive resume
  and remain pinned across context compaction.

State JSON uses atomic replacement. A resume launch copies durable artifacts to a
new run ID with `resumed_from` / `parent_run_id`; the old execution remains
inspectable. Saved configuration and spent request admissions survive. Source
reconciliation precedes accepting more work. Other native workflows retain their
existing checkpoint, journal, activity-retention and live-process ownership stores.

## Verification and Trust Boundaries

- A model response is never proof of completion.
- `prove` succeeds only after the assigned declaration and requested project
  scope pass deterministic placeholder, diagnostic, kernel, and final build
  gates.
- `formalize` succeeds when the statement draft builds and passes source
  review; intentional theorem holes are then handed to `prove`.
- Axiom checks use elaborated declarations, not source-text heuristics alone.
- Promoted negations and helper proofs pass the same trust checks as main proof
  edits.
- Prover sessions route feedback through `check_process.py` workers using warm
  LeanProbe. macOS sandbox-exec or Linux Bubblewrap limits writes and disables
  worker networking; unsupported isolation fails closed. Controller commands
  explicitly grant only required build/configuration outputs.
- `session_transport.py` sends one provider request per durable admission.
  Interrupted/failed requests count; compaction makes no model calls.
- The original source snapshot and request ledger are outside job write
  authority. Lean metaprogram IO is also subject to the OS boundary.
- In retained native compatibility paths, repeated target timeouts can mechanically promote a large local `have` to a
  private lemma: Mathlib supplies the exact context signature, LeanProbe checks
  the helper and replacement site, and the verified patch transaction commits
  only the authenticated source image.
- `lean/lean_interact_compat.py` installs a version-guarded linear response
  reader when the installed LeanInteract still uses quadratic REPL output
  concatenation; unfamiliar future implementations remain untouched.
  `lean/lean_probe_deadline.py` independently bounds every LeanProbe call and
  terminates owned REPL sessions when IPC stalls.
- Incremental results label the probe session as `warm`, `cold_initial`, or
  `cold_rebuild`. Their `leanflow_timing` separates LeanProbe's reported
  command time from unattributed probe wall time so dependency-side startup
  omissions remain visible without being mislabeled as theorem checking.
- Verified graph state is derived from Lean evidence and reconciled after
  source changes.
- File locks serialize supported writes during user-approved swarm runs.
- Process termination requires exact token, PID, process-group, and session
  identity; stale PID-only records fail closed.
- Managed agents cannot read raw live workflow transcripts through ordinary
  file tools. Bounded, generated summaries are the model-facing interface.
- Clean-room policy is enforced across web, repository, terminal, and file
  surfaces, including canonical-path and symlink checks.
- The optional clean-room numeric terminal routes audited inline Python only
  through the active LeanFlow interpreter; ordinary terminal commands retain
  their normal executable resolution.
- Empirical computation runs in a restricted child process with bounded
  resources and no filesystem, process, or network capability.

## Compatibility Surfaces

The following interfaces are load-bearing:

- `from run_agent import AIAgent`
- top-level `model_tools`, `toolsets`, and `utils` imports
- `AIAgent.run_conversation()` result keys:
  `final_response`, `last_reasoning`, `messages`, `api_calls`, `usage`,
  `completed`, `exit_reason`, `partial`, `interrupted`, and
  `response_previewed`, `wall_timed_out`, with conditional interruption/error
  fields
- tool names and import-time self-registration
- module attributes intentionally used as monkeypatch targets in tests,
  especially in `native_runner.py`, `run_agent.py`, and terminal tooling
- the `LEANFLOW_`-only native child-process environment contract
- `core.home.leanflow_home()` as the single state-home authority

Compatibility imports that exist solely as public re-exports or patch targets
must carry an explicit `# noqa: F401` and a test or real call site that proves
the reference is intentional.

## Change Discipline

Before changing a coupled boundary:

1. Add characterization tests for current behavior.
2. Extract one cohesive responsibility without changing semantics.
3. Preserve public imports and monkeypatch seams or migrate them explicitly.
4. Add the cleaned module to the mypy gate.
5. Run Black, Ruff, mypy, and the full test suite.
6. Update this map when ownership or a public surface changes.

The complete contribution and quality-gate requirements are in `AGENTS.md` and
`CONTRIBUTING.md`.

### Managed tools and launch compatibility

- `cli/loogle_index_compat.py` classifies Loogle's index dialect, patches the
  managed client to negotiate flags, and records compatibility without starting
  an index during status reads. `loogle_local.py` owns build/lock/lifecycle;
  `leanflow mcp repair` reapplies managed patches offline.
- `workflow.prover_config_preview` resolves dedicated prover settings from the
  actual child environment. `flags.resolve.profile_launch_surfaces` reports
  terminal/extension compatibility without bypassing the extension allowlist.
- `workflows/project.resolve_repl_revision` checks available REPL tags before
  selecting a compatible source revision and reports unresolved/offline states.
- `tools/utilities/empirical_compute_runtime.py` remains a standalone isolated
  child: its capability contract drives both AST checks and the research tool
  description. Expanded exact arithmetic has no filesystem/network authority.

### Lean-IMO comparison harness

`scripts/lean_imo_campaign/` is repository experiment tooling, outside the product
workflow layer: `matrix.py` owns problem-lane scheduling, `artifacts.py` freezes
the runtime and prepares private offline Lake projects, `worker.py` invokes the
dedicated prover, `recovery.py` bounds provider reconnects without budget resets,
`runtime_versions.py` pins each cell to an immutable runtime, `adoption.py`
reattaches a replacement dispatcher to recorded worker identities, and
`runner.py` persists the two-lane queue and metrics (including runtime hashes). It introduces no generic
batch workflow. `vscode-extension/src/core/benchmarkCampaign.ts` owns presentation
types and navigation confinement; `panels/benchmarkPanel.ts` reads the durable
manifest and links cells into the existing prover dashboard. See
`docs/lean-imo-campaign.md` for the explicit experiment contract and controls.
