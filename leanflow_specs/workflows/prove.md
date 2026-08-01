---
id: prove
kind: workflow
title: Prove
summary: Queue-driven autonomous theorem proving with LeanProbe-first checking, native search fallbacks, helper decomposition, and strict verification gates.
aliases: [autoprove]
skills: [lean-proof-loop, lean-theorem-queue-worker]
tools: [lean_capabilities, lean_inspect, lean_incremental_check, lean_search, lean_proof_context, lean_auto_search, lean_multi_attempt, lean_extract_have, lean_decompose_helpers, lean_reasoning_help, lean_verify, lean_sorries, lean_axioms, web_search, web_fetch, web_download]
workers: []
review_actions: [continue, decompose, plan, negate, re-state, ask-human]
stop_conditions: [verified, disproved, cancelled, paused-infrastructure]
route_actions: [queue-worker, final-sweep]
phases: [phase-search, phase-draft]
---

# Native Prove Spec

Use `/prove` or `/autoprove` for the same autonomous workflow.

## When To Use

Use this workflow when the goal is to repair or complete Lean proofs until the requested scope is actually verified.

Typical inputs:

- a specific Lean file
- a project-wide proving run
- a resumed proving session with existing queue state
- optional supplemental skills via `--additional-skill path/to/SKILL.md`, including generated formalization blueprint skills

## What Not To Use This Workflow For

Do not use this workflow for:

- pure review-only work with no intent to change proofs
- save-point work (persisted checkpoints are automatic in managed runs)
- declaration drafting with no proving intent
- post-compilation simplification where the theorem already compiles cleanly

Use `review`, `draft`, `refactor`, or `golf` for those cases.

## Tool Order

1. `lean_capabilities`
   - use first to see whether diagnostics MCP, search providers, and helper tools are actually available
   - do not assume LSP-backed goals or semantic search exist on this machine
2. `lean_inspect`
   - use to read diagnostics, goals, blocker kind, queue items, and the current capability snapshot
   - do not start proving from stale terminal output when `lean_inspect` can give a structured state
3. `lean_incremental_check`
   - use LeanProbe as the normal inner-loop checker for the assigned declaration
   - use `action=check_target` for exact declaration candidates and
     `action=feedback, include_tactics=true` when diagnostics alone do not expose the next goal
   - keep canonical Lake checks for final sweeps, explicit milestones, or unavailable/crashed
     LeanProbe sessions; a bounded candidate timeout is a rejected attempt, not a reason to start an
     unbounded duplicate check
4. `lean_search`
   - use before guessing theorem names, imports, or proof shapes
   - prefer the smallest relevant mode:
     - `local` for nearby project facts
     - `semantic` or `natural-language` for library discovery
     - `type-pattern` when the goal shape matters most
   - do not loop on compiler failures caused by missing lemmas before searching
   - in a managed file-scoped assignment, results marked
     `source_access=future_same_file_unavailable` are source-order evidence only: never submit
     them to tactic screening. Use prior same-file or imported declarations instead
- the empty-search budget and provider order are the `phase-search` contract: if 3 search attempts in a row return no usable result, stop searching and either make the best concrete proof/edit attempt you have or report a blocker with a requested route
   - treat `repeated empty search loop detected` in `degraded_reasons` as a hard signal to stop searching in this turn
5. `lean_proof_context`
   - use when theorem-local search is exhausted, attempt history is nonzero, or the blocker looks automation-suited
   - this is theorem-context retrieval: theorem statement, original proof, hypotheses, in-scope names, namespace, and similar proofs
   - do not treat it as a replacement for `lean_inspect` goals/diagnostics
6. `lean_auto_search`
   - use only after proof context or concrete local evidence exists and the theorem is still blocked
   - this is for one theorem-local automated candidate search, not broad queue triage
7. `lean_multi_attempt`
   - use only with a known proof location and 2-6 short local tactic candidates
   - do not use it for vague search, speculative whole-proof generation, declaration headers, or candidates containing `sorry`
   - if you have one full candidate proof, screen it with LeanProbe, patch the file, and let the
     managed post-edit LeanProbe gate check the assigned declaration
