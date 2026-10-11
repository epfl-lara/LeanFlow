# LeanFlow for VS Code

Launch, observe, and ablate LeanFlow proof workflows without leaving the editor.

The extension does not reimplement any part of LeanFlow. It drives the `leanflow`
CLI and reads run-scoped state through `leanflow runs`, so the editor, shell,
evaluation harness, and exported results use the same evidence boundary.

## Provider setup

Open **Providers** in the dashboard, use the setup link beside **Agent backend**,
or run **LeanFlow: Configure Provider** from the command palette.

- **Codex:** sign in through `codex login`, or add a standard OpenAI API key.
- **Claude Code:** sign in through `claude auth login`, or add an Anthropic API key.
- **Lemex:** add a key and, if needed, an endpoint such as
  `https://inference.rcp.epfl.ch/v1`. The LeanFlow installer supplies a managed
  Lemex runtime when needed.

Keys are entered through VS Code's password prompt and stored in SecretStorage,
scoped to the local or remote extension host. They never pass through the webview,
workspace settings, profiles, command arguments or saved run records. At launch,
the extension supplies `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LEMEX_API_KEY` and
`LEMEX_BASE_URL` to the native runtimes using their standard environment names.
Planner and prover roles may use different backends, so all explicitly saved
backend keys are available to the workflow process.

Saved values override inherited environment values for **new extension-launched
runs**. Remove a saved key, or clear the Lemex endpoint, to restore the inherited
setup. Existing runs and independent terminal sessions keep their current settings.
Native auth files, native homes, model defaults and sampling settings remain owned
by each CLI. **Open native config** opens that CLI's configuration in the editor.

**Refresh setup** checks executable discovery/version, native login status, and
the runtime's model catalog without making an inference request. A saved or exported key is shown as present,
not as validated API access. Complete any login in its terminal and refresh again.
For Remote SSH, install and configure the native runtimes on the displayed remote
host. This page requires the matching LeanFlow CLI with `provider --json --check`;
an older CLI shows an update message.

**Model** shows the configured default by name and offers the models reported by
the selected runtime. Prover and orchestrator can choose separate backends and
models; their reasoning menus contain only the levels reported for that model.
Switching a backend or model clears incompatible role overrides. Native defaults
stay native: no sampling or compression settings are injected. **Refresh models**
reloads the catalog after native configuration or account changes. When a runtime
cannot report its catalog or reasoning capabilities, the UI explains what is
unknown instead of inventing choices. Listing a model does not verify paid access.

**Clean room** is available for prove with Codex, Claude Code, and Lemex. It blocks
external research and restricts tools to the project and installed libraries;
the runtime can still connect to its model. Clean-room checkpoints can resume;
existing online or legacy sessions cannot be reused. This does not remove knowledge learned during
model training. To prove a file, select the file containing the `sorry` goals,
rather than an import-only module, or leave Target empty to scan the project.

## What it gives you

**Launch** — a form over the whole `leanflow workflow` surface: workflow kind,
target, provider, model, swarm width, research mode and worker count, clean
room, allowed axioms, extra skills, and a free-text prompt. The
rendered command is always visible, and **Preview plan** resolves the launch
without starting it, showing the exact argv and the complete `LEANFLOW_*`
environment the child process would receive, with credentials redacted and
prompt/command-template contents represented by fingerprints. An
actual run seals its launch configuration and source identity separately from
its final source and outcome, so later edits cannot rewrite what it claims.

**Live → Agents and jobs** includes each job's latest output and an expandable
feed of messages, native tool calls/results, plans and reported reasoning. Codex
and Lemex public reasoning items and Claude's emitted thinking blocks are shown
when available; missing or encrypted thinking is never reconstructed. Messages
appear when the native CLI emits completed blocks, not necessarily token by token.
The feed shows the latest 30 matching events from the bounded run buffer. Expand
an entry to read its full recorded text; **Open agent log** opens the filtered run
log. Older runs retain their existing final reports and cannot gain missing traces.
Native tool claims remain separate from independent Lean acceptance.

**Live** — phase, target declaration, active file, sorry counts here and across
the project, provider and model, skill, agent capacity, held locks, the
declaration queue, the current blocker, Lean diagnostics, and goals. A stale
snapshot (the owning process died) and a quiet heartbeat are called out rather
than shown as if fresh.

