# LeanFlow Agent Guide

Instructions for coding agents working on this repository.

## Product Scope

LeanFlow is a Lean-first automation kernel.

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

## Quality gate — run before every commit

Every change must pass all four. CI (`.github/workflows/tests.yml`) enforces them on push/PR.

```bash
source .venv/bin/activate
black .                # format with black (https://github.com/psf/black); CI runs `black --check .`
ruff check .           # lint: pyflakes (F incl. F401 unused-import), import-sort (I), pyupgrade (UP)
mypy                   # type-check the gated module set (pyproject [tool.mypy] files=[...])
python -m pytest -q    # full suite
```

- Config lives in `pyproject.toml` (`[tool.black]` for formatting, `[tool.ruff.lint]` for linting,
  `[tool.mypy]` for types).
- **Black is the formatter** ([psf/black](https://github.com/psf/black)). Do not hand-format; let
  black do it. Ruff is used only for linting and import-sorting, not formatting.
- **Unused imports are not allowed.** `F401` is enforced; a genuinely dead import must be removed. The
  few deliberate re-exports / dynamic-access points (see the anti-pattern below) carry an explicit
  `# noqa: F401`. Never add an unused import without that comment.
- As you clean/extract a module, **add it to the mypy `files` list** so its types stay checked.
- The suite is green except **one known xdist flake**
  (`tests/tools/test_mcp_tool.py::...::test_existing_tool_names_reflect_registered_subset`): it passes in
  isolation (`-n0`) and only fails under full-parallel runs. Ignore *only* that one; investigate any other failure.

## Coding standards

**Write modular code.** One module = one responsibility. The layering is strict and one-directional:
`core/` (depends on nothing above it) → `agent/` and `tools/` → `leanflow_cli/`. Never import "up" the
stack (e.g. `core/` must not import `leanflow_cli`). When a file grows past a few hundred lines or starts
mixing concerns, **extract a cohesive leaf module** (a sibling in the same subpackage) instead of growing
it. `run_agent.py` and `leanflow_cli/native/native_runner.py` are legacy monoliths — put new logic in a
focused module and call it; do not pile onto them. Group new modules into the existing subpackages
(`agent/{accounting,execution,prompting,providers,compression,display,runtime}/`,
`tools/{implementations,utilities,mcp,environments}/`,
`leanflow_cli/{lean,native,formalization,workflows,cli,runtime}/`) and update `ARCHITECTURE.md` when you move/add modules.

**Document as you go.** Every module opens with a docstring stating its responsibility. Every non-trivial
function gets a concise docstring: a one-line imperative summary (`Return …`, `Build …`, `Drive …`) plus key
behavior, side effects, or the non-obvious "why". Match the terse, technical house style — no param-by-param
`:param:` walls, no filler ("Helper function."), no restating the name. Add inline comments for *why*, not
*what*, and only where the logic is non-obvious. Type-hint signatures.

**Write tests.** New behavior ships with tests. **Before refactoring coupled code, write characterization
tests that pin the current behavior first** (see `tests/test_golden_cores.py`, `tests/agent/test_managed_run.py`).
Keep the full suite green.

### Anti-patterns we already paid for — do NOT repeat

- **`F401` is enforced, but a few imports are deliberate re-exports / dynamic-access points** reached
  via module-attribute access ruff cannot see — `run_agent.OpenAI`, `…terminal_tool._interrupt_event`, and
  `run_agent`'s lazy-import surface (e.g. `run_agent._get_tool_emoji`). Those carry an explicit
  `# noqa: F401`. When you add a `# noqa: F401`, it must be a *genuine* re-export/patch target — confirm a
  real `module.NAME` / `from module import NAME` reference exists elsewhere. Do not use it to paper over a
  truly dead import; remove that instead.
- **`native_runner.py` is coupled to its tests by design.** `tests/leanflow/test_native_runner.py`
  monkeypatches internals via `setattr(runner, NAME, …)`. A function moved into a sibling module that calls
  those helpers by bare name binds to *its own* import and silently bypasses the patch. Do not extract from
  `native_runner` until those tests move off `setattr`-monkeypatching.
- **One home authority.** All home/state paths resolve through `core.home.leanflow_home()`
  (`$LEANFLOW_HOME` or `~/.leanflow`).
  Don't add per-module home resolvers.
- **The native-workflow env contract is `LEANFLOW_`-only.** `leanflow_cli/workflow.py` sets
  `LEANFLOW_NATIVE_*` / `LEANFLOW_PROJECT_ROOT`; `native_config` reads them.

## Documentation map

When you want to understand or change something, this is where it is written down — keep these in sync:

- **`README.md`** — product overview: what LeanFlow is, install, the core workflows.
- **`AGENTS.md`** (this file) — how to work in the repo: standards, the quality gate, layout, anti-patterns.
- **`ARCHITECTURE.md`** — the authoritative current module map, package boundaries, execution paths,
  and load-bearing invariants (public imports, the `run_conversation` result schema, and tool
  self-registration). **Update it whenever module ownership or the public surface changes.**
- **`CONTRIBUTING.md`** — contribution basics and the skill-vs-tool decision.
- **In-code docstrings** — the per-module / per-function reference (the primary source of truth for behavior).
- **`docs/`** — deeper operational references (`product-reference.md` and `sandbox-runtime.md`).

## Main Active Codepaths

```text
LeanFlow/
├── leanflow_cli/        # Shell UX, workflow orchestration, providers, local runtimes, locks, workflow state, Lean services
├── leanflow_skills/     # Curated Lean-first skills
├── agent/                # Prompt assembly, compression, display, auxiliary clients, AIAgent collaborators
├── tools/                # Lean-kernel tools
├── core/                 # Lowest layer: home authority (home.py) + session store (state.py) +
│                         #   clock/constants + model_tools/toolsets/utils kernel
├── run_agent.py          # Core conversation loop (AIAgent)
└── README.md             # Main product documentation
```

The shared kernel (session store, clock, constants, tool registry API, toolsets, helpers) lives under
`core/`. Top-level `model_tools` / `toolsets` / `utils` are thin re-export shims that keep
`from model_tools import …` etc. working.

The runtime is organized into single-responsibility leaf modules under
`agent/{accounting,compression,display,execution,prompting,providers,runtime}/`,
`leanflow_cli/{cli,formalization,lean,native,runtime,workflows}/`, and
`tools/{environments,implementations,mcp,utilities}/`. Entry points and compatibility shims remain
stable; see `ARCHITECTURE.md` for the current map and invariants. The key structures are:

- `agent/` holds `AIAgent` collaborators. For example, accounting is under
  `agent/accounting/`, context management under `agent/compression/`, tool execution under
  `agent/execution/`, prompt shaping under `agent/prompting/`, and provider routing under
  `agent/providers/`. `AIAgent` delegates through thin wrappers, properties, and lazy accessors in
  `agent/execution/collaborator_resolvers.py`, preserving public imports and patch targets.
- `leanflow_cli/` separates CLI handling, source formalization, Lean services, native process
  lifecycle, provider/sandbox runtime, and persistent workflow coordination into their named
  subpackages.
- `tools/` separates callable implementations from deterministic utilities, MCP transport, and
  execution environments.

When adding behavior, prefer the smaller extracted module over growing the original monolith again.

## Current Architecture

- `leanflow_cli/main.py` is the active `leanflow` CLI entrypoint (CLI handlers in `cli/cli_handlers.py`; slash-command routing in `cli/commands.py`)
- `leanflow_cli/native/native_runner.py` is the managed Lean workflow runtime (its leaf helpers live in sibling `leanflow_cli/native/` modules)
- `leanflow_cli/lean/lean_services.py` is the Lean services hub (diagnostics/declarations/search/automation/sorry-stats split into sibling `leanflow_cli/lean/lean_*` modules)
- `leanflow_cli/workflow.py` resolves workflow requests and toolset selection
- `leanflow_cli/workflows/workflow_state.py` persists activity, checkpoints, logs, and status (status shaping in `workflows/activity_preview.py`)
- `leanflow_cli/runtime/file_locks.py` handles cross-agent file reservations
- `leanflow_cli/runtime/skill_core.py` resolves builtin, user, and project skill overlays
- `agent/prompting/prompt_builder.py` injects skill guidance into the agent prompt
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
- the mini-swe-agent dependency and its docker/modal terminal backends (removed; the supported
  terminal backends are local, ssh, singularity, daytona, and the `leanflow sandbox` for isolation)

## Testing

Preferred verification commands:

```bash
source .venv/bin/activate
python -m pytest tests/leanflow -q -n 0
python -m pytest tests/leanflow tests/agent/test_prompt_builder.py tests/agent/test_context_compressor.py -q -n 0
python -m leanflow_cli.main --help
./scripts/install-internal.sh
```

When changing workflow UX or runner behavior, also smoke-test the installed wrapper:

```bash
~/.local/bin/leanflow --help
```
