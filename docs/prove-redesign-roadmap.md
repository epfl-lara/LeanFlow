# LeanFlow `/prove` Next-Gen Architecture — Roadmap (items 17–22)

Status: **planning / not yet implemented.** Grounded design produced by a 4-agent study
(current-architecture map, SOTA research, target design, synthesis). Every phase attaches to a
seam verified in the tree; code cited as `file:line`. Implementation is on hold pending greenlight.

---

## 0. Locked product-owner decisions & refinements

These were decided with the product owner and refine the phased plan below.

1. **Orchestrator = SMART / strong model.** The top-level orchestrator makes the consequential
   calls (route by difficulty, when to split, when to probe negation, how to interpret a failure),
   so it must be a strong model — not a cheap one. (This overrides the earlier "cheap router"
   suggestion for the orchestrator specifically.)
2. **LLM-manager (nudger) = small / fast, ADVISORY-ONLY.** It shapes guidance/nudges beside the
   deterministic checker; the Lean kernel stays the sole authority on correctness and the LLM can
   never flip the verdict.
3. **Routing is TASK-ADAPTIVE, not a fixed aggressiveness.** The orchestrator decides per theorem
   by difficulty; a deterministic floor keeps easy runs as cheap as today (`direct-prove`), and the
   split/plan strategy is **discovered dynamically** through failures (it may start-by-splitting for
   known-hard problems, or discover hardness mid-run and re-route).
4. **The orchestrator's core FEEDBACK LOOP (product-owner addendum).** When a prover/planner cannot
   close a goal, it means either (a) too hard, or (b) not solvable (the negation is true). The
   orchestrator responds:
   - Decide whether to spawn a **negation attempt** (prove `¬P` / `P → False`).
   - **Negation proves** and `P` is a **sub-lemma** on the path to the main theorem ⇒ the
     decomposition that produced `P` was **wrong** ⇒ **backtrack: try another route / re-decompose**.
   - **Negation proves** and `P` is the **main statement** ⇒ **NOT SOLVABLE ⇒ escalate/report**
     (stop burning cycles; the statement is false or mis-stated).
   - **Negation also hard / inconclusive** ⇒ treat as **too hard ⇒ split into many easier
     sub-lemmas** (decompose).
   The dynamic: hardness and the right split are *discovered* through these failure→probe cycles,
   not assumed up front.
5. **Dispatch: synchronous-blocking for v1** (cap 3 concurrent children), strict deployed-agent
   registry; add async only after the ledger + manager-prover pairs are proven.
6. **Negation probe budget/trigger:** ~1 probe/lemma, triggered after ~2 genuine failures OR a
   risk-flagged statement — not on every hard lemma.
7. **Specs (item 21):** fold `search.md`/`draft.md`/`review.md` into orchestrator-invoked phases;
   **delete `checkpoint.md`**; keep `golf.md`/`refactor.md` standalone (post-verification intent).
8. **Checkpoints (item 20):** retire user-facing `/checkpoint`,`/rollback`,`/resume-plan`; the
   living `plan.md`+`summary.json` become the resume authority; KEEP the internal baseline-`sorry`
   restore snapshot and git-shadow rollback as silent safety only.
9. **Sequencing:** build **Phases 0–3 first** (near-zero correctness risk, high signal) before any
   parallel fan-out.

Key correctness invariant (non-negotiable): the deterministic Lean-kernel gate
(`_manager_check_queue_item`) is never LLM-overridable.

---

All the load-bearing seams are confirmed at the exact lines the explorations cited: `decide()` exists at `queue_manager.py:484` and is called nowhere in production; the manager gate is at `native_runner.py:1890`, the LLM review-agent template at `:5661`, the loop at `:9059`; dispatch is gated at `lean_services.py:140`; `save_workflow_live_status` at `workflow_state.py:305` is the plan-state write seam; `run_model_verification_review` at `verification_providers.py:160` is the LLM-manager backbone. The roadmap is fully grounded. Here is the deliverable.

---

# LeanFlow Next-Gen `/prove` Architecture — Phased Execution Roadmap (Items 17–22)

*Synthesis of the three explorations (current-architecture map, SOTA research, target design). Every phase attaches to a seam verified in the tree at `/Users/lmilikic/Desktop/LeanFlow`. Code cited as `file:line`.*

