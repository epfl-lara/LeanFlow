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
│   └── model_tools.py toolsets.py utils.py runtime_modes.py provider_availability.py provider_capacity.py project_resource_admission.py minisweagent_path.py
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
- Managed delegate interruptions preserve bounded mathematical tool evidence through
  `tools/utilities/delegate_handoff.py`; terminal/file output is intentionally excluded from this
  handoff so a search-route boundary cannot leak unrelated workspace or credential data.
- Prover file tools route managed-transcript access through
  `tools/utilities/workflow_artifact_guard.py`: broad searches prune `.leanflow`, direct live
  `.log`/`.jsonl` reads and workflow-state searches are rejected. Raw `summary.json` and
  `blueprint.json` reads are also rejected because those machine snapshots can contain large
  historical ledgers and stale declaration bodies. Managed `plan.md` reads reuse an
  8,000-character generated-only transform that prioritizes the Goal/Current state/Strategy/
  Frontier prefix and the recent decision/final-report tail, with source hashes and explicit
  omission counts. The `## Notes` heading and its user-owned body stay hidden, and offset
  pagination that could enter that history is rejected. Only explicit
  `LEANFLOW_DIAGNOSTIC_FILE_ACCESS=1` operator mode bypasses these guards.
- Local terminal ownership is enforced by `tools/utilities/process_tree.py`: interrupt, timeout,
  and native-runner shutdown signal every process group in the spawned command's isolated POSIX
  session, then escalate and confirm detached descendants have disappeared. Selection uses PID
  relationships plus `getsid` session identity, revalidates stale PIDs before escalation, and never
  relies on cwd or command-text matching, so unrelated host processes are outside the cleanup boundary.
- Scratch research terminal authority is enforced by
  `tools/utilities/scratch_terminal_guard.py` before an execution environment is created. Only
  audited read-only diagnostics and pipelines are accepted; redirects, shell wrappers, command
  substitution, host/out-of-project read operands (including symlink escapes), process environment
  disclosure, mutating Git/find/Lean modes, and arbitrary executables fail closed.
- `tools/implementations/empirical_compute.py` and
  `tools/utilities/empirical_compute_runtime.py` provide the empirical dispatch lane's only Python
  computation surface. The tool is unavailable unless the current process owns a scratch-only
  empirical JobSpec; each call runs an AST-restricted integer/Fraction program in an isolated
  `-I -S` child with an ephemeral cwd, minimal environment, CPU/memory/output limits, and a hard
  parent timeout. The child has no filesystem, process, network, background, or PTY capability.
- Persisted workflow-agent cleanup is separately guarded by `core/process_identity.py`. Every
  spawned native runner receives a fresh opaque launch token; activity/live status retain only its
  hash plus the runner's process-group/session identity. Shell control and force-stop paths
  revalidate all three immediately before signaling, and legacy PID-only records fail closed. The
  runner claims live-status ownership in `starting`/`reconciling` phases before expensive resume
  work; retained proof fields remain marked as pending reconciliation until fresh Lean state lands.
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
- `agent/accounting/error_log.py` — runtime-home-aware, thread-safe optional error-log handler
  ownership and deduplication for `AIAgent` construction.
- `agent/provider_client.py` — `ProviderClientFactory`: provider/OpenAI client construction + credential refresh.
- `agent/tool_executor.py` — `ToolExecutor`: concurrent/sequential tool-call dispatch for a turn;
  scopes admission observers around composite tools so their actual inner Lean gates remain visible
  without leasing the whole MCP-backed call. For an admitted foreground call it also applies the
  optional policy from `agent/execution/admission_handoff.py` before releasing the main project slot,
  and retains a pending provider-to-tool priority lease across the complete foreground tool batch.
- `agent/execution/admission_handoff.py` — generic fail-open adapter from an agent-owned policy
  callback to the core admission's bounded post-tool foreground handoff request, plus identity-safe
  install/release handling for cancellable pre-admission leases.
- `agent/execution/tool_batch_priority.py` — deterministic priority/capacity gate for concurrent
  memory-heavy tools; exact target checks outrank diagnostic inspection while provider result order
  remains unchanged.
- `agent/conversation_manager.py` — `ConversationManager`: session/message persistence + per-turn API-message shaping.
- `agent/interrupt_controller.py` — `InterruptController`: `threading.Event`-backed interrupt state (requested flag, message, children).
- `agent/response_normalizer.py` — `ResponseNormalizer`: raw provider response → normalized assistant response.
- `agent/reasoning_processor.py` — `ReasoningProcessor`: thinking/reasoning-block (mostly pure) text helpers.
- `agent/prompt_manager.py` — `PromptManager`: per-session system-prompt build/cache/invalidate lifecycle.
- `agent/api_caller.py` — `ApiCaller`: mediation between the agent loop and the provider API call.
- `agent/compression_policy.py` — `CompressionPolicy`: when/how context compression fires.
- `agent/compression/summary_handoff.py` — provider-aware compaction summaries plus the bounded local recovery handoff.
- `agent/anthropic_messages.py` — `AnthropicMessagePreparer`: Anthropic message-preparation cluster.
- `agent/output_manager.py` — `OutputManager`: conversation-start / token-usage / session-usage logging.
- `agent/collaborator_resolvers.py` — the lazy `_resolve_X(agent)` accessors that materialize/cache each collaborator on first use (re-exported on `run_agent`).
- `agent/command_safety.py` — destructive-command detection (Phase 4).
- `agent/log_formatting.py` — pure tool arg/result log formatters (Phase 1).
- `agent/managed_run.py` — typed managed-run contract (Phase 1.5).

### From `native_runner.py`

- `native_config.py` — env/config readers (`_read_native_env`, `_managed_home`, `_project_root`, …).
- `lean_parsing.py` — pure Lean source/declaration text parsers (comment/string stripping, decl
  extraction, and the shared dependent-let-aware declaration-statement identity used by negation
  promotion and false-helper cleanup).
- `native_state.py` — module-level mutable de-dup caches + `_cache_once`.
- `queue_edit_guard.py` — declaration-edit protect/restore guards.
- `formalization_document_runner.py` — `/formalize` workflow predicates + blueprint manifest parsers.
- `manager_verification.py` — verification-record/outcome + timeout/retry helpers.
- `final_report_failure_reuse.py` — fail-closed, same-provider-turn cache for an unchanged
  exact-target rejection. It binds negative evidence to the queue assignment, raw source,
  declaration, and provider-turn identities; successful or replacement checks are never cached.
- `candidate_commit_priority.py` — fail-closed recognition of a fully clean exact-target temporary
  replacement or one exact-file, exact-assignment, single-declaration helper check, including a
  reserved `research_helper_candidate_priority.py` candidate and complete allowed-axiom evidence,
  before requesting the bounded foreground-to-commit admission handoff. It never verifies or
  commits a candidate itself.
- `banked_helper_inspection.py` — source-revision-bound reuse of the exact parent helper gate for an
  immediate redundant symbol-scoped `lean_inspect`; broader or stale inspections still run Lean.
- `parent_helper_verification_reuse.py` — fail-closed promotion of an accepted parent helper check
  after the model commits exactly the deterministic pre-anchor insertion image. It binds the
  candidate, declaration, target signature, pre-edit source, integrated whole-file revision, and
  allowed-axiom evidence; stale, reordered, reformatted, or bundled edits run the ordinary Lean gate.
- `verified_patch_batch_reuse.py` — authenticates a successful `apply_verified_patch` file check
  against the exact post-verification source revision, then combines that whole-file elaboration
  with one all-or-nothing multi-declaration axiom batch for the changed target and helpers. Stale,
  partial, placeholder-bearing, or wrong-scope evidence falls back to the ordinary per-declaration
  parent gates; a complete profile outside the current axiom policy remains an exact rejection.
- `scope_entry_admission.py` — one-shot bounded foreground priority across research scope entry,
  ordinary accepted source edits before their next parent gate, and selected parent helper
  rechecks. Research providers still run concurrently while the next foreground Lean admission
  takes priority.
- `helper_integration_admission.py` — continuously refreshes an overlapping, crash-bounded
  foreground marker while one exact parent-checked helper remains ready for integration. It does
  not hold the Lean gate or cancel admitted workers; it prevents new background admissions across
  provider/tool retries and releases on authoritative candidate retirement or runner shutdown.
  A zero lease disables the marker; positive sub-tick leases are clamped so refresh always precedes
  expiry with scheduler margin.
- `verification_batch_admission.py` — starts inside a successful `apply_verified_patch` project
  admission and continuously refreshes foreground priority until its complete parent helper/target
  verification and live queue refresh finish. Overlapping patch callbacks share one
  invocation-bound, reference-counted marker; the old one-shot lease is not left behind.
- `native_utils.py` — shared leaf text/JSON/format helpers (`_single_line`, `_extract_json_payload`, …).
- `project_prove_manager.py` — file-level work-queue sizing/prioritization helpers.
- `proof_state_builder.py` — pure proof-state text/snapshot shaping helpers (safe subset).
- `lean_diagnostic_feedback.py` — pure diagnostic / goal text parsers (safe subset).
- `native_checkpoints.py` — workflow-state and checkpoint persistence helpers (safe subset).
- `checkpoint_handoff.py` — the single structured checkpoint-status derivation used by metadata
  and deterministic handoff prose; signal-interrupted unresolved campaigns remain in progress even
  when their blocker evidence is concrete.
- `campaign_roots.py` — deterministic pre-provider requested-scope enumeration for authoritative
  negation promotion. It leases canonical explicit/project Lean sources in global order, expands
  only named open theorem/lemma declarations, materializes missing stated queue-sync graph nodes,
  and seals the immutable campaign registry (including an authenticated empty scope) before any
  native auxiliary or foreground provider can run.
