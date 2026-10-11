# LeanFlow

**LeanFlow is a Lean-first AI automation tool.** It drives a language model
inside a real Lean 4 project to repair proofs, formalize mathematics from source
documents, and complete proof workflows until no `sorry` remains.

Point it at a Lean file or project with `sorry` holes. Its prover works in private
scratch files, independently verifies candidates, and saves its plan, theorem
dependencies, logs, and spending until the requested scope is verified, an explicit global limit is reached, or
an actionable blocker prevents progress.

```bash
leanflow                          # interactive shell
leanflow workflow prove Main.lean # or run a workflow directly
leanflow workflow prove Main.lean --provider codex --research
leanflow workflow prove Main.lean --provider lemex --model zai-org/GLM-5.2
```

## Features

- **Protected proof completion** — fills authorized `sorry` holes with independently checked proofs. Warm LeanProbe provides feedback; strict axiom checks and a final Lake build decide completion.
- **Formalization** — turns a LaTeX/PDF source document or TeX project into a buildable, statement-verified Lean draft with source-linked declarations, then hands off to proof repair.
- **Two proving modes** — research is the default, with fresh planning/review sessions and concurrent provers working bottom-up through a theorem graph. Standard uses one prover directly.
- **Resumable limits** — agent invocations, active time, notes, scratch proofs, and failed attempts survive restart. Limits are optional; there is no LeanFlow per-response deadline.
- **Grounded research** — bounded paper/web retrieval and arithmetic experiments support an informal plan; reviewed plans can add explicitly pinned Lean libraries.
- **Live proof workspace** — the VS Code extension shows theorem dependencies, the plan, per-job logs, clickable source changes and diffs, and usage metrics.
- **Three native backends** — Codex, Claude Code, and Lemex retain their own authentication, model settings, tools, retries, and context compression.
- **Host isolation** — an optional sandbox runs the agent in a container and exports the result as a patch, never touching your working tree.
- **Explicit concurrency** — research proving defaults to two prover jobs; standard proving uses one. Other workflows use one native agent and an independent source reviewer where required.

The scope is deliberately narrow: Lean automation, not a general chat assistant.

## Install

```bash
git clone https://github.com/epfl-lara/LeanFlow.git
cd LeanFlow
./scripts/install-internal.sh
```

Verify the install:

```bash
leanflow --help
leanflow doctor          # checks the Lean toolchain, MCP backends, and external tools
```

`doctor` also checks the external CLIs the workflows use: `rg` for local search and Poppler's
`pdftotext` / `pdfinfo` / `pdfimages` for reading PDF sources.

## Quick start

Register an existing Lean project, then run a workflow:

```bash
cd /path/to/lean-project
leanflow project init                  # registers the project (and sets up Lean acceleration when safe)
leanflow workflow prove Main.lean      # repair proofs in a file
leanflow workflow formalize paper.tex  # formalize a source document
```

Or use the interactive shell (the leading `/` is optional):

```bash
leanflow
```

```text
/prove [Main.lean]      /formalize docs/paper.tex      /autoformalize docs/
/goals   /diagnostics   /proof-state
/workflow status | activity | log 120
/skills   /provider   /doctor   /mcp status   /exit
```

From another terminal, `leanflow status` returns a bounded live summary without
waiting on the sandbox engine. Use `leanflow status --verbose` only when you
also want the larger run history and a live sandbox-engine probe.

When it can do so safely, `project init` also prepares Lean REPL acceleration (adds the
`leanprover-community/repl` dependency and builds it) and local `lean-lsp-mcp` power modes — local
Loogle, REPL-backed tactic screening for `lean_multi_attempt`, and optional local LeanExplore
semantic search (`lean-explore[local]`). Anything unavailable falls back cleanly and is reported by
`leanflow doctor`.

## What a run guarantees

A `prove` run succeeds only after independent Lean checks accept its proof
replacements and the final gate passes:

- the relevant Lean code building
- no unfinished proof or disallowed axiom in accepted declarations
- no `sorry` in the requested source files

The prover preserves supplied source outside authorized holes. The controller can
add reviewed helper imports/modules and Lake declarations, recording their diffs.
Original statements remain fixed; repairing arbitrary code outside a hole needs a
separate editing workflow.

