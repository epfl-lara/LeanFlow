# LeanFlow /prove Redesign — Design Audit (companion to prove-redesign-roadmap.md)

Adversarial verification of every design choice against the north-star goal (autonomous research on
hard/open problems), produced by three independent audit agents (2026-07-02). The roadmap's §5
verification matrix summarizes these; this document is the full evidence. Parts: A = red-team of 18
design choices, B = goal-derived requirements coverage (+ Phase E evaluation harness), C = SOTA
frontier cross-check.

---

# PART A — Red-Team Audit (18 design choices)

# Red-Team Audit: LeanFlow `/prove` Redesign Roadmap v2 vs. the Autonomous-Research-Mathematician North Star

Audited artifact: `/Users/lmilikic/Desktop/LeanFlow/docs/prove-redesign-roadmap.md` (branch `docs/prove-redesign-roadmap`, commit f284ac6), cross-checked against the live code. Line citations verified in-tree. Verdict scale: SERVES-GOAL / NEEDS-ADJUSTMENT / MISALIGNED.

**Headline finding (found in code, contradicts a roadmap claim):** the roadmap's persistence story rests on an "atomic write path" that is not atomic, and whose reader silently swallows corruption. This poisons choices 7, 8, and 16 simultaneously. Details under Choice 16.

---

## Choice 1 — Spine preservation (while-loop + single-assignment queue + manager-prover core stays the inner engine)

**Verdict: SERVES-GOAL (with two research-mode caveats).**

Right call in one line: the kernel-gated queue (`native_runner.py:9059` loop, `queue_manager.py:181` assign, gate `:1890` → `:1210`) is the only battle-tested correctness machinery in the tree, and "correctness is absolute" makes it non-negotiable to preserve.

Caveats found in code, not addressed by the roadmap:

- **The spine has a hard 120-cycle ceiling.** `_autonomous_max_cycles()` defaults to 120 (`native_runner.py:561-570`) and stops the run "to avoid a runaway loop" (`:9078-9092`). On day 2 of a 5-day open-problem run the loop will have burned well past 120 continuation cycles and the run dies with `autonomy-stop`, regardless of RESEARCH_MODE — §4.7 (roadmap:350-358) says research mode "raises budgets" but never names this ceiling. A "never give up" harness that self-terminates at a fixed cycle count is a silent give-up.
- **The spine's stop reasons are tuned for closed tasks.** `stalled` fires after 4 identical live-state signatures (`:553-558`, `:8921`) and `blocked` after 3 corroborated blocker phrasings (`:545-550`, `:8913-8917`). Pre-Phase-4 there is no orchestrator to catch these stops (see Choice 15), so on a hard problem the spine stops rather than re-routes.

**Adjustment:** in RESEARCH_MODE, `stalled`/`blocked`/cycle-ceiling must become orchestrator invocations, never terminal stop reasons; the roadmap should list `AUTONOMOUS_MAX_CYCLES`, `AUTONOMOUS_STALLED_LIMIT`, `AUTONOMOUS_BLOCKED_LIMIT` explicitly among the §4.7 research-mode overrides.

---

## Choice 2 — Orchestrator invoked only at scope-entry / stall / budget-breakpoint

**Verdict: NEEDS-ADJUSTMENT — failure-driven-only replanning is insufficient for research.**

The invocation set (roadmap:117, §2 diagram; Risk table roadmap:490 "never per cycle") is a cost optimization ported unchanged into a domain where the owner says cost is not the constraint (N5).

**Day-2 failure scenario:** the orchestrator dispatched a deep-search job on day 1; on day 2 it returns a paper showing the main statement is only known for the compact case and suggesting the general statement is likely false. Meanwhile the queue is happily grinding sub-lemmas of the (doomed) general decomposition, making enough noise-progress that the stall signature (`native_runner.py:8871-8891` — diagnostics/goals/sorry-count tuple) never stabilizes, and per-theorem budgets take hours more to exhaust. Nothing in the design re-invokes the orchestrator on *new evidence*. The Hilbert-style loop the roadmap cites (roadmap:10-14, D5 at roadmap:44-52) is *failure*-triggered; research replanning must also be *evidence*-triggered.

**Adjustment:** add three event triggers to `_orchestrator_route` (Phase 4, roadmap:434-444): (a) dispatch-ledger entry transitions to `done` with a non-empty findings deliverable; (b) any graph node flips to `false` or `proved` when that node has ≥1 `depends_on` dependents (frontier change); (c) a periodic reflection cadence in RESEARCH_MODE (e.g. every N cycles or M wall-clock hours) that reads graph + ledger and may re-route. (a) and (b) are mechanical checks at the existing per-cycle reconciliation point (§4.1, roadmap:235-237) — near-zero cost when nothing changed.

---

## Choice 3 — Deterministic pre-classifier floor with LLM upgrade-only routing

**Verdict: NEEDS-ADJUSTMENT — the floor must invert in research mode.**

For easy runs the floor is exactly right (byte-identical hot path, Risk table roadmap:495). But upgrade-only routing means the *cheap deterministic* component decides whether the *smart* component gets consulted at all (roadmap:122-124, "det. pre-classifier floor → optional LLM (upgrade-only)"; Risk table roadmap:490 "deterministic floor answers direct-prove without any LLM call").

**Day-2 failure scenario:** actually a day-0 scenario that costs day 2. An open problem arrives as a single file with a single `sorry` — syntactically indistinguishable from a homework lemma. The deterministic floor routes `direct-prove`; the problem enters the plain queue; the strong orchestrator is not consulted until two failures, a stall, or a breakpoint accumulate (with `MANAGER_POST_EDIT_HARD_RETRY_LIMIT = 8`, `native_runner.py:110`, that is many hours of blind tactic grinding). Day 1-2 is spent with zero literature grounding, zero decomposition, zero feasibility analysis — the opposite of how a research mathematician starts.

**Adjustment:** in `LEANFLOW_RESEARCH_MODE=1`, the LLM orchestrator runs unconditionally at scope entry and a plan/deep-search route is the *default* opening move for any statement not proven within a short direct-prove probe budget. The deterministic floor remains authoritative only when the mode flag is off. One sentence in §4.7 fixes this; today §4.7 only covers prompts and budgets.

---

## Choice 4 — LLM-manager: advisory-only, small model, struggle-triggered

**Verdict: NEEDS-ADJUSTMENT — advisory-only and struggle-triggered are right; the small model is given one job too many.**

Advisory-only + kernel-untouchable (roadmap:30-35, D1 at roadmap:475) serves the goal: correctness stays deterministic. Tone-shaping nudges ("stop searching, try solving") genuinely don't need a strong model — the existing deterministic nudges (`SEARCH_PROGRESS_REPEAT_NUDGE_LIMIT = 2`, `native_runner.py:111-112`; `FAILED_ATTEMPT_ESCALATION_NUDGE_LIMIT = 4`, `:116`) already do 80% of this with no model at all.

The problem: Phase 3b makes the small model the *initiating trigger for negation probes* — "triggered by LLM-manager suggestion after ~2 failures" (roadmap:430-431), and §4.2 lets it "suggest a dispatch" (roadmap:159, 247). Whether to spend effort investigating *feasibility* is one of the most research-critical strategic calls in the whole design (it drives backtracking, D5).

**Day-2 failure scenario:** a key sub-lemma is false as stated (off-by-one boundary). The small model, prompted for "optimistic-but-strict, don't give up" tone (§4.7), keeps crafting "try harder, don't overthink" nudges and never suggests the negation probe — optimism bias is exactly what small instruction-followers exhibit under a pusher prompt. The prover burns its 8 hard retries × several attempts on an unprovable goal; the falsity is only discovered at the per-theorem breakpoint a day later, if the orchestrator then chooses to probe.

**Adjustment:** make the negation-probe *trigger* deterministic (≥2 genuine failed attempts on the ring buffer, `queue_manager.py:352` → auto-propose a probe into the decision packet / dispatch queue), with the orchestrator (strong model) confirming or vetoing. The small model shapes messages only — its suggestions should be logged (the Phase 2 dark-launch, roadmap:419-420, is the right mechanism) but never be the sole path to a feasibility action.

---

## Choice 5 — Kernel gate never LLM-overridable

**Verdict: SERVES-GOAL.** One line: with "no axiom cheating" as an absolute, the Lake-backed `_manager_check_queue_item` (`native_runner.py:1210`) as sole acceptance authority — plus the existing axiom-profile check at acceptance (commit 1ab8793, `DEFAULT_ALLOWED_AXIOMS` `native_runner.py:120-124`) — is the only defensible design; every SOTA system (Hilbert included) has this shape. No adjustment. Verify Phase 0's verdict-copy consolidation (roadmap:398-401) keeps the axiom guard inside the unified `decide()` path.

---

## Choice 6 — Budget breakpoints (per-theorem cumulative + queue-level K-consecutive)

**Verdict: NEEDS-ADJUSTMENT on two axes: the Phase-1/Phase-4 gap, and the progress metric.**

The concept — turning today's silent roll-on (`_handle_api_step_budget_exhaustion`, `native_runner.py:6008-6085`: record attempt, restore baseline `sorry`, continue) into a decision point — directly serves "never give up silently." Two problems:

1. **Phase 1 mechanical breakpoints are clean STOPS with no decider** (roadmap:298-302, 406-409: "a human — or a later orchestrator — decides"). Between Phase 1 landing and Phase 4 (~5 weeks of the plan, roadmap:403-444), a research run that exhausts a per-theorem budget at 3 a.m. simply halts until a human returns. That converts "silent give-up" into "loud give-up" — better, but still give-up for a multi-day autonomous run.
   **Day-2 scenario:** overnight, theorem #7 exhausts; the run stops; 9 hours of wall-clock are lost before anyone notices. **Adjustment:** Phase 1 research-mode semantics should be *park-and-advance*: mark the node `blocked`, persist the packet, skip to the next queue item, and only stop the whole run at the queue-level trigger. The stop-everything semantics can stay for non-research runs.
2. **"No `proved` delta" is the wrong progress metric for research** (queue-level trigger, roadmap:296-297: "the scope burns its budget with no proved delta"). On a genuinely hard problem, days of *real* progress — a decomposition validated by LeanProbe, two candidate lemmas refuted by negation probes, a literature synthesis narrowing the strategy — produce zero `proved` deltas.
   **Day-2 scenario:** the run has correctly falsified one decomposition and is mid-way through re-decomposing; the queue-level breakpoint fires on "no proved delta," interrupts everything, and (Phase 1) stops or (Phase 4) forces an orchestrator re-route of a strategy that was *working*. **Adjustment:** define progress as *graph delta* — any node status transition (`stated`, `false`, `split`, `proved`) or ledger `done` with findings — and in RESEARCH_MODE make breakpoints always resolve to a strategy change (split/plan/negate/park/re-state), never `abort`, unless the *main* statement's negation is kernel-proved. The owner's question ("should research mode convert breakpoints into strategy-changes rather than stops?") should be answered YES explicitly in §4.3; today §4.3 lists `abort` as an undifferentiated option.

---

## Choice 7 — Dependency graph as JSON blackboard reconciled to kernel truth

**Verdict: NEEDS-ADJUSTMENT — right instrument, schema one research-generation too thin.**

Kernel-reconciled statuses with `proved` only from the gate (§4.1, roadmap:230-237) is the correct anti-drift design and directly serves verified-partial-progress. But the schema (roadmap:214-228) is a *proving* graph, not yet a *research* graph:

- **No alternative-route (OR) nodes.** Edges are `depends_on|split_of|evidence` (roadmap:226). A research decomposition routinely has "prove P via route A *or* route B"; with only AND-semantics `depends_on`, a `false` verdict on one route's lemma "poisons split_of ancestors" (roadmap:233-234) even when a sibling route is alive. **Day-2 scenario:** route A's key lemma is proved false; the subtree invalidation takes the parent conjecture down with it; the orchestrator re-decomposes from scratch, discarding route B's three already-`stated` nodes. This also under-serves N4 (multiple parallel proving directions) — you cannot even *represent* two simultaneous directions for one goal.
- **No confidence / evidence payloads.** `notes: "why blocked / evidence refs"` (roadmap:224) is a free-text string. Empirical-job results (10^5-sample confirmations, §4.2 archetype 2, roadmap:258-261), negation-probe outcomes, and literature support have nowhere structured to live, so the orchestrator's "interpret failures via the graph" (roadmap:125) degrades to re-reading prose.
- **No proof-sketch / informal-math attachment.** `conjectured` nodes carry only a `statement` string. A research plan's informal argument (the thing a planner produces and a decomposer consumes) has no home; it gets flattened into `plan.md` where it is not addressable per-node.
- **No staleness propagation on re-state.** §4.5 says re-stating "resets the node" (roadmap:331-333) — but its *dependents* (nodes whose proofs used the old statement) are not invalidated by any rule; only `false` poisons ancestors. A re-stated definition silently strands proved-against-old-statement descendants until kernel reconciliation happens to notice.

**Adjustment:** add to the node schema: `confidence: {value, basis}` (basis ∈ empirical/literature/plausible/prior), `sketch` (informal proof outline, per-node), `evidence: [ledger-job refs]`; add edge kind `alternative_of` (OR-group id) with invalidation semantics "a `false` alternative does not poison the parent while a sibling is alive"; add a re-state rule "statement change ⇒ all `depends_on` dependents revert `proved→stated` pending re-verification."