- `runtime_cleanup.py` — process-owner shutdown for task-scoped terminal trees, provider clients,
  incremental Lean sessions, and MCP servers when the native runner exits; SIGHUP/SIGTERM are
  translated into catchable cleanup boundaries instead of bypassing Python `finally` blocks. A
  single idempotent finalizer orders owned-work reconciliation, lock release, terminal live-status
  persistence, one `runner-exit` event, and one process-outcome record across every return/signal
  path; terminal live status clears the runner PID and held-lock count before process exit. Its
  bounded foreground-drain primitive reinterrupts and joins the exact captured managed worker in
  short slices, preserves any replacement registration, and fails closed instead of checkpointing
  while a source writer remains live. Finalization runs the side-effectful owned-work stop pass once;
  only a sole typed foreground-writer failure is eligible for that bounded drain followed by one
  all-writer reconciliation, so descendant/portfolio/runtime shutdown is never replayed.
- `process_artifact_cleanup.py` — post-persistence finalization for the exact run-log owner token and
  current-PID foreground-admission markers. It removes only unlocked markers after owned work has
  quiesced, preserves concurrent runners, and reports any still-locked residual instead of hiding it.
- `route_execution.py` — typed, exact-scope evidence for mechanical orchestrator routes. A fresh-
  epoch or in-flight `decompose`, `plan`, or `negate` reservation completes only after durable
  route-specific evidence is recorded; deferred/no-op work remains replayable. A narrow migration
  can consume pre-contract reservations only from post-selection activity with matching target,
  file, and strong helper/planner/probe evidence.
- `terminal_authority.py` — acquires canonical requested-source leases in global order followed by
  the dependency-graph lease, and holds both across the final mathematical validation and durable
  outcome commit so cooperative writers cannot reopen an exit-0/exit-3 TOCTOU window.
- `parent_maintenance.py` — runs a blocking planner wave in a supervised worker while the native
  runner's process-owning thread continues its research-portfolio heartbeat. That heartbeat reaps
  and consumes completed background workers but reserves each freed actor slot until the pending
  planner lane has acquired capacity; normal portfolio refill resumes after planner synthesis.
- `verification_review.py` — verification-decision / advisory text parsers (safe subset).
- `lean_module_paths.py` — pure Lean module-name ↔ import-path ↔ on-disk-path translation helpers (safe subset).
- `formalization_generated_lean.py` — generated-Lean inspection helpers (safe subset).
- `native_lean_files.py` — active-file/target-symbol resolution + per-file/project `sorry` counting
  (imports the native_config / native_utils / lean_parsing leaves; no cycle).
- `source_only_startup.py` — stable source-revision capture and explicitly non-kernel startup
  snapshots for unresolved file-scoped proving. These snapshots may select a graph-eligible
  `sorry` item but can never certify completion; a pre-provider revision recheck falls back to the
  full Lean-backed builder on any source change.
- `source_placeholder_guard.py` — deterministic pre-tool rejection for an exact assigned-target
  incremental check when unchanged source still contains `sorry`/`admit`; replacement candidates
  and edited sorry-free declarations continue to the ordinary Lean gate.
- `queue_item_predicates.py` — pure queue-item classification predicates (sorry vs diagnostic
  blocker, current-item/status selection, attempted-proof shape). Current-item selection indexes
  the active Lean source once instead of reparsing it for every queue candidate.

### From `lean_services.py`

- `lean_diagnostics.py` — diagnostic/blocker/goal text parsers (incl. the backtracking-fixed `diagnostic_items`).
- `lean_axiom_batch.py` — exact source/import-revision keyed multi-declaration `#print axioms`
  harnesses and a short-lived process cache; the public inspection surface prefetches sibling theorem
  profiles in one Lean compilation, while the parent acceptance gate opts into a one-query exact-target
  harness because each proof edit changes the cache revision. Missing/partial evidence still fails over
  to the authoritative one-target harness.
- `lean_incremental_axioms.py` — one-query incremental axiom evidence. It appends a uniquely marked
  `#print axioms` command to the exact declaration chunk already sent through LeanProbe and accepts
  only one complete, ordered marker-delimited profile. The native parent gate applies its own axiom
  allowlist to that evidence; missing or malformed output falls back to the independent exact harness.
- `lean_ephemeral.py` — exact-project full-source validation in a bounded system-temp harness.
  It runs `lake env lean` outside the project tree, bounds disk-backed diagnostic output, reaps the
  complete process group on timeout, admits one Lean-heavy operation per project, reclaims the
  caller-owned incremental session before spawning, and distinguishes retryable project-environment
  failures from genuine Lean elaboration failures for negation-promotion recovery.
- `lean_helper_ephemeral.py` — exact helper validation without a resident LeanProbe. Dispatch workers
  always use it; foreground `check_helper` selects it when `include_axiom_profile=true`.
  It inserts named placeholder-free helpers into the exact source prefix before the assigned anchor,
  preserves the anchor preamble, appends its temporary-sorry skeleton, and accepts the advisory
  artifact only after one-shot Lake elaboration plus an allowlisted axiom-profile check. The parent
  still performs the authoritative recheck; target and feedback actions remain on LeanProbe.
- `lean_declarations.py` — pure path-based Lean declaration indexing / lookup helpers, plus the
  token-cheap `declaration_outline` / `declaration_region` readers backing the `lean_outline` tool.
- `lean_attempt_location.py` — safe line/column normalization for `lean_multi_attempt`, including
  stale post-proof blanks and inline `:= by` tactic bodies.
- `lean_services.lean_goals` — a goals-only public service used by managed queue rotation. A caller-
  supplied capability mapping, including an empty mapping, suppresses capability discovery; the
  service invokes only the goals backend and never repeats diagnostics or file/project `sorry` scans.
- `lean_incremental.py` — the LeanProbe-backed exact-check service. Foreground research runs and
  process-isolated dispatch workers raise a requested sub-300-second timeout to a 300-second
  cold-start floor, because reclaimed Lean services must rebuild their environment; larger requests
  are preserved and every successful early return remains immediate. An internal authoritative
  deadline ceiling may cap those floors for a parent-owned whole-request budget. Result telemetry
  records the requested/effective timeout, optional ceiling, and applied policy.
- `lean_command_timeout.py` — the canonical subprocess timeout policy. Ordinary commands retain the
  120-second default, while research-mode `lake env lean FILE` gates receive the same 300-second
  cold-start floor as incremental checks; `LEANFLOW_LEAN_COMMAND_TIMEOUT_S` remains a bounded expert
  override and cannot lower that research floor.
- `lean_lemma_suggest.py` — goal->candidate-lemma retriever: reads the assigned declaration's
  goal/hypotheses (via `lean_proof_context` / `lean_inspect`, resolved lazily off `lean_services`),
  derives targeted queries, runs `lean_search` across modes, and dedupes/ranks candidates. Backs
  the `lean_lemma_suggest` tool.
- `lean_search_providers.py` — stateless Lean search-provider helpers.
- `lean_search_horizon.py` — managed source-order projection for search results. It uses the current
  disk declaration index to move confirmed later same-file declarations out of the usable results
  list while preserving provider provenance; ambiguous, imported, and prior declarations fail open.
- `lean_automation.py` — pure Lean auto-prove normalization / parsing helpers.
- `lean_attempt_helpers.py` — pure multi-attempt / path / comment text helpers.
- `lean_sorry_stats.py` — pure `sorry`-counting helpers.
- `lean_proof_context_circuit.py` — durable campaign-scoped timeout memory for the managed
  proof-context backend; preserves the local declaration fast path across runner restarts.
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
  makes lean-lsp-mcp take the same lock. `patch_lean_lsp_loogle_lifecycle` also closes the managed
  Loogle subprocess after startup timeout and stdio-session teardown, preventing multi-gigabyte
  orphan search servers across campaign resumes. It builds on the low-level primitives kept in
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

- `queue_models.py` — the `TheoremQueueManager` queue dataclasses + legacy dict<->typed mapping.
  Route exhaustion records a non-terminal `deferred` outcome: queue precedence gives it a rank-2
  cooldown but never excludes it, the dependency graph remains `stated`, and strategy refreshes
  reopen both current deferrals and legacy checkpoint `blocked` outcomes as `unresolved`.
- `queue_manager_live.py` — Phase 0 of the /prove redesign: one live `TheoremQueueManager` per
  `autonomy_state` dict (fingerprint-guarded get-or-create keyed by `id()`), replacing per-helper
  reconstruction; the flush keeps writing the exact legacy `OWNED_AUTONOMY_KEYS` dict shape.
- `queue_decide_shadow.py` — Phase 0 P0.4 shadow-compare harness: under
  `LEANFLOW_QUEUE_DECIDE_SHADOW=1` the legacy verdict gates also evaluate the pure `decide()`
  policy on a throwaway hydration and log `queue-decide-shadow-mismatch` activity events on
  divergence; the legacy branches stay authoritative.
- `decomposer.py` — Phase 4 §4.2 mechanical decomposer: guards (stub shape, forbidden-axiom
  scan, anti-sorry-offloading), between-turn stub placement with in-place LeanProbe
  validation and all-or-nothing revert, dependency-graph split recording, and the prover
  guard-cache refresh. Each insertion first records pending exact source/helper/parent
  ownership through `decomposition_provenance.py`; only after the source validates and the
  complete split graph is durably saved does that provenance become committed. Startup
  reconstructs a missing graph from the pending exact payload or quarantines and pauses.
  Prover-created helpers are distinct: they enter as non-structural evidence under every route
  and become proof-support edges only when the current target proof body references their exact
  Lean identifier. The v3 resume migration applies that rule to every journal-proven
  `via=prover-edit` helper, removes false mechanism/progress credit, restores the durable route
  floor, and preserves managed decomposer placements as structural work.
- `decomposition_provenance.py` — crash-safe decomposer source-ownership ledger. It records
  pre-edit parent declarations plus exact source and helper-signature hashes before insertion,
  leases canonical path and inode identities across cooperating writers, reconciles interrupted
  source/graph writes, and fail-closed migrates older campaigns from successful decomposer
  activity plus verified pre-edit patch checkpoints (never from project Git). Pending and
  quarantined source transactions are retained and prevent provider startup until exact source
  truth makes recovery safe.