- **`prove <file>`** completes holes in that file. Definition holes require explicit `LEANFLOW_PROVER_FILL_DEFINITIONS=1` authorization.
- **`prove`** without a file discovers eligible project source files.
- **Research (the default)** develops an informal plan, constructs and reviews a DAG, and schedules independent prover jobs bottom-up. Optional top-down candidates remain untrusted until dependencies close and strict checks pass.
- **`prove --clean-room`** works with Codex, Claude Code and Lemex using only project files and installed libraries. External web/repository research and remote Lean search are disabled; bridge commands run without network access. Native model connections remain available. Clean-room checkpoints can resume, but online or legacy runs cannot be converted into clean-room tests.
- **`formalize` / `autoformalize`** turn a LaTeX/PDF source into a buildable Lean draft with source-linked statements and intentional `sorry`s. The draft is handed off once it builds and its statement/source review is approved; you then run `/prove` to fill in the proofs.

Headless proof outcomes are explicit: `0` means verified, `3` means an authoritatively promoted
main-goal disproof, `2` means unresolved but checkpointed/resumable, `1` is a startup/runtime
failure, and `130` is a signal interruption. LeanFlow never returns success while the requested
scope still contains `sorry`.

See the [prover workflow](docs/prover-workflow.md) for budgets, verification,
isolation requirements, saved artifacts, and current limits; the
[research note](docs/prover-redesign-research.md) records the source evidence.
Other workflow mechanics remain in the [product reference](docs/product-reference.md).

## Workflows

- `prove` — repair and complete existing Lean proofs.
- `formalize` — turn a LaTeX/PDF source document or TeX project into statement-verified Lean declarations; `/prove` then fills the resulting `sorry`s.
- `draft` — create Lean declarations and proof skeletons.
- `review` — inspect blockers, diagnostics, goals, and remaining `sorry`.
- `refactor` / `golf` — simplify existing Lean code without breaking verification.

`autoprove` and `autoformalize` are compatibility aliases of `prove` and `formalize`.

## Sandbox (host isolation)

Run a workflow inside a container so the model can edit freely without touching your working tree:

```bash
./scripts/install-sandbox.sh
cd /path/to/lean-project
leanflow-sandbox workflow prove Main.lean
leanflow sandbox status
```

The sandbox builds a local Docker/Podman image, copies the active project into a per-run worktree,
and exports the final diff as `changes.patch` under `~/.leanflow/sandbox/runs/<run-id>/`. See the
[sandbox runtime](docs/sandbox-runtime.md) doc for image options and the update flow.

## Native backends

LeanFlow supports exactly `codex`, `claude`, and `lemex`. Inspect the selection with
`leanflow provider`; select a backend with `--provider` or `model.provider`.

| Backend | Authentication and settings |
| --- | --- |
| `codex` | Existing Codex login/subscription or standard `OPENAI_API_KEY`; native Codex configuration |
| `claude` | Existing Claude Code login/subscription or `ANTHROPIC_API_KEY`; native Claude configuration |
| `lemex` | Lemex configuration, including `LEMEX_BASE_URL` and `LEMEX_API_KEY` |

Configure the native CLI first, then run LeanFlow. For example:

```bash
codex login
leanflow workflow prove Main.lean --provider codex
leanflow workflow prove Main.lean --provider claude

export LEMEX_BASE_URL="https://inference.rcp.epfl.ch/v1"
export LEMEX_API_KEY="..."
leanflow workflow prove Main.lean --provider lemex
```

Lemex owns endpoint details, model catalogs, sampling and compaction. LeanFlow does
not translate RCP credentials, inject temperature/top-p, or choose a default model
or effort. `--model` and explicitly configured effort are deliberate overrides.
The same native boundary serves planning, research, proof jobs and source reviews.

The installer adopts an existing self-contained Lemex distribution into an
immutable, content-hashed LeanFlow runtime. If none is available, it installs a
pinned Lemex source revision into a private prefix; that fallback needs Lemex's
build dependencies. It preserves the global executable and `~/.lemex` settings.
`leanflow doctor` checks executable availability; it does not authenticate or make
paid model requests.

