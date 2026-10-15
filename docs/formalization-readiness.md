# Formalization readiness and review scope

Formalization owns source inspection, dependency preparation, translation, review,
publication and the exact prover handoff. These stages have separate evidence.
A prepared project, a compiling translation and a completed proof are different results.
See [the workflow guide](formalization.md) for commands and editor use.

## Preparation and translation

A real translation launch requires a current project setup checkpoint before any
paid planning call. The Formalization tab exposes **Set up project** first;
the matching CLI command is `leanflow project setup`. A current formalization
preparation can also establish this infrastructure prerequisite. Source inspection
and viewing completed unaccepted drafts remain available without initializing tools.

After this baseline, formalization freezes its source and estimates the
mathematical prerequisites, useful existing declarations, imports, representation
choices and a representative slice. The estimate comes from a fresh read-only
native session. It can propose dependencies; it cannot authorize an unlisted
source, replace an existing package pin or approve its own mathematics.
Missing approved dependencies can therefore be added after initial setup, while
the baseline and dependency expansion remain visible separate checkpoints.

The controller validates a bounded plan. Existing compatible dependencies are
reused. The shipped catalog pins Mathlib and REPL for the default Lean version. Missing
official compiler releases are acquired with private Elan state and published only
after version checks; existing toolchains and native configuration stay protected.
Other new packages require an explicit HTTPS source and full Git commit supplied
through the user plan. A missing Mathlib for another compiler requires a compatible
explicit pin. Unsupported layouts and effectful project configurations remain
visible blockers.

Acquisition, cache fetching, dependency builds and import checks run in a private
project under exclusive setup ownership, OS isolation, cancellation and bounded
process resources. Original mathematics remains protected. A separate preparation
journal records infrastructure selection, preserves every displaced version,
recovers interrupted work and revokes readiness after a conflict resolution.
Preparation also checks the declaration inspector against the selected compiler
without exporting or approving any declarations. Compiler compatibility failures
stop before a translation author starts; a stale or missing check requires
preparation again.
Setup completes before normal translation ownership begins; an active author
cannot upgrade its lock or run dependency hooks against the canonical project.

The preparation receipt binds the checked compiler, dependency contents, configuration,
layout, source bundle and plan. It also shares the campaign budget with discovery,
author, reviewer and correction calls. Source changes invalidate the preparation
estimate for a new launch. Compiler, dependency or checker changes invalidate
translation acceptance and the prover handoff.

Reusing a preparation receipt also requires its exact selected, conflict-free
committed transaction. A missing, corrupt or older registry cannot erase retained
history or restore readiness. Recovery observes and preserves available versions;
an explicit acknowledgment records the loss and requires fresh preparation.
Progress records never reconstruct a missing plan, receipt or approval.

Native translation uses protocol 2. The controller first admits only the selected
representative source items and their complete regions. Definitions, instances and
constructions must be complete; pending named propositional theorems are allowed.
The slice is compiled and reviewed by a fresh source reviewer. Its receipt binds
its actual bytes, dependencies, source and representation choices. It stays private.
Only a passed slice permits full expansion; the completed document then receives
its own coverage, per-unit and whole-document review before publication.

## Source and editor boundaries

The deterministic provenance scanner partitions all source regions, including
prose outside theorem environments. Optional plasTeX comparison adds candidates;
neither parser certifies that all mathematics was found. Explicit item, boundary,
shared-mapping and region repairs remain revision-bound.
Class/package loading, inherited meanings, commands and conditionals can make
structural extraction uncertain, even for ordinary article sources. Eligible
diagnostics with exact frozen spans can receive an author-proposed literal source
reading in the normal workflow. The proposal binds every eligible diagnostic and
the complete original inventory; it grants no user verification, branch selection
or TeX execution authority. Fresh coverage and coherence reviews must independently
assess the reading before the corresponding translation stage can pass. Missing
inputs, unsupported effects, preprocessing and incomplete structural evidence stay
blocked. Explicit user repairs remain available as a separate path.
Recognized unfrozen file-loading aliases and file/I/O operations inside saved
macro replacement bodies also stay blocked. Old TeX inventories need explicit
`retry_extraction` before fresh translation or current acceptance, including old
inventories with no recorded warnings. Their original bytes and saved reviews
remain inspectable as history.

