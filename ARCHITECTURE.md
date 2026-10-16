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
`core/regular_files.py` supplies bounded, nonblocking reads with inode/version
checks; source intake and currentness use them before hashing or freezing bytes.

| Entry point | Implementation |
| --- | --- |
| `leanflow` | `leanflow_cli.main:main`, CLI handlers, and `workflow.py` |
| `prove` / `autoprove` | `leanflow_cli/workflows/prover/runtime.py` |
| Formalization workflow controller | `leanflow_cli/native/harness_workflow.py` |
| Lean inspection and search | `leanflow_cli/lean/lean_services.py` and sibling leaves |
| Native agent execution | `agent/runtime/harness/` |

`run_agent.py`, `leanflow-agent`, the former `native_runner`, and direct-provider
entry points return migration errors. The SDK conversation loops, direct API
routing, and custom compression are removed. Retained deterministic helpers have
direct tests in their owning modules. New execution features use the harness.

## Native harness

| Module | Responsibility |
| --- | --- |
| `models.py` | Typed invocation/result/session/usage contracts; explicit verified prelaunch failure and uncertain-cleanup outcomes |
| `commands.py`, `discovery.py` | Native executable resolution and command construction |
| `runner.py`, `processes.py`, `metadata.py` | Structured process I/O, resume and cancellation; Popen-bound root identity, retired ancestry after reaping, cached genuine descendants, membership-guarded group signaling and physical output EOF before closure |
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

`NativeLaunchFailure` carries a result with verified no-launch evidence only after
owned preflight helpers have closed. `NativeCleanupUnverified` can carry observed
usage without granting cleanup or acceptance authority; known usage remains charged,
and unavailable usage remains unknown. Process tracking retries transient metadata
failures and stops the invocation after five consecutive failed samples. Final
process-group, tracked-identity and output-pipe closure checks remain strict.
`agent/runtime/harness/resources.py` owns process-bound RSS measurement and the
native memory ceiling. Native calls use a 16 GiB default, support an explicit
1–20 GiB setting, and stop after a sampled excess or bounded inspection failure.
Setup reuses the lower-layer RSS measurement; the native harness never imports
CLI orchestration.

`runtime/workflow_resources.py` owns the controller-level aggregate RSS window for
Formalization and Prove. It binds the actual controller start identity before
actor launch, excludes preexisting unrelated branches, includes explicit cached
warm-checker identities and samples the deduplicated observed process union every
0.25 seconds. It shares native limit resolution, cancels after a sampled excess or
five inspection failures, and gates launch and acceptance. Cached child cleanup
never signals the controller or grants physical output EOF authority.

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
| `native_snapshot.py`, `native_snapshot_publication.py` | Exclusive filtered snapshot publication after durable exclusion provenance; canonical publication guard before storage ownership; exact public authority controls omitted from private job views without creating portable authority; crash-safe scoped resume, dependency-view release and pin restoration |
| `native_bridge.py`, `mcp_relay.py` | Job capabilities and durable start/poll/cancel operations |
| `native_command.py` | Snapshot-confined long commands and owned-process cleanup |
| `session_tools.py`, search/research/compute leaves | Role-scoped deterministic tool services |
| `source.py`, `source_transaction.py`, `materialization_transaction.py` | Protected hole/helper replacement and run-bound proof/planning checkpoints |
| `source_mutation.py`, `source_mutation_recovery.py`, `source_journal.py` | Bounded durable inode captures, no-clobber installation/rollback, and retained-version auditing |
| `planning_controller.py`, proposal/review leaves | Plan validation, helper materialization, and branch repair |
| `check_process.py`, `check_worker.py`, `native_check.py` | Isolated LeanProbe feedback and controller deadlines |
| `lean_environment.py`, `submission_cache.py` | Dependency freshness and controller-owned exact-input acceptance reuse |
| `verification.py`, `type_profile.py` | Exact-source compilation, complete axiom profiles, and protected kernel types |
| Negation and recovery leaves | Exact-negation certificates and evidence-preserving continuation |
| `libraries.py`, `library_transaction.py` | Private pinned Lake resolution, no-replace new package promotion, journaled configuration recovery |
| `library_configuration.py` | Bounded Lake configuration parsing and additive helper-target configuration in private previews |
| `library_checkout.py`, `installed_libraries.py` | Independent ordinary Git checkout/commit evidence and read-only locked dependency reuse |

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