- `orchestrator.py` — Phase 4 §4.1 deterministic orchestrator floor (pure): `RouteContext`
  snapshot + the ordered route table that turns stalls/breakpoints/retry exhaustion into
  routes (`direct-prove`/`decompose`/`plan`/`negate`/`park`/`re-state`/`escalate`); the
  research runtime converts difficulty/route exhaustion away from `park` and into a fresh epoch.
- `orchestrator_llm.py` — Phase 4 §4.4 LLM routing layer
  (`LEANFLOW_ORCHESTRATOR_LLM_ENABLED`): prompt composition over `RouteContext` + the floor's
  proposal (research mode receives a target-scoped 12,000-character prompt over bounded history
  digests; the preserved `plan.md` Notes tail is excluded), a fence-tolerant strict-vocabulary
  decision parser (LLM vocabulary excludes `ask-human` — that is the runtime's own conversion),
  with `orchestrator_coverage.py` supplying current proved-graph context and a deterministic
  duplicate guard for exact, failed-signature, and covered affine-subfamily routes. Orchestrator
  graph context is assignment-scoped: campaign-global frontier nodes are labeled as scheduling
  inventory, same-file proved declarations carry an exact/different/unverified conclusion-shape
  classification, and route replies that cite non-compatible nodes as dependencies are rejected,
  `orchestrator_arithmetic_preflight.py` conservatively rejecting plainly false affine identities
  and affine divisibility claims with explicit modular counterevidence, plus exact ground
  counterexamples for simple Nat-quantified rational declarations, before they become route
  authority (unsupported mathematics fails open),
  and `llm_route()` with the upgrade-only rule — the LLM may refine the floor's route but a
  park/escalate answer against a non-terminal floor is rejected, and a protected floor
  (park/escalate/ask-human) is LLM-immutable: the consult is skipped outright. Every failure mode (flag
  off, provider down, unparseable) keeps the deterministic floor authoritative. Provider routing
  comes from `auxiliary.orchestration` (default: the strong main-agent model, no fallback
  inheritance).
- `orchestrator_prompt_budget.py` — research-orchestrator prompt shaping. It reserves the exact
  assigned declaration, priority error diagnostics, deterministic floor, and reply contract,
  then spends the remaining hard cap on explicitly bounded target graph facts, failed routes,
  findings, plan state, and phase policy. Every shortened or omitted history carries its complete
  SHA-256 plus original/included character and item counts in prompt and activity telemetry.
- `orchestrator_llm_circuit.py` — campaign-scoped shared advisory latency circuit for research
  mode. The research orchestrator and persistence coach contribute task-independent fingerprints
  for the same resolved provider/model failure, so one timeout or two identical connection/
  unavailable outcomes open a five-minute cooldown for every affected advisory task. A failed
  half-open probe doubles the delay up to thirty minutes; a successful affected-task probe clears
  it, and a new campaign starts clean. Skipped calls emit explicit activity while the deterministic
  route and coach fallbacks run immediately, so availability backoff never changes proof authority.
- `planner_phase.py` — Phase 5 §5.5 planner phase (dark behind `LEANFLOW_PLANNER_ENABLED`):
  the `plan` route's mechanical arm — ≤3 research lanes (web/mathlib/empirical) run in waves
  bounded by the shared research actor capacity, a `planner_synthesis` model turn merges
  the JSON deliverables, the graph delta lands through `plan_state.apply_delta` only, and
  target-file stubs are stated through `decomposer.place_helpers` (every Phase 4 guard
  applies). N1: every lane is recorded in the outcome + journal, parse failures included;
  any failure falls back to the prompt-level directive. `empirical_pilot.py` gives the empirical
  lane a small-case prompt contract plus a hard per-child terminal-call and timeout cap, so a
  planner turn cannot become an exhaustive foreground computation. Saturated lanes return a
  journaled `capacity-deferred` outcome after a bounded wait and are retried at the next safe
  orchestration boundary without constructing another agent. Interrupted/cancelled requested
  lanes defer synthesis, retaining completed lane evidence without admitting graph nodes. Before
  any synthesis merge, graph
  write, or stub placement, short standalone grounding/strategy/node-note assertions are passed
  through the conservative affine arithmetic preflight, while complete declarations use only the
  exact ground-rational countercheck. Historical examples, JSON, inventories, existential or
  hypothesis-bearing declarations, and conditional/residue prose fail open. A refuted assertion
  journals structured evidence and rejects the entire synthesis.
- `planner_candidate_admission.py` — deterministic admission policy for advisory planner output.
  It recognizes only explicit self-disqualification in node metadata (for example, `needs
  checking`, `placeholder`, or `if it fails`) and keeps those candidates out of graph/stub state;
  it also classifies cancelled lane outcomes that must defer synthesis. Draft statement bodies are
  deliberately excluded from this lexical check because their `sorry` placeholders are expected.
- `planner_arithmetic_reconciliation.py` — versioned resume/read-boundary migration for advisory
  plan state. Before persisted graph or prompt state can be reused, it applies the shared exact
  ground-rational countercheck only to complete planner declarations and the candidate-admission
  policy to planner/decomposer metadata. Directly refuted nodes are retired, while explicitly
  unchecked advisory nodes are conservatively demoted to conjectures with their dependency edges
  retained so quarantine cannot accidentally unblock a downstream node.
  Exact summary references are scrubbed and `plan.md` regenerated without touching Lean source.
  Ordinary notes, empirical prose, valid siblings, and `proved`/`false` gate-backed nodes are
  preserved.
- `plan_state.py` — Phase 1 living plan-state substrate behind `LEANFLOW_PLAN_STATE`
  (default off): the dependency graph `blueprint.json` (frontier / OR-route / kernel-truth
  status rules), `summary.json`, the `plan.md` render with a preserved Notes tail, the
  append-only `journal.jsonl` lab notebook, the `reconcile()` anti-drift pass, decision-packet
  persistence, and the artifact prompt blocks the runner injects. Generated Strategy/Decision
  sections incorporate the campaign's current route plus a bounded journal-tail history; resume
  prompts surface the durable queue assignment and explicitly rank current Lean source/kernel
  state above stored statement snapshots and historical Notes. Its bounded generated-plan view is
  shared with the model-facing file-tool guard, so a direct plan read cannot re-ingest the Notes
  tail or paginate into it. Oversized generated views use an 8,000-character current-state-first
  projection with source hash/count telemetry; raw machine-snapshot reads are likewise blocked.
  Phase 5 adds `apply_delta`
  (the planner's ONLY door into the graph: nodes enter conjectured/stated only, existing
  statuses/statements immutable, edges validated + deduped, pure w.r.t. persistence) and
  `merge_planner_findings` (capped deduped `grounding_findings`/`strategy_notes` prose keys;
  `## Strategy` joins the plan.md render).
- `verified_transition_reconciliation.py` — pure identity fence for immediate graph sync at a
  solved theorem boundary. It requires the exact accepted outcome, completed assignment, and live
  next assignment to agree before the runner promotes the completed node, refreshes its source
  snapshot, clears proving ownership, and installs the next proving node ahead of any potentially
  slow incremental warmup; the ordinary post-warmup sync remains idempotent.
- `planner_graph_identity.py` — proof-insensitive exact declaration signatures for planner graph
  admission. A reused textual `(name, file)` identity inherits graph truth only when its normalized
  declaration signature matches; conflicting or unauthenticated proved identities fail closed and
  cannot receive dependency edges or enter stub placement.
- `advisor_route_facts.py` + `target_handoff.py` + `queued_helper_handoff.py` — exact-assignment knowledge continuity for
  fresh prover and decomposer contexts. Direct `lean_reasoning_help` results contribute only
  bounded negative route facts, fail closed on malformed/truncated or mismatched payloads, and
  are hidden after a declaration-signature change. The renderer joins those unverified exclusions
  with direct kernel-proved graph neighbors and consumed exact-target research findings. Findings
  remain in durable consumption order so a later parent-recheckable finite witness can correct an
  earlier method obstruction; neither one is promoted into a parametric target verdict. Evidence
  edges are labeled already-banked, evidence-only facts and cannot be rediscovered as graph progress.
  An unchanged decomposer-created child may additionally inherit the exact worker-checked helper
  declaration from its originating parent finding only when committed source provenance, the parent
  assignment revision, the current child stub/signature, and the candidate hash all agree. That
  declaration remains a hint: the foreground prover must recheck it as the current target replacement
  before editing, and the ordinary manager/kernel gate remains authoritative. A uniquely identified
  worker declaration with the same signature may be adapted by changing only its declaration-head
  name before that recheck. Newly placed zero-attempt children receive one concrete foreground proof
  turn before generic fresh-epoch negation, search, or further decomposition.
  `native_runner` injects this block at startup, continuation, direct decomposer preflight, and the
  mechanical decomposer route; `lean_decompose_helpers` batches execute sequentially so preceding
  source discovery in the same model turn completes first.
- `struggle_signals.py` + `manager_nudge.py` — Phase 2: the pure struggle-signal classifier and
  the message-only persistence coach. Every rejected prover turn gets a usable coach message;
  the model call uses off/dark/live modes, while disabled/unavailable/unusable output receives a
  deterministic fallback. Its isolated model request defaults to five seconds and is hard-capped
  at ten seconds before falling back. Model text may acknowledge effort and the rejection as useful
  evidence, but any Lean/kernel/source-status assertion, proof-progress claim, or route selection is
  rejected. Kernel-verified helper names are appended only by deterministic parent code; zero-helper
  turns cannot describe an unchanged `sorry` as compiled progress. `summary.json.manager_nudges`
  and `campaign_metrics` audit coverage.
- `prover_jobs.py` — Phase 5 §5.7 shape-A prover jobs: the dispatch spawn backend. A stub
  file is discharged by a nested file-scoped `/prove` subprocess (`spawn_workflow`) with a
  hygienic child env (fresh run id, `LEANFLOW_WORKFLOW_PARENT_RUN_ID` lineage edge,
  `LEANFLOW_DISPATCH_JOB_ID`/`LEANFLOW_JOB_LINEAGE`, budget as `AGENT_MAX_TURNS`, blanked
  runner-owner + `LEANFLOW_FORMALIZATION_*`), a `dispatch:{job_id}` stub-file lock for the
  child's lifetime, a synchronous wall-clock wait with SIGINT→terminate→kill escalation, and
  the PARENT's own kernel gate (`decl_verdicts`: present + sorry-free + zero-error
  `lean_incremental_check`) as the only source of `proved` on the graph.
