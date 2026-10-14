# Formalization readiness and review scope

The source-backed formalization refactor implements intake, translation acceptance,
publication and scoped proof handoff. It is a review candidate, not completion of
the full formalization product. In particular, it still starts from a prepared Lean
project. [The workflow guide](formalization.md) describes implemented behavior;
the preparation contract below describes work that remains to be built.

## Plan coverage

| Area | Implemented boundary | Remaining work or evidence |
| --- | --- | --- |
| Source inventory | Frozen TeX/includes/macros or PDF; complete region partition; explicit scope and missed-item repair | Broader corpus evaluation, including custom macros and PDF-only documents; extraction alone does not certify coverage |
| Parser choice | Deterministic provenance scanner; optional plasTeX comparison adapter | Measure recall and provenance on a representative corpus before changing the default |
| Editor inspection | Revision-bound actions, TeX links, packaged PDF viewer, approximate page candidates, source/Lean mappings and separate review/proof status | Complete installed-editor and remote-workspace interaction checks, including PDF-only repair |
| Project organization | Allocation beneath an existing Lake library, explicit aggregator, real module membership and whole-project checks | Source-only folder setup, dependency planning and bounded creation of a dedicated library |
| Translation | Many-to-many units, library reuse evidence, complete definitions/data, named propositional proof slots, visible conditional constructions | A successful live native-author journey through representative-slice review and complete project acceptance |
| Semantic review | Fresh native reviewer, complete source evidence, bounded batches and final coherence, correction rounds and positive/negative caching | Independent assessment of semantic review quality on a small, unambiguous real fixture |
| Technical gate | Recorded pins and exact compiled dependency bytes, independent declaration and axiom checks, private project build, exact input fingerprints | More layout/platform coverage and resource profiling; path/source-only dependencies and dynamic Lake configurations remain explicitly unsupported |
| Publication | Revision selection, stale-author rejection, durable recoverable transaction and concurrent-edit checks | Broader filesystem/platform fault testing; unsupported publication environments must retain staged status |
| Prover handoff | Exact accepted proof slots, protected statements/definitions, proof preservation receipts and explicit authorization for open claims | Live author → reviewer → publication → prover run in the installed editor |
| Cleanup | Five obsolete formalization/runner modules removed; replacement tests and documentation | Recheck installed packages whenever publication/build inputs change |

These boundaries distinguish code and contract tests from product-level evidence.
A deliberately blocked paper is a useful negative test, but cannot establish the
successful end-to-end workflow.

Checking a representative slice is currently author guidance, not a separate
controller-enforced preparation milestone. The preparation design must make its
receipt and transition explicit if expanding the translation depends on it.

## Project preparation belongs to formalization

The intended user flow is:

1. Inspect the source and record unresolved mathematical questions.
2. Plan representations, prerequisite declarations, libraries and module layout.
3. Prepare the project and acquire the selected dependencies.
4. Compile and independently review a representative slice.
5. Translate the remaining units, review each batch and check global coherence.
6. Pass the full technical gate and publish the accepted revision.
7. Hand only the accepted proof obligations to the prover.

Formalization currently implements read-only dependency admission in
`leanflow_cli/formalization/intake.py`. It reports missing libraries or compiled
artifacts before starting the author. It does not infer and install mathematical
packages from a paper. The shared preparation service in
`leanflow_cli/workflows/project_setup/service.py` provides scaffolding, toolchain
checks, REPL configuration, Mathlib cache restoration and protocol readiness.
Reuse and extend those building blocks rather than introduce a second installer
or allow arbitrary author edits to Lake files. The service is not yet a suitable
transactional preparation boundary: its failure rollback restores only some files,
does not preserve concurrent saves, and does not establish that every dependency
has the compiled artifacts and layout accepted by formalization. A setup-ready
status is insufficient for this gate.

### Proposed preparation contract

Separate two kinds of dependency:

- **Packages and imports:** inspect the existing project and pinned Mathlib first;
  record which package/module supplies each required capability. Add a missing
  package only with an identified source, compatible toolchain, explicit pinned
  revision and reason. Record REPL tooling additions explicitly too. Transitive
  dependencies and effective compiler options are part of the prepared environment.
  Verify checkouts against the recorded pins and do not silently upgrade them.
- **Mathematical prerequisites:** search existing declarations and inspect their
  actual types and definitions. Reuse suitable material; implement missing data,
  definitions and instances. Missing lemmas may become explicitly mapped proof
  obligations. Missing semantics must remain an ambiguity rather than an invented
  axiom or a dummy definition.

Library search, complete definitions and named obligations are already part of
translation guidance and acceptance. Preparation plans and checks the shared
prerequisites early; it does not replace the translation or semantic review gates.

The plan should record the existing environment, proposed package changes,
representation choices, declaration reuse, new definitions, proof obligations and
file/import graph. The editor should show what was reused, added and left blocked,
with source links. A small representative slice must exercise the expensive or
uncertain choices before the whole paper is translated.

Preparation must be controller-owned and recoverable. Stage bounded configuration
and root-file changes, preserve unrelated project edits, acquire/build dependencies
with cancellation and resource limits, then verify actual imports and REPL behavior.
Restoration must distinguish an originally absent file from an existing file and
must preserve concurrent edits. Track acquired artifacts separately from authority;
an incomplete preparation cannot be admitted merely because its files exist.
Use the shared setup service for an empty folder as well as an existing project.
Existing pins, incompatible toolchains or unsupported Lake layouts must produce
actionable conflicts rather than implicit migration.

Package acquisition must respect the user's authorized sources and revisions,
network policy and existing setup consent. A proposed source or incompatible
upgrade outside that authority needs a decision before execution. Model-selected
Lake hooks must not run with unrestricted access to user state or credentials.
Use an explicit sandbox and resource policy for acquisition/builds; restricted runs
must not silently enable network access. The current setup subprocess service
inherits the host environment, so wiring it in unchanged does not satisfy this
contract.

Prepare infrastructure changes, including any new library and its import-only root,
in a separate transaction before translation intake freezes the environment.
Formalization scaffolding should not introduce an unrelated example proof hole;
existing unrelated user obligations must remain outside the handoff. Canonical
translation files still appear only after accepted publication. Check every selected
dependency's compiled imports and the existing project before paid author work,
with actionable reports for unsupported layouts and unrelated broken modules.

Successful preparation should produce a receipt bound to the formalization
environment identity: toolchain, manifests, package revisions, actual admitted
artifacts, effective options, layout and checker policy. The setup service's lighter
readiness fingerprint is not a substitute. Current dependency admission fingerprints
actual bytes and records manifest pins; it does not certify checkout provenance or
source-to-build correspondence. Those claims must remain separate.

If translation reveals another required package or representation change, stop with
an explicit preparation-required outcome. Settle the run and release its activity
and controller ownership before launching exclusive setup. Publish the new prepared
environment, create a new translation revision and recheck/review before continuing.
Setup cannot run inside the current formalization run's shared ownership. The rule
against changing dependencies inside an author turn remains in force.

Environment binding already exists and is conservative: any bound dependency or
LeanFlow controller Python change invalidates the handoff and all associated review
cache entries. Selective invalidation is not implemented. A stable handoff therefore
requires retaining its compatible checker/runtime, or explicit revalidation after
an upgrade. Define that migration contract before promising seamless upgrades;
never weaken acceptance identity just to reuse a review. A preparation rerun that
leaves the authority inputs unchanged should preserve the existing identity.

The prover must receive an importable, organized project with checked definitions
and exact named proof obligations. Dependency acquisition, source interpretation,
representation design and initial file organization are not prover repair tasks.

### Acceptance scenarios for preparation

