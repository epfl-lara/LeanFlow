---
id: prove
kind: workflow
title: Prove
summary: Queue-driven autonomous theorem proving with LSP-first inspection, native search fallbacks, worker escalation, and strict verification gates.
aliases: [autoprove]
skills: [lean-proof-loop, lean-theorem-queue-worker]
tools: [lean_capabilities, lean_inspect, lean_search, lean_proof_context, lean_auto_search, lean_multi_attempt, lean_verify, lean_sorries, lean_axioms, lean_worker_dispatch]
workers: [proof-repair, axiom-eliminator, sorry-filler-deep]
review_actions: [continue, replan, redraft, falsify, stop]
stop_conditions: [verified, blocked, interrupted, stalled]
route_actions: [queue-worker, final-sweep, delegate-proof-repair, delegate-axiom-eliminator, delegate-sorry-filler-deep]
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
- checkpoint/save-point work
- declaration drafting with no proving intent
- post-compilation simplification where the theorem already compiles cleanly

Use `review`, `checkpoint`, `draft`, `refactor`, or `golf` for those cases.

## Tool Order

1. `lean_capabilities`
   - use first to see whether diagnostics MCP, search providers, and workers are actually available
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
   - if 3 search attempts in a row return no usable result, stop searching and either make the best concrete proof/edit attempt you have or escalate the blocker
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
7. edit the current target minimally
   - queue-driven runs should change one declaration-sized unit at a time
   - local helper lemmas are allowed when they directly unblock the assigned declaration
8. `lean_worker_dispatch`
   - use only when the route decision or blocker history points to a specialist worker
   - do not delegate by default
9. `lean_verify`
   - use the narrowest verification mode that matches the current gate
   - do not treat `grep`, truncated logs, or disappearing `sorry` text as verification
10. `lean_sorries` or `lean_axioms`
   - use when the blocker is global `sorry` inventory or axiom risk rather than local proof construction

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
  - default route: focused local repair, then `proof-repair` if repeated
- search blocker
  - missing lemma or unknown proof shape
  - default route: `lean_search` before rewriting the proof blindly
  - after repeated empty searches, stop theorem-name fishing and either try the most plausible local step, call `lean_proof_context`, or escalate as stuck
- axiom-risk blocker
  - proof compiles but the axiom profile is unacceptable or unknown
  - default route: `lean_axioms`, then `axiom-eliminator` if needed
- stuck queue item
  - same blocker persists after repeated focused attempts or search is exhausted
  - default route: bounded escalation to `sorry-filler-deep`
- final-sweep blocker
  - queue emptied but the file or project still has warnings, malformed proof fragments, or residual diagnostics
  - default route: whole-file or whole-project cleanup pass, then verification

## Worker Escalation

The router chooses the route. This spec explains how to interpret it.

- `proof-repair`
  - use for repeated compiler-style blockers after local direct fixes stop improving the state
  - keep the scope narrow and verify frequently
- `axiom-eliminator`
  - use when the proof shape is acceptable but the axiom report is not
  - preserve theorem meaning while reducing custom axiom dependence
- `sorry-filler-deep`
  - use when a queue item remains stuck after repeated search-backed attempts
  - stay inside the active file unless the workflow explicitly widens scope

If the runner recommends a worker, the normal next step is:

1. confirm the blocker still matches the route on the refreshed state
2. dispatch the worker with a concrete goal
3. re-check the same declaration after the worker result lands

## Stop Conditions

Stop only when one of these is true:

- the requested scope is verified
- a concrete hard blocker has been recorded and another focused attempt is not justified
- the workflow was interrupted
- progress is stalled and the next step is a clear handoff, not another speculative edit

Do not stop merely because:

- one theorem was fixed
- `sorry` text disappeared in one location
- the current file looks cleaner but the verification gate has not been satisfied

## Handoff Format

When the workflow cannot finish in the current turn, leave a compact handoff that includes:

- active file
- declaration or scope still blocked
- blocker kind
- last successful verification gate
- search modes/providers already tried
- failed-attempt summary
- recommended worker or next route action

The handoff should be short, factual, and ready for the next autonomous cycle.