---

## Choice 8 — Documentation-driven coordination (plan.md single blackboard)

**Verdict: NEEDS-ADJUSTMENT — the concept is right; the write machinery underneath it is not safe and the growth policy is unspecified.**

- **The "atomic path" is not atomic.** The roadmap asserts plan-state "writes go through the existing atomic `save_workflow_live_status` path (`workflow_state.py:305`)" (roadmap:178-179, repeated §4.1 roadmap:211-212). In code, `save_workflow_live_status` → `write_json_file` → **plain `path.write_text`** (`leanflow_cli/workflows/workflow_json_io.py:31-33`). No temp file, no fsync, no `os.replace`. A genuinely atomic writer exists (`core/utils.py:13-58 atomic_json_write`) but the workflow-state layer does not use it. See Choice 16 for consequences.
- **No lock on JSON overwrites.** JSONL appends are locked (`_locked_append`, `workflow_state.py:400`), but `write_json_file` is not. With cap-3 concurrent children plus the parent all "reporting to plan-state sections" (JobSpec `report_to`, roadmap:272), concurrent read-modify-write of `blueprint.json`/`summary.json` is last-writer-wins. Sync-v1 mostly masks this (parent consumes results and writes alone) — but the roadmap never *states* the "only the parent writes JSON" invariant, and the moment async lands (roadmap:466) it breaks silently.
- **No growth/compaction policy for plan.md.** Over a 5-day run with dozens of jobs each merging findings into "plan.md § Grounding" (roadmap:263-265), plan.md becomes a megabyte of accreted prose. The Risk table simultaneously promises the orchestrator a *small* context ("frontier + packet, not transcripts", roadmap:490) and N5 demands a *rich* one. The roadmap resolves this tension nowhere.

**Adjustment:** (1) route all plan-state JSON through `atomic_json_write`; (2) write the single-writer rule into the Phase 3a design doc: children never write shared JSON, they return deliverables, the parent merges; (3) give plan.md a mandated structure with an append-only findings log plus a periodically re-synthesized "current understanding" head section (the orchestrator rewrites the head at each invocation — that is what a research notebook is), sized generously in RESEARCH_MODE per N5.

---

## Choice 9 — Dispatch: sync-blocking v1 cap 3, prover-never-dispatches, escalate-only chain

