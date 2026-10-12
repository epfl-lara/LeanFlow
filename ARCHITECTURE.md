# LeanFlow architecture

LeanFlow coordinates Lean workflows around three native agent runtimes: Codex,
Claude Code, and Lemex. Native runtimes own authentication, model requests,
retries, sampling, tools, history, and compression. LeanFlow owns scheduling,
accounting, job isolation, independent Lean acceptance, canonical source
transactions, and persistent evidence.

## Layers and entry points

Dependencies flow from `core/` to `agent/` and `tools/`, then to `leanflow_cli/`.
The shared core has no imports from higher layers. Top-level `model_tools`,
`toolsets`, and `utils` preserve historical imports through thin core re-exports.

| Entry point | Implementation |
| --- | --- |
| `leanflow` | `leanflow_cli.main:main`, CLI handlers, and `workflow.py` |
| `prove` / `autoprove` | `leanflow_cli/workflows/prover/runtime.py` |
| Other supported workflows | `leanflow_cli/native/harness_workflow.py` |
| Lean inspection and search | `leanflow_cli/lean/lean_services.py` and sibling leaves |
| Native agent execution | `agent/runtime/harness/` |

`run_agent.py`, `leanflow-agent`, the former `native_runner`, and direct-provider
entry points return migration errors. The SDK conversation loops, direct API
routing, and custom compression are removed. Retained deterministic helpers have
direct tests in their owning modules. New execution features use the harness.

## Native harness

| Module | Responsibility |
| --- | --- |
| `models.py` | Typed invocation/result/session/usage contracts |
| `commands.py`, `discovery.py` | Native executable resolution and command construction |
| `runner.py`, `processes.py` | Structured process I/O, resume, cancellation, and process-tree cleanup |
| `events.py` | Native event parsing and cumulative-usage normalization |
| `observations.py` | Emitted messages, reasoning, plans, tool activity, and settings observations |
| `settings.py` | Per-call reported model/effort history, separate from requested overrides |
| `metadata.py`, `model_defaults.py` | Bounded read-only metadata transport and labelled native model defaults |
| `redaction.py` | Credential and recognizable-secret removal before persistence/callbacks |
| `isolation.py`, `restricted.py` | Private filesystem authority and restricted native context |
| `network_policy.py`, `context_policy.py` | Local-only research and native automatic-context admission |

One complete native process invocation is one LeanFlow agent call. Internal model
requests, retries, and compaction remain native. Metadata inspection starts no
model turn. An emitted tool result or assistant claim is an observation, never
independent Lean acceptance.

`runtime/runtime_provider.py` selects exactly `codex`, `claude`, or `lemex`.
`env_loader.py` preserves native environment variables and excludes legacy
provider credentials from LeanFlow dotenv. `config.py` rejects obsolete schema
settings with migration guidance. `runtime/lemex_install.py` adopts or installs a
pinned self-contained npm or native-source Lemex bundle, including companion files, without replacing its global executable
or copying native authentication.

Native tools run in filtered private project snapshots under macOS seatbelt or
Linux Bubblewrap. Unsupported isolation fails closed. The outer OS boundary owns
filesystem permissions; nested Codex/Lemex seatbelt is disabled on macOS inside
that boundary. Runtime configuration is protected from tool writes. Unrelated
MCP servers, hooks, plugins, and project-local native settings cannot introduce
extra authority.

`agent/runtime/harness/preflight.py` checks host sandbox permissions without
starting a model; `doctor` reports this separately from runtime installation.

Normal sessions permit network research. Restricted sessions exclude prior native
context and automatic memories, and disable native research tools. Controller
bridge commands receive OS-enforced network denial; native model connections
retain authentication. Saved research provenance permits clean-room checkpoint
resume and rejects online or unclassified history reuse.

## Core primitives

- `home.py` is the sole `LEANFLOW_HOME` resolver.
- `campaign_limits.py` holds global non-prover invocation, reported-token, and
  active-time accounting, including interrupted charges and unknown usage.
- `deadlines.py` provides optional monotonic deadlines and local cancellation.
- `file_copy.py` owns private copy-on-write files and atomic directory publication.
- `state.py` retains historical sessions.
- `model_tools.py`, `toolsets.py`, and registry modules own deterministic discovery
  and self-registration; tool visibility alone grants no job authority.
- Error redaction, filesystem, process identity, verified-edit authority, and
  resource admission remain dependency-light leaves.

## Prover execution

