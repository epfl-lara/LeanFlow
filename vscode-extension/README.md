# LeanFlow for VS Code

Launch and inspect LeanFlow workflows without leaving the editor. The extension
uses the `leanflow` CLI, so the editor and terminal share project setup, launch
settings, proof verification, and recorded run evidence.

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
  managed pinned Lemex distribution.

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

## Launch a workflow

**Launch** provides workflow, target, backend, model, prompt, skills, and axiom
settings. **Preview plan** resolves the command and effective settings without
starting work.

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