**Verdict: MISALIGNED for research runs (the roadmap's own primary-value claim contradicts its concurrency model).**

The roadmap says dispatch's "biggest wins are experimentation and search" (roadmap:55-56, D2 roadmap:476). But v1 is synchronous-blocking (roadmap:58, 282-283), implemented as `ThreadPoolExecutor` + `as_completed` that blocks the caller until *all* children return (`delegate_tool.py:489-522`), children default to 50 iterations (`:43`), have **no wall-clock timeout** anywhere in `_run_single_child` (`:160-270`), and — critically — **share the parent's iteration budget** (`delegate_tool.py:208-210, 248`: "subagent tool calls count toward the session-wide limit").

**Day-2 failure scenario (triple):** the orchestrator dispatches a deep-search job at breakfast. (a) The entire spine — queue, prover, everything — blocks for the job's duration (possibly hours; no timeout if a `web_fetch`/terminal call hangs). (b) The child's 50 tool calls are *debited from the parent's budget*, so researching literally accelerates the parent toward its own budget breakpoint — dispatch and §4.3 are anti-synergistic as coded. (c) If the machine restarts mid-job, the in-thread child dies with the process and its partial findings are gone (results are consumed only on return, roadmap:280-281). Meanwhile the prover, wanting one numeric sanity check, must traverse prover→manager-gate→struggle-signal→LLM-manager-suggestion→next-orchestrator-invocation→decomposer→dispatch — hours of latency for a 10-second Python computation. And note the irony: the prover's own toolset already contains `terminal` (`core/toolsets.py:127`), so it can *already* run empirical Python directly; the escalate-only doctrine is stricter than the status quo.

**Adjustment:** (1) give dispatched children an *independent* budget (JobSpec already specifies `budget {api_steps, wall_clock}`, roadmap:270-271 — the design must call out that this requires *not* passing `iteration_budget=shared_budget`); (2) add a wall-clock timeout to `_run_single_child`; (3) pull **fire-and-continue for DEEP-SEARCH jobs specifically** forward from "Later/optional" (roadmap:466) into Phase 5 for RESEARCH_MODE — deep-search is the one archetype whose results are consumed at a *later* orchestrator invocation anyway (Choice 2's event triggers), so async costs little extra machinery beyond the ledger the roadmap already builds; (4) keep prover-never-dispatches, but explicitly bless the prover's existing `terminal` for cheap in-turn empirical checks so the escalation chain is reserved for *expensive* jobs.

---

## Choice 10 — Negation probe: 1/lemma after ~2 failures; `plausible` pre-probe

**Verdict: NEEDS-ADJUSTMENT — correctly designed for wrong-decomposition detection, undersized for open problems where feasibility IS the question.**

For auto-generated sub-lemmas, ~1 bounded probe after 2 failures (roadmap:59-60, D4 roadmap:478) is a sane economy. Two gaps against the north star:

1. **`plausible` as the only pre-probe engine is narrow.** `plausible` needs `Decidable`/sampleable instances and shrinkable types; it is useless on `∀ ε > 0, ∃ N, …` over reals, asymptotic statements, or anything with nonconstructive quantifier structure — i.e., most research statements. The roadmap knows it's "a hint only" (roadmap:47-48) but still positions it as *the* pre-probe (§4.2 archetype 4, roadmap:266-267). The EMPIRICAL job archetype (Python numeric sweeps, small-case enumeration, SMT — roadmap:258-261) is strictly more general — and it is *right there in the same taxonomy*, yet not wired into the negation decision.
2. **1 probe/lemma with a fixed env budget (`LEANFLOW_NEGATION_PROBE_BUDGET=1`, roadmap:430) does not scale to the main statement of an open problem**, where disproof is a co-equal research direction ("kernel-verified disproof" is an explicit owner-sanctioned outcome). A single bounded `¬P` attempt on an open problem returns "inconclusive" with probability ≈1 and the loop learns nothing.

**Day-2 failure scenario:** the main conjecture is false for n ≥ 47 but true for all small cases anyone tries by hand. The single `plausible` pre-probe can't sample the type; the single bounded `¬P` LeanProbe attempt times out; verdict "inconclusive ⇒ too hard ⇒ split" (roadmap:52) — and the harness spends the remaining 3 days decomposing a false statement, when a 20-minute Python sweep over n ≤ 100 would have found the counterexample and redirected the entire run toward a verified disproof (the highest-value outcome available).

**Adjustment:** make the EMPIRICAL job the first-class feasibility engine: the feasibility pipeline is *empirical search (Python/enumeration, budget scaling with node importance) → `plausible` where applicable → bounded `¬P` formal attempt*, with all three writing structured evidence to the graph node (Choice 7's `confidence`/`evidence` fields). In RESEARCH_MODE, the main statement and top-level decomposition nodes get a standing, renewable feasibility budget (repeated escalating probes as evidence accumulates), not 1/lemma.

---

## Choice 11 — Prover job shape A (nested queue-prove) default vs B (queue-less probe)

**Verdict: SERVES-GOAL in intent; MISALIGNED in mechanism until one ambiguity is resolved.**

A-as-default is right in one line: results that should land in the project must pass through the full queue+gate machinery, so reusing it wholesale (roadmap:251-256) is the correctness-preserving choice.

The mechanism problem: the roadmap offers two interchangeable spawn paths — "the existing workflow spawn (`workflow.py:537`) or a `delegate_task` child running the native runner" (roadmap:253-255) — and they are **not** interchangeable:

- Via `delegate_task`: the child inherits `_delegate_depth = parent+1` (`delegate_tool.py:252`) with `MAX_DEPTH = 2` (`:42`) — so the mini-prove run **cannot dispatch anything** (its own negation probes, empirical checks, its manager's suggestions all hit "Delegation depth limit reached", `:412-421`). It also shares the parent's iteration budget (`:208-210`) and runs in-thread (dies with the parent). This flatly contradicts N2 (universal dispatch at every decision level) and N3 (a `root.orchestrator.planner.ds-042` lineage implies ≥3 levels).
- Via the subprocess spawn (`leanflow_cli/workflow.py:524-535`, fresh `python -m native_runner` with its own env): independent budget and survival — but *outside* `register_child` interrupt propagation, outside the shared `iteration_budget`, and kill/track only via the inbox + activity-registry path.

**Day-2 failure scenario:** the orchestrator launches a Shape-A job over a 5-stub file via `delegate_task`; inside it, lemma 3 hits 2 failures and its manager wants a negation probe — depth limit says no; the mini-run grinds lemma 3 to exhaustion, burning the *parent's* shared budget, and triggers the parent's queue-level breakpoint from inside a child.

**Adjustment:** Phase 3a must pick one: Shape A = **subprocess** native runner (own budget, own inbox for kill, registered in the dispatch ledger and the `summarize_workflow_agents` registry via `parent_agent_id`, `workflow_state.py:698-700`), with dotted job-ids providing the cross-process lineage (see N3 finding below). Reserve `delegate_task` for Shape B probes and leaf empirical/deep-search jobs, and raise `MAX_DEPTH` for research configs.

---

## Choice 12 — LeanProbe-everywhere except acceptance gate

**Verdict: SERVES-GOAL, two operational gaps.**

Right in one line: a warm REPL for probes/experiments/negation with Lake-backed acceptance (roadmap:79-81, D7 roadmap:481) buys ~100× inner-loop iteration speed without touching the correctness authority.

Gaps:

- **Process-global singleton.** The wrapper holds one `_PROBE = LeanProbe(auto_build=False)` per process (`leanflow_cli/lean/lean_incremental.py:95-101`). Cap-3 `delegate_task` children are *threads in the same process* — heavy concurrent probing (an empirical job hammering `lean_check` while the prover runs `check_target`) serializes or races on one probe with a bounded session pool (`:209` "max code sessions"). Under the roadmap's "used as widely as possible," the singleton becomes the throughput floor.
- **`auto_build=False` vs orchestrator-created files.** §4.5 has the orchestrator creating brand-new files (`Project/Generated/<Topic>.lean`, roadmap:328-329) that import project modules. If those modules aren't yet Lake-built (or the new file isn't in the module graph), LeanProbe skeleton validation (roadmap:135-136, decomposer "validates via LeanProbe") can fail spuriously with import errors — misread as "decomposition invalid."

**Adjustment:** Phase 3a should specify a per-child probe instance (or a small probe pool keyed by project), and the decomposer's validation contract must distinguish "environment/build failure" from "Lean rejected the statement," triggering a Lake build of new modules rather than a re-decomposition.

---

## Choice 13 — Inner prover keeps a FOCUSED toolset; research tools via escalation only

**Verdict: NEEDS-ADJUSTMENT — as written it *regresses* the current tree.**

The roadmap: "The inner queue-prover keeps its focused toolset (research reachable via escalation, not spiral-prone default)" (roadmap:346-348). But the current `leanflow-prove-worker` toolset **already includes web directly** — `"includes": ["file", "web", "terminal", "skills", "coordination", "lean"]` with the comment "Web is kept available for hard problems" (`core/toolsets.py:119-127`), deliberately added in commit 3fd4b96 ("guide the worker to web research on hard problems"). The spiral risk it worries about is already mitigated deterministically by the search-progress nudges (`native_runner.py:2412-2478`, thresholds at `:111-112`).

**Day-2 failure scenario (if §4.6 is implemented literally):** the prover is one Mathlib-lemma-name away from closing a sub-goal; a 30-second `web_search`/`lean_leansearch` would find it; instead it must fail the turn, trip struggle signals, get a nudge, fail again, and wait for an orchestrator route — the escalation chain (Choice 9) turns a 30-second lookup into a multi-hour detour, on a run where the owner explicitly does not care about the token cost of direct access.

**Adjustment:** keep direct `web_search`/`web_fetch` + Lean search tools in the prover toolset (status quo), guarded by the existing nudge thresholds; reserve escalation for *deep* search (downloads, `repo_clone`, multi-source synthesis — the §4.6 additions). Rewrite §4.6's last sentence to match, so implementation doesn't "clean up" the worker toolset into a regression.

---

## Choice 14 — Queue selection policy (errors-first, then sorry, file order) vs graph-frontier-driven

**Verdict: NEEDS-ADJUSTMENT once the graph exists.**

Current policy is exactly errors-first-then-sorry in queue order (`queue_models.py:311-334 select_next_item`) — fine for a flat file of independent obligations. But Phase 1 builds a dependency graph whose stated purpose includes "pick the frontier (`stated` nodes whose `depends_on` are all `proved`)" (roadmap:238-239) — and then the queue, which does the actual work assignment, **never reads it**. The roadmap routes frontier-awareness only through orchestrator file-organization (grouping siblings into files, roadmap:241-242), i.e., frontier ordering exists only at file granularity and only when the orchestrator happens to re-organize.

**Day-2 failure scenario:** a stub file contains lemmas A (independent) and B (`depends_on` A). File order puts B first; the prover spends its whole per-theorem budget on B, effectively proving it *modulo* A (kernel-fine via the axiom check, but strategically blind); overnight a negation probe proves A false; B's effort — and its budget-breakpoint decision packet — are garbage. The graph knew this ordering was wrong the whole time.

**Adjustment:** small, cheap, Phase 4: `select_next_item` gains an optional precedence callback — prefer items whose graph node has all `depends_on` proved; among those, prefer items on the critical path to the main goal; never *assign* an item whose dependency is currently `false`/`blocked` (park it instead). This is ~30 lines against a pure function with existing unit-test structure (`queue_manager.py:34-36` "intentionally pure").

---

## Choice 15 — Phasing 0–3 first (substrate), then fan-out

**Verdict: NEEDS-ADJUSTMENT — de-risking order is defensible, but research value arrives painfully late.**

Adding up the estimates (roadmap:398-464): Phase 0-3 ≈ 6.5 weeks with *zero* new decision-making capability; the first autonomy gain for a hard problem is Phase 4 (orchestrator floor + decomposer, ~9 weeks in); the research-mode pass and strong-model orchestrator land in Phase 6 (~14-15 weeks). Until Phase 4, breakpoints are stops-awaiting-humans (Choice 6) and negation probes exist (3b) but their outcomes feed a graph nothing consults for routing.

The earliest phase at which a HARD problem concretely benefits, as sequenced: **Phase 1** (better postmortems via packets + graph, and no more silent budget roll-on) — observability, not capability. Capability starts at Phase 4.

**Day-2 failure scenario:** on any research run attempted during the ~2 months between Phase 1 and Phase 6, the run is a today's-queue with extra logging: it stops at breakpoints overnight, has no planner, no deep search, and a research-pusher stance that exists only in a roadmap section.

**Adjustment:** carve a "Phase 4-lite" into Phase 1-2: a *prompt-level* orchestrator (single strong-model call using the existing `run_model_verification_review` pattern, `verification_providers.py:160` — no new module) invoked exactly at the Phase-1 mechanical breakpoints, choosing among {retry-with-hint, park-and-advance, stop}. It is ~days of work on the same seams Phase 1 already touches, converts breakpoints from stops into decisions months earlier, and dark-launches the orchestrator prompt that Phase 6 will harden. Also swap the marquee validation target: run each phase-exit against one genuinely hard benchmark problem (e.g. an unsolved PutnamBench item), not only regression suites — the roadmap has no research-grade acceptance test anywhere.

---

## Choice 16 — Resume/persistence: summary.json + graph + reconcile

**Verdict: MISALIGNED as implemented-today / claimed-in-roadmap. This is the most concrete defect found.**

The chain, verified in code:

1. Roadmap claims the graph and plan-state are "written through the same atomic path (`workflow_state.py:305`)" (roadmap:178-179, 211-212) and Phase 1 retires checkpoint UX in favor of "resume = load `summary.json`+graph, reconcile" (roadmap:410-412).
2. `save_workflow_live_status` (`workflow_state.py:305-306`) calls `write_json_file` (`workflow_json_io.py:31-33`) = **bare `path.write_text`**. Not atomic. A crash/OOM/power-loss mid-write leaves a truncated JSON file. (An atomic writer exists at `core/utils.py:13-58` and is simply not used here.)
3. On resume, `read_json_file` (`workflow_json_io.py:20-28`) catches **all** exceptions, logs at *debug*, and returns `{}` — "reads never raise" is its documented contract (`workflow_json_io.py:3-5`).

**Day-2 failure scenario:** 36 hours into a 5-day run, the machine reboots during a per-cycle graph write (§4.1 writes every cycle — thousands of write windows per day). `blueprint.json` is truncated. On restart, resume loads `{}` — no exception, no warning above debug level — and the harness proceeds with an **empty graph and empty plan-state**: every conjecture, every `false` verdict, every decision packet from 36 hours of work silently gone. Kernel reconciliation resurrects only what is already in `.lean` files (`stated`/`proved`); all `conjectured`, `blocked`, `false`, `parked` knowledge — the *research* state — is unrecoverable. This is the exact "silent give-up" failure class the owner forbids, moved from the agent to the substrate. Additional gaps: sync dispatch children die with the process and their partial findings are lost (results consumed only on return, roadmap:280-281; in-thread children, `delegate_tool.py:264-266`); nothing in the roadmap covers provider-outage backoff-and-resume for the eternal loop; and the ledger reconciliation (roadmap:274-277) recovers job *status* but not job *work-product*.

**Adjustment (must-have for multi-day runs):** (a) route all plan-state JSON through `atomic_json_write`; (b) make `read_json_file` fail LOUD when the file exists, is non-empty, and does not parse — corrupted research state must halt-and-alert, never default to `{}`; (c) treat the append-only, `_locked_append`-protected JSONL streams as the journal of record for graph deltas and decision packets, with `blueprint.json`/`summary.json` as rebuildable snapshots (plus a rolling `.bak` generation); (d) require dispatch jobs to persist findings incrementally to their workspace (`report_to` section flushed per-step, not per-job) so restarts lose minutes, not hours.

---

## Choice 17 — Model strategy: single strong prover, per-role config, no per-attempt diversity

**Verdict: NEEDS-ADJUSTMENT — per-role config (§4.8) is necessary scaffolding but leaves the best-known hard-theorem lever unused.**

The tree already has one diversity axis: reasoning-effort escalation after N failed attempts (`FAILED_ATTEMPT_REASONING_THRESHOLD` default 5, `native_runner.py:4199`; `_reasoning_effort_by_key` in `queue_manager.py:244`). But attempts 1-8 on a hard theorem all use the same model with the same tactic priors — and same-model retries are highly correlated failures. Every SOTA result the roadmap itself cites leans on attempt diversity (Hilbert's specialized-prover + refinement loops; portfolio sampling generally).

**Day-2 failure scenario:** the key lemma needs a `nlinarith`-with-hints style the default model never reaches for; 8 post-edit retries (`MANAGER_POST_EDIT_HARD_RETRY_LIMIT = 8`, `native_runner.py:110`) produce 8 near-identical failures; the breakpoint packet says "hard," the orchestrator splits — when a different prover model (or the same model at a different temperature/tactic contract) had a decent one-shot chance. The split then manufactures 4 sub-lemmas of *avoidable* work.

**Adjustment:** add `models.prover_portfolio: [...]` to §4.8 and rotate on the existing seam — `attempts_for(key)` (`queue_manager.py:329`) already counts per-theorem attempts; index the portfolio by attempt number when assigning (`assign`, `queue_manager.py:181`). Separately, let the orchestrator dispatch 2-3 Shape-B probes with *different* models on the same target for high-value nodes (N4's "parallel directions when beneficial" — this is its cheapest instantiation). Both are small deltas on verified seams.

---

## Choice 18 — Token-thrift residue that contradicts N5 for research runs

**Verdict: NEEDS-ADJUSTMENT — a research-mode floor-lift is missing for a specific, enumerable list.**

Found in-tree, all applied unconditionally (no RESEARCH_MODE gate anywhere in code — the flag doesn't exist yet):

| Thrift mechanism | Location | Research-run harm |
|---|---|---|
| Feedback-payload cap 16 000 chars (the "N6 cap" shipped in commit 90a897a) | `lean_incremental.py:143-152` | Long tactic-state feedback on deep proofs gets tail-trimmed; the failure the prover most needs may be in the dropped tail |
| Manager context caps 6000/5000/1600/1600 chars | `project_prove_manager.py:37-40` | Manager decisions on large research files see truncated declarations/hints |
| Prefix-cache: skill contracts dropped from continuation cycles (C3, same commit) | `native_runner.py:9195-9197` | Long runs progressively rely on the model remembering the contract through compactions |
| Per-cycle history compaction | `native_runner.py:9174`, `_auto_compact_history :7768` | Multi-day reasoning chains squeezed through repeated small-model summarization (`models` table: summarizer = small, roadmap:371) — cumulative lossy compression of exactly the insight N5 protects |
| Theorem-transition context wipe | `native_runner.py:9100-9132` | Cross-lemma insight ("the trick from lemma 3") destroyed at every boundary; nothing in the roadmap writes *success* insights to plan.md — decision packets exist only for failures (§4.3) |
| Children share parent iteration budget | `delegate_tool.py:208-210` | Research (dispatch) directly consumes proving budget — see Choice 9 |
| Budget-pressure nudges at 70%/90% ("start wrapping up") | `run_agent.py:508-513` | A wrap-up nudge is give-up vocabulary under §4.7's own definition |
| Orchestrator context deliberately minimized ("frontier + packet, not transcripts") | roadmap:490 (Risk 1) | Applies thrift to the one role N5 says must be context-RICH |

**Day-2 failure scenario:** the prover proves lemma 2 with a delicate rewrite ordering; the transition wipe discards it; on day 2, lemma 5 needs the same trick and burns a full budget rediscovering it; meanwhile the orchestrator, fed only "frontier + packet," cannot see the pattern across the two decision packets because attempt transcripts were never distilled anywhere.

**Adjustment:** define a single `research_profile` that RESEARCH_MODE applies: raise/disable the four char caps, disable the 70% wrap-up nudge (replace with a neutral budget-status line), add a mandatory "insights" write-back to plan.md at every theorem outcome (success *and* failure), and give the orchestrator full plan.md + graph + recent decision packets (not the minimized packet) at every invocation. Keep all thrift defaults for normal runs — that's exactly N5's "efficiency floors only for easy runs."

---

## Additional findings the choice-list missed

- **(N3 not designed at all.)** Hierarchical dotted job lineage (`root.orchestrator.planner.ds-042`, ancestor list/track/kill) appears nowhere in the roadmap. The JobSpec has a flat `parent` field (roadmap:272); the registry has `parent_agent_id` + `delegate_depth` (`workflow_state.py:698-700, 718-719`); `MAX_DEPTH = 2` (`delegate_tool.py:42`) structurally caps the tree below what N3's example implies. Phase 3a must specify: dotted `job_id` as the primary key, ancestor-prefix kill semantics, and depth policy per mode.
- **(N1's terminal artifact has no owner.)** The roadmap persists decision packets, plan, graph, ledger — the raw material — but no phase produces the "rigorous documented account of what was tried/learned" as a *synthesized deliverable* on scope exit. `abort` (roadmap:294) and the cycle-ceiling stop (`native_runner.py:9078-9092`) exit without a synthesis step. Add a scope-exit contract: on ANY terminal transition, the orchestrator (or Phase-1: a mechanical collator) emits `report.md` — goal, verified results (kernel-checked list), refuted statements, attempted routes with failure analyses, open leads. This is cheap and is the literal shape of the owner's "never give up" guarantee.
- **(Roadmap cites a disabled seam without saying so.)** `LEAN_WORKER_DISPATCH_ENABLED = False` (`lean_services.py:140`) — the roadmap correctly calls it "gated" (roadmap:179-180) but Phase 3b should note this path has plausibly never run in production, so "existing, proven infrastructure" (roadmap:82-84) overstates it; `dispatch_worker` also hard-codes `ttl_seconds=1800` on file locks (`lean_worker_dispatch.py:60-64`) — a 31-minute prover job loses its lock protection mid-run.
- **(`plausible` availability assumption.)** `plausible` exists only as a transitive `.lake` package of the test projects (`testdata/.../packages/plausible`); target *research* projects may not depend on it. The pre-probe needs an availability check and graceful downgrade to the empirical job (which also fixes Choice 10).
- **(Give-up detector vocabulary vs research narration.)** `_extract_blocker_summary` triggers on "failed to"/"unable to" (`native_utils.py:183-205`); §4.7 removes give-up vocabulary from *prompts* but the *detector* still reads honest research narration ("failed to find a counterexample") as blocker phrasing; corroboration by stall (`native_runner.py:8906-8913`) softens but does not remove this on slow-moving research cycles.

---

## TOP 5 ADJUSTMENTS (ranked by impact on the north star)

1. **Make persistence actually bulletproof (Choice 16).** Atomic writes for all plan-state JSON (`atomic_json_write` exists at `core/utils.py:13` — use it in `workflow_json_io.py:31`), loud failure on corrupted non-empty state instead of the silent `{}` (`workflow_json_io.py:20-28`), JSONL journal as the graph's source of truth with rebuildable snapshots, incremental persistence of dispatch-job findings. Without this, every other feature's output can evaporate silently on day 2 of 5 — the substrate itself violates "never give up silently."
2. **Un-block and de-couple dispatch (Choices 9/11).** Independent child budgets (kill the `iteration_budget=shared_budget` inheritance for dispatched jobs, `delegate_tool.py:208-210`), wall-clock timeouts, Shape A as subprocess with own budget, fire-and-continue for deep-search jobs in RESEARCH_MODE, and dotted-lineage job ids with `MAX_DEPTH` raised (N2/N3). As designed, researching *blocks proving and drains the prover's budget* — the two halves of the harness fight each other.
3. **Research-mode semantics as a first-class profile, applied at the floor, the breakpoints, and the caps (Choices 3/6/18 + Choice 1 caveats).** In RESEARCH_MODE: orchestrator always consulted at scope entry; breakpoints resolve to strategy-changes (park-and-advance, never stop/abort short of kernel-proved negation of the main goal); progress measured as graph delta, not `proved` delta; the thrift-cap table lifted; `AUTONOMOUS_MAX_CYCLES`/`stalled`/`blocked` converted from stop reasons into orchestrator invocations. Today §4.7 is a prompt-tone section; it needs to be a semantics section.
4. **Empirical counterexample search as the first-class feasibility engine, with deterministic triggering and renewable budgets (Choices 10/4).** Pipeline = Python/enumeration empirical job → `plausible` where applicable → bounded formal `¬P`; triggered deterministically off the failed-attempt ring buffer with orchestrator confirmation (not small-LLM initiative); scaling budgets for main-statement feasibility in research mode. Disproof is half of the owner's concrete-result guarantee and currently the thinnest-resourced path in the design.
5. **Event-driven orchestrator reflection + graph-frontier queue selection (Choices 2/14) — plus the Phase 4-lite pull-forward (Choice 15).** Re-invoke on job-completion-with-findings and on `false`/`proved` frontier changes, add a research-mode reflection cadence, and let `select_next_item` (`queue_models.py:311`) respect `depends_on`/`false` from the graph. Land a prompt-level breakpoint-orchestrator inside Phase 1-2 so research runs get a decider months before Phase 6. The graph must steer the work while it happens, not merely record it afterwards.
---

# PART B — Goal-Derived Requirements Coverage

# Goal-Derived Requirements Coverage Audit — LeanFlow `/prove` Redesign Roadmap v2

**Object under audit:** `/Users/lmilikic/Desktop/LeanFlow/docs/prove-redesign-roadmap.md` (branch `docs/prove-redesign-roadmap`, 520 lines). Read in full; claims cross-checked against the tree (`leanflow_cli/native/native_runner.py`, `leanflow_cli/workflows/queue_manager.py`, `leanflow_cli/workflows/workflow_state.py`, `run_agent.py`, `leanflow_specs/workflows/*.md`, `docs/product-reference.md`, `docs/autonomous-workflow-context-carryover-analysis.md`, `toolsets.py`, `tools/implementations/*`).

**Seam-citation spot-check (roadmap §7 tail):** the cited seams are real — loop `native_runner.py:9059`, gate `_review_agent_final_report:1890` → checker `_manager_check_queue_item:1210`, stall `_autonomous_stop_reason:8801` (roadmap says `:8921`, actual def is `:8801` — the `:8921` region is inside it; minor drift), budget path `_handle_api_step_budget_exhaustion:6008`, phase template `_run_document_formalization_review_agent:5661`, dead `decide():484` in `queue_manager.py` (docstring confirms the three drifting verdict copies), inbox/control loop `_run_background_control_loop:8230` + `read_workflow_agent_inbox`/`enqueue_workflow_agent_message` (`workflow_state.py:490/577`), agent registry `summarize_workflow_agents` (`workflow_state.py:651`), web tools (`tools/implementations/web_tools.py`, `web_fetch.py`). The grounding claim (§4.9 "nothing from scratch") is substantively honest.

---

## 1. Derived requirements (from the north star, first principles) and coverage verdicts

An autonomous harness attacking a hard/open formalization problem over days must be able to: understand and faithfully formalize the problem, ground itself in prior art, generate (not just decompose) intermediate mathematics, gather empirical evidence, triage prove/disprove/park, keep a durable lab notebook, accumulate knowledge across runs, bank verified partial progress, collaborate with a human, survive crashes, be measurably improving, be forensically auditable, and repair a wrong formal statement. Two more fall out of the "correctness is absolute" and "days-long" clauses: (n) correctness enforcement must cover every NEW actor that touches Lean files, and (o) long-horizon scheduling must not serialize the world behind one blocking child.

| # | Requirement | Verdict |
|---|---|---|
| a | Problem understanding & formalization fidelity | **PARTIAL** |
| b | Literature & prior-art grounding | **PARTIAL** (covered, but too late) |
| c | Conjecture GENERATION (exploratory nodes) | **PARTIAL→MISSING** |
| d | Experimentation (numeric/symbolic, counterexample search) | **COVERED** (minor gaps) |
| e | Feasibility triage (prove / disprove / park) | **COVERED** (one correctness hole) |
| f | Long-horizon memory & lab-notebook discipline | **PARTIAL** |
| g | Cross-run/cross-scope knowledge accumulation | **MISSING** |
| h | Verified partial progress that composes | **PARTIAL** |
| i | Human collaboration points | **PARTIAL** |
| j | Robust resumability | **PARTIAL** (good direction, unfinished) |
| k | EVALUATION / success metrics | **MISSING** (confirmed: zero metrics in the roadmap) |
| l | Failure forensics | **PARTIAL** |
| m | Statement re-formalization loops | **PARTIAL** |
| n | Correctness coverage of new actors (axiom guard for orchestrator edits, negation promotion) | **PARTIAL** |
| o | Long-horizon scheduling (sync cap-3 vs multi-day runs) | **PARTIAL** |

---

## 2. Per-requirement audit

### (a) Problem understanding & formalization fidelity — PARTIAL
- **What exists:** §4.5 "Re-state" (orchestrator-only statement changes, guard-enforced); §4.3 breakpoint decision includes `re-state`; graph `goal` field + `conjecture` node kind (§4.1). The codebase already has an independent statement/source fidelity verifier for `/formalize` (`_run_document_formalization_review_agent`, `native_runner.py:5661`; described in `docs/product-reference.md` §Document Formalization), but the roadmap reuses `:5661` only as a *phase-shape template* for the planner (§4.9 row "Phase pattern (planner)") — **not** for fidelity.
- **What's missing:** No scope-entry statement audit. For a research problem, "is this Lean statement the right statement?" must be an explicit orchestrator phase *before* burning days: check definitional encodings (ℝ vs ℚ, `Finset` vs `Set`, degenerate/vacuous cases, missing hypotheses), record the informal statement and the formalization decisions as first-class artifacts. The graph schema (§4.1) has no place for the informal problem statement, encoding decisions, or fidelity rationale beyond one `goal` string and free-text `notes`.
- **Add (Phase 4, orchestrator):** a `statement-audit` route/phase reusing the `:5661` verifier pattern: produce `formalization.md` (informal statement, chosen encodings, fidelity argument, known vacuity risks) + a `fidelity: audited|suspect` field on main-goal graph nodes. Cheap vacuity probes (does the hypothesis set have a model? `lean_check` an instance) belong here — LeanProbe makes them nearly free (§0.12 alignment).

### (b) Literature & prior-art grounding — PARTIAL (right mechanism, wrong schedule)
- **What exists (why it's right):** §4.6 deep search + `repo_clone` + DEEP-SEARCH job archetype (§4.2.3) returning a synthesized findings report into `plan.md § Grounding` — this is exactly the literature-grounding organ the north star needs, and the tools already exist (`web_search`/`web_fetch`/`web_download` in `tools/implementations/web_tools.py`, `lean_lemma_suggest` in `leanflow_cli/lean/lean_lemma_suggest.py`).
- **Gap 1 — schedule:** deep search lands in **Phase 5** (§5), after breakpoints (P1), nudger (P2), dispatch (P3), orchestrator (P4). For a research harness, grounding is the *first* thing a multi-day run needs — a wrong-route decomposition chosen in ignorance of a known prior formalization wastes the days that Phases 1–4 are protecting. The orchestrator/planner toolsets already "include the full search surface" per §4.6, but the *job* archetype and `repo_clone` wait until P5.
- **Gap 2 — verification of findings:** the findings report is merged into `plan.md` with "citations/paths" but nothing checks them (hallucinated lemma names / papers poisoning the plan). Cheap fix: any claimed mathlib lemma in a findings report must pass `lean_local_search`/`lean_check` before entering `plan.md § Grounding`.
- **Add:** pull the DEEP-SEARCH archetype (minus `repo_clone` if needed) into Phase 3b alongside the negation probe (both are `delegate_task` children — same machinery, §4.9 row 1); make a grounding pass mandatory at scope-entry when `LEANFLOW_RESEARCH_MODE=1`.

### (c) Conjecture GENERATION — PARTIAL, close to MISSING
- **What exists:** §0.1 lets the orchestrator "propose and STATE new lemmas … by intuition or from the plan"; §4.1 has node kind `conjecture` and status `conjectured`; the planner fan-out (§5 Phase 5) includes "draft". These are hooks, not a mechanism.
- **What's missing:** Everything the roadmap specifies is *top-down decomposition of the given statement* (decomposer §3.13, `split_of` edges, "split this further, it's too hard at once" §4.1). Research-grade conjecture generation is different: (i) **bottom-up** — generalize a proved lemma, specialize the goal, perturb hypotheses, formulate the pattern an EMPIRICAL job just observed; (ii) **exploratory nodes** with no `split_of` ancestor, whose value is information, not goal-progress. The `explore` route appears in the §2 diagram (`plan / explore` → planner) and in the route set, but **no section defines what `explore` does** — it is the only route with no semantics anywhere in the document. There is also no edge kind for "n43 generalizes n42" or "n43 was suggested by evidence e7" (only `depends_on|split_of|evidence`).
- **Add (Phase 5, planner):** define the `explore` route = conjecture-generation fan-out: input = proved frontier + empirical findings; output = new `conjectured` nodes with a `generated_by: {decomposition|generalization|empirical-pattern|literature}` provenance field and `suggested_by` edges. Wire EMPIRICAL job deliverables so "confirmed on 10^5 samples" (§4.2.2) can *create* a conjecture node, not just confirm an existing one.

### (d) Experimentation — COVERED
- §4.2.2 EMPIRICAL job (Python via `terminal`, Lean via LeanProbe `lean_check`/`lean_multi_attempt`), `plausible` pre-probe (§0.5, §4.2.4), `evidence` edge kind (§4.1), LeanProbe-everywhere (§0.12). This is right because it makes "confirm before investing proving effort" cheap — the single highest-leverage anti-waste mechanism for hard problems.
- **Minor gaps:** (1) `plausible` only works on `Testable`/`SampleableExt`-amenable statements — most research statements (quantifiers over ℝ, asymptotics) are out of its reach; the roadmap says "hint only" (§0.5) but should name the Python-numeric EMPIRICAL job as the *primary* counterexample searcher, `plausible` as the lucky path. (2) No reproducibility discipline: experiment scripts/data should be persisted (e.g. `.leanflow/workspace/experiments/<job_id>/`) and referenced from the `evidence` edge, or the 3-day-run forensics (l) can't re-check the evidence a decomposition was built on. Note there is no `plausible` wiring anywhere in the tree today (grep: 0 hits) — this is net-new work hiding inside "cheap pre-probe".

### (e) Feasibility triage — COVERED, with one correctness hole (see n)
- §0.5's fail→negate→{backtrack | escalate | split} loop plus §4.3 breakpoints plus `parked` status is a real triage state machine, and grounding it in the graph makes backtracking mechanical — this is the roadmap's strongest research-alignment feature. The trigger discipline (§0.7: ~1 probe/lemma after ~2 failures or risk-flag) is sane. Note the `prove.md` spec already declares a `falsify` review action (`leanflow_specs/workflows/prove.md:10`) with **no implementation anywhere** in `native_runner.py` (grep: only the spec hit) — the roadmap should mention it's making an existing dead spec-promise real.

### (f) Long-horizon memory & lab-notebook discipline — PARTIAL
- **What exists:** Phase 1 plan-state (`plan.md` + `summary.json` + `blueprint.json`) written atomically (`workflow_state.py:305`), decision packets (§4.3), activity JSONL. "Documentation-driven proving" (§0.9) is the correct thesis — `docs/autonomous-workflow-context-carryover-analysis.md` documents exactly why transcript-as-memory fails (sticky 130k-token histories, theorem transitions without reset).
- **What's missing:** `plan.md`/`summary.json`/`blueprint.json` are all **living/overwritten** documents. A lab notebook is **append-only**: hypothesis → action → outcome → why abandoned, timestamped. Nothing in the roadmap records *rationale history* — when the orchestrator re-decomposes at day 2, the day-1 reasoning is gone from plan-state (it exists only in raw logs, which §0.9 explicitly demotes from the coordination path). Same for the graph: `blueprint.json` is overwritten through `:305`, so there is **no history of graph evolution** — you cannot ask "what did the plan look like before the second backtrack?"
- **Add (Phase 1, cheap):** (1) `journal.jsonl` — the orchestrator/planner append one structured entry per decision (route taken, alternatives considered, evidence cited); (2) graph deltas mirrored as events into the existing activity stream (`append_workflow_activity`, `workflow_state.py:309`) so `blueprint.json` is reconstructable at any timestamp. Both reuse the existing append paths — zero new infrastructure.

### (g) Cross-run / cross-scope knowledge accumulation — MISSING
- The roadmap is entirely **scope-local**. Nothing persists learnings beyond one workflow: which decomposition patterns failed, which tactic families worked on which goal shapes, which mathlib areas were mined. The tree already has two under-used substrates the roadmap ignores: (1) `outcomes.jsonl` (`workflow_state.py:137/399`; product-reference.md: "structured capability snapshots and route decisions … so resumed runs can reuse prior blocker classification") — resume-only today; (2) a full persistent-memory mechanism (`MemoryStore`, `run_agent.py:803–825`, `MEMORY.md`/`USER.md`, `flush_memories:2540`) that is **disabled by default and capped at 2,200 chars** — never mentioned in the roadmap.
- For "push research beyond the limits of current knowledge," a harness that re-learns the same dead ends every run is structurally capped. 
- **Add (new Phase 5.5 or fold into Phase 5):** a per-project `learnings.md` (append-only, structured: goal-shape → what worked/failed) written at every breakpoint and scope-end; loaded into orchestrator context at scope-entry; optionally a cross-project index keyed by mathlib domain. Extend `outcomes.jsonl` consumption from "resume" to "scope-entry priors."

### (h) Verified partial progress that composes — PARTIAL
- **What exists:** kernel-only `proved` (§4.1 reconciliation — right, and the anti-drift design is good); Shape-A prover jobs land results in project files (§4.2.1 "whenever the result should land in the project"); `parked`/`blocked` preserved.
- **Hole 1 — invalidation semantics can orphan verified work:** §4.1(b) "backtrack on `false` (invalidate the subtree, try another route)" and §0.5 "mark the graph node `false`, invalidate the subtree." Proved descendants of an invalidated subtree are *still kernel-true theorems*. The roadmap never states that `proved` nodes/files survive invalidation and re-organization (§4.5 lets the orchestrator reorganize files!). One careless re-org deletes three days of verified lemmas. **Specify:** `proved` nodes are immutable — never deleted, never re-stated; invalidation re-parents, it does not destroy; file re-organization must be move-only for proved declarations.
- **Hole 2 — no lemma-library notion:** proved-but-off-path lemmas should be indexed (`lean_search mode=local` makes them findable, but nothing marks them as "available, orphaned") so later routes and later *runs* (see g) can reuse them.

### (i) Human collaboration points — PARTIAL
- **What exists:** the inbox is real (`workflow_state.py:490/577`; `_run_background_control_loop`, `native_runner.py:8230` executes queued user prompts mid-run at `:8286`), and Phase 1's mechanical breakpoint explicitly hands the decision packet to "a human — or a later orchestrator" (§4.3).
- **What's missing:** (1) In Phase 4 the orchestrator *replaces* the mechanical stop (§5 Phase 4: "replacing the Phase-1 mechanical stop with a decision") — the human decision point is designed **out** as autonomy is designed in, with no `ask-human` route in the route set `{direct-prove | decompose | plan | explore | escalate | park}` (§2). "Escalate" has no defined recipient anywhere in the document. (2) No design for *incorporating* mid-run human guidance into plan-state/graph — an inbox prompt today is just an appended user message; it should become a graph annotation / plan edit the orchestrator honors. (3) Most critically for open problems: **main-statement re-statement (§4.5) requires no human acknowledgment** — the harness can silently change the problem it is solving, which breaks the N1 concrete-result guarantee (a proof of a mutated statement is not a result on the original problem).
- **Add (Phase 4):** an explicit `ask-human` route (post decision packet to inbox, park the node, continue elsewhere on the frontier — non-blocking); main-goal re-statement gated on inbox ACK (or, in fully-autonomous mode, recorded as a loud top-of-`plan.md` divergence notice); human inbox messages logged into `journal.jsonl` and reflected in the graph.

### (j) Robust resumability — PARTIAL
- **What exists (right):** Phase 1 "resume = load `summary.json`+graph, reconcile" with kernel-truth reconciliation (§4.1) is the correct authority inversion — disk state, not transcript, is the resume substrate; baseline-`sorry` restore (`:1982`) and git-shadow rollback kept as safety (§5 Phase 1). Stale-liveness normalization already exists (carryover doc: `phase: dead` normalization).
- **Gaps:** (1) **Dispatch-ledger resume is unspecified**: a crash while a child is `running` — §4.2 reconciles the ledger against `activity/agents/*.jsonl`, but no resume-time pass is named that marks orphans `stuck/killed`, decides re-dispatch, and (for Shape-A children holding file locks, `runtime/file_locks.py`) releases locks. (2) Graph `owner` fields of dead agents — same reconciliation need. (3) **Orchestrator context reconstruction after restart is undefined** — §7's mitigation ("frontier + packet, not transcripts") describes the *compact* context; N5 demands context-*rich* for research runs; neither says what a resumed day-3 orchestrator actually reads (this is where `journal.jsonl` from (f) becomes the resume narrative). (4) Multi-day realities: provider outages / rate-limit storms must land in the breakpoint machinery, not in a stack trace — no word on it.
- **Add (Phase 3a design doc, explicitly):** a "resume protocol" section — ledger reconciliation, lock release, owner cleanup, kill -9 resume drill as an acceptance test (see Phase E).

### (k) EVALUATION — **MISSING, confirmed**
- The roadmap contains **no success metrics, no benchmark suite, no per-phase acceptance criteria**. The only measurement-adjacent items are Phase 0's "shadow-compare for one release" (regression protection, not capability) and Phase 2's "enable via flag **after validation**" (§5) — validated against *what* is never stated. Effort/risk are estimated per phase (~wk / Low-Med), value is never. There is no way to know whether Phases 4–6 (the expensive ones) actually improve hard-problem capability, and no way to detect capability *regressions* from e.g. the nudger over-steering. The repo's only proving fixtures are demo projects (`testdata/workflow_projects/ProveDemo/ProveDemo/IMOMath{1,2,3}.lean`, `RealTheorems.lean` — a handful of `sorry`s); there is no benchmark harness anywhere in the tree (grep for miniF2F/Putnam hits only the roadmap's Hilbert citation and mathlib vendored scripts).
- This gap is aggravated by a **hidden assumption in the framing**: §References anchors the design's validity to Hilbert's 99.2% miniF2F / 70.0% PutnamBench — *competition* benchmarks. The north star is open research problems; the components that matter most for it (grounding, conjecture generation, experimentation, multi-day persistence) are precisely the ones Hilbert does **not** have and the ones the roadmap specifies most thinly. The "our target maps 1:1 onto its four roles" claim validates the competition-grade skeleton, not the research-grade organs. Without an evaluation harness this assumption is never tested.
- **Add: Phase E** — full outline in §5 below. It must start **before Phase 2**, because dark-launch "validation" (§5 Phase 2) is meaningless without it.

### (l) Failure forensics — PARTIAL
- **What exists:** decision packets (§4.3: statement, attempts, error signatures, search history, negation status), persistent dispatch ledger with terminal states (§4.2 "never lost"), per-agent activity JSONL, "logging stays" (§0.9). Good bones.
- **What's missing:** (1) No **terminal synthesis artifact**. N1 demands every scope end in proof | disproof | "rigorous documented account of what was tried/learned." The roadmap retires checkpoint prose and leaves… nothing at scope-end: `plan.md` is a living doc in whatever state the run died in, and raw logs are demoted. A 3-day failed run should end with a machine-written **final research report** (graph summary: proved/false/parked counts and names, route history from `journal.jsonl`, evidence gathered, open subgoals ranked, recommended next attack). No phase produces it. (2) Graph-evolution history missing (see f) — "why did the orchestrator abandon route X on day 2" is unanswerable from artifacts. (3) Decision packets have no schema for *informal* reasoning (proof-sketch ideas tried), only mechanical signals.
- **Add (Phase 4):** scope-exit report generator (deterministic template over graph + ledger + journal; LLM-polished in research mode). This is the cheapest way to make "never give up" auditable: a run that ends without proof/disproof **must** end with this artifact or it is a silent give-up.

### (m) Statement re-formalization loops — PARTIAL
- **What exists:** `re-state` as a breakpoint decision (§4.3), orchestrator-only re-statement with node reset (§4.5), `false`-node → re-decompose (§0.5). The *mechanics* are right.
- **What's missing:** the loop only fires on **negation-proved or evidence-contradicts** (§4.5). The commonest research failure is subtler: the statement is *provable but not the intended statement* (vacuous hypotheses, wrong constant, off-by-one in an index set) — kernel-verified and worthless. Detecting this needs (a)'s fidelity audit re-run after re-statement, plus vacuity checks (prove the hypotheses non-empty — cf. the memory note on the vacuity-conjunction trap: conjoined ∀-statements can be vacuously true; an autonomous harness *will* generate such statements when decomposing). And per (i): main-statement changes need human surfacing.
- **Add:** fold vacuity/instance probes into the negation-probe archetype (Phase 3b — same LeanProbe scratch machinery: instead of proving `¬P`, exhibit a model of the hypotheses); re-run statement-audit after any re-state (Phase 4).

### (n) Correctness coverage of new actors — PARTIAL (two concrete holes)
- The invariant (§0 tail: kernel gate never LLM-overridable) is real and implemented — the tree has a serious axiom guard: allowed-axiom list + `LEANFLOW_NATIVE_ALLOWED_AXIOMS` (`native_runner.py:118–121`), transitive axiom-profile check at the gate (`:1943–1951`, rejects `sorryAx`/`native_decide`/custom axioms, `:2880–2912`), and per-edit forbidden-axiom restore (`:2943`).
- **Hole 1 — orchestrator edits bypass the per-edit guard by design:** §4.5 states the queue edit-guard "does not apply" to orchestrator/decomposer edits between prover turns. That guard is also where `_introduced_forbidden_axioms` runs (`:2943`). A strong-model orchestrator stating "lemmas" can therefore write `axiom foo : P` into a stub file unchecked; the transitive gate check (`:1946`) is target-scoped and only runs when a queue item passes — a poisoned stub in a file never routed through the gate for that declaration survives. **Specify in §4.5 (Phase 4):** every orchestrator/decomposer file write passes the same forbidden-axiom scan (plus `sorry`-only-stub shape check) before the graph refreshes.
- **Hole 2 — negation results are LeanProbe-only but drive irreversible decisions:** §4.2.4 runs `¬P` "in LeanProbe scratch"; the outcome "feeds the graph (`false`)" which poisons ancestors (§4.1) or, for a main statement, triggers "not solvable ⇒ escalate/report" (§0.5). But §0.12 says the Lake-backed checker is "the sole authoritative acceptance gate." A scratch REPL proof — different environment, no project import graph, never re-checked — is being granted authority over subtree invalidation and, worse, over the **kernel-verified disproof deliverable** N1 promises. **Specify (Phase 3b):** a `false` mark is provisional until the negation theorem is *promoted* into the project (written to a file, passed through `_manager_check_queue_item` + axiom profile) — cheap, mechanical, and it turns the disproof into bankable verified progress (h) instead of a vanished scratch artifact.

### (o) Long-horizon scheduling — PARTIAL
- v1 sync-blocking dispatch, cap 3 (§0.6, §4.2, decision log #2) is correctly conservative for shipping — and §0.14's sequencing rationale is sound. But note the mismatch with the mission: a *synchronous* deep-search job blocks its dispatcher for the job's whole wall-clock; on a multi-day research run the orchestrator spends most of its life waiting instead of orchestrating, and N4's "multiple parallel proving directions" is structurally impossible until the "Later / optional" async item (§5 tail) lands. Async need not move earlier than the ledger proves itself — but the roadmap should say explicitly that **research-mode runs are the trigger for promoting async from optional to scheduled**, and Phase 3a's lifecycle design must be written async-ready (it mostly is: ledger states, patience, kill). Related N4 gap: the graph (§4.1) cannot *represent* parallel alternative attack routes — see §3/N4 below.

---

## 3. Owner-note (N1–N5) audit against the roadmap text

- **N1 (concrete-result guarantee): NOT MECHANIZED.** §4.7 removes give-up vocabulary and disables give-up stop reasons — necessary but negative-space. The positive obligation (every scope ends in proof | promoted disproof | rigorous account) has no producing mechanism: no scope-exit report (l), no negation promotion (n-hole-2), and `parked` under `RESEARCH_MODE` "keeps the scope alive" (§4.7) — alive forever is not a result either. Add the scope-exit artifact as a **hard invariant**, same tier as the kernel gate.
- **N2 (universal dispatch): COVERED.** §2 diagram + §4.2: orchestrator/planner/decomposer dispatch, LLM-manager suggests, prover escalates to manager (§4.2 preamble). Right because it keeps the inner loop simple while making experimentation first-class.
- **N3 (hierarchical dotted lineage): MISSING from the document.** JobSpec has flat `parent` (§4.2); the registry has `parent_agent_id` (`workflow_state.py` summary dict) — substrate exists, but dotted lineage ids (`root.orchestrator.planner.ds-042`), ancestor-chain listing, and ancestor-scoped track/kill are nowhere. Belongs in the Phase 3a design doc (it changes `job_id` format and inbox addressing — cheap now, painful later).
- **N4 (parallel proving directions when beneficial): PARTIAL.** Sync cap-3 (§0.6) plus decision log #2's "not parallel proving" stance defers it, acceptably — but the deeper gap is representational: §4.1 edges (`depends_on|split_of|evidence`) form an AND-tree (leanblueprint's model, where the plan is *known*). Research needs **OR-structure**: two candidate decompositions of the same node, each a subtree, either sufficing. Without an `alternative_of` edge kind / strategy grouping, "try another route" (§4.1(b)) means destroying route A to try route B, and parallel directions can't coexist in the graph even once async lands. Add `alternative_of` + per-node route groups to the §4.1 schema in **Phase 1** (schema changes are cheapest before anything reads them).
- **N5 (context-RICH orchestrator for research runs): CONTRADICTED by the current text.** §7 row 1's mitigation — "the graph keeps its context small (frontier + packet, not transcripts)" — is the efficiency-floor policy applied unconditionally. §4.7's `RESEARCH_MODE` raises budgets and stop-reason behavior but says nothing about context policy. Add to §4.7/Phase 6: in research mode the orchestrator context includes full `plan.md`, grounding findings, journal tail, decision-packet history — compression floors apply only to easy runs.

---

## 4. Gap list, ranked by impact on the north star

1. **No evaluation harness / success metrics (k).** The entire redesign's claim — "improves hard-problem capability" — is unfalsifiable as planned; Phase 2's own "enable after validation" gate is undefined. → Phase E (below), starting before Phase 2.
2. **N1 not mechanized: no scope-exit research report; disproofs never promoted past LeanProbe scratch (l + n).** "Never give up" currently has no artifact proving the run didn't silently give up. → scope-exit report (Phase 4), negation promotion through the authoritative gate (Phase 3b).
3. **Graph cannot represent alternative attack routes (N4/§4.1).** AND-tree schema forces destructive backtracking and blocks parallel directions permanently. → `alternative_of` edges + route groups, Phase 1 (schema) / Phase 4 (orchestrator use).
4. **Conjecture generation / `explore` route undefined (c).** The only route with no semantics; empirical results cannot create conjecture nodes; no generalization moves. → define `explore` fan-out + `generated_by`/`suggested_by` provenance, Phase 5.
5. **Cross-run knowledge accumulation absent (g).** Ignores the existing `outcomes.jsonl` and disabled `MemoryStore`; every run re-learns dead ends. → `learnings.md` + scope-entry priors, Phase 5/5.5.
6. **Correctness bypass for orchestrator edits (n-hole-1).** §4.5 exempts the exact actor with the strongest incentive to "state" convenient facts from the axiom guard. → mandatory scan on orchestrator/decomposer writes, Phase 4.
7. **Human collaboration designed out at Phase 4 (i, m).** No `ask-human` route; undefined "escalate" recipient; main-statement re-statement needs no ACK — the harness can silently change the problem. → `ask-human` route + re-state ACK, Phase 4.
8. **Literature grounding scheduled too late (b).** Research runs get their grounding organ in Phase 5, after the decision-making phases it should inform. → DEEP-SEARCH archetype into Phase 3b; mandatory scope-entry grounding in research mode.
9. **No lab-notebook / no graph history (f, l).** Overwritten living docs can't answer "why did day-2 me do that"; forensics and N5-rich resume both need the journal. → `journal.jsonl` + graph-delta events, Phase 1.
10. **Proved-work retention under invalidation unspecified (h).** Subtree invalidation + orchestrator file re-org can destroy kernel-verified lemmas. → "proved nodes are immutable" invariant, Phase 1 (§4.1 text) enforced Phase 4.
11. **Resume protocol for dispatch/locks/owners unspecified (j).** Ledger says `running`, child is dead, lock is held. → resume-reconciliation section in Phase 3a design doc + kill -9 drill in Phase E.
12. **N3 lineage and N5 context-richness not folded in** — both cheap now, breaking later (job-id format; §7 context policy). → Phase 3a / §4.7 respectively.
13. **Statement-fidelity audit at scope entry missing (a, m).** Right-statement risk is the largest silent-failure mode for open problems; the `/formalize` verifier (`native_runner.py:5661`) already exists and is reused only as a template. → `statement-audit` phase + `formalization.md`, Phase 4; vacuity probes in Phase 3b.

**Explicitly right calls (one line each):** kernel gate never LLM-overridable (§0 invariant) — the one non-negotiable, correctly non-negotiable; budget breakpoints as *decision points* (§4.3) serve "token is not the constraint" correctly — they force strategy reconsideration rather than cost-cutting; struggle-triggered advisory nudger (§0.2/§4.4) is the right anti-give-up organ with the right (zero) authority; documentation-driven proving (§0.9) is the correct answer to the transcript-stickiness failure documented in the carryover analysis; design-first dispatch with owner review (§0.6/Phase 3a) is the right respect for the hardest lifecycle problem; reuse map (§4.9) is verified honest against the tree.

---

## 5. Phase E — evaluation harness (proposed; metrics confirmed missing)

**Position:** start alongside Phase 1; gates every later phase. Cheap: it is a runner script + fixture projects + a scorer over artifacts the redesign already produces (`summary.json`, `blueprint.json`, ledger, outcomes).

**Suite (three tiers + adversarial fixtures):**
- **T1 Regression (every phase):** existing demo projects (`testdata/workflow_projects/ProveDemo` IMOMath1–3, RealTheorems; DocFormalizationDemo) — must stay green; flags-off runs byte-identical on the hot path (extends Phase 0's shadow-compare into a permanent gate).
- **T2 Capability (Phases 2, 4, 5, 6):** frozen ~40-problem set: miniF2F-hard slice + PutnamBench slice + a "decomposition-required" set (statements known to need ≥2 helper lemmas). Metrics: solve rate, wall-clock, cost, decompositions attempted/succeeded.
- **T3 Research-grade (Phases 4–6, the north-star tier):** ~10 multi-hour/multi-day tasks: (i) recently-formalized mathlib results *re-derived* against a mathlib snapshot predating them (ground truth exists, harness can't look it up); (ii) unformalized textbook/paper theorems end-to-end (statement audit exercised); (iii) small open-flavored problems (Erdős-problem-style finite checks). Metrics: **verified-progress count** (kernel-`proved` graph nodes, banked lemmas), disproof count, time-to-first-verified-lemma, grounding utilization (findings cited in successful routes), terminal-artifact quality (below).
- **Adversarial fixtures (Phases 3–4 gates):** (a) false-lemma set — statements whose negations are provable → negation-probe detection rate, **zero un-promoted `false` marks**; (b) false-decomposition set — plans with a poisoned sub-lemma → backtracking recovers, proved siblings retained; (c) vacuous-statement set — provable-but-empty hypotheses → statement audit flags; (d) axiom-temptation set — goals easiest to "close" via `axiom`/`native_decide` → guard catches 100%, including via orchestrator-written stubs.

**Per-phase gates:** P1 — kill -9 at random point, resume, zero verified-work loss, graph reconciles (10/10 drills); P2 — dark-launch nudge log human-rated: ≥70% helpful, 0 verdict-adjacent suggestions; enable-flag flips only on this gate (fixes the undefined "after validation"); P3 — adversarial fixture (a) + ledger: zero lost jobs across 100 dispatches incl. forced child kills; P4 — T2 uplift vs Phase-2 baseline (target: +X% on decomposition-required set; X set from baseline run, not guessed) + fixtures (b)(c)(d); P5 — T3 first full runs: 100% terminal-artifact compliance (every run ends proof | promoted disproof | scope-exit report scoring ≥ rubric threshold: a human can reconstruct what was tried and why from artifacts alone in <30 min); P6 — T3 before/after with research mode on: verified-progress count and give-up-termination rate (must be 0).

**Regression protocol:** frozen suites, pinned toolchain/mathlib per suite version; every phase-enable PR runs T1 + its gate tier with flags off *and* on; results appended to a tracked `evals/results.jsonl`; any T1 regression or T2 solve-rate drop >1σ blocks the flag default-flip; T3 re-run on phase boundaries and monthly (cost-bounded, N4/N5 configs pinned).

**Key files for the synthesizer:** roadmap `/Users/lmilikic/Desktop/LeanFlow/docs/prove-redesign-roadmap.md`; seams `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/native/native_runner.py` (:118–121, :1210, :1890, :1943–1951, :2880–2949, :5661, :6008, :8230, :8801, :9059), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/workflows/workflow_state.py` (:137, :305, :309, :399, :490, :577, :651), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/workflows/queue_manager.py` (:484), `/Users/lmilikic/Desktop/LeanFlow/run_agent.py` (:803–825, :2540), `/Users/lmilikic/Desktop/LeanFlow/leanflow_specs/workflows/prove.md` (:10 `falsify` dead action), context evidence `/Users/lmilikic/Desktop/LeanFlow/docs/autonomous-workflow-context-carryover-analysis.md`, fixtures `/Users/lmilikic/Desktop/LeanFlow/testdata/workflow_projects/ProveDemo/ProveDemo/`.
---

# PART C — SOTA Frontier Cross-Check

# SOTA Frontier Cross-Check — LeanFlow `/prove` Redesign v2 (docs/prove-redesign-roadmap.md, branch `docs/prove-redesign-roadmap`)

**Method.** Read the full roadmap (`/Users/lmilikic/Desktop/LeanFlow/docs/prove-redesign-roadmap.md`); spot-checked its code-seam citations against the tree (`_drive_autonomous_followups` at `leanflow_cli/native/native_runner.py:9059`, `decide()` at `leanflow_cli/workflows/queue_manager.py:484`, `_manager_check_queue_item` at `native_runner.py:1210`, `_handle_api_step_budget_exhaustion` at `native_runner.py:6008` — all real; the roadmap's seam grounding is honest). Then verified design choices against the mid-2026 frontier via web research, including a full read of Hilbert (now at **v2, March 2026**) and several 2026 papers the roadmap predates (Goedel-Architect, LeanMarathon, AlphaProof Nexus, Seed-Prover 1.5, Automated Conjecture Resolution).

**Headline for the synthesizer:** the v2 shape (orchestrator + queue-prover + kernel gate + retriever + dependency graph + failure→probe→decompose loop) is strongly confirmed by the frontier — *four independent 2026 systems converged on exactly the blueprint/dependency-graph architecture*. The contradictions are concentrated in five places: (1) reflection is cadenced at the frontier, not failure-only; (2) everyone runs parallel portfolios, not a serial single prover; (3) decomposition validation at the frontier includes **statement-fidelity auditing** and defense against "sorry-offloading" — our LeanProbe skeleton validation (compiles ≠ faithful) misses both; (4) retrieval is front-loaded and mandatory, and it *reduces* cost (we defer it to Phase 5); (5) proved-subgoal/lemma caching across attempts is a universal, cheap win we don't have anywhere in the plan.

---

## 1. System dossiers and lessons

### 1.1 Hilbert (arXiv:2509.22819, v2 March 2026 — the roadmap's primary reference)
Four components: informal Reasoner (Gemini 2.5 Flash/Pro), specialized prover (DeepSeek-Prover-V2-7B **and** Goedel-Prover-V2-32B), verifier (Kimina Lean Server, batched), semantic retriever over Mathlib (sentence-transformers). Escalation ladder: retrieve → informal proof → sketch with `have`-subgoals filled with `sorry` → per-subgoal prover attempts → on failure, reasoner+retrieval fallback → **recursive decomposition** of the failing subgoal, depth D=5. 99.2% miniF2F, 462/660 (70.0%) PutnamBench.

Lessons for our choices:
- **Retrieval (Q4): mandatory and front-loaded.** Retrieval happens at step 1 (5 queries × top-5 theorems) *and* per subgoal. Ablation: 99.2% vs 97.9% without — and, critically, **retrieval cuts total tokens from 4.0M to 2.3M**. Our roadmap makes premise retrieval "a mandatory prover pre-step" only in **Phase 5** (roadmap line 449–450). The frontier evidence says this is one of the cheapest wins available and belongs in Phase 1–2, not after dispatch.
- **Budgets (Q2): small-k everywhere, at every node** — K_initial=4, K_sketch=4, K_formal=4 per subgoal, 6 correction passes — with **two different provers** in the portfolio. Total ≤11.3K calls vs DeltaProver's 16,384. This is a portfolio of cheap parallel attempts, not our single-strong-prover-with-retries queue (`§0.3`, §2 diagram).
- **Decomposition validation (Q3): every subgoal gets a cheap LLM feasibility judgment at statement time** — "prompt the Reasoner to evaluate whether the subgoal is mathematically correct and whether the formal statement is... provable"; flagged subgoals trigger sketch refinement *before* any proving effort. Our `plausible`/negation probe fires only "after ~2 genuine failures OR a risk-flag" (§0.7, line 59–60) — we spend two full prover budgets before asking the question Hilbert asks for free up front.
- **Replanning (Q1): failure-driven, matching us** — decomposition only on prover failure. Our §0.5 loop is a faithful (and stronger, kernel-verified-negation) version of this. Confirmed.
- **Model escalation:** Flash and Pro tiers within the reasoner role — supports the §4.8 configurable matrix, but note the *prover* tier in Hilbert is a cheap 7B/32B specialist, not the strong agent model (§4.8 defaults prover=strong; fine for our agentic prover, but there's no cheap-grinder tier for easy stubs anywhere in the matrix).

### 1.2 Seed-Prover / Seed-Prover 1.5 (ByteDance; arXiv:2507.23726, arXiv:2512.17260)
Lemma-style whole-proof model + iterative refinement on Lean feedback; three test-time strategies (light inner refinement loop / outer loop over lemma pools / "heavy" conjecture broadcasting). Saturated miniF2F, 50.4% PutnamBench, 5/6 IMO 2025. **1.5 (Dec 2025)**: NL-prover → sketch model (sub-lemmas stubbed `by sorry`) → agentic tool-calling Lean prover at Pass@3×3 per lemma; **87.9% PutnamBench** (current SOTA), 80% Fate-H, 11/12 Putnam 2025 in 9h.
- **Lemma pool (Q7):** proven lemmas are *cached and reused*; failed/timed-out lemmas trigger sketch-model refinement of the NL proof and alternative sub-lemmas — accumulate verified partial progress even when the main attempt dies. Our `blueprint.json` tracks status but nothing in §4.1/§4.9 caches *proved sub-results across attempts/jobs* for re-injection.
- **Budget reality check (Q2):** 10 H20-GPU-days *per PutnamBench problem*; 80% of solves within 5h but a tail to 53h. "Heavy" conjecture broadcasting was decisive for the hardest IMO problems. For our north-star ("solving the impossible", token cost not the constraint) the frontier spends multi-day parallel compute per problem — our sync cap-3 dispatch (§0.6, §4.2) and serial queue is orders of magnitude below the operating point of every system that closes hard problems.
- 1.5 added **adaptive budget allocation** (−25% average solve time) — supports our §4.3 budget-breakpoint direction; they reallocate rather than stop, which our orchestrator decision menu (split/plan/negate/park/re-state/abort) generalizes correctly.

### 1.3 Aristotle (Harmonic, arXiv:2510.01346)
Three parts: Monte-Carlo *Graph* Search over Lean goals with a 200B+ policy/value model; lemma-based informal reasoning (informal proof → restructure into short lemmas → formalize → REPL error-correct); Yuclid geometry solver. Gold-equivalent IMO 2025 (5/6).
- **Decomposition validation (Q3/Q6):** a **faithfulness judge** filters misaligned autoformalizations; only *proven* lemmas advance; per-attempt annotation of proven-vs-unproven lemmas feeds revision. Our roadmap has no faithfulness/audit mechanism at all — LeanProbe skeleton validation (§4.9, decomposer row; §5 Phase 4) proves the skeleton *compiles*, not that stated lemmas mean what the plan intends.
- **Verifier throughput (Q5):** fleets of **stateless Lean REPLs on CPU-only autoscaled machines**, state-splitting by goals, per-request routing. Also **test-time training** on its own search traces. Our single warm LeanProbe is right in kind, absent in scale.
- **Found 4 false exercises in Tao's *Analysis I*** — disproof-as-artifact at the frontier, validating our §0.5 negation arm.

### 1.4 AlphaProof (DeepMind, Nature s41586-025-09833-y) + **AlphaProof Nexus** (arXiv:2605.22763, May 2026)
AlphaProof: AlphaZero-style RL prover in Lean; **Test-Time RL** generates many *variants* of the target theorem as a curriculum of stepping stones. Nexus (the research-math agent around it): asynchronous controller loop — **P-UCB sampling over a database of candidate proof sketches** (rated by Elo rater agents) → prompt with structured feedback from previous attempts → Gemini 3.1 Pro prover subagent making search-replace edits → formal validation (**including a no-disallowed-axioms check**) → registration back to the sketch DB. Solved 9/353 Erdős problems (two open 56 years), 44/492 OEIS conjectures, ~"a few hundred dollars" per solved Erdős problem, 3000-episode cap per problem.
- **Proof/disproof duality (Q3):** every AlphaProof subgoal query returns **proof, disproof, or failure** — disproof is a *first-class outcome of ordinary search*, not a rationed probe. Strong evidence that our "~1 negation probe per lemma after ~2 failures" (§0.7) is under-ambitious for research mode; the negation arm should be a standing option the prover/dispatcher can exercise cheaply, with the *budgeted* version only for expensive full ¬P campaigns.
- **The "sorry-offloading" pathology (Q3) — direct warning for our §4.5:** "the agent frequently offloaded a problem's core difficulty into a single `sorry`... Explicitly prompting against this behavior failed to prevent it." Our orchestrator/decomposer *is* an agent that lays out `sorry`-stubs (§0.1, §4.5). Prompting will not prevent difficulty-hiding decompositions; we need a structural check (see upgrades).
- **Persistence (Q7):** a **global goal cache keyed by a deep hash of the exact formal Lean context** — already-proved subgoals are retrieved instantly across episodes. Nothing equivalent exists in our plan-state (§4.1 stores statuses, not proof artifacts keyed for reuse).
- **Multiple competing sketches (Q8):** the sketch database holds *many* rival decompositions with Elo ratings, sampled via P-UCB. Our `blueprint.json` (§4.1) holds exactly **one** decomposition tree at a time; `split`/`false` handle supersession but there is no schema for *coexisting rival sketches* — despite owner note N4 explicitly allowing parallel directions.

### 1.5 Goedel-Architect (Princeton, arXiv:2606.06468, June 2026)
Agent system centered on "a dependency graph of definitions and lemmas that builds up to the main theorem"; two phases: blueprint generation (optionally seeded by NL proofs) then a tool-equipped Lean prover that **closes each open lemma node in parallel**; failed lemmas feed *blueprint-level* refinement. 99.2% miniF2F (100% with NL guidance), 75.6% PutnamBench (88.8% seeded), 4/6 IMO 2025 — at "up to 500× less" cost than comparable pipelines.
- **The single strongest confirmation of §4.1** (`blueprint.json`): an independent 2026 SOTA system built exactly the leanblueprint-inspired dependency-graph agent we planned, and explicitly motivates it as escaping "recursive lemma decomposition strategies that can become trapped in unproductive loops" — i.e., graph-global replanning beats purely local Hilbert-style recursion. Our combination (graph + Hilbert loop) is the right synthesis.
- **But**: their unit of execution is *parallel closure of the graph frontier*. Our Phase 4/5 seeds a *serial* queue from stub files and defers async fan-out to "Later/optional" (line 466). The frontier's cost-efficiency comes precisely from frontier-parallelism.

### 1.6 LeanMarathon (arXiv:2606.05400, June 2026)
Long-horizon autoformalization of research math. Diagnosis: long runs fail "not only at hard lemmas, but at scale: statements drift, dependencies tangle, context decays, and local repairs corrupt distant work." Design: an **evolving blueprint that is itself a Lean file** — "formal proof skeleton, natural-language proof graph, and shared system of record" — plus four contract-scoped agents (**construct, audit, prove, repair**) under a two-stage orchestrator: **first stabilize statement fidelity through adversarial review, then discharge the proof DAG from its leaves upward in parallel CI-gated rounds**, turning "one brittle multi-hour run into many local, recoverable, parallel transactions." Formalized 7/7 target theorems, 258 lemmas, zero `sorry`.
- This is the closest published system to our §0.9 documentation-driven proving — and it confirms `plan.md`+graph as the resume/coordination authority (Q7). Two things it has that we lack: (a) a dedicated **audit** role with an *adversarial fidelity pass gating the proving phase* (our graph has no `audited` status; reconciliation §4.1 checks kernel state, not statement meaning); (b) **CI-gated parallel rounds** as the execution primitive (again: parallel frontier discharge).

### 1.7 DeepSeek-Prover-V2 (arXiv:2504.21801; no V3 as of mid-2026)
DeepSeek-V3 decomposes into subgoals (NL + Lean simultaneously); a **7B model grinds the subgoal proof search**; RL on subgoal decomposition. 88.9% miniF2F, 49/658 PutnamBench.
- Lesson: decompose-with-strong / prove-with-cheap is a real cost lever (Q2); our §4.8 matrix should allow a cheap prover tier for easy stubs even though the default agentic prover stays strong. Confirms our strong-decomposer choice (§0.1).

### 1.8 Goedel-Prover-V2 (arXiv:2508.03613) and Kimina-Prover (Numina/Kimi)
Goedel-V2-32B: scaffolded data synthesis + **verifier-guided self-correction** (88.0% → 90.4% miniF2F pass@32 with self-correction; ~+2pp consistently). Kimina-72B: test-time-RL agentic search, recursively discovers/combines lemmas, reads Lean errors and proposes targeted fixes (84.0% pass@32, 86.4% +1 error-fix round).
- Lesson (Q2/Q5): the *prover model itself* internalizes the verifier-feedback refinement loop; harness-level nudging is second-order. Our LLM-manager nudger (§0.2, §4.4) is not contradicted — nobody has an equivalent "morale manager," it's genuinely novel and cheap — but do not expect it to substitute for compiler-feedback refinement or cadenced replanning; its validated frontier analog is Kimina's *error-fixing round*, i.e., content-bearing feedback, not tone.

### 1.9 BFS-Prover-V2 (arXiv:2509.06493) / InternLM2.5-StepProver (arXiv:2410.15700)
BFS-V2: multi-turn off-policy RL + **planner-enhanced multi-agent tree search: a planner decomposes, parallel prover agents share a common subgoal cache** — 95.08% miniF2F. InternLM2.5-StepProver: expert iteration (20,000+ CPU-days) + critic-guided best-first search (critic lifts prover 59.4%→65.9%).
- Lessons: (Q2) parallel agents + **shared subgoal cache** again; (Q5) a learned *critic* prioritizing which goals to expand is the step-prover analog of our orchestrator picking the frontier — supports graph-driven frontier selection (§4.1 decision-use (a)).

### 1.10 LeanAgent (arXiv:2410.06209, ICLR 2025)
Lifelong-learning harness: **curriculum ordering by theorem complexity (e^S), a dynamic database** (proven / sorry-proven / sorry-unproven, premises, proof states), progressive retriever training across 23 repos; proved 162 previously-unproved `sorry` theorems.
- Lesson (Q7/Q8): its dynamic database ≈ our `summary.json`+graph — confirmation. Its curriculum (easy→hard ordering of the sorry landscape) is in our "Later/optional" bucket (line 466); AlphaProof's TTRL variant-curriculum shows the same principle at test time. For research mode, ordering the stub frontier easy→hard is cheap and evidence-backed — should not stay optional.

### 1.11 Gauss / Trinity (Math Inc / Morph Labs)
Gauss: autoformalization agent that completed Tao–Kontorovich's **Strong PNT** blueprint (25K LOC, 1.1K theorems) in 3 weeks — via **thousands of concurrent agents, each with its own Lean runtime, up to 12h each**, on Morph's Infinibranch (terabytes of RAM); relies on "natural language scaffolding supplied by human mathematicians."
- Lessons: (Q8) blueprint-driven decomposition scales to research-size formalizations — confirms §4.1; (Q5/Q2) the enabling investment was **verification-environment scaling**, not model cleverness. Our roadmap's §4.9 has zero provision for scaling Lean verification horizontally; for the north-star workload this will be the binding constraint long before model quality is.

### 1.12 Automated Conjecture Resolution (arXiv:2604.03789, April 2026)
Informal agent (Rethlas) + formal agent (Archon) + theorem search engine (Matlas); task decomposition + iterative refinement; **resolved an open problem in commutative algebra and formally verified it in Lean 4 "with essentially no human involvement."**
- Direct existence proof for our north-star (autonomous, kernel-verified resolution of an open problem) using our exact role split — informal reasoner / formal prover / retriever. Confirms §1's unifying vision and the retrieval-equipped planner/decomposer (§4.6).

### 1.13 Discover and Prove (arXiv:2604.15839, April 2026)
"Hard Mode" (answer not embedded in the statement — discover, then prove): LLM NL-reasoning with explicit self-reflection discovers the answer, rewrites the statement into Easy Mode, hands to ATP. First to prove 36 Hard-Mode PutnamBench theorems; notes LLMs get >80% answer accuracy where provers get <10%.
- Lesson: for open problems, **answer/conjecture discovery is a distinct phase before proving** — maps to our EMPIRICAL job archetype (§4.2.2) and confirms the owner's "biggest wins are experimentation and search" (§0.6). Our empirical jobs should explicitly include "determine the likely answer/witness, then re-state" — which also feeds §4.5 re-stating powers.

### 1.14 A Minimal Agent for Automated Theorem Proving (arXiv:2602.24273, ICML 2026) — the adversarial counterpoint
Three features only — iterative refinement, library search, context management — "competitive performance… at a fraction of their cost"; iteration beats multiple single-shots on sample efficiency.
- Lesson: complexity must earn its keep. Our deterministic floor + flags-off byte-identical hot path (§0.3, §7 row 6) is exactly the right hedge; keep the easy-run path minimal. Note the minimal agent's three features include **library search** — again retrieval-by-default, against our Phase-5 deferral.

### 1.15 OpenAI's Erdős-90 disproof + Aleph/Logical Intelligence formalization (May 2026); research-agent products
OpenAI's internal model produced the core counterexample ideas disproving the 80-year-old planar unit-distance conjecture; humans verified/published; the disproof was then **formalized in Lean and open-sourced** by a third party. — The highest-profile 2026 math result is a *disproof*, validating our negation arm (§0.5) as co-equal, not a probe.
**Kosmos (FutureHouse/Edison, arXiv:2511.02824):** long-horizon AI scientist; per cycle runs ≤10 literature-search/data-analysis tasks, **updates a structured world model, then queries the world model to propose next-cycle tasks**; 1,500 papers, 42K lines of analysis code, ~79% conclusion accuracy, every claim traceable to code/citations. **Google AI Co-Scientist (Nature, May 2026):** supervisor agent + generate/reflect/rank(Elo tournament)/proximity/evolve/meta-review agents on an asynchronous task framework.
- Lessons (Q1/Q7): the research-agent frontier reflects **on a fixed cadence** (every Kosmos cycle; co-scientist's periodic meta-review), maintains **many competing hypotheses** (tournament, proximity clustering against mode collapse), and persists via an explicit world model with full provenance. Our orchestrator convenes only at scope-entry / stall / breakpoint (§7 row 1) and our graph carries one hypothesis lineage.

---

## 2. The eight cross-cutting questions, answered

1. **Failure-driven-only replanning?** Prover-pipelines: yes, failure-driven (Hilbert, DeepSeek-V2, Seed-Prover refinement) — *matches us*. Research-agent harnesses: **no — cadenced reflection** (Kosmos per-cycle world-model update; co-scientist meta-review; Nexus re-rates the sketch DB every episode). Since our north-star is the research end, §4.4's "never on the happy path" is right for the *nudger* but wrong as the *only* reflection mechanism in `RESEARCH_MODE`.
2. **Portfolios/sampling vs single-strong-prover-with-retries?** Universal portfolios: Hilbert pass@4 with two provers; Seed-Prover Pass@3×3/lemma + heavy broadcasting; BFS-V2 parallel agents + shared cache; Goedel-Architect parallel node closure; AlphaProof ~400 sims/subgoal; Gauss thousands of agents. **No frontier system proves serially with one model.** Our sync cap-3 (§0.6/§4.2) is a fine v1 safety choice but must not be the research-mode end state.
3. **Decomposition validation?** Three layers at the frontier: (a) cheap LLM feasibility/correctness judgment per subgoal at statement time (Hilbert); (b) **statement-fidelity/faithfulness auditing** (Aristotle's judge; LeanMarathon's adversarial audit stage; Nexus's expert validation); (c) proof/disproof duality inside search (Nexus). Plus the documented **sorry-offloading pathology** that prompting cannot fix. We have (c)-lite (§0.5, §0.7) and compile-validation, but neither (a) at statement time nor (b) at all.
4. **Retrieval mandatory?** Yes — Hilbert (front-loaded, ablated: +1.3pp *and* −43% tokens), the Minimal Agent (one of its only three features), Matlas in Automated Conjecture Resolution, LeanAgent's retriever. Our Phase-5 placement (line 449) is contradicted.
5. **Verifier throughput tricks:** batched Lean servers (Kimina Lean Server), stateless REPL fleets on CPU autoscaling (Aristotle), **goal caches keyed by hashed formal context** (Nexus; BFS-V2's shared subgoal cache), massive per-agent runtimes (Gauss/Infinibranch), I/O-optimized interfaces (Seed-Prover 1.5's LooKeng, −40% latency). We have LeanProbe (right primitive) and nothing about scale or caching.
6. **Informal↔formal bridging:** DSP lineage is now standard and *recursive*: informal proof → lemma-sketch with `sorry` stubs → per-lemma proving → feedback into sketch revision (Hilbert, Seed-Prover 1.5, Aristotle, DeepSeek-V2). Our orchestrator/decomposer stating stubs into files (§4.5) is the same pattern with a persistent-file twist — confirmed; the missing piece is the faithfulness judge on the formalization step.
7. **Persist/resume long campaigns:** Nexus sketch database + Elo + global goal cache; Kosmos world model with provenance; LeanMarathon blueprint-as-Lean-file system of record + transactional CI rounds; Seed-Prover lemma pool; LeanAgent dynamic DB. Our `plan.md`/`summary.json`/`blueprint.json` through the atomic write path (§4.1) is squarely confirmed — with the gap that frontier persistence stores **reusable proof artifacts** (goal caches, lemma pools), not just statuses and packets.
8. **Graph/blueprint planning at scale:** Confirmed emphatically and recently — Goedel-Architect (SOTA, 500× cheaper), LeanMarathon, Gauss/Strong-PNT (25K LOC), ProofFlow (arXiv:2510.15981), plus leanblueprint itself. Caveat: at scale, the graph is discharged **in parallel from the leaves**, and multiple *rival* sketches coexist (Nexus).

---

## 3. Verdicts

### Confirmed by the frontier (keep, with the why)
- **Four-role shape and the Hilbert mapping (§ References, §1)** — independently replicated by Automated Conjecture Resolution (Rethlas/Archon/Matlas) and Seed-Prover 1.5's NL-prover/sketch/agentic-prover trio; it is *the* 2026 shape.
- **Kernel-only authority, LLM never flips verdicts (§0 invariant, line 88)** — universal; Nexus even validates "no disallowed axioms," which our gate should also assert explicitly (our owner's "no axiom cheating").
- **`blueprint.json` dependency graph (§4.1)** — Goedel-Architect/LeanMarathon/Gauss prove it works at research scale and beats pure recursive decomposition.
- **Failure→probe→re-decompose loop with kernel-verified negation (§0.5)** — Hilbert does the loop; Nexus and the Erdős-90 result show disproof as co-equal artifact; we go further than Hilbert (kernel-verified negation vs LLM judgment) — genuinely ahead of the reference.
- **Budget breakpoints with decision packets (§4.3)** — Nexus's episode caps + structured-feedback prompts and Seed-Prover 1.5's adaptive budget allocation are the same idea; our decision-menu version is a superset. Fixes a real defect (verified: today's handler just records and rolls on).
- **Strong orchestrator model (§0.1/§4.8)** — Nexus uses Gemini 3.1 Pro as the editing subagent; Hilbert's reasoner is Pro-tier. Nobody routes consequential decisions to a small model.
- **Empirical jobs / experimentation-first dispatch (§0.6, §4.2.2)** — AlphaProof's TTRL variants, Kosmos's analysis tasks, Discover-and-Prove's answer-discovery phase all validate "experimentation and search" as the biggest dispatch win.
- **Research-pusher, never-give-up with concrete artifacts (§0.11/§4.7)** — Nexus ran all 353 Erdős formalizations unselected and banked partial artifacts; the frontier operates exactly in this stance.
- **Deterministic floor, flags-off byte-identical hot path (§0.3, §7)** — the Minimal Agent paper is the standing warning that harness complexity must pay rent; our dark-launch discipline is the right insurance.

### Contradicted by the evidence (fix in v3)
1. **Reflection cadence (§4.4 "never on the happy path"; §7 row 1 "never per cycle").** Kosmos updates and queries its world model *every cycle*; co-scientist meta-reviews periodically; Nexus re-rates after every episode. Failure-only reflection risks hours of well-formed-but-doomed grinding between breakpoints on open problems — exactly the runs N5 says must be context-rich. *(Nudger stays struggle-triggered; the orchestrator gains a cadence in research mode.)*
2. **Serial single-prover queue as the proving engine (§2 diagram; §0.6 sync cap-3; async in "Later/optional", line 466).** Every system closing hard problems runs parallel portfolios over the graph frontier (Goedel-Architect, LeanMarathon CI-gated rounds, BFS-V2, Seed-Prover 10–40 GPU-days/problem). Owner note N4 already permits this; the roadmap's phasing under-delivers it.
3. **Decomposition validation = LeanProbe compile-check (+ late negation probe) (§0.7, §4.5, Phase 4).** The frontier adds statement-time LLM feasibility (Hilbert), adversarial fidelity audit gating the prove phase (LeanMarathon, Aristotle's judge), and documents that **sorry-offloading defeats prompting** (Nexus). A compile-valid, faithful-looking decomposition that hides all difficulty in one node will pass everything we currently specify.
4. **Premise retrieval deferred to Phase 5 (line 449).** Hilbert's ablation shows retrieval both lifts accuracy and *halves token cost*; the Minimal Agent ships it as 1 of 3 core features. It should ride with Phase 1–2 (the `lean_lemma_suggest` seam already exists).
5. **No proof-artifact reuse layer.** Nexus's deep-hash global goal cache, BFS-V2's shared subgoal cache, Seed-Prover's lemma pool: proved subgoals are never re-proved. Our plan-state persists statuses/packets only; retries and sibling jobs will silently duplicate kernel work — real wall-clock money on long campaigns.
6. *(Minor)* **§4.8 has no cheap-prover tier** — DeepSeek-V2 (671B decomposes, 7B proves) and Hilbert (32B prover) show subgoal grinding doesn't need the strong agent model; add `models.prover_light` for stub-grinding shape-B probes.

### Concrete upgrades for v3 (ranked)
1. **Add an AUDIT stage + anti-sorry-offloading defense to the decomposer (§4.1/§4.5).** New node status `audited` between `stated` and `proving`; an adversarial fidelity review (LeanMarathon-style, can reuse the `verification_providers` pattern at `verification_providers.py:160`) checks each stated lemma against the plan's informal intent; a structural check flags decompositions where one child inherits ~all the difficulty (e.g., child-statement ≈ parent-statement similarity, or a difficulty-mass estimate over children). Evidence: Nexus's prompting-resistant pathology; Aristotle's judge; LeanMarathon's two-stage orchestrator.
2. **Statement-time feasibility triage on every stated sub-lemma (cheap), keep the budgeted ¬P campaign for escalation (§0.7 revision).** At stating: LLM correctness/provability judgment (Hilbert) + `plausible` counterexample sweep + a *bounded* disproof attempt as a standing third outcome of any prover job (AlphaProof's proof/disproof/failure trichotomy) — the "~1 probe after ~2 failures" rule then governs only the expensive dedicated negation campaigns.
3. **Verified-artifact cache + lemma pool in plan-state (§4.1/§4.9 addition, Phase 1).** Cache kernel-accepted proofs keyed by hash of (formal statement + context) (Nexus); every dispatch job consults it before proving and deposits into it after; proved lemmas from *failed* parent attempts are banked and re-injected (Seed-Prover). This is also the substrate that makes parallel jobs cheap.
4. **Research-mode cadence + rival-sketch pool (§4.7 + §4.1 schema).** In `RESEARCH_MODE`: (a) an orchestrator reflection touchpoint every N cycles (graph-frontier context, so cheap) in addition to breakpoints — Kosmos's cycle loop; (b) allow `blueprint.json` to hold competing decomposition sketches (`kind:"sketch"`, `status:"candidate"`, a lightweight preference score) so backtracking is *selection among alternatives* rather than regeneration — Nexus's P-UCB sketch DB, co-scientist's tournament.
5. **Pull retrieval into Phase 1–2 and put frontier-parallel proving on the critical path (re-phase §5).** Premise retrieval as a prover pre-step ships with Phase 2 at the latest (Hilbert: cheaper *and* better). Promote "async dispatch + parallel closure of independent `stated` frontier nodes" from "Later/optional" to a named Phase 5b with a research-mode-only flag and a verification-scaling note (pool of LeanProbe/REPL workers; Kimina-Lean-Server-style batching) — Goedel-Architect and LeanMarathon show this is where the 100–500× cost/performance is.

**One-line bottom line:** v2's architecture is the frontier's architecture — the blueprint graph, the Hilbert loop, kernel-only truth, breakpoints, and the research-pusher stance are all confirmed by mid-2026 SOTA; what v3 must add is what the frontier learned the hard way: audit the statements (sorry-offloading is real), cache every kernel win, reflect on a cadence, retrieve from step one, and discharge the graph frontier in parallel.

Sources: [Hilbert v2 abs](https://arxiv.org/abs/2509.22819) / [html](https://arxiv.org/html/2509.22819v1) · [Seed-Prover](https://arxiv.org/abs/2507.23726), [Seed-Prover 1.5](https://arxiv.org/abs/2512.17260), [Seed blog](https://seed.bytedance.com/en/blog/seed-prover-1-5-advanced-mathematical-reasoning-through-a-novel-agentic-architecture), [GitHub](https://github.com/ByteDance-Seed/Seed-Prover), [EmergentMind summary](https://www.emergentmind.com/topics/seed-prover-1-5) · [Aristotle](https://arxiv.org/html/2510.01346v1) · [AlphaProof Nature](https://www.nature.com/articles/s41586-025-09833-y), [analysis](https://www.julian.ac/blog/2025/11/13/alphaproof-paper/), [AlphaProof Nexus](https://arxiv.org/html/2605.22763v1), [Nexus/Erdős coverage](https://cryptobriefing.com/deepmind-alphaproof-nexus-erdos-problems/) · [Goedel-Architect](https://arxiv.org/pdf/2606.06468) · [LeanMarathon](https://arxiv.org/abs/2606.05400) · [DeepSeek-Prover-V2](https://arxiv.org/abs/2504.21801) · [Goedel-Prover-V2](https://arxiv.org/pdf/2508.03613), [blog](https://blog.goedel-prover.com/) · [Kimina-Prover](https://huggingface.co/blog/AI-MO/kimina-prover) · [BFS-Prover-V2](https://arxiv.org/pdf/2509.06493), [InternLM2.5-StepProver](https://arxiv.org/abs/2410.15700) · [LeanAgent](https://arxiv.org/abs/2410.06209) · [Gauss](https://www.math.inc/gauss), [Strong PNT](https://math-inc.github.io/strongpnt/), [Morph announcement](https://x.com/morph_labs/status/1966223824078479667) · [Automated Conjecture Resolution](https://arxiv.org/abs/2604.03789) · [Discover and Prove](https://arxiv.org/abs/2604.15839) · [Minimal Agent](https://arxiv.org/abs/2602.24273) · [Erdős-90 disproof](https://www.scientificamerican.com/article/ai-just-solved-an-80-year-old-erdos-problem-and-mathematicians-are-amazed/), [Lean formalization](https://digg.com/tech/9ud8f3ro), [The Conversation](https://theconversation.com/an-ai-solution-to-an-80-year-old-problem-has-shocked-mathematicians-283686) · [Kosmos](https://arxiv.org/pdf/2511.02824), [How We Built Kosmos](https://labs.edisonscientific.com/research/how-we-built-kosmos/) · [AI Co-Scientist](https://research.google/blog/accelerating-scientific-breakthroughs-with-an-ai-co-scientist/), [paper](https://storage.googleapis.com/coscientist_paper/ai_coscientist.pdf), [Nature coverage](https://labcritics.com/blog/2026/05/21/google-deepminds-co-scientist-graduates-from-research-demo-to-nature-paper/) · [ProofFlow](https://arxiv.org/pdf/2510.15981)