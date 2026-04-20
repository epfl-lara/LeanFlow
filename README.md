# EPFLemma

EPFLemma is a Lean AI for Math shell focused on automated Lean coding agents. The command you install and run is still `epflemma`, so existing local setup and scripts stay stable.

The product is optimized for two main jobs:

- `prove`: drive Lean proof repair and completion until the code compiles cleanly
- `formalize`: translate mathematical intent into Lean declarations and verified proofs

It installs as `epflemma`, uses `~/.epflemma` for user-level config, keeps project-owned workflow state in `.epflemma/`, and can live alongside an existing `gauss` install without overwriting it.

This fork removes the old managed `claude-code` and `codex` backend flow. EPFLemma now runs Lean workflows through its own internal `epflemma-native` runtime and routes inference through direct provider APIs, OpenAI-compatible endpoints such as RCP, or local runtimes such as `vllm`, `ollama`, and `llama.cpp`.

## Product Direction

EPFLemma is intentionally Lean-first and automation-first.

- The default shell and workflow UX are built around Lean proving and formalization, not generic assistant chat.
- Autonomous workflows are judged by strict Lean verification, not by partial progress:
  - explicit successful build
  - clean diagnostics
  - no open goals
  - no `sorry`
- Multi-agent execution exists only as an explicit user-approved mode.
- Agents do not auto-spawn by default.
- When swarm mode is enabled, file locks are used to keep concurrent agents off the same file.

## Skills

EPFLemma ships with a small curated skill core for Lean workflows. Skills are not a side feature here; they are part of how the agent is steered toward proving, diagnostics, formalization, resume, and user-approved swarm behavior.

Built-in skills:

- `lean-proof-loop`
  - standard proof-repair loop for `prove`
  - emphasizes: inspect diagnostics/goals first, make minimal edits, rebuild, and do not stop until the project is actually verified
- `lean-diagnostics`
  - focused diagnostic mode for `review` and `checkpoint`
  - emphasizes: current blockers, open goals, verification state, and project-wide remaining `sorry`
- `lean-formalization`
  - formalization and declaration-building skill for `formalize` and `draft`
  - emphasizes: small verifiable steps, dependency order, and zero build errors / zero `sorry`
- `lean-refactor-golf`
  - refactor / golfing skill for `refactor` and `golf`
  - emphasizes: simplifying proof structure without breaking verification
- `lean-autonomous-swarm`
  - swarm skill used only when you explicitly launch a workflow with `--agents N`
  - emphasizes: file ownership, verifier roles, and strict final verification
- `provider-fallback`
  - provider/runtime fallback helper when you need to switch between direct APIs, RCP/custom endpoints, or local runtimes
- `long-session-resume`
  - resume/handoff skill for compacted or checkpoint-restored workflows
  - emphasizes: trust persisted state, compare it to the current filesystem, and continue from the real current Lean state

### How Skills Are Assigned

There are three ways a skill gets into the agent:

1. Automatic workflow assignment
   - `prove` -> `lean-proof-loop`
   - `formalize`, `draft` -> `lean-formalization`
   - `review`, `checkpoint` -> `lean-diagnostics`
   - `refactor`, `golf` -> `lean-refactor-golf`
   - `--agents N` on autonomous workflows switches to `lean-autonomous-swarm`

2. Manual shell activation
   - `/skill lean-proof-loop`
   - `/skill lean-diagnostics`
   - `/skill reload`
   - direct activation also works via `/<skill-name>`

3. Resume/continuation context
   - the managed runner can carry the active skill through checkpoints, compaction, and autonomous continuation cycles

When a skill is active, its `SKILL.md` content is inserted into the agent prompt as explicit workflow guidance.

### Skill Install And Override Paths

Skill discovery order is:

- built-in repo skills in `epflemma_skills/`
- user overrides in `~/.epflemma/skills`
- project overrides in `.epflemma/skills`

Precedence is:

- project overrides user
- user overrides built-in

That means you can replace a built-in skill for one machine or one project without editing the shipped repo skill.