8. `lean_extract_have`
   - use after repeated bounded timeouts when the current declaration already contains a substantial
     sorry-free local `have`: the tool automatically selects the largest eligible block unless a name
     is supplied, recovers its exact context with Mathlib's `extract_goal`, verifies the generated
     private lemma and rewritten call site independently with LeanProbe, and commits only that checked
     source transition
   - prefer this mechanical extraction before asking a model to restate proof-specific helper
     signatures; a failed extraction leaves source unchanged and returns structured diagnostics
9. `lean_decompose_helpers`
   - use when a theorem is hard because the direct proof needs intermediate invariants, helper lemmas, or an affine/algebraic split before editing will be productive
   - after repeated bounded verification timeouts on a sorry-free declaration, stop replaying the unchanged broad check; decompose into cohesive top-level helpers, verify each helper independently with LeanProbe, and retry the parent only after its body is materially smaller
   - call it after focused search/proof-context work has identified the obstacle but before inserting theorem-sized comment blocks, placeholder `sorry`, or broad speculative helper declarations
   - pass the exact theorem statement, current diagnostics/goals, current attempt, and a concise failed-attempt summary
   - treat returned helpers as checked decomposition advice: insert only `ready_to_insert` skeletons deliberately, then prove each helper without lingering `sorry`
10. edit the current target minimally
   - queue-driven runs should change one declaration-sized unit at a time
   - declaring local helper lemmas is allowed/encouraged when they directly unblock the assigned declaration
11. `lean_reasoning_help`
   - use for broad proof-strategy advice when the missing piece is conceptual or library-navigation oriented
   - prefer `lean_decompose_helpers` instead when the useful next step is a structured sublemma split
   - treat an open-problem or blocker assessment as unverified route-change evidence, never a terminal verdict; the tool removes surrendering conclusion fragments and appends a deterministic continuation contract
12. `lean_verify`
   - use the narrowest verification mode that matches the current gate
   - do not treat `grep`, truncated logs, or disappearing `sorry` text as verification
13. `lean_sorries` or `lean_axioms`
   - use when the blocker is global `sorry` inventory or axiom risk rather than local proof construction
14. `web_search` / `web_fetch` / `web_download` (external research — for HARD or unfamiliar problems)
   - after local `lean_search` is exhausted and the obstacle is conceptual or needs outside knowledge, use `web_search` for the open web, code (Sourcegraph/GitHub), and papers (arXiv/Semantic Scholar): find a known proof, a prior formalization, a similar result, or the right lemma/technique
   - use `web_fetch <url>` to actually READ a promising page or PDF, and `web_download <url>` to save a paper/artifact (then `read_pdf` it)
   - this is research to inform the Lean proof, never a substitute for verification — the theorem is only solved when the verification gate passes

## Queue Contract

For file-scoped autonomous runs, the runner owns the declaration queue and the agent owns only the current assignment.

When a queue item is assigned:

- work only on that declaration until it is solved; a concrete blocker changes the route but does
  not end the assignment
- do not start the next theorem just because it is nearby in the file
- treat failed-attempt history as negative guidance
- after a meaningful edit, expect the runner to refresh diagnostics and queue state before the next large move

When no queue item is assigned:

- use the refreshed structured state to determine whether the workflow needs a final file sweep, a module/project verification pass, or a blocker handoff

## Plan-State Freshness

When living plan artifacts are enabled, the deterministic queue assignment plus current Lean
source/kernel diagnostics outrank stored plan or dependency-graph declaration bodies. The managed
`plan.md` file-tool view is bounded to generated sections plus the existing canonical `## Notes`
append anchor; never create another Notes heading and never paginate it or otherwise expose its
hidden body. The preserved
Notes tail is user-owned historical context, not current sorry inventory, helper
inventory, declaration truth, or permission to revisit an obsolete proof shape.
Do not read raw `summary.json` or `blueprint.json`; their machine state is supplied through the
bounded graph digest, completed-finding handoff, and deterministic queue context.

## Verification Ladder

Verification is layered. Use the smallest gate that is truthful for the current turn.

1. Per-edit gate
   - `lean_incremental_check(action=check_target)`
   - this LeanProbe-backed exact declaration check is the default acceptance gate for a managed
     theorem turn; use `lean_inspect` and incremental feedback for diagnostics and goal inspection
2. File acceptance gate
   - `lean_verify(mode=file_exact)`
   - for file-scoped theorem turns, this is the only acceptable final proof acceptance check
   - do not substitute `lake build`, `grep`, `head`, or partial terminal output
