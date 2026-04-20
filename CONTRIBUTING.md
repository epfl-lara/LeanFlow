# Contributing to EPFLemma

EPFLemma is a Lean-first automation kernel. Contributions should improve automated Lean proving, formalization, verification rigor, workflow visibility, or the shell UX around those jobs.

## Priorities

1. Fix bugs in `prove`, `autoprove`, `formalize`, and `autoformalize`.
2. Make the autonomous runner stricter and more reliable.
3. Improve shell status, workflow logs, checkpoints, and resume behavior.
4. Strengthen Lean-first skills and prompt guidance.
5. Keep the README and install surface accurate.

## Development Setup

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Useful checks:

```bash
python -m pytest tests/opengauss -q -n 0
python -m pytest tests/opengauss tests/agent/test_prompt_builder.py tests/agent/test_context_compressor.py -q -n 0
python -m opengauss_cli.main --help
./scripts/install.sh
```

## Main Codepaths

- `opengauss_cli/` for shell UX, workflow orchestration, provider routing, local runtimes, locks, and workflow state
- `opengauss_skills/` for the curated Lean-first skill core
- `agent/` for prompt assembly, compression, display, and shared agent internals
- `tools/` for the Lean-kernel tool surface
- `run_agent.py` for the core conversation loop
- `model_tools.py` and `toolsets.py` for tool discovery and Lean-kernel toolsets

Some lower-level support modules still keep `gauss_*` names internally. Treat those as compatibility residue, not as a supported Gauss product surface.

## Skill vs Tool

Default to a skill when the behavior can be expressed as Lean workflow guidance on top of the existing tool surface.

Add a tool only when the runtime needs deterministic behavior that prompt instructions cannot reliably enforce, such as:

- Lean-specific verification plumbing
- workflow state persistence
- provider/runtime lifecycle management
- file locking or other hard safety constraints

## What Not To Reintroduce

Do not reintroduce broad product surfaces that were intentionally cut from the kernel:

- gateway or messaging platforms
- ACP/editor server integration
- cron or scheduler product flows
- browser-automation product flows
- voice-first product features
- marketplace-style skill hubs
- success criteria that stop before Lean is actually clean