---

## 1. The unifying vision

Restructure `/prove` around a **thin LLM orchestrator** that, by task difficulty, decides each scope-entry whether to *feed the existing queue directly*, *decompose into sub-lemma stubs*, or *run pre-queue planning* (explore/search/test/draft) — while the untouched **queue + deterministic manager-prover core keeps proving and stays the sole authority on correctness** (the Lean kernel via `_manager_check_queue_item`, `native_runner.py:1210`, is never LLM-overridable). Alongside the deterministic checker, an **LLM-manager** emits *optimistic-but-strict strategic nudges* (`nudge | dispatch | escalate`) that shape control flow but can never mark an unverified proof as done. All agents read and write **one living plan-state artifact** (`plan.md` + `summary.json`) that replaces the agent-hostile prose checkpoints, and **tracked dispatch** launches manager-prover pairs or free inspect/test/negation sub-agents through a strict registry. The redesign is deliberately *wiring + gating*, not a rewrite: every new component is an instance of the already-working `_run_document_formalization_review_agent` phase pattern (`native_runner.py:5661`), delegating actual proving to the load-bearing core.

---

## 2. Target architecture

```
 native main() → _drive_autonomous_followups  (native_runner.py:9059) — eternal loop, SPINE UNCHANGED
        │
        │  [NEW: at scope-entry + on stall, before first prover turn]
        ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ (22) ORCHESTRATOR  _orchestrator_route(RouteContext)                        │
 │   det. pre-classifier (retry_count_for, blocker_kind, stall streak) ──┐     │
 │   optional LLM override (may UPGRADE difficulty, never DOWNGRADE) ─────┘     │
 │   emits ROUTE ∈ {direct-prove | decompose | plan | explore | escalate}      │
 └───┬───────────────┬──────────────────┬───────────────────────┬────────────┘
     │direct         │decompose          │plan / explore         │escalate
     ▼               ▼                    ▼                       │(neg. proved)
 ┌────────┐   ┌──────────────┐   ┌───────────────────────────┐   │
 │ QUEUE  │   │ lean_decompose│  │ (18) PLANNER fan-out:      │   │
 │ PROVE  │◄──│ _helpers →    │  │  web·mathlib·empirical·    │   │
 │ (inner │   │ seed queue via│  │  draft·(19)NEGATION probe  │   │
 │  loop, │   │ assign()      │  └────────────┬──────────────┘   │
 │ EXISTING)  └──────┬───────┘                │ synth→plan       │
 └───┬────┘          │                        ▼                  │
     │               └───────────►┌───────────────────────────┐◄─┘
     │  (queue seeded from plan)  │ (20) PLAN-STATE            │
     ▼                            │  plan.md + summary.json    │
 ┌──────────────────────────────┐ │  (living; read on resume, │
 │ PROVER turn (_run_managed_    │ │   by every sub-agent)     │
 │  conversation)  claims solved │ └───────────────────────────┘
 └───────────────┬──────────────┘        ▲ append (dispatch ledger,
                 ▼                         │  works/does-not-work)
 ┌──────────────────────────────────────────────────────────────┐
 │ (17) MANAGER GATE  _review_agent_final_report (:1890)          │
 │  ┌───────────────────────┐   ┌──────────────────────────────┐ │
 │  │ DETERMINISTIC CHECKER  │──►│ LLM-MANAGER (advisory only)    │ │
 │  │ _manager_check_queue_  │ok?│ run_model_verification_review  │ │
 │  │ item = KERNEL TRUTH    │   │ → nudge | dispatch | escalate  │ │
 │  └───────────────────────┘   └──────────────┬───────────────┘ │
 │   ok=True ⇒ advance queue                    │ shapes next prompt│
 │   ok=False ⇒ det. retry ladder + baseline restore│ / triggers ▼   │
 └──────────────────────────────────────────────┬─────────────────┘
                                                 ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ (19) DISPATCH  dispatch_worker (re-enabled, lean_worker_       │
 │  dispatch.py:47) → manager-prover PAIR | free inspect/test |   │
 │  negation-probe.  REGISTRY (dispatch_ledger in summary.json).  │
 │  Result consumed one-way: verified disk edits + plan delta.    │
 └──────────────────────────────────────────────────────────────┘
```