- `leanflow_specs/phases/` — Phase 6 §6.9 phase fragments (`kind: phase`): the shared
  search/draft/review/negation/planning contracts, embedded into consumer prompts via
  `lean_workflow_specs.phase_fragment_text` (schema included where the fragment IS the reply
  contract, body-only POLICY where it is not). Wired consumers: the planner (lanes +
  synthesis) and the orchestrator-LLM routing turn consume fragments directly by id, and the
  prover/formalization skill prompts embed them through the `phases` field on the
  `prove`/`search`/`draft`/`review` specs (`build_skill_prompt` dedupes so a fragment shared
  by two specs appears once). Each fragment declares
  `consumed_by` (validated against `KNOWN_PHASE_CONSUMERS`) and a machine-readable
  `deliverable_schema` (validated YAML mapping); fragment ids are `phase-`-prefixed and the
  loader now refuses duplicate spec ids loudly instead of letting a later file shadow an
  earlier one. Fragments carry no `skills:` of their own — they reach a skill prompt only when
  a consuming workflow's `phases` field pulls them in, never as standalone specs.
- `research_mode.py` — the complete research profile selected by `--research`,
  `--research-workers N`, or `LEANFLOW_RESEARCH_MODE=1`. It enables the plan/retrieval/
  orchestration/fidelity/frontier/planner/dispatch/negation/report/learnings/coaching stack and
  makes 120 cycles a per-epoch context boundary rather than a campaign stop. It keeps foreground
  `lean-lsp` but suppresses its private local Loogle index unless
  `LEANFLOW_RESEARCH_LOCAL_LOOGLE=1`; ordinary prove workflows retain local Loogle.
- `core/provider_capacity.py` — dependency-light cross-process actor leases shared by dispatch
  workers and planner delegates. Dispatch acquires before `_build_agent`; delegation acquires
  before child construction. Context propagation lets nested provider/auxiliary helpers retain
  the same slot, while foreground prover and control-plane calls remain outside the background cap.
- `core/provider_availability.py` — dependency-light parsing and normalization of provider-owned
  usage-limit reset windows. It prefers structured error bodies, bounds hostile timing values, and
  carries one absolute reset authority across foreground, delegate, and dispatch process boundaries.
- `core/project_resource_admission.py` — project-scoped cross-process admission for Lean-heavy
  subprocesses, independent from provider/research-agent capacity. It canonicalizes nested paths
  to one Lean root and retains a sticky process-lifetime lease when owned LeanProbe cleanup fails.
  Crash-released waiter markers give a waiting top-level runner priority over dispatch-worker
  reacquisition without weakening the single-slot flock or reclaiming a live owner. On foreground
  release, the same marker remains as an unlocked, bounded handoff lease so immediate parent-side
  queue finalization can reacquire before an already-queued dispatch worker; stale/crashed markers
  fail open when that deadline expires. A re-entrant verification transaction retains the foreground
  lease across sequential exact-declaration and transitive axiom gates so a background worker cannot
  enter between authoritative stages. A context-local observer reports real inner gate lifecycles to
  higher layers without importing workflow/activity code into `core/` or affecting admission authority.
  Its cancellable pre-admission lease publishes foreground intent before provider inference without
  holding the main Lean slot; process exit or the hard deadline remains the crash-safe release path.
- `campaign_epoch.py` — durable campaign identity and epoch rollover. It checkpoints boundaries,
  resets only local route/stability/context state, preserves verified and negative evidence, emits
  `campaign-epoch-ended`/`campaign-epoch-started`, records truthful process outcomes, and owns the
  campaign-persistent `verified_mechanisms` ledger used for route-streak accounting. Each rollover
  persists the spent route portfolio and requires a distinct non-direct strategy to start before
  ordinary direct proving can resume. The campaign-wide `no_progress_semantic_routes` ledger also
  records each unique route's exact assignment, strategy family, target hypothesis, and proof-shape
  identity. Only kernel-gated graph progress clears it: renamed reasons, new counters, and fresh job
  hashes cannot buy another equivalent turn. Exhausting every viable identity requests an epoch and
  worker-portfolio refresh through the non-terminal `refresh-portfolio` action instead of parking.
  That internal action uses the crash-durable in-flight marker rather than the fresh executable-route
  token; it retires immediately after checkpointing the rollover request and never waits for a model
  turn.
  One epoch token binds that route obligation to the fresh
  context and keeps it pending until observable managed work completes, so stale routes and provider
  failures cannot consume it. Mechanical `decompose`, `plan`, and `negate` selections complete at
  their exact application boundary only after typed durable evidence; a later provider return
  cannot launder deferred/no-op/capacity-blocked work. The selected route reservation is durable with that token, epoch,
  target, and file: an infrastructure restart rehydrates and reapplies it without another LLM/floor
  selection, route-history entry, or no-progress-streak charge. The exact durable fresh selection
  also outranks an ordinary event-triggered consultation without relying on a process-local replay
  token, preventing a capacity-deferred route from minting a second in-flight route after restart.
  A newer exact-scope route explicitly requested by the completed prover turn is the narrow
  exception: it retires a conflicting in-flight replay and runs ahead of a conflicting fresh-epoch
  selection. The fresh-epoch obligation remains pending, so a checked helper produced by the
  requested route owns the following foreground boundary and the older portfolio route remains
  available afterward. Kernel-authenticated counterexample evidence and authoritative disproof
  still outrank this strategy ordering.
  Prompt-level routes complete after
  their exact managed turn; later stall/event decisions remain fresh charged routes. A separate versioned
  `planner_capacity_reservation` row preserves a capacity-deferred `plan` route across process
  restart. It is exact to campaign, epoch, target, and file; portfolio maintenance remains
  harvest-only until the planner runs, and target/epoch/route completion clears it. An in-flight
  refill that loses the reservation race rolls back only the replacement jobs launched by that
  transaction, preserving older research and completed findings. The same atomic rollover
  records a worker-refresh obligation;
  `research_portfolio.py` replays it after a crash, harvests completed results, retires only
  pre-refresh workers, and clears the token before refilling distinct routes.
  A versioned legacy reconciliation repairs checkpoints written before route reservations were
  durable only when activity proves an exact same-assignment scope-entry replay after an exit-2
  provider failure, the replay precedes route start, and no graph-progress reset makes subtraction
  ambiguous. It leaves managed-cycle accounting and legitimate event/stall repetitions untouched.
  It also reserves a campaign-wide monotonic provider-turn nonce before each foreground prover
  request; the same summary transaction validates the immutable requested-root gate, so a fresh
  authoritative campaign cannot race an unsealed scope into a provider call. Failed-attempt
  identity combines that nonce with campaign, epoch, and cycle so resume and rollover cannot merge
  genuine rejections. Marker-absent legacy and ordinary non-negation campaigns remain resumable
  without acquiring terminal-disproof authority retroactively.
- `mechanism_progress.py` — derives proof-strategy provenance for newly verified graph helpers.
  Exact local declaration references are the primary mechanism identity; a normalized proof-body
  signature is the fallback for direct certificates. Signatures are scoped by explicit graph parent,
  historical proved siblings seed the campaign ledger on demand, and repeated mechanisms never
  delete or downgrade their kernel-proved graph nodes. Parent closure and an explicit completed
  `split` decomposition remain unconditional graph progress. `campaign_epoch.py` version-stamps
  this accounting policy and repairs a legacy repeated-mechanism reset on resume so an already-due
  epoch rollover cannot disappear across an upgrade.
- `conditional_helper_progress.py` — keeps kernel-valid conditional bridge helpers as proved graph
  facts while deferring campaign-progress credit for new quantified or theorem-valued premises that
  are neither explicit graph obligations nor used by the assigned target. A helper premise that
  contains the unresolved target result is circular even when that result is an opaque proposition.
  Exact target integration or graph representation releases the helper through ordinary mechanism accounting. A bounded
  campaign reconciliation removes historical deferred nodes from the mechanism ledger and repairs
  the current epoch's most-recent false route-streak reset without editing Lean source.
- `finite_branch_progress.py` — gives singleton and one-congruence helpers one shared source-backed
  family identity. Before a prover edit reaches graph mutation or helper verification, a narrow
  queue guard preserves the first closed base case but rolls back later unintegrated singleton
  additions and publishes a deduplicated orchestrator reroute event. Repairs, newly closed targets,
  exact target integration, and explicit uniform or exhaustive graph bridges remain eligible for
  their ordinary kernel gates. Once four distinct branches exist beneath the same still-open graph parent,
  further isolated branches remain kernel-proved, actionable evidence but cannot reset graph,
  queue, mechanism, or route progress. Closed target-prefixed base cases such as
  `erdos_242_at_twenty_one` are contained narrowly without classifying ordinary closed lemmas.
  Resume reconciliation replays gate-backed promotion order, removes every historical saturated
  branch from progress accounting, and reconstructs the route streak from durable route/reset
  history while preserving a genuine latest progress anchor. Persisted anchor/ledger references
  remain eligible for cleanup after an epoch rollover, but cannot add route debt to the new epoch.
- `environment_memory.py` — campaign-scoped deterministic environment evidence. It records exact
  missing-Python-module signatures from terminal results, restores them across runner restarts and
  fresh epochs, injects them into prover handoffs, and blocks unchanged import retries.
- `helper_gate_retry.py` — bounded retry policy for prover-created, sorry-free helpers whose exact
  or transitive axiom gate is temporarily unavailable. Durable outcomes survive resume; source-scoped
  attempt reservations stay process-local so live-state refreshes cannot spin on failed infrastructure.
- `helper_integration_pending.py` — bounded, assignment-scoped continuity for graph helpers promoted
  from evidence to proof support before the committed target gate accepts. It persists at most one
  small helper set, counts subsequent exact-gate retries, and retires stale assignment records; the
  native gate independently rechecks exact helper references before granting campaign progress.
