# LeanFlow for VS Code

Launch and inspect LeanFlow workflows without leaving the editor. The extension
uses the `leanflow` CLI, so the editor and terminal share project setup, launch
settings, proof verification, and recorded run evidence.

## Inspect and formalize a paper

Open the source folder and choose **Formalization** in the LeanFlow sidebar. This is available before creating a Lean project. Start with **1. Project setup → Set up project** to prepare Lean, Mathlib and the REPL in that tab's selected workspace. Existing projects keep their configuration and toolchain. Setup is a separate, reusable tools checkpoint and does not call a model or approve the paper's statements.

You can inspect sources before setup finishes. Choose a TeX
file, TeX project folder, or PDF inside the active workspace. Inspection freezes the source inventory without
launching a model. Review included items, explicit exclusions, and unresolved
source regions before using **Configure formalization** to select the native
model and budget in the same Formalization tab. Configuring does not launch work;
review the settings and choose **Start formalization** after project tools are ready. Native planning inspects that baseline and can add missing pinned dependencies through the checked private preparation transaction. A current formalization preparation receipt also satisfies the tools prerequisite when an older general setup checkpoint has become stale.

To inspect a TeX source with a rendered companion, select both the `.tex` and
`.pdf` files together. The PDF is frozen explicitly; text matching provides
approximate page candidates or a visible missing match. Pairing does not certify
that the two files have identical content, and PDF pages remain part of coverage review.

Source and draft text links open named, read-only frozen documents beside the
dashboard, preserving the selected source line. They do not open editable Untitled
buffers or the canonical project file. The PDF rerenders to fit the available
pane width; its text selection layer uses the same page scale. Opening a PDF
reveals and focuses its viewer; refreshing saved status preserves your focus.

The inventory displays source review, Lean elaboration, and proof state separately.
The overview shows the next action, while setup, issues, preparation, revisions,
draft artifacts and handoff details can be collapsed. Item and region tables are
paged, with full totals and search. Saved status opens quickly as **historical**;
**Check currentness** checks the current environment separately without blocking
source navigation. Repairs display a saved-revision acknowledgment before refreshing.
If that refresh fails, the UI keeps the saved acknowledgment and asks for a refresh
without resubmitting the edit.

A completed draft with unresolved review notes shows **Delivered draft project**
and one **Open draft project** button. This opens the controller-verified Lean
project folder in another window of the installed VS Code, retaining the owning
Remote SSH/WSL host when applicable. Source/PDF links remain frozen previews.
Full source totals, explicit exclusions, coverage gaps and review notes stay
visible. Saved compilation checks are distinguished from unverified incomplete
projects; revising notes is optional for draft
delivery. This draft has no translation acceptance or accepted prover handoff,
and theorem proofs may remain `sorry`.

Select an item to compare **Original reading**, **Adopted reading**, and its
**Lean statement** under **Source corrections and interpretations**. Frozen
source excerpts and PDF/TeX links preserve the original wording. Lean links show
the saved compiled type and its type, body and definition-context identities.
Local evidence, scoped user decisions, independent review, and affected units
are collapsed separately. A saved **PASS** remains saved evidence until the
existing whole-revision currentness and acceptance gates succeed; theorem proof
completion remains a separate state. Encoding repairs are labelled separately
from mathematical source corrections.

Under **Optional correction choices**, **Enable bounded correction policy for this
paper** records permission once for every currently included item in the exact
frozen source bundle. Mathematical repairs seeking acceptance need closed local
evidence and fresh independent source review. Encoding repairs preserve the
source meaning and do not require a local mathematical proof. For a nonunique
reading, expand **Record a specific reading or convention**, enter the reading
and reason, and choose **Record interpretation** or **Record convention**. These
actions create versioned user decisions; they do not approve a statement or prove the paper. The same controls
allow revocation. These decisions are optional for draft delivery; unresolved
readings can stay explicitly flagged for later revision. Choices are disabled while the source is stale, setup or a
workflow is active, or another change is waiting to save. A missing exact source
binding offers no decision actions; an oversized paper policy scope is never
silently shortened.

Open or unclassified claims also show **Proof attempt permission**. Select the item,
enter a reason, and choose **Allow proof attempt for this open claim** to opt in;
the same control lets you revoke permission. Each change creates a new working
revision for review. Source claim status and proof completion remain independent.
Use **Repair inventory or mapping** to add a missed item, correct its source range
or Lean declaration mapping, or change scope with a reason. Region dispositions
remain auditable; neither an exclusion nor an extraction warning counts as an
independently reviewed mathematical statement. PDF-only sources also support
manual full-page transcription, which still needs item accounting and review.

