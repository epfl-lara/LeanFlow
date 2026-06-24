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
EPFLemma/
├── epflemma_cli/        # Shell UX, workflow orchestration, providers, local runtimes, locks, workflow state, Lean services
├── epflemma_skills/     # Curated Lean-first skills
├── agent/                # Prompt assembly, compression, display, auxiliary clients, AIAgent collaborators
├── tools/                # Lean-kernel tools
├── core/                 # Lowest layer: home authority (home.py) + session store (state.py) +
│                         #   clock/constants + model_tools/toolsets/utils kernel
├── run_agent.py          # Core conversation loop (AIAgent)
└── README.md             # Main product documentation
```

The shared kernel (session store, clock, constants, tool registry API, toolsets, helpers) lives under
`core/`. Top-level `model_tools` / `toolsets` / `utils` are thin re-export shims that keep
`from model_tools import …` etc. working. The legacy `gauss_*` module names and `OPENGAUSS_`/`GAUSS_`
env/home prefixes were dropped entirely in Phase II — do not reintroduce them.

A completed decomposition (now on `refactor/epflemma-cores-2`) split the historical monoliths into
single-responsibility leaf modules and then grouped them into subpackages. The entry points and public
surface are unchanged — see `ARCHITECTURE.md` for the full module map and the subpackage layout
(`agent/{accounting,execution,prompting,providers,…}/`, `epflemma_cli/{lean,native,formalization,workflows,cli,runtime}/`,
`tools/{implementations,utilities,mcp,environments}/`). The leaf-module names below now live inside
those subpackages. The key structures to know:

- `agent/` holds the `AIAgent` **collaborators** extracted from `run_agent.py`: `token_accounting`,
  `provider_client`, `tool_executor`, `conversation_manager`, `interrupt_controller`,
  `response_normalizer`, `reasoning_processor`, `prompt_manager`, `api_caller`,
  `compression_policy`, and `anthropic_messages`. `AIAgent` delegates to these via thin wrappers,
  `@property` shims, and lazy `_resolve_*` accessors (now collected in
  `agent/collaborator_resolvers.py`), so existing imports and monkeypatch targets still resolve.
  Provider routing for the auxiliary client lives in `agent/auxiliary_adapters.py`, and model
  metadata + pricing are unified behind `agent/model_capabilities.py`.
- `epflemma_cli/` holds leaf modules carved out of `native_runner.py` (e.g. `native_config`,
  `lean_parsing`, `native_state`, `native_utils`, `native_checkpoints`, `proof_state_builder`,
  `manager_verification`, `project_prove_manager`, `lean_module_paths`), out of `lean_services.py`
  (`lean_diagnostics`, `lean_declarations`, `lean_search_providers`, `lean_automation`,
  `lean_attempt_helpers`, `lean_sorry_stats`, plus the `lean_backend` `LeanBackend` wrapper), out of
  `main.py` (`cli_handlers`, `shell_ui`; slash-command routing unified in `commands.py` behind
  `COMMAND_REGISTRY`), out of `formalization_documents.py` (`document_extraction`), and out of
  `workflow_state.py` (`activity_preview`).
- `tools/` gained `lean_experts` + `lean_patch` (from `lean_tool.py`) and `mcp_transport` +
  `mcp_sampling` (from `mcp_tool.py`).

When adding behavior, prefer the smaller extracted module over growing the original monolith again.

## Current Architecture

- `epflemma_cli/main.py` is the active `epflemma` CLI entrypoint (CLI handlers in `cli/cli_handlers.py`; slash-command routing in `cli/commands.py`)
- `epflemma_cli/native/native_runner.py` is the managed Lean workflow runtime (its leaf helpers live in sibling `epflemma_cli/native/` modules)
- `epflemma_cli/lean/lean_services.py` is the Lean services hub (diagnostics/declarations/search/automation/sorry-stats split into sibling `epflemma_cli/lean/lean_*` modules)
- `epflemma_cli/workflow.py` resolves workflow requests and toolset selection
- `epflemma_cli/workflows/workflow_state.py` persists activity, checkpoints, logs, and status (status shaping in `workflows/activity_preview.py`)
- `epflemma_cli/runtime/file_locks.py` handles cross-agent file reservations
- `epflemma_cli/runtime/skill_core.py` resolves builtin, user, and project skill overlays
- `agent/prompt_builder.py` injects skill guidance into the agent prompt
- `run_agent.py` hosts `AIAgent`; its responsibilities are delegated to the `agent/` collaborators listed above (the `run_conversation` loop itself is not yet extracted)

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
python -m pytest tests/epflemma -q -n 0
python -m pytest tests/epflemma tests/agent/test_prompt_builder.py tests/agent/test_context_compressor.py -q -n 0
python -m epflemma_cli.main --help
./scripts/install-internal.sh
```

When changing workflow UX or runner behavior, also smoke-test the installed wrapper:

```bash
/Users/$USER/.local/bin/epflemma --help
```
