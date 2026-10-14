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
| `observations.py` | Public messages, reasoning, plans, tool descriptions, and settings observations |
| `observation_stream.py` | Bounded live snapshots, throttling, invocation identity, and measured tool duration |
| `settings.py` | Per-call reported model/effort history, separate from requested overrides |
| `metadata.py`, `model_defaults.py` | Bounded read-only metadata transport and labelled native model defaults |
| `redaction.py`, `stream_redaction.py` | Credential removal, including fragmented streamed secrets, before persistence/callbacks |
| `isolation.py`, `restricted.py` | Private filesystem authority and restricted native context |
| `network_policy.py`, `context_policy.py` | Local-only research and native automatic-context admission |

One complete native process invocation is one LeanFlow agent call. Internal model
requests, retries, and compaction remain native. Metadata inspection starts no
model turn. An emitted tool result or assistant claim is an observation, never
independent Lean acceptance. Public stream fragments become bounded cumulative
previews; each persisted observation keeps a unique evidence ID. The editor
coalesces only its visible timeline by invocation, native item and reasoning kind.
Completed native messages remain authoritative; summaries and reported traces
are labelled separately. Unreported or encrypted reasoning is never reconstructed.

`runtime/runtime_provider.py` selects exactly `codex`, `claude`, or `lemex`.
`env_loader.py` preserves native environment variables and excludes legacy
provider credentials from LeanFlow dotenv. `config.py` rejects obsolete schema
settings with migration guidance. `runtime/lemex_install.py` adopts or installs a
pinned self-contained npm or native-source Lemex bundle, including companion files, without replacing its global executable
or copying native authentication. Explicit refresh adopts a compatible local upgrade
atomically while retaining immutable distributions used by older jobs.

Native tools run in filtered private project snapshots under macOS seatbelt or
Linux Bubblewrap. Unsupported isolation fails closed. The outer OS boundary owns
filesystem permissions; nested Codex/Lemex seatbelt is disabled on macOS inside
that boundary. Runtime configuration is protected from tool writes. Unrelated
MCP servers, hooks, plugins, and project-local native settings cannot introduce
extra authority.

On Linux, Codex and Lemex use a dedicated `leanflow-sqlite` directory inside
their native home for databases and journals. Configuration remains read-only;
authentication is read from its original location. Resume retains this database
location. Migrating older prover sessions starts a fresh native conversation while
preserving controller progress and consumed calls.

`agent/runtime/harness/preflight.py` checks host sandbox permissions without
starting a model; `doctor` reports this separately from runtime installation.
`workflows/prover/session_setup.py` checks LeanProbe and the assigned imports in
each actual private prover/negation workspace before native model admission. It
uses the job's `check_process.py` sandbox and retains accepted warm workers plus
run-bound setup diagnostics. `workflows/prover/session_build.py` rebuilds local
assignment imports from filtered private sources, granting writes only to private
build artifacts while canonical sources and dependencies remain read-only.
Dependency snapshots preserve private Git metadata
for Lake while applying source filtering independently of metadata retention.
`runtime/mcp_protocol.py` guards the MCP v1 API required by managed Lean servers
and job bridges; installers constrain dependencies and probe existing environments.

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
identity before promotion. Source-backed formalization uses dedicated controller leaves:

