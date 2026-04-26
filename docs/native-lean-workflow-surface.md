# Native Lean Workflow Surface

This document summarizes the native Lean workflow/tooling contract that now drives EPFLemma.

## Canonical Workflow IDs

EPFLemma normalizes the public Lean workflow commands to these internal workflow IDs:

- `prove`
  - `/prove`
  - `/autoprove`
- `formalize`
  - `/formalize`
  - `/autoformalize`
- `draft`
- `review`
- `checkpoint`
- `refactor`
- `golf`

The auto-prefixed forms are aliases only. They are not separate runtimes or policy bundles.

## Specs Are The Contract

The canonical contract lives in markdown-backed specs under:

- `epflemma_specs/workflows/`
- `epflemma_specs/workers/`

Workflow specs currently shipped:

- `prove`
- `formalize`
- `draft`
- `review`
- `refactor`
- `golf`
- `checkpoint`
- `doctor`
- `search`

Worker specs currently shipped:

- `proof-repair`
- `proof-golfer`
- `axiom-eliminator`
- `sorry-filler-deep`

Skills remain the routing layer, but the prompt builder, doctor, router, and Lean tools all read the same spec metadata. `epflemma_cli/lean_workflow_specs.py` validates alias collisions and unknown worker references in tests.

## Native Lean Tools

The repo-owned Lean tool surface is defined in `tools/lean_tool.py` and backed by `epflemma_cli/lean_services.py`.

- `lean_capabilities`
  - project validity
  - `lean` / `lake` / `elan` binary availability
  - MCP/LSP tool discovery
  - search-provider availability
  - helper availability
  - worker availability
  - degraded-mode reasons
- `lean_inspect`
  - `diagnostics`
  - `goals`
  - `sorry_count`
  - `project_sorry_count`
  - `blocker_kind`
  - `queue_items`
  - `capability_report`
- `lean_verify`
  - `mode=file_exact|module|project`
  - `file_exact` is the acceptance path for file-scoped theorem turns
- `lean_search`
  - `mode=auto|local|semantic|type-pattern|natural-language`
  - MCP-first provider selection with `rg`/Mathlib fallback
  - provider provenance in `attempted_providers` and per-result metadata
  - explicit `degraded_reasons` when semantic providers are missing or skipped
- `lean_proof_context`
  - theorem-local context retrieval from the managed automation backend
  - returns theorem statement, original proof, hypotheses, in-scope names, namespace, and optional similar proofs
  - not a replacement for `lean_inspect` goals
  - prefers local declaration-range stabilization when the active file already contains the target declaration
  - falls back to a local declaration slice when proof-auto reports `theorem_not_found` or another backend-side context miss
  - keeps proof-auto MCP enabled after a theorem-lookup miss; only transport or systemic backend failures are sticky-disabled for the current run
- `lean_multi_attempt`
  - theorem-local screening for 2-6 concrete tactic candidates at one file position
- `lean_auto_probe`
  - theorem-local automation probing
- `lean_auto_search`
  - theorem-local automated proof candidate search after context/probe data exists
- `lean_auto_try`
  - validate one concrete theorem-local automated proof candidate before patching
- `apply_verified_patch`
  - compatibility path for one atomic Lean patch, pre-edit checkpoint, and immediate verification payload
  - managed queue workflows normally use `patch`/`write_file`, because the runner verifies successful edits before advancing the queue
- `lean_sorries`
  - project/file-scoped `sorry` findings with line number and declaration name
- `lean_axioms`
  - best-effort `#print axioms` wrapper
  - returns `axioms`, `custom_axioms`, `classical`, and `choice`
- `lean_worker_dispatch`
  - dispatches native worker presets
  - uses file locks when owner/delegation context is available
  - returns a structured plan instead of hard-failing when delegation is unavailable

These tools are available through the `lean`, `epflemma-native`, and `epflemma-native-swarm` toolsets.

EPFLemma installs and manages the Lean MCP backends by default:

- `lean-lsp-mcp`
  - role: `primary-state-search`
  - exposes diagnostics, goals, search, state/premise/hover/outline discovery, and tactic attempt screening
  - configured with `LEAN_REPL=true` and local Loogle on Linux/macOS/WSL
  - prefers local acceleration first, then public remote Lean search fallbacks, then native project/Mathlib search
- `lean-proof-auto-mcp`
  - role: `secondary-automation-context`
  - used through native wrappers, with local fallback when backend theorem lookup misses a declaration visible in the current file
- `lean-explore`
  - role: `semantic-declaration-search`
  - configured disabled by default because the API backend requires `LEANEXPLORE_API_KEY`; enable it or switch to a prepared local backend when semantic declaration search should join `lean_search`

Those servers exist to back the native tools above. Raw `mcp_*` tools are not part of the normal native Lean workflow surface.

## Queueing, Routing, And Workers

The file/declaration queue is still the core execution model. The runner now supplements it with a structured route decision:

- inputs
  - workflow kind
  - queue item
  - blocker kind
  - attempt count
  - search exhaustion
  - capability/degraded-mode state
- outputs
  - `skill_name`
  - `route_action`
  - `recommended_worker`
  - `reason`

Queue items are enriched with:

- line numbers
- blocker signatures
- search hints
- verification gates
- recommended workers

Current worker recommendation rules:

- `proof-repair`
  - repeated compiler-style blockers
- `proof-golfer`
  - explicit `golf` routes
- `axiom-eliminator`
  - axiom-risk cleanup
- `sorry-filler-deep`
  - repeated stuck queue items or exhausted search

## Project Prove Manager

`/prove SomeFile.lean` enters the existing file-scoped theorem queue directly. `/prove` with no file enters the project prove manager first.

The manager is intentionally a thin scheduler above the file queue:

- it only chooses the next file
- it does not edit Lean files itself
- it does not replace theorem-level queueing, diagnostics, failed-attempt tracking, or verification gates
- it delegates each assigned file back to the same path used by `/prove SomeFile.lean`

Startup behavior:

1. The workflow resolver normalizes `/prove` and `/autoprove` to `prove`.
2. The native runner checks whether the command contains an explicit `.lean` file.
3. If there is no explicit file, the project prove manager scans project Lean files for `sorry`.
4. Candidate files are summarized with:
   - relative label
   - absolute path
   - module name
   - `sorry_count`
   - `line_count`
   - declaration count
   - pending declaration names, source excerpts, and per-declaration difficulty scores
   - hint and worked-example counts
   - full source for small files, or selected header/import/hint/theorem excerpts for larger files
   - direct and transitive candidate-file imports/dependents
   - direct and transitive project-file imports/dependents
   - `imported_by_count`
   - `import_count`
5. A deterministic fallback queue is built from candidate-to-candidate dependency importance, unresolved candidate dependencies, project-wide downstream importance, theorem difficulty, first-pending-declaration difficulty, `sorry` count, length, declaration count, and stable path order.
6. The configured LLM is asked to reorder the bounded candidate list using the same policy and the provided source context, with competition-style theorem names treated as harder and hinted/worked-example-heavy files treated as easier.
7. The LLM response is accepted only as JSON-like file labels that match known candidates; missing fallback files are appended, and deterministic dependency/difficulty buckets remain guardrails around the model order.
8. The first file is assigned by setting `EPFLEMMA_NATIVE_ACTIVE_FILE`.
9. The normal file-scoped theorem queue takes over.

Continuation behavior:

- after the active file verifies, the manager records it as completed
- the manager refreshes candidates from the current filesystem, so solved files disappear from the queue
- if candidates remain, the next file is assigned
- if no candidates remain, the project prove queue is complete

Parallelism policy:

- default `/prove` uses one managed agent and one assigned file at a time
- parallel agents are only enabled by explicit user flags such as `--agents 3`
- explicit file workflows with a file argument continue to force file-local handling instead of becoming project scheduling runs

## Doctor And MCP

`epflemma doctor` now uses the same capability layer as the Lean workflows.

Supported modes:

- `all`
- `env`
- `mcp`
- `search`
- `migrate`
- `cleanup`

Useful commands:

```bash
epflemma doctor
epflemma doctor mcp --json
epflemma doctor search --json
epflemma mcp bootstrap lean
epflemma mcp status
epflemma mcp status --json
```

`epflemma mcp status` reports server role, managed/install/config health, connection state, registered tools, local Loogle/REPL power-mode status, public remote fallback policy, and sampling counters.

`epflemma mcp bootstrap lean` is the idempotent repair/setup command for the managed Lean MCP stack.

Power-mode details:

- Local Loogle avoids the public Loogle rate limit and is attempted on Linux/macOS/WSL. First local setup may take 5-10 minutes and about 2GB of disk. If it is cold or unavailable, public remote Lean search fallback remains enabled.
- REPL mode makes line-based `lean_multi_attempt` faster after the project has a built `repl` binary. `epflemma project init` attempts safe setup, prints progress for `lake update repl` and `lake build repl`, and continues with LSP fallback if setup fails.
- LeanExplore API mode remains opt-in because it requires `LEANEXPLORE_API_KEY`; users can switch it to a prepared local backend manually.

For persistent sampling audit logs, set `mcp_servers.<name>.sampling.audit_jsonl: true` in `~/.epflemma/config.yaml`. The default path is `~/.epflemma/logs/mcp-sampling.jsonl`, with `audit_jsonl_path` available as an override.

## Persisted Workflow State

Native Lean workflows now persist more than logs and checkpoints.

Relevant files under `.epflemma/workflow-state/` include:

- `live_status.json`
- `activity/`
- `runs/`
- `file_locks.json`
- `outcomes.jsonl`

For project-scoped `/prove`, `live_status.json` stores `project_prove_manager`, `project_prove_file_queue`, `project_prove_completed_files`, `project_prove_plan_source`, and `project_prove_plan_reason` alongside the normal active-file, queue, diagnostics, build, route, checkpoint, and provider/model fields.

The activity JSONL stream records project prove-manager events:

- `project-prove-file-queue-planned`
- `project-prove-file-assigned`
- `project-prove-file-queue-empty`
- `project-prove-file-queue-complete`

`project-prove-file-queue-planned` contains the candidate metrics and final ordered file labels. `project-prove-file-assigned` contains the assigned file, absolute path, remaining queue, plan source, and plan reason. These events are intended to be machine-readable enough for detailed inspection and offline training trace curation.

`outcomes.jsonl` records route decisions and worker outcomes so later cycles and resumed sessions can reuse prior blocker classifications, worker recommendations, and search/repair history.