```text
workflow resolution and source discovery
  → fresh outline, graph construction, and independent review in Research mode
  → deterministic theorem-DAG scheduling
  → private snapshot, job-bound MCP, and native invocation
  → independent Lean checks and compiled kernel inspection
  → controller source transaction
  → requested-scope placeholder check and project Lake build
```

Research defaults to bottom-up order and two prover slots. Standard uses one
prover. Top-down candidates remain untrusted until prerequisites close and strict
acceptance passes. Model reports never establish proof or disproof.

| Prover module group | Responsibility |
| --- | --- |
| `config.py`, `research_policy.py` | Native backends, limits, Lean policy, and persisted local-only provenance |
| `runtime.py`, `controller.py` | Lifecycle, progress, and deterministic scheduling |
| `allocation.py`, `role_limits.py`, `usage.py`, `live_progress.py` | Durable call admission, role caps, usage, and active time |
| `agent_session.py` | Native invocation and same-session submission feedback |
| `native_snapshot.py` | Atomic filtered snapshots, dependency-view release, and pin restoration |
| `native_bridge.py`, `mcp_relay.py` | Job capabilities and durable start/poll/cancel operations |
| `native_command.py` | Snapshot-confined long commands and owned-process cleanup |
| `session_tools.py`, search/research/compute leaves | Role-scoped deterministic tool services |
| `source.py`, transaction leaves | Hole replacement, protected source, journaling, and conflict detection |
| `planning_controller.py`, proposal/review leaves | Plan validation, helper materialization, and branch repair |
| `check_process.py`, `check_worker.py`, `native_check.py` | Isolated LeanProbe feedback and controller deadlines |
| `lean_environment.py`, `submission_cache.py` | Dependency freshness and controller-owned exact-input acceptance reuse |
| `verification.py`, `type_profile.py` | Exact-source compilation, complete axiom profiles, and protected kernel types |
| Negation and recovery leaves | Exact-negation certificates and evidence-preserving continuation |
| `libraries.py` | Pinned additive Lake requirements with configuration rollback |

Only the controller changes canonical sources, PLAN, and DAG state. Original
proof source is immutable outside authorized holes. Captured target types and
fixed local definitions are checked after import changes. `sorryAx` cannot be an
accepted completion axiom. Definition-hole authorization is explicit.

Compiler output moves to a separate inspector workspace before trusted inspection
code is created; compiler descendants cannot write there. Receipts bind exact
source hashes, canonical environment, and job revision. Pending operations finish
before a completed native response is handled; controller shutdown cancels and
joins them. A disconnected MCP client does not cancel its check.

Source transactions hold the controller lock through committed state. Resume
checks exact before/after images and rejects outside edits. Final acceptance
requires the requested scope free of `sorry` and a project Lake build.

## Other workflows and project setup

`native/harness_context.py` prepares source and skill context.
`harness_workflow.py` owns native execution, global limits, submission, review,
and promotion. `harness_observer.py` publishes process ownership, heartbeats,
accounting, settings, and bounded activity through the status API.

`harness_artifacts.py` freezes proposed bytes in an independent verification copy,
checks declarations and original types/definitions, and rechecks canonical
identity before promotion. `harness_formalization.py` runs a fresh source reviewer
and stores a strict PASS/BLOCK receipt bound to source/proposal hashes.
`formalization/` owns TeX/PDF intake, extraction, and blueprints. Formalization
allows theorem proof holes but requires complete definitions and source coverage;
a `draft_ready` outcome requires a separately started `prove` run for proofs.

`workflows/project_setup/` owns scaffolding, compatible REPL preparation, real
protocol checks, cancellable processes, readiness receipts, and invalidation.
`workflows/project.py` owns discovery and registration; `cli/project_command.py`
routes setup/init/create to the shared pipeline. `runtime/project_operations.py`
serializes setup against active workflows using canonical-path locks and verified
process identity. Unchanged verified setup reuses readiness; repair bypasses it.

## Storage, freshness, and installation

`runtime/dependency_cache/` owns exact compiler/platform/dependency compatibility,
sealed publication, and private writable materialization. No writable shared
symlinks or hardlinks are exposed. Root proof artifacts never enter the store.

`runtime/storage/` owns policy, leases, marked entries, LRU cleanup, process-view
protection, and abandoned-view recovery. Only managed entries or registered
views are collectible; proofs and checkpoints remain outside deletion scope.
Linux inventories exclude unreadable SSH/PAM login helpers only after validating
their service parent; unknown unreadable processes still protect all entries.
Defaults are a 20 GiB soft target and 10 GiB reserve. `prover/storage_admission.py`
guards dispatch and quiet polling; storage pressure checkpoints owned work.
`cli/storage_command.py` exposes inspection, cleanup, and explicit publication.

