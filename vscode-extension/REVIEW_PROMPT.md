# Code review request — LeanFlow VS Code extension

You are reviewing a VS Code extension that we intend to publish on the
Visual Studio Marketplace. It is new code, written in one pass, and has never
been reviewed. Assume nothing in it is correct because it looks deliberate.

Your job is to find what is wrong, what is unsafe, and what will embarrass us
in public — not to praise what works.

---

## 1. What the extension is

LeanFlow is a Lean 4 proof-automation tool: it drives a language model inside a
real Lean project to repair proofs and formalize mathematics, re-verifying with
Lean after every edit. It ships as a Python CLI (`leanflow`).

This extension is an editor front end over that CLI. It does **not** reimplement
any LeanFlow logic. It:

- shells out to `leanflow` for everything it knows,
- spawns proof workflows as **detached background processes**,
- reads run state the CLI records under `<project>/.leanflow/workflow-state/`,
- renders five views in a webview: Launch, Live, Logs, Knobs, Sweeps.

The two features that motivated it:

- **Knobs** — LeanFlow's behavior is controlled by ~250 `LEANFLOW_*` environment
  variables. The extension browses a declared catalog of them, saves named
  "profiles" (sets of knob values), and diffs profiles.
- **Sweeps** — ablation experiments: run targets × knob-profiles × models,
  score each cell, compare. This is used to produce results for an academic
  paper, so **reproducibility of a run's exact configuration is a correctness
  requirement, not a nicety.**

## 2. Layout and sizes

~6,100 lines. Extension host is Node/CJS; the webview is React bundled to an
IIFE. Both built by `esbuild.mjs`.

```
src/                          # extension host (has `vscode`, Node APIs)
  extension.ts          151   activation, commands, status bar
  core/
    cli.ts              259   locate + invoke `leanflow`; spawn detached runs
    launch.ts           145   form -> argv + child environment  ← reproducibility seam
    project.ts          126   discover the LeanFlow project; list Lean files
    runManager.ts       425   run lifecycle, polling, event buffering, persistence
    experiments.ts      359   sweep expansion, sequential execution, scoring, export
    services.ts         134   app-wide state, refresh
    types.ts            366   shared types + the webview message protocol
  panels/
    webviewHost.ts      393   message handling, CSP/HTML, profile file I/O
    dashboardPanel.ts    54   editor-column webview
    controlView.ts       30   sidebar webview
  views/runsTree.ts     162   tree view of runs
webview/                      # React UI (NO `vscode`, NO Node APIs)
  store.tsx             240   reducer + host message handling
  App.tsx               152   shell, nav
  views/*.tsx          1480   Launch, Live, Logs, Knobs, Sweeps, Sidebar
  components.tsx        195   shared presentational pieces
  icons.tsx             135   inline SVG icons
  styles.css            819   VS Code theme-token styling
  vscodeApi.ts           58   postMessage + persisted view state
test/launch.test.mjs    175   argv/env construction tests (17 cases)
```

Build/verify:

```bash
npm install
npm run typecheck     # tsc --noEmit, currently clean
npm test              # 17 tests, currently green
npm run build -- --production
npm run package       # -> .vsix
```

## 3. Review dimensions

Weight them in this order. Be concrete: cite `file:line`, state the concrete
input or sequence that triggers the problem, and say what the user observes.

### 3.1 Security — highest priority, this ships to strangers

- **Webview → host trust boundary.** `webviewHost.ts` handles messages from the
  webview. Messages are typed in TypeScript but **not validated at runtime**.
  Assume a compromised or buggy webview can send any payload of any shape.
  Walk every `case` in the dispatcher and ask what a hostile payload achieves.
- **Path traversal.** Profile names and paths from the webview reach the
  filesystem. We already found and fixed traversal in `saveProfile`,
  `deleteProfile` (which calls `fs.rm` — arbitrary file deletion), and
  `openPath`. **Verify those fixes are actually sufficient** (see
  `safeProfileName` and `resolveInside` in `webviewHost.ts`) and find any path
  we missed, including in `experiments.ts` `export()`.