**Logs** — the structured activity stream rather than scraped console text. The
runtime emits around fifty event types at very different densities, so the
filter is the point: presets for narrative, tool calls, Lean, provider, and
research, plus per-type selection with counts.

Rows are grouped into the agent sessions they belong to, each under one
collapsible header naming its role, theorem, calls against budget, duration and
outcome. A row from a parallel job that lands between two sessions stays a plain
row rather than acquiring a header of its own. Event types are labelled in
words, each provider turn is numbered against the job's call budget, and a tool
call is described by its subject rather than its payload, so every row is one
scannable line.

Selecting a row expands it into the full model output behind it: the
assistant's text and reasoning summary, each requested tool call with its
arguments, tool results, verification verdicts, and the final job report. It
renders as readable text rather than JSON — model prose keeps its headings,
lists, and fenced code; tool arguments and results become labelled fields; Lean,
Python, and shell payloads become code blocks — with the raw JSON one click
away. A finished job leads with its outcome, then its final response, then the
files it left behind named by the part that differs. That output is read on
demand from the job's own transcript through
`leanflow runs event RUN_ID EVENT_ID`, so the polled stream stays compact; rows
from runs recorded before shared event ids existed are matched by kind and time
and say so.

The structurally raw run log, including the closing token and cost summary, is
one toggle away after prompt and credential fields have been scrubbed. Long runs
retain a bounded tail in both the extension host and webview; eviction resets
the displayed tail instead of duplicating or growing it without limit.

**Knobs** — every declared `LEANFLOW_*` knob, grouped by subsystem, with its
type, default, and a description of what flipping it does. Knobs worth flipping
in an ablation are marked. Build a working set, save it as a named profile, and
diff two profiles to see exactly which knobs differ — comparing effective
values, so a knob absent from one profile is compared against its real default
rather than treated as unset. Settings that redirect storage, expose raw
requests, disable redaction, or bypass approval remain visibly terminal-only;
the extension cannot save or launch them.

**Sweeps** — targets × profiles × models × repeats, expanded into a randomized,
recorded run order and scored into a comparison table. Each row includes the
proof outcome, declarations, failure taxonomy, duration, API calls, tokens,
cost, complete launch/final provenance, and its exact frozen knob profile.
Missing or unverifiable final evidence is marked **unscored** and excluded from
means rather than becoming a zero or a failed proof. Repeats report measured
and missing sample counts, Student-t mean intervals, and direct Welch intervals
for condition contrasts. Export writes one JSON object per cell in the shape
`evals/harness.append_result` uses. A sweep requires an explicit provider and
model; before any paid cell, the extension resolves provider aliases, model,
reasoning effort, and a credential-free endpoint identity and freezes them as
the condition actually being tested.

### Sweeps isolate every cell

Sequential cells must not share a mutable checkout: a proof written by an early
condition would otherwise become a hidden input to every later one. Before the
first cell, LeanFlow therefore requires a clean Git checkout, records its commit
and tree, freezes the selected profiles, and gives every cell a detached local
clone with a private dependency/build tree. The source checkout is read-only;
the extension never resets, cleans, or removes it. Deleting a sweep removes only
that sweep's app-owned clones after its process has stopped.

Cells still run one at a time so CPU, memory, provider throttling, and cache
pressure do not become an uncontrolled parallelism variable. The order is
randomized once from a persisted seed. The extension blocks overlapping runs it
owns and rechecks the source project's durable owner before each cell. On
scoring or reload it also audits the complete hot and retained run history for
the source checkout; damaged, truncated, or nonterminal history fails closed
instead of being interpreted as no overlap. Other programs and unrelated load
on the machine remain outside its control, so published experiments should
still run on an otherwise quiet host.

Isolation fails closed if the source is dirty or untracked, is not in Git, uses
submodules or tracked symlinks, or routes project storage outside the clone.
Private clones deliberately do not share `.lake` trees; budget disk and initial
build time accordingly. Additional sweep skills must be tracked,
project-relative `SKILL.md` paths; mutable user-level resolver names are
rejected. The selected active skill and its linked files are content-bound per
target.

### What a scored cell proves, and what it does not

A scored cell is bound to one verified run: an exact run id, a complete stream
whose recomputed event count and SHA-256 match the sealed snapshot, verified
terminal exit evidence, and non-source inputs that are identical in the sealed
launch and final snapshots — runtime, Python packages, selected skills, behavior
configuration, project configuration, toolchain, and dependency revisions. The
Lean source is deliberately *not* held fixed: proving rewrites it, so the change
is measured and reported rather than forbidden. Anything missing, truncated,
mismatched, or nonterminal is reported **unscored** rather than estimated, and
unscored rows never enter a mean, solve rate, or contrast.

