# EPFLemma

EPFLemma is a Lean-first automation shell for mathematical coding agents.

Use it when you want an agent to work inside a Lean project, inspect Lean diagnostics and goals, repair proofs, formalize statements, and keep going until the project is actually clean.

The installed command is:

```bash
epflemma
```

The product is intentionally narrow. It is not a general chat assistant, browser workflow runner, scheduler, marketplace, or data-generation platform. The supported surface is Lean automation: proof repair, formalization, project verification, workflow logs, checkpoints, resumability, provider routing, local runtimes, and user-approved multi-agent runs.

## Quick Start

Install from a local checkout:

```bash
git clone https://github.com/epfl-lara/EPFLemma.git
cd EPFLemma
./scripts/install-internal.sh
```

If you already have the repo:

```bash
./scripts/install-internal.sh
```

Check the install:

```bash
epflemma --help
epflemma doctor
epflemma mcp status
epflemma config show
```

Initialize an existing Lean project:

```bash
cd /path/to/lean-project
epflemma project init
epflemma project show
```

`project init` also prepares Lean REPL acceleration when it can do so safely.
It prints step-by-step progress while checking the Lean toolchain, adding the
`leanprover-community/repl` dependency for `lakefile.toml` projects, and running
`lake update repl` / `lake build repl`. If REPL setup is not safe or fails,
EPFLemma keeps working and reports the fallback clearly.

The installer configures local `lean-lsp-mcp` power modes by default:

- local Loogle on Linux/macOS/WSL, with public remote Loogle fallback when local setup is cold or unavailable
- REPL-backed `lean_multi_attempt` for faster tactic screening after `project init` builds `repl`
- local LeanExplore semantic search when `lean-explore[local]` is installed and `lean-explore data fetch` has prepared the index; hosted LeanExplore API calls remain opt-in via `LEANEXPLORE_API_KEY`

Run the main workflows:

```bash
epflemma workflow prove Main.lean
epflemma workflow formalize docs/paper.tex
epflemma workflow autoformalize docs/paper-directory
```

Run those workflows in a host-isolated sandbox when you want the model to edit
freely without touching your working tree:

```bash
./scripts/install-sandbox.sh
cd /path/to/lean-project
epflemma-sandbox workflow prove Main.lean
epflemma status
epflemma sandbox status
```

The sandbox runner builds a local Docker/Podman image, copies the active
EPFLemma project into a per-run worktree, mounts only that worktree plus
EPFLemma sandbox cache directories, and exports the final diff as
`changes.patch` under `~/.epflemma/sandbox/runs/<run-id>/`. Re-run
`./scripts/update-sandbox.sh` after pulling repository changes to reinstall and
rebuild the sandbox image. Use `./scripts/install-sandbox.sh --with-local-lean-explore`
when you want the image to include the local LeanExplore embedding stack.

Start the interactive shell:

```bash
epflemma
```

Inside the shell, the most useful commands are:

```text
/prove
/prove Main.lean
/formalize docs/paper.tex
/autoformalize docs/paper-directory
/goals
/diagnostics
/proof-state
/workflow status
/workflow activity
/workflow log 120
/skills
/provider
/doctor
/mcp status
/exit
```

The shell also accepts forgiving forms without the leading slash:

```text
prove Main.lean
formalize docs/paper.tex
autoformalize docs/paper-directory
```

## What EPFLemma Tries To Guarantee

`prove` is not considered done because the agent made a plausible edit. A successful proof run should end with:

- the relevant Lean code building successfully
- clean diagnostics
- no open goals
- no `sorry` in the active target
- no remaining project `sorry` outside dependencies

Document formalization has a separate handoff boundary. `/formalize` and `/autoformalize` first produce a buildable Lean draft with source-linked declarations and intentional `sorry` proofs. That draft is considered ready when the module builds and the blueprint's statement/source review is approved; proof filling is then the next phase, either through `/prove SomeFile.lean` or the managed proof queue after handoff.

For project-scoped work, `/prove` without a file starts the project prove manager. It scans Lean files with remaining `sorry`, ranks them with candidate-to-candidate dependency analysis, theorem difficulty, local hints/examples, bounded source context, and length signals, asks the configured LLM for a prioritized file order when available, records that plan, and then assigns one file at a time to the existing `/prove SomeFile.lean` path. Parallel agents stay disabled unless the user explicitly opts into swarm mode.

For document formalization, `/formalize` accepts a project-local `.tex` file, `.pdf` file, or directory containing a TeX project. Directory inputs are resolved deterministically to a main `.tex` source, with included `.tex`, bibliography, and local asset files recorded in the preflight manifest; ambiguous TeX roots fail before launch. EPFLemma exposes `read_pdf` as the direct model-facing tool for reading project-local PDF text. EPFLemma creates a preflight manifest, extracted-text cache, Markdown planner blueprint, generated blueprint skill, and active Lean target file under the project, then asks the drafting agent to plan definitions/lemmas/theorems with source comments. When the draft is otherwise ready and only source-review approval is missing, the runner starts a fresh independent statement/source verifier agent; after that review-approved handoff, the managed proof queue can handle the remaining `sorry`s. `/prove SomeFile.lean` auto-attaches the generated blueprint skill when the file has a nearby `Blueprint.md`; you can also pass `--additional-skill path/to/SKILL.md`.