Install patterns:

- user-wide skill:
  - create `~/.epflemma/skills/<skill-name>/SKILL.md`
- project-local skill:
  - create `.epflemma/skills/<skill-name>/SKILL.md` inside the Lean project

Example:

```text
~/.epflemma/skills/my-proof-policy/SKILL.md
.epflemma/skills/lean-proof-loop/SKILL.md
```

The second example overrides the built-in `lean-proof-loop` only for that project.

### How The Agent Sees Skills

The agent does not install skills as code plugins. It loads them as prompt-time workflow instructions:

- the skill resolver finds the highest-precedence matching skill
- EPFLemma reads the skill’s `SKILL.md`
- that content is embedded into the agent prompt for the active workflow
- supporting files under `references/`, `templates/`, `scripts/`, and `assets/` stay discoverable through the skill system when needed

Use `/skills` to see what the agent can currently load and where each skill came from.

## What Ships

- `epflemma` CLI with EPFLemma shell branding
- `epflemma-agent` shared agent entrypoint
- Lean workflows:
  - `/draft`
  - `/review`
  - `/checkpoint`
  - `/refactor`
  - `/golf`
  - `/prove`
  - `/formalize`
- Local runtime commands:
  - `epflemma models local list`
  - `epflemma models local start`
  - `epflemma models local stop`
  - `epflemma models local status`
  - `epflemma models local logs`
  - `epflemma models local use`

## Kernel-Only Scope

This repo is now intentionally trimmed to the Lean workflow kernel.

Supported product surface:

- EPFLemma shell UX on top of `epflemma`
- Lean proving and formalization workflows
- user-approved multi-agent swarm mode
- file locking for concurrent Lean editing
- curated Lean skill core in `epflemma_skills/`
- provider routing for direct APIs, RCP/custom endpoints, and local runtimes
- managed local runtimes: `vllm`, `ollama`, `llama.cpp`

Removed from the supported product:

- gateway and messaging platforms
- cron/scheduler product surfaces
- browser automation workflow surface
- website/landing page/docs site
- RL/benchmark environment suites
- generic and marketplace-style skill catalogs
- WhatsApp bridge and other non-Lean platform extras

If you still see references to legacy Gauss-era modules in comments or compatibility fallbacks, treat them as migration residue rather than supported product features.

## Name, CLI, and Paths

- Product name: `EPFLemma`
- CLI command: `epflemma`
- State directory: `~/.epflemma`
- Project manifest: `.epflemma/project.yaml`

The interface is styled around EPFL / Lean / AI-for-math work, but the executable name stays `epflemma` for compatibility and coexistence.

## Install

Direct local install from the current repo:

```bash
git clone https://github.com/Lemmy00/EPFLemma.git
cd EPFLemma
./scripts/install-internal.sh
```

If you already have the repo checked out locally, just run:

```bash
./scripts/install-internal.sh
```

`./scripts/install.sh` is the Morph/local-template wrapper. Use `./scripts/install-internal.sh` when you want the repo to install its own local CLI wrappers directly.

Default install locations:

- state: `~/.epflemma`
- wrappers: `~/.local/bin/epflemma`, `~/.local/bin/epflemma-agent`
- virtualenv: `./.epflemma-venv`

The installer does not touch `~/.gauss` or replace an existing `gauss` binary.

Custom install locations:

```bash
./scripts/install-internal.sh \
  --epflemma-home "$HOME/.epflemma" \
  --bin-dir "$HOME/.local/bin" \
  --venv-dir "$PWD/.epflemma-venv"
```

## Update

EPFLemma no longer uses the old `gauss update` flow. Update by reinstalling from the repo:

```bash
cd EPFLemma
git pull
./scripts/install-internal.sh
```

## Coexistence And Migration

EPFLemma is designed to coexist with Gauss:

- `gauss` and `epflemma` are separate binaries
- Gauss state stays in `~/.gauss`
- EPFLemma state stays in `~/.epflemma`
- Gauss project manifests stay in `.gauss/project.yaml`
- EPFLemma project manifests stay in `.epflemma/project.yaml`

