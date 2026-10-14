# Source-backed formalization

Formalization prepares and reviews the mathematical statements and definitions that
the prover will use. Coverage, Lean elaboration, source fidelity and proof completion
are separate results. A green build alone does not establish a faithful translation.

For supported boundaries and release evidence, see [formalization readiness and review scope](formalization-readiness.md).

Start in a source folder or an existing Lean project:

```bash
leanflow formalization inspect docs/paper.tex --json
leanflow formalization inspect docs/paper.tex --companion-pdf docs/paper.pdf --json
leanflow workflow formalize docs/paper.tex
leanflow formalization list --json
leanflow formalization show <document-id> --json
```

Before translation, LeanFlow estimates representations and dependencies in a
read-only native session, prepares pinned infrastructure privately, and checks a
representative slice with a fresh reviewer. An existing project keeps its package
pins and mathematical files. The prover receives a checked setup and reviewed
statements after complete translation acceptance.

For deterministic inspection and explicit setup without launching a model:

```bash
leanflow formalization plan <document-id> --expected-revision <revision-id> --json
leanflow formalization prepare <document-id> --expected-revision <revision-id> --json
```

Automatic native launch starts with Lean core and selects the shipped Mathlib version
when discovery requires it. Explicit `plan` and `prepare` commands default to Mathlib;
`--preset lean` prepares a core-only project. `--plan-json` accepts explicit pinned packages and imports;
`--offline` forbids acquisition. A model recommendation cannot authorize a new
package source. Setup runs privately and selects infrastructure only after checking
it. Interrupted publication or concurrent saves preserve every version and require
recovery or explicit reconciliation before another launch.

Preparation publishes only changed infrastructure entries and preserves existing
dependency Git settings. Exact checked infrastructure can be reused for a new source
revision when its layout, pins and requested imports match the checked receipt.
Private stages and unused incoming entries are removed only after controller-owned
process cleanup is verified; uncertain ownership retains the files for recovery.
Retained transaction history has its own identity audit, while current environment
acceptance still checks complete dependency bytes.

Changed local Lean files require a setup check before the next translation, including
files added by an earlier publication. Proof handoffs have their own freshness checks.
Setup bounds Lake concurrency and labels its sampled process-RSS sum conservatively;
shared mapped pages can appear in more than one process's RSS.
The default setup ceiling is 16 GiB. Set `LEANFLOW_SETUP_MAX_RSS_GIB=20` for a larger
project; whole-number values from 1 to 20 are supported. Worker retention stays
bounded when the ceiling increases.

Ambiguous compiler acquisition history preserves every version. A healthy installed
compiler can still be checked and reused. If acquisition needs an acknowledgment,
use the journal and target paths from the setup diagnostic:

```bash
python -m leanflow_cli.workflows.project_setup.toolchain_recovery inspect \
  --journal <journal-path> --target <compiler-target>
python -m leanflow_cli.workflows.project_setup.toolchain_recovery acknowledge \
  --journal <journal-path> --target <compiler-target> \
  --observation <observation-id> --confirm-processes-stopped
```

Inspect the preserved versions and check that acquisition processes stopped before
acknowledging. A changed observation or a live original owner rejects acknowledgment;
acknowledgment records the uncertainty and never deletes ambiguous compiler files.
Preparation keeps its immutable plan and receipt in full snapshots, with small
durable per-entry progress records between transaction state changes. Large
changed-file sets therefore write journal data in proportion to their size.
Recovery binds each progress record to its snapshot before checking paths and
retained versions; progress cannot replace a missing plan or receipt.

The source may be a TeX file, an unambiguous TeX project directory or a PDF. Inspection
freezes source files and creates a complete region inventory without creating Lean
files. Project preparation creates import-only roots and the actual Lake layout. The author works in an isolated copy. LeanFlow allocates modules under an
actual Lake library root, including projects whose library is named `Main`.

An explicitly selected companion PDF is frozen with the TeX bundle. This records an
association, not verification that the PDF is the same rendering or has identical
contents. The original TeX spans remain the statement authority. Shared prose can
provide approximate physical-page navigation; ambiguous matches retain multiple
candidate pages and missing matches remain unavailable. Companion pages require
coverage audit, and missing TeX figures or bibliographies remain acceptance blockers. Subsequent
launches preserve the explicit pairing; changing either file creates a new revision.
The editor picker accepts one TeX file and one PDF together. A same-named neighboring
PDF is never paired automatically.

