# Changelog

All notable changes to the LeanFlow VS Code extension are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

- Add an exact-run prover workspace with navigable theorem dependencies,
  conditional-proof status, a readable proof plan, job budgets, and usage metrics.
- Open recorded source lines, per-agent scratch files and logs, and file diffs
  against the run baseline; filter structured events by agent.
- Queue guidance for the selected orchestrator or prover without editing their
  durable plan directly. Finished runs reject new guidance.
- Expose standard/research mode and focused model, context, compression,
  parallelism, API-call, restart, and plan-refinement controls from the CLI catalog.
- Show terminal failure details, resumed-run lineage, nested research jobs, and
  candidate proofs separately from verified results. Label incomplete cost totals.
- Preserve full redacted proof plans and reject guidance for finished jobs.
  Retire the unsupported prover human-review launch option, including saved presets.

## [0.1.9] — 2026-08-11

### Fixed
- Explain a rejected request instead of dropping it silently. A launcher field
  over its length cap previously made the button appear dead; the host now
  reports which field failed, and rejection text names the field only, never the
  value that failed.
- Keep the launcher inside the limits the host enforces, so an over-long value
  is a visible form problem rather than a dropped message. Target, model,
  axioms, prompt, knob values, and profile fields stop at their cap while
  typing; the additional-skill list and the knob override map, whose limits are
  per entry and per count, are reported instead. Preview no longer sends a
  request that can only be refused, which previously left the plan panel
  resolving forever.
- Refuse to call a run's cost complete when a provider's cumulative session
  total decreases. Session totals replace rather than accumulate, so a reset
  would have silently dropped the earlier spend; token and API-call totals are
  summed per turn and stay exact.

### Packaging
- Keep internal review and verification prompts out of the `.vsix`. They are
  excluded by pattern rather than by filename, so a newly named one cannot ship
  local absolute paths to the Marketplace the way the previous list allowed.

### Documentation
- State what run evidence proves: hash self-consistency detects accidental
  edits, truncation, and wrong-run attribution, but is not a signature against
  a party who can write to the workflow-state directory. Drift is compared
  between the sealed launch and final snapshots.

## [0.1.8] — 2026-08-11

### Fixed
- Discover a single nested LeanFlow project when VS Code is opened at its
  parent folder, matching the extension activation contract. Ambiguous parent
  folders still fail closed instead of choosing a project silently; archived
  project copies are not treated as active candidates.
- Keep verified CLI-run analytics live without requiring that the workflow was
  launched by the current editor window. Polling stops with the process and now
  defaults to a quieter five-second cadence.

## [0.1.7] — 2026-08-11

### Fixed
- Restore live-run analytics for workflows launched by the CLI or a previous
  extension host. Live status now carries its durable run id, and legacy active
  snapshots are bound only through an exact process-token match.
- Do not let a stopped tracked row suppress the active project's phase, target,
  sorry counts, declaration queue, diagnostics, or other live status fields.

## [0.1.6] — 2026-08-11

### Fixed
- Upgrade pre-0.1.5 tracked-run override records during activation. A legacy
  empty or flat override map previously threw before the Control provider was
  registered, leaving the sidebar blank with a permanent loading indicator.

## [0.1.5] — 2026-08-11

### Security
- Validate the complete webview command surface at the extension-host boundary,
  cap experiment axes and cells, and require a native confirmation before a
  sweep can launch more than ten potentially billable cells.
- Harden profile create, replace, open, and delete against symbolic-link and
  realpath escapes. Non-editable and sensitive terminal-only knobs can no
  longer be persisted or injected into a child process; manual targets and
  path-like skills must resolve inside the selected project.
- Persist run ownership before detached spawn, reserve launch identity against
  races, and stop restored runs through the CLI's verified process-identity
  check instead of trusting a persisted raw pid.
- Keep prompts and credentials out of ordinary extension state, webview
  snapshots, run history, notifications, logs, and exports. Resumable sweep
  prompts live in VS Code SecretStorage and are verified against a persisted
  content digest before every cell.

### Research
- Bind metrics to a verified run stream and separately digested final snapshot.
  Launch and final provenance include source/project guidance, runtime and
  Python package identities, selected skills, behavior configuration, toolchain,
  and dependency revisions; partial historical evidence remains unscored.
- Run every experiment cell in a private detached clone of one clean, frozen Git
  baseline. Profiles and launch settings are frozen at sweep creation, cell
  order is randomized, and clone cleanup is limited to app-owned storage.