Reviewers see bounded contiguous groups of source regions. Every group binds its
exact original members, and controller validation expands complete group verdicts
back to canonical region IDs. The complete token census remains immutable and
retrievable; grouping does not shrink the coverage denominator. Literal custom
proof wrappers can supply provisional proof context without establishing proof
status. Known source clauses require exact authored dispositions and independent
clause verdicts. An omission, narrower specialization or unresolved requirement
blocks full-item acceptance even when disclosed in setup notes.

An explicitly paired PDF also permits `--source-draft-only` for checked setup and
one retained unaccepted author preview. This mode grants no publication or proving
authority, even if the author supplies a complete proposal. Resuming a completed
preview campaign makes no new author call.

PDF-only extraction never passes fidelity because a page has selectable text.
A page with raster mathematics requires verified full-page transcription, or
explicit user exclusion of all its regions. Replacing a transcription preserves
original PDF bytes and history while reopening item boundaries, mappings and proof
consent. This PDF-only gate is unchanged by companion image delivery.
For explicitly paired TeX/PDF, TeX remains statement authority. The controller
freezes rendered physical page images and native reviewers must retrieve every
assigned companion page before a positive coverage batch can count. Reviewers
assess correspondence and additional mathematics from the same frozen source
review; text metadata alone cannot establish image delivery. Required unextractable
material still needs source repair. Navigation remains approximate or unavailable
when the source does not establish an exact page correspondence.

Text evidence responses fit 16,000 UTF-8 bytes including their transport envelope;
full dossiers remain paginated. Page images use a separate bounded 6 MiB envelope.
Image receipts bind the PDF, physical page, renderer and PNG bytes, and publication
preserves the delivered image evidence with its source review.

The editor can inspect a source-only workspace, prepare its project, inspect frozen
TeX/PDF and Lean links, repair coverage and monitor each gate. Publication conflicts
have an independent version viewer and exact-observation resolution, even when the
normal document view is blocked. Keeping reconciled files archives the decision and
requires new mathematical acceptance. Restoring older accepted bytes does not
restore revoked authority.

Saved editor views and history are non-authorizing projections. Exact currentness
checks and prover startup retain the independent source, environment and recovery
gates. Native and checker cleanup requires physical output EOF and verified owned
process closure. Closing a local stream or recording a successful model response
does not establish cleanup; uncertain ownership preserves inputs for recovery.

Direct reuse distinguishes generated declarations, immutable existing-project
declarations and dependency declarations. Existing project references retain
verified file/module navigation and source hashes. They grant no write scope and
cannot add unrelated proof holes to the prover assignment.

## Verification boundaries

Deterministic tests cover positive and negative controller paths, recovery,
source repair, evidence pagination and editor message contracts. Real Lean tests
establish compiler behavior; a simulated reviewer does not establish native
semantic review. VSIX packaging does not establish installed editor behavior.

Release evidence must include a small unambiguous native author → fresh reviewer →
publication → prover journey, source-only and existing/custom-root layouts, direct
reuse and an unrelated existing proof hole. Keep expensive Lean and Mathlib tests
serial, measure owned process resources and confirm cancellation cleanup. Repeat
isolated installation and actual MCP startup on available supported platforms.
Record exact inputs and platform limitations in ignored evidence. A blocked paper
is useful negative evidence and cannot replace the successful small-source journey.

Native Windows publication and unsupported filesystems retain staged results.
Dynamic Lake configurations, local path dependencies, and source-only mathematical
dependency adapters remain outside the supported compiled-input boundary.

## Independent review prompt

Review the current pull-request revision and repository online. Assess whether
source-only inspection and preparation, dependency pin preservation and acquisition,
representative-slice enforcement, complete translation, source fidelity review,
transactional publication and exact prover handoff form a usable supported workflow.

Read `formalization/preparation*`, `slice_gate.py`, `proposal*`, `review*`,
`conflict_resolution.py`, `freshness.py`, publication/recovery and handoff code,
project setup execution, native launch/phase wiring, the shared CLI/editor contracts
and corresponding tests. Examine exact authority and recovery boundaries, including
missing dependencies, lost preparation registries, source-bound lexical readings,
exact grouped coverage, known clause dispositions, delivered companion page images,
PDF-only mixed raster pages, shared mappings, direct reuse, UTF-8 evidence pagination,
zero obligations, absent proof receipts, physical process/output closure and late saves.

Return actionable findings with severity, concrete trigger, file/line evidence and
consequence. Separate implemented behavior from live validation and supported
platforms from unavailable ones. Give a verdict for the refactor and a separate
verdict for the complete supported workflow. Do not treat model reports or a green
build as source-fidelity acceptance, and do not infer installed behavior from
packaging alone.
