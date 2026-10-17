# Changelog

User-visible changes to the LeanFlow VS Code extension. Entries before 0.1.20
include features and settings from the former direct-provider runtime.

## Unreleased

- Open frozen source and Lean previews during independent verification without
  waiting on publication locks; retain proof and project ownership checks.
- Navigate unaccepted drafts through separate item declaration links and a
  collapsed, paged blueprint, with explicit mapping gaps and acceptance status.
- Bring opened PDFs into view with keyboard focus; preserve focus during saved
  status refreshes and item navigation.
- Open delivered full-paper draft projects from one **Open draft project** button
  in the owning VS Code host. Keep unresolved review notes visible, human revision
  optional for delivery, and acceptance/prover handoff separate.
- Compare original and adopted correction readings with frozen source/PDF and
  Lean links; collapse local evidence, user decisions, saved review and affected
  units while keeping source acceptance separate from theorem proofs.
- Record a versioned bounded correction policy for the included paper, or a
  specific interpretation/convention with a reading and reason. Keep choices
  disabled during source drift, active work or a pending save; saved verdicts
  confer current acceptance only after the existing whole-revision gates pass.
- Explain known native check failures in activity rows and their details while
  preserving compact receipts and keeping raw tool payloads private.
- Keep Formalization panels unique across source navigation and refreshed views.
- With the updated backend, stop Formalization during dependency planning and setup
  through the same verified run ownership used during translation.
- Show dependency-planning activity in Live and label retained mathematical results
  as belonging to the previous run until preparation finishes.
- Collapse advanced formalization sections, page large inventories and retain
  visible totals, blockers and one suggested next action.
- Acknowledge committed repairs before refreshing; separate historical source
  navigation from expensive exact currentness checks and keep navigation responsive.
- Review paired PDF pages through frozen, source-bound image evidence while
  preserving independent coverage, source fidelity and proof gates.
- Bound native model processes and their observed child builds to 16 GiB by
  default, with an explicit option up to 20 GiB in the updated backend.
- With the updated backend, recover statement candidates across conditional TeX
  layout wrappers while retaining structural uncertainty and acceptance gates.
- Preserve supplied job-name bibliography evidence and distinguish missing
  databases from ambiguous rendered bibliography support.

## 0.1.46

- Point active formalization runs with no recorded events to their terminal for
  setup and dependency-planning output, without inferring the current phase.

## 0.1.45

- Open verified frozen TeX, Lean and Markdown artifacts as named read-only
  documents, with source-line navigation and bounded viewer storage.
- Fit PDF pages to a resized dashboard pane, keeping rendered text and selection
  on the same scale.

## 0.1.44

- Make manual project setup the first step in Formalization, before paid native
  dependency planning. Source inspection stays available before setup.
- Bind setup progress, cancellation, retries and logs to the selected workspace;
  preserve that selection when an older workspace finishes setup.
- Reuse current formalization preparation authority without requiring a stale
  general setup checkpoint, while blocking launches during active setup.


## 0.1.43

- Name the source-only sidebar entry “Open Dashboard”; open its Formalization tab
  without starting project setup or translation.

- Preserve native model identifiers with version tags and context suffixes during
  formalization launch and resume.
- Resume formalization with the original provider, model and source-draft intent;
  report missing legacy identity instead of silently switching providers.
- Clear source-draft mode for accepted revisions and require a bound source
  document before any editor formalization launch.
- Hide retired human-review launch controls while preserving historical records.

## 0.1.42

- Separate the primary formalization configuration action from an optional source
  draft disclosure; explain that draft previews keep acceptance and proving blocked.

## 0.1.41

- Give Prove and Formalization separate tabs and persisted configurations; retire
  other workflow choices without reinterpreting old saved intents.
- Configure and start source translation beside its inventory, project setup,
  PDF, review gates and frozen previews. Resume only exact stopped editor-tracked
  campaigns; keep untracked CLI history explicit.

## 0.1.40

- Bind formalization requests, asynchronous results and prover launch to the owning
  workspace generation; discard stale replies after project or request changes.
- Display publication registry loss as an explicit recovery blocker.
- Label mathematical preparation estimates as unreviewed and reject inconsistent
  empty prover-launch requests.
- Record checked conditional interpretations against frozen source occurrences
  without excluding branches or approving translation and proof gates.
- Inspect frozen unaccepted source drafts beside paired PDF and TeX evidence;
  keep publication and proving blocked while source execution is uncertain.
- Keep the selected project and active preparation when opening external
  toolchain or dependency references through editor navigation.


## Unreleased

- Open the dashboard from a source-only sidebar and inspect TeX/PDF sources before
  a Lean project exists; prepare pinned dependencies and project layout with
  progress and cancellation before starting translation.
- Require reviewed representative translations before complete document expansion.
- Repair verified PDF transcriptions and shared/imported mappings without losing history.
- Inspect preserved publication conflicts and keep explicitly reconciled files with
  a recorded resolution; acknowledge unavailable history without reconstructing
  missing evidence, and require new review after acceptance is revoked.
- Let preparation cancellation finish safe cleanup and preserve the controller's
  final readiness receipt before refreshing the selected document.
- Show stale compiler/dependency acceptance separately from historical proof results.

- Add a backend-owned formalization inventory with explicit source-region
  accounting, revision-bound repairs, and separate review, elaboration, and proof states.
- Inspect TeX roots, project folders, and PDFs before model launch. Open verified
  frozen source and Lean references, and navigate PDF pages and text with packaged PDF.js.