Ordinary source promotion follows controller ownership → publication ownership;
library promotion adds the library-install lock after publication ownership.
Recovery before job startup and final checking after job drainage do not introduce
a concurrent reverse acquisition. Optional publication-lock cancellation/deadline
polling checks before and after acquisition; legacy blocking and same-thread
reentrant cleanup remain available. Cancellation stops new integration while
preserving journals needed for safe rollback.

Source mutation preflights supported same-device filesystems and the actual
no-replace API before displacing canonical files. Captured original/candidate
inodes and bounded rollback slots survive late editor descriptors. Sticky conflicts
block the owning campaign; restoring bytes does not clear that authority loss.
Fresh campaigns independently rediscover reconciled ordinary source. An interrupted
library promotion additionally blocks fresh discovery until its pending protected
configuration transaction can recover. No same-run prover conflict-reconciliation
command or automatic retained-inode garbage collection is currently provided.

## Formalization and project setup

`native/harness_context.py` prepares source and skill context and authenticates the
complete source partition copied into private job scratch. Stage assignments use
bounded review groups with an exact phase partition, retaining the full canonical
source census for audit.
`harness_workflow.py` owns native execution, global limits, submission, review,
and promotion. `harness_observer.py` publishes process ownership, heartbeats,
accounting, settings, and bounded activity through the status API.
`native/harness_heartbeat.py` owns the author checkpoint writer and bounded closure.
It records active accounting ownership before execution, cancels on persistence
failure and releases ownership only after final elapsed-time persistence and
physical worker closure. Display locks cannot block this accounting lifecycle.
Tool activity exposes compact receipts and allowlisted, controller-authored failure
summaries; arbitrary error text, arguments and results remain private.

Native callers durably record possible live ownership before entering the harness,
including its preflight and MCP discovery. A verified failure to launch clears that
record after owned tool cleanup. Normal formalization also clears it after a verified
native result, bridge operation drainage and checker closure, before independent
Lean checks, source reviews or publication. Draft retention additionally requires
the full bridge to close. Uncertain closure preserves private inputs and prevents
another invocation from reusing them.
Checker and bridge shutdown remain retryable: failed cleanup retains the worker
registry and private paths until independent owned-group and tracked-identity
checks succeed and both output pipes reach physical EOF. A closing bridge refuses
new operations and serializes closers.
Recovered draft previews bind frozen source blobs, with current source attachment
reported separately. That binding does not certify what the author actually read
from its editable private copy.

`harness_artifacts.py` captures shared project source and configuration bytes.
Source-backed formalization uses dedicated controller leaves for checking and
publication; proving uses its own controller. These are the two exposed workflows:

