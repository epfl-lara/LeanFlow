# LeanFlow `/prove` Next-Gen Architecture — Shipped Roadmap v4

Status: **implemented on `prove-redesign`; promotion evidence in progress.** v1 = grounded design
(4-agent study). v2 folded in the product owner's answers. v3 (2026-07-02) adversarially audited
the plan. **v4 (2026-07-14) records the promoted relentless-prover implementation:** persistence
coaching on every rejected turn, a public research profile, process-isolated research jobs,
campaign epochs, authoritative negation promotion, truthful headless exits, and frozen T2/T3/
adversarial evaluation inventories. The historical phase ordering remains below as a rollout
record; it is no longer a list of unimplemented phases. v3 went beyond the owner's
comments: every design choice was adversarially verified against the north-star goal by three
independent audit agents, and every phase was ground into an implementation-ready spec by four
deep-grounding agents.** This document is the plan; its two companions carry the depth:

- **`prove-redesign-implementation-specs.md`** — per-phase, function-level specs verified against
  the tree (Parts I–III) + the definitive existing-assets reuse inventory (Part IV).
- **`prove-redesign-audit.md`** — the full audit evidence: red-team of 18 design choices (A),
  goal-derived requirements coverage incl. the evaluation harness (B), SOTA frontier cross-check (C).

**The north star** (owner's words): an autonomous research harness for the hardest, possibly fully
open problems — pushing beyond current knowledge, never silently giving up, always ending in
kernel-verified results or a rigorously documented frontier. Correctness is absolute (Lean kernel
only, no axiom cheating); for research runs, token cost is not the constraint.

**Existence proof for the north star:** *Automated Conjecture Resolution* (arXiv:2604.03789,
Apr 2026) resolved an open problem in commutative algebra and formally verified it in Lean 4
autonomously — using exactly this architecture's role split (informal reasoner / formal prover /
retriever). The 2026 frontier confirms the shape (audit Part C); the highest-profile 2026 result
(the Erdős-90 unit-distance **disproof**) validates the negation arm as co-equal.

---

## 0. Locked decisions (v3)

1. **Orchestrator = SMART / strong model, expanded mandate.** Routes by difficulty; decides splits;
   **proposes and STATES new lemmas** (private/public, same/other/new files); **creates and
   organizes files** (lays out `sorry`-stub theorems so queue-prove jobs can run over them; a
   one-lemma file is legitimate for a hard probe); orders probes; interprets failures via the
   graph. Never a cheap model — nobody at the frontier routes consequential decisions to a small
   model (audit C). All roles configurable in `config.yaml` (§4.8).
2. **Persistence coach = small/fast, ADVISORY-ONLY, message-shaping only.** It runs after every
   kernel-rejected prover turn and every unresolved/no-tool final response, deduplicated per
   theorem/attempt/verdict. Its schema contains only a message, acknowledged verified progress,
   and a commitment to the assigned route. It has no action or verdict vocabulary. Disabled,
   malformed, surrendering, or unavailable model output receives the deterministic positive
   fallback, so coach coverage remains 100%. Kernel verdicts and routes are untouchable.
3. **Routing is TASK-ADAPTIVE with a deterministic floor — which INVERTS in research mode.** Easy
   runs stay byte-identical (`direct-prove` floor, no LLM call). In `RESEARCH_MODE` the orchestrator
   is consulted unconditionally at scope entry (an open problem is syntactically indistinguishable
   from a homework lemma; audit A, choice 3), and hardness/splits are discovered dynamically
   through failure→probe cycles.
4. **Budget exhaustion is a routing BREAKPOINT, never a mathematical stop.** Per-theorem and
   queue-level exhaustion persists a decision packet. The orchestrator selects split, plan,
   negate, re-state, or a fresh route portfolio. `park` is reserved for statement-fidelity or
   human-approval pauses; only promoted negation of the main goal resolves as disproved.
5. **The failure→feasibility FEEDBACK LOOP** (Hilbert-style recursion): can't-close ⇒ (a) too hard
   or (b) not solvable. Feasibility pipeline (v3, audit A choice 10): **EMPIRICAL counterexample
   search first** (Python/enumeration — the strongest engine), then `plausible` where applicable
   (hint only), then a bounded formal `¬P` attempt. Negation proves on a sub-lemma ⇒ decomposition
   was wrong ⇒ backtrack/re-decompose (non-destructively, via OR-routes §4.1). On the main
   statement ⇒ **not solvable ⇒ kernel-verified disproof is the deliverable**. Inconclusive ⇒ too
   hard ⇒ split.