Unknown TeX commands list every frozen occurrence under **Extraction diagnostics**.
Open those source links, record the command's meaning and supporting evidence, and
confirm each rendered occurrence before submitting an interpretation. This creates
a new revision for coverage and translation review. Missing includes and other
source errors require repairing the source and inspecting it again.

Unresolved conditional predicates support the same checked interpretation form.
Both original branches and all candidate items remain visible; recording an
interpretation selects no branch or proof permission. Loader and inherited-source
uncertainty retain separate gates. The checkbox resets when the revision changes.

Source links open hash-checked frozen text beside the dashboard. Selecting lines
in that source or Lean view shows candidate source items back in the inventory.
The packaged PDF.js viewer works without LaTeX Workshop or an external website:
select an item to open its physical page, or select PDF text to find linked items.
PDF-only sources can prefill a source-boundary repair; paired sources keep their
statement boundaries in TeX. Approximate, absent, and stale mappings stay
visible. The editor limits a single artifact preview to 64 MiB and reports larger
files explicitly. Source access and the CLI stay on the owning Remote SSH/WSL host.

**Start proving accepted statements** shows and hands over only the exact named
obligations of the selected accepted revision. The backend revalidates the
revision, environment, and files before launching the existing prover. A handoff
with no pending obligations launches no model. Refresh the inventory after
external changes; stale snapshots disable repair and proving actions.
Completed proofs show the controller's checked proof certificate revision
separately from source review.
If checking cannot run, the editor reports the operational error and offers a
retry through workflow configuration; a rejected Lean draft instead needs correction.
If local work and a newer published revision disagree, choose the intended
revision explicitly before repairing or launching another workflow.