This is a breaking provider migration. Old direct API/provider names, local-server
management, command-template advisors, sampling/compression settings and per-turn
budgets are rejected with migration guidance. Keep a backup of the old LeanFlow
config, remove retired settings, set `schema_version: 2`, and choose one backend.
Clear `model.default` and `agent.reasoning_effort` to inherit native defaults, or
keep intentional overrides. Native credentials stay in their native setup.
Historical run artifacts remain inspectable; start a new native run rather than
resuming an old provider conversation.

## Optional global limits

All three limits default to unset (unlimited); zero means exhausted:

```bash
leanflow config set limits.agent_calls 100
leanflow config set limits.tokens 1000000
leanflow config set limits.wall_time_s 28800
```

The corresponding environment overrides are `LEANFLOW_PROVER_AGENT_CALLS`,
`LEANFLOW_PROVER_TOKENS`, and `LEANFLOW_PROVER_WALL_TIME_S`. These also apply to
formalization, review, refactor, golf and draft workflows. One native process invocation is one agent
call, regardless of its internal model requests or compaction. Active time is
cumulative across resumes and includes tools and verification. Native-reported
tokens gate future invocations; a running invocation can exceed the threshold.
Missing usage remains unknown and prevents another token-capped invocation.
There are no LeanFlow three-minute or twenty-minute response/check cutoffs.