| Module | Authority |
| --- | --- |
| `formalization/source_*` | Unabridged source bundles, original spans, include/macro provenance, complete regions and optional parser comparison; `source_companion` binds explicitly selected PDF evidence and approximate navigation without granting correspondence or coverage |
| `formalization/source_numeric_conditionals.py`, `source_literal_macros.py`, `source_builtins.py` | Bounded lexical decimal-condition evaluation with scoped literal constants, required-brace argument effect refusal and shared standard command recognition; none executes TeX or grants source fidelity |
| `formalization/source_environment_paths.py` | Bounded independent conditional alternatives with shared persistent candidate histories; preserve malformed-path witnesses and strict resource failures without correlating unknown guards or granting source fidelity |
| `formalization/source_bibliography.py` | Freeze contained literal bibliography resources and root job-name rendered bibliography evidence; distinguish missing databases and ambiguous support without executing TeX or BibTeX |
| `formalization/source_draft.py`, `source_draft_artifacts.py`, `source_draft_recovery.py`, `native/harness_source_draft.py` | Explicit paired-PDF uncertainty admission for one author turn; durable observed completion and physically closed native/bridge markers; bounded local preview retention and exact run history bound to source selection and preparation; no mappings, review receipt, publication or proof authority |
| `formalization_documents.py`, `formalization_tex_discovery.py`, `formalization_models.py`, `intake.py` | Source path selection, TeX root discovery, frozen intake context and workflow environment; document-tool displays project the same source inventory |
| `preparation_plan.py`, `preparation_bounds.py`, `preparation_inputs.py`, `preparation_discovery.py`, `preparation_launch.py` | Require a checked project baseline before native planning or budgets; shared advertised recommendation bounds, read-only prerequisite estimates, bounded user/catalog dependency expansion, source-only previews and shared-budget setup before native-controller ownership |
| `preparation_campaign.py` | Persist controller-owned run/source accounting identity before paid planning; reopen the exact budget independently of preparation freshness, reject inconsistent or unbound resume history and preserve prior charges through failed preflight |
| `corrections.py`, `correction_evidence.py` | Versioned scoped user decisions, immutable original/adopted readings, replayed closed local warrants, exact clause dispositions and same-call independent correction verdicts; existing review/publication/handoff gates remain acceptance authority |
| `service_corrections.py` | Project correction evidence and current controller-bound decision templates into the existing item details; saved verdicts do not imply fresh acceptance |
| `native/harness_best_effort.py`, `draft_delivery.py`, `draft_delivery_census.py`, `draft_review.py` | Same-campaign whole-paper completion after unresolved readings, independent compiled-name/data/build checks, isolated advisory full-source review and immutable versions of unaccepted drafts; retain a real buildable project without slice, publication or prover authority |
| `preflight_owner.py`, `workflows/workflow_json_io.py` | Publish a verified CLI preparation owner with cooperative Stop, aggregate memory supervision, bounded planning activity and heartbeats; skip contended progress writes, bound ownership transitions and refuse handoff after failed release; retain prior mathematics separately from current preparation errors, preserve inherited interpreter flags; previews claim no owner |
| `preparation.py`, `preparation_run.py`, `preparation_stage.py`, `preparation_layout.py`, `preparation_reuse.py`, `preparation_resources.py`, `preparation_inspection.py`, `preparation_lifecycle.py` | Exact setup authority, private dependency acquisition/build/import checks, bounded metadata-gap diagnostics with positive final supervision, measured resource bounds and disposal only after verified process cleanup |
| `preparation_hash_cache.py` | Verify new Lake hash caches through the pinned compiler's pure API under stopped private ownership; admit only exact derived additions beside the unchanged original source inventory and retain them in transactionally published, fingerprinted infrastructure |
| `preparation_transaction.py`, `preparation_fs_inputs.py`, `preparation_journal.py`, `preparation_progress.py`, `preparation_loss.py`, `preparation_resolution.py`, `preparation_resolution_progress.py`, `preparation_recovery.py`, `preparation_delta.py`, `preparation_audit.py`, `preparation_disposal.py` | Independent durable infrastructure journals, bounded per-entry progress and archive intents, exact unavailable-history observations, changed-entry publication and conflict-preserving recovery/disposal |
| `preparation_errors.py`, `preparation_history.py`, `preparation_membership.py`, `preparation_registry_loss.py` | Bounded surviving-history discovery, positive selected committed receipt membership, preserved missing/stale-index evidence and exact acknowledgment that requires a new checked commit |
| `workflows/project_setup/baseline_configuration.py`, `baseline_transaction.py`, `invocation_evidence.py`, `public_process.py` | Private baseline configuration/update, conflict-preserving promotion and recovery, durable expected invocation identities with missing-ledger rejection, bounded public execution and physical EOF before setup readiness |
| `workflows/project_setup/baseline_recovery.py`, `recovery_barrier.py` | Exact-observation inspect/external acknowledgment of preserved baseline conflicts; positive crash-safe fresh-setup barriers, independent owner/group/identity absence, explicit output-closure confirmation, unchanged false verification flags and retained unknown inputs |
| `workflows/project_setup/baseline_history.py`, `history_archive.py` | Bounded positive archive membership and exact-ID closed-record lookup; an external membership/head anchor detects missing or rolled-back archives, with verified-only compaction while continuously auditing every preserved baseline version |
| `workflows/project_setup/execution.py`, `execution_process.py`, `cleanup.py`, `toolchain_install.py`, `toolchain_publication.py`, `toolchain_recovery.py`, `toolchain_journal_loss.py`, `toolchain_disposal.py` | Scrubbed bounded setup commands, verified descendant shutdown, official compiler acquisition into private Elan state, recoverable exclusive publication, registered private disposal captures and exact-observation acknowledgment of ambiguous or unreadable history without replacing existing installations |
| `slice_gate.py`, `inventory_authority.py`, `preparation_assignment.py`, `stage_errors.py`, `native/harness_formalization_stages.py` | Canonical complete source denominator, checked-plan delivery, controller-selected representative slice, immutable independent review and terminal setup-drift handling before full expansion |
| `freshness.py`, `environment_display.py`, `conflict_resolution.py`, `pdf_actions.py`, `extraction_actions.py`, `mapping_actions.py`, `review_transport.py` | Current acceptance without proof-certificate lookup, exact version reconciliation/revocation, fidelity-preserving source repairs and UTF-8 bounded evidence envelopes |
| `records.py`, `receipt_history.py`, `receipt_recovery.py`, `actions.py`, `diagnostic_actions.py`, `service.py`, `service_snapshot.py`, `service_artifacts.py` | Immutable selected revisions, exact-revision durable edit acknowledgments, safe frozen artifact IDs, fast non-authorizing historical projections and separately requested currentness checks |
| `service_history.py` | Bounded revision-identity pages without loading historical receipts or computing environment authority |
| `clause_obligations.py` | Exact source-bound known clause checklists, compiled declaration dispositions and independent verdicts that cannot waive known omissions or specializations |
| `source_reconciliation.py`, `source_review_regions.py`, `source_proof_wrappers.py` | Author-proposed literal source readings, exact compact partitions of the complete token census, conservative custom proof candidates and independent canonical coverage gates; old TeX policies require explicit re-extraction before current authority |
| `source_effects.py` | Shared recognized file-loading and I/O command families, including replacement-body effects that must survive lexical masking |
| `source_visual.py`, `source_visual_process.py`, `source_visual_receipt.py`, `review_transport.py` | Bounded frozen companion-page renders and image capabilities, successful transport-delivery evidence, complete physical-page review and portable image artifacts; no relaxation of PDF-only transcription |
| `workflows/prover/check_command_cleanup.py`, `check_capture_failure.py` | Retryable one-shot command ownership retaining private inputs until owned absence and physical output EOF are both verified; bounded redacted interruption provenance preserves failed acceptance separately from eventual closure |
| `vscode-extension/src/panels/formalizationProjectSetupHost.ts`, `src/core/projectSetup.ts` | Explicit baseline setup bound to the host's selected Formalization workspace, separate setup reply correlation, cancellation outside the source queue and workspace-specific readiness, retry and logs; native planning still revalidates controller preparation |
| `vscode-extension/src/panels/frozenArtifactDocuments.ts` | Named read-only virtual text from already verified artifact bytes, distinct viewer ownership, bounded retained contents and registration disposal; no filesystem lookup or source/translation authority |
| `lean_layout.py`, `lean_policy.py`, `lean_inspector.py`, `lean_origin.py` | Evaluated Lake roots, explicit module builds, complete compiled declaration policy and exact compiler-helper provenance |
| `inspector_readiness.py` | Compiler-only declaration-inspector compatibility before translation, empty kernel replay without statement approval and current receipt validation |
| `proposal.py`, `proposal_schema.py`, `environment.py`, `dependency_inputs.py`, `dependency_files.py` | Owned write set, total region/item coverage, exact mappings, compiled-library/tooling inventory and persistent content identity; no inferred source-to-binary provenance |
| `dependency_snapshot.py` | Private canonical dependency copies, exact admitted-resource preservation, clean-room refusal and canonical/frozen identity checks without compatibility-cache substitution |
| `lean_build.py`, `lean_build_capture.py` | Read-only build admission, offline pinned Lean compilation of declared libraries/executables, isolated per-module outputs, exact artifact census and hash evidence without dependency Lake execution; one fresh capture retry only for interrupted owned-process stopping after prior normal compiler exit and verified closure, with unchanged inputs, original limits and both attempt records |
| `lean_configuration.py` | Bounded static Lake syntax, ordinary template metadata and typed compiler-option allowlist |
| `dependency_modules.py` | Compiler/library namespace separation and disjoint top-level module roots matching Lean's first-root loader behavior |
| `compiled_evidence.py` | Merge scoped library evidence by compiled identity without dropping bodies or relabeling project declarations as reuse |
| `review.py`, `review_cache.py`, `review_dossier.py`, `review_scope.py`, `review_registry.py` | Fresh per-unit verdicts, whole-document gate, authority-bound review caches and frozen read-only retrieval; controller-derived slice/complete scope preserves the canonical denominator and planned remainder, while a complete declaration index links exact mathematical rows and the separate unabridged controller audit |
| `publication.py`, `publication_journal.py`, `publication_recovery.py`, `publication_loss.py`, `publish.py`, `evidence.py` | Project-owned durable publication evidence, concurrent-save protection, explicit history-loss revocation and portable records |
| `handoff.py`, `handoff_slots.py`, `proof_snapshot.py`, `proof_receipts.py` | Exact reviewed proof slots and frozen independent proof-preservation certificates |
| `publication_case_paths.py`, `publication_membership.py` | Bounded literal control-path validation, case-alias authority-loss detection, and positive committed transaction membership binding each published document's current, handoff, revision and receipt bytes |
| `workflows/prover/scoped_imports.py` | Private compilation of evaluated local import membership for authenticated formalization handoffs, source/configuration drift guards and controller publication of derived cache entries with retained prior versions; setup readiness grants no proof acceptance |
| `workflows/prover/scoped_cache.py`, `scoped_cache_barrier.py` | Multipart cache publication, exact rollback without replacing competing versions, retained refresh-pointer captures and full local refresh before renewed admission |

