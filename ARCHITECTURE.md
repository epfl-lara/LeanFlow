# LeanFlow Architecture & Refactoring Map

This document tracks the module structure of LeanFlow and the in-progress decomposition of its
monoliths. It is the living companion to the refactoring program (see `TODO.md` "Refactoring
Plan" and the per-phase plan). Update it as modules move.

## Entry points (do not break)

- `leanflow` → `leanflow_cli.main:main` — the interactive shell / CLI.
- `leanflow-agent` → `leanflow_agent:main` — thin shim that seeds `LEANFLOW_HOME` and calls
  `run_agent.main()`.

`native_runner.py` builds `run_agent.AIAgent` **in-process** (not via subprocess); the `leanflow`
shell spawns `leanflow workflow …` subprocesses for managed runs.

## Top-level layout (post Phase II)

```
LeanFlow/
├── run_agent.py            # AIAgent conversation loop (collaborators live under agent/)
├── leanflow_agent.py       # leanflow-agent entry shim (seeds LEANFLOW_HOME + runs the legacy seed)
├── core/                   # lowest layer (NO leanflow_cli deps): the home authority + shared kernel
│   ├── home.py             #   leanflow_home() + migrate_legacy_home() — single source of truth
│   ├── state.py time.py constants.py   # SQLite session store / clock / endpoint constants
│   └── model_tools.py toolsets.py utils.py minisweagent_path.py
├── agent/                  # AIAgent collaborators, grouped into cohesive subpackages:
│                           #   providers/ prompting/ compression/ execution/ display/ accounting/ runtime/
├── tools/                  # agent tools, grouped: implementations/ utilities/ mcp/ environments/
│                           #   (registry.py + response.py kept at the top for self-registration)
└── leanflow_cli/           # shell UX + workflow orchestration, grouped:
                            #   lean/ native/ formalization/ workflows/ cli/ runtime/
                            #   (main.py shell.py config.py workflow.py kept at the top — entrypoint + patch targets)
```

> **Phase II reversed the earlier "keep everything flat" stance.** The conservative first wave
> split the monoliths in place; Phase II then (a) grouped every extracted leaf into the subpackages
> above, (b) renamed the historical flat module names (e.g. the state store → `core.state`) and
> consolidated to the single `LEANFLOW_` env + `~/.leanflow` home contract, and (c) completed
> the managed-run contract. The detailed extraction history further down lists modules by their
> original *flat* names — they now live under the subpackages here (e.g. the `agent/` collaborators
> are under `agent/{accounting,execution,prompting,…}/`; the `native_runner.py`-era leaves under
> `leanflow_cli/native/`; the `lean_*` leaves under `leanflow_cli/lean/`).

### Phase II program (deep restructure → legacy drop → contract → quality → docs)

Behavior-preserving throughout; each step gated by `ruff`+`mypy`+full pytest and committed separately.

1. **Risky-now fixes** — locked concurrent workflow-state appends (`_locked_append`), closed two
   shell-injection vectors in `tools/file_operations.py`, narrowed a broad except.
2. **Golden/characterization net** — `tests/test_golden_cores.py` pins the appendix-per-turn,
   callback-ordering, result-schema, and interrupt invariants before the DI work.
3. **Deep restructure** — `core/` package introduced; `agent/`, `tools/`, `leanflow_cli/` each grouped
   into the subpackages above via collision-safe import rewrites (4 commits).
4. **Env/home consolidation** — `core.home` single home authority (`~/.leanflow`); all runtime/session
   vars consolidated to the single `LEANFLOW_` prefix; internal markers renamed. *Kept:* the
   `scripts/install.sh`→external Morph-template var contract.
5. **Managed-run contract** — `native_runner` now drives the post-tool-result appendix through
   `AIAgent.{stage,set,clear}_tool_result_appendix` instead of reaching into the private attribute.
   (The DI seams were already in place — 14+ injected collaborators; a `PostToolResultAppendixBroker`
   was evaluated and rejected because the raw attr is load-bearing for incompatible test semantics.)
6. **Quality** — safe ruff `B`/`SIM` autofixes + 67 `try/except: pass` → `contextlib.suppress`.

## Monoliths being decomposed

Line counts below are **current** (post-decomposition on `refactor/leanflow-cores`); the
"Target" column records what was carved off. The remaining bulk in each file is the coupled
core called out under "Deferred".

| File | Lines | Target |
|---|---|---|
| `leanflow_cli/native/native_runner.py` | 11,671 → 8,962 | Phase 2: leaves → `native_state` boundary → cluster modules → `proof_state_builder` / `verification_review` / `lean_module_paths` / `native_lean_files` / `queue_item_predicates`; managed-conversation/follow-up core deferred |
| `run_agent.py` (`AIAgent`) | 7,123 → 4,878 | Phase 4: 12 collaborators + `collaborator_resolvers`; Phase 4 module-level leaves `workflow_events` + `runtime_helpers`; `run_conversation` loop deferred |
| `leanflow_cli/lean/lean_services.py` | 2,847 → 1,987 | Phase 5: lean_diagnostics / declarations / search_providers / automation / attempt_helpers / sorry_stats / proof_context_local + `lean_backend` wrapper + `lean_models` (result dataclasses) + `lean_worker_dispatch`; full backend abstraction deferred |
| `tools/implementations/web_tools.py` | 1,670 → 1,309 | Phase 5: `web_research_providers` (arXiv/Semantic-Scholar/Crossref/Sourcegraph search + provider-ordering router + constants) split out |
| `agent/providers/auxiliary_client.py` | 1,626 → 1,286 | Phase 5: `auxiliary_adapters` (routing) + `model_capabilities` (metadata+pricing) + `auxiliary_rcp` (RCP predicates) + `auxiliary_nous` (Nous auth/endpoint) split out |
| `tools/mcp/mcp_tool.py` | 1,638 → 1,029 | Phase 5: `mcp_transport` (stdio/HTTP) + `mcp_sampling` (server-initiated LLM) + `mcp_schema` (schema/utility-schema/config-filter) + `mcp_config` (`_load_mcp_config`) split out |
| `leanflow_cli/workflows/workflow_state.py` | 1,325 → 1,068 | Phase 3: `activity_preview` (event/status shaping) + `workflow_state_paths` (path-root discovery) + `workflow_json_io` (read/write JSON) split out |
| `leanflow_cli/formalization/formalization_documents.py` | 1,512 → 536 | Phase 5: `document_extraction` + `formalization_markdown` + `formalization_models` + `formalization_tex_discovery` split out |
| `leanflow_cli/main.py` | 1,331 → 426 | Phase 3: `cli_handlers` + `shell_ui` + `shell` (`InteractiveShell` REPL) split out; main.py is now a thin argparse dispatcher |
| `tools/implementations/lean_tool.py` | 1,693 → 759 | Phase 5: `lean_experts` (advisor tools) + `lean_patch` (verified-patch apply) split out |

### Phase 6 hardening (code-cleaning / improvement; behavior-preserving)

- **mypy gate** grown to **71** modules (all extracted leaves that pass cleanly).
- **Silent-swallow logging**: 10 highest-value `except: pass` sites now log (debug/warning) without
  changing control flow — persistence loads, checkpoint-before-mutation, telemetry writes.
- **ruff `UP` modernization** applied tree-wide (~875 fixes: PEP585 `list/dict`, PEP604 `X | None`,
  `datetime.UTC`, OSError aliases) and **`UP` is now enforced** in the lint config (`select=[F,I,UP]`,
  ignoring the unsafe/manual `UP035`/`UP022`/`UP042`). Requires Python ≥3.11 (already pinned).
- **ruff `B904`** exception chaining (`raise … from exc`) at 7 sites for better tracebacks.
- Verified: full pytest suite green, `ruff`+`mypy` clean, plus codex + a 4-reviewer adversarial pass
  (0 HIGH/0 MED findings).

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
5. Verify: targeted tests → full suite → `--help` smoke → ProveDemo workflow for runner changes.
6. One behavior-preserving extraction per commit/PR.

## Tooling gates (Phase 0)

- **ruff** (`[tool.ruff.lint]`): `select = ["F", "I", "UP"]` (UP added in Phase 6), `ignore`
  retains `F401`/`F841` (re-export/patch-target safety) plus the unsafe `UP035`/`UP022`/`UP042`.
  - F401 (unused-import) and F841 (unused-variable) are **deferred to Phase 6**. F401 auto-removal
    is unsafe here because module-level imports are re-exported as patch/dynamic-access targets
    without `__all__`; blanket removal silently breaks runtime and tests. Phase 6 handles them
    per-file with `__all__` / `# noqa: F401`.
- **mypy** (`[tool.mypy]`): incremental gate. Only the modules listed in `files = [...]` are
  type-checked; the list grows as modules are extracted/cleaned. (Per-module overrides tune
  settings but do not select targets — the explicit `files` list does.)
- CI runs ruff → mypy → pytest (`.github/workflows/tests.yml`).

## Test-suite status

The full suite is green except **one pre-existing xdist flake**:
`tests/tools/test_mcp_tool.py::TestMCPSelectiveToolLoading::test_existing_tool_names_reflect_registered_subset`
fails only under full-parallel `-n auto` (parallel workers pollute the module-global tool
registry with `mcp_lean_lsp_*` entries); it passes in isolation and reproduces identically at the
branch base. Two earlier classes of local failure were FIXED on this branch: the
`tests/agent/test_auxiliary_client.py` failures (test-ordering pollution — a conftest autouse
fixture now snapshots/restores provider env between tests) and the `test_non_quiet_logging`
assertion (stale `1/180` fixture vs the default `max_iterations=200`).

## Decomposition progress (branches refactor/leanflow-deep → refactor/leanflow-cores)

> The first wave (`refactor/leanflow-deep`) was squashed and merged to `main`; the follow-on
> `refactor/leanflow-cores` branch continues with further leaf extractions (the `shell`, `lean_models`,
> `formalization_*`, `native_lean_files`, `queue_item_predicates` modules below).

Behavior-preserving extractions completed so far (each: move verbatim → re-export shim from the
original module → ruff/mypy gate → full suite green → one commit). All extracted modules are leaf
modules (no back-import into their origin), keeping `origin._name` valid for callers and tests.

### From `run_agent.py` (the `AIAgent` god class)

Phase 4 carved the `AIAgent` method clusters into single-responsibility **collaborators** under
`agent/`. `AIAgent` retains its public surface and now delegates: each collaborator is reached
through a lazy `_resolve_*(agent)` module-level accessor (which lazily constructs and caches the
collaborator if absent, so a bare-constructed or test-built agent still works), with thin method
wrappers and `@property` shims forwarding the old attribute/method names. This preserves the
patch/monkeypatch surface tests rely on while moving the logic out.

- `agent/token_accounting.py` — `TokenAccounter`: cumulative token/cost counters.
- `agent/provider_client.py` — `ProviderClientFactory`: provider/OpenAI client construction + credential refresh.
- `agent/tool_executor.py` — `ToolExecutor`: concurrent/sequential tool-call dispatch for a turn.
- `agent/conversation_manager.py` — `ConversationManager`: session/message persistence + per-turn API-message shaping.
- `agent/interrupt_controller.py` — `InterruptController`: `threading.Event`-backed interrupt state (requested flag, message, children).
- `agent/response_normalizer.py` — `ResponseNormalizer`: raw provider response → normalized assistant response.
- `agent/reasoning_processor.py` — `ReasoningProcessor`: thinking/reasoning-block (mostly pure) text helpers.
- `agent/prompt_manager.py` — `PromptManager`: per-session system-prompt build/cache/invalidate lifecycle.
- `agent/api_caller.py` — `ApiCaller`: mediation between the agent loop and the provider API call.
- `agent/compression_policy.py` — `CompressionPolicy`: when/how context compression fires.
- `agent/anthropic_messages.py` — `AnthropicMessagePreparer`: Anthropic message-preparation cluster.
- `agent/output_manager.py` — `OutputManager`: conversation-start / token-usage / session-usage logging.
- `agent/collaborator_resolvers.py` — the lazy `_resolve_X(agent)` accessors that materialize/cache each collaborator on first use (re-exported on `run_agent`).
- `agent/command_safety.py` — destructive-command detection (Phase 4).
- `agent/log_formatting.py` — pure tool arg/result log formatters (Phase 1).
- `agent/managed_run.py` — typed managed-run contract (Phase 1.5).

### From `native_runner.py`

- `native_config.py` — env/config readers (`_read_native_env`, `_managed_home`, `_project_root`, …).
- `lean_parsing.py` — pure Lean source/declaration text parsers (comment/string stripping, decl extraction).
- `native_state.py` — module-level mutable de-dup caches + `_cache_once`.
- `queue_edit_guard.py` — declaration-edit protect/restore guards.
- `formalization_document_runner.py` — `/formalize` workflow predicates + blueprint manifest parsers.
- `manager_verification.py` — verification-record/outcome + timeout/retry helpers.
- `native_utils.py` — shared leaf text/JSON/format helpers (`_single_line`, `_extract_json_payload`, …).
- `project_prove_manager.py` — file-level work-queue sizing/prioritization helpers.
- `proof_state_builder.py` — pure proof-state text/snapshot shaping helpers (safe subset).
- `lean_diagnostic_feedback.py` — pure diagnostic / goal text parsers (safe subset).
- `native_checkpoints.py` — workflow-state and checkpoint persistence helpers (safe subset).
- `verification_review.py` — verification-decision / advisory text parsers (safe subset).
- `lean_module_paths.py` — pure Lean module-name ↔ import-path ↔ on-disk-path translation helpers (safe subset).
- `formalization_generated_lean.py` — generated-Lean inspection helpers (safe subset).
- `native_lean_files.py` — active-file/target-symbol resolution + per-file/project `sorry` counting
  (imports the native_config / native_utils / lean_parsing leaves; no cycle).
- `queue_item_predicates.py` — pure queue-item classification predicates (sorry vs diagnostic
  blocker, current-item/status selection, attempted-proof shape).

### From `lean_services.py`

- `lean_diagnostics.py` — diagnostic/blocker/goal text parsers (incl. the backtracking-fixed `diagnostic_items`).
- `lean_declarations.py` — pure path-based Lean declaration indexing / lookup helpers, plus the
  token-cheap `declaration_outline` / `declaration_region` readers backing the `lean_outline` tool.
- `lean_lemma_suggest.py` — goal->candidate-lemma retriever: reads the assigned declaration's
  goal/hypotheses (via `lean_proof_context` / `lean_inspect`, resolved lazily off `lean_services`),
  derives targeted queries, runs `lean_search` across modes, and dedupes/ranks candidates. Backs
  the `lean_lemma_suggest` tool.
- `lean_search_providers.py` — stateless Lean search-provider helpers.
- `lean_automation.py` — pure Lean auto-prove normalization / parsing helpers.
- `lean_attempt_helpers.py` — pure multi-attempt / path / comment text helpers.
- `lean_sorry_stats.py` — pure `sorry`-counting helpers.
- `lean_proof_context_local.py` — pure local proof-context assembly helpers (safe subset).
- `lean_models.py` — the frozen Lean result/report dataclasses (`LeanCapabilityReport`,
  `LeanSorryFinding`, `LeanInspection`, `LeanVerificationResult`, `LeanSearchResult`,
  `LeanAxiomReport`, `WorkflowRouteDecision`, `LeanWorkerRequest`, `LeanWorkerResult`).
- `lean_backend.py` — `LeanBackend`, a thin façade forwarding to the LSP/MCP JSON tool invoker
  (`_invoke_json_tool`), the Lake/subprocess runner (`_run_command`), and a capability reader.
  A first, partial realization of the deferred backend abstraction: it wraps the existing
  primitives verbatim (resolving them lazily off `lean_services` for monkeypatch safety) without
  owning backend state — the full LSP/REPL/Lake interface redesign remains deferred.

### From `main.py`

- `cli_handlers.py` — argparse handler/formatter functions (`_handle_config/_sandbox/_models`, …).
- `shell_ui.py` — pure presentation helpers (prompt / bottom-toolbar formatters) that turn
  already-gathered shell state into display strings.
- `shell.py` — the full `InteractiveShell` REPL (prompt-toolkit loop, slash-command dispatch,
  workflow launch/monitor, status rendering), re-exported from `main` for the historical
  `from leanflow_cli.main import InteractiveShell` surface. main.py is now a thin argparse
  dispatcher (~425 lines). Tests driving shell methods patch collaborators on `leanflow_cli.shell`;
  tests driving `main()` patch them on `leanflow_cli.main` (both import the names independently).
- Shell slash-command routing is now unified in `commands.py` behind a single `COMMAND_REGISTRY`
  (`tuple[WorkflowCommandSpec, …]`), replacing the scattered per-command branches.

### From `mcp_bootstrap.py`

- `loogle_local.py` — project-toolchain-matched local Loogle: per-toolchain cache resolution
  (`loogle_cache_dir_for_project`), the idempotent build (`ensure_local_loogle_for_project` and its
  detached `_async` launcher, both under an exclusive `<cache>/.loogle-build.lock`), the fast
  no-build gate (`local_loogle_needs_build`), and the `patch_lean_lsp_loogle_build_lock` patch that
  makes lean-lsp-mcp take the same lock. It builds on the low-level primitives kept in
  `mcp_bootstrap` (`managed_loogle_cache_dir`, `local_loogle_supported`, `_read_lean_toolchain`,
  `_lean_lsp_env_from_home`); `mcp_bootstrap` reaches back only via lazy imports
  (`managed_mcp_power_status`, `bootstrap_lean_mcp`) to avoid a cycle. The lean-lsp server is pointed
  at the same per-toolchain dir by `tools/mcp/mcp_transport._augment_lean_stdio_env` — build, server,
  and status must agree (a test pins this). mypy-gated.

### From `formalization_documents.py`

- `document_extraction.py` — the text/LaTeX/PDF extraction layer: turns a resolved source file
  into a structured summary (theorem blocks, sections, references, extracted text). A closed
  set under "calls" that reaches no origin-mutable state, re-exported on `formalization_documents`.
- `formalization_markdown.py` — planner-context Markdown rendering (theorem blocks, sections,
  TeX-project discovery, source excerpt); imports only the `document_extraction` leaf.
- `formalization_models.py` — the `FormalizationDocumentError` exception + the
  `FormalizationDocumentContext` / `_FormalizationDocumentSelection` frozen dataclasses (shared
  data types, decoupled so the TeX-discovery leaf can use them without a cycle).
- `formalization_tex_discovery.py` — path resolution + TeX-project entrypoint/include/asset
  discovery (19 helpers + the `TEX_PROJECT_*` constants); imports only stdlib, `document_extraction`
  and `formalization_models`. formalization_documents.py is now a focused selection/context-prep module.

### From `queue_manager.py`

- `queue_models.py` — the `TheoremQueueManager` queue dataclasses + legacy dict<->typed mapping (verbatim move).
- `queue_manager_live.py` — Phase 0 of the /prove redesign: one live `TheoremQueueManager` per
  `autonomy_state` dict (fingerprint-guarded get-or-create keyed by `id()`), replacing per-helper
  reconstruction; the flush keeps writing the exact legacy `OWNED_AUTONOMY_KEYS` dict shape.
- `queue_decide_shadow.py` — Phase 0 P0.4 shadow-compare harness: under
  `LEANFLOW_QUEUE_DECIDE_SHADOW=1` the legacy verdict gates also evaluate the pure `decide()`
  policy on a throwaway hydration and log `queue-decide-shadow-mismatch` activity events on
  divergence; the legacy branches stay authoritative.
- `plan_state.py` — Phase 1 living plan-state substrate behind `LEANFLOW_PLAN_STATE`
  (default off): the dependency graph `blueprint.json` (frontier / OR-route / kernel-truth
  status rules), `summary.json`, the `plan.md` render with a preserved Notes tail, the
  append-only `journal.jsonl` lab notebook, the `reconcile()` anti-drift pass, decision-packet
  persistence, and the artifact prompt blocks the runner injects.

### From `workflow_state.py`

- `activity_preview.py` — pure activity/event-shaping helpers for managed-workflow status views.

### From `auxiliary_client.py`

- `agent/auxiliary_adapters.py` — OpenAI-client-compatible provider adapters for the auxiliary router.
- Model metadata (`agent/model_metadata.py`) + pricing are unified behind the
  `agent/model_capabilities.py` façade.

### From `tools/lean_tool.py`

- `tools/lean_experts.py` — auxiliary LLM-advisor Lean tools.
- `tools/lean_patch.py` — verified-patch application tool.

### From `tools/mcp_tool.py`

- `tools/mcp_transport.py` — stdio/HTTP transport plumbing for MCP servers.
- `tools/mcp_sampling.py` — `SamplingHandler`: the server-initiated `sampling/createMessage`
  callback (server asks the agent's LLM to complete a message) plus its numeric-coercion /
  audit-path helpers; re-exported on `mcp_tool`.

### Bug fixes landed alongside the moves

- **Test-pollution fix:** importing `run_agent` ran `load_leanflow_dotenv()` at import time before
  the autouse `_isolate_leanflow_home` fixture had set `LEANFLOW_HOME`, leaking the developer's real
  `.env` provider-resolution vars (`LEANFLOW_*`) into the session and
  breaking `tests/agent/test_auxiliary_client.py` whenever `test_run_agent` ran first. The fixture
  now strips those vars (`monkeypatch.delenv`, auto-restored) so resolution starts clean regardless
  of test order. No production behavior changed.
- **Workflow-termination fixes:** the headless stdin-exit guard now writes the pre-exit workflow
  checkpoint (`force_filesystem_checkpoint=True`) for autonomous workflows, restoring resumability
  on headless non-verified exits; the hard cycle-ceiling stop now labels its phase by
  `_live_state_is_verified` (verified vs stalled) instead of always "stalled"; and
  `mcp_bootstrap._write_bootstrap_document` now calls `invalidate_config_cache()` after writing the
  managed config directly (it bypasses `config.save_config`), fixing a stale `load_config()` cache
  that returned the pre-bootstrap config later in the same process.

### Deferred (needs dependency-injection seams / method-surgery, not safe as one-shot moves)

The still-coupled cores that resist behavior-preserving one-shot moves:

- `native_runner.py`: the autonomous follow-up loop and `_run_managed_conversation` / `main`.
- `AIAgent`: the `run_conversation` main loop and its retry/recovery orchestration.
- `main.py`: `InteractiveShell` (its callees are test-monkeypatched on `main`).
- `lean_services.py`: the full backend **abstraction** (the LSP/REPL/Lake interface), which is a
  redesign rather than a move. The `LeanBackend` wrapper (`lean_backend.py`) is a first partial
  step; routing all call sites through it is the remaining invasive work.

These are the next, more invasive refactoring steps.
