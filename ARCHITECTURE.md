# EPFLemma Architecture & Refactoring Map

This document tracks the module structure of EPFLemma and the in-progress decomposition of its
monoliths. It is the living companion to the refactoring program (see `TODO.md` "Refactoring
Plan" and the per-phase plan). Update it as modules move.

## Entry points (do not break)

- `epflemma` → `epflemma_cli.main:main` — the interactive shell / CLI.
- `epflemma-agent` → `epflemma_agent:main` — thin shim that seeds `EPFLEMMA_HOME` and calls
  `run_agent.main()`.

`native_runner.py` builds `run_agent.AIAgent` **in-process** (not via subprocess); the `epflemma`
shell spawns `epflemma workflow …` subprocesses for managed runs.

## Top-level layout

```
EPFLemma/
├── run_agent.py            # AIAgent conversation loop (monolith → Phase 4)
├── epflemma_agent.py       # epflemma-agent entry shim
├── model_tools.py          # tool registry API: get_tool_definitions / handle_function_call
├── toolsets.py             # named toolset definitions
├── utils.py                # atomic_json_write / atomic_yaml_write (+ shared helpers)
├── gauss_state.py          # SQLite session store (schema-versioned + migrations)
├── gauss_time.py           # timezone-aware timestamps
├── gauss_constants.py      # API endpoint constants
├── minisweagent_path.py    # mini-swe-agent submodule path discovery
├── agent/                  # prompt assembly, providers, compression, display, metadata
├── epflemma_cli/           # shell UX, workflow orchestration, providers, Lean services
└── tools/                  # agent tools (terminal, file, lean, web, mcp, delegate, …)
```

> The flat top-level modules and the legacy `gauss_*` names are intentionally **kept in place**
> for this refactor (conservative decision): no package move, no rename. The work is splitting the
> monoliths and de-duplicating, not relayout.

## Monoliths being decomposed

| File | Lines | Target |
|---|---|---|
| `epflemma_cli/native_runner.py` | 11,671 | Phase 2: leaves → `native_state` boundary → cluster modules → `proof_state_builder` last |
| `run_agent.py` (`AIAgent`) | 7,123 | Phase 4: TokenAccounter, ResponseNormalizer, ProviderRouter, ToolExecutor, ConversationManager, InterruptController, ApiCallOrchestrator |
| `epflemma_cli/lean_services.py` | 2,847 | Phase 5: lean_diagnostics / search_providers / automation / backends |
| `agent/auxiliary_client.py` | 1,626 | Phase 5: resolution / vision / call_llm behind ProviderRouter |
| `epflemma_cli/main.py` | 1,495 | Phase 3: shell / cli_handlers / shell_ui + one COMMAND_REGISTRY |

## Load-bearing invariants

- Public imports: `from run_agent import AIAgent`; `from model_tools import get_tool_definitions,
  handle_function_call, check_toolset_requirements`; `toolsets.*`; `utils.atomic_json_write`.
- `AIAgent.run_conversation()` result schema (pinned by `tests/test_run_conversation_schema.py`):
  `final_response, last_reasoning, messages, api_calls, usage, completed, exit_reason, partial,
  interrupted, response_previewed` (+ `interrupt_message` when interrupted, `error` on error).
- Tool self-registration: `tools/*` register at import via `tools/registry.py`; `model_tools`
  imports tool modules by **string name** — keep names or update the discovery list.
- Module-level imports used as **patch targets / dynamic-access points** (e.g.
  `tools.terminal_tool._interrupt_event`) are part of the public surface even when they look
  "unused" — see the F401 note below.

## The extraction recipe

1. Characterize first (tests pinning current behavior).
2. Move, don't rewrite — cut cohesive functions to a sibling module, fix imports only.
3. Re-export shim from the original module path; keep `__all__` accurate. Extracted modules must
   be leaves (no back-import into the monolith) — `run_agent.py` has ~46 lazy imports that make
   cycles easy to introduce.
4. Gate: `ruff check` + add the new module to mypy's `files` list.
5. Verify: targeted tests → full suite → `--help` smoke → GaussTest workflow for runner changes.
6. One behavior-preserving extraction per commit/PR.

## Tooling gates (Phase 0)

- **ruff** (`[tool.ruff.lint]`): `select = ["F", "I"]`, `ignore = ["F401", "F841"]`.
  - F401 (unused-import) and F841 (unused-variable) are **deferred to Phase 6**. F401 auto-removal
    is unsafe here because module-level imports are re-exported as patch/dynamic-access targets
    without `__all__`; blanket removal silently breaks runtime and tests. Phase 6 handles them
    per-file with `__all__` / `# noqa: F401`.
- **mypy** (`[tool.mypy]`): incremental gate. Only the modules listed in `files = [...]` are
  type-checked; the list grows as modules are extracted/cleaned. (Per-module overrides tune
  settings but do not select targets — the explicit `files` list does.)
- CI runs ruff → mypy → pytest (`.github/workflows/tests.yml`).

## Known pre-existing local test failures

On a developer machine with real provider credentials (`~/.codex` auth, `~/.epflemma` config),
several `tests/agent/test_auxiliary_client.py` provider-resolution cases and one
`test_run_agent.py` logging case fail because the resolver finds locally-available providers the
tests assume are absent. These pass in CI (empty API keys) and are unrelated to the refactor.

## Decomposition progress (branch refactor/epflemma-decomposition)

Behavior-preserving extractions completed so far (each: move verbatim → re-export shim from the
original module → ruff/mypy gate → full suite green → one commit). All extracted modules are leaf
modules (no back-import into their origin), keeping `origin._name` valid for callers and tests.

**From `native_runner.py` (11,671 → ~10,400 lines):**
- `native_config.py` — env/config readers (`_read_native_env`, `_managed_home`, `_project_root`, …)
- `lean_parsing.py` — pure Lean source/declaration text parsers (comment/string stripping, decl extraction)
- `native_state.py` — module-level mutable de-dup caches + `_cache_once`
- `queue_edit_guard.py` — declaration-edit protect/restore guards
- `formalization_document_runner.py` — `/formalize` workflow predicates + blueprint manifest parsers (25 fns)
- `manager_verification.py` — verification-record/outcome + timeout/retry helpers
- `native_utils.py` — shared leaf text/JSON/format helpers (`_single_line`, `_extract_json_payload`, …)
- `project_prove_manager.py` — file-level work-queue sizing/prioritization helpers (20 fns)

**From `lean_services.py` (2,847 lines):**
- `lean_diagnostics.py` — diagnostic/blocker/goal text parsers (incl. the backtracking-fixed `diagnostic_items`)

**From `main.py` (1,495 → 1,339 lines):**
- `cli_handlers.py` — argparse handler/formatter functions (`_handle_config/_sandbox/_models`, …)

**From `run_agent.py`:**
- `agent/log_formatting.py` — tool arg/result log formatters (Phase 1)
- `agent/command_safety.py` — destructive-command detection
- `agent/managed_run.py` — typed managed-run contract (Phase 1.5)

**Deferred (needs dependency-injection seams / method-surgery, not safe as one-shot moves):**
the tightly-coupled orchestration cores — native_runner's `_build_live_proof_state`, the autonomous
follow-up loop, `_run_managed_conversation`/`main`; the `AIAgent` method clusters (provider routing,
tool executor, conversation manager, interrupt controller); `main.py`'s `InteractiveShell` (its callees
are test-monkeypatched on `main`); and the provider/backend *abstractions* (ProviderRouter, Lean backend
interface) which are redesigns rather than moves. These are the next, more invasive refactoring steps.