Proposal checks and editor actions acquire project publication ownership before
document ownership, including nested preparation admission under that project lock.
The explicit preparation command releases its initial document selection lock before
setup, then reacquires publication followed by document ownership to check the
selected revision and report readiness. Blocking OS lock acquisition can defer a
recorded cancellation for legacy callers without wait options. Callers supplying a
cancellation check or deadline use bounded nonblocking polling, checking before
and after acquisition; this does not make every startup path cancellable.

Source, layout, code, checks and scope changes invalidate the selected revision.
Author retry ownership also names its native run; candidates from another run sharing
the same base revision cannot be superseded. Explicit source edits remove that ownership.
Review receipts are separate immutable evidence keyed to the selected identity. A passing
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
| `workflowForms.ts`, Prove and Formalization views | Separate persisted author forms, fixed workflow identities, and explicit migration of retired launches |
| `proverModelSettings.ts`, `agentActivity.ts` | Per-call setting evidence and bounded native activity |
| Event buffer and storage-security modules | Bounded tails and realpath/no-symlink checks |
| Experiment isolation/scoring/matrix modules | Frozen clean baselines, private cells, and evidence-based aggregates |

Prove and Formalization have separate tabs and launch forms. Formalization launch
and resume bind the selected document, revision, original source and workspace in
the host. Resume restores the trusted run's original provider, resolved model and
draft intent. Historical runs without sufficient source or launch identity remain
CLI-only. Configuration selects settings; a separate start action launches work.
Webviews also provide setup, provider controls, graph/tree, plan, jobs, limits,
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

The prover source-mutation leaves own bounded durable inode capture, installation,
rollback and recovery without sharing formalization publication state. Unknown or
late-written editor versions remain retained and conflict blocks integration.
`formalization/publication_registry_inventory.py` owns bounded discovery of surviving
publication evidence and registry-completeness checks.
`publication_registry_loss.py` binds acknowledgment to the exact observed authority
loss and retires an incomplete index without granting selected acceptance; a fresh
accepted publication remains required. `preparation_authorization.py` preserves
baseline/plan authority through effectful preparation and final commitment.
`preparation_dependency_pins.py` applies Lake's first-priority inherited-manifest
resolution to the transitive locked closure, using roots independently validated
by preparation authority. `prover/library_configuration.py` owns bounded Lake parsing and
private helper-target previews; `library_transaction.py` owns distinct private
dependency inputs, sanitized locked Git metadata, no-replace new checkout promotion,
and durable configuration recovery.
The extension's formalization scope/operation leaves freeze host-owned workspace
identity across asynchronous calls and filter obsolete workspace/request results.