For file-scoped work, EPFLemma drives the agent one declaration at a time. The runner owns the queue, refreshes diagnostics after edits, records failed attempts per theorem, and advances only when Lean verification says the current target is clean. Same-file queue steps use the LeanInteract-backed incremental verifier first, so imports/header state and prior declaration environments stay warm; Lake remains the final file/project sweep and fallback gate. If a theorem turn exhausts its API-step budget, the runner records that as a failed attempt, comments the failed declaration in the Lean file, and restores the original safe `sorry` body when it has an exact baseline slice; the theorem remains pending for the next queue cycle.

## Main Workflows

- `prove`: repair and complete existing Lean proofs.
- `formalize`: turn a project-local LaTeX/PDF source document or TeX project directory into statement-verified Lean declarations; `/prove` fills the resulting `sorry`s.
- `draft`: create Lean declarations and proof skeletons.
- `review`: inspect blockers, diagnostics, goals, and remaining `sorry`.
- `checkpoint`: summarize workflow state for resume or handoff.
- `refactor` / `golf`: simplify existing Lean code without breaking verification.

Compatibility aliases:

- `autoprove` is an alias of `prove`
- `autoformalize` is an alias of `formalize`

## Project State

EPFLemma keeps user-level state separate from project workflow state:

- user config: `~/.epflemma/config.yaml`
- user env: `~/.epflemma/.env`
- project manifest: `.epflemma/project.yaml`
- project workflow state: `.epflemma/workflow-state/`

Workflow state includes activity, logs, checkpoints, file locks, route decisions, failed-attempt history, project prove-manager plans, and outcomes. This is what lets long Lean runs resume without starting blind.

EPFLemma can coexist with an older `gauss` install. It uses `~/.epflemma` and `.epflemma/`; it does not overwrite `~/.gauss` or the `gauss` binary.

## Skills And Specs

EPFLemma steers agents with a small curated Lean skill core in `epflemma_skills/`.

Common built-in skills:

- `lean-proof-loop`
- `lean-theorem-queue-worker`
- `lean-diagnostics`
- `lean-formalization`
- `lean-project-search`
- `lean-mathlib-search`
- `lean-refactor-golf`
- `lean-autonomous-swarm`
- `provider-fallback`
- `long-session-resume`

The canonical workflow contract lives in markdown specs under:

- `epflemma_specs/workflows/`
- `epflemma_specs/workers/`

Skills route the agent to the right workflow behavior. Specs define the native tool order, verification gates, doctor reporting, and worker recommendations.

## Provider And Runtime Setup

Inspect the active route:

```bash
epflemma provider
epflemma provider --requested custom
epflemma provider --requested local
```

For OpenAI-compatible endpoints such as RCP:

```bash
export EPFLEMMA_OPENAI_BASE_URL="https://inference.rcp.epfl.ch/v1"
export EPFLEMMA_OPENAI_API_KEY="..."
epflemma provider --requested custom
```

For local runtimes:

```bash
epflemma models local use vllm google/gemma-4-31B-it
epflemma models local start vllm google/gemma-4-31B-it
epflemma models local status vllm
epflemma provider --requested local
```

Supported local runtime families include `vllm`, `ollama`, and `llama.cpp`.

## Multi-Agent Mode

EPFLemma does not spawn agents by default.

Use swarm mode only when you explicitly want concurrent Lean work:

```bash
epflemma workflow prove Main.lean --agents 3
epflemma workflow formalize docs/paper.tex --agents 3
```

Swarm mode activates file-lock-aware delegation. Locks are stored in `.epflemma/workflow-state/file_locks.json`, and normal file write tools reject edits when another agent owns the file.

Use `--prompt` for run-specific task guidance without replacing the Lean-first workflow contract:

```bash
epflemma workflow prove Main.lean --prompt "try abs_abs_sub before ring_nf"
epflemma workflow autoformalize docs/paper-directory --prompt "focus on the main theorem only"
```

`--goal` is still accepted as a compatibility alias for the same scoped prompt field.

## Repository Map

- `epflemma_cli/`: CLI, shell UX, workflow orchestration, providers, local runtimes, locks, workflow state
- `epflemma_skills/`: curated Lean-first skills
- `epflemma_specs/`: workflow and worker contracts
- `agent/`: prompt assembly, compression, display, auxiliary clients
- `tools/`: Lean-kernel tools and file/session tooling
- `run_agent.py`: core conversation loop
- `model_tools.py`: tool discovery and dispatch
- `toolsets.py`: Lean toolset definitions
- `gauss_state.py`: compatibility SQLite session store

## Documentation

The README is now the human entry point. Deeper operational details live in:

- [Product reference](docs/product-reference.md): full detailed documentation that used to live in the README.
- [Sandbox runtime](docs/sandbox-runtime.md): isolated container runtime, patch export, install, and update flow.
- [Native Lean workflow surface](docs/native-lean-workflow-surface.md): native Lean workflow and tool contract.
- [Autonomous workflow context carryover](docs/autonomous-workflow-context-carryover-analysis.md): historical analysis and current context-reset behavior.

## Development

Create the repo venv and install editable deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'
```

Run focused tests:

```bash
source .venv/bin/activate
python -m pytest tests/epflemma -q -n 0
```

Recommended broader verification:

```bash
source .venv/bin/activate
python -m pytest tests/epflemma tests/agent/test_prompt_builder.py tests/agent/test_context_compressor.py -q -n 0
python -m epflemma_cli.main --help
./scripts/install-internal.sh
```
