# Native Lean Workflow Surface

This document summarizes the native Lean workflow/tooling contract that now drives LeanFlow.

## Canonical Workflow IDs

LeanFlow normalizes the public Lean workflow commands to these internal workflow IDs:

- `prove`
  - `/prove`
  - `/autoprove`
- `formalize`
  - `/formalize`
  - `/autoformalize`
- `draft`
- `review`
- `refactor`
- `golf`

The auto-prefixed forms are aliases only. They are not separate runtimes or policy bundles.

`/formalize` and `/autoformalize` require a project-local `.tex` source, `.pdf` source, or directory containing a TeX project. The resolver prepares document preflight artifacts, a generated supplemental blueprint skill, and an active Lean target file before the native runner starts. Directory inputs are resolved to a main TeX source and record included `.tex`, bibliography, PDF, figure, and TeX support files in the manifest. LaTeX preflight recognizes standard theorem environments plus custom declarations from `\newtheorem`, `\declaretheorem`, `\newmdtheoremenv`, `\mdtheorem`, `\spnewtheorem`, and `\newtcbtheorem`; it also handles plain-TeX `\profess...\endprofess` blocks and nearby proof bodies. Once the drafting pass has a compilable `sorry` skeleton and only statement/source approval is missing, the native runner starts the configured independent verifier pass over the source document, blueprint, and Lean draft. By default this uses the managed reviewer agent; `auxiliary.blueprint_verification` can instead route advisory review to a model/RPC provider, Codex CLI, Claude Code, or deterministic local-only checks. `auxiliary.autoformalizer_verification` separately controls advisory review around the deterministic autoformalization handoff verifier. After review approval and deterministic local/Lean checks, the formalizer gets one focused generated-file organization pass, then exits; proof filling waits for an explicit user-run `/prove SomeFile.lean` or `/prove`. `/prove SomeFile.lean` auto-attaches the generated skill when `SomeFile.lean` has a nearby `Blueprint.md`; users can also pass `--additional-skill path/to/SKILL.md`.

## Specs Are The Contract

The canonical contract lives in markdown-backed specs under:

- `leanflow_specs/workflows/`
- `leanflow_specs/workers/`

Workflow specs currently shipped:

- `prove`
- `formalize`
- `draft`
- `review`
- `refactor`
- `golf`
- `doctor`
- `search`

Dormant worker specs currently shipped:

- `proof-repair`
- `proof-golfer`
- `axiom-eliminator`
- `sorry-filler-deep`

Skills remain the routing layer, but the prompt builder, doctor, router, and Lean tools all read the same spec metadata. `leanflow_cli/lean_workflow_specs.py` validates alias collisions and unknown worker references in tests.

## Native Lean And Document Tools

The repo-owned Lean tool surface is defined in `tools/lean_tool.py` and backed by `leanflow_cli/lean_services.py`.

Document formalization also exposes `read_pdf` and `formalization_document_inspect` from `tools/document_tool.py`, backed by `leanflow_cli/formalization_documents.py`. `read_pdf` is the obvious model-facing tool for extracting text from project-local PDF papers. `formalization_document_inspect` inspects project-local `.tex` and `.pdf` sources, extracts LaTeX sections, standard and custom theorem-like environments, adjacent proof excerpts, and PDF text metadata when local tools are available, and reports degraded extraction reasons.

Managed queue turns allow new helper declarations that directly support the assigned theorem. The edit guards preserve existing theorem/lemma/example statements and restore edits to pre-existing non-assigned declarations or future queue items.

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
  - exact-symbol calls retain every file-wide error and aggregate `sorry` count while scoping
    non-error diagnostics and queue rows to the declaration; their `capability_report` is an
    explicitly lossy status digest with project-validity, error/degradation signals, a source
    SHA-256, and omission counts. Use `lean_capabilities` for the full capability inventory.
- `lean_verify`
  - `mode=file_exact|module|project`
  - `file_exact` is the acceptance path for file-scoped theorem turns
- `lean_search`
  - `mode=auto|local|semantic|type-pattern|natural-language`
  - MCP-first provider selection with `rg`/Mathlib fallback
  - provider provenance in `attempted_providers` and per-result metadata
  - explicit `degraded_reasons` when semantic providers are missing or skipped
  - managed file-scoped assignments move only confirmed later declarations from the current file
    into `source_order_inaccessible_results`; prior/imported results stay usable and ambiguous
    matches fail open. The current disk declaration index, not stale provider line metadata, is
    authoritative