**How this EVOLVES (not replaces) the queue + manager-prover.** The three load-bearing pieces stay exactly where they are and keep their current behavior on the easy path: the `while` loop (`native_runner.py:9059`), the single-assignment queue (`TheoremQueueManager.assign`, `queue_manager.py:181`), and the deterministic gate (`_review_agent_final_report`, `native_runner.py:1890` → `_manager_check_queue_item`, `:1210`). Everything new is *bolted above and beside* them. The orchestrator sits **above** the loop and only chooses *when* to enter the existing inner loop vs. run a pre-phase first — a `direct-prove` route falls straight into today's cycle body (`native_runner.py:9192+`) with zero hot-path change. The LLM-manager sits **beside** the deterministic checker inside the gate, reading its verdict but forbidden from altering `ok`. Plan-state is written through the **existing** atomic `save_workflow_live_status` path (`workflow_state.py:305`) next to `live_status.json` — no new persistence infra. Dispatch reuses the **already-built** `dispatch_worker` (`lean_worker_dispatch.py:47`, gated off at `lean_services.py:140`) and the `summarize_workflow_agents` registry (`workflow_state.py:651`). The one structural gap that must be *added* to the queue — not reworked — is an **agent-ownership / lifecycle dimension** (a claim keyed on `TheoremKey`, `queue_models.py:52`) so dispatched pairs can own sub-scopes; this is additive and lands only when Phase 4 needs it. The correctness floor never moves.

---

## 3. Phased plan (ordered by value / risk)

Every early phase is **additive, dark-launchable behind a flag, and independently shippable**. The ordering front-loads the substrate and the zero-correctness-risk wins, and defers the high-surface fan-out and cleanup.

### Phase 0 — Finish the half-done queue migration (enabling, near-zero risk)
- **Deliverables.** (a) Route the production verdict path through `TheoremQueueManager.decide()` (`queue_manager.py:484`), which is confirmed dead in production (`grep` for `.decide(` finds no non-test caller). Consolidate the three drifting verdict copies — `_review_agent_final_report` (`:1890`), `_manager_feedback_kind` (`:1834`), `_same_queue_assignment_still_blocked` (`:5815`) — so `decide()` composed from `classify_check` (`queue_models.py:337`) becomes the *single* decision policy. (b) Make `TheoremQueueManager` the live authority rather than a per-call reconstruction (`_queue_manager_from_state`, `:869` / `_flush_queue_manager`, `:886`). Behavior-preserving refactor: golden-test the existing verdicts before/after.
- **Advances.** Enabling for 17 (gives the LLM-manager one clean composition point) and 19/20 (a live authority that can carry ownership + lifecycle).
- **Seams.** `queue_manager.py:484`, `native_runner.py:1890/1834/5815/869/886`.
- **Effort/Risk.** ~1 week / **Low-Medium** — pure refactor, no new capability, fully test-covered by the existing `decide()`/`classify` tests. Risk is verdict drift; mitigate with a shadow-compare assertion (old open-coded verdict == `decide()` output) for one release before deleting the old path.
- **Unlocks.** A single, typed seam for the LLM-manager (Phase 2) and an authority object that can hold agent claims (Phase 4).

### Phase 1 — Plan-state artifacts + retire checkpoint UX (item 20; substrate for 18/19/22)
- **Deliverables.** Write `plan.md` (human/agent-readable: `Goal / Route history / Decomposition+status / Grounding (cited) / Works / Does-not-work / Open questions / Next action`) and `summary.json` (machine control-flow state: `scope`, `route.history`, `queue_seed`, `sublemmas[]`, `dispatch_ledger[]`, `works[]`, `does_not_work[]`, `manager_nudges[]`, `blockers[]`) alongside `live_status.json`, through the existing atomic `save_workflow_live_status` / `write_json_file` path (`workflow_state.py:305`). Initially **pure additive telemetry** — the runner writes them, nothing reads them for control yet. Replace `_resume_plan_from_checkpoint` (`native_runner.py:252`) with "load `summary.json`, reconcile against live Lean state." Remove user-facing `/checkpoint`, `/rollback`, `/resume-plan` (`native_runner.py:3788`) **but keep** the internal baseline-`sorry` restore snapshot that retry-exhaustion depends on (`native_runner.py:1982`) and the filesystem-hash rollback substrate (`native_checkpoints.py:205`).
- **Advances.** 20 fully; substrate for 18, 19, 22.
- **Seams.** `workflow_state.py:98-99,305`; `native_runner.py:252,3788,1982`; `native_checkpoints.py:205`.
- **Effort/Risk.** ~1–1.5 weeks / **Low** — no control-flow change on write; the only behavioral change is resume, which is guarded by the surviving rollback net.
- **Unlocks.** The shared blackboard every later phase reads/writes; the reconcile-every-cycle loop that kills plan drift.