3. Module milestone gate
   - `lean_verify(mode=module)`
   - use when the active file is close to clean and a focused module build is cheaper than repeated exact-file checks
4. Project completion gate
   - `lean_verify(mode=project)`
   - required before declaring a project-scoped proving workflow complete

A proving workflow is verified only when the requested scope has:

- explicit successful Lean verification
- clean diagnostics
- no open goals
- no remaining `sorry`

## Blocker Taxonomy

Use the blocker kind from `lean_inspect` and the route decision from the runner as the primary signals.

- compiler-style blocker
  - type mismatch
  - unknown identifier
  - failed instance synthesis
  - timeout / tactic failure with clear compiler output
  - default route: focused local repair with richer local feedback if repeated
- search blocker
  - missing lemma or unknown proof shape
  - default route: `lean_search` before rewriting the proof blindly
  - after repeated empty searches, stop theorem-name fishing and either try the most plausible local step, call `lean_proof_context`, call `lean_decompose_helpers` when the proof needs sublemmas, or escalate as stuck
- decomposition blocker
  - the proof is mathematically plausible but too large to attack directly, repeated searches are broad, or the next productive edit is a helper lemma/invariant split
  - default route: call `lean_decompose_helpers` for ordered helper skeletons and proof hints, then patch and verify those helpers one at a time
  - do not replace this with comments plus `sorry`; if the helper skeleton is not ready to insert, report the failed skeleton diagnostics as blocker context
- false generated-helper blocker
  - a decomposer- or planner-owned helper has a concrete boundary counterexample, or its proof needs an
    indispensable parent hypothesis omitted from its statement
  - default route: request `negate` with the exact counterexample or missing-hypothesis evidence; do not
    fabricate the premise or keep expanding an impossible proof
  - the kernel-backed false-decomposition cleanup retires the invalid subtree and restores the parent for a
    sound split
- axiom-risk blocker
  - proof compiles but the axiom profile is unacceptable or unknown
  - default route: `lean_axioms`, then direct proof cleanup if needed
- stuck queue item
  - same blocker persists after repeated focused attempts or search is exhausted
  - default route: use feedback, helper decomposition, or reasoning help before reporting a concrete blocker
- final-sweep blocker
  - queue emptied but the file or project still has warnings, malformed proof fragments, or residual diagnostics
  - default route: whole-file or whole-project cleanup pass, then verification

## Stuck-Proof Handling

When repeated local attempts fail, keep escalation inside the active tool surface:

1. request richer local feedback with `lean_incremental_check(action=feedback, include_tactics=true)`
2. use `lean_decompose_helpers` when the proof needs intermediate invariants or helper lemmas
3. use `lean_reasoning_help` when the blocker is conceptual or library-navigation oriented
4. report a blocker with a requested route (`decompose` | `negate` | `plan`) if another edit would
   only repeat failed proof shapes; the manager must route that evidence and continue

Every rejected prover turn receives a persistence-coach message. The coach may acknowledge
kernel-verified progress and reinforce the assigned route only. It cannot choose strategy, launch
jobs, alter a verifier verdict, or recommend stopping. If the coach model is disabled, malformed,
surrendering, or unavailable, the deterministic positive fallback is still applied.

Preserve accumulated proof work. Never use `git restore`, `git checkout`, `git reset`, or an
equivalent bulk reversion to discard a partially verified declaration. Rework the active proof with
managed patches; only the deterministic manager may restore a captured safe baseline, and it must
record the failed attempt before doing so.

## Stop Conditions

Stop only when one of these is true:

- the requested scope is verified
- the main statement has been authoritatively disproved by a promoted Lean negation
- the user explicitly cancels the campaign
- the provider/runtime remains unavailable after retry and the checkpointed campaign pauses for
  infrastructure recovery

Transient provider failures use exactly three interruptible retries with 5/15/45-second backoff.
Only failure of the fourth total provider attempt may trigger the infrastructure-pause condition.

Do not stop merely because:

- one theorem was fixed
- `sorry` text disappeared in one location
- the current file looks cleaner but the verification gate has not been satisfied
- a hard blocker, retry budget, route budget, or cycle boundary was reached
- the prover says “NOT SOLVED”, “cannot proceed”, or otherwise tries to surrender

## Orchestration

The run is supervised. Artifacts and routes exist — use them instead of
improvising strategy:

- `plan.md`, `blueprint.json` (the dependency graph), and `summary.json`
  live in the workflow state; decision packets record every budget
  breakpoint. Read `plan.md` when it is injected — Strategy and Grounding
  are written by the planner phase.
- Under the orchestrator, stalls, completed local feedback windows, and budget breakpoints are ROUTED
  (decompose, plan, negate, or re-state); they never form a mathematical stop. `park` is reserved
  for a statement-fidelity or human-approval pause. A blocker report must carry
  a requested route and the evidence for it — the orchestrator consumes
  the request as a suggestion.
- Helper stubs stated above your target are the next queue assignments;
  prove them first, then assemble the target.
- The kernel gate remains the only acceptance authority; orchestration
  never overrides it.

## Research Profile

Use `leanflow workflow prove FILE --provider codex --research` for the complete research profile.
It enables plan state, retrieval, breakpoints, deterministic and LLM orchestration, fidelity
auditing, graph frontier management, planner lanes, dispatch, negation probing, reports, learnings,
and coaching. `--research` starts two background research workers by default;
`--research-workers N` changes the capacity and implies research. `--no-parallel` sets background
capacity to zero while keeping sequential research routing.

The foreground proof attempt starts immediately. One grounding/deep-search job starts at scope
entry; after two rejected proof attempts, the default second lane explores empirical feasibility.
Semantic saturation rotates that lane first to negation and then, after an inconclusive/spent
negation direction, to process-isolated decomposition. A decomposition child returns only a
source-backed `decomposition_report` of subgoal/dependency proposals; the parent remains the sole
Lean/plan/graph writer. Completed findings are consumed once and the slot is refilled with an
assignment-distinct objective while the goal remains unresolved.
An exact evidence-to-helper follow-up reserves its source from foreground delivery while active.
After termination, only an actionable, schema-valid exact helper or replacement keeps the source
reserved while awaiting harvest; every other result releases it. A materialized actionable candidate
is delivered first and couples both receipts after the next assistant response.
The lossless dispatch ledger may contain much more evidence than the prompt cache. The active
target cache is bounded at 32 undelivered findings, with only one three-finding batch reserved for
safe split-ancestor evidence; exact-target findings take priority so inherited history cannot block
scope-entry research or replacement workers.

When a completed job supplies a canonical checked helper, delivery acknowledgement is not action.
The deterministic parent persists the exact candidate, reruns `check_helper` plus its axiom profile
against current source before orchestration, and grants one immediate insertion opportunity. During
that opportunity do not search, decompose, or synthesize a different helper. Insert the exact
parent-accepted declaration through the managed patch path, let the current-source helper gate bank
it, then continue the still-unresolved target. Source drift causes a recheck; infrastructure failure
is resumable; only a genuine elaboration or axiom rejection discards the candidate.

The 120-cycle limit is per campaign epoch, not per mathematical campaign. Four no-progress route
decisions or context pressure also roll a fresh epoch. Epoch rollover checkpoints the graph, plan,
job ledger, findings, verified helpers, and failed proof shapes, then starts a distinct route
portfolio in a fresh model context under the same campaign ID. If infrastructure pauses before the
selected fresh-epoch route completes a managed turn, resume reuses that exact token-bound route
without charging a duplicate no-progress decision.
Semantic-cooldown evidence remains durable across rollover. If ordinary uncooled selection cannot
fill background capacity, only a cooldown produced in an older epoch may be relaxed, and the new
job must still have an assignment-distinct route objective and signature. Never relax a cooldown
produced in the current epoch.

## Process Outcomes

Headless prove/autoprove uses these exit codes:

- `0`: requested scope kernel-verified with no unresolved `sorry`
- `3`: main statement authoritatively disproved
- `2`: unresolved but checkpointed/resumable pause
- `1`: configuration/runtime failure before a valid campaign starts
- `130`: signal interruption

Before an exit-`130` checkpoint is written, owned writers quiesce and the runner refreshes the
durable queue assignment plus source-derived `sorry` counts without starting Lean, MCP, or another
provider request.

## Handoff Format

When the workflow cannot finish in the current turn, leave a compact handoff that includes:

- active file
- declaration or scope still blocked
- blocker kind
- last successful verification gate
- search modes/providers already tried
- failed-attempt summary
- next route action

The handoff should be short, factual, and ready for the next autonomous cycle.