| Module | Authority |
| --- | --- |
| `formalization/source_*` | Unabridged source bundles, original spans, include/macro provenance, complete regions and optional parser comparison; `source_companion` binds explicitly selected PDF evidence and approximate navigation without granting correspondence or coverage |
| `formalization_documents.py`, `formalization_tex_discovery.py`, `formalization_models.py`, `intake.py` | Source path selection, TeX root discovery, frozen intake context and workflow environment; document-tool displays project the same source inventory |
| `records.py`, `receipt_history.py`, `receipt_recovery.py`, `actions.py`, `diagnostic_actions.py`, `service.py` | Immutable selected revisions, explicit repairs, safe artifact IDs and shared CLI/editor snapshots |
| `lean_layout.py`, `lean_policy.py`, `lean_inspector.py` | Evaluated Lake roots, explicit module builds, complete compiled declaration policy |
| `proposal.py`, `proposal_schema.py`, `environment.py`, `dependency_inputs.py`, `dependency_files.py` | Owned write set, total region/item coverage, exact mappings, compiled-library/tooling inventory and persistent content identity; no inferred source-to-binary provenance |
| `dependency_snapshot.py` | Private canonical dependency copies, exact admitted-resource preservation, clean-room refusal and canonical/frozen identity checks without compatibility-cache substitution |
| `lean_build.py` | Read-only build admission, offline pinned Lean compilation of declared libraries/executables, isolated per-module outputs, exact artifact census and hash evidence without dependency Lake execution |
| `lean_configuration.py` | Bounded static Lake syntax, ordinary template metadata and typed compiler-option allowlist |
| `dependency_modules.py` | Compiler/library namespace separation and disjoint top-level module roots matching Lean's first-root loader behavior |
| `compiled_evidence.py` | Merge scoped library evidence by compiled identity without dropping bodies or relabeling project declarations as reuse |
| `review.py`, `review_cache.py`, `review_dossier.py` | Fresh per-unit verdicts, whole-document gate, positive/negative cache and frozen read-only retrieval |
| `publication.py`, `publish.py`, `evidence.py` | Durable recoverable publication, concurrent-save protection and portable records |
| `handoff.py`, `handoff_slots.py`, `proof_snapshot.py`, `proof_receipts.py` | Exact reviewed proof slots and frozen independent proof-preservation certificates |

Source, layout, code, checks and scope changes invalidate the selected revision;
review receipts are separate immutable evidence keyed to that identity. A passing
translation does not imply completed proofs. Named Prop proof slots are allowed,
complete definitions and opaque bodies are valid, and conditional constructions
expose their pending dependencies. Published source bundles, blueprint, receipts
and handoff live in `formalization/<document-id>/`; private author/reviewer state
and continuation logs stay under `.leanflow/`. Unsupported publication primitives
produce `accepted_staged`, never a published readiness claim. The extension calls
the shared formalization CLI service and does not infer acceptance from badges or
parse statements itself. Native source-backed formalization uses the revision gate;
Markdown blueprints are generated evidence displays, never acceptance authority.

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

`runtime/project_build_cache/` optionally reuses controller-built local assignment
imports across fresh private snapshots. Source/resource/compiler/dependency keys,
cryptographic output manifests, sealed publication, and private materialization
preserve provenance. Reuse requires a relocatable TOML project and explicit opt-in;
resumed workspaces cannot publish. Lake validates restored traces before warming
LeanProbe. Canonical root proof artifacts are never cache seeds.

`lean/lean_session_lifecycle.py` selects foreground warm retention or memory-mode
reclamation before independent cold checks. Exact verification still uses a fresh
process. Cold boundaries compare current imports with the recorded warm generation;
module/project builds invalidate retained state after dependency changes.
Scoped cleanup preserves unrelated projects. Loader stamps cover compiled imports;
ordinary source edits are checked by the source-protection and acceptance layers.

LeanProbe owns scoped declaration segmentation and its response transport.
LeanFlow calls its public Python API directly without replacing backend functions.
`lean/lean_incremental.py` retains LeanFlow's admission, parent deadlines, source
protection, axiom policy, and output contract. Coordinated development installs
use adjacent or explicitly selected LeanProbe source, with a pinned source
download for standalone checkouts.

`runtime/storage/` owns policy, leases, marked entries, LRU cleanup, process-view
protection, and abandoned-view recovery. Only managed entries or registered
views are collectible; proofs and checkpoints remain outside deletion scope.
Linux inventories exclude unreadable SSH/PAM login helpers only after validating
their service parent; unknown unreadable processes still protect all entries.
Defaults are a 20 GiB soft target and 10 GiB reserve. `prover/storage_admission.py`
guards dispatch and quiet polling; storage pressure checkpoints owned work.
`cli/storage_command.py` exposes inspection, cleanup, and explicit publication.

