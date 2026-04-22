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
epflemma mcp status
epflemma mcp status --json
```

`epflemma mcp status` reports configured server state, last error, registered tools, and sampling counters.

For persistent sampling audit logs, set `mcp_servers.<name>.sampling.audit_jsonl: true` in `~/.epflemma/config.yaml`. The default path is `~/.epflemma/logs/mcp-sampling.jsonl`, with `audit_jsonl_path` available as an override.

## Persisted Workflow State

Native Lean workflows now persist more than logs and checkpoints.

Relevant files under `.epflemma/workflow-state/` include:

- `live_status.json`
- `activity/`
- `runs/`
- `file_locks.json`
- `outcomes.jsonl`

`outcomes.jsonl` records route decisions and worker outcomes so later cycles and resumed sessions can reuse prior blocker classifications, worker recommendations, and search/repair history.
