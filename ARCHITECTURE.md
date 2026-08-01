# LeanFlow Architecture

This document is the maintainer map for LeanFlow's current runtime. It records
the package boundaries, principal execution paths, compatibility surfaces, and
invariants that must survive implementation changes. User-facing behavior is
documented in `README.md` and `docs/product-reference.md`.

## Design Boundaries

LeanFlow is a Lean-first automation kernel. Its dependency direction is:

```text
core
├── agent
├── tools
└── leanflow_cli
```

- `core/` owns dependency-light runtime primitives and must not import from the
  higher layers.
- `agent/` owns provider conversations, prompt assembly, context management,
  tool execution, and accounting.
- `tools/` owns model-callable capabilities and deterministic safety guards.
- `leanflow_cli/` composes the agent and tools into Lean workflows, persistent
  project state, provider routing, and shell commands.

New behavior belongs in the smallest cohesive leaf module. Do not add new
responsibilities to `run_agent.py` or
`leanflow_cli/native/native_runner.py` when a focused collaborator can own
them.

## Entry Points

- `leanflow` → `leanflow_cli.main:main`
- `leanflow-agent` → `leanflow_agent:main`
- `run_agent.AIAgent` → the provider conversation loop used by managed
  workflows
- `leanflow_cli.native.native_runner` → the long-running native Lean workflow
  runtime

The shell launches managed workflows as child processes. Inside a managed
process, `native_runner` constructs `AIAgent` directly and coordinates its
turns with Lean verification and durable workflow state.

## Repository Layout

```text
LeanFlow/
├── core/                 # shared runtime kernel and compatibility authorities
├── agent/                # model conversation collaborators
├── tools/                # model-callable tools and deterministic guards
├── leanflow_cli/         # CLI, Lean services, workflows, runtimes, persistence
├── leanflow_skills/      # packaged prompt-time Lean guidance
├── leanflow_specs/       # packaged workflow and worker contracts
├── evals/                # frozen evaluation harness and adversarial fixtures
├── testdata/             # deterministic Lean workflow fixtures
├── tests/                # unit, integration, installer, and contract tests
├── run_agent.py          # AIAgent compatibility surface and core loop
├── leanflow_agent.py     # leanflow-agent entry shim
└── README.md             # product overview and quick start
```

Local campaigns and their logs belong under ignored project state or an
external results repository, never in this source tree. The repository-level
`artifacts/` directory is ignored for this reason.

## Core

`core/` contains primitives that are shared across the agent, tool, and CLI
layers:

- `home.py` is the only authority for `LEANFLOW_HOME` and `~/.leanflow`.
- `state.py` owns the SQLite conversation/session store.
- `model_tools.py` and `toolsets.py` expose tool discovery and toolset
  selection.
- `process_identity.py` provides token-backed process, process-group, and
  session ownership checks.
- `provider_availability.py` and `provider_capacity.py` coordinate provider
  recovery and bounded background actors.
- `project_resource_admission.py` coordinates resource-heavy Lean work.
- `runtime_modes.py` centralizes process-scoped runtime flags.
- `verified_edit_authority.py` carries single-use, hash-bound authorization
  between managed orchestration and atomic patch tools when prior Lean evidence
  proves one exact source transition.
- `filesystem.py`, `time.py`, `constants.py`, and `utils.py` provide shared
  dependency-light utilities.

The top-level `model_tools.py`, `toolsets.py`, and `utils.py` modules are
compatibility shims over `core.*`.

## Agent Runtime

`run_agent.AIAgent` retains the public conversation-loop surface. Its
collaborators are grouped by responsibility:

- `agent/providers/` — primary and auxiliary provider routing, Codex Responses,
  Anthropic adaptation, isolated auxiliary calls, retries, and model metadata
- `agent/prompting/` — system prompts, prompt caching, reasoning normalization,
  and provider-response normalization
- `agent/compression/` — conversation persistence, context compression, and
  provider-aware summary handoff
- `agent/execution/` — tool batches, interrupts, command safety, skill
  commands, and resource handoff
- `agent/accounting/` — token/cost accounting, redaction, and error logging
- `agent/display/` — terminal rendering and structured log formatting
- `agent/runtime/` — managed-run contracts, trajectory capture, and workflow
  events

`agent/execution/collaborator_resolvers.py` preserves lazy construction and
test patch points for these collaborators. Changes to a collaborator must
retain the corresponding `AIAgent` wrapper or compatibility property unless
the public surface is intentionally migrated.

