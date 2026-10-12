# Changelog

User-visible changes to the LeanFlow VS Code extension. Entries before 0.1.20
include features and settings from the former direct-provider runtime.

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