With a frozen explicit companion PDF, missing `includegraphics`, `bibliography` or
`addbibresource` files permit preparatory author work only. The author can explore
definitions and save a representative draft with exact source-gap notes, then report
an honest blocker. The controller stops after one completed author turn, even if the
author claims completion or submits a proposal; the private draft remains available.
Repair the source and restart before attempting acceptance. Diagnostics stay blocking for complete proposal acceptance;
coverage, publication and prover handoff are not approved. No source material is
silently excluded and the original TeX is unchanged. Missing included TeX files,
uncertain macros, parse failures and PDF extraction errors still stop before author launch.

## Inventory and source navigation

The provenance scanner records original UTF-8 byte spans, labels, complete statements
and proofs, include occurrences and macro context. Regions include prose outside
recognized theorem environments. Counts of detected statements are useful for
navigation; they are never the denominator for claiming complete source coverage.

The deterministic scanner is the initial default. The optional plasTeX comparison
adapter is a measurement and compatibility seam, not an installed runtime requirement
or proof that extraction is complete. Unknown constructs and missing includes remain
visible. Adopting another default parser requires corpus evidence, including custom
macros, included files, long proofs and missed statements.

In VS Code, use the Formalization view to inspect items, classify remaining regions,
add a missed item, repair a boundary, change an explicit scope decision or repair a
Lean mapping. Each action names the visible revision and fails if it changed. Explicitly selecting
a revision also revokes a running author’s authority to replace that selection.
An unknown command can be repaired by inspecting all its frozen occurrences and
recording a verified interpretation. This reopens affected coverage for review; it
does not waive missing files or approve a translation. The host resolves artifact IDs and verifies content before opening it; webviews do not
choose arbitrary host paths. PDF.js is packaged locally and needs no LaTeX extension.
PDF links identify physical pages and distinguish approximate from exact locations.
If coordinate extraction fails but complete page text is available, intake reports
the fallback and provides approximate page-only links without fabricated boxes.
Image-only or missing-text pages need a verified transcription or source repair;
acknowledging a warning cannot turn unknown content into complete coverage.

## Definitions, files and acceptance

The proposal records many-to-many translation units: source IDs, exact Lean names,
representation notes and dependencies. This supports split statements, merged setup,
supporting lemmas and direct library reuse. Every included source item is mapped;
every selected region has an explicit disposition and review verdict.

Definitions, data fields and instances must be implemented. Holes are permitted only
in named propositional proof slots. A complete opaque definition is valid. A
construction using a pending named existence theorem is conditional on that theorem,
and the dependency stays visible. Already proved claims and exact pinned library
declarations need no artificial `sorry`. Source conjectures retain their claim status.
The reviewer classifies that status from the content, including any supplied proof.
An environment titled “Conjecture” is only a detection hint. Open or unknown claims
cannot become prover assignments until the user selects **Allow proof attempt** and
records a reason. That permission is revocable and never grants source approval.

The controller explicitly compiles every generated module, checks the complete kernel
declaration registry including auxiliary declarations, then compiles every declared library module and executable root. Undeclared
scripts are reported separately and do not become mathematical modules. Both private gates invoke pinned Lean directly with compiler-parsed
import ordering and exact artifact paths; they do not run dependency Lake files or
Git package materialization. A final canonical Lake build remains a separate project
integration check. The aggregator imports the generated modules. Only the controller registers
the bounded root import; the author cannot rewrite existing project files or
dependency configuration. The initial supported layout is static Lake TOML/DSL.
Standard `lake new` math, math-lax and library/executable layouts are supported in
TOML and the bounded Lean DSL, including literal package versions, keywords and
scoped Git requirements. The validated DSL is translated offline by pinned Lake
in a configuration-only copy. Effective package/library/executable Lean options
are fingerprinted and passed to each compiler invocation. The option allowlist is
`autoImplicit`, `relaxedAutoImplicit`, `pp.unicode.fun`,
`weak.linter.mathlibStandardSet`, `maxSynthPendingDepth`, `maxRecDepth` and
`maxHeartbeats`; other options require an adapter. Non-release `buildType` settings
remain blocked. The whole-project gate elaborates executable roots; it does not
link or run native executables, and mathematical libraries cannot import them.