- **Command and argument injection.** `cli.ts` uses `execFile`/`spawn` with an
  argv array, never a shell. Confirm there is no path where a shell is
  introduced, and check whether user-controlled strings that begin with `-`
  can be interpreted as flags by `leanflow` (argument injection, not shell
  injection). The `--prompt` field is free text; `--model`, `--axioms`,
  `--additional-skill` are user strings.
- **Secret handling.** Runs need provider API keys. The launch *preview*
  redacts credential-shaped values (`launch_plan_payload` on the Python side),
  but the child process inherits `process.env`. Check that no key is ever
  written to: webview state, `workspaceState` persistence, notifications, the
  tree view tooltip, or an exported experiment JSONL. `TrackedRun.appliedOverrides`
  is persisted — can it ever hold a secret?
- **CSP.** `buildWebviewHtml` sets a strict CSP with a per-render nonce. Verify
  it is actually strict (no `unsafe-inline` script, no remote origins) and that
  `localResourceRoots` is correctly scoped. Note `style-src` allows
  `unsafe-inline` — judge whether that is justified and whether it can be removed.
- **Process control.** Stopping a run does `process.kill(-pid, "SIGINT")` on a
  detached process group. Assess: PID reuse (the recorded pid may belong to an
  unrelated process after a reboot or a window reload), signalling a group we
  do not own, and what happens on Windows where process groups differ.
- **Marketplace surface.** Any telemetry? Any network call? There should be
  none — confirm.

### 3.2 Functionality and correctness

- **`runManager.adoptRunIds`** is the piece I trust least. The extension cannot
  know the runtime's run id at spawn time, so it claims the newest unclaimed
  activity stream whose workflow kind matches and whose `started_at` is at or
  after the launch time — comparing **ISO strings**, one of which is sliced to
  19 chars. Attack this: two runs of the same kind started within the same
  second, a run started from a terminal outside the editor, clock skew, a
  timezone difference between the runtime's timestamps and `Date.toISOString()`.
  What happens when the wrong stream is adopted? (Logs and, worse, sweep
  metrics get attributed to the wrong cell.)
- **Sweeps are deliberately sequential.** A LeanFlow project permits only one
  live workflow owner — a second concurrent run is rejected with
  `WorkflowLiveStatusOwnerConflictError` — and `live_status.json` is per project,
  so a parallel cell would fail to start *and* clobber the metrics of the cell
  being scored. Verify the implementation actually enforces this, including
  across a window reload and when a user launches a manual run from the Launch
  tab while a sweep is mid-flight. **That last case looks unguarded to me.**
- **`experiments.scoreCell`** reads the *current* `live_status.json` after a
  cell finishes. Confirm this cannot capture the wrong run's numbers, and judge
  whether `cellSettleMs` (default 3s) is a real fix or a race dressed up as one.
- **Run lifecycle across reload.** Runs are detached and survive a window
  reload; their exit callbacks do not. `restore()` downgrades and
  `reconcileFromStatus` reconciles against recorded status. Find the states
  where a run is shown as running forever, or shown as finished while alive.
- **Event streaming.** `pullEvents` polls with an `event_id` cursor; an unknown
  cursor falls back to the tail. Check for duplicated or dropped events at the
  webview, and whether `eventBufferSize` trimming can desync the cursor.
- **`launch.ts`** is the reproducibility seam. Verify the argv matches what the
  Python CLI parses (there is a matching contract test at
  `tests/leanflow/test_extension_launch_contract.py` in the parent repo), and
  that `resolveLaunchEnv` layering (profile, then per-launch overrides, with
  `""` meaning "unset") cannot silently produce a configuration different from
  what the UI displayed.

### 3.3 Utility — does this actually help its user?

The user is a researcher running long, expensive proof campaigns and ablations.

- Is anything essential missing from the Launch form relative to the CLI surface?
- Live/Logs: with ~50 event types at very different densities, do the filter
  presets in `LogsView.tsx` surface what matters during a multi-hour run?
