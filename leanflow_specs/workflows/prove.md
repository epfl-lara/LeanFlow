---
id: prove
kind: workflow
title: Prove
summary: Queue-driven autonomous theorem proving with LSP-first inspection, native search fallbacks, helper decomposition, and strict verification gates.
aliases: [autoprove]
skills: [lean-proof-loop, lean-theorem-queue-worker]
tools: [lean_capabilities, lean_inspect, lean_search, lean_proof_context, lean_auto_search, lean_multi_attempt, lean_decompose_helpers, lean_reasoning_help, lean_verify, lean_sorries, lean_axioms, web_search, web_fetch, web_download]
workers: []
review_actions: [continue, decompose, plan, negate, re-state, park]
stop_conditions: [verified, blocked, interrupted, stalled]
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
3. `lean_search`
   - use before guessing theorem names, imports, or proof shapes
   - prefer the smallest relevant mode:
     - `local` for nearby project facts
     - `semantic` or `natural-language` for library discovery
     - `type-pattern` when the goal shape matters most
   - do not loop on compiler failures caused by missing lemmas before searching
- the empty-search budget and provider order are the `phase-search` contract: if 3 search attempts in a row return no usable result, stop searching and either make the best concrete proof/edit attempt you have or report a blocker with a requested route
   - treat `repeated empty search loop detected` in `degraded_reasons` as a hard signal to stop searching in this turn
4. `lean_proof_context`
   - use when theorem-local search is exhausted, attempt history is nonzero, or the blocker looks automation-suited
   - this is theorem-context retrieval: theorem statement, original proof, hypotheses, in-scope names, namespace, and similar proofs
   - do not treat it as a replacement for `lean_inspect` goals/diagnostics
5. `lean_auto_search`
   - use only after proof context or concrete local evidence exists and the theorem is still blocked
   - this is for one theorem-local automated candidate search, not broad queue triage
6. `lean_multi_attempt`
   - use only with a known proof location and 2-6 short local tactic candidates
   - do not use it for vague search, speculative whole-proof generation, declaration headers, or candidates containing `sorry`
   - if you have one full candidate proof, patch the file and finish with `lean_verify`
7. `lean_decompose_helpers`
   - use when a theorem is hard because the direct proof needs intermediate invariants, helper lemmas, or an affine/algebraic split before editing will be productive
   - call it after focused search/proof-context work has identified the obstacle but before inserting theorem-sized comment blocks, placeholder `sorry`, or broad speculative helper declarations
   - pass the exact theorem statement, current diagnostics/goals, current attempt, and a concise failed-attempt summary
   - treat returned helpers as checked decomposition advice: insert only `ready_to_insert` skeletons deliberately, then prove each helper without lingering `sorry`
8. edit the current target minimally
   - queue-driven runs should change one declaration-sized unit at a time
   - declaring local helper lemmas is allowed/encouraged when they directly unblock the assigned declaration
9. `lean_reasoning_help`
   - use for broad proof-strategy advice when the missing piece is conceptual or library-navigation oriented
   - prefer `lean_decompose_helpers` instead when the useful next step is a structured sublemma split
10. `lean_verify`
   - use the narrowest verification mode that matches the current gate
   - do not treat `grep`, truncated logs, or disappearing `sorry` text as verification
11. `lean_sorries` or `lean_axioms`
   - use when the blocker is global `sorry` inventory or axiom risk rather than local proof construction
12. `web_search` / `web_fetch` / `web_download` (external research — for HARD or unfamiliar problems)
   - after local `lean_search` is exhausted and the obstacle is conceptual or needs outside knowledge, use `web_search` for the open web, code (Sourcegraph/GitHub), and papers (arXiv/Semantic Scholar): find a known proof, a prior formalization, a similar result, or the right lemma/technique
   - use `web_fetch <url>` to actually READ a promising page or PDF, and `web_download <url>` to save a paper/artifact (then `read_pdf` it)
   - this is research to inform the Lean proof, never a substitute for verification — the theorem is only solved when the verification gate passes

## Queue Contract

For file-scoped autonomous runs, the runner owns the declaration queue and the agent owns only the current assignment.

When a queue item is assigned:

- work only on that declaration until it is solved or concretely blocked
- do not start the next theorem just because it is nearby in the file
- treat failed-attempt history as negative guidance
- after a meaningful edit, expect the runner to refresh diagnostics and queue state before the next large move

When no queue item is assigned:

- use the refreshed structured state to determine whether the workflow needs a final file sweep, a module/project verification pass, or a blocker handoff

## Verification Ladder

Verification is layered. Use the smallest gate that is truthful for the current turn.

1. Per-edit gate
   - `lean_inspect`
   - this is the default iteration tool for diagnostics, goals, and blocker classification
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
4. report a blocker with a requested route (`decompose` | `negate` | `plan`) if another edit would only repeat failed proof shapes

## Stop Conditions

Stop only when one of these is true:

- the requested scope is verified
- a hard blocker has been recorded WITH its requested route and another focused attempt is not justified
- the workflow was interrupted
- progress is stalled and the next step is a clear handoff CARRYING a requested route, not another speculative edit

Do not stop merely because:

- one theorem was fixed
- `sorry` text disappeared in one location
- the current file looks cleaner but the verification gate has not been satisfied

## Orchestration

The run is supervised. Artifacts and routes exist — use them instead of
improvising strategy:

- `plan.md`, `blueprint.json` (the dependency graph), and `summary.json`
  live in the workflow state; decision packets record every budget
  breakpoint. Read `plan.md` when it is injected — Strategy and Grounding
  are written by the planner phase.
- Under the orchestrator, stalls, retry exhaustion, and budget
  breakpoints are ROUTED (decompose, plan, negate, re-state, park);
  classic runs still stop on them — either way the handoff carries a
  requested route. A blocker report must carry
  a requested route and the evidence for it — the orchestrator consumes
  the request as a suggestion.
- Helper stubs stated above your target are the next queue assignments;
  prove them first, then assemble the target.
- The kernel gate remains the only acceptance authority; orchestration
  never overrides it.

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