## Tool Runtime

Tools self-register through `tools/registry.py`. Their schemas and normalized
results flow through `core.model_tools` and `tools/response.py`.

- `tools/implementations/` contains model-callable Lean, file, terminal,
  document, web, repository, delegation, memory, skill, and empirical tools.
  `lean_have_extraction.py` owns the transactional local-`have` promotion tool;
  its source parser lives in `leanflow_cli/lean/lean_have_extraction.py`.
- `tools/utilities/` contains deterministic guards and reusable implementation
  support, including process ownership, transcript protection, repository
  research policy, scratch-terminal policy, verified patch parsing, helper
  admission, and bounded authoritative source context for Lean advisors.
- `tools/mcp/` contains MCP configuration, schema shaping, transport, sampling,
  and managed-server lifecycle behavior.
- `tools/environments/` contains the local, SSH, Singularity, Daytona, and
  persistent-shell execution backends.

Tool discovery depends on import-time registration. Adding a tool requires
updating every applicable discovery list and toolset, plus tests that prove the
tool is reachable through the public registry.

## CLI and Workflow Runtime

`leanflow_cli/` is organized by responsibility:

- `main.py` and `cli/` own argument parsing, shell commands, status rendering,
  doctor checks, MCP bootstrap, and expert-help configuration.
- `workflow.py` resolves workflow requests, providers, toolsets, and the
  `LEANFLOW_NATIVE_*` child-process environment contract.
- `runtime/` owns provider credentials/routing, file locks, sandbox execution,
  branding, environment loading, and built-in skill discovery.
- `lean/` owns diagnostics, goals, declaration inspection, incremental checks,
  automation, proof context, premise search, axiom checks, ephemeral
  validation, target-owning verification-path resolution, and the typed
  `LeanBackend` facade. Tactic-hole portfolios route
  through `lean_attempt_screening.py`, which prepares the target environment
  once and exact-checks bounded candidates with LeanProbe before any positional
  LSP fallback.
- `formalization/` owns source-document extraction, TeX discovery, generated
  Lean shaping, and the statement-review handoff.
- `workflows/` owns proof queues, verification transactions, persistent
  plan/graph state, orchestration, research portfolios, decomposition,
  monolithic partial-proof detection, negation, project proving, campaign
  epochs, repeated-tool loop boundaries, and activity retention.
- `native/` owns the managed workflow process, startup/resume reconciliation,
  assignment transitions, completion policy, durable bounded-search synthesis
  admission (`search_synthesis_admission.py`), same-revision verification-timeout
  backpressure and structural-recovery handoff, verified companion-module
  publication (`support_module_materialization.py`), checkpoints, and shutdown.
  Hard-diagnostic edit rollback lives in `managed_edit_rollback.py` so the
  exact after-image check and atomic restoration remain independently tested.
  Revision-authenticated successful-gate reuse lives in
  `verified_gate_handoff.py`; it carries a checked queue snapshot across the
  provider boundary without replaying diagnostics, goals, or full-file builds.
  Large local-proof partitioning is split between the comment-safe candidate
  inventory in `leanflow_cli/lean/lean_have_extraction.py` and bounded,
  transactional LeanProbe extraction in `tools/implementations/lean_have_extraction.py`.

The larger coordination modules remain intentionally coupled where tests patch
their module attributes. Extracting behavior from them requires
characterization tests and an explicit dependency seam first.

## Runtime Contracts

The packaged Markdown under `leanflow_skills/` and `leanflow_specs/` is runtime
input, not supplementary prose:

- skills select concise prompt-time behavior for the active workflow
- workflow specs define tool order, verification gates, route actions, and stop
  conditions
- worker and phase specs define specialist responsibilities

These files ship in the wheel. A change to a workflow contract must update its
spec, the routing skill when applicable, and the relevant deterministic tests.

## Principal Execution Paths

### Proving

```text
leanflow workflow prove
  → workflow request and provider resolution
  → native runner startup/resume reconciliation
  → project/file declaration queue
  → AIAgent turn
  → Lean/file/search tools
  → manager verification and transaction commit
  → queue advance or verified completion
  → project-wide Lake gate
```

The model proposes edits and research routes. LeanFlow's deterministic manager
decides whether an edit is accepted, retried, restored, or advanced.

### Formalization

```text
leanflow workflow formalize
  → source extraction and TeX/PDF preflight
  → declaration blueprint
  → buildable Lean statement draft
  → source-fidelity review
  → explicit handoff to prove
```

