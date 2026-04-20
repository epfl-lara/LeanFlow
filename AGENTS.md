# EPFLemma Agent Guide

Instructions for coding agents working on this repository.

## Product Scope

EPFLemma is a Lean-first automation kernel.

Primary goals:

- automated Lean proof repair via `prove` and `autoprove`
- mathematical formalization via `formalize` and `autoformalize`
- strict project-wide verification with no lingering `sorry`
- clear shell UX, workflow state, logs, checkpoints, and resumability

Do not optimize this repo for generic chat-assistant breadth. Prefer changes that improve Lean workflows directly.

## Development Environment

Always activate the repo venv before Python commands:

```bash
source .venv/bin/activate
```

## Main Active Codepaths

```text
ReallyOpenGauss/
├── opengauss_cli/        # Shell UX, workflow orchestration, providers, local runtimes, locks, workflow state
├── opengauss_skills/     # Curated Lean-first skills
├── agent/                # Prompt assembly, compression, display, auxiliary clients
├── tools/                # Lean-kernel tools
├── run_agent.py          # Core conversation loop
├── model_tools.py        # Tool discovery and dispatch
├── toolsets.py           # Lean-kernel toolset definitions
├── gauss_state.py        # SQLite session store used by history/session search
└── README.md             # Main product documentation
```

Some lower-level support modules still keep `gauss_*` names internally. Treat those as compatibility residue, not as a supported Gauss product surface.

## Current Architecture

- `opengauss_cli/main.py` is the active `opengauss` CLI entrypoint
- `opengauss_cli/native_runner.py` is the managed Lean workflow runtime
- `opengauss_cli/workflow.py` resolves workflow requests and toolset selection
- `opengauss_cli/workflow_state.py` persists activity, checkpoints, logs, and status
- `opengauss_cli/file_locks.py` handles cross-agent file reservations
- `opengauss_cli/skill_core.py` resolves builtin, user, and project skill overlays
- `agent/prompt_builder.py` injects skill guidance into the agent prompt

## Contribution Priorities

1. Fix Lean workflow correctness bugs.
2. Make autonomous workflows continue until the project is actually clean.
3. Improve status visibility, workflow logs, checkpoints, and resume behavior.
4. Improve Lean-focused skills and prompt guidance.
5. Keep install/docs/README aligned with the shipped product.

## Skill vs Tool

Default to a skill when the behavior can be expressed as Lean workflow guidance on top of the existing tool surface.

Add a tool only when deterministic runtime behavior is required, such as:

- verification plumbing
- file locking
- workflow state persistence
- provider/runtime lifecycle management

## What Not To Reintroduce

Do not reintroduce broad product surfaces that were intentionally removed:

- gateway or messaging platforms
- ACP/editor server integration
- cron or scheduler product flows
- browser or voice-first product flows
- data-generation/batch-runner subsystems
- marketplace-style skill hubs

## Testing

Preferred verification commands:

```bash
source .venv/bin/activate
python -m pytest tests/opengauss -q -n 0
python -m pytest tests/opengauss tests/agent/test_prompt_builder.py tests/agent/test_context_compressor.py -q -n 0
python -m opengauss_cli.main --help
./scripts/install.sh
```

When changing workflow UX or runner behavior, also smoke-test the installed wrapper:

```bash
/Users/$USER/.local/bin/opengauss --help
```