The VS Code launch page exposes global limits for every workflow. Research
shows separate **Prover calls** and **Orchestrator calls** settings, defaulting
to **150** and **50** per run. One `codex exec`, `lemex exec`, or Claude invocation
counts as one call; planning, review and research share the orchestrator allowance.
Clear a role limit for unlimited calls. Retry, proof-direction change,
recovery-decision and graph-size controls remain configurable. See
[limits and accounting](docs/product-reference.md#limits-and-accounting) for
their exact meanings and environment overrides.

## Multi-agent mode

Research is the default: a planning orchestrator coordinates concurrent provers
on the theorem DAG, solving dependencies first (bottom-up). Change concurrency
with:

```bash
leanflow workflow prove Main.lean --research --research-workers 3
```

Choose Standard in VS Code for direct sequential proving, or set
`LEANFLOW_PROVER_MODE=standard` in the shell. The help control beside Mode
explains both options. Explicit saved mode and order choices are preserved.

`--agents 3` is a compatibility spelling for the same prover concurrency. Only
one controller owns canonical source; jobs have independent scratch workspaces.
Other workflows run one native agent. Use `--prompt` for
run-specific guidance:

```bash
leanflow workflow prove Main.lean --prompt "try abs_abs_sub before ring_nf"
```

## Project state

LeanFlow keeps user-level state separate from per-project workflow state:

- user config: `~/.leanflow/config.yaml`  ·  user env: `~/.leanflow/.env`
- project manifest: `.leanflow/project.yaml`  ·  project workflow state: `.leanflow/workflow-state/`

Prover state lives in `.leanflow/workflow-state/prover/<run-id>/`, including
`PLAN.md`, `DAG.json`, protected source baselines, metrics, and separate job logs.
`leanflow runs prover <run-id> --json` returns the selected run's snapshot.
Resume with `LEANFLOW_PROVER_RESUME_RUN_ID=<run-id>` and the same workflow target;
it creates a new run with lineage while retaining the saved configuration and
invocation spending and active time. Other native workflows store proposals,
receipts, logs and limits in `.leanflow/native-workflows/<run-id>/`; resume with
`LEANFLOW_NATIVE_RESUME_RUN_ID` and the same backend, scope and limits.

Rejected proof submissions return feedback within the same pass. A prover can
request a resource agent for a concrete question; its native invocation also
counts toward the same global call limit.

## Runtime knobs

The runtime reads feature switches and budgets from `LEANFLOW_*` environment
variables. `leanflow flags` is the catalog of them:

```bash
leanflow flags list --ablatable        # knobs worth flipping in an experiment
leanflow flags show LEANFLOW_PROVER_AGENT_CALLS
leanflow flags effective --changed     # what this environment actually sets
leanflow flags diff default research   # compare the named profiles
```

Named knob profiles are JSON files under `.leanflow/flag-profiles/` (project) or
`~/.leanflow/flag-profiles/` (user), listed by `leanflow flags profiles`. Pair
them with `leanflow workflow --dry-run --json` to see the exact argv and
environment a run would receive before starting it — which is what makes two
runs comparable. The catalog also marks which settings are safe for editor
profiles: path redirection, raw-request capture, secret-redaction controls, and
approval bypasses remain terminal-only and cannot be injected by the extension.

`leanflow runs` exposes recorded state without requiring consumers to know the
on-disk layout. `metrics RUN_ID --json` reports only evidence bound to that run:
the verified activity stream plus a write-once final declaration/outcome and
launch/final provenance snapshot. A separately sealed digest detects accidental
edits and storage corruption (it is not a signature against a hostile filesystem
owner). Exact evidence includes a canonical,
credential-redacted digest of the effective `LEANFLOW_*` launch environment,
content-addressed proof source, ignored project manifest/guidance, selected
skills, LeanFlow runtime, Python/package inventory, behavior configuration, and
dependency identities, complete Git command evidence, and matching
`runner-exit`/final-outcome exit codes. Usage reports independent
API-call, token, and cost completeness; an earlier provider total followed by
unaccounted usage remains `null`, not a lower bound presented as exact. Provider
retries, auxiliary calls, command experts, iteration-limit summaries, and
dispatched child runs are explicitly unmetered until their full usage can be
bound, so their API/token/cost totals cannot be presented as complete. Missing
evidence remains explicitly unscored instead of falling back to mutable
project-wide state. `list --json` strictly audits hot, final, and retained
history, reports completeness (including limit truncation), and preserves
`exit_code`, `terminal_phase`, and a stable terminal status for both hot and
archived runs. A retained stream without terminal evidence is `unknown`, never
assumed still running. `log RUN_ID` reads that
run's timestamped console log, while `stop RUN_ID` interrupts only a revalidated
live process identity. The other observers are `status`, `events`, `journal`,
`outcomes`, and `types`; `provenance --json` describes the current checkout
separately from any historical run.

## VS Code extension

[`vscode-extension/`](vscode-extension/README.md) is an editor front end over the
same CLI: a launcher with a resolved-plan preview, live run state, a filtered
view of the structured activity stream, a browsable knob catalog with profile
save/diff, and knob-ablation sweeps. Prover runs also show a theorem dependency
tree, the shared plan, per-job scratch/log links, source diffs, addressed guidance,
and request/token/cost metrics. Research cells use private detached clones
of one clean Git baseline, freeze their profile definitions and randomized
order, resolve an explicit provider/model before the first paid launch, and
score only from run-bound final evidence plus a complete source-history audit;
unverifiable rows remain explicitly unscored. Exports use the
`evals/harness.append_result` shape.

```bash
cd vscode-extension && npm install && npm run package
```

## Skills and specs

LeanFlow steers the agent with a small curated Lean skill core in `leanflow_skills/` (e.g.
`lean-bounded-prover`, `lean-prover-orchestrator`, `lean-diagnostics`, `lean-formalization`,
`lean-search`, `lean-refactor-golf`). The canonical
workflow contract lives in markdown specs under `leanflow_specs/workflows/` and `leanflow_specs/workers/`.

Skills route the agent to the right workflow behavior; specs define the native tool order, verification
gates, and worker recommendations. Keep skills thin — if a rule changes the workflow contract, put it in
the linked spec and have the skill point to it rather than duplicating the procedure.

## Documentation

- [Product reference](docs/product-reference.md) — the full feature documentation.
- [Prover workflow](docs/prover-workflow.md) — current proving modes, settings, source protection, progress, and resume.
- [Prover research note](docs/prover-redesign-research.md) — cited papers, design decisions, and Firecrawl assessment.
- [Sandbox runtime](docs/sandbox-runtime.md) — the isolated container runtime, patch export, and update flow.
- [Architecture](ARCHITECTURE.md) — the module map and internals.
- [Contributing / agent guide](AGENTS.md) — coding standards, the quality gate, and the repo's gotchas.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the quality gate before committing (CI enforces all four):

```bash
black .                # format (https://github.com/psf/black); CI checks with `black --check .`
ruff check .           # lint (incl. unused-import F401)
mypy                   # type-check the gated module set
python -m pytest -q    # full suite
```

Coding standards, the layering rules, and the gotchas to avoid are in [AGENTS.md](AGENTS.md); the
module map is in [ARCHITECTURE.md](ARCHITECTURE.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