Snapshots exclude configured build directories. Each module compiles in fresh
scratch storage, with earlier artifacts read-only. The controller copies only the
expected compiled artifacts into a clean library directory and checks their exact
hashes; unexpected files, directories and altered earlier outputs block acceptance.
Build directories must be disjoint from dependency directories in both directions.
Each configured library must make all its roots buildable. Selective or rootless
globs need a separate membership adapter. Mathematical libraries must have disjoint
top-level module roots and cannot capture any root supplied by the pinned compiler.
Unsupported dynamic settings, effectful generated syntax and loader overrides fail
with a blocker rather than silently weakening the accepted-input contract.
Launch and resume apply the builder's read-only admission before starting an author:
source-only dependencies, missing compiled library artifacts and conflicting module
roots require repair first. This admission does not establish kernel or source acceptance.

## Independent reviews

```bash
leanflow workflow formalize docs/paper.tex --review-provider claude --review-model <model> --review-effort max --correction-rounds 2
```

The reviewer runs in fresh native sessions, without the author's conversation. Each
coherent batch receives complete required statements and proofs plus scoped read-only
retrieval of frozen context. Every translation unit and required source ID needs a
verdict. Large projects use batches of at most eight units and forty source regions,
then a final review checks the complete project and prior verdicts together. Small
projects combine region coverage with that final review. Reviewers can retrieve
exact region text and unchanged Lean definitions from the frozen project.
The reviewer can block a dummy definition even if Lean accepts it.
Large dossiers are paginated without dropping trailing text or other evidence.
Each tool response is bounded to 16,000 UTF-8 bytes; reviewers can request smaller
pages. Batches contain their declaration summaries, with complete compiled bodies
available separately. Inaccessible evidence is an operational failure and cannot
be cached as a semantic rejection.
All batch feedback is collected before correction. A malformed reply or failed
reviewer launch is an operational error, not a mathematical rejection.

Reviewer calls share the campaign's existing call, token and time budget. Two
correction rounds are allowed by default. Both positive and negative decisions are
cached by the complete evidence and reviewer contract. An unchanged rejection is
not rerolled, including when only Lean comments change. Positive reuse requires
exact bytes. Changing source, scope, representation, module contents, environment
or policy invalidates the corresponding acceptance; historical receipts remain
available without becoming approval of a newly selected revision.

## Publication and proof handoff

Passing files and evidence are published together under a recoverable transaction.
The portable `formalization/<document-id>/` directory contains immutable revisions,
source blobs, immutable receipt history, prior review decisions, `Blueprint.md` and
`handoff.json`. Private native logs and disposable caches remain under `.leanflow/`;
removing private caches does not
remove the published source links.

Publication journals and retained file versions belong to the project under
`.leanflow/formalization-publication-transactions/`; author-run folders contain
only pointers, so cleaning an old run preserves recovery evidence. Keep these
transaction directories when removing disposable caches. If historical evidence
is unavailable, publication recovery can acknowledge its exact observed loss
while keeping current workspace files. This permits fresh work and revokes the
affected acceptance; missing versions and write plans are never reconstructed.

The transaction records its exact write set before mutation, refuses overwriting a
concurrent save and retains displaced files for recovery, including writes through
already-open editor handles. Pending transactions and conflicts block cooperating
LeanFlow readers. Starting a workflow automatically recovers an interrupted
transaction before loading source or handoff evidence; conflicting user edits
remain preserved and block the new run. Ordinary external filesystem readers can observe intermediate
files; multi-file publication is not claimed to be globally atomic. On unsupported
hosts/filesystems the result stays `accepted_staged` and is not advertised as
published. Resolve a reported conflict before starting another run.

Start proving from the selected published revision:

```bash
leanflow workflow prove --from-formalization <document-id> --formalization-revision <revision-id>
```

The handoff fixes exact source hashes, declarations and proof slots. It never falls
back to every hole in the project. Unrelated holes remain protected. With zero
obligations, no prover model is launched. Proof-only changes can receive a new
independently checked preservation certificate that inherits the original source
review. Changed statements, definitions, imports or environmental inputs require a
new review. The original acceptance record is never edited to manufacture proof
completion.

## Supported input boundary