### Phase 2 — LLM-manager in dark-launch (item 17)
- **Deliverables.** Insert `run_model_verification_review(task="manager_nudge")` (`verification_providers.py:160`) inside `_review_agent_final_report` immediately after the deterministic block computes `ok/feedback_kind/retry_count` (~`native_runner.py:2007`), before the canned feedback append (~`:2068`). New proving-oriented system prompt analogous to `_verification_review_system_prompt` (`manager_verification.py:116`), emitting `{action: nudge|dispatch|escalate, message, dispatch_role?}` from the vocabulary already declared in `prove.md:10` (`continue/replan/redraft/falsify/stop`). **Dark-launch:** log its would-be action next to the deterministic outcome in `summary.json.manager_nudges`; do **not** let it steer. After nudge quality is validated on real runs, flip `LEANFLOW_MANAGER_LLM_ENABLED=true` to let it replace the feedback message (never the verdict).
- **Advances.** 17.
- **Seams.** `native_runner.py:2007→2068`; `verification_providers.py:160`; `manager_verification.py:116`.
- **Effort/Risk.** ~1 week to dark-launch, +few days to enable / **Low** (dark) → **Medium** (enabled). Zero correctness risk while dark; reuses the entire `verification_providers` + `auxiliary_client` telemetry stack. Cost risk mitigated by skipping the LLM call on the happy path (`ok=True`).
- **Unlocks.** Validated nudge vocabulary that becomes the orchestrator's re-route trigger (Phase 4) and dispatch trigger (Phase 3).

### Phase 3 — Dispatch re-enable, registry-first, starting with the negation probe (item 19)
- **Deliverables.** Turn `LEAN_WORKER_DISPATCH_ENABLED` (`lean_services.py:140`) into a config flag. Ship the **negation/feasibility probe first** as a new dispatch role: given hard lemma `L : P`, spawn a `delegate_task` child that attempts `¬P` / `P → False` in a **LeanProbe scratch** (`lean_check`, no file mutation), bounded by `LEANFLOW_NEGATION_PROBE_BUDGET=1`/lemma. Wire outcomes to the declared `falsify` action (`prove.md:10`): negation-proved ⇒ `escalate` (statement wrong, stop burning cycles); negation-hard ⇒ "do not give up" signal; inconclusive ⇒ soft note. Build the **dispatch ledger** (`{agent_id, role, sub_scope, status∈{deployed,running,done,stuck}, verdict, result_ref}`) as a `summary.json` section, derived from `summarize_workflow_agents` (`workflow_state.py:651`) + `DEAD_AGENT_STATUSES` (`native_runner.py:123`). Result consumption is strictly one-way (verified disk edits + plan delta). Reuse the existing file-lock primitive (`lean_worker_dispatch.py:57`) and `append_workflow_outcome` (`:104`).
- **Advances.** 19 (negation probe + tracked registry); begins the dispatch mechanism.
- **Seams.** `lean_services.py:140`; `lean_worker_dispatch.py:47,57,104`; `workflow_state.py:651`; `delegate_tool.py:391`.
- **Effort/Risk.** ~2 weeks / **Medium** — dispatch is built but gated; negation probe is read-only and bounded, the safest possible first dispatch. Triggered by the Phase-2 LLM-manager `dispatch` action on repeated hard failure (not on every lemma → cost-bounded).
- **Unlocks.** The tracked-agent registry and the (manager,prover)-pair machinery that Phase 5 needs; realizes the highest-ROI item-19 behavior (catching mis-stated theorems) before the risky parallel fan-out.