Formalization supports the pinned compiler, standard library and fully pinned
Mathlib dependencies through the [compiled-library adapter](../docs/formalization.md#supported-input-boundary).
Source fidelity, dependency availability and checked proofs remain separate gates.

## Install and prepare a project

Install [LeanFlow](../README.md#install) on the workspace host, then install the
extension. To build a local package:

```bash
cd vscode-extension
npm install
npm run package
```

Install the generated `.vsix` using **Extensions: Install from VSIX**. Reload
VS Code to activate an extension update after any active workflow has finished.
Set `leanflow.cliPath` if the CLI is not on the extension host's `PATH`.

Open a folder and choose **Set up project** in the LeanFlow sidebar. Mathlib is
the default. Setup creates an empty-folder project, prepares existing Lean files,
or checks an existing Lake project while preserving its source and versions.
Multi-root workspaces use a folder picker; Remote SSH runs setup on the remote
workspace host. A trusted workspace, Git, and a real filesystem are required.

Readiness requires a real Lean verification-protocol check. **Check setup**
reuses unchanged readiness; **Repair dependencies** explicitly rebuilds when
needed. Progress, **Show setup log**, **Retry**, and **Cancel** remain available.
Setup cannot change a project with an active workflow. Existing `sorry` holes
are valid starting points.

## Configure a native backend

Open **Providers** or use **LeanFlow: Configure Provider**:

- **Codex:** use its native login or an OpenAI API key.
- **Claude Code:** use its native login or an Anthropic API key.
- **Lemex:** configure its key and endpoint. The LeanFlow installer can supply a
  managed pinned Lemex distribution with installer `--with-lemex` (or when
  Lemex is already selected in saved configuration).

The native runtime owns authentication, home, model requests, sampling, retries,
and compression. **Open native config** opens its configuration.
**Refresh setup** checks executable, login presence, and model metadata without
an inference request. Presence does not validate paid API access.

Explicit keys use VS Code password prompts and SecretStorage, scoped to the local
or remote extension host. New extension-launched runs receive the native
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LEMEX_API_KEY`, and `LEMEX_BASE_URL`
variables. Saved overrides take precedence over inherited values; removing them
restores the native environment. Existing runs retain their launch settings.

Model and reasoning menus use the selected runtime's reported capabilities.
Prover and orchestrator can select separate backends and models. Leave overrides
empty to preserve native defaults. **Refresh models** reloads metadata after
native configuration changes. Unknown metadata stays visibly unknown.

## Prove and Formalization

**Prove** and **Formalization** are separate workflow tabs. Each keeps its own
source or Lean target, backend, model, prompt, limits and knob profile. **Prove**
repairs existing Lean proofs; **Preview plan** resolves its command without
starting work. **Formalization** selects TeX/PDF evidence, estimates and prepares
required dependencies, translates statements, and exposes the source and Lean
review gates before an accepted handoff can start proving.

In **Formalization**, inspect a source and choose **Configure formalization**.
The model and limits appear beside its inventory, PDF, setup and review evidence.
Complete the manual **Project setup** step before native planning. Starting the run then checks and expands required project dependencies before translation; setup
readiness alone does not approve coverage or mathematical fidelity. The scoped setup card keeps its progress, Retry, Cancel and log bound to the selected workspace. Switching workspaces cannot reuse another folder's ready status, retry target or log, and a late completion does not change the selected folder. A stopped
run launched by this extension can resume from the same source configuration.
The host checks its recorded document, source, workspace and exact runtime ID.
Resume restores that run's trusted original provider, resolved model and draft
intent, even if the current form has changed. Runs discovered only from CLI history,
or legacy runs without sufficient trusted launch identity, must resume through the
CLI with that ID; the editor does not substitute current settings.

Saved configurations for retired workflows require explicitly selecting Prove or
Formalization. Their old target and prompt are not reused as a new proof run.

New proof runs default to **Research**, **bottom-up**, and two concurrent prover
jobs. Research adds planning and review around a theorem dependency graph.
**Standard** uses one prover directly. Target selects the file containing proof
holes; an empty target scans eligible project source files.

The **Global limits** card defaults to 250 agent calls and 24 hours of active time;
reported tokens are unlimited by default.
Time is entered in minutes. Empty limits display as **∞**; zero means exhausted.
Research also defaults to 200 prover calls and 50 orchestrator calls across the run.
Explicitly clear a role limit for unlimited. Profile values and launch overrides
remain distinguishable, with help beside unfamiliar controls.

One **agent call** is one `codex exec`, `lemex exec`, or `claude -p` process. It may
make many model requests, replies, and tool steps. Internal retries and compaction
share that call; a new process consumes another. Token limits gate future calls
and may be exceeded by an active invocation. Missing usage stays unknown.

**Clean room** restricts proof research to the project and installed libraries,
blocking external research and remote Lean search while retaining model access.
Clean-room checkpoints can resume; online or legacy native context cannot be
reused. See the [prover guide](../docs/prover-workflow.md).

Foreground checks retain warm LeanProbe workers by default. Low-memory and
background-worker modes reclaim them. Local-import compilation reuse is an
opt-in project contract described in the [storage guide](../docs/storage.md).
LeanProbe owns the parser and transport; LeanFlow independently accepts proofs.

## Follow and control runs

**Live jobs**, **Past jobs**, the Runs tree, and the shared Jobs menu select the
same exact run for details and logs. Live workflows started from a terminal are
also discoverable. Temporary CLI failures retain saved settings and job
navigation while the extension retries; persistent installation problems provide
an actionable path and update guidance.

When available, each new job uses tmux on the workspace host. It can continue
through closing VS Code or disconnecting; **Open terminal** reattaches without
starting another job. Closing that terminal detaches. Without tmux, a regular
integrated terminal is used and must remain open. Host shutdown still ends work.

Every live job exposes **Stop** in the dashboard, sidebar, and Runs tree,
including jobs started from a terminal. Stopping goes through
`leanflow runs stop RUN_ID`, which revalidates the exact run's process identity
before signalling. Incomplete history is marked unknown.
With the updated backend, Formalization publishes this verified ownership during
dependency planning and preparation too; Stop does not have to wait for translation.
Live reports preparation activity while dependencies are inspected. Until preparation
finishes, retained mathematical results and blockers are labelled as belonging to
the previous run.

The proof workspace shows the theorem graph or tree, statements, dependencies,
plan, jobs, usage, controller checks, stop reasons, and recorded source changes.
Only independently accepted proofs count as solved. Candidates awaiting checks
and proposed graph changes remain visibly provisional. Source links and diffs
open the selected run's artifacts. **Send guidance** queues a message for the
controller or a selected job at its next decision boundary.

**Agents and jobs** opens live activity for running jobs. It shows messages,
current tools, elapsed time, plans, and reported reasoning. **Follow live** keeps
new activity in view. Streamed text updates in place, and expanding a row shows
readable arguments and results with the recorded JSON available below.
Native workflow tool events retain compact receipts instead of raw arguments and
results. Known check failures show a short explanation, such as source changes
during a check; unknown failures retain the operation ID for diagnosis. These
summaries describe an attempted check and do not grant Lean acceptance.

Use **Show reported reasoning** or the Logs view’s **Reasoning** filter to follow
public summaries and traces. Availability depends on the native runtime and its
settings. Missing or encrypted reasoning is not reconstructed. Activity counts
are separate from agent-call limits and independent Lean acceptance.

**Agents and models** and each job's **Model history** show model and effort
settings recorded for each call, including resumes. Requested values, native
session evidence, response model names, and **Runtime model default** are
labelled separately. Absent values remain **Not reported**. Older history is not
filled from today's runtime configuration.

## Profiles and experiments

**Knobs** lists the installed CLI's declared `LEANFLOW_*` settings, defaults,
and descriptions. Save a profile or compare effective profile values. Project
profiles live in `.leanflow/flag-profiles/`; user profiles live in
`~/.leanflow/flag-profiles/`. Terminal-only or incompatible settings are explained
and can be omitted from an editor-compatible copy.

**Sweeps** compare targets, profiles, models, and repeats in a persisted randomized
order. Every cell uses a private detached clone of one clean Git baseline with
its own dependency/build tree. The source checkout is retained. Cells run
sequentially, with frozen launch inputs and profiles. Budget initial build time
and disk space for those private copies.

Scoring requires exact-run terminal evidence, a complete activity stream, and
matching launch/final non-source inputs. Missing, partial, or mismatched evidence
makes a cell **unscored** and excludes it from aggregates. Missing tokens and cost
stay unavailable. Exports include recorded provenance and configuration; inspect
them before sharing. Integrity digests detect corruption and wrong-run evidence,
not malicious rewriting by someone with access to the state directory.

## Storage and privacy

Providers shows managed storage, active protections, and free space.
**Clean unused storage** removes owned, unused caches and obsolete runtimes.
Defaults are a 20 GiB soft target and 10 GiB free-space reserve. Proofs, results,
checkpoints, and logs are retained. See [storage and cleanup](../docs/storage.md).

The extension has no telemetry or independent background network requests.
Workflows contact the selected native model service. Credentials are excluded
from webview state, profiles, ordinary run records, and exports. Half-filled
prompts are not persisted. Resumable sweep prompts use SecretStorage; a missing
or mismatched secret blocks pending cells.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `leanflow.cliPath` | `leanflow` | CLI executable |
| `leanflow.projectRoot` | empty | Explicit project selection |
| `leanflow.pollIntervalMs` | 5000 | Active status/event polling |
| `leanflow.idlePollIntervalMs` | 20000 | Idle check for externally started work |
| `leanflow.cellSettleMs` | 3000 | Wait for final evidence before scoring |
| `leanflow.eventBufferSize` | 4000 | Retained activity events per run |

## Development

```bash
npm install
npm run watch
npm run typecheck
npm test
npm run package
```

Press F5 in VS Code for an Extension Development Host. CLI contracts and host
message validation have tests in the main repository and extension test suite.
See [CONTRIBUTING.md](../CONTRIBUTING.md) for the repository quality gate.

Keep `@types/vscode` aligned with the minimum version in `engines.vscode`.
Major React and TypeScript upgrades require a separate migration and must pass
the full button audit and extension packaging checks.

Formalization can inspect a TeX/PDF source in an empty workspace. First use **Set up project** at the top of the tab for the baseline Lean and REPL check; source extraction diagnostics do not disable this action. The subsequent **Prepare dependencies and project** action checks planned imports, pinned dependencies and Lake
layout with cancellable progress. Native planning can expand this setup when translation needs additional dependencies. The view shows
representative-slice review separately from complete translation and proof status.
Cancellation waits for the controller's safe stopping point and then refreshes
actual readiness, including when the preparation committed before cancellation.
Verified transcription replacement reopens affected PDF boundaries and mappings;
**Retry extraction** preserves original source bytes and prior revisions. Recovery
cards expose preserved publication and setup versions even when normal launches
are blocked. Missing historical evidence can be acknowledged against the exact
current observation; affected mathematical acceptance still needs a new review.
A stale checker or dependency environment requires a new formalization
review before proving.

Formalization requests are bound to a host-issued workspace generation and a request
identity. Switching projects discards stale inventories and stops subsequent steps
of an older picker, repair, preparation or prover-launch operation. Identical
document/revision IDs in copied projects do not allow actions to cross workspaces.
Publication-registry loss is shown in recovery and requires explicit acknowledgment
and fresh mathematical acceptance. Preparation estimates are labeled unreviewed;
checked infrastructure does not establish source fidelity.
Opening an external toolchain or dependency reference retains the selected project
and its active preparation. Selecting another project's editor or an explicit
source TeX/PDF file still changes the owner.

After explicitly pairing a TeX source with its PDF and preparing prerequisites,
**Configure unaccepted source draft** sets the launch option for one author turn.
This secondary action configures the form; **Create unaccepted source draft**
starts the run separately.
Frozen preview files and the author report appear beside source navigation, with
source uncertainty still blocking publication and proving. The option stays in
the Formalization configuration and never applies to Prove. It does not mark items reviewed or mapped;
resuming the completed campaign makes no new author call.

The backend preserves alternative conditional layout wrappers when recovering
candidate statements. Ambiguous nesting remains visible and blocks acceptance;
the explicit paired-PDF draft option grants no source approval. Supplied root
job-name `.bbl` files are frozen as bibliography evidence, with missing database
and ambiguous support diagnostics kept separate.