Formalization intentionally leaves theorem bodies as `sorry` after statement
approval. The subsequent `prove` workflow owns proof completion.

### Research Mode

Research mode keeps one foreground prover and a bounded portfolio of background
research actors. Planner lanes and process-isolated jobs share the configured
background capacity. The parent process alone may mutate the authoritative
Lean source, proof graph, and workflow plan.

Web, paper, code, and repository research are enabled for normal campaigns.
Clean-room flags remove repository and task-solution research while retaining
general mathematical search. All research findings remain advisory until they
pass the ordinary source-fidelity and Lean verification gates.

## Persistence and Resumability

User-level state lives under `LEANFLOW_HOME` (normally `~/.leanflow`).
Project-level state lives under `.leanflow/` in the registered Lean project.

The workflow state includes:

- activity and human-readable logs
- checkpoints and terminal outcomes
- declaration queues and failed-attempt history
- plan and proof-graph snapshots backed by an append-only journal
- research dispatch ledgers and delivery receipts
- campaign epochs, route decisions, and learnings
- file locks and live-run ownership metadata

Writes use the workflow JSON/append helpers and atomic replacement where
appropriate. Resume first reconciles persisted state with current Lean source;
historical state is never assumed current before that reconciliation.

## Verification and Trust Boundaries

- A model response is never proof of completion.
- `prove` succeeds only after the assigned declaration and requested project
  scope pass deterministic placeholder, diagnostic, kernel, and final build
  gates.
- `formalize` succeeds when the statement draft builds and passes source
  review; intentional theorem holes are then handed to `prove`.
- Axiom checks use elaborated declarations, not source-text heuristics alone.
- Promoted negations and helper proofs pass the same trust checks as main proof
  edits.
- Managed theorem workers route inner-loop Lean checks through
  `lean_incremental_check`; `native/terminal_check_policy.py` reserves direct
  terminal Lean and Lake processes for manager-owned canonical gates.
- Repeated target timeouts can mechanically promote a large local `have` to a
  private lemma: Mathlib supplies the exact context signature, LeanProbe checks
  the helper and replacement site, and the verified patch transaction commits
  only the authenticated source image.
- `lean/lean_interact_compat.py` installs a version-guarded linear response
  reader when the installed LeanInteract still uses quadratic REPL output
  concatenation; unfamiliar future implementations remain untouched.
  `lean/lean_probe_deadline.py` independently bounds every LeanProbe call and
  terminates owned REPL sessions when IPC stalls.
- Verified graph state is derived from Lean evidence and reconciled after
  source changes.
- File locks serialize supported writes during user-approved swarm runs.
- Process termination requires exact token, PID, process-group, and session
  identity; stale PID-only records fail closed.
- Managed agents cannot read raw live workflow transcripts through ordinary
  file tools. Bounded, generated summaries are the model-facing interface.
- Clean-room policy is enforced across web, repository, terminal, and file
  surfaces, including canonical-path and symlink checks.
- Empirical computation runs in a restricted child process with bounded
  resources and no filesystem, process, or network capability.

## Compatibility Surfaces

The following interfaces are load-bearing:

- `from run_agent import AIAgent`
- top-level `model_tools`, `toolsets`, and `utils` imports
- `AIAgent.run_conversation()` result keys:
  `final_response`, `last_reasoning`, `messages`, `api_calls`, `usage`,
  `completed`, `exit_reason`, `partial`, `interrupted`, and
  `response_previewed`, with conditional interruption/error fields
- tool names and import-time self-registration
- module attributes intentionally used as monkeypatch targets in tests,
  especially in `native_runner.py`, `run_agent.py`, and terminal tooling
- the `LEANFLOW_`-only native child-process environment contract
- `core.home.leanflow_home()` as the single state-home authority

Compatibility imports that exist solely as public re-exports or patch targets
must carry an explicit `# noqa: F401` and a test or real call site that proves
the reference is intentional.

## Change Discipline

Before changing a coupled boundary:

1. Add characterization tests for current behavior.
2. Extract one cohesive responsibility without changing semantics.
3. Preserve public imports and monkeypatch seams or migrate them explicitly.
4. Add the cleaned module to the mypy gate.
5. Run Black, Ruff, mypy, and the full test suite.
6. Update this map when ownership or a public surface changes.

The complete contribution and quality-gate requirements are in `AGENTS.md` and
`CONTRIBUTING.md`.
