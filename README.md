# LeanFlow

**LeanFlow is a Lean-first AI automation tool.** It drives a language model inside a real Lean 4 project to repair proofs, formalize mathematics from source documents, and verify a whole project until no `sorry` remains.

Point it at a Lean file or project and it inspects diagnostics and goals, edits proofs, re-verifies with Lean after every step, and keeps going — with workflow logs, checkpoints, and resumable state — until the target actually builds clean.

```bash
leanflow                          # interactive shell
leanflow workflow prove Main.lean # or run a workflow directly
```

## Features

- **Proof repair** — completes and fixes Lean proofs one declaration at a time, re-verifying with Lean after every edit (warm LeanProbe incremental checks, with Lake as the final gate). A run is "done" only when the code builds with no open goals and no `sorry`.
- **Formalization** — turns a LaTeX/PDF source document or TeX project into a buildable, statement-verified Lean draft with source-linked declarations, then hands off to proof repair.
- **Whole-project verification** — scans a project for remaining `sorry`s, ranks the files by dependency and difficulty, and works them in order until the project is clean.
- **Resumable** — every run records activity, logs, checkpoints, file locks, and its work queue under the project, so long sessions resume without starting blind.
- **Flexible providers** — OpenAI-compatible endpoints, a Codex CLI login, or local model runtimes (vLLM / Ollama / llama.cpp).
- **Host isolation** — an optional sandbox runs the agent in a container and exports the result as a patch, never touching your working tree.
- **Opt-in multi-agent** — file-lock-aware swarm mode for concurrent work, off by default.

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

When it can do so safely, `project init` also prepares Lean REPL acceleration (adds the
`leanprover-community/repl` dependency and builds it) and local `lean-lsp-mcp` power modes — local
Loogle, REPL-backed tactic screening for `lean_multi_attempt`, and optional local LeanExplore
semantic search (`lean-explore[local]`). Anything unavailable falls back cleanly and is reported by
`leanflow doctor`.

## What a run guarantees

A `prove` run is not "done" because the agent made a plausible edit — it is done only when Lean agrees. A successful run ends with:

- the relevant Lean code building
- clean diagnostics and no open goals
- no `sorry` in the active target
- no remaining project `sorry` outside dependencies

LeanFlow reaches that by working in small, Lean-verified steps rather than one big edit:

- **`prove <file>`** drives the model one declaration at a time, re-checking with Lean after every edit and advancing only when the target is clean. Failed attempts are recorded and the original `sorry` is restored, so the file always stays buildable.
- **`prove`** (no file) scans the project for remaining `sorry`s, ranks the files, and works them one at a time. Parallel agents stay off unless you opt into swarm mode.
- **`formalize` / `autoformalize`** turn a LaTeX/PDF source into a buildable Lean draft with source-linked statements and intentional `sorry`s. The draft is handed off once it builds and its statement/source review is approved; you then run `/prove` to fill in the proofs.

The deeper mechanics (LaTeX preflight, the blueprint/verifier handoff, the project prove-manager, queue and checkpoint internals) are in the [product reference](docs/product-reference.md).

## Workflows

- `prove` — repair and complete existing Lean proofs.
- `formalize` — turn a LaTeX/PDF source document or TeX project into statement-verified Lean declarations; `/prove` then fills the resulting `sorry`s.
- `draft` — create Lean declarations and proof skeletons.
- `review` — inspect blockers, diagnostics, goals, and remaining `sorry`.
- `checkpoint` — summarize workflow state for resume or handoff.
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

## Providers and local runtimes

Inspect the active route with `leanflow provider`. To point at an OpenAI-compatible endpoint:

```bash
export LEANFLOW_OPENAI_BASE_URL="https://inference.rcp.epfl.ch/v1"
export LEANFLOW_OPENAI_API_KEY="..."
leanflow provider --requested custom
```

To use an existing Codex CLI login (model and reasoning effort are read from `~/.codex/config.toml`
unless `LEANFLOW_CODEX_MODEL` / `LEANFLOW_CODEX_REASONING_EFFORT` are set):

```bash
codex login
leanflow config set model.provider codex
```

To run a local model server (`vllm`, `ollama`, or `llama.cpp`):

```bash
leanflow models local start vllm google/gemma-3-27b-it
leanflow provider --requested local
```

Override the provider for a single run without changing the saved default:

```bash
leanflow workflow --provider codex prove Main.lean
```

## Multi-agent mode

LeanFlow does not spawn agents by default. Opt into swarm mode only when you want concurrent Lean work:

```bash
leanflow workflow prove Main.lean --agents 3
```

Swarm mode uses file-lock-aware delegation: locks live in `.leanflow/workflow-state/file_locks.json`,
and file-write tools reject edits when another agent owns the file. Use `--prompt` for run-specific
guidance on top of the Lean-first workflow contract:

```bash
leanflow workflow prove Main.lean --prompt "try abs_abs_sub before ring_nf"
```

## Project state

LeanFlow keeps user-level state separate from per-project workflow state:

- user config: `~/.leanflow/config.yaml`  ·  user env: `~/.leanflow/.env`
- project manifest: `.leanflow/project.yaml`  ·  project workflow state: `.leanflow/workflow-state/`

Workflow state holds activity, logs, checkpoints, file locks, route decisions, failed-attempt
history, project prove-manager plans, and outcomes — this is what lets long Lean runs resume.

## Skills and specs

LeanFlow steers the agent with a small curated Lean skill core in `leanflow_skills/` (e.g.
`lean-proof-loop`, `lean-theorem-queue-worker`, `lean-diagnostics`, `lean-formalization`,
`lean-search`, `lean-refactor-golf`). The canonical
workflow contract lives in markdown specs under `leanflow_specs/workflows/` and `leanflow_specs/workers/`.

Skills route the agent to the right workflow behavior; specs define the native tool order, verification
gates, and worker recommendations. Keep skills thin — if a rule changes the workflow contract, put it in
the linked spec and have the skill point to it rather than duplicating the procedure.

## Documentation

- [Product reference](docs/product-reference.md) — the full feature documentation.
- [Sandbox runtime](docs/sandbox-runtime.md) — the isolated container runtime, patch export, and update flow.
- [Native Lean workflow surface](docs/native-lean-workflow-surface.md) — the native Lean workflow and tool contract.
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