- `lean_proof_context`
  - theorem-local context retrieval from the managed automation backend
  - returns theorem statement, original proof, hypotheses, in-scope names, namespace, and optional similar proofs
  - not a replacement for `lean_inspect` goals
  - prefers local declaration-range stabilization when the active file already contains the target declaration
  - falls back to a local declaration slice when proof-auto reports `theorem_not_found` or another backend-side context miss
  - keeps proof-auto MCP enabled after a theorem-lookup miss; only transport or systemic backend failures are sticky-disabled for the current run
- `lean_multi_attempt`
  - theorem-local screening for 2-6 concrete tactic candidates at one file position
- `lean_auto_search`
  - theorem-local automated proof candidate search after proof context or concrete local evidence exists
- `apply_verified_patch`
  - compatibility path for one atomic Lean patch, pre-edit checkpoint, and immediate verification payload
  - managed queue workflows normally use `patch`/`write_file`, because the runner verifies successful edits before advancing the queue
- `lean_sorries`
  - project/file-scoped `sorry` findings with line number and declaration name
- `lean_axioms`
  - best-effort `#print axioms` wrapper
  - returns `axioms`, `custom_axioms`, `classical`, and `choice`
  - dispatches native worker presets
  - uses file locks when owner/delegation context is available
  - returns a structured plan instead of hard-failing when delegation is unavailable

These tools are available through the `lean`, `leanflow-native`, and `leanflow-native-swarm` toolsets.

LeanFlow installs and manages the Lean MCP backends by default:

- `lean-lsp-mcp`
  - role: `primary-state-search`
  - exposes diagnostics, goals, search, state/premise/hover/outline discovery, and tactic attempt screening
  - configured with `LEAN_REPL=true` and local Loogle on Linux/macOS/WSL
  - prefers local acceleration first when toolchain-compatible with the active project, then public remote Lean search fallbacks, then native project/Mathlib search
- `lean-proof-auto-mcp`
  - role: `secondary-automation-context`
  - used through native wrappers, with local fallback when backend theorem lookup misses a declaration visible in the current file
- `lean-explore`
  - role: `semantic-declaration-search`
  - `lean_search` prefers the local LeanExplore backend when `lean-explore[local]` is installed and `lean-explore data fetch` has prepared the index
  - `lean_search` can fall back to the hosted LeanExplore API when `LEANEXPLORE_API_KEY` is set
  - the managed MCP server is still configured disabled by default; enable it for MCP `search_summary`/getter tools, or switch it to a prepared local backend after fetching LeanExplore data

Those servers exist to back the native tools above. Raw `mcp_*` tools are not part of the normal native Lean workflow surface.

Set `LEANFLOW_LOW_MEMORY=1` for a deliberately low-memory run. LeanFlow skips all
configured MCP subprocesses, the in-process LeanExplore index, and LeanProbe's warm
incremental environment cache for that process. Native Lean wrappers use exact Lean
checks plus project/Mathlib text-search fallbacks. This reduces search and automation
breadth; it does not weaken the final kernel gate. `LEANFLOW_DISABLE_MCP=1` remains a
narrower switch when only MCP subprocesses should be disabled.

Process-isolated research workers use a lighter profile even when the foreground runs
with `LEANFLOW_LOW_MEMORY=0`: they start no MCP subprocesses and no local LeanExplore
index by default. Native Lean checks, local declaration extraction, and project/Mathlib
text-search fallbacks remain available. This avoids duplicating proof-auto, lean-lsp,
local LeanExplore, and local Loogle service trees per worker. A memory-provisioned worker
may first restore configured MCP servers with `LEANFLOW_DISPATCH_MCP_SERVERS=*`; enabling
that worker's private local Loogle additionally requires
`LEANFLOW_DISPATCH_LOCAL_LOOGLE=1`. Set
`LEANFLOW_DISPATCH_LEANEXPLORE_BACKEND=local` only when each worker is also provisioned
for its own local semantic index. These worker-only settings never narrow the foreground
prover.