6. **Dispatch = production process-isolated sub-JOB launching at multiple levels.**
   Orchestrator, planner, and decomposer can dispatch ANY archetype (prover / empirical /
   deep-search / negation / decomposition); the LLM-manager suggests; the prover escalates. **v3 (audit A choice
   9): dispatched jobs get INDEPENDENT budgets** (never the prover's shared iteration budget) and
   wall-clock timeouts. Deep-search, empirical, decomposition, and negation jobs are
   fire-and-continue subprocesses in research mode; children return structured deliverables and
   the parent is the sole graph/plan writer. The decomposition deliverable is a bounded normalized,
   source-backed subgoal/dependency proposal: its web/Lean tools are genuinely read/check-only, its
   accepted subgoals must connect to the target and state why they are strictly easier, and malformed
   output cannot become a successful finding. It cannot mutate files or graph state, and duplicate
   assignment objectives are suppressed. Exact evidence-to-helper follow-ups reserve their source
   while active; after termination, only an actionable, schema-valid exact helper or replacement
   keeps the reservation, and foreground delivery couples both receipts. A staged canonical helper
   also creates a durable parent-action record: acknowledgement is not completion, the parent exact
   gate runs before orchestration, and one fenced insertion opportunity precedes broad search. Only
   the current-source helper gate retires it. Prover jobs retain locked stubs and parent-side gates.
7. **Hierarchical job LINEAGE (owner N3):** every job id carries its dotted dispatch chain —
   `root.orchestrator.planner.ds-042` — persisted in the ledger; **every ancestor can list, track,
   and kill its descendants.**
8. **TRUTHFUL OUTCOME GUARANTEE (owner N1) — a hard invariant, mechanized.** Mathematical terminal
   states are **proved** (kernel), **disproved** (main-goal negation promoted through the
   authoritative gate), and explicit user cancellation. Infrastructure failure is a resumable
   operational pause. Headless exits are `0=verified`, `3=disproved`, `2=checkpointed unresolved`,
   `1=startup/runtime failure`, and `130=signal`; unresolved `sorry` can never exit zero. A
   provider/API pause after campaign start forces a deterministic post-quiescence filesystem
   checkpoint, without asking the failed provider to summarize it. Negation promotion is a durable
   evidence/graph transaction; restart revalidates current source, signature, proof, and axioms
   before preserving `disproved`, otherwise it quarantines the stale record and resumes proving.
   Signal checkpoints likewise refresh the durable queue assignment and source-derived `sorry`
   counts after writers quiesce, without starting Lean/MCP/provider work during cleanup.
9. **Documentation-driven proving.** Living `plan.md` + `summary.json` + `blueprint.json` (the
   graph) + an append-only `journal.jsonl` (v3; the lab notebook and the source of truth for
   rebuildable snapshots). Every deployed agent receives the artifact paths. The orchestrator may
   skip the apparatus for simple direct tasks; for important tasks it is the crucial feature.
   Checkpoint UX retired; logging stays.
10. **Deep search is a multi-level ability** — search AND download/clone papers, docs, repos,
    libraries (`repo_clone`), available to orchestrator/planner/decomposer and deep-search jobs.
    **v3 (audit C): premise retrieval becomes mandatory from Phase 1** (`lean_lemma_suggest`
    injected at assignment — Hilbert's ablation: +accuracy AND −43% tokens).
11. **Research-pusher stance is a public complete profile.** `--research` (or
    `LEANFLOW_RESEARCH_MODE=1`) enables plan state, retrieval, breakpoints, both orchestrator
    layers, fidelity auditing, graph frontier selection, planner lanes, process dispatch,
    negation probing, reports, learnings, and coaching. Two background workers are the default.
    Explicit CLI activation forces the required feature switches on over stale inherited `0`
    values. Environment-only activation supplies those values as defaults while retaining
    deliberate per-feature diagnostic overrides; router availability never grants surrender
    authority in either form.
    The 120-cycle ceiling, four no-progress routes, and context pressure roll a fresh campaign
    epoch while preserving verified and negative knowledge; they never terminate the campaign.
    A fresh epoch durably requires a distinct non-direct route and replaces still-open workers
    from the spent route portfolio after harvesting completed deliverables. The atomic epoch record
    carries a replayable refresh token: crashes cannot strand old workers, stale route selections
    cannot clear the obligation, and provider failure leaves it pending for resume.
12. **LeanProbe is the guy** — probes, experiments, negation, decomposition validation — with the
    Lake-backed gate (`_manager_check_queue_item`) as the sole acceptance authority. **v3: any
    scratch result that drives an irreversible decision (a `false` mark, a banked lemma) must be
    PROMOTED through the authoritative gate first** (§4.11).
13. **Ground in existing infrastructure** — the reuse inventory (specs Part IV) verifies every
    component composes existing machinery; genuinely new modules are few and listed there.
14. **Decomposition confidence (v3, from live-run forensics §4.10):** provers are measurably afraid
    to decompose. The optionality language is removed, helper-`sorry` is legitimized as
    work-in-progress, kernel-verified helpers earn partial credit, and at attempt ≥ K the route
    *becomes* decompose. The cheap prompt fixes ship immediately (Wave −1).
15. **Evaluation-gated development (v3, audit B):** **Phase E** starts alongside Phase 1 — without
    it, "improves hard-problem capability" is unfalsifiable. Every later phase has a defined gate
    (§4.13); the flag-enable criteria are part of the phase definition.
16. **Human collaboration points:** an `ask-human` route; main-statement re-statements require
    human ACK (via the existing agent inbox); scope-exit reports are surfaced to the user.
17. **Sequencing:** Wave −1 (immediate fixes) → Phase 0 → Phase E + Phase 1 → Phases 2–3 → 4 → 5 →
    6. Every phase flag-gated, dark-launchable, independently shippable.

Non-negotiable correctness invariants: (i) the deterministic Lean-kernel gate is never
LLM-overridable; (ii) orchestrator/decomposer file writes pass the same forbidden-axiom scan as
prover edits (§4.11); (iii) `false` marks and banked lemmas require gate promotion; (iv) `proved`
graph nodes are immutable; (v) every scope ends in a concrete-result artifact (D8).

---

## 1. The unifying vision (v3)

Restructure `/prove` around a **strong-model LLM orchestrator** that, per scope-entry, at every
breakpoint, **and on new evidence** (job findings, graph frontier changes, a research-mode
cadence), decides whether to feed the existing queue directly, decompose — stating new sub-lemmas
into organized files — or run pre-queue planning (explore / search / test / draft), while the
untouched **queue + deterministic manager-prover core keeps proving and remains the sole authority
on correctness**. The orchestrator owns file organization and the **dependency graph** (with
OR-routes for rival decompositions); failures flow back through the Hilbert-style loop
(fail → empirical/`plausible`/negation feasibility → re-decompose | promoted disproof | split).
A small **persistence coach** reinforces the already-selected route after every rejected turn
without touching strategy or verdicts. All agents read and write **living documentation** — plan, summary, graph,
journal — which replaces checkpoint prose as the resume/coordination authority, and **tracked,
lineage-addressed dispatch** launches prover, empirical, and deep-search jobs from any
decision-making level with independent budgets. Every scope terminates in a kernel-verified proof,
a kernel-verified (promoted) disproof, or a machine-written research report — never silence.

---

## 2. Target architecture (v3)

```
 native main() → _drive_autonomous_followups (native_runner.py:9059) — eternal loop, SPINE UNCHANGED
        │
        │ INVOCATIONS: scope-entry · stall · budget breakpoint (§4.3)
        │              + v3: job-done-with-findings · graph frontier change (false/proved
        │                w/ dependents) · RESEARCH_MODE cadence (§4.4)
        ▼
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ (22) ORCHESTRATOR — strong model, context-rich in research mode                │
 │  ROUTE ∈ {direct-prove | decompose | plan | explore | escalate | park |        │
 │           ask-human}                                                           │
 │  POWERS: state lemmas · organize files/stubs · launch queue-prove jobs ·       │
 │  order probes · interpret failures via the graph · guarantee a CONCRETE        │
 │  RESULT (proof | promoted disproof | scope-exit report)                        │
 └────┬────────────────┬──────────────────────┬──────────────────────────────────┘
      │ direct         │ decompose            │ plan / explore
      ▼                ▼                      ▼
 ┌──────────┐   ┌────────────────┐   ┌──────────────────────────┐
 │  QUEUE   │   │  DECOMPOSER    │   │  PLANNER (strong model)  │
 │  PROVE   │◄──┤ graph-aware ·  │   │ fan-out: web · mathlib · │
 │ (existing│   │ states stubs · │   │ empirical · draft ·      │
 │  inner   │   │ LeanProbe-     │   │ feasibility              │
 │  loop)   │   │ validates      │   └──────────┬───────────────┘
 └────┬─────┘   └──────┬─────────┘              │
      │                └───────────┐            │
      ▼                            ▼            ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ (20) PLAN-STATE blackboard — read/written by EVERY agent         │
 │  plan.md · summary.json · blueprint.json (graph w/ OR-routes) ·  │
 │  journal.jsonl (append-only truth; snapshots rebuildable)        │
 └────────────────────────────────────────────────────────────────┘
      │ prover turn → (17) MANAGER GATE (kernel verdict is FINAL)
      │                │ struggle signals (§4.4)? → LLM-nudger (message only)
      ▼                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ (19) DISPATCH SERVICE — one shared lifecycle, ALL levels          │
 │                                                                   │
 │  WHO CAN DISPATCH (owner N2):                                     │
 │    orchestrator ✅   planner ✅   decomposer ✅  — ANY archetype   │
 │    LLM-manager: SUGGEST only · prover: ESCALATE only              │
 │                                                                   │
 │  ARCHETYPES: PROVER (A: mini queue-prove over a stub file — full  │
 │   existing machinery · B: queue-less LeanProbe probe) · EMPIRICAL │
 │   (python/Lean experiments) · DEEP-SEARCH (search/fetch/download/ │
 │   clone → findings) · FEASIBILITY (empirical → plausible → ¬P)    │
 │                                                                   │
 │  LINEAGE (owner N3): root.orchestrator.planner.ds-042 — ancestors │
 │   list / track / kill descendants                                 │
 │  BUDGETS: independent per job (never the prover's) + wall-clock;  │
 │   sync cap-3 v1; deep-search fire-and-continue in research mode   │
 │  LEDGER: proposed→deployed→running→{done|failed|stuck|killed} —   │
 │   persisted, reconciled vs activity/agents, never lost            │
 └────────────────────────────────────────────────────────────────┘
```

**How this EVOLVES the queue + manager-prover** — unchanged: the loop (`native_runner.py:9059`),
the queue (`assign` `queue_manager.py:181`), the gate (`:1890` → `:1210`) stay put; `direct-prove`
is byte-for-byte today's path; dispatch reuses `dispatch_worker` (`lean_worker_dispatch.py:47`,
gate `lean_services.py:140`), `delegate_task` (`delegate_tool.py:391`), and the agent registry
(`workflow_state.py:651`). Corrected seams (grounding pass): `spawn_workflow` is
`workflow.py:564` (env contract `:476-523`); checkpoint handlers are `native_runner.py:9602/9623/
9642` (help text `:3788`); and the plan-state write path **must** use the truly-atomic
`core/utils.py:13 atomic_json_write` — the current `write_json_file` (`workflow_json_io.py:31`) is
NOT crash-atomic and its reader silently swallows corruption (audit A headline; fixed in Phase 1).

---

## 3. What changed v2 → v3 (the audit-driven replan)

| # | Area | v2 | v3 (source) |
|---|---|---|---|
| 1 | Reflection | failure-driven only (scope-entry/stall/breakpoint) | + **event triggers** (job findings, frontier flips) + **research-mode cadence** (audit A#2, C: Kosmos/co-scientist) |
| 2 | Research mode | prompt tone + budgets | **semantics profile**: floor inverts; ceiling/stalled/blocked → orchestrator invocations; breakpoints → strategy changes; thrift caps lifted; context-rich (audit A#1/3/6/18) |
| 3 | Feasibility | `plausible` → `¬P`, nudger-triggered | **empirical-first pipeline**, deterministically triggered, orchestrator-confirmed; budgets scale in research mode (audit A#4/10) |
| 4 | Disproof | LeanProbe scratch marks `false` | **negation PROMOTION** through the authoritative gate — disproof = bankable kernel artifact (audit B hole-2; N1) |
| 5 | Correctness | prover edits guarded | + orchestrator/decomposer writes pass the **same axiom scan**; `proved` nodes immutable (audit B hole-1) |
| 6 | Persistence | "atomic write path" | **actually atomic** (`atomic_json_write`), loud corruption failure, **journal.jsonl** source of truth (audit A#16 — real bug found) |
| 7 | Graph | AND-tree | + **`alternative_of` OR-routes** (rival decompositions coexist; non-destructive backtracking), `audited` status, provenance, conjecture nodes (audit B; N4) |
| 8 | Dispatch budgets | children share prover budget | **independent budgets + wall-clock timeouts**; fire-and-continue deep-search in research mode; async-ready 3a (audit A#9) |
| 9 | Retrieval | Phase 5 | **mandatory from Phase 1** (`lean_lemma_suggest` at assignment; Hilbert ablation) (audit C#4) |
| 10 | Statement trust | negation-only | + **statement-fidelity audit** at scope entry & after re-state; **vacuity/instance probes**; anti-sorry-offloading check on decompositions (audit B a/m/n, C#3) |
| 11 | N1 | prompt-level | **mechanized**: scope-exit research report generator = hard invariant (audit B) |
| 12 | Evaluation | none | **Phase E** — T1/T2/T3 suites + adversarial fixtures + per-phase gates (audit B k) |
| 13 | Decomposition | orchestrator-level only | + **prover-level confidence workstream** (§4.10, live-run forensics): prompt fixes, partial credit, route-forcing |
| 14 | Human | absent | `ask-human` route; re-state ACK; report surfacing (audit B i/m) |
| 15 | Knowledge | per-scope | + cross-run `learnings.md` + priors; proof-artifact reuse named for Later (audit B g, C#5) |
| 16 | Models | strong everywhere + small nudger | + `models.prover_light` tier for stub grinding (DeepSeek-V2 pattern) (audit C) |
| 17 | Verdict copies | three | **four** (incl. `_finish_queue_step_boundary` `:3032`) — Phase 0 scope corrected (grounding I) |

---

## 4. Core design details (v3)

### 4.1 The dependency / exploration graph (`blueprint.json`)
As v2 (leanblueprint-adapted; machine-first JSON; kernel-reconciled every cycle) **plus**:
- **OR-structure:** edge kind `alternative_of` + per-node route groups — two candidate
  decompositions of the same node coexist; "try another route" no longer destroys route A; N4's
  parallel directions become representable.
- **Statuses:** `conjectured | stated | audited | proving | proved | blocked | false | split |
  parked` — `audited` = statement-fidelity pass (§4.11); `proved` **immutable** (file re-org may
  move, never delete, a kernel-verified lemma); `false` only after gate promotion (§4.11).
- **Provenance:** `generated_by` (decomposer | planner | empirical job | human), `evidence` edges
  to grounding findings; exploratory **conjecture nodes** are first-class (research needs
  conjecture *generation*, not just decomposition — audit B c).
- **History:** every node/edge mutation appends a `journal.jsonl` event (who, why, route) — the
  lab notebook; snapshots (`blueprint.json`) are rebuildable from the journal.
Schema, APIs, reconciliation functions: specs Part I §Phase-1.

### 4.2 Dispatch: taxonomy + lifecycle
As v2 (one shared service; archetypes PROVER A/B, EMPIRICAL, DEEP-SEARCH, FEASIBILITY; JobSpec;
ledger; sync cap-3 v1) **plus v3**:
- **Universal callers** (owner N2) as in the §2 matrix; the prover never dispatches (escalates).
- **Lineage** (owner N3): dotted ids; ancestor-scoped list/track/kill; ledger + inbox addressing
  carry the chain. Cheap now, painful later — lands in the Phase-3a design.
- **Independent budgets:** each job declares `{api_steps, wall_clock}`; children never inherit the
  dispatcher's iteration budget (today `delegate_tool.py` shares it — that coupling is severed for
  dispatched jobs). Patience policy derives from the declared budget; kill via the existing inbox.
- **Fire-and-continue** for deep-search jobs in research mode (findings arrive as an orchestrator
  event, §4.4); everything else stays sync-blocking in v1. The 3a design is written async-ready;
  research-run experience is the explicit trigger for promoting async from Later to scheduled.
Full JobSpec/ledger/kill/patience mechanics + what exists vs missing: specs Part II.

### 4.3 Budget breakpoints
As v2 (per-theorem decision packets; queue-level K-consecutive interrupt; Phase-1 mechanical stop)
**plus v3**: in research mode a breakpoint always resolves to a **strategy change** — split /
plan / negate / re-state / park-and-advance; `abort` exists only for a kernel-promoted negation of
the main goal. The decision packet is part of the concrete-result chain (§4.12). Exhaustion
accounting seams and stop-reason plumbing: specs Part I.

### 4.4 Orchestrator invocation & struggle signals
- **Invocations (v3):** scope-entry · stall · breakpoint · **job-done-with-findings** ·
  **graph-frontier change** (`false`/`proved` on nodes with dependents) · **research-mode cadence**
  (every N cycles / M wall-clock hours). The first three exist as seams today; the event triggers
  use a theorem-scoped monotonic watermark. Parent maintenance may publish many job completions
  during one foreground model turn, but it never calls the orchestrator from inside that turn.
  The next read/search tool result closes a normal step boundary; edit, terminal, and Lean
  verification callbacks retain exclusive ownership of their commit/gate. At the outer loop, one
  consultation atomically captures the current event prefix and acknowledges only that prefix on
  success. Later events remain pending and failed consultations retry without duplicate publication
  or event loss. Per-cycle frontier/cadence discovery feeds the same coalescer, with near-zero cost
  when nothing changed.
- **Struggle signals** (deterministic, all already tracked — table in v2 §4.4) summon the
  **nudger** for message-shaping only. **v3:** ≥2 genuine failed attempts also *deterministically
  proposes a feasibility probe* into the decision packet; the orchestrator confirms or vetoes.
  The nudger's optimism can no longer suppress feasibility checking (audit A#4 failure scenario).

### 4.5 Orchestrator file-organization & lemma-stating powers
As v2 (state lemmas between prover turns; organize the sorry landscape; re-state on falsity)
**plus v3 (audit B hole-1):** every orchestrator/decomposer file write passes the **same
forbidden-axiom scan** and a stub-shape check (stated stubs are `theorem/lemma … := by sorry` —
nothing else) before guard snapshots and the graph refresh. Statement changes remain
orchestrator-only actions; **main-statement changes require human ACK** (§0.16).

### 4.6 Deep search & repository acquisition
As v2 (`web_search`/`web_fetch`/`web_download` + new `repo_clone`, multi-level availability)
**plus v3:** premise retrieval (`lean_lemma_suggest`, `lean_hammer_premise` via the LSP surface)
is injected at queue assignment from **Phase 1** — evidence says it both lifts accuracy and cuts
tokens. Deep-search *jobs* land in Phase 3b (pulled forward from Phase 5) so research runs get
their grounding organ before the decision-making phases it informs.

### 4.7 Research mode — a semantics profile (v3)
`LEANFLOW_RESEARCH_MODE=1` now means, concretely:
| Dimension | Easy-run default | Research profile |
|---|---|---|
| Orchestrator at scope entry | deterministic floor only | **always consulted** |
| Cycle ceiling / stalled / blocked | terminal stop reasons | **orchestrator invocations** |
| Breakpoints | mechanical stop (Phase 1) / decision (Phase 4) | **strategy change; abort only on promoted negation of the main goal** |
| Reflection | on failure only | + cadence (N cycles / M hours) |
| Thrift caps (feedback cap, reasoning-replay trim, compression floors) | active | **lifted** |
| Orchestrator/planner context | frontier + packet | **context-rich** (plan.md, grounding, journal tail, packet history) |
| Feasibility budgets | 1 probe/lemma | **scaled**; main-statement feasibility is co-equal work |
| Progress metric | proved-delta | **graph delta** (stated/audited/false/evidence all count) |
| Give-up vocabulary | discouraged | removed; difficulty = routing signal |

### 4.8 Model configuration matrix
As v2 (strong orchestrator/planner/decomposer/prover; small nudger; per-role `config.yaml` keys)
**plus** `models.prover_light` — an optional cheap tier for stub-grinding shape-B probes
(DeepSeek-Prover-V2's 671B-decompose/7B-prove pattern).

### 4.9 Reuse map
Superseded by the definitive inventory in **specs Part IV** (every asset: file:line, reuse mode,
gotchas, pinning tests; plus the short genuinely-new-modules list).

### 4.10 Decomposition confidence (v3 — from live-run forensics, run pid92647)
Provers demonstrably fear decomposition. Evidence: 6+ direct-proving failures before the first
`lean_decompose_helpers` call; the model's own reasoning — *"It seems like I can solve it myself…
I'm wary of time constraints"*; 6 ready-to-insert helpers returned and NOT inserted; helpers
finally adopted only ~10 attempts later, reformulated. Root causes and fixes:
| Cause (receipt) | Fix (where) |
|---|---|
| Optionality language: "This is optional, not required" (`lean-theorem-queue-worker/SKILL.md:30`); "decomposition proposal only" (`lean_experts.py:701`) | Rewrite both: decomposition is a **standard, first-class strategy**; after ~2 failed direct attempts it is the *expected* next move; "Insert the ready helpers now, prove each, assemble" (**Wave −1**) |
| Budget economics: one-edit-then-stop cadence + step-budget discipline make the multi-step decompose path feel expensive | Decompose+insert batch counts as **one meaningful edit**; budget framing exempts strategy changes (**Wave −1** prompt text; Phase 2 mechanics) |
| Helper-`sorry` ambiguity: gate rejects surface "declaration uses `sorry`" mid-decomposition | Contract text: a new helper's `sorry` is **normal work-in-progress during the turn**; sorry-free applies at final acceptance (**Wave −1**) |
| Binary acceptance — no partial credit | Manager feedback acknowledges **kernel-verified helpers as progress**; graph `proved` nodes make it structural (Phase 2/4) |
| Self-confidence bias, unframed attempt counter | Queue block frames attempts as an escalation trigger; at attempt ≥ K the route **becomes** decompose (orchestrator-enforced, Phase 4) |

### 4.11 Statement trust: fidelity, vacuity, and negation promotion (v3)
- **Statement-fidelity audit** at scope entry (reuses the `/formalize` verifier pattern,
  `native_runner.py:5661`) and re-run after any re-state: is the Lean statement the intended
  statement? (kernel-verified-but-wrong-statement is the largest silent failure mode for open
  problems.)
- **Vacuity/instance probes** in the feasibility archetype: exhibit a model of the hypotheses
  (catches vacuously-true decomposition products — the conjunction trap).
- **Anti-sorry-offloading check** on decompositions: children must be judged strictly easier and
  no single child may absorb the whole difficulty (frontier evidence: prompting alone does not fix
  this).
- **Negation promotion:** a scratch `¬P` proof marks the node `false(provisional)`; the negation
  theorem is then written into the project and passed through `_manager_check_queue_item` + the
  axiom profile. Only then does `false` poison ancestors — and the disproof becomes a bankable,
  kernel-verified artifact (half of the concrete-result guarantee).
- **False-branch retirement:** an authoritatively false decomposition child invalidates the exact
  same-parent unresolved branch above it. Current-source decomposer declarations are removed from
  source and graph; source-less planner/decomposer artifacts are removed only from graph. The parent
  is reopened, unrelated proved helpers and negation evidence survive, and any external, verified,
  evidence-bearing, or identity-drifted dependent forces a resumable quarantine. A historical
  committed cleanup that retains only its evidence tombstone has no branch left to migrate and is a
  read-only startup no-op; the exact obsolete no-work evidence quarantine auto-resolves without
  weakening any active cleanup gate.

### 4.12 Scope-exit research report (v3 — N1 mechanized)
A deterministic generator over graph + ledger + journal (LLM-polished in research mode):
goal · route history · proved/false/parked node inventory (with names/files) · evidence gathered
(cited) · what was tried and why it failed (packet digest) · open subgoals ranked · recommended
next attack. Produced at EVERY scope end that lacks a proof/promoted-disproof; surfaced to the
user. This is the artifact that makes "never give up silently" auditable.

### 4.13 Phase E — the evaluation harness (v3)
Implemented in `evals/harness.py` and `evals/corpus_manifest.json`. **T1 regression** inventories
the demo projects. **T2 capability** freezes 40 exact Lean 4 declarations across miniF2F and
PutnamBench. **T3 research-grade** freezes ten multi-hour campaigns, including IMOMath3 and nine
solved Formal Conjectures declarations at `bench-v1-lean4.27.0`. **Adversarial fixtures** ship as
local false-lemma, false-decomposition, vacuous-statement, and axiom-temptation files. Campaign
scoring reports voluntary give-up termination rate, unresolved-success exit rate, coach coverage,
route/proof-shape diversity, jobs launched/consumed/replaced, verified graph progress, and epoch
rollovers.

---

## 5. Design-verification matrix (audit summary)

18 choices audited against the north star (full evidence: audit Part A; SOTA: Part C):

| Choice | Verdict | v3 resolution |
|---|---|---|
| Spine preservation | SERVES-GOAL | + research-mode: ceiling/stops → orchestrator invocations (§4.7) |
| Failure-only orchestrator invocation | NEEDS-ADJ | event triggers + cadence (§4.4) |
| Deterministic floor, upgrade-only | NEEDS-ADJ | floor inverts in research mode (§0.3) |
| Small advisory nudger | NEEDS-ADJ | message-shaping only; deterministic probe proposals (§4.4) |
| Kernel gate never LLM-overridable | SERVES-GOAL | keep; axiom guard rides inside unified `decide()` |
| Budget breakpoints | NEEDS-ADJ | research mode: strategy-changes, graph-delta progress (§4.3/4.7) |
| Graph blackboard | NEEDS-ADJ | OR-routes, `audited`, provenance, immutable-proved, journal (§4.1) |
| plan.md coordination | NEEDS-ADJ | atomic writes, loud corruption, journal source-of-truth (§2 note) |
| Sync dispatch cap-3, shared budgets | **MISALIGNED** | independent budgets, timeouts, fire-and-continue, async-ready (§4.2) |
| Negation probe 1/lemma, plausible-first | NEEDS-ADJ | empirical-first pipeline, deterministic trigger, scaled budgets (§0.5) |
| Prover job A/B | mixed | A default confirmed; child-budget ambiguity resolved (§4.2) |
| LeanProbe-everywhere | SERVES-GOAL | + promotion invariant for irreversible decisions (§4.11) |
| Focused prover toolset | NEEDS-ADJ | premise retrieval mandatory at assignment from Phase 1 (§4.6) |
| File-order queue selection | NEEDS-ADJ | graph-frontier selection once the graph exists (Phase 4) |
| Phasing 0–3 first | NEEDS-ADJ | Phase 4-lite breakpoint-decider pulled into Phase 1–2 (§6) |
| Resume via summary.json | **MISALIGNED** (as-is) | persistence hardening + journal + resume drills (§4.1, Phase E gate) |
| Single-prover, no portfolios | NEEDS-ADJ | parallel frontier discharge = research end-state; `prover_light` (§4.8) |
| Token-thrift residue | NEEDS-ADJ | research profile lifts the enumerated caps (§4.7) |

---

## 6. Phased plan (v3)

### Wave −1 — Immediate fixes — **implemented**
Decomposition-confidence prompt fixes (§4.10: SKILL.md optionality, `lean_experts.py:701`
"proposal only", helper-`sorry` legitimization, attempt-count framing) + the persistence bug
(atomic writes via `core/utils.py:13`; loud corruption failure in `workflow_json_io.py`).
**~2 days / near-zero risk / immediate behavioral value.**

### Phase 0 — Queue-verdict unification — **implemented**
Route production verdicts through `decide()` (`queue_manager.py:484`, dead in production);
consolidate the **four** drifting copies (`:1890`, `:1834`, `:5815`, and `_finish_queue_step_
boundary` `:3032`); make `TheoremQueueManager` the live authority (19 reconstruct call-sites →
one instance). Shadow-compare for one release; axiom guard stays inside the unified path.
Function-level plan: specs Part I. **~1–1.5 wk / Low-Med.**

### Phase E — Evaluation harness — **implemented; live result collection ongoing**
§4.13. Runner + fixtures + scorer over artifacts the redesign produces anyway. Gates every later
enable-flag. **~1 wk initial, then continuous.**

### Phase 1 — Plan-state + graph + journal + breakpoint + retrieval — **implemented**
v2 scope **plus**: `journal.jsonl` (append-only truth, rebuildable snapshots); OR-route graph
schema (§4.1); **premise-retrieval injection at assignment** (§4.6); immutable-proved invariant;
**breakpoint-decider-lite** (prompt-level orchestrator at mechanical breakpoints — research runs
get a decider years before Phase 6); research-profile skeleton (§4.7 flags). Checkpoint UX
retirement per corrected seams (`:9602/:9623/:9642`); baseline-`sorry` restore + git-shadow kept.
Spec: Part I. **~2.5 wk / Low.**

### Phase 2 — Struggle signals + persistence coach — **implemented and default-on for prove**
v2 scope **plus**: ≥2-failures auto-proposes a feasibility probe into the decision packet
(orchestrator-confirmed); partial-credit feedback for kernel-verified helpers (§4.10). Enable gate
defined by Phase E. Spec: Part II. **~1 wk dark + enable / Low→Med.**

### Phase 3 — Dispatch and negation promotion — **implemented**
**3a (design, owner-review gate):** lifecycle per §4.2 — JobSpec with lineage ids, ledger,
independent budgets, patience/kill, async-ready semantics, resume-reconciliation (ledger says
`running`, child dead, lock held). **3b (build):** dispatch service over `delegate_task`/
`dispatch_worker`; **feasibility archetype first** (empirical → `plausible` → `¬P`, with **negation
promotion** §4.11 and vacuity probes); **deep-search archetype** (pulled forward; `repo_clone`
lands here); ledger + lineage live. Promoted false sublemmas now transactionally retract their
same-revision unresolved dependent decomposition chain from source, graph, and queue state while
preserving unrelated verified source; ambiguous or verified dependents pause for quarantine. Exact
stale tombstones from already-committed version-1 cleanups are upgraded through a predecessor-bound,
source-first migration rather than being left active or heuristically rewritten.
Spec: Part II. **~3 wk / Med.**

### Phase 4 — Deterministic/event-driven orchestrator + decomposer + reports — **implemented**
v2 scope **plus**: event triggers + research cadence (§4.4); statement-fidelity audit (§4.11);
axiom-scan on orchestrator/decomposer writes; **scope-exit report generator** (§4.12);
`ask-human` route + re-state ACK; graph-frontier queue selection option; breakpoint decisions
replace the Phase-1 mechanical stop. Spec: Part III. **~3 wk / Med.**

### Phase 5 — Planner fan-out + frontier discharge + learnings — **implemented**
v2 scope **plus**: **multi-direction proving** (N4 — several stub-file prove jobs over the graph
frontier; sequential v1, parallel as async lands); fire-and-continue deep-search; cross-run
`learnings.md` + scope-entry priors; **curriculum ordering** of the stub frontier (easy→hard —
pulled from optional; LeanAgent/AlphaProof evidence). Spec: Part III. **~3 wk / Med-High.**

### Phase 6 — LLM orchestrator + complete research mode — **implemented**
v2 scope (strong-model routing over the floor; spec fold **and rewrite to the current quality
bar** incl. the golf/refactor overhaul) **plus**: full research-profile semantics (§4.7);
`models.prover_light`. Research routing uses a target-scoped 12,000-character digest with explicit
per-section omission hashes/counts. Advisory latency is bounded by a twenty-second isolated
foreground deadline and a persisted two-minute circuit after timeout; the deterministic route
floor continues immediately while the circuit is open. Spec: Part III. **~2.5–3 wk / Med.**

### Remaining promotion work
Run and publish the pinned live campaign results; harden stuck-worker reclaim from observed runs;
**proof-artifact reuse** (kernel-proved lemma pool + hashed goal→proof cache — frontier-universal);
**verification-environment scaling** (parallel Lean runtimes/goal cache — the binding constraint
at research scale, per Gauss); graph web view.

---

## 7. Decisions log (v3 additions)

| # | Question | Resolution |
|---|---|---|
| 9 | Can the nudger initiate feasibility actions? | **No** — deterministic proposal + orchestrator confirmation; nudger shapes messages (audit A#4). |
| 10 | Are LeanProbe scratch results authoritative? | **No** for irreversible decisions — promotion through the gate required (`false` marks, banked lemmas). |
| 11 | Do orchestrator writes bypass the axiom guard? | **No** — same scan as prover edits + stub-shape check. |
| 12 | Is "never give up" enforceable? | **Yes — mechanized**: proof | promoted disproof | scope-exit report; hard invariant. |
| 13 | How do we know any phase improved capability? | **Phase E gates** — defined suites, fixtures, and per-phase enable criteria. |
| 14 | Multi-direction proving? | Supported (graph OR-routes + frontier discharge, Phase 5); probing/experimentation/search remain the emphasized dispatch value. |
| 15 | Token stance? | Efficiency floors for easy runs; research profile is context-rich and cap-lifted — "value is in solving the impossible." |

*(v2 decisions 1–8 stand as amended above.)*

## 8. Risks & mitigations (v3 deltas)

| Risk | Mitigation (v3) |
|---|---|
| Persistence loss on multi-day runs (**real bug found**) | Wave −1: atomic writes + loud corruption failure; Phase 1 journal + rebuildable snapshots; Phase E kill-9 resume drills (10/10). |
| Research and proving fight over budget | Independent job budgets + wall-clock; fire-and-continue deep-search; research profile. |
| Well-formed-but-doomed grinding between breakpoints | Event-driven orchestrator + research cadence; graph-frontier selection. |
| Nudger optimism suppresses feasibility | Deterministic probe proposals; orchestrator confirmation; Phase E false-lemma fixture gates. |
| Poisoned stubs / axiom cheating by new actors | Same axiom scan on all writes; promotion invariant; axiom-temptation fixture. |
| Sorry-offloading decompositions | Anti-offloading check + fidelity audit + negation probes on suspicious children. |
| Harness complexity outruns value | Minimal-agent warning heeded: flags-off byte-identical hot path; Phase E T1 regression gate; deterministic floor for easy runs. |
| (v2 risks stand: sprawl caps, ledger, shadow-compare, guard interplay.) |

## 9. References
Hilbert (arXiv:2509.22819) · leanblueprint (PatrickMassot) · **Automated Conjecture Resolution**
(arXiv:2604.03789 — the north-star existence proof) · Discover-and-Prove (arXiv:2604.15839) ·
Goedel-Prover-V2 (arXiv:2508.03613) · BFS-Prover-V2 (arXiv:2509.06493) · DeepSeek-Prover-V2
(arXiv:2504.21801) · LeanAgent (arXiv:2410.06209) · Minimal Agent for ATP (arXiv:2602.24273) ·
Kosmos (arXiv:2511.02824) · Gauss/Strong-PNT (Math Inc) · Erdős-90 disproof + Lean formalization
(2026). Full per-system lessons: audit Part C.