`runtime/install_inputs.py` validates companion API metadata without executing it;
`install_companion.py` obtains an exact reviewed Git commit under the home authority.
`install_identity.py` hashes LeanFlow and companion package inputs.
`install_snapshot.py` records explicit-environment input receipts and copies LeanFlow
wheel and companion packaging inputs per runtime, keeping builds independent and
excluding stale `build/lib` modules. Editable LeanFlow installs retain the original checkout.
`install_native.py` adopts optional Lemex or builds it only for a selected provider.
`runtime/runtime_install.py` claims fresh versioned Python environments and
atomically selects a validated wrapper. Active runtimes are leased; healthy MCP
environments are reused. Existing environments are not upgraded in place.
Editable installs still reference their source checkout, which must stay stable
while jobs run; wheel installs separate runtime code from checkout edits.
Ownership registration and input freshness checks precede command publication.
A persistent switch journal protects both pointer generations across abrupt exit.
`install_storage.py` reads scalar block limits before YAML dependencies are available.
`cli/mcp_install_generation.py` owns permanent MCP generations, interprocess
selection locking, actual MCP initialization checks and atomic configuration
replacement. `cli/mcp_install_receipts.py` binds reuse to the current package
specification, patch sources, installed metadata and files, and initialized arguments.
Repair never patches a running environment in place. Selected
commands are validated as contained managed paths. Uncertain subprocess shutdown
retains a failed candidate; MCP generations and pinned companion sources are
currently outside automatic storage collection.

Acceptance reuse is controller-only and binds candidate bytes, protected types,
axiom policy, import closure, and Lean environment. Dependency changes invalidate
reuse and restart affected warm workers. Environment generation uses local file
identity, size, and nanosecond modification/change timestamps; it assumes changes
are visible through normal filesystem metadata. Final verification remains required.

## Accounting and persistent contracts

`core/workflow_limits.py` supplies new-run defaults shared by configuration,
the controller, and the editor catalog: 250 total agent calls, 200 prover calls,
50 orchestration calls, and 24 active hours. Explicit null/empty limits remain
unlimited; zero is exhausted. Saved runs retain their limits unless overridden.
A spawned call is
charged even if interrupted; only proven non-start is refunded. Missing tokens
remain unknown, and a token cap gates later admission rather than interrupting
an invocation at an exact service-side count.

Active time includes queues, tools, and checks, survives resume, and excludes
time between stopped controllers. There is no default response/check deadline.
Cancellation stops owned process trees and operations and prevents later promotion.
Role allowance exhaustion defers recovery until independent active provers finish.
`prover/submission_imports.py` stages explicit or scratch-header import requests,
checks protected kernel types, and retains them across candidate checks and resume.
Proof imports are journaled with proof installation and preserved across replans.

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
| Formalization service/artifact/message modules | Backend revision snapshots, verified frozen source navigation, typed inventory repairs, and exact prover handoff |
| `proverModelSettings.ts`, `agentActivity.ts` | Per-call setting evidence and bounded native activity |
| Event buffer and storage-security modules | Bounded tails and realpath/no-symlink checks |
| Experiment isolation/scoring/matrix modules | Frozen clean baselines, private cells, and evidence-based aggregates |

Webviews provide setup, provider controls, launch, graph/tree, plan, jobs, limits,
changes, guidance, activity, model history, storage, and experiment views. The
formalization view exposes coverage, command interpretations, independent review,
proof certificates, and explicit revision conflicts from backend evidence. Host
messages are validated before filesystem/process actions. Webview bundles may
import host types, never host runtime code. Workspace Trust and virtual-workspace
restrictions require a trusted real filesystem. Credential values do not enter
webviews, profiles, or exported state.

Model settings retain evidence sources and call history. Missing historical
settings are not inferred from current defaults. Contextual help stays inside
the viewport. CLI retries retain navigation and known settings. Source and job
artifacts remain bound to the selected run; proposals are never solved states.
Stop is available for tracked launches and the project's exact externally
discovered live owner. Historical process ids cannot authorize an interrupt;
the CLI revalidates the live owner's identity immediately before signalling.

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
