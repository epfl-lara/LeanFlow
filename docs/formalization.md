# Source-backed formalization

Formalization prepares and reviews the mathematical statements and definitions that
the prover will use. Coverage, Lean elaboration, source fidelity and proof completion
are separate results. A green build alone does not establish a faithful translation.

For supported boundaries and release evidence, see [formalization readiness and review scope](formalization-readiness.md).

Start in a source folder or an existing Lean project. Check the project baseline
before launching translation:

```bash
leanflow project setup --preset mathlib
leanflow formalization inspect docs/paper.tex --json
leanflow formalization inspect docs/paper.tex --companion-pdf docs/paper.pdf --json
leanflow workflow formalize docs/paper.tex
leanflow formalization list --json
leanflow formalization show <document-id> --json
leanflow formalization check <document-id> --expected-revision <revision-id> --json
```

`list`, `show` and editor source navigation display saved evidence without scanning
the current Mathlib environment. Their `currentness: historical` projection grants
no proof handoff, even when it displays a saved successful review. Use **Check current
acceptance** or `formalization check` for exact currentness; proving independently
rechecks admission. A repair can return `--acknowledge-only`: its committed revision
is acknowledged before a separate view refresh, so a slow refresh cannot turn a
saved edit into an apparent failure. Source links stay usable during currentness checks.

In VS Code, **Set up project** is the first step in the Formalization tab. It
checks Lean, the installed dependencies and the REPL without launching a model.
An existing core-only project can remain core-only; use `--preset lean` for a new
minimal baseline. Existing source files, toolchains and package pins are preserved.

After an interrupted baseline setup, first retry setup for controller-verified recovery.
If a competing editor version or a dead controller still blocks it, inspect the preserved
versions with `leanflow project recovery inspect <project> --json`. After independently
confirming every old process and output writer stopped, acknowledge that exact
`observation_id` with `leanflow project recovery acknowledge <project> --observation <id>
--confirm-processes-stopped --confirm-output-closed --keep-current --json`, then run setup
again. Acknowledgment keeps current files and every retained version; it records external
confirmation and does not claim verified physical EOF, mathematical acceptance, or readiness.
Live processes, missing identity evidence, changed observations and later retained-inode
edits keep recovery blocked. Uncertain inputs remain retained. Verified closed command
records and terminal setup journals move into indexed archives before the 256-record
active limit; archived versions remain audited, and bounded archive exhaustion fails
with a preservation instruction rather than deleting history.
Acknowledgment writes a positive fresh-setup barrier before recording external closure.
An interrupted acknowledgment, a missing barrier, or a missing or rolled-back archive
blocks readiness. A failed subsequent setup can be retried while preserving the exact
acknowledged versions; it does not require acknowledgment again solely because setup
temporarily promoted a file. An archive missing its trusted external membership anchor
requires restoring trusted history, rather than reconstructing approval from surviving
files.