The pinned compiler and its standard library form the kernel base. Fully pinned
Git dependencies, including Mathlib, use a compiled-library adapter with the
conventional `.lake/build/lib/lean` layout. Every source, configuration, resource
and compiled artifact is fingerprinted. The exact compiled declarations are the
mathematical authority: a Git pin does not certify that a cached artifact was built
from that checkout. Reviewers receive the compiled types and definition bodies,
including recursively referenced library declarations, and must assess their
meaning when checking the translation.

Repeated inspections merge body detail and review scope only when compiled identity
agrees. Conflicting module attribution or compiled meaning for the same name blocks
the proposal; use an unambiguous mapping rather than relying on import order.

The compiled statement is the translation authority. Reviewer dossiers prioritize
its semantic surface: statement types and non-proof definition bodies, with
implicit arguments and instances exposed. Proof-only dependencies remain available
in the complete audit evidence. A reviewer must block when the compiled meaning
cannot be established. Receipts explicitly record that source-to-build provenance
is unverified and that source-fidelity review is an assessment, not a proved
semantic equivalence. The pinned Lean kernel and binary importer, including their
memory safety, remain trusted implementation components; kernel replay is not a
separate memory-safe validation of hostile binary encodings.

All shipped controller, isolation and reviewer Python sources participate in the
checker identity. Changing a shared helper invalidates prior acceptance just as
changing the inspector does.

The controller invokes the pinned Lean executable directly for inspection, without
evaluating dependency Lake files, loading plugins or running imported initializers.
It replays every generated declaration, or explicitly selected library declaration,
and the complete recursive closure of their types and values through the kernel.
Unused library proofs are not certified by a check of an unrelated statement.
The complete import inventory still rejects extra axioms and compiler-root
shadowing, and checked declarations cannot depend on omitted unsafe or partial
implementation code. Verdict computation and rendering use only the replayed
environment; imported native reducers cannot execute there. Generated declarations still pass their
separate hole, axiom and construction policies. Package names and repository URLs
never substitute for these checks.

Without an explicit campaign deadline, independent formalization inspection allows
ten minutes for input verification, compilation and kernel replay. Cold Mathlib
imports can spend several minutes on setup alone. Explicit deadlines remain binding;
ordinary statement drafting retains its existing five-minute default.

Normal project setup's REPL package is fingerprinted as tooling when its declared
library and executable have the conventional REPL layout. Its library search path
is unavailable to the mathematical checker, including through transitive imports.
Other package layouts receive the ordinary library checks. Dependencies without a
full Git pin retain the conservative declarative-source inventory policy, but the
private compiler currently rejects source-only dependency builds with
`unsupported_declarative_dependency_build`. Those packages need a recorded static
package build adapter; inventory admission alone does not establish build support.

Formalization snapshots copy canonical dependency bytes privately and compare their
identities before and after copying and checking. They do not substitute a shared
compatibility cache. Confined relative resource-file links are copied by value;
directory, Lean-code, compiled-code, configuration and escaping aliases are refused.
Git bookkeeping is omitted from the checker view. Input changes invalidate review
and proof-preservation receipts.

Project-local elaboration extensions, executable project Lake hooks and loader
overrides remain unsupported. Dependency elaboration is untrusted: acceptance binds
its actual compiled output and independently checks that output, rather than
claiming deterministic execution of arbitrary tactics. The persistent fingerprint
binds compiler content, dependency bytes and checker policy. These formalization
checks do not change ordinary proving.

Dependency inventory is checked before paid author work and again on direct or
resumed launches. Passing inventory establishes input availability only; it never
replaces kernel replay, source review or the whole-project build.

## Stale acceptance and recovery

Editor refreshes reuse display hashes only while complete input metadata, including
inode, size, modification time and change time, remains unchanged. Actual acceptance
and prover startup independently check the bytes. A runtime, compiler or dependency
change disables the old handoff while preserving its evidence. Prepare the project
and run formalization again under the current checker; complete declarations can be
reused from preserved modules after fresh source review.

Publication conflicts can be inspected through the editor or
`leanflow formalization conflicts --json`. Reconcile the current workspace files,
then confirm the exact observed versions with a reason. Resolution archives every
version and revokes old acceptance. Infrastructure has its own
`preparation-conflicts`, `recover-preparation` and `resolve-preparation` operations;
normal launches remain blocked until every pending conflict is resolved.