`runtime/runtime_install.py` claims fresh versioned Python environments and
atomically selects a validated wrapper. Active runtimes are leased; healthy MCP
environments are reused. Existing environments are not upgraded in place.
Editable installs still reference their source checkout, which must stay stable
while jobs run; wheel installs separate runtime code from checkout edits.

Acceptance reuse is controller-only and binds candidate bytes, protected types,
axiom policy, import closure, and Lean environment. Dependency changes invalidate
reuse and restart affected warm workers. Environment generation uses local file
identity, size, and nanosecond modification/change timestamps; it assumes changes
are visible through normal filesystem metadata. Final verification remains required.

## Accounting and persistent contracts

Unset global calls/tokens/time are unlimited; zero is exhausted. Prover role caps
default to 15 prover and 5 orchestrator invocations per run. A spawned call is
charged even if interrupted; only proven non-start is refunded. Missing tokens
remain unknown, and a token cap gates later admission rather than interrupting
an invocation at an exact service-side count.

Active time includes queues, tools, and checks, survives resume, and excludes
time between stopped controllers. There is no default response/check deadline.
Cancellation stops owned process trees and operations and prevents later promotion.

Prover evidence remains under `.leanflow/workflow-state/prover/<run-id>/`.
Other workflows use `.leanflow/native-workflows/<run-id>/`. Private ledgers and
accepted evidence are outside agent write authority. Atomic state, transaction
images, session identity, and lineage retain limits across native resumes.
Legacy direct-provider conversations require a new run.

Exact-run stream, history, snapshot, metrics, and provenance leaves require
terminal evidence and complete coverage before reporting complete results.
Integrity digests detect corruption and mismatches, not hostile filesystem owners.

## VS Code extension

The workspace extension uses public CLI contracts for setup, launches, exact-run
observers, settings, and metrics. Focused host modules own these boundaries:

| Host module group | Responsibility |
| --- | --- |
| `cli.ts`, `cliHealth.ts`, `services.ts` | Strict command parsing, launch, serialized refresh, and transient recovery |
| Project setup/discovery/path modules | Workspace selection, readiness, and filesystem containment |
| Provider setup/credentials/models modules | SecretStorage, native login, read-only metadata, and role inheritance |
| Run manager/catalog/ownership/selection modules | Durable identity, exact job navigation, external discovery, and validated Stop |
| `workflowTerminal.ts` | Private tmux job execution, reattachment, and terminal fallback |
| Prover snapshot/graph/progress/cache modules | Exact-run views, shared statuses, dependencies, operations, and coherent refresh |
| `proverModelSettings.ts`, `agentActivity.ts` | Per-call setting evidence and bounded native activity |
| Event buffer and storage-security modules | Bounded tails and realpath/no-symlink checks |
| Experiment isolation/scoring/matrix modules | Frozen clean baselines, private cells, and evidence-based aggregates |

Webviews provide setup, provider controls, launch, graph/tree, plan, jobs, limits,
changes, guidance, activity, model history, storage, and experiment views. Host
messages are validated before filesystem/process actions. Webview bundles may
import host types, never host runtime code. Workspace Trust and virtual-workspace
restrictions require a trusted real filesystem. Credential values do not enter
webviews, profiles, or exported state.

Model settings retain evidence sources and call history. Missing historical
settings are not inferred from current defaults. Contextual help stays inside
the viewport. CLI retries retain navigation and known settings. Source and job
artifacts remain bound to the selected run; proposals are never solved states.

Sweeps freeze a clean Git baseline and profiles, randomize order, and use private
sequential cell clones. Scoring requires complete exact-run final evidence;
missing or mismatched evidence is unscored. Sample statistics stay separate from
missing observations.

## Development and evaluation

Skills and specs are packaged guidance; deterministic source and verification
gates take precedence over model assertions. Preserve native request/result and
usage contracts, job receipts, tool registration, the single home authority, and
exact-source acceptance. Add characterization tests before changing coupled
code and include new leaves in the mypy gate. See [AGENTS.md](AGENTS.md).

`scripts/lean_imo_campaign/` is separate evaluation tooling with native conditions,
frozen runtime inputs, and optional global limits. Version-2 manifests reject
legacy API-budget experiments. See [the campaign guide](docs/lean-imo-campaign.md).