`--research-workers N` is also the shared live-background-actor bound. Dispatch workers acquire a
cross-process lease before building their agent, and planner delegates acquire the same lease before
constructing a conversation. Nested auxiliary calls reuse that context-local lease. A busy planner
lane defers after a bounded wait and is retried at a later safe orchestration boundary; it does not
remain as an additional resident agent. With `--no-parallel`, process dispatch is disabled while
planner lanes run one at a time.

The research foreground retains `lean-lsp-mcp`, Lean diagnostics/goals, remote Loogle, and native
text-search fallbacks. Its private local Loogle index is disabled by default because it remains a
separate multi-gigabyte resident process alongside the Lean language server and background research
portfolio. `LEANFLOW_RESEARCH_LOCAL_LOOGLE=1` explicitly restores that index for a
memory-provisioned campaign. Non-research workflows keep the established local-Loogle default.

Canonical `lake env lean FILE` verification keeps its 120-second default outside research mode. In
research mode LeanFlow applies a 300-second cold-start floor, so `apply_verified_patch` and parent
file gates do not expire earlier than the incremental checker on large fixtures. The bounded
`LEANFLOW_LEAN_COMMAND_TIMEOUT_S` override may raise this budget but cannot lower the research floor.

Research mode also bounds the lifetime of the primary Lean worker after
`lean_multi_attempt`. The tactic evidence is returned unchanged, then LeanFlow retires that exact
managed `lean-lsp` connection at the first request-idle boundary. Already-admitted concurrent calls
finish under their own tool timeouts; a timed-out coroutine is canceled so it cannot hold retirement
open forever. New calls wait and reconnect lazily across that boundary, so diagnostics, goals,
search, and later multi-attempts remain available without retaining a several-gigabyte post-attempt
peak for an unbounded interval. Set
`LEANFLOW_RESEARCH_RECYCLE_MULTI_ATTEMPT_MCP=0` only for controlled short-run benchmarking where
retaining a warmed server is intentional. Lazy reconnect consumes only the original tool call's
remaining timeout. If retirement fails, LeanFlow retains fail-closed ownership of the old server,
reports the teardown error, and refuses to start an overlapping replacement.
A replacement that exceeds that deadline remains fenced until its canceled startup has completed
transport cleanup, so the next call cannot create a second Lean worker prematurely. Process exit
similarly waits for registered servers, unregistered startups, retire tasks, and startup fences;
any retained identity is reported as native runtime cleanup failure rather than a successful stop.

Manager, orchestrator, verifier, and planner-synthesis model turns use a separate text-only process
boundary. The parent enforces their configured timeout against elapsed wall-clock time and kills
and reaps the isolated process group on timeout or interruption. Provider SDK timeouts are still
forwarded as transport hints, but cannot pin the foreground workflow past the parent deadline.
Structured worker errors are bounded and credential-redacted unconditionally before persistence,
including when optional display redaction is disabled.

Each isolated research job also receives an explicit assignment-scoped recent-history window:
prior worker routes and outcomes, foreground orchestrator routes, and kernel-rejected proof
shapes. The journal read and serialized context are hard-capped, and changing this observational
window does not change the stable route signature used for duplicate suppression. The worker must
compare its result to the supplied records, and the parent retains that context in the structured
deliverable so a later replacement can audit novelty without relying on an opaque digest.

Before a new foreground persistence route is recorded, deterministic semantic admission compares
its strategy family, exact theorem, concrete target hypothesis, and proof-shape evidence with the
campaign's no-progress ledger. Operational prose, generation counters, timestamps, worker ids, and
route hashes are ignored. A duplicate rotates to a distinct viable family; if none remains, the
internal `refresh-portfolio` action requests a fresh epoch and background portfolio and continues.
It never enters the parked-scope path or waits for a provider turn. The action uses the ordinary
crash-durable in-flight marker, retires immediately after checkpointing its rollover request, and
replays once without recharging if the process stopped before application. Kernel-gated graph
progress clears the ledger, while a new mathematical target or proof shape is retained as genuine
novelty.

Research mode applies target-scoped foreground grace after a completed-job event interrupts a safe
read/search boundary. During that grace period LeanFlow continues harvesting and replacing workers,
publishing events, and staging target-matched findings for the prover, but it suppresses another
research-event interruption. A successful foreground turn or authoritative queue/gate boundary
releases grace; changing the assigned theorem resets it. Provider failures, user/signal
interruptions, and research-event interruptions do not consume the owed foreground opportunity.