On first run, EPFLemma can import legacy settings from:

- `~/.gauss/config.yaml`
- `~/.gauss/.env`
- `.gauss/project.yaml`

That import is one-time and non-destructive. After import, EPFLemma uses only its own paths.

## Quick Start

Check the install:

```bash
epflemma --help
epflemma doctor
epflemma config show
```

Initialize an existing Lean project:

```bash
cd /path/to/lean-project
epflemma project init
epflemma project show
```

Run a workflow:

```bash
epflemma workflow prove Main.lean
epflemma workflow prove Main.lean --agents 3
epflemma workflow formalize "Define the object and prove the first lemma"
```

Interactive mode:

```bash
epflemma
```

Inside the shell:

```text
/banner
/status
/workflow status
/workflow history
/workflow activity
/workflow log 120
/goals
/diagnostics
/proof-state
/provider
/swarm
/skills
/skill lean-proof-loop
/skill reload
/models local list
/cd path/to/project
/project init
/project create DemoProject --template-source https://github.com/example/lean-template.git
/prove Main.lean
/prove Main.lean --agents 3
/formalize "state the theorem"
/doctor
/config get model.default
/quit
```

Workflow commands also accept forgiving forms without the leading slash:

```text
prove Main.lean
prove Main.lean --agents 3
formalize "formalize this statement"
```

The interactive shell starts with an EPFLemma banner that shows the current route and the main Lean commands you are expected to use.

The bottom toolbar is live workflow context, not decoration. It surfaces:

- current managed workflow phase
- active file and target theorem
- latest build state
- active skill
- latest structured workflow event

The shell also reads persisted managed-workflow state so these commands work across resumed sessions:

- `/workflow status`
- `/workflow history`
- `/workflow activity`
- `/workflow log 120`
- `/goals`
- `/diagnostics`
- `/proof-state`

## Autonomous Lean Behavior

The native runner is designed for Lean automation, not free-form chat.

What the autonomous runner tries to do:

- identify the active Lean file and target declaration
- inspect diagnostics and goals first
- make the smallest useful edit
- rebuild or re-check diagnostics after meaningful edits
- continue until the target is verified or a concrete blocker remains

What counts as success:

1. the relevant Lean code builds successfully
2. diagnostics are clean
3. there are no open goals
4. there are no `sorry` in the active target
5. there are no remaining `sorry` elsewhere in the project outside dependencies

Autonomous workflows are intentionally stricter than a local file-only loop. `prove` and `formalize` should keep going until the project is clean, not merely until the current theorem looks finished.

EPFLemma writes managed workflow status, activity, checkpoints, file locks, and the full latest managed runner log into the active project’s `.epflemma/workflow-state/` directory by default so long runs stay next to the Lean repo you are debugging.

The verification loop is intentionally Lean-LSP-first:

- use diagnostics and proof goals for most iterations
- avoid repeated `lake env lean <file>` checks because they are slow on large imports
- prefer a focused `lake build <Module>` when the active file is close to clean
- reserve full-project `lake build` for milestone verification and final success checks

The inspection split is intentional:

- `/workflow activity` is the structured step feed: API calls, assistant plans, tool starts, resumes, checkpoints, and autonomous follow-ups
- `/workflow log 120` is the raw saved runner transcript when you want the exact command/tool chronology that scrolled by during execution

## User-Approved Swarm Mode

EPFLemma supports multi-agent Lean work, but only when the user explicitly requests it.

Default behavior:

- `prove` and `formalize` run as a single autonomous agent
- no automatic agent spawning is allowed

Explicit swarm behavior:

```bash
epflemma workflow prove Main.lean --agents 3
epflemma workflow formalize "formalize theorem X" --agents 3
```

What `--agents N` does:

- switches the native workflow from `epflemma-native` to the swarm-capable tool surface
- enables user-approved delegation inside that workflow only
- activates the `lean-autonomous-swarm` skill unless you manually selected another skill
- records the configured agent count in workflow status