- Select a TeX file and companion PDF together, with visibly approximate or
  unavailable page links and canonical statement-boundary repairs kept in TeX.
- Hand the exact accepted revision and named obligations to the existing prover;
  stale evidence and empty proof scopes cannot launch unrelated work.
- Show source claim status and explicit proof-attempt permission separately. Open
  claims require a reasoned opt-in, and permission can be revoked before re-review.
- Resolve unknown TeX commands with an explicit interpretation checked against
  every frozen occurrence; display checked proof certificate revisions separately.

- Obtain the reviewed LeanProbe source automatically during installation; build
  Lemex only when requested or selected, and repair MCP backends in fresh environments.
- Harden WSL2 selection, unattended installation, and existing checkout preservation.

- Retain foreground Lean proof workers across independent file checks while
  preserving low-memory reclamation and compiled-import invalidation.
- Allow audited projects to opt into reuse of controller-built local imports
  across fresh private proof workspaces.
- Use LeanProbe directly for declaration parsing and response transport.

## 0.1.39

- Follow live prover and orchestrator messages, public reasoning, and tool output.
- Keep streamed updates together and expanded details open as text arrives.
- Show current tools, elapsed time, readable arguments/results, and a Reasoning filter.
- Distinguish reported reasoning summaries from traces and preserve access to full records.

## 0.1.38

- Default new proof runs to 250 total agent calls, 200 prover calls, 50
  orchestration calls, and 24 hours of active time.
- Preserve explicit unlimited global limits when clearing the launch controls.
- Keep project setup cancellation reliable on macOS when a subprocess exits
  during Stop.

## 0.1.34

- Keep saved provider settings separate for each Remote SSH host, including
  hosts that expose workspace files through local file URIs.

## 0.1.33

- Recognize host-local file paths in Remote SSH workspaces so project discovery
  and one-click setup work on the remote machine.

## 0.1.32

- Record native model and reasoning settings for every agent call, including
  resumed calls, with timestamps and evidence sources in Model history.
- Keep requested overrides separate from runtime-reported settings and defaults;
  missing historical values remain unreported.

## 0.1.31

- Explain effective native model defaults in live agent settings without filling
  missing history from current provider configuration.

## 0.1.30

- Retry temporary CLI check failures while retaining settings and job navigation.
- Keep contextual help visible inside the viewport and support dismissal.

## 0.1.29

- Share compatible Lean dependency caches through private writable copies.
- Inspect and reclaim unused managed storage while protecting active runs,
  proofs, checkpoints, and logs.

## 0.1.28

- Use tmux for persistent jobs when available, with an integrated-terminal fallback.
- Unify live and historical job navigation, terminal attachment, and log selection.

## 0.1.27

- Explain native agent-call accounting and add accessible contextual help.

## 0.1.26

- Reuse unchanged project readiness and show setup logs inline.
- Release finished agent dependency copies while retaining proof work and resume state.

## 0.1.25

- Add one-click Lean project setup with verified readiness, progress, cancellation,
  repair, and logs.

## 0.1.24

- Default new proof runs to Research and bottom-up scheduling with role call caps.

## 0.1.23

- Restore research controls and expose native prover/orchestrator call allowances.

## 0.1.22

- Support clean-room native proving and global calls, tokens, and active-time limits.

## 0.1.21

- Read native model and reasoning choices and clarify launch failures.

## 0.1.20

- Add native Codex, Claude Code, and Lemex setup and emitted agent activity.
- Improve provider layout and retained recovery status.

## 0.1.19

- Clarify context and compression settings for the former provider runtime.

## 0.1.18

- Label cost sources and coverage; show actual benchmark runtime and stop reasons.
- Freeze stale elapsed counters and explain retained prover allocations.

## 0.1.17

- Shorten generated theorem names in graph and tree labels while retaining full names.

## 0.1.16

- Distinguish pending resume, verification, and integration from running provers.
- Remove accepted draft-plan duplication while retaining review outcomes.

## 0.1.15

- Add a benchmark campaign view with cell outcomes, metrics, artifacts, and pause control.

## 0.1.14

- Align proof states across graph, jobs, counts, proposals, and source changes.
- Expose controller checks, budget scopes, profile compatibility, and external-run discovery.

## 0.1.13

- Group log rows by agent session and present readable event and tool summaries.

## 0.1.12

- Expand events into recorded model output, tool calls, results, and verification evidence.

## 0.1.11

- Add an interactive dependency graph with search, zoom, keyboard navigation, and live states.

## 0.1.10

- Add the exact-run prover workspace, plan, jobs, source diffs, guidance, and usage.

## 0.1.9

- Explain rejected launcher fields and incomplete usage.
- Exclude internal review prompts from extension packages.

## 0.1.8

- Discover an unambiguous nested project and follow externally launched analytics.

## 0.1.7

- Reconcile live analytics by exact run identity instead of historical row selection.

## 0.1.6

- Upgrade legacy tracked-run override records during activation.

## 0.1.5

- Validate webview actions, profile paths, process identity, and experiment isolation.
- Bind experiment scoring to final evidence and improve first-run guidance and accessibility.

## 0.1.4

- Add complete run metrics, per-declaration outcomes, provenance, and comparison statistics.
- Reattach active sweep cells after reload without restarting them.

## 0.1.3

- Restrict environment overrides and validate launches, paths, and run identity.
- Preserve frozen experiment inputs and explicit incomplete metrics.

## 0.1.2

- Improve theme contrast and the activity-bar icon.

## 0.1.0

- Initial launcher, live state, logs, flag profiles, and experiment sweeps.