Foreground research delivery is separately crash-consistent. LeanFlow stages target-scoped FIFO
batches of at most three complete findings and caps each tagged research prompt at 64 KiB. Exact
checked or unchecked replacement text travels alone; a single oversized result stays durable and
unacknowledged while later bounded evidence may still flow. An ordered transcript scan acknowledges
only delivery tokens that precede a later assistant message. Consequently, an internal safe-step
boundary can commit an older consumed batch while leaving findings appended by the newest tool result
pending. Provider failures and user/signal interrupts never acknowledge findings. Every receipt is
scoped to `(job id, foreground target)`, so delivering parent evidence to a split child does not mark
the parent itself delivered.

An exact evidence-to-helper follow-up reserves its source finding from foreground delivery while
active. After termination, only an actionable, schema-valid exact helper or replacement keeps the
source reserved while awaiting harvest; every other result releases it. LeanFlow delivers a
materialized actionable candidate first and couples the source and follow-up receipts after the next
assistant response, preventing duplicate synthesis while preserving crash-consistent redelivery.

Canonical worker-checked helpers also create a durable parent-action record when their foreground
batch is staged. A later assistant response acknowledges delivery only; it does not clear that
record. At the next safe outer boundary LeanFlow reruns the exact helper and axiom profile against
the current file before consulting the orchestrator. A clean result receives one bounded foreground
insertion opportunity, during which broad search/decomposition calls are fenced. Only the ordinary
managed edit guard plus current-source helper gate can bank and retire the record; the assigned
target remains unresolved. A changed source forces recheck, elaboration/axiom rejection retires the
candidate, and operational unavailability remains checkpointed for resume.

Semantic novelty also controls how a consumed finding is rendered, not whether it remains durable.
A finding explicitly classified as duplicate, subsumed, malformed, or otherwise ineligible becomes
`EVIDENCE_ONLY` before prompt sizing. A separate proof-use policy also makes a novel finite
congruence/singleton leaf evidence-only after two rejected proof shapes when the finding explicitly
admits it does not cover the remaining target and has no exact target-closing checked replacement.
That leaf remains in semantic history for deduplication, but cannot fuel another recursive research
refresh. LeanFlow retains its counterexamples, noncoverage facts,
obstructions, issues, and unresolved dependencies, but suppresses worker objectives, candidate code,
helper outlines, target deltas, proof shapes, and action clauses embedded in negative fields behind
audit hashes. Foreground and orchestrator
prompts may use that evidence to exclude spent routes only; it cannot define the next implementation
action or raise queue priority. The ordinary delivery token still acknowledges the record, avoiding
an undrainable evidence backlog.

The consumed dispatch ledger is the lossless finding archive. The prompt-facing
`research_findings` list is an active-scope materialization: at every scope/restart reconciliation it
pages the current theorem into a 32-finding window. Safe same-file split ancestors receive a
three-finding inherited window (one foreground batch), leaving the remaining capacity for the exact
current target. Exact-target results are selected before inherited history.
Switching targets de-stages process-local prompts and may dematerialize an inactive unacknowledged
copy only after its exact ledger payload and hashes validate. Missing, malformed, ambiguous, or
hash-mismatched records are retained and quarantined. Reopening a target rematerializes any archive
record lacking that target's pair-scoped receipt; larger exact-target and inherited backlogs flow in
later bounded pages as acknowledgements free slots. An inherited copy already acknowledged by the
child is dematerialized immediately because the pair-scoped receipt and ledger remain authoritative.

The parent stops launching replacement research jobs only when this active delivery window reaches
32 undelivered findings. An ancestor can occupy at most three slots, so it cannot exert full
backpressure on a new split child. LeanFlow continues to reap already-running workers, reports the
exact target backlog in portfolio status, and records both backpressure and deferred-archive
transitions in workflow activity.

An empirical background JobSpec additionally receives `empirical_compute`, a dedicated
process-isolated exact-arithmetic surface for bounded integer and rational experiments. It does not
receive terminal access: arbitrary Python remains unavailable, and deep-search/decomposition/negation
workers never receive the compute schema. The compute child has no filesystem or process API and is killed at a short hard
timeout under memory and output ceilings.