Important constraint:

- swarm mode is user-approved only
- agents do not decide to spawn other agents unless the workflow was launched with `--agents N`

Recommended use:

- one agent per file when files are independent
- one verifier-oriented path to keep builds and diagnostics honest
- no duplicate ownership of the same Lean file

## Project Model

EPFLemma currently exposes two project commands:

- `epflemma project init [path] [--name NAME]`
- `epflemma project create <path> [--template-source SOURCE] [--name NAME]`
- `epflemma project show [path]`

Requirements for `project init`:

- the target must be inside a Lean 4 repo
- a Lean root must be detectable from `lakefile.lean` or `lakefile.toml`

EPFLemma writes:

- `.epflemma/project.yaml`
- `.epflemma/runtime/`
- `.epflemma/cache/`
- `.epflemma/workflows/`
- `.epflemma/workflow-state/`

## Skills And Overlays

EPFLemma ships a curated Lean-first skill core. It does not use the old broad marketplace-style catalog in the supported product.

Builtin skills live in:

```text
epflemma_skills/
```

User and project overlays live in:

```text
~/.epflemma/skills
.epflemma/skills
```

Overlay precedence is:

1. project
2. user
3. builtin

Supported shell commands:

```text
/skills
/skill <name>
/skill reload
/<skill-name>
```

Current curated builtin skills:

- `lean-proof-loop`
- `lean-diagnostics`
- `lean-formalization`
- `lean-refactor-golf`
- `lean-autonomous-swarm`
- `provider-fallback`
- `long-session-resume`

Lean workflows automatically select a matching default skill unless you activate another one explicitly.

The swarm-specific skill is only relevant when the user enabled parallel agents. It encodes:

- file-specific delegation
- verifier roles
- strict zero-sorry finish conditions
- lock-before-edit behavior for shared Lean files

## File Locking

EPFLemma includes file reservations for concurrent Lean work.

Purpose:

- stop two agents from editing the same Lean file at the same time
- make user-approved swarm runs safer and easier to reason about

How it works:

- file reservations are stored in `.epflemma/workflow-state/file_locks.json` inside the active project
- the native workflow tool surface includes:
  - `acquire_file_lock`
  - `release_file_lock`
  - `list_file_locks`
- file writes through `write_file` and `patch` respect existing locks
- if another agent owns the lock, the write is rejected instead of silently racing
- locks are keyed to the stable agent session id, not a transient per-turn task id
- native runner and delegated child agents release their locks on exit

Current scope:

- lock enforcement is wired into file-tool writes
- this gives strong protection for the recommended edit path
- terminal-based ad hoc shell edits are still a weaker path and should be avoided in swarm workflows

## Repository Layout

The main active codepaths are:

- `epflemma_cli/` for shell UX, config, project/workflow orchestration, local runtimes, locks, and workflow state
- `epflemma_skills/` for the curated Lean skill core
- `agent/` for prompt assembly, context compression, display, and shared agent internals
- `tools/` for the Lean-kernel tool surface
- `tests/epflemma/` plus selected agent/runtime tests for the supported product

You should not expect deleted gateway, website, cron, data-generation, voice, or broad skill-catalog directories to exist anymore.

## Provider Configuration

Inspect the active provider selection:

```bash
epflemma provider
epflemma provider --requested zai
epflemma provider --requested local
epflemma provider --requested custom
```

EPFLemma supports three provider classes:

1. Direct provider APIs
2. OpenAI-compatible remote endpoints
3. Managed local runtimes

### Direct Providers

Supported direct providers include:

- `zai`
- `kimi-coding`
- `minimax`
- `minimax-cn`
- `deepseek`
- `anthropic`

Example:

```bash
export GLM_API_KEY=...
epflemma provider --requested zai
```

### OpenAI-Compatible Remote Endpoints

RCP-style endpoints work through the `custom` path:

```bash
export OPENAI_BASE_URL="https://inference.rcp.epfl.ch/v1"
export OPENAI_API_KEY="..."
epflemma provider --requested custom
```

If GLM is down, the tested fallback model on that endpoint is:

```text
google/gemma-4-31B-it
```

Use the exact model name. The endpoint is case-sensitive.

Inside the interactive shell, `/provider` shows both the resolved provider and the supported target names so you can verify the route before launching a workflow.

### Local Runtimes

Select a local runtime:

```bash
epflemma models local use vllm google/gemma-4-31B-it
epflemma provider --requested local
```

Start a local runtime:

```bash
epflemma models local start vllm google/gemma-4-31B-it
epflemma models local status vllm
epflemma models local logs vllm
```

Other supported runtimes:

- `ollama`
- `llama_cpp`

## Workflow Tool Surfaces

There are now two important internal workflow surfaces:

- `epflemma-native`
  - default single-agent Lean workflow runtime
  - includes file, terminal, web, session search, skills, and file-lock coordination
  - does not include delegation
- `epflemma-native-swarm`
  - enabled only for user-approved `--agents N` workflows
  - adds delegation to the native Lean tool surface
  - intended for bounded multi-agent Lean runs with file ownership rules

## Configuration

Main config file:

```text
~/.epflemma/config.yaml
```

Main env file:

```text
~/.epflemma/.env
```

Top-level config shape:

```yaml
epflemma:
  project:
    template_source: ""
  workflow:
    managed_state_dir: ""
    autonomous_followups: 6

model:
  default: zai-org/GLM-5
  provider: zai
  base_url: "https://inference.rcp.epfl.ch/v1"
  api_key: ""

compression:
  enabled: true
  threshold: 0.5
  summary_model: zai-org/GLM-5
  summary_provider: zai
  reserved_output_tokens: 20000
  prune_tool_output: true
  prune_keep_recent_user_turns: 2

local_models:
  default_runtime: vllm
  active_runtime: ""
  active_model: ""
  runtimes:
    vllm:
      host: 127.0.0.1
      port: 8000
      extra_args: []
    ollama:
      host: 127.0.0.1
      port: 11434
      extra_args: []
    llama_cpp:
      host: 127.0.0.1
      port: 8080
      extra_args: []
```

Useful commands:

```bash
epflemma config get model.default
epflemma config set model.default '"zai-org/GLM-5"'
epflemma config set model.provider '"zai"'
epflemma config set model.base_url '"https://inference.rcp.epfl.ch/v1"'
```

Compression defaults are tuned for long Lean sessions:

- `reserved_output_tokens` keeps headroom for the next response instead of filling the full context window.
- `prune_tool_output` replaces stale old tool result bodies with a fixed marker.
- `prune_keep_recent_user_turns` keeps the newest user turns and their nearby tool output intact.

## Doctor

Run:

```bash
epflemma doctor
```

It checks:

- `git`
- `rg`
- `lake`
- current EPFLemma home and config
- active project discovery
- current provider resolution

## Packaging

Python package:

```text
epflemma-agent
```

Console scripts:

- `epflemma`
- `epflemma-agent`

## Verification Notes

Current verified behavior from this repo:

- focused EPFLemma test suite passes
- standalone install works with a separate EPFLemma home
- existing `gauss` remains independently resolvable
- workflow request resolution works against `.epflemma/project.yaml`
- RCP remote smoke succeeded with `google/gemma-4-31B-it`
- dead gateway/cron/voice/data-generation/website/community-skill directories have been removed from the repo tree

## Development

Create the repo venv and install editable deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e '.[dev]'
```

Run the focused EPFLemma tests:

```bash
source .venv/bin/activate
python -m pytest tests/epflemma -q -n 0
```

Recommended broader verification for the supported kernel:

```bash
source .venv/bin/activate
python -m pytest tests/epflemma tests/agent/test_prompt_builder.py tests/agent/test_context_compressor.py tests/test_run_agent.py tests/test_run_agent_codex_responses.py tests/test_windows_installer_links.py -q -n 0
```