- `verification_candidate_replay.py` — bounded foreground exact-candidate continuity. It retains one
  placeholder-free replacement per exact declaration only after the kernel passed and candidate-bound
  axiom evidence was unavailable, rechecks it once per verifier process-launch fingerprint and
  contract (including a previously ready candidate after restart), and exposes it for commit only
  after the current kernel and axiom allowlist both pass. It never edits source or grants authoritative
  theorem status; malformed non-list checkpoint state is discarded fail-closed.
- `verification_transaction.py` — workflow-layer parent verification scope. It wraps one helper or
  target's exact elaboration plus axiom inspection in the core re-entrant foreground transaction.
  The incremental path combines both into one LeanProbe declaration request; low-memory mode and
  incomplete inline evidence retain the independent exact axiom-harness fallback. Multi-helper batches
  open one transaction per helper rather than monopolizing the slot.
  The public `lean_incremental_check` wrapper forwards `include_axiom_profile`; managed exact
  replacements set it automatically so the temporary candidate and its axiom evidence cannot diverge.
- `resume_graph_reconciliation.py` — resume-only eligibility and evidence policy for graph declarations
  whose historical gate outcome was lost. Sorry-free source is only a candidate filter; promotion still
  requires a fresh exact-target incremental check and an explicit blocker-free transitive axiom profile.
  `apply_verified_patch` therefore reports a successful broad check as
  `status=patch_elaborated`, `patch_elaborated=true`, and
  `target_verified=false`; only the later queue gate may set theorem truth.
  Declaration reconciliation also refreshes each graph node's exact on-disk
  declaration text and `source_sha256`, so a proved node cannot keep a stale
  `by sorry` planning snapshot after its gate-backed source revision changes.
- `resume_projection_reconciliation.py` — bounded provider-free repair before an active usage-limit
  pause returns. It reclassifies persisted conditional-helper progress against current graph/source
  state, regenerates assignment-scoped plan strategy/frontier views, and promotes a matching broad
  patch status only after a later exact target outcome or current-source gate-owned proved graph
  node. It performs
  no Lean invocation, `DispatchService` construction, cold dispatch-archive hydration, provider
  call, or workflow-activity scan; an already-current conditional policy is summary-write-free.
- `resume_gate_rejection_cache.py` — bounded summary-owned negative cache for completed resume-time
  axiom-policy rejections. Reuse requires the exact canonical file, target, full source digest, import
  environment, verifier contract, and enabled allowlist policy; source races, unavailable profiles,
  operational failures, and mismatches are never retained. Cache hits remain rejection evidence only
  and cannot promote graph truth.
- `research_route_context.py` — assignment-scoped history handoff for isolated research workers. It
  reads at most a 512 KiB journal tail, combines recent foreground routes, rejected proof shapes, and
  prior worker outcomes into a 10 KiB structured window, and appends that window after the stable
  route-defining objective. The same explicit context is retained in structured deliverables while
  parent-only metadata is excluded from novelty evidence and replacement-route signatures.
  Context v3 also carries a code-free, completion-ordered digest of consumed exact-target facts:
  progress-ineligible finite witnesses and method obstructions remain visible for deduplication,
  repeated certificates coalesce by mathematical values, and prompt trimming preserves the newest
  obstruction/witness correction pair before generic research evidence.
  Active sibling jobs remain visible as route-coordination records, but only terminal ledger rows
  expose or classify result evidence.
  Semantic novelty uses checked helper source before report-level witnesses, so tactic variation,
  supporting survivor metadata, and changing moduli cannot evade the saturated finite-branch family.
- `research_obstruction_dominance.py` — fail-closed portfolio policy for a parent-kernel-proved,
  exact-target universal obstruction. It requires the existing same-file graph evidence authority and
  an exact syntax-preserving pointwise or closed negation relation; finite instances, method
  obstructions, unproved helpers, and unrelated negative facts do not qualify. Once qualified, the
  policy suppresses dominated empirical finite-instance objectives while leaving authoritative
  negation promotion and alternate proof-shape research live.
- `research_semantic_identity.py` — fail-closed canonicalization for model-authored research proof
  shapes. It preserves mathematical constants while removing job IDs, route hashes, timestamps, and
  generation counters so provenance-only refresh churn cannot masquerade as mathematical progress.
  The same leaf derives foreground route identities from strategy family, exact assignment, coarse
  mathematical mechanism, concrete target hypothesis, and research proof shapes for deterministic
  pre-turn admission.
- `research_portfolio.py` — parent-side portfolio driver. It launches a scope-entry deep-search
  process, adds the default empirical lane after two rejected attempts, then rotates a saturated
  empirical lane through negation and the dedicated decomposition archetype. Decomposition workers
  return normalized source-backed subgoal/dependency proposals only; the parent remains the sole
  plan/graph writer. The driver consumes each structured finding once and refills terminal slots
  with assignment-distinct objectives. When a primary lane semantically cools down, all remaining
  archetypes become replacement candidates even before attempt-gated capacity expands; the original
  slot count is retained. Each open lane reserves an archetype-independent
  mathematical-delta signature; fallback deep-search and empirical lanes explicitly separate
  parametric/library work from the next uncovered finite pattern. A proved exact universal obstruction
  retires any still-open dominated finite-instance worker and rotates capacity to negation promotion,
  decomposition, then deep-search lanes, so further samples cannot displace promotion/replanning or a
  materially different proof shape. The managed-conversation supervisor uses its
  main-thread heartbeat to reap and refill while the foreground tool thread is blocked; serialized
  maintenance prevents that heartbeat and post-tool callbacks from double-consuming a job.
  A pending planner route switches maintenance into harvest-only mode: completed findings are still
  delivered immediately, but replacement launch is deferred so an instant portfolio refill cannot
  starve the planner lane indefinitely. The reservation is scoped to the planner wave and normal
  refill resumes even when synthesis fails or is capacity-deferred. Every freed-but-unfilled slot
  also creates one versioned, exact-assignment `research_portfolio_pending_replacement` intent with
  the worker count, attempt count, requested slots, and triggering terminal jobs. Unchanged
  heartbeats are write/event deduplicated; ordinary refill clears the intent only after capacity is
  actually filled. The intent survives an epoch rollover, whose refresh path reconciles old workers
  first and then immediately launches a distinct fresh-epoch replacement portfolio.
  Every consumed result persists its cross-lane semantic-novelty classification. Refill pauses at
  32 findings due to the active delivery target, while already-running workers may still be
  harvested. A split child inherits at most one foreground batch of safe same-file ancestor
  evidence; exact-child findings have priority for the remaining window. Excess ancestor results
  remain lossless dispatch-ledger archive pointers and emit explicit archival activity, so inherited
  history cannot starve a new target's research lanes. Inactive theorem obligations remain
  recoverable from the dispatch archive but cannot deadlock a later scope; status and activity
  identify the exact target whose window exerted backpressure.
  Epoch rollover first harvests any just-completed result, retires still-open workers from the
  spent epoch, and leaves refill enabled so the next tick launches distinct replacement routes.
  Evidence-derived replacements carry the bounded canonical source finding, exact route/target
  provenance, digest, anchor job id, and a once-only consumption key in both JobSpec and prompt.
  An exact evidence-to-helper follow-up reserves that source from foreground delivery while active.
  After termination, only an actionable, schema-valid exact helper or replacement keeps the source
  reserved while awaiting harvest; every other result releases it. A materialized actionable
  candidate is delivered first and couples both receipt markers after the next assistant response,
  preventing duplicate synthesis without weakening crash-consistent redelivery.
  Empirical JobSpecs alone add the internal `empirical-compute` toolset; deep-search,
  decomposition, and negation workers retain the ordinary scratch read/check surface without
  interpreter access. Scratch web access resolves through the focused `web-research` toolset
  (`web_search`/`web_fetch` only), so download and repository-clone writers are not callable.
  Terminal legacy PID-only rows stop consuming capacity only after process absence or an exact
  dispatch-worker command/spec mismatch proves that the PID was reused; wall-clock start times are
  diagnostic only. The release tombstone and deterministic activity key survive restart, and a
  failed activity append is retried without blocking foreground proving or portfolio refill.
- `orchestrator_event_watermark.py` — theorem-scoped notification coalescer between parent research
  maintenance and the outer orchestrator. Job/frontier/cadence events advance a monotonic produced
  watermark; one safe outer-loop consultation atomically captures and acknowledges only that prefix.
  Events arriving after capture remain pending, failed consultations release the prefix for retry,
  and only a fail-closed allowlist of reviewed read/search callbacks may close the foreground turn.
  A target-scoped foreground-grace reservation prevents replacement jobs from repeatedly preempting
  consecutive prover turns: harvesting, refill, event publication, and finding staging continue, but
  another research-event interrupt waits until a successful foreground turn or authoritative queue/gate
  boundary releases the reservation. Assignment changes reset the grace state. Edit, verification, lock,
  dispatch, download, clone, and unknown callbacks never invoke routing inside their commit or cleanup
  boundary.
