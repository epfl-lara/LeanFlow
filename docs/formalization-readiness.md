# Formalization readiness and review scope

Formalization owns source inspection, dependency preparation, translation, review,
publication and the exact prover handoff. These stages have separate evidence.
A prepared project, a compiling translation and a completed proof are different results.
See [the workflow guide](formalization.md) for commands and editor use.

## Preparation and translation

A real formalization launch starts by freezing its source and estimating the
mathematical prerequisites, useful existing declarations, imports, representation
choices and a representative slice. The estimate comes from a fresh read-only
native session. It can propose dependencies; it cannot authorize an unlisted
source, replace an existing package pin or approve its own mathematics.

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
Setup completes before normal translation ownership begins; an active author
cannot upgrade its lock or run dependency hooks against the canonical project.

The preparation receipt binds the checked compiler, dependency contents, configuration,
layout, source bundle and plan. It also shares the campaign budget with discovery,
author, reviewer and correction calls. Source changes invalidate the preparation
estimate for a new launch. Compiler, dependency or checker changes invalidate
translation acceptance and the prover handoff.

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

PDF-only extraction never passes fidelity because a page has selectable text.
A page with raster mathematics requires verified full-page transcription, or
explicit user exclusion of all its regions. Replacing a transcription preserves
original PDF bytes and history while reopening item boundaries, mappings and proof
consent. Until native reviewers receive rendered pages, text retrieval cannot
substitute for that visual source audit. An explicitly paired TeX/PDF retains TeX
as statement authority and approximate or unavailable PDF navigation.

The editor can inspect a source-only workspace, prepare its project, inspect frozen
TeX/PDF and Lean links, repair coverage and monitor each gate. Publication conflicts
have an independent version viewer and exact-observation resolution, even when the
normal document view is blocked. Keeping reconciled files archives the decision and
requires new mathematical acceptance. Restoring older accepted bytes does not
restore revoked authority.

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
missing dependencies, PDF-only mixed raster pages, shared mappings, direct reuse,
UTF-8 evidence pagination, zero obligations, absent proof receipts and late saves.

Return actionable findings with severity, concrete trigger, file/line evidence and
consequence. Separate implemented behavior from live validation and supported
platforms from unavailable ones. Give a verdict for the refactor and a separate
verdict for the complete supported workflow. Do not treat model reports or a green
build as source-fidelity acceptance, and do not infer installed behavior from
packaging alone.