The dedicated background `decomposition` JobSpec uses the read/check-only `web-research` plus
`lean-research` surfaces and returns a normalized `decomposition_report`. Every proposed subgoal and
`depends_on`/`split_of` edge cites an exact source-basis identifier; malformed or unbacked entries are
dropped or marked incomplete. The subprocess never writes Lean, plan, or graph state and returns no
plan delta. The parent alone decides whether to materialize a proposal through the ordinary
statement-fidelity and kernel gates.

Every scratch archetype receives a non-empty explicit toolset allowlist. `lean-research` preserves
deterministic Lean inspection and inline candidate checking but excludes shared-file patching and the
LLM-backed `lean_reasoning_help`/`lean_decompose_helpers` advisors. Handler-level guards reject those
advisors inside scratch dispatch even if a stale registry surface tries to invoke them.

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
  - `reason`

Queue items are enriched with:

- line numbers
- blocker signatures
- search hints
- verification gates

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
8. The first file is assigned by setting `LEANFLOW_NATIVE_ACTIVE_FILE`.
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

## Document Formalization Preflight

`/formalize docs/paper.tex`, `/autoformalize docs/paper.pdf`, and `/autoformalize docs/paper-directory` normalize to the same `formalize` workflow.

Before launch, the resolver:

1. requires the source path to exist inside the active LeanFlow project
2. accepts `.tex`, `.pdf`, or a directory containing a TeX project
3. for directory inputs, deterministically selects the main `.tex` entrypoint, collects included `.tex` files, bibliography files, and local assets, and fails ambiguous roots with a clear error
4. creates `.leanflow/workflow-state/formalization/<source>/manifest.json`
5. creates `.leanflow/workflow-state/formalization/<source>/extracted.txt`
6. creates `.leanflow/workflow-state/formalization/<source>/blueprint.md`
7. creates an active Lean target file if it does not already exist
8. records both the original request path and the selected source document in workflow state
9. sets `LEANFLOW_WORKFLOW_CONTEXT` so the runner prompt includes the document contract
10. sets `LEANFLOW_NATIVE_ACTIVE_FILE` to the generated target file so statement/source review and later proof work have a stable Lean entrypoint

The generated Markdown blueprint is the default planning artifact. If a project already has `blueprint/` or `leanblueprint` available, the planner should keep that TeX blueprint in sync with the generated declaration names and dependency labels.

## Doctor And MCP

`leanflow doctor` now uses the same capability layer as the Lean workflows.

Supported modes:

- `all`
- `env`
- `mcp`
- `search`
- `migrate`
- `cleanup`

Useful commands:

```bash
leanflow doctor
leanflow doctor mcp --json
leanflow doctor search --json
leanflow mcp bootstrap lean
leanflow mcp status
leanflow mcp status --json
```

`leanflow mcp status` reports server role, managed/install/config health, connection state, registered tools, local Loogle/REPL power-mode status, public remote fallback policy, and sampling counters.

`leanflow mcp bootstrap lean` is the idempotent repair/setup command for the managed Lean MCP stack.

Power-mode details:

- Local Loogle avoids the public Loogle rate limit and is attempted on Linux/macOS/WSL. First local setup may take 5-10 minutes and about 2GB of disk. If it is cold or unavailable, public remote Lean search fallback remains enabled.
- REPL mode makes line-based `lean_multi_attempt` faster after the project has a built `repl` binary. `leanflow project init` attempts safe setup, prints progress for `lake update repl` and `lake build repl`, and continues with LSP fallback if setup fails.
- LeanExplore local mode is preferred when available. Install with `pip install 'leanflow-agent[lean-explore]'` or `pip install 'lean-explore[local]'`, run `lean-explore data fetch`, then use `lean_search mode=semantic|natural-language`. Hosted API mode remains credential-gated through `LEANEXPLORE_API_KEY`.

For persistent sampling audit logs, set `mcp_servers.<name>.sampling.audit_jsonl: true` in `~/.leanflow/config.yaml`. The default path is `~/.leanflow/logs/mcp-sampling.jsonl`, with `audit_jsonl_path` available as an override.

## Persisted Workflow State

Native Lean workflows now persist more than logs and checkpoints.

Relevant files under `.leanflow/workflow-state/` include:

- `live_status.json`
- `activity/`
- `runs/`
- `file_locks.json`
- `outcomes.jsonl`
- `plan.md`, `summary.json`, `blueprint.json`, and `journal.jsonl` for enabled living plan state