- `research_findings.py` — target-scoped research evidence handoff. It matches consumed findings
  back to dispatch inputs and renders bounded valid JSON for both orchestrator and foreground
  prover prompts, preventing completed jobs from becoming write-only ledger artifacts. The consumed
  dispatch ledger is the lossless payload archive; `research_findings` is a 32-item active-scope
  materialization window with a lightweight result-hash/provenance index. Scope reconciliation pages
  the active theorem and same-file `split_of` ancestors oldest-first, removes an inactive unacknowledged
  copy only after exact ledger/hash validation, and quarantines malformed, missing, or mismatched
  payloads instead of dropping or prompting them. Delivery acknowledgement is always the pair
  `(job_id, foreground target)`: a child receipt never acknowledges its parent, so reopening that
  parent rematerializes the evidence. Operational timeout/error text suppresses a result only when
  no mathematical semantic fingerprint and no managed boundary evidence survived; a nested advisor
  timeout therefore cannot erase an already-derived obstruction or proof shape. The versioned
  archive/substance migration rematerializes older records under this rule. Foreground
  delivery uses evidence-complete batches of at most three findings under a 64 KiB hard prompt
  bound. Actionable checked source for the exact target preempts generic FIFO findings and travels
  alone; oversized evidence remains durable without starving later bounded findings. Prompt
  construction only stages a tagged process-local batch, and an
  ordered transcript scan writes the durable target-scoped marker only for tokens followed by a
  later assistant response. Internal workflow-step boundaries may therefore acknowledge an older
  prefix while retaining newer tool-result evidence. Provider failure, user/signal interruption,
  crash, compaction, or epoch reset redelivers unacknowledged evidence. Assignment changes explicitly
  de-stage old process-local prompt copies without marking their durable findings delivered. Before
  either foreground or orchestrator rendering, findings whose parent-owned semantic novelty marks
  them ineligible are projected to `EVIDENCE_ONLY`. The stricter proof-use policy applies the same
  projection to a mathematically novel congruence/singleton leaf after two rejected proof shapes
  when the worker explicitly says it is non-exhaustive and supplies no exact target-closing checked
  replacement. Such a finite leaf remains durable novelty but cannot seed another recursive
  evidence-to-helper/audit job. Checked and free-form candidates, objectives, target deltas, and
  proof shapes are replaced by audit hashes while explicit counterexamples, noncoverage,
  obstructions, issues, and sanitized unresolved facts remain. This projection happens
  before batching and truncation, so stale code cannot crowd the negative evidence out of a prompt;
  the finding still travels through ordinary acknowledgement and cannot wedge the backlog.
- `research_delivery_gate.py` — versioned assignment-scoped dirty/watermark gate for expensive
  research-ledger reconciliation. Missing, malformed, upgraded, or assignment-mismatched state
  fails closed to one scan; each newly published completion prefix dirties the gate once, duplicate
  consumed statuses remain clean, and a failed scan stays retryable. The checked-source-priority
  schema upgrade forces one recovery scan for older clean watermarks that may have skipped a bounded
  suffix. Safe no-op tool callbacks can therefore inspect delivery readiness without hydrating the
  dispatch archive.
- `research_helper_candidate_priority.py` — durable one-candidate action authority for canonical
  worker-checked helpers. Successful foreground staging records an exact assignment, target
  signature, file revision, declaration hash, and delivery provenance without running Lean under
  the portfolio lock. At the next safe outer boundary the parent rechecks the exact helper and
  allowed-axiom profile before orchestration. An accepted candidate receives one fenced foreground
  insertion opportunity and durably binds its axiom set plus the deterministic integrated-source
  revision. The ordinary source-edit/helper path may reuse that parent gate only for the exact
  authenticated insertion image; acknowledgement alone never retires it, broad search cannot displace it,
  and only that source-edit/helper gate can bank and retire it while the target stays open.
  Source changes force recheck, mathematical rejection retires the candidate, and operational
  unavailability remains resumable.
- `research_helper_source_coverage.py` — proof-insensitive exact-signature deduplication for checked
  helpers already present before the current source target. It deliberately grants no semantic
  subsumption authority to graph status: reconciliation can preserve `proved` after imports or
  earlier declarations change, so every non-identical helper fails open to parent rechecking.
- `research_delivery_observability.py` — bounded activity-event shaping for acknowledged findings.
  Ordinary delivery batches expose exact job ids and stable receipt markers; fixed-size acknowledgement
  tokens plus a complete-set digest retain audit correlation when legacy identifiers exceed the hot
  activity payload limits.
- `research_finding_priority.py` — conservative research-only queue tie-breaking. It promotes
  targets explicitly named by target-scoped structured findings, then declarations with a strong
  proof/artifact identifier suffix match, while generic prose and unrelated same-file findings stay
  neutral. Explicit `EVIDENCE_ONLY` findings are also neutral and cannot promote a queue target.
  Diagnostic buckets and graph-frontier ranks remain authoritative. Within a graph frontier, the
  ready current assignment is sticky; ready members of its transitive `depends_on` family then
  precede unrelated historical ready nodes. Once the current helper is proved or leaves the source
  queue, one `split_of` level opens to its ready siblings or parent. This keeps a newly placed
  decomposition local without pulling older ancestor branches into the same priority class. Ranks
  are keyed by file plus declaration, propagate transitive
  false/parked dependencies, and route an unresolved all-excluded queue to replanning instead of
  falling back to the previous target.
- `golf_mode.py` — Phase 6 §6.9 managed-golf SUBSTRATE (runtime wiring deferred to a
  dedicated follow-on: review established it needs drain-to-done queue lifecycle, baseline
  capture at assignment, and metrics on classified acceptance — so NO flag and NO metrics
  recorder ship, only the flag-free substrate the follow-on composes from): the golf queue
  builder (declared sorry-free theorems/lemmas — a structural read, not an elaboration;
  block-comment phantoms and `sorry`-bodied decls excluded; `golf candidate` reason), the
  `declaration_chars` size primitive, and the `golf candidate` selection bucket strictly
  after diagnostics and sorries (prove selection byte-identical) — all tested, none
  reachable from the runner yet. The compile filter itself is deferred to the runtime.
- `learnings.py` — Phase 5 cross-run knowledge (dark behind `LEANFLOW_LEARNINGS`): every
  terminal scope exit — verified included, independent of the final-report flag — appends one
  compact sanitized entry (outcomes, THIS run's route history from its activity stream, top
  blockers) to a rolling `learnings.md` (locked, atomic), and the next run's scope entry gets
  the newest entries as priors. The priors READER enforces structure (only `## `/`- ` lines
  pass, capped) so hostile or hand-edited content cannot fabricate prompt structure — prompt
  fuel only, never a verdict source. Companion: `LEANFLOW_CURRICULUM_ORDERING` (easy→hard
  tie-break within a frontier rank via statement length — the all-or-nothing `order_key`
  option on `select_next_item`, which can never override the diagnostic-first bucket or the
  frontier ranks). Fire-and-continue deep search is driven by `research_portfolio.py` using
  subprocess workers, never thread-based stdout redirection.
- `multi_direction.py` — Phase 5 §5.8 multi-direction proving (N4): rival attack directions
  from direction-tagged `statements_to_state` become sibling stub FILES (goal-file import
  header + shape-guarded stubs, all-or-nothing validation, never clobbered), each discharged
  sequentially as a shape-A job. Merge protocol is graph-only: the first direction whose full
  set passes the parent gate wins — goal `depends_on` rewires to the winning stubs, losing
  directions' unproved nodes are `parked` (files kept, N1), the choice lands in the decision
  log; all-exhausted leaves one packet per direction. Dark by construction (LLM-only tags +
  `LEANFLOW_DISPATCH_ENABLED`).
- `dispatch_models.py` + `dispatch_service.py` — Phase 3: tracked, lineage-addressed job
  dispatch (`summary.json.dispatch_ledger`, transactional under the shared
  `workflow_json_io.json_write_lock`; independent job budgets; ancestor-gated kill;
  agent-evidence reconciliation). `propose` atomically rejects a duplicate open mathematical-delta
  reservation for the same exact target/file before appending a ledger row. `deploy_async` serializes the JobSpec and starts
  `leanflow_cli.native.dispatch_worker` in its own process group; parent polling harvests the
  atomic structured result. Async launch is a crash-recoverable ledger transaction: a random
  nonce and capacity-counted `deployed` reservation are durable before the spec or `Popen`, and
  `running` is published only after the parent has an exact PID/group/session/token identity.
  An optional policy-neutral async-launch admission predicate is evaluated against the same locked
  `summary.json` snapshot before nonce reservation; research mode uses it to linearize a durable
  provider-reset pause against process creation, leaving a rejected job `proposed` for explicit
  parent retirement without writing a spec or invoking `Popen`.
  A per-job in-process/POSIX sidecar lock spans reservation or recovery rotation through spec write,
  `Popen`, and the running CAS; a resumed stale launcher rechecks the ledger nonce under that lock
  before it may write or spawn. Retry rotation publishes the new shared spec fence before committing
  the new ledger nonce, so a crash between the two can only reject extra work.
  Parent- and child-written nonce-bound identity receipts close the `Popen`/ledger-commit gap;
  restart reconciliation adopts that worker or rotates the nonce after a short handshake and
  retries without refilling the reserved lane. One atomically replaced job-global spec is the
  current-nonce fence read twice by each worker; the final read and a synchronous expected-parent
  liveness check take the same sidecar lock. Cross-parent recovery waits for the exact stale worker
  boundary to disappear after bounded TERM/KILL escalation before rotating or spawning; ambiguous
  identity/permission failures fail closed and keep the reservation instead of overlapping work.
  Identity and result files use nonce-digest names as well as nonce-bearing payloads, so a delayed
  older child cannot overwrite or complete the current attempt. Kill and wall-clock cleanup
  revalidate the exact identity and never signal a legacy or reused PID. Result publication alone
  cannot free actor capacity: normal and crash-recovery harvest requires successful reaping or
  exact structural process-exit evidence, rechecked inside terminal recovery transactions.
  Scratch-only delegates
  map onto internal `web-research` and `lean-research` toolsets: the former excludes download/clone
  writers and the latter retains Lean inspection and inline checking while removing
  `apply_verified_patch` plus the nested `lean_reasoning_help`/`lean_decompose_helpers` model calls.
  Dispatch applies an explicit archetype-specific allowlist before child construction; an omitted
  toolset receives a non-empty safe surface, while a wholly disallowed request fails before provider
  invocation instead of inheriting parent/default tools. Scratch workers receive no terminal access;
  empirical work uses only `empirical-compute` plus deterministic Lean checking. Runtime guards in
  the advisor handlers enforce the same no-nested-model boundary in depth. Their prompt also forbids
  shared project-file writes. The dedicated
  `decomposition`/`dc-*` contract accepts only a bounded normalized `decomposition_report`.
  Source kinds are an exact allowlist; every accepted subgoal must retain a dependency path to the
  target and concrete strict-difficulty reduction evidence. Malformed/incomplete reports fail the
  job, while bounded source/subgoal/dependency collections remain structured for parent review;
  graph updates and plan deltas remain parent-only. Deploy is gated
  on `LEANFLOW_DISPATCH_ENABLED`. `dispatch_ledger_compaction.py` removes copied parent
  launch-context payloads and their rendered objective suffixes from terminal job specifications
  while retaining the stable semantic objective, exact pre-compaction objective/context hashes,
  result-integrity bindings, and all worker-produced proof evidence.