Those checks are unkeyed SHA-256 self-consistency, not signatures. They detect
accidental edits, truncation, crashed or archived runs, and evidence belonging
to a different run — the failure modes that actually corrupt a comparison. They
are **not** tamper-proof against someone who can write to
`<project>/.leanflow/workflow-state`, because such a party can rewrite a payload
and its digest together. Run evidence is trustworthy exactly as far as
filesystem access to the state directory is.

Drift in those non-source inputs is compared between the sealed launch and final
snapshots only. One modified during a run and restored to its original bytes
before the run ends is therefore not reported as changed.

## Requirements

- `leanflow` on your `PATH` (or set `leanflow.cliPath`)
- A registered LeanFlow project: `leanflow project init` inside your Lean repo
- Git and a local filesystem (virtual workspaces are not supported)
- A trusted VS Code workspace, because LeanFlow executes the project's Lean toolchain

If the CLI is missing, the sidebar and Launch view show the failing path and
offer direct actions for the install guide, CLI setting, and a fresh check.
Install LeanFlow, set the path if necessary, then reload the window.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `leanflow.cliPath` | `leanflow` | Path to the executable. |
| `leanflow.projectRoot` | *(empty)* | Pin the project instead of discovering it. |
| `leanflow.pollIntervalMs` | `5000` | How often live status and events are re-read while a run is active. |
| `leanflow.idlePollIntervalMs` | `20000` | How often an idle dashboard checks the live-status file for a run started elsewhere; the CLI is read only when the file changed. |
| `leanflow.cellSettleMs` | `3000` | Wait for a sealed final metrics snapshot before scoring. |
| `leanflow.eventBufferSize` | `4000` | Activity events kept in memory per run. |

## How runs are executed

Runs are spawned detached, not in an integrated terminal. They survive a window
reload, a sweep can keep going while you work, and their output is already
recorded as structured state that the log viewer reads. On reload the extension
re-adopts tracked runs and reconciles them against recorded status, because the
process outlives the exit callback that would otherwise report it finished.

A run started or resumed from a terminal is noticed while the dashboard is
idle. The extension watches the project's `live_status.json` and, on a bounded
interval, compares that file's size and modification time; only a change makes
it read state through `leanflow runs status`. The file is a change signal, never
parsed by the editor. A discovered run is observed in the Live tab; stopping it
remains with the terminal that owns it.

Stopping a run started by the current window interrupts its owned process group.
After a reload, the extension asks `leanflow runs stop RUN_ID` instead; the CLI
signals only after revalidating the run id, process token, and process-group
identity. A stale or reused pid is refused and the UI reports why.

## Privacy and network behavior

The extension has no telemetry and makes no background network request of its
own. LeanFlow workflows can contact the model provider selected by the user.
Provider credentials are inherited by the child process but are not placed in
webview state, tracked-run persistence, notifications, or experiment exports.
Half-filled launcher prompts are intentionally not persisted. A resumable sweep
stores its frozen prompt in VS Code SecretStorage and keeps only a SHA-256
content identity in ordinary workspace state; a missing or mismatched secret
blocks all pending cells. Prompt-bearing commands are omitted from exports.
Exports still contain configuration, source remotes, content fingerprints, and
local provenance paths, so inspect an export before sharing it publicly.

## Where profiles live

The agent backend selector supports **Codex**, **Claude Code**, and **Lemex**.
Each CLI keeps its own login, API configuration, model defaults, sampling, and
context compression. Leave the model and effort overrides empty to preserve
those defaults. Lemex inherits `LEMEX_BASE_URL` and `LEMEX_API_KEY` unchanged.

The always-visible **Global limits** launch card covers agent calls, reported
tokens, and active campaign time across resumes. Enter time in minutes; LeanFlow
converts it to seconds. Empty means unlimited; zero means exhausted. Clearing a
profile limit makes this launch unlimited; **Use profile limit** restores it.
One CLI invocation, including a retry or resume, counts as one call.
Token limits stop subsequent calls; an active invocation may overshoot and
missing usage remains unknown. There are no LeanFlow per-response deadlines.