At startup, LeanFlow keeps the newly selected run JSONL lossless and hot, then streams provably
closed historical run and mirrored-agent JSONLs into `activity/archive/**/*.jsonl.gz`. Full raw
bytes remain recoverable there. `activity/historical-summary.json` is a small crash-atomic evidence
index with source/archive and status-shard checksums. Replaceable per-run agent/lifecycle summaries
and bounded recent tails live as streamable JSONL under `activity/historical-runs/`. `/workflow
status` and `/workflow activity` read those uncompressed shards plus live streams only; they never
fingerprint or open the gzip evidence. Archive and status-shard commits precede index commit, which
precedes source unlink, so startup can safely retry any interrupted boundary. Any recorded live
parent or child process identity vetoes the run transaction.

At process launch, the native runner immediately claims `live_status.json` with its current PID,
launch-token fingerprint, process-group/session identity, and heartbeat. The phase advances through
`starting` and `reconciling` before the potentially expensive checkpoint, plan, queue, and Lean
preflight work. Any retained theorem, diagnostic, or `sorry` fields are the previous durable
snapshot while `startup_reconciliation_pending` is true; the first rebuilt live proof state replaces
the snapshot and clears that marker. Shell status renders those retained proof fields as a prior
durable snapshot pending reconciliation rather than presenting them as current Lean truth.

The prover sees `plan.md` through an 8,000-character generated file-tool view. It prioritizes the
Goal, Current state, Strategy, and Frontier prefix while retaining the recent decision/final-report
tail; every shortened view reports its source hash and source/returned/omitted character counts.
The canonical `## Notes` heading and user-owned historical body are hidden, and offset pagination
into them is rejected. Current queue assignment and Lean source/kernel diagnostics remain
authoritative over stored plan or dependency-graph declaration snapshots. Model-facing raw reads
of `summary.json` and
`blueprint.json` are also rejected because these machine snapshots can grow with historical
ledgers; managed prompts receive bounded graph and finding digests instead. Raw artifact inspection
is reserved for explicit operator diagnostics with `LEANFLOW_DIAGNOSTIC_FILE_ACCESS=1`.
The research orchestrator further scopes graph context to the current assignment: explicit target
dependencies and the campaign-global scheduling frontier are rendered separately, proved same-file
declarations carry conclusion-shape compatibility labels, and unsupported dependency references in
an LLM route are discarded in favor of the deterministic floor.
Its advisory prompt is capped at 12,000 characters. The assigned declaration, error-bearing target
diagnostics, floor decision, and strict reply contract reserve space first; graph facts, failed
routes, findings, generated plan state, and phase policy use explicit section caps with full-source
hash/count omission telemetry. The isolated consult has a twenty-second research ceiling (the
normal orchestrator timeout setting may lower it), after which the deterministic floor resumes and
the project-local cooldown circuit suppresses repeated waits.

Terminal `live_status.json` snapshots record the truthful `exit_code` and `reason` alongside
the final mathematical state. A new startup clears those process-outcome fields while retaining
the prior proof snapshot for reconciliation.
After a signal stops owned writers, the runner refreshes the current queue assignment and
source-derived file/project `sorry` counts without starting Lean, MCP, or a provider. The exit-130
checkpoint and terminal live status therefore describe the bytes in the linked quiescent snapshot,
even when the outer loop was interrupted before receiving a completed followup state.

For project-scoped `/prove`, `live_status.json` stores `project_prove_manager`, `project_prove_file_queue`, `project_prove_completed_files`, `project_prove_plan_source`, and `project_prove_plan_reason` alongside the normal active-file, queue, diagnostics, build, route, checkpoint, and provider/model fields.

The activity JSONL stream records project prove-manager events:

- `project-prove-file-queue-planned`
- `project-prove-file-assigned`
- `project-prove-file-queue-empty`
- `project-prove-file-queue-complete`

`project-prove-file-queue-planned` contains the candidate metrics and final ordered file labels. `project-prove-file-assigned` contains the assigned file, absolute path, remaining queue, plan source, and plan reason. These events are intended to be machine-readable enough for detailed inspection and offline training trace curation.

`outcomes.jsonl` records route decisions so later cycles and resumed sessions can reuse prior blocker classifications and search/repair history.