- Resolve explicit provider/model aliases and endpoint identity before the first
  paid cell, require complete source-run history after reload, and reject
  source/runtime/config/skill drift. Unmetered provider paths and dispatched
  child runs fail closed instead of understating cost or API usage.
- Retain per-declaration outcomes and use Student-t confidence intervals plus
  Welch comparisons without treating one sample as zero variance.

### Fixed
- Reconcile a restored run only from exact run-id evidence, including immutable
  terminal records, with an explicit grace period for a persisted pre-spawn
  record. Unrelated live status can no longer finish or adopt it.
- Scope log viewing, event polling, scoring, and project roots to the selected
  run. Terminal experiment cells remain immutable instead of being rerun on
  resume.
- Bound retained event streams in both the extension host and webview, resetting
  the visible tail after host eviction. Opening a recorded history row now reads
  that row's own run log and project root instead of the newest selection.
- Add first-run CLI guidance, actionable command errors, accessible progress and
  notifications, high-contrast styling, keyboard focus states, and honest
  failed/blocked/unscored progress segments.

### Marketplace
- Declare Workspace Trust and virtual-workspace limitations, document privacy,
  isolation, storage, and first-run behavior, and ship Marketplace metadata for
  the 0.1.5 package.

## [0.1.4] — 2026-08-11

### Added
- Score experiment cells from `leanflow runs metrics`, a new CLI aggregate over
  the complete recorded stream. Counts are totals rather than whatever the UI
  had buffered, and a cell that cannot be scored says so instead of guessing.
- Record per-declaration outcomes (proved / blocked / parked / split, with
  attempt counts) and a failure taxonomy, so a cell is an analysable row rather
  than a single sorry count.
- Capture run provenance at scoring time: git commit and dirty state, Lean
  toolchain, every dependency revision, LeanFlow version, model and provider.
- Summarize repeats per condition with mean, sample standard deviation, and a
  95% interval. A single repeat reports no interval rather than a spurious zero.
- "Attention" log preset for the events worth noticing during a long run:
  rejection, rollback, owner conflict, provider retry, timeout, stall.
- Additional-skills control on the Launch form, which the launch contract
  already supported but the UI never exposed.

### Fixed
- Re-attach to a sweep cell that was mid-flight when the window reloaded,
  instead of resetting it to pending and paying for the run a second time.

## [0.1.3] — 2026-08-11

### Security
- Reject launch knobs that are not in the declared `LEANFLOW_*` catalog. Arbitrary
  names such as `PYTHONPATH` or `LD_PRELOAD` previously reached the child process
  environment, which could execute workspace content in the extension host's
  security context.
- Validate every message arriving from the webview at runtime instead of trusting
  the TypeScript types, which do not exist at runtime.
- Reject profile names and sweep names that could escape their directory. The
  profile delete path could previously remove an arbitrary file, and sweep export
  could truncate one.
- Refuse a workflow target beginning with `-`, which the CLI would otherwise parse
  as an option and silently change what the run does.

### Fixed
- Mint the run id before spawning and pass it as `LEANFLOW_WORKFLOW_RUN_ID`. Run
  identity was previously inferred by matching workflow kind and a second-precision
  timestamp, which could attribute logs and experiment metrics to the wrong run.
- Score an experiment cell after the settle delay rather than before it, so a
  cell cannot record the previous run's numbers.
- Export a sweep using the configuration recorded at launch, not the profile's
  current contents, which changes when a profile is edited after a run.
- Report `0` tool calls as zero rather than as missing.
- Flag counts derived from a truncated event buffer as incomplete.
- Reconcile a run's status only against a snapshot that describes that run, and
  treat `exited`, `stopped`, and `interrupted` as terminal phases.
- Stop a run restored after a window reload by confirming its recorded pid, and
  say so plainly when it cannot be confirmed instead of showing a stop that did
  nothing.
- Refuse a manual launch while a sweep is running: a project permits one live
  workflow owner.
- Apply an explicitly cleared knob as an unset rather than an omission, so an
  ambient value cannot survive it.

## [0.1.2] — 2026-08-11
- Lift surfaces off the page background so the dashboard is legible on dark
  themes whose editor and widget backgrounds are the same color.
- Simplify the icon to a turnstile closing on a QED square, which survives the
  flat 24px mask VS Code paints activity-bar icons with.

## [0.1.0] — 2026-08-11
- Initial release: launcher with resolved-plan preview, live run state, filtered
  activity log, knob catalog with profiles and diffing, and knob-ablation sweeps.