Saved knob profiles are JSON files under `<project>/.leanflow/flag-profiles/`,
readable by the CLI too (`leanflow flags profiles`). Note that `.leanflow/` is
git-ignored by default in the LeanFlow repo — if a profile defines a published
ablation, copy it somewhere tracked.

## Prover workspace

For redesigned `prove` runs, **Live** opens a theorem dependency graph, each
statement and its informal justification, the current `PLAN.md`, individual
prover jobs, and files changed. Select a theorem to inspect its dependencies and
dependants; open its source at the recorded line. Conditional proofs remain
visibly provisional until their dependencies pass verification.

Every theorem is classified once and shown the same way in the header count,
the graph, the tree, its detail panel, and its job row. Only the independent
verifier marks a theorem solved. A live prover makes it active. A prover that
completed while the theorem still reads `running` is "Pending verification";
`submitted`, `verifying`, and `integrating` show the check or integration step
in progress; a failed prover never implies a proof. The header reads, for
example, `Solved 0 · Active 2 · Pending 2 · Awaiting verification 1`.

The workspace shows global campaign usage and limits, controller checks and
builds, planning stages, agent capacity, staged changes awaiting verification,
and the exact stop reason. Native runs show agent invocations; historical runs
retain their original API-call accounting. A proposed graph under review stays
separate from the canonical graph.

Arrows run from each goal to its prerequisites. Shared lemmas appear once, with
all incoming dependencies retained. The legend distinguishes solved, active,
pending, awaiting-verification, blocked, and failed nodes. Activity reflects the
current prover jobs; status updates keep the graph layout stable.

Use **Zoom**, **Fit width**, and **Locate selected** to navigate. Search highlights
matching theorems without removing their dependency context; **Next match** moves
to each result. Nodes support keyboard selection. **Tree** keeps the compact
lookup view available, and either view shares the statement and source details.

File changes open in the editor, with **View diff** comparing against the run's
recorded baseline when one exists. Each job links to its scratch proof and log;
**Events** opens the structured log filtered to that exact agent, where each row
expands into the model output it summarizes. **Send guidance**
queues a message for the orchestrator or a prover at its next decision boundary.
It cannot edit the plan or bypass verification.

The **Prover setup** launch card provides standard/research mode, dependency
order, parallelism, and separate **Prover** and **Orchestrator** sections with
aligned model and reasoning-effort selectors. Reasoning can use minimal, low,
medium, high, or extra high effort, restore a saved profile, or inherit the
launch/prover default. Compact badges identify profile values and overrides.
The **Global limits** card appears above Prover setup with optional campaign-wide
calls, reported tokens, and active-time limits. Controls come from the installed CLI’s catalog;
unknown usage remains `—` and unset global limits display as `Unlimited`.

The editor reads `leanflow runs prover RUN_ID --json`, which returns
`{version: 1, found, prover}` for that exact run. It submits guidance through
`leanflow runs prover-message RUN_ID --agent AGENT_ID --message TEXT`.
The CLI owns state layout and durable inbox writes; editor paths are checked
against the selected run's project and recorded artifacts before opening them.

The workspace refreshes every three seconds while a run is live, but the CLI is
invoked only when the run's `state.json` has changed: the host compares the
file's size and modification time and otherwise serves the last snapshot.
Opening an artifact reuses that snapshot for its allowlist check. A snapshot
never replaces a newer one, and a failed read keeps the last good state visibly
marked stale. Refresh always re-reads.

Saved profiles say where they can launch. A knob the extension refuses is
listed with its reason — unknown to the installed catalog, terminal-only,
launcher-owned, or an invalid value — and one click saves a VS Code-compatible
copy without those knobs or drops refused one-off overrides. The resolved plan
lists the effective dedicated prover configuration, including values inherited
from the profile, and says where the run id appears once it is assigned.

## Development

```bash
npm install
npm run watch      # rebuild host + webview on change
npm run typecheck
npm test           # launch, trust-boundary, storage, isolation, scoring, and statistics tests
npm run package    # produces a .vsix
```

Press <kbd>F5</kbd> in VS Code to launch an Extension Development Host.

The CLI surfaces this extension depends on — `leanflow flags`, `leanflow runs`,
and `leanflow workflow --dry-run --json` — are covered by
`tests/leanflow/test_flag_catalog.py`, `tests/leanflow/test_runs_command.py`, and
`tests/leanflow/test_extension_launch_contract.py` in the main repository.