### Phase 4 — Orchestrator as a deterministic pre-classifier (item 22, LLM off) + direct sub-lemma route
- **Deliverables.** Add `_orchestrator_route(RouteContext)` at scope-entry after `_build_live_proof_state_compat` (`native_runner.py:9094`) and on stall in `_autonomous_stop_reason` (`:8921`) — turning today's "give up on stall" into "re-route/replan." Ship **only the deterministic route table** (no LLM call): `direct-prove` (0 attempts, compiler blocker) / `decompose-then-prove` (≥ `HARD_LIMIT/2` fails or decompose blocker) / `plan-then-prove` (unfamiliar/large) / `escalate` (prior negation proved). Implement the `decompose` route immediately via `lean_decompose_helpers` (`tools/implementations/lean_tool.py:658`) seeding the queue with helper decls through `assign` (`queue_manager.py:181`). Add the queue **ownership/lifecycle** dimension here (claim keyed on `TheoremKey`, richer status vocabulary) needed for pairs. Budget: `LEANFLOW_ORCHESTRATOR_MAX_ROUTES=3`/scope. Rule: LLM (added in Phase 6) may only *upgrade* difficulty, never *downgrade* below the classifier.
- **Advances.** 22 (routing + direct sub-lemma construction), extends 19 (ownership).
- **Seams.** `native_runner.py:9094,8921`; `lean_tool.py:658`; `queue_manager.py:181`; `queue_models.py:52`.
- **Effort/Risk.** ~2 weeks / **Medium** — real routing value with no new LLM dependency; `direct-prove` is the current behavior so easy runs are unchanged. Risk: over-eager decompose; mitigate with the conservative pre-classifier defaults and `MAX_ROUTES` cap.
- **Unlocks.** The pre-phase entry points that the Planner (Phase 5) and LLM override (Phase 6) plug into.

### Phase 5 — Planner fan-out + manager-prover-pair dispatch (item 18; completes 19)
- **Deliverables.** `_run_planner_phase` modeled on `_run_document_formalization_review_agent` (`native_runner.py:5661`), invoked on `plan-then-prove`/`explore-first`. Fan out (via `delegate_task` batch, `MAX_CONCURRENT_CHILDREN=3`) to scoped sub-agents: **web** (`WebSearch/WebFetch`), **mathlib** (`lean_search`/`lean_lemma_suggest`/`lean_loogle`), **empirical** (`lean_multi_attempt`/`lean_check`), **draft** (skeletons), plus the Phase-3 **negation** probe. A synthesizer turn merges findings into `plan.md` and seeds the queue. **Premise retrieval becomes a mandatory pre-step** before spawning any prover (inject `lean_lemma_suggest`/`lean_hammer_premise` results into prover context). Ship the **(manager,prover) pair** dispatch shape: a child runs the inner loop against a sub-scope with its own deterministic gate; parent consumes only verified verdict + plan delta.
- **Advances.** 18 fully; completes 19 (pairs); feeds 22.
- **Seams.** `native_runner.py:5661,5718,9133`; `delegate_tool.py:391,439`; `lean_worker_dispatch.py:47`.
- **Effort/Risk.** ~3 weeks / **Medium-High** — highest surface area (parallel sub-agents, state reconciliation, sprawl). Depends on Phases 1 (plan-state) + 3 (registry) being solid. Keep dispatch **synchronous-blocking** for v1.
- **Unlocks.** Full grounding for genuinely hard theorems; the curriculum/easy→hard ordering can layer on here.

### Phase 6 — LLM orchestrator override + spec-phase reintegration (items 22, 21)
- **Deliverables.** Enable the LLM layer over the Phase-4 pre-classifier (`LEANFLOW_ORCHESTRATOR_ENABLED`, `call_llm(task="orchestration")`, cheap/fast model per the low-orchestrator/high-worker finding) — constrained to *upgrade only*. Reintegrate specs (`lean_workflow_specs.py`): add `phase: true` frontmatter and extend `VALID_SPEC_KINDS` (`lean_workflow_specs.py:20`) so `search.md`→mathlib/web grounding fragment, `draft.md`→draft fragment, `review.md`→LLM-manager fragment; **delete `checkpoint.md`** (superseded by Phase 1); keep `golf.md`/`refactor.md` standalone (post-verification is a distinct user intent, `prove.md:31`). Move `prove.md`'s taxonomy/ladder into the inner-prover contract and add an "Orchestration" section. Compose fragments via the existing `_worker_prompt` builder (`lean_worker_dispatch.py:19`).
- **Advances.** 22 (LLM routing), 21 (spec reintegration).
- **Seams.** `lean_workflow_specs.py:20`; `lean_worker_dispatch.py:19`; `leanflow_specs/workflows/{prove,search,draft,review,checkpoint}.md`.
- **Effort/Risk.** ~2 weeks / **Medium** — mechanical cleanup best done once phases stabilize their prompts; the upgrade-only constraint bounds cost/risk.
- **Unlocks.** Full item-22 vision; a single maintainable spec surface.