- `dispatch_incremental_evidence.py` — bounded nonce- and exact-JobSpec-bound journal for canonical
  helper declarations observed through successful worker `lean_incremental_check(check_helper)`
  calls. Each accepted callback atomically replaces at most eight exact declarations before the
  delegate can return. On POSIX, the main thread defers SIGHUP/SIGTERM through this short
  fsync+rename boundary. Because a process-directed signal can still arrive through an existing
  unblocked guard/provider thread, the dedicated termination exception retries the same exact
  payload after the handler enters its graceful-shutdown quiet period, then is re-raised. Thus
  cancellation remains prompt without erasing an already returned Lean check. Only the parent
  dispatch service may harvest the journal after
  re-proving that the exact worker process boundary exited; the recovered finding carries no plan
  delta, remains `worker_observation_only`, and requires a foreground parent Lean recheck before it
  can become proof authority. A complete worker result first absorbs the exact journaled helpers
  and then removes the redundant journal; interrupted findings retain the per-job bounded artifact
  for audit and resume.
- `scratch_artifact_cleanup.py` — one-time, provenance-gated startup migration for campaigns
  created before scratch-only tool isolation. It removes only untracked, inactive, unmodified
  project artifacts tied to a terminal scratch-job PID plus a matching tool event and add-file
  checkpoint (or the exact legacy `lean_axioms` temp-harness signature), then clears only their
  shared patch checkpoints/status. Ambiguous, tracked, symlinked, or later-modified files remain.
- `leanflow_cli/native/dispatch_worker.py` — isolated subprocess entrypoint for research JobSpecs.
  It owns only its local agent context and result artifact; it never writes shared plan/graph state.
  The worker publishes its exact launch receipt before backend entry, rechecks the durable nonce
  after that handshake, and binds both successful and failed result artifacts to that nonce.
  Scratch-only workers also set a process-scoped write-denial flag, so leaked `write_file`, `patch`,
  or `apply_verified_patch` calls fail before disk or authoritative patch-status mutation.
  The assignment environment also records the exact archetype, allowing runtime-only tools such as
  `empirical_compute` to fail closed even if a schema is invoked outside its intended toolset.
  Workers start no MCP servers by default and retain native Lean/local-search fallbacks. An
  explicitly provisioned worker may restore configured MCP servers with
  `LEANFLOW_DISPATCH_MCP_SERVERS=*`; if that includes `lean-lsp-mcp`, its diagnostics/REPL and
  remote Loogle return, while private local Loogle additionally requires
  `LEANFLOW_DISPATCH_LOCAL_LOOGLE=1`.
  A parent-PID liveness guard interrupts and then force-reaps its detached process tree if the
  native runner disappears without a normal portfolio shutdown.
- `leanflow_cli/lean/negation_probe.py` — Phase 3: the negation feasibility probe
  (mechanical ¬P construction, `plausible` pre-probe, cheap tactic ladder over LeanProbe
  scratch with a standard-axioms check; budgeted via `summary.json.negation_probes`;
  verdicts are routing evidence only — `false` requires gate promotion). Filled exact-target rows
  preserve dependent top-level `let` bindings in declaration result types instead of mistaking their
  assignments for the theorem proof boundary.
  survive outcome-stream append failures and are recovered against a matching post-selection route.
  If a selected route loses the final budget unit to concurrent work, the latest completed row may
  satisfy it only after freshly rebuilding the same declaration and matching its persisted signature
  hash; promotion still reruns the authoritative source/axiom gate. Ordinary pre-fill exceptions
  release only their own reservation. Timed reservations are reclaimed consistently by preflight and
  the locked gate after a floor of `max(900s, 8 * probe timeout)`, while malformed/ownerless legacy
  reservations remain fail-closed infrastructure pauses.
- `negation_promotion.py` — authoritative false gate. It matches theorem identity, declaration
  signature and source revision, reruns the exact negation/tactic, rejects `sorry` and nonstandard
  axioms, then commits full evidence and graph falsity through a durable pending/committed
  transaction. Startup replays or quarantines interrupted transactions, revalidates the exact
  current main-goal evidence before provider construction, and only then rehydrates terminal
  `disproved`; stale evidence reopens the graph for proving. Exact kernel-check completion activity
  retains the first bounded Lean error alongside its failure kind so malformed harnesses remain
  actionable without persisting unbounded compiler output. Fresh source-candidate failures expose
  an explicit definitive-incompatibility whitelist: only candidate absence, harness-local kernel
  failure, or unacceptable printed axioms may advance candidate scheduling. Raw proposition and
  placeholder spellings are never authority: the complete statement parser retains dependent
  `let`/`have` assignments, while the full-source Lean harness decides whether `P → False` or a
  reducible alias is the exact negation and exposes real placeholders through `sorryAx`. Source,
  lease, goal, parser, audit, and graph/transaction uncertainty remains retryable. A scheduler's
  expected source revision is rechecked after acquiring the source lease, preventing an A-to-B
  race from advancing A's cursor.
- `source_negation_harness.py` — deterministic proof bridge used by the authoritative source gate.
  It turns either a direct negation lemma or a finite specialization counterexample into the exact
  target negation, while revalidation accepts only that canonical proof identity or the historical
  direct `exact` form; the full-source elaboration and axiom audit remain authoritative.
- `source_negation_batch.py` — bounded exact-source compatibility batching for research-mode
  negate routes. It inserts one uniquely named alias beside each source helper, maps diagnostics
  back to exact generated proof spans, rejects foreign/unlocated/truncated evidence, and exposes
  only non-authoritative compatibility verdicts. A compatible alias must still pass the ordinary
  single-candidate revision, identity, axiom, and graph transaction before promotion.
- `source_negation_candidates.py` — non-authoritative scheduling for source-backed negation
  promotion. Exact-target graph evidence and target-derived helper names run before generic
  same-file outcomes, and authenticated graph names remain candidates even without a redundant
  theorem-outcome row. Definitive source-revision-bound incompatibilities advance constant-size
  exact/generic v3 cursors authenticated by complete lane-order hashes, so stable lists of any
  length are eventually exhausted across resumes without one route performing unbounded cold Lean
  checks. Ordinary verified helpers are banked without an eager whole-source promotion check;
  immediate post-helper promotion requires authenticated exact-target counterexample identity.
  Stale current, requested, epoch-selection, and in-flight negate metadata is telemetry rather
  than admission authority, while the bounded negate route still exhaustively revisits generic
  helpers. The v4 check contract reopens older rejections after batched diagnostics gained exact
  path/proof-span attribution; the unchanged v3 schema still stores the two authenticated cursors.
- `negation_revalidation_policy.py` — raise-only cold-start deadline for the exact whole-source
  Lean and axiom rerun used by source-negation promotion and startup revalidation.
- `campaign_root_registry.py` — pure fail-closed authentication of the immutable pre-provider
  requested-root registry. It validates the raw list without filtering malformed entries and is
  shared by native setup and workflow-level terminal authority.
- `negation_transaction_registry.py` — pure classification and retention for every raw
  negation-promotion transaction. Live, unknown, non-mapping, duplicate, or malformed records stay
  exact and unresolved; only authenticated terminal records enter bounded history.
- `false_cleanup_transaction_registry.py` — pure fail-closed authentication and retention for raw
  false-decomposition cleanup transactions and quarantine decisions. Pending, quarantined,
  manual-retry, malformed, unknown, or duplicate evidence remains exact and unresolved; only
  authenticated commits and resolved quarantines enter bounded history. Version-2 transactions
  additionally seal every same-revision conjectured dependent removed with the false helper;
  version-1 evidence cannot acquire an unsealed dependent deletion during replay. An exact
  archived version-1 commit that still has its original false-node/dependent tombstone may be
  atomically replaced by a version-3 dependent-only migration carrying the predecessor id. The
  migration seals source-backed obligations separately from source-less graph artifacts, so replay
  removes Lean declarations only for the former while retiring the complete invalid research branch.
- `false_decomposition_cleanup.py` — source-first transaction that consumes a promoted
  negation of a campaign-created sublemma: exact provenance and fresh negation evidence gate a
  surgical helper removal, transitive removal of exact unresolved decomposer obligations that
  depend on the disproved condition, and restoration of the durable pre-edit parent. Unrelated
  verified source and negation evidence remain intact; verified, externally owned, or ambiguous
  dependents quarantine the transaction. Graph and queue replay retire the deleted identities so
  later plan synchronization cannot resurrect them. Startup also recognizes the exact stale
  tombstone emitted by older committed cleanups and prepares a source-first, crash-replayable
  migration; archive, source, or graph drift quarantines instead of broadening authority. Restart
  migration. Its closure is restricted to unresolved current-source decomposer declarations and
  source-less planner/decomposer artifacts with one exact `split_of` edge to the same parent;
  source-backed external, verified, or evidence-bearing nodes quarantine instead of being detached.
  Current-source graph reconciliation may have advanced the source hash on the already-committed
  proof node and later proved obstruction nodes; migration accepts those exact, unique,
  placeholder-free `prover-edit` declarations only while retaining the committed-v1 archive as its
  authority. An evidence-only committed-v1 tombstone with no remaining structural branch is a
  read-only no-op: startup does not reopen its historical evidence checks or rewrite summary state.
  The one obsolete evidence-classifier quarantine formerly emitted for that exact no-work shape is
  resolved automatically from the authenticated transaction/archive pair; other quarantine reasons
  remain live. Fresh promotion cleanup remains bound to the promoted revision. The parent is reopened
  and unrelated proved helpers remain intact. Archive, source, or graph drift quarantines instead of
  broadening authority. Restart replay is idempotent; stale,
  user-edited, or ambiguous ownership is quarantined while valid negation evidence remains archived.
  Live cleanup transactions are never evicted by history retention, and any remaining pending
  or quarantined transaction pauses the campaign before another provider turn.
