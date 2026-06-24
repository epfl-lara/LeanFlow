# EPFLemma

EPFLemma is a Lean-first AI automation tool. It drives a language model inside a real Lean 4 project to repair proofs, formalize mathematics from source documents, and verify a whole project until no `sorry` remains.

Point it at a Lean file or project and it inspects diagnostics and goals, edits proofs, re-verifies with Lean after every step, and keeps going — with workflow logs, checkpoints, and resumable state — until the target actually builds clean.

```bash
epflemma                          # interactive shell
epflemma workflow prove Main.lean # or run a workflow directly
```

The scope is deliberately narrow: Lean automation — proof repair, formalization, and project verification — plus the machinery that supports it: provider routing, local model runtimes, a host-isolated sandbox, and opt-in multi-agent runs. It is not a general chat assistant.

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

- local Loogle on Linux/macOS/WSL, with public remote Loogle fallback when local setup is cold, unavailable, or built for a different Lean toolchain than the active project
- REPL-backed `lean_multi_attempt` for faster tactic screening after `project init` builds `repl`
- local LeanExplore semantic search when `lean-explore[local]` is installed and `lean-explore data fetch` has prepared the index; hosted LeanExplore API calls remain opt-in via `LEANEXPLORE_API_KEY`

It also checks the external CLI tools used by core workflows: `rg` for local
search and Poppler's `pdftotext`, `pdfinfo`, and `pdfimages` for PDF source
inspection.

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

Document formalization has a separate handoff boundary. `/formalize` and `/autoformalize` first produce a buildable Lean draft with source-linked declarations and intentional `sorry` proofs. That draft is considered ready when the module builds and the blueprint's statement/source review is approved. The formalizer then exits; proof filling starts only when the user explicitly runs `/prove SomeFile.lean` or `/prove` after reviewing the generated formalization.

For project-scoped work, `/prove` without a file starts the project prove manager. It scans Lean files with remaining `sorry`, ranks them with candidate-to-candidate dependency analysis, theorem difficulty, local hints/examples, bounded source context, and length signals, asks the configured LLM for a prioritized file order when available, records that plan, and then assigns one file at a time to the existing `/prove SomeFile.lean` path. Parallel agents stay disabled unless the user explicitly opts into swarm mode.

For document formalization, `/formalize` accepts a project-local `.tex` file, `.pdf` file, or directory containing a TeX project. Directory inputs are resolved deterministically to a main `.tex` source, with included `.tex`, bibliography, PDF, figure, and TeX support files recorded in the preflight manifest; ambiguous TeX roots fail before launch. LaTeX preflight recognizes standard theorem environments, plain-TeX `\profess...\endprofess` blocks, and common custom theorem declarations such as `\newtheorem`, `\declaretheorem`, `\newmdtheoremenv`, `\mdtheorem`, `\spnewtheorem`, and `\newtcbtheorem`; adjacent proof environments are copied into the initial blueprint as source proof excerpts. EPFLemma exposes `read_pdf` as the direct model-facing tool for reading project-local PDF text. EPFLemma creates a preflight manifest, extracted-text cache, Markdown planner blueprint, generated blueprint skill, and active Lean target file under the project, then asks the drafting agent to plan definitions/lemmas/theorems with source comments. When the draft is otherwise ready and only source-review approval is missing, the runner starts the configured independent statement/source verifier; by default this is the managed reviewer agent, while command/model verifier outputs are logged as advisory review. After the deterministic local handoff checks, review-approved blueprint pass, and final generated-file organization pass, the formalizer stops and prints the explicit `/prove` command to run after user review. `/prove SomeFile.lean` auto-attaches the generated blueprint skill when the file has a nearby `Blueprint.md`; you can also pass `--additional-skill path/to/SKILL.md`.

For file-scoped work, EPFLemma drives the agent one declaration at a time. The runner owns the queue, refreshes diagnostics after edits, records failed attempts per theorem, and advances only when Lean verification says the current target is clean. Same-file queue steps use the LeanProbe-backed incremental verifier first, so imports/header state and prior declaration environments stay warm; Lake remains the final file/project sweep and fallback gate. If a theorem turn exhausts its API-step budget, the runner records that as a failed attempt, comments the failed declaration in the Lean file, and restores the original safe `sorry` body when it has an exact baseline slice; the theorem remains pending for the next queue cycle.

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

Skills route the agent to the right workflow behavior. Specs define the native tool order, verification gates, doctor reporting, and worker recommendations. Keep skills thin: if a rule changes the workflow contract, put it in the linked spec and let the skill point to that contract instead of duplicating the full procedure.

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

For an existing Codex CLI login:

```bash
codex login
epflemma config set model.provider codex
epflemma provider --requested codex
```

When using the Codex provider, EPFLemma reads the Codex CLI model and
reasoning effort from `~/.codex/config.toml` unless
`EPFLEMMA_CODEX_MODEL` or `EPFLEMMA_CODEX_REASONING_EFFORT` is set.

To test a single run without changing the saved provider:

```bash
epflemma workflow --provider codex prove Main.lean
```

For local runtimes:

```bash
epflemma models local use vllm google/gemma-3-27b-it
epflemma models local start vllm google/gemma-3-27b-it
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
- `core/`: shared kernel — home authority (`home.py`), SQLite session store (`state.py`), clock,
  constants, tool registry (`model_tools.py`), toolsets, helpers
- `agent/`: prompt assembly, compression, display, auxiliary clients, `AIAgent` collaborators
- `tools/`: Lean-kernel tools and file/session tooling
- `run_agent.py`: core conversation loop

## Documentation

This README is the entry point. Deeper references:

- [Product reference](docs/product-reference.md): the full, detailed feature documentation.
- [Sandbox runtime](docs/sandbox-runtime.md): the isolated container runtime, patch export, install, and update flow.
- [Native Lean workflow surface](docs/native-lean-workflow-surface.md): the native Lean workflow and tool contract.
- [Architecture](ARCHITECTURE.md): the module map and internals. [Contributing](AGENTS.md): coding standards and the quality gate.

## Development

Create the repo venv and install editable deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'
```

Before committing, run the quality gate (format, lint, types, tests):

```bash
source .venv/bin/activate
ruff format .          # black-compatible formatter (CI checks with `ruff format --check .`)
ruff check .           # lint
mypy                   # type-check the gated module set
python -m pytest -q    # full suite
```

Coding standards, the layering rules, and the gotchas to avoid are documented in
[AGENTS.md](AGENTS.md); the module map is in [ARCHITECTURE.md](ARCHITECTURE.md).