- A source-only folder obtains a pinned Lean/Mathlib project and working REPL.
- An existing project reuses available declarations without unnecessary packages
  or changes to unrelated files and pins.
- A genuinely missing package is identified, pinned, acquired and import-checked;
  incompatible versions or unavailable artifacts stop with a clear explanation.
- REPL tooling is a recorded change; package sources outside existing authority,
  restricted-network runs and effectful hooks obey the preparation policy.
- A missing mathematical definition is implemented and reviewed; an undefined
  source concept remains visibly blocked.
- A layout requiring a new library is prepared transactionally and becomes part
  of the real build, including the aggregator and generated modules.
- Cancellation, acquisition failure and concurrent edits preserve the original
  project, including originally absent manifests, and allow recovery without
  selecting a half-prepared environment. Failed setup cannot pass admission.
- Returning from an active author run releases ownership before setup starts;
  newly prepared configuration/library files are committed before new intake.
- A later dependency change uses the existing conservative invalidation policy;
  an unchanged setup rerun does not unnecessarily invalidate authority.
- A LeanFlow update between publication and proving gives a deliberate compatible
  runtime or revalidation path, without silently inheriting incompatible evidence.
- The accepted project reaches the prover without a setup or representation task.

## Verification still required for release

The deterministic suite covers controller, inventory, review transport, publication
recovery, handoff and editor contracts. Real Lean integration tests are separate
opt-in checks; a test using real Lean with a simulated reviewer does not establish
live native review. Likewise, packaging a VSIX is not installed UI verification.

Use a small, self-contained source for the positive end-to-end fixture: a definition,
a theorem, a conditional choice construction, a reused library declaration and a
deliberately missed prose statement. Exercise `Main`, an existing library and a
custom source root. Verify both rejection cases and a successful native author and
fresh reviewer path through publication and exact proof handoff. Include duplicate
occurrences, a repaired mapping, a changed revision and an unrelated existing proof
hole that must not enter the prover assignment.

Run expensive Lean/Mathlib checks serially while measuring peak memory, cancellation
and subprocess cleanup. Establish practical aggregate resource bounds before
parallelizing them. Attribute resource failures with measurements before claiming
that a cache or concurrency change fixes them.

Repeat real installation and publication checks on supported platforms. Existing
macOS/Linux smoke evidence does not imply Windows/WSL or interactive remote-editor
success. Keep machine-specific logs and private input papers outside this repository.

## Independent repository review

Review the exact commit identified by the pull request, including new files, rather
than relying on this document or a previous review's verdict. Start with:

- `leanflow_cli/formalization/{intake,source_inventory,source_tex,source_pdf}.py`
- `leanflow_cli/formalization/{proposal,review,review_dossier,service}.py`
- `leanflow_cli/formalization/{dependency_inputs,dependency_snapshot,lean_build,lean_inspector}.py`
- `leanflow_cli/formalization/{publication,publication_recovery,handoff,proof_receipts}.py`
- `leanflow_cli/workflows/project_setup/` and the formalization CLI/native entry points
- `vscode-extension/src/core/formalization*.ts`, its host and webview components
- `tests/leanflow/test_formalization_*.py`, `test_source_inventory.py` and editor tests

Assess correctness of the current acceptance boundary separately from completion
of the preparation contract. Prioritize missed source material, stale review reuse,
changed dependencies/artifacts, dummy definitions, unauthorized publication,
concurrent-save loss and prover scope widening. Also assess whether the workflow
is understandable to a user navigating between PDF, TeX and Lean.

Return actionable findings with file/line evidence, severity, a concrete failing
scenario and a regression test. Identify unsupported claims in the documentation.
Give separate verdicts for merging the foundational refactor and declaring the
formalization product complete. Recommend the smallest next implementation slice
for project preparation and a final release acceptance checklist. Do not infer
test execution from the presence of tests or approval from model narration.