Translation requires a current setup checkpoint or stronger current formalization
preparation before any planning call or campaign budget is created. The native
planner then estimates representations, existing declarations and missing
dependencies. The controller privately prepares any approved expansion and checks
a representative slice with a fresh reviewer. This expansion uses the same setup
service as `leanflow project setup` and its `project init` alias. The terminal
reports setup phases and log paths; verbose dependency output remains in those logs.
The prover receives checked infrastructure and reviewed statements after complete
translation acceptance. Baseline setup alone grants no source or proof approval.
Declaration search hints support qualified Unicode names and primes, question marks
and exclamation marks, such as `Finset.sup'`, `List.head?` and `Option.get!` (see
the [Lean identifier rules](https://lean-lang.org/doc/reference/latest/Source-Files-and-Modules/)).
These are bounded, unverified hints rather than a complete Lean identifier parser.
Imports and project paths retain
their separate portable module-name checks. A valid hint never establishes that a
declaration exists or has the required type.

For deterministic inspection and explicit setup without launching a model:

```bash
leanflow formalization plan <document-id> --expected-revision <revision-id> --json
leanflow formalization prepare <document-id> --expected-revision <revision-id> --json
```

New baseline projects use Mathlib by default. A later native plan can recommend
adding compatible Mathlib to an existing minimal project. Explicit `plan` and `prepare` commands default to Mathlib;
`--preset lean` prepares a core-only project. `--plan-json` accepts explicit pinned packages and imports;
`--offline` forbids acquisition. A model recommendation cannot authorize a new
package source. Setup runs privately and selects infrastructure only after checking
it. Interrupted publication or concurrent saves preserve every version and require
recovery or explicit reconciliation before another launch.

Native planning receives the same field limits enforced by the validator. It can
retain up to 128 representation notes, each containing 1–4,000 Unicode characters,
with a combined 512 KiB UTF-8 limit. Exceeding a limit reports the count or size and
preserves the original recommendation; notes are never silently dropped. These
notes record estimates and ambiguities, without granting translation approval.

An interrupted compilation capture remains a failed check and retains its cause.
If capture was interrupted while stopping an owned process after the compiler
had already exited successfully, and final process and output closure are verified,
setup can make one fresh compilation attempt against unchanged pinned inputs within
the original time limit. Both attempts remain in the diagnostic record. Late output
pipe closure, unknown process closure, changed inputs, cancellation, expired limits
and Lean errors stop setup.

Preparation publishes only changed infrastructure entries and preserves existing
dependency Git settings. Exact checked infrastructure can be reused for a new source
revision when its layout, pins and requested imports match the checked receipt.
Root dependency pins take precedence over deeper package manifests. New transitive
pins follow Lake's reverse root-requirement order and breadth-first priority; later
build outputs cannot replace those independently checked selections.
Private stages and unused incoming entries are removed only after controller-owned
process cleanup is verified; uncertain ownership retains the files for recovery.
New Lake file-hash caches outside the build directory are independently verified
against unchanged inputs using the pinned Lake API after process quiescence.
Only those exact verified additions are retained as prepared infrastructure and
published with their input, pin and cache identities. The original dependency
source inventory remains unchanged; validation admits its exact bytes plus the
explicit verified caches. Keeping these caches allows later read-only Lake builds
to reuse them. Tracked files, preexisting caches and unrelated additions remain
protected, and complete environment fingerprints include the retained caches.
Retained transaction history has its own identity audit, while current environment
acceptance still checks complete dependency bytes.

Changed local Lean files require a setup check before the next translation, including
files added by an earlier publication. Proof handoffs have their own freshness checks.
Setup bounds Lake concurrency and labels its sampled process-RSS sum conservatively;
shared mapped pages can appear in more than one process's RSS.
The default setup ceiling is 16 GiB. Set `LEANFLOW_SETUP_MAX_RSS_GIB=20` for a larger
project; whole-number values from 1 to 20 are supported. Worker retention stays
bounded when the ceiling increases.

Preparation tolerates up to four consecutive OS or subprocess metadata gaps;
the fifth stops the stage. Identity ambiguity, malformed observations, policy
failures and observed memory excess stop it immediately. A fresh healthy sample
and verified descendant cleanup are required before preparation can finish and
publish infrastructure. Private diagnostics retain only bounded counters and
allowlisted failure categories. An unjoined monitor keeps serial ownership until
it has irrevocably finished every process inspection and cleanup action. One
terminal diagnostic writer may then finish without holding serial ownership;
the uncertain stage remains retained.

Native author and reviewer calls also sample the combined RSS of the native CLI
and its observed children, including builds started through native shell tools.
`LEANFLOW_NATIVE_MAX_RSS_GIB` selects a whole-number ceiling from 1 to 20 GiB;
without it, calls use `LEANFLOW_SETUP_MAX_RSS_GIB`, then the 16 GiB default.
Exceeding the ceiling or losing process inspection stops the invocation and requires
verified process and output closure before further work. The metric counts shared
pages per process and cannot measure peaks between samples.

The Formalization and Prove controllers also enforce that same ceiling across the
controller and all its observed workflow processes together. This includes native
authors and reviewers, bridge commands and Lean checker siblings. A separate
quarter-second monitor cancels the workflow after an observed excess or five
consecutive inspection failures; launch and acceptance boundaries require fresh
measurements. Preexisting unrelated child branches are excluded, while already
owned warm checkers are included. Process RSS counts shared pages repeatedly,
and short peaks between samples may be missed. The aggregate monitor does not
replace each actor's independent process and physical output-closure checks.

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
Unreadable journals have a separate observation of their exact raw bytes and target
version. Acknowledgment preserves those bytes and does not reconstruct missing
acquisition authority.
Preparation also compiles the declaration inspector against the selected compiler
and checks an empty kernel replay before translation starts. This compiler-only
compatibility check approves no declarations. Missing or stale inspector readiness
requires preparation again.

Preparation keeps its immutable plan and receipt in full snapshots, with small
durable per-entry progress records between transaction state changes. Large
changed-file sets therefore write journal data in proportion to their size.
Recovery binds each progress record to its snapshot before checking paths and
retained versions; progress cannot replace a missing plan or receipt.
Stopping immediately after infrastructure commits records the committed receipt
and stops before author launch. A later launch can reuse that checked setup.
Each new formalization run durably binds its source selection and campaign budget
before the first paid planning call. Resume reopens that exact ledger with the
same effective limits, including after failed preparation that published no
receipt. Stale setup or controller readiness requires fresh checking, not a fresh
allowance. A missing, inconsistent or legacy unbound budget stops before paid
work with recovery or explicit new-campaign guidance; the controller never chooses
the newest ledger or infers a binding from displayed counters. Changing campaign
limits requires an explicit new campaign. Retained historical usage remains
visible and is not silently merged into a different campaign.
Preparation checkpoints active time even when its native call emits no output.
Translation does the same. A durable active-writer marker prevents a killed or
failed checkpoint writer from silently refunding quiet native time on resume.
Accounting persistence failure cancels the author and blocks review, publication
and another admission. Stop waits a bounded time for the checkpoint thread; if
closure remains uncertain, ownership and private inputs stay retained. Contended
display or activity writes are skipped and cannot hold up accounting or Stop.
An unfinished checkpoint writer retains exclusive accounting ownership until its
last write ends; a bounded shutdown failure cannot admit a competing writer.
Actual CLI launches publish a verified preparation owner before dependency planning,
so the same Stop action also interrupts discovery and setup. A one-time CLI restart
establishes an initially inherited process token while preserving its PID, interpreter
flags and native authentication. Preparation releases only its exact owner before
the translation controller takes over. Preview and direct API inspection do not
create Stop authority. Preparation and translation each enforce the shared workflow
memory budget and keep process closure separate from mathematical acceptance.
During preparation, Live receives current phases, native activity, heartbeats and
the shared campaign counters. It labels retained mathematical results as belonging
to the prior run. A preparation failure has its own error field, and a heartbeat
does not advance the time of the last real activity.
Dependency discovery starts from a compact frozen index of every source item and
known clause, with exact source locators and complete per-item evidence. The full
canonical inventory remains available and still determines coverage. Discovery
has a separate 15-minute wall-time limit, configurable with the finite positive
`LEANFLOW_FORMALIZATION_DISCOVERY_WALL_TIME_S` setting, capped by the campaign's
remaining time. A quiet process is still bounded by that deadline. Verified
cancellation or timeout preserves the draft and charged campaign history and is
reported separately from unverified process cleanup.
Native tool activity retains compact receipts. Known check failures have bounded,
controller-authored summaries without copying raw error text, tool arguments or
results into public activity. Unknown failures retain their operation ID. A source
change during an asynchronous check invalidates that attempt; finish polling a
check before editing any project Lean file or building modules, then check the
corrected files again.
Kernel replay currently rejects unsafe/partial project constants, including the
executable helpers Lean can generate for an ordinary safe recursive definition.
A complete noncomputable mathematical definition or an appropriate existing fold
avoids that unsupported implementation form. Computability and efficiency still
need their own faithful named theorem obligations; a noncomputable declaration
does not establish either claim. Compiler names and navigation hints cannot waive
the independent kernel check.

Source-reading rejections identify the entry and failed constraint without exposing
its proposed text. Assignment guidance lists the exact required fields and limits;
these checks validate metadata only and do not grant source-fidelity approval.
Progress updates skip busy status locks. Preparation ownership changes have a
finite lock wait, and failed release prevents translation from taking over.

Receipt reuse requires positive membership in the selected, conflict-free committed
preparation transaction. Missing or corrupt registries and older indexes that omit
retained transactions are authority loss. Planning, preparation readiness and reuse
stop until the exact observed history is reconciled. Acknowledgment preserves the
available versions and selects no approved preparation; a fresh checked transaction
must establish readiness. Progress records are recovery evidence, never replacement
approval.

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
unsupported effects, incomplete structural evidence and PDF extraction errors remain
hard blockers. Eligible lexical uncertainty has the ordinary reviewed path described
below; it does not require selecting this unaccepted preview mode.

To retain a preparatory preview without attempting ordinary source acceptance,
request unaccepted draft mode explicitly with a paired original PDF:

```bash
leanflow formalization inspect docs/paper.tex --companion-pdf docs/paper.pdf --json
leanflow workflow formalize docs/paper.tex --source-draft-only
```

This mode admits only structured source diagnostics with frozen byte spans and
known uncertainty codes. Missing included TeX files, unreadable PDFs and unsupported
project or dependency setup remain hard blockers. It prepares and checks dependencies,
runs one author turn, and retains bounded files from the assigned module tree plus
the author report as frozen preview artifacts. Original source files, candidate
scopes and blockers remain unchanged. The snapshot labels the preview unaccepted;
it adds no translation mappings, review receipt, publication or proof authority.
Even a complete author proposal cannot pass the gate in this mode. Resuming a
completed draft campaign exposes its retained result without another author call.
In VS Code, choose **Configure unaccepted source draft** after pairing and setup,
review its settings, and start the run separately. Inspect the resulting preview
files beside their source links.

Unresolved conditionals can contain alternative environment wrappers. The scanner
keeps those alternatives separate when proposing statement boundaries; repeated
unknown tests are independent, even if their text is identical. If a balanced
complete alternative exists, malformed alternatives remain structural uncertainty
with original byte evidence. A normal lexical-reading proposal may address it only
when all bounded violation witnesses are retained; truncation stays blocking. The
scanner retains up to 1,024 distinct witnesses without correlating repeated tests.
The absence of a balanced alternative, malformed
unconditional input, unsupported case conditionals, unmappable delimiters, and
structural resource limits remain hard blockers and produce no recovered blocks.
Candidate recovery does not choose an active branch or establish coverage. Nearby
proofs with uncertain branch attachment remain source context rather than establishing
that the statement is proved in the source.
Candidate histories share bounded persistent nodes, so long sequential environment
lists do not repeatedly copy every earlier candidate. Node, edge, frontier and
nesting limits still fail closed. When conditional path analysis is active,
plain-TeX theorem/proof macro candidates are not recovered anywhere in the document;
unclosed plain blocks still emit hard
diagnostics. A possibly skipped definition is structurally unsupported when
conditional controls escape its complete declaration, including default arguments,
parameter text or environment end-code. Self-contained balanced known conditional
controls alone do not discard the document's other candidates. Comment-sensitive
controls and unknown conditional names still require conservative independent review.

Normal translation can propose a source-bound lexical reading for eligible loader,
inherited-meaning, command, conditional and bounded structural diagnostics. The
controller supplies exact requirements for the frozen revision, source bundle,
diagnostic spans and complete item/region inventory. The author must describe every
eligible uncertainty with a substantive interpretation and reason. The original
diagnostics, candidate scopes, include occurrences and conditional alternatives
remain immutable evidence. Candidate diagnostics become pending independent review,
not user-verified facts. Fresh coverage and final coherence review must approve the
reading for the current stage; complete translation still requires the full source
denominator. No proposal can waive unknown effects, invent exclusions or claim that
TeX or package code was executed. Inaccessible evidence and mathematical ambiguities
still block acceptance.

Literal custom proof environments that wrap `proof` can supply provisional proof
candidates. Their complete declaration, argument count, original proof bytes and
any explicit label reference remain available to the reviewer. Effectful,
ambiguous or unsupported wrappers receive no automatic attachment, and a candidate
association alone does not establish that the source claim is proved.

Traditional `\bibliography{...}` intake also freezes a supplied root job-name
`.bbl`, which may accompany a source archive without its original `.bib` database.
The inventory distinguishes rendered bibliography evidence from reproducible BibTeX
inputs. Missing databases remain visible; ambiguous rendered bibliography files
block acceptance. A complete set of named `.bib` inputs also supplies source context
without a generated `.bbl`. An unrelated literal `.bbl` alone cannot substitute for
those databases or the root job-name rendering. Bibliography inputs have a fixed
4 MiB read limit. Oversized support is not read; its path, role, observed size and
limit remain diagnostic evidence. An oversized database is unavailable for rebuild,
while a unique valid root job-name `.bbl` can still supply rendered context. An
oversized rendered bibliography, or unavailable required support without that
rendering, remains blocking. Files growing during a bounded read still fail closed.
Valid UTF-8 bibliography contents remain available as complete frozen text for
source review; intake does not execute BibTeX or bibliography macros.
Case aliases stay visible as blockers. Explicit
`\addbibresource` paths still require their named resource.

A narrowly identified commented `\bibliography` candidate can instead receive a
lexical-reading proposal when the frozen same-file source supplies a complete
literal inline bibliography and an explicit frozen PDF companion is available.
The exact unescaped percent prefix and inline bibliography spans bind this
counterfactual missing-reference diagnostic. Fresh source and companion-image
reviews must assess that reading. It neither fabricates a supplied `.bib`/`.bbl`
nor permits active missing bibliography commands or missing figures to pass.

The selected candidate belongs to its author campaign until an explicit revision
selection or source repair changes that ownership. A new campaign cannot silently
adopt another campaign's candidate, including by naming it as a new base revision.
Resume the original run or explicitly select the intended source revision first.
Published and accepted staged revisions cannot be converted into source drafts.

Published acceptance requires a matching committed publication transaction for
that document's current pointer, handoff, revision and receipt. Case-only control
renames cannot turn retained authority into an ordinary project or authenticate
through a case-insensitive alias. An ordinary Lean library with a similar name
remains usable; a colliding publication destination leaves acceptance staged and
reports the collision before writing controls.

Draft retention uses a short local deadline after verified closure of the native
leader, its process group, observed descendants and bridge. A campaign budget or
storage stop still attempts this local save; an explicit user cancellation before
the save defers it until resume. A saved preview remains labelled as saved if
cancellation arrives afterward. A durably observed successful author turn is never
rerun on resume. If a crash leaves process
closure unknown, retain the private files and resolve that ownership before recovery.
Process-table sampling cannot certify a descendant that escaped and was reparented
before it was observed; the closure verdict covers the owned group and recorded identities.
Transient process-table errors are retried; five consecutive failed samples stop
the invocation, while final cleanup verification remains strict.
An invocation records possible live ownership before harness preflight and MCP
discovery. `NativeLaunchFailure` supplies explicit verified no-launch evidence
after its owned helpers have closed. Normal formalization clears possible live
ownership after verified native cleanup, bridge operation drainage and checker
closure, before independent Lean checks, source reviews and publication. Draft
capture still requires full bridge closure. `NativeCleanupUnverified` preserves
known observed usage without granting cleanup or acceptance authority; missing
usage remains unknown. Resume refuses to start another writer in the same private
project while ownership is uncertain;
the retained history and inputs remain available for inspection.
Failed checker shutdown preserves its registry entry and private files for a
verified retry, including when an output pipe remains open after the tracked
processes exit. Local stream closure never substitutes for physical EOF. A closing
bridge refuses new operations; only successful cleanup
clears the corresponding ownership warning.
Shutdown interrupts owned HTTP connections before joining handlers, so stalled or
slowly sending clients cannot keep a closing bridge alive. A page image counts as
delivered only after its complete authenticated response was written; interrupted
responses and text-only metadata reads do not satisfy the visual gate.
Each run loads its own immutable preview even if another run is currently selected;
recovery binds it to the frozen source blobs rather than the author's editable
source copy. This binding does not certify what the author actually read. Current
source attachment is checked and reported separately. A corrupt optional preview
produces a diagnostic without blocking original source inspection. Oversized author
reports retain a marked summary with the original size and hash rather than dropping
the Lean draft files.

## Inventory and source navigation

The provenance scanner records original UTF-8 byte spans, labels, complete statements
and proofs, include occurrences and macro context. Regions include prose outside
recognized theorem environments. Counts of detected statements are useful for
navigation; they are never the denominator for claiming complete source coverage.

Author and reviewer views group contiguous regions without changing the canonical
token census. Groups preserve file, include occurrence, mathematical membership,
exclusion and repair boundaries. Each normally contains at most 256 original members
and 16 KiB of source; a single larger original member remains whole and paginated.
Compact views expose coordinates, member counts and an exact membership hash, with
the full ordered partition in frozen audit evidence. Proposals may name every
canonical region or every exact group. Controller checks reject mixed, stale,
partial or duplicate coverage, then expand complete group dispositions and review
verdicts back to all original region IDs. No mathematical text or alternative path
is removed to make a batch fit.

Scanner policy v15 recognizes bounded decimal `\ifnum` comparisons
using literal parameter-free `\def` constants. It tracks local groups and direct
redefinitions, retains inactive branches at their original byte offsets, and keeps
numeric operands requiring recursive, parameterized, effectful or otherwise
unsupported expansion unresolved.
Required-argument rendering helpers receive only a bounded effect check of literal
brace arguments; they never become numeric constants. Optional/delimited arguments,
hidden scope changes and external reads refuse numeric selection. Assignments in
math and redefinitions of scanner controls remain unsupported effects requiring
source repair; a normal lexical-reading proposal cannot waive them.
Recognized file-loading commands outside the literal include/support resolver,
including `\@input`, `\@@input`, the import family, conditional file inputs and
PDF/standalone inclusion, remain hard I/O blockers. Saved macro and environment
replacement bodies are checked for recognized file or execution effects before
masking, including environment end bodies. A later class or package load cannot
hide those effects. Unknown `@` command names receive source-bound diagnostics
unless they have an explicit known or local definition; they are not silently
treated as builtins. The scanner never executes these commands to recover files.
It does not execute TeX or package code. Standard rendering commands and frozen
`\DeclareMathOperator` bodies provide lexical context, never coverage or translation
approval.
Every class or package loader revokes numeric and structural selection authority,
including standard distribution packages. Included files inherit unknown executable
meanings, and the caller retains that uncertainty after an include. The scanner
preserves all later candidates and reports blocking diagnostics instead of assuming
that conditional, environment or document-end commands retain their meanings.
Inactive branches are skipped token by token using only known conditional primitives;
an arbitrary command beginning with `if` does not create nesting. Generated environment
end hooks and theorem-number meanings invalidate earlier literal definitions. Comment
sensitive helpers and paragraph breaks in operands remain unsupported. A known
`\endinput` finishes the current physical line before excluding subsequent lines.
Any raw `^^` notation is refused before lexical masking because TeX preprocesses it
before tokenization; the scanner never partially decodes it. Percent text remains
visible after tokenizer authority is lost. `\includeonly` can exclude an occurrence
only from one direct, literal, unscoped directive whose execution is known before
that include. Unknown meanings, conditional or macro-generated directives, multiple
assignments and intervening unknown execution retain every literal include candidate.
These are conservative extraction rules, not verification of TeX execution or rendered
correspondence; independent source review still owns fidelity.

Fresh TeX authoring, publication and current source acceptance require the current
extraction policy, even when an older inventory recorded no diagnostics. Use the
revision-bound `retry_extraction` action to re-extract the unchanged original
sources explicitly. Saved source and review evidence remain available as history;
opening that history neither upgrades its scanner policy nor grants current
publication or prover authority.

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
does not waive missing files or approve a translation. Unknown conditional predicates
also support a checked user interpretation with exact source occurrences and a reason.
This action retains both branches, every original byte region and candidate scope;
it selects no branch and grants no coverage, translation or proof approval. Loader
and inherited-meaning diagnostics require their own exact proposed reading and
independent review; a user command interpretation does not silently resolve them.
Verification checkboxes reset when the selected revision changes. The host resolves
artifact IDs and verifies content before opening it; webviews do not
choose arbitrary host paths. PDF.js is packaged locally and needs no LaTeX extension.
PDF links identify physical pages and distinguish approximate from exact locations.
If coordinate extraction fails but complete page text is available, intake reports
the fallback and provides approximate page-only links without fabricated boxes.
PDF-only image or missing-text pages need a verified transcription or source repair;
acknowledging a warning cannot turn unknown content into complete coverage.
For a TeX companion, the same independent review also receives frozen physical page
images to check correspondence and additional mathematics. This does not bypass
an extraction failure or a required source-repair flag.

## Definitions, files and acceptance

The proposal records many-to-many translation units: source IDs, exact Lean names,
representation notes and dependencies. This supports split statements, merged setup,
supporting lemmas and direct library reuse. Every included source item is mapped;
every selected region has an explicit disposition and review verdict.

Explicitly identified source clauses are mandatory checklist entries, without a
claim that every clause was automatically found. Each affected unit records whether
the clause is represented, a named pending proof, omitted, specialized or unresolved,
with exact mapped declaration names and a rationale. Independent clause verdicts
must assess the original frozen spans and compiled meaning. Omitted efficiency
requirements, narrower domains and unresolved trailing qualifiers block the full
item even when disclosed in a representation note. A pending proof must be an
explicit proposition with complete data and types, not a missing computation.

### Documented source corrections

Human revision is optional. A prepared run continues to a complete best-effort
project when source ambiguities remain after review or the author cannot select a
reading. It uses the same campaign budget and deadline, completes all included
items and preserves the complete original denominator, including trusted exclusions.
Provisional readings and unresolved clauses remain explicit. Independent compilation
checks actual mapped declarations and the whole project; a fresh advisory review
then assesses the full source, continuing through flagged items. If review cannot
finish within the budget, its unreviewed scope remains visible.

The editor's **Open draft project** action opens the concrete retained Lean project.
A separate unaccepted draft blueprint and item links join saved source IDs to the
exact frozen Lean files and compiled locations. They leave canonical mappings,
source review and proof status unchanged; absent or inconsistent navigation
evidence remains a visible gap. The blueprint is collapsed and paged.
Frozen TeX/PDF and Lean previews do not wait for publication or document mutation
locks during verification. Their selected revision, allowlist and immutable bytes
are checked independently. Certified proof files and opening a retained project
folder retain their existing authority and ownership guards.
Frozen source, proposal, coverage census, check report and review findings remain
inspectable. **Completed with ambiguities** means the full saved project passed
technical checks; it grants no source acceptance or Prove handoff. Missing files,
uncompiled mappings, cancellation or uncertain process closure remain distinct
incomplete/operational outcomes. Correcting an optional interpretation later
requires checking the changed project again; previous evidence stays archived.

The original TeX/PDF and its item, clause and region denominator remain unchanged.
The editor offers a whole-document bounded correction policy, or a scoped reading
and reason for a specific item. These trusted decisions create versioned authority
records for the exact source bundle and scope. The model can propose a correction;
it cannot create permission or approve its own proposal. Revoking or changing a
decision requires fresh review; selecting old evidence must not restore an older
permission.

Each proposed correction links the original frozen passage and literal reading to
the adopted reading, exact Lean declarations, source context, rationale and any
applicable user decision. A corrected clause uses `represented_via_correction`
and its exact correction ID instead of a generic representation note. Every affected
unit retains that clause. Source encoding repairs restore the paper's actual meaning;
they do not need to prove equivalence to an incorrect earlier Lean encoding.

A mathematical repair or substantive interpretation also supplies a closed local
expected proposition and a complete theorem of exactly that named proposition.
The independently replayed environment must show no extra hypotheses, pending
proof dependencies, new axioms, unsafe/native evidence or circular justification.
This initial evidence role accepts closed facts; general repairs requiring a
quantified local warrant remain blocked rather than weakening the check. Local
evidence can establish an arithmetic discrepancy or feasible counterexample. It
does not itself identify the intended replacement or prove the corrected theorem.

The existing statement reviewer sees the original and adopted readings, checked
facts, competing alternatives and exact authority references together. That same
review must return an explicit correction verdict before the affected clause can
pass. The document coherence review checks interactions through shared definitions.
Missing source support, competing plausible repairs or a substantive unapproved
choice blocks acceptance with the alternatives recorded. A refuted literal claim
cannot be handed to a prover as an ordinary pending theorem.

Publication, the blueprint and the prover handoff retain these records and bind
them to the exact checked declarations, environment and current authority. The
editor separates saved verdicts from current acceptance and separates both from
proof completion. The adopted main theorem may still have a named `sorry`; the
local justification for a correction may not. Preserved unaccepted drafts include
the proposed correction JSON for inspection, with no permission or acceptance
inferred from model-authored status fields.

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

Generated Lean source has a narrower option policy: positive numeric
`maxHeartbeats` and `maxRecDepth`, plus literal `set_option autoImplicit false`,
including its scoped `in` form. Disabling auto-implicit variables requires explicit
binders; it does not relax kernel inspection. Other source option names or values,
including `autoImplicit true`, remain unsupported. Changing this admission policy
invalidates earlier acceptance receipts until the current checker reviews them again.

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
verdict. Large projects use batches of at most eight units and forty exact source
groups,
then a final review checks the complete project and prior verdicts together. Small
projects combine region coverage with that final review. Reviewers can retrieve
exact region text and unchanged Lean definitions from the frozen project.
The reviewer can block a dummy definition even if Lean accepts it.
The reviewer receives the frozen controller scope for the current representative
slice or complete stage, including preparation identity, selected source/region
IDs and the full source inventory. Remaining slice items stay planned for later
expansion; they still require full review before publication. Author-written scope
notes cannot change that authority. Reviewers assess shared source context and
block ambiguities that affect the selected statements or representations.
Large dossiers are paginated without dropping trailing text or other evidence.
Text tool responses are bounded to 16,000 UTF-8 bytes including the complete transport
envelope; reviewers can request smaller pages. Batches contain their declaration
summaries, with complete compiled bodies available separately. Inaccessible
evidence is an operational failure and cannot
be cached as a semantic rejection.
For an explicitly paired TeX/PDF, the controller renders every physical companion
page into immutable evidence bound to the exact PDF hash, renderer and flags, page
index and PNG hash. PNGs are limited to 4 MiB and 2,200 pixels per dimension; image
responses use a separate 6 MiB transport envelope. Every assigned companion page
must actually be retrieved as an image before a positive coverage batch counts.
Text metadata, an unread image catalog or an incomplete response cannot substitute
for delivery. Cached passes must retain the exact delivered image bindings. Final
coherence assesses the same source, images and prior coverage; it is not a separate
visual review campaign. Image delivery does not approve correspondence by itself,
and the PDF-only transcription gate remains unchanged.
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
source blobs, immutable receipt history, prior review decisions, delivered companion
image blobs and metadata, `Blueprint.md` and `handoff.json`. Private native logs and
disposable caches remain under `.leanflow/`;
removing private caches does not
remove the published source links.

Publication journals and retained file versions belong to the project under
`.leanflow/formalization-publication-transactions/`; author-run folders contain
only pointers, so cleaning an old run preserves recovery evidence. Keep these
transaction directories when removing disposable caches. If historical evidence
is unavailable, publication recovery can acknowledge its exact observed loss
while keeping current workspace files. This permits fresh work and revokes the
affected acceptance; missing versions and write plans are never reconstructed.

Dependency acquisition preserves existing immutable pins and the reverse priority
of direct Lake requirements. Supported dependency roots use clean, committed,
complete flattened manifests; newly inherited descendants must be named in those
root locks. Incomplete or stale inherited manifests block setup with a repair reason.
An explicit root override owns its independently checked pin and descendant closure.
An existing requirement without a manifest must supply its original exact Git URL,
full 40-character commit and conventional root checkout. Symbolic references require
a restored immutable manifest or an explicit full pin before preparation.

Publication control files must be regular files of at most 8 MiB. Reads reject
nonregular files without waiting on them. Automatic authority-loss recovery supports
at most 16,384 owned entries, 262,144 directory probes and 32 MiB total control bytes. Unrelated
native runs do not consume the owned-entry budget. Above a limit, recovery remains
unsupported and no truncated acknowledgment can grant authority. Retain the complete
history and restore a complete consistent registry backup, or retain a full project
archive and begin fresh source acceptance in a new project. Do not delete retained
history to make the gate pass. Healthy registry checks scale with the known index;
these recovery bounds do not cap ordinary project size.

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

Acceptance includes the checker implementation fingerprint. Upgrading to changed
checker code requires rechecking the translation before proving or resuming a
formalization-backed prover run. Historical receipts and source review remain
available; an upgrade does not silently transfer their authority to new code.

Before checking exact theorem types, the controller privately compiles the required
local imports from their checked static Lake membership. This also supports custom
source directories. It publishes only derived cache artifacts after checking source
and configuration identity, retaining replaced versions and rejecting concurrent
cache changes. Private prover snapshots repeat this setup as accepted proofs change
imported modules. These compilation results establish import readiness; independent
type and proof checks still decide proof acceptance.
Cache updates publish all compiled parts as a guarded batch and retire obsolete Lake
trace/hash sidecars. A failed update restores matching prior versions and preserves
competing versions. The durable refresh warning blocks further checks until every
admitted local module has been rebuilt from current checked inputs.
Private compilation inputs and prior cache generations remain retained indefinitely;
the ordinary compiler runner provides no positive descendant-closure receipt for
automatic reclamation. Storage-reserve admission runs before another scoped import
build; reaching the reserve stops the run. Remove an obsolete private generation
manually only after its owned processes have verifiably stopped and no pending
transaction, review or resume needs it. Preserve competing versions and recovery
records until their exact conflict or loss has been resolved. Never manually delete
`needs-refresh.json`, `barriers/`, `refresh-*` records or batch outcome/progress
records: they preserve the admission and recovery boundary. Manual reclamation
applies only to obsolete private compilation input trees, after the checks above.
Scoped native snapshots exclude the evaluated project build directory, including
custom paths. The controller durably records exclusion provenance before publishing
the snapshot. An interruption before publication preserves the unused record and
permits a fresh snapshot; an interruption after publication can resume the same
snapshot. Older snapshots without matching provenance remain preserved and cannot
be used for private checks. If a job encounters unverifiable provenance, resume
retires that job and allocates a fresh workspace while preserving the old files and
accounted provider usage. Other view-layout and pinned dependency errors can still
require restoring the recorded environment before resuming.

Document and publication lock callers that supply a cancellation check or deadline
poll ownership nonblockingly, checking before and after acquisition. A stopped or
expired waiter can exit while an unrelated holder continues working. Legacy callers
without these options still block until ownership is available; some startup and
recovery paths use that behavior. Long holders use their own operation deadlines
and cancellation checks. This does not guarantee immediate Stop at every startup
boundary.

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
An equation theorem generated while importing a definition must bind to that exact
freshly inspected parent definition, including its kernel body, type and source
provenance. Imported pattern-match helpers additionally bind to the exact compiled
matcher and its source definition through an actual kernel-body dependency. These
parent profiles provide provenance without adding authored mappings or proof slots.
A user-authored auxiliary receives no compiler-generated exemption.

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

### Workspace and publication authority

Editor operations carry a host-issued workspace identity and generation. The host
freezes the owning root across pickers, paged reads, preparation, recovery and prover
launch; a workspace switch stops further continuations and suppresses stale replies.
Request identities also prevent older replies from replacing a newer selection.
Document and revision identities alone do not identify a workspace.

A missing publication registry with surviving journals, publication pointers or
proof evidence is authority loss, not a new project. Inspect the explicit recovery
observation and acknowledge the loss while preserving current files and available
history. The recovery index grants no selected acceptance. A fresh independently
accepted publication is required before proving, including for a portable project
imported into another workspace. Restoring old source bytes cannot restore approval.

Preparation independently rechecks the authorized pins, requirements, configuration,
layout and clean checkout inputs after effectful work and at commitment. Hashing a
build's resulting environment does not authorize changes made by that build.

Ordinary prover installation and rollback retain the actual displaced source inode
in a durable bounded transaction. Unexpected editor versions and late writes through
open descriptors remain preserved; a conflict prevents successful integration or
restart from silently overwriting them. Retained inodes require explicit ownership
and reconciliation and are not ordinary disposable caches.