**Later / optional.** Curriculum ordering of the queue (easy→hard, LeanAgent), proof-progress de-prioritization of stuck items (BFS-Prover signal), and truly-async dispatch with stuck-agent reclaim — all layer on top once Phases 1–6 are proven.

---

## 4. Key decisions for the product owner

1. **Can the LLM-manager ever influence the correctness verdict?** *Either*: keep the Lean kernel (`_manager_check_queue_item`, `native_runner.py:1210`) the sole authority and let the LLM shape only guidance/routing — *or* let the LLM adjust acceptance in edge cases. **Recommend: NO, hard invariant.** This is the single safety line that prevents "the LLM talked us into a bad proof"; it matches the existing PASS/BLOCK-but-not-kernel-evidence disclaimer (`manager_verification.py:118`). Tradeoff: the LLM can't rescue a genuinely-correct proof the deterministic checker rejects — acceptable, because that path stays visible via `escalate`.

2. **Dispatch concurrency: synchronous-blocking or truly async?** **Recommend: synchronous-blocking for v1** (as `delegate_task` is today), cap `MAX_CONCURRENT_CHILDREN=3`. Tradeoff: less parallel throughput, but async multiplies state-reconciliation and file-lock-race surface — the top sprawl risk. Add async only after the ledger + pair dispatch are proven.

3. **How aggressive is the orchestrator about planning?** **Recommend: conservative — deterministic pre-classifier defaults to `direct-prove`; the LLM may only *upgrade* difficulty, never downgrade.** Tradeoff: occasionally under-plans a sneaky-hard theorem, but keeps easy runs exactly as cheap as today and prevents expensive fan-out on trivial items.

4. **Negation-probe budget and trigger.** **Recommend: 1 probe/lemma, triggered by the LLM-manager `dispatch` action after ~2 genuine failures OR by the orchestrator on a risk-flagged statement — not on every hard lemma.** Tradeoff: misses a mis-statement on the first attempt, but probing everything roughly doubles cost; probing after real failures catches the expensive case (grinding a false auto-decomposed sub-lemma).

5. **Fate of the standalone specs (`search/draft/review/golf/refactor/checkpoint`).** **Recommend: fold `search/draft/review` into orchestrator-invoked phases, delete `checkpoint.md`, keep `golf/refactor` standalone.** Tradeoff: fewer top-level workflows for power users, but eliminates the spec↔router drift and matches the "not a pre-proving phase" boundary already in `prove.md:31`.