- Sweeps: are the recorded metrics enough to write a paper table, or is
  something obviously missing (variance across repeats, per-declaration outcome,
  failure taxonomy)?
- Is any part of the UI actively misleading — a stale value shown as live, a
  status that does not distinguish "working" from "hung"? Note that the Live
  view already flags stale snapshots and quiet heartbeats; judge whether that
  is sufficient.
- What would you cut? Unused surface is a maintenance cost.

### 3.4 Style, structure, maintainability

- React: unnecessary re-renders, missing `key`s, effect dependency bugs, state
  that should be derived. `store.tsx` replaces the whole app snapshot on every
  host push — is that a performance problem at 4,000 buffered events?
- Are the host/webview boundaries clean? The webview imports **types** from
  `src/core/types.ts` — confirm no runtime host code leaks into the bundle.
- Error handling: are failures surfaced to the user or swallowed? There are
  several `.catch(() => ...)` fallbacks in `runManager.ts` — judge each.
- Accessibility: keyboard navigation, focus management, ARIA on the custom
  nav and the pill-as-button patterns, contrast in both themes.
- Theming: everything uses VS Code CSS variables. Elevation uses neutral
  translucent overlays rather than theme background tokens (because several
  dark themes give the editor and its widgets the same value). Check the result
  holds up on high-contrast themes.
- Dead code, over-abstraction, comments that explain *what* instead of *why*.

### 3.5 Marketplace publication readiness

- `package.json`: `publisher`, `license`, `repository`, `categories`,
  `keywords`, `engines.vscode`, activation events. Are activation events too
  broad? (`onLanguage:lean4` plus `workspaceContains:**/.leanflow/project.yaml`.)
- Is there a `LICENSE` file in the packaged extension? (Apache-2.0 is declared;
  I believe the file itself is **missing** — confirm.)
- Missing: `icon` (marketplace gallery icon, 128×128 PNG), `CHANGELOG.md`,
  `badges`, `galleryBanner`. What else does the Marketplace expect?
- `.vscodeignore` correctness — does the `.vsix` contain anything it should not?
- Does the extension degrade gracefully with **no** `leanflow` CLI installed?
  That is the state most Marketplace visitors will be in on first launch.
  Is the first-run experience comprehensible to someone who has never used
  LeanFlow?
- Bundle size, activation time, and whether any work happens at activation that
  should be lazy.

## 4. Known context — do not report these as findings

- **Sweeps run one cell at a time by design.** See 3.2. Do report if the code
  fails to *enforce* it.
- **Runs are detached deliberately**, so they survive a window reload and a
  sweep keeps going. Do report lifecycle bugs this causes.
- **The `.vsix` version is bumped manually** to defeat VS Code's per-version
  asset cache during development.
- Saved profiles land in `<project>/.leanflow/flag-profiles/`, which is
  git-ignored in the parent repo. Known and accepted for now.

## 5. Output

Produce findings ordered by severity. For each:

```
[SEVERITY] file:line — one-line claim
  Trigger:  the concrete input or sequence
  Impact:   what the user experiences or what an attacker gains
  Fix:      the specific change, with a diff if it is small
```

Use `CRITICAL` (security hole or data loss), `HIGH` (wrong results, or a bug
most users will hit), `MEDIUM` (real but bounded), `LOW` (style,
maintainability). Findings that only affect ablation *correctness* are at least
HIGH — wrong numbers in a paper are worse than a crash.

Then, separately and briefly:

1. **Ship / do not ship** for the Marketplace, with the blocking items listed.
2. The three changes with the best value-to-effort ratio.
3. Anything in section 3.3 (utility) you think is missing that a researcher
   running multi-hour proof campaigns would actually want.

Prefer a small number of well-evidenced findings over a long list of
speculation. If you are unsure whether something is a real defect, say so
explicitly rather than asserting it — but do not stay silent about a suspicion
in the security section.