- `evals/harness.py` + `evals/corpus_manifest.json` — frozen T2/T3/adversarial inventories and
  scoring for surrender exits, false-success exits, coach coverage, route/proof-shape diversity,
  job lifecycle, verified graph progress, and epoch rollover.

### From `workflow_state.py`

- `activity_preview.py` — pure activity/event-shaping helpers for managed-workflow status views.
- `workflow_activity_reader.py` — streaming JSONL reader used by activity/status summaries so
  historical run volume does not become process memory.
- `workflow_activity_retention.py` — crash-safe long-campaign retention. Startup streams each
  provably closed, non-current run and eligible mirrored agent stream into checksum-verified gzip
  evidence, then atomically records a replaceable per-run summary/tail JSONL shard plus a small
  evidence index before unlinking the hot JSONL. Normal status streams those uncompressed shards
  plus live JSONLs; it never materializes the bulk historical summaries or opens gzip evidence. A
  live writer identity vetoes compaction even when the top-level runner already emitted
  `runner-exit`.

### From `auxiliary_client.py`

- `agent/auxiliary_adapters.py` — OpenAI-client-compatible provider adapters for the auxiliary router.
- `agent/providers/isolated_auxiliary.py` — text-only control-plane auxiliary calls behind a
  parent-enforced subprocess deadline. The worker exchanges normalized JSON over stdio; timeout,
  signal, and cancellation paths kill and reap its isolated process group so a stuck provider SDK
  cannot freeze the managed workflow loop. Worker and parent error paths both apply unconditional
  bounded credential redaction before an error can enter workflow telemetry.
- Model metadata (`agent/model_metadata.py`) + pricing are unified behind the
  `agent/model_capabilities.py` façade.

### From `tools/lean_tool.py`

- `tools/implementations/lean_experts.py` — auxiliary LLM-advisor Lean tools. Its free-form
  theorem-advisor responses pass through `tools/utilities/advisor_persistence.py`, which preserves
  mathematical blocker evidence while removing terminal surrender recommendations and appending
  the deterministic route-change contract. Helper decomposition requests pass through
  `tools/utilities/decomposer_prompt.py`, which removes unrelated file-wide linter noise and
  hard-bounds target diagnostics, goals, attempts, and source facts. The source scan in
  `tools/utilities/decomposer_source_guard.py` makes the exactly resolved on-disk declaration
  signature authoritative over caller-supplied statement text, supplies sorry-free target-scoped
  consistency facts, and rejects terminal-contradiction helpers that conflict with those facts
  before spending a Lean validation check. Caller/source drift is reported as shaping telemetry;
  it never changes the target skeleton. `tools/utilities/decomposer_admission.py` is the shared
  advisor, mechanical decomposer, and orchestrator route-statement boundary that rejects a
  sorry-bodied closed numeral instantiation of a single-parameter parent conclusion while
  preserving parameterized residue and distinct structural helpers.
  `tools/utilities/helper_skeleton_diagnostics.py` fail-closed classifies batches
  where every proposed skeleton directly references an external unknown identifier, avoiding
  unrecoverable per-skeleton retries while preserving diagnostic sequential fallback for partial,
  dependency-order, or mixed failures. `leanflow_cli/lean/lean_decomposition_shape.py` is the shared deterministic
  single-declaration/identity/stub-shape boundary used by both the advisor validator and the
  mechanical decomposer. Multiple eligible templates use one cumulative validation check first,
  with the diagnostic sequential path retained as a fallback. Checked exact `by sorry` templates
  are marked `ready_for_managed_placement`; this authorizes only the decomposer's guarded graph
  insertion path, while `ready_to_insert` remains false until a complete helper has its own
  sorry-free and allowed-axiom verification artifact. The decomposition tool owns one monotonic
  request deadline shared by its advisor and every cumulative or sequential Lean check; each inner
  validation receives only the remaining budget through the authoritative timeout ceiling.
- `tools/utilities/lean_inspection_projection.py` — model-facing exact-symbol inspection shaping.
  It retains all file-wide errors and aggregate `sorry` counts while limiting non-error diagnostics
  and queue items to the resolved declaration. Its capability field is a lossy status digest that
  retains project-validity, error/degradation, and explicitly unavailable capability signals plus
  source hashes and omission counts; `lean_capabilities` remains the full capability surface. The
  authoritative Lean service payload stays full.
- `tools/lean_patch.py` — verified-patch application tool.

### New tools (prove-redesign)

- `tools/implementations/repo_clone.py` — Phase 5 §5.6 repository acquisition: shallow
  single-branch `git clone` into `.leanflow/workspace/repos/` with the `web_download`
  sandbox contract (sanitized dir name, symlink-escape refusal, post-clone size cap with
  cleanup, `cached: true` idempotency). Rides the `web` toolset (registry + `_WEB_TOOLS` +
  the `_discover_tools` module list — all three are required for reachability).

### From `tools/mcp_tool.py`

- `tools/mcp_transport.py` — stdio/HTTP transport plumbing for MCP servers.
- `tools/mcp/mcp_reclaim.py` — pure post-call lifecycle policy for managed MCP servers. Research
  runs retire only `lean-lsp` after `lean_multi_attempt`, because that call can leave a multi-GB
  shared Lean worker resident beyond the returned result. `MCPServerTask` stops admitting new
  requests at the retirement boundary, lets already-admitted calls finish under their handler
  timeouts, closes the exact server identity, and leaves the other MCP servers/event loop live. A
  pre-probed handler crosses that lifecycle boundary through one serialized lazy reconnect, and a
  still-pending recycle is retryable rather than a run-wide backend failure. Reconnect consumes the
  original handler deadline; teardown failure retains fail-closed ownership and never advertises
  completion or admits an overlapping replacement. A timed-out replacement startup keeps a
  per-server fence until async cancellation cleanup has actually unwound, so an immediate retry
  cannot overlap a second Lean process. Final runtime shutdown owns registered servers, starting
  servers, and retire tasks; it stops the shared loop only after every identity and startup fence is
  clear, and reports retained names to the native finalizer on cleanup failure.
- `tools/mcp/mcp_config.py` — MCP server config loading plus the process-scoped
  `LEANFLOW_DISABLE_MCP=1` bypass and worker-only MCP portfolio filter. Foreground MCP remains
  unchanged. Process-isolated research dispatch workers start no private MCP server by default,
  avoiding a retained multi-gigabyte lean-lsp tree while their built-in Lean tools remain usable;
  explicitly provisioned workers can opt in through `LEANFLOW_DISPATCH_MCP_SERVERS`.
- `core/runtime_modes.py` — shared process-scoped resource-mode flags, including the
  `LEANFLOW_LOW_MEMORY=1` umbrella used by MCP, LeanExplore, and LeanProbe layers.
- `core/process_identity.py` — dependency-free capture and token-backed revalidation of native
  workflow PID, process-group, and session ownership across persisted status/activity records.
- `core/provider_availability.py` — bounded structured reset metadata for provider admission;
  durable consumers use its absolute deadline without extending relative timing during harvest.
- `core/provider_capacity.py` — crash-safe file-slot leases plus in-process semaphores for the
  campaign's live background-actor cap. Context propagation makes nested tool/provider helpers
  reentrant without allowing planner and dispatch conversations to exceed the configured total.
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
- **Workflow-outcome fixes:** the headless stdin-exit guard writes a forced filesystem checkpoint
  and returns `2` whenever proof scope remains unresolved; only a clean requested scope returns
  `0`, promoted main-goal negation returns `3`, and signal interruption returns `130`. Provider/API
  infrastructure exits also write a deterministic post-quiescence filesystem checkpoint before
  releasing locks, without making another provider call. Signal exits refresh the durable queue
  assignment and source-derived sorry counts after owned writers quiesce, preventing an outer-loop
  snapshot cached before a followup from entering the checkpoint. The hard
  cycle ceiling now rolls a fresh campaign epoch instead of stopping; and
  `mcp_bootstrap._write_bootstrap_document` now calls `invalidate_config_cache()` after writing the
  managed config directly (it bypasses `config.save_config`), fixing a stale `load_config()` cache
  that returned the pre-bootstrap config later in the same process.
- **Managed proof-state latency and callback fixes:** startup reuses the capability report returned
  by its single `lean_inspect` call and probes independently only when inspection has no report;
  `startup-proof-state-refresh-started/finished` records wall and phase timing. When queue selection
  rotates the target, the runner reuses that capability map with the goals-only service instead of
  starting another full inspection. Final-report review rejects a non-success report immediately
  when the exact assigned declaration contains a literal comment/string-stripped `sorry` or `admit`;
  this rejection-only source gate is target-scoped and never replaces kernel verification for a
  success claim or placeholder-free source. Post-tool callbacks run structural edit finalization
  only for a matching `patch`/`write_file`/`apply_verified_patch` snapshot; read-only, malformed, or
  support-file callbacks still receive ordinary managed-result handling but cannot consume a stale
  source-edit snapshot or emit an `[unknown]` finalization event.
- **Provider recovery:** the provider loop makes one initial request and three interruptible retries
  after 5, 15, and 45 seconds. Managed activity records each scheduled retry and the exhausted
  boundary; only then may the native runner checkpoint a resumable infrastructure pause.

### Deferred (needs dependency-injection seams / method-surgery, not safe as one-shot moves)

The still-coupled cores that resist behavior-preserving one-shot moves:

- `native_runner.py`: the autonomous follow-up loop and `_run_managed_conversation` / `main`.
- `AIAgent`: the `run_conversation` main loop and its retry/recovery orchestration.
- `main.py`: `InteractiveShell` (its callees are test-monkeypatched on `main`).
- `lean_services.py`: the full backend **abstraction** (the LSP/REPL/Lake interface), which is a
  redesign rather than a move. The `LeanBackend` wrapper (`lean_backend.py`) is a first partial
  step; routing all call sites through it is the remaining invasive work.

These are the next, more invasive refactoring steps.