6. **Checkpoint retirement scope.** **Recommend: remove all user-facing checkpoint UX (`/checkpoint`,`/rollback`,`/resume-plan`, `native_runner.py:3788`) but keep the internal baseline-`sorry` restore snapshot** (`native_runner.py:1982`) and filesystem-hash rollback substrate (`native_checkpoints.py:205`). Tradeoff: users lose an explicit save-point concept they may rely on; `summary.json` becomes the resume authority, which is strictly better for agents (item 20's whole premise).

7. **Where do sub-agents run Lean?** **Recommend: LeanProbe `lean_check` for all probe/experiment/inspect/negation roles (sub-second, never mutates files); Lake-backed `_manager_check_queue_item` for the authoritative acceptance gate only.** Tradeoff: LeanProbe's environment scope isn't the whole project, so acceptance must stay on Lake — which is exactly the split recommended.

8. **Orchestrator model tier.** **Recommend: cheap/fast model for orchestration + LLM-manager routing; strong model for provers and any correctness-adjacent reasoning** (per the measured low-orchestrator/high-worker +31% result and the existing per-task routing in `auxiliary_client`/`model_capabilities`). Tradeoff: a weaker router occasionally mis-routes, but the deterministic pre-classifier is the floor and the strong model still does the proving.

---

## 5. Risks & mitigations

| Risk | Where it bites | Mitigation (grounded in seams) |
|---|---|---|
| **Token blowup** (multi-agent ≈ 15× tokens; orchestrator LLM on every cycle) | Orchestrator + planner fan-out | Thin orchestrator on a cheap model; run it only at **scope-entry + on stall**, never per-cycle; skip the LLM call entirely when the deterministic pre-classifier says `direct-prove`; skip the LLM-manager on `ok=True`. Fresh context per dispatched prover (`delegate_task`) bounds per-agent tokens. Difficulty-gate all planning behind flags. |
| **Sub-agent sprawl** ("bag of agents," children clobbering each other) | Phase 5 fan-out | Reuse the existing file-lock (`lean_worker_dispatch.py:57`) + "no two children edit the same file" swarm rule; hard cap `MAX_CONCURRENT_CHILDREN=3`; **deployed-agent registry** (`dispatch_ledger` over `summarize_workflow_agents`, `workflow_state.py:651`) so the orchestrator never double-dispatches a sub-scope and can reclaim `stuck` agents. Synchronous-blocking for v1. |
| **Plan drift / stale state** (agents act on outdated plan) | Plan-state (Phase 1) | **Reconcile `summary.json`/`plan.md` node-status against live `lean_inspect`/`sorry`-count every cycle** — Lean state, never the plan, is the source of truth for "is it proved." The loop already refreshes diagnostics between moves. |
| **Regressing the working queue** | Phases 0 & 4 touching the core | Phase 0 is a behavior-preserving refactor guarded by a shadow-compare assertion (old verdict == `decide()`) for one release; every new phase is **flag-gated and dark-launchable**; `direct-prove` route is literally today's code path, so the hot path is byte-for-byte unchanged when flags are off; the deterministic correctness gate is never modified. |
| **Wasted search on false sub-lemmas** (decompose emits an unprovable stub) | Phase 4 decompose | The Phase-3 negation probe runs on auto-decomposed sub-lemmas; a proved negation routes `escalate`/`redraft` before cycles are burned. |
| **LLM-manager over-steers** | Phase 2 enable | Dark-launch first (log-only), enable only after nudge quality is validated; the nudge shapes the *message*, never `ok`; kept out of the happy path. |
| **Checkpoint-as-memory confusion** | Phase 1 retirement | Keep git-shadow rollback as *silent safety only*; the agent's memory is the separate `plan.md` blackboard — do not conflate the two. |

---

**Start Phase 0/1 immediately.** Phase 0 (route verdicts through the dead `decide()` seam at `queue_manager.py:484`) and Phase 1 (write `plan.md`+`summary.json` through `save_workflow_live_status`, `workflow_state.py:305`; retire checkpoint UX at `native_runner.py:3788`) are both low-risk, additive, and unblock everything above. Together with Phase 2's dark-launched LLM-manager and Phase 3's negation probe, they deliver three high-signal, near-zero-correctness-risk wins that de-risk the entire redesign before any parallel fan-out is attempted.

Key files: `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/native/native_runner.py` (loop `:9059`, manager gate `:1890`, checker `:1210`, review-agent template `:5661`, live-state `:6108`, resume `:252`, checkpoint UX `:3788`), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/workflows/queue_manager.py` (dead `decide()` `:484`, `assign` `:181`, `peek_assignment` `:219`), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/workflows/queue_models.py` (`TheoremKey` `:52`, `classify_check` `:337`), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/lean/lean_worker_dispatch.py` (`:47`), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/lean/lean_services.py` (gate `:140`), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/workflows/verification_providers.py` (`:160`), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/workflows/manager_verification.py` (`:116`), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/workflows/workflow_state.py` (`:98-99,:305,:651`), `/Users/lmilikic/Desktop/LeanFlow/leanflow_cli/lean/lean_workflow_specs.py` (`:20`), `/Users/lmilikic/Desktop/LeanFlow/tools/implementations/delegate_tool.py` (`:391`), `/Users/lmilikic/Desktop/LeanFlow/tools/implementations/lean_tool.py` (`:658`), `/Users/lmilikic/Desktop/LeanFlow/leanflow_specs/workflows/prove.md` (`:10`).