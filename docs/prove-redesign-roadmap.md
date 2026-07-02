# LeanFlow `/prove` Next-Gen Architecture — Roadmap v2 (items 17–22)

Status: **planning / not yet implemented.** v1 was a grounded design from a 4-agent study
(current-architecture map, SOTA research, target design, synthesis). **v2 folds in the product
owner's answers and annotations** (2026-07-01): expanded orchestrator mandate, the dependency
graph, budget breakpoints, the generalized dispatch model, deep search, spec improvement, and the
research-pusher stance. Every phase attaches to a seam verified in the tree; code cited as
`file:line`. Implementation is on hold pending greenlight.

References: **Hilbert** (arXiv:2509.22819) — informal reasoner LLM + specialized prover LLM +
formal verifier + semantic theorem retriever, with *recursive decomposition on prover failure* and
verifier-feedback refinement (99.2% miniF2F, 70.0% PutnamBench). Our target maps 1:1 onto its four
roles (orchestrator ≈ informal reasoner + recursion controller; queue-prover ≈ specialized prover;
kernel gate ≈ verifier; `lean_search`/`lean_lemma_suggest` ≈ retriever) — validation that this
shape is the current SOTA shape. **leanblueprint** (PatrickMassot/leanblueprint) — LaTeX blueprint
with a dependency graph (`\uses` edges; node statuses `stated / proved / can_state / can_prove /
not_ready / fully_proved`) — the template for our machine-readable graph, §4.1.

---

## 0. Locked product-owner decisions (v2)

1. **Orchestrator = SMART / strong model, with an expanded mandate.** It makes the consequential
   calls: route by difficulty; decide when to split; **propose and STATE new lemmas** (private or
   public, in the same / a different / a brand-new file, by intuition or from the plan); **create
   and organize files** — laying out `sorry`-stub theorems so that one or more queue-prove jobs can
   be launched over them (for a hard probe, even a file with a single important lemma is fine);
   decide negation probes; interpret failures. Never a cheap model. Every role's model is
   configurable in `config.yaml` (§4.8).
2. **LLM-manager (nudger) = small / fast, ADVISORY-ONLY, STRUGGLE-TRIGGERED.** It is invoked *by
   the deterministic manager* only when struggle signals fire (§4.4) — **not after every prover API
   step** and not on the happy path. It crafts the adjusted, optimistic-but-strict message for the
   prover (don't overthink, stop searching and try solving, try dispatching, don't give up, start
   exploration). The Lean kernel stays the sole authority on correctness; the LLM can never flip
   the verdict.
3. **Routing is TASK-ADAPTIVE.** Deterministic floor keeps easy runs as cheap as today
   (`direct-prove`); hardness and the right split are **discovered dynamically** through
   failure→probe cycles (may also start-by-splitting for known-hard problems).
4. **Budget exhaustion is a real BREAKPOINT, not a silent restart.** Today, when a theorem's API
   budget is exhausted the queue records a failed attempt and rolls on — there is no true
   breaking point. v2: exhausting the per-theorem budget (or the queue burning budget without
   progress) **interrupts the queue**, persists a decision packet to plan-state, and hands the
   decision to the orchestrator (split / plan / negate / park / re-state / abort). §4.3.
5. **The failure→feasibility FEEDBACK LOOP** (Hilbert-style recursive orchestration). A goal the
   prover/planner cannot close means (a) too hard, or (b) not solvable (negation true):
   - Orchestrator decides whether to spawn a **negation attempt** (`¬P` / `P → False`); a cheap
     `plausible` (counterexample search) pre-probe may run first — a *hint only*, it doesn't
     always work and is never authority.
   - **Negation proves, `P` is a sub-lemma** ⇒ the decomposition was wrong ⇒ **backtrack:
     re-route / re-decompose** (mark the graph node `false`, invalidate the subtree).
   - **Negation proves, `P` is the main statement** ⇒ **not solvable ⇒ escalate/report**.
   - **Negation also hard / inconclusive** ⇒ **too hard ⇒ split** into easier sub-lemmas.
6. **Dispatch = general sub-JOB launching, at MULTIPLE levels, lifecycle designed first.** Not
   just queue-level, and not primarily for parallel proving — the biggest wins are
   **experimentation and search**. Three job archetypes (§4.2): prover jobs, empirical jobs,
   deep-search jobs. Orchestrator, planner, and decomposer can all dispatch (the LLM-manager can
   *suggest* a dispatch). The assign→track→report→kill/patience lifecycle is complex and is
   **designed and reviewed before implementation** (Phase 3a). v1 synchronous-blocking, cap 3.
7. **Negation probe budget/trigger:** ~1 probe/lemma, after ~2 genuine failures OR a risk-flagged
   statement — not on every hard lemma.
8. **Specs (item 21): fold AND IMPROVE.** Fold `search.md`/`draft.md`/`review.md` into
   orchestrator-invoked phases; **delete `checkpoint.md`**; keep `golf.md`/`refactor.md` standalone
   — but **all specs are rewritten to the quality bar already set** in this effort, and
   `golf`/`refactor` get a real functional overhaul (they were inherited from OpenGauss and are
   not properly usable today).
9. **Documentation-driven proving (item 20).** Planning and progress-tracking through written
   artifacts becomes a first-class feature: living `plan.md` + `summary.json` + the **dependency /
   exploration graph** (§4.1) telling us what is done, what is planned, and which paths are dead
   or proven false. **Every deployed agent is made aware of these artifacts.** The orchestrator
   may still choose to work directly on a file without them for simple tasks — but for the most
   important tasks this is a crucial feature. Checkpoint UX is retired; logging stays.
10. **Deep search is a multi-level ability**: searching AND downloading/cloning papers, articles,
    docs, repos, libraries — available to the orchestrator, planner, decomposer, and deep-search
    jobs (§4.6).
11. **Research-pusher stance.** This harness targets research problems, potentially fully open
    ones. Orchestrator and managers must **prevent difficulty-complaining and push beyond the
    limits of current knowledge**: difficulty is a routing signal (split / probe / search /
    experiment), never a terminal state (§4.7).
12. **LeanProbe is the guy** — used as widely as possible (probes, experiments, negation,
    decomposition validation, inspect jobs); Lake-backed `_manager_check_queue_item`
    (`native_runner.py:1210`) remains the sole authoritative acceptance gate.
13. **Ground in existing, proven infrastructure — no from-scratch agent framework.** Everything
    composes existing machinery: `delegate_task`, the `/prove` workflow itself, the agent inbox
    control loop, the workflow-state registry, `verification_providers`, LeanProbe (§4.9).
14. **Sequencing:** Phases 0–3 first (near-zero correctness risk, high signal) before any
    parallel fan-out.

Key correctness invariant (non-negotiable): the deterministic Lean-kernel gate
(`_manager_check_queue_item`) is never LLM-overridable.

---

## 1. The unifying vision (v2)

Restructure `/prove` around a **strong-model LLM orchestrator** that, per scope-entry and at every
breakpoint, decides whether to *feed the existing queue directly*, *decompose — stating new
sub-lemmas into organized files*, or *run pre-queue planning* (explore / search / test / draft) —
while the untouched **queue + deterministic manager-prover core keeps proving and stays the sole
authority on correctness**. The orchestrator owns **file organization** and the **dependency
graph**: it lays out `sorry`-stub theorems across files and launches queue-prove jobs over them,
reading failures back through the Hilbert-style feedback loop (fail → probe negation → re-decompose
| escalate | split). Beside the deterministic checker, a **small struggle-triggered LLM-manager**
crafts adjusted nudges that keep the prover moving without ever touching verdicts. All agents read
and write **living documentation** — `plan.md`, `summary.json`, and `blueprint.json` (the graph) —
which replaces checkpoint prose as the resume/coordination authority. **Tracked dispatch** launches
prover, empirical, and deep-search jobs from any decision-making level through one strictly-managed
lifecycle. The redesign remains *wiring + gating, not a rewrite*: every component composes existing
machinery, and the hot path is byte-identical when flags are off.

---

## 2. Target architecture (v2)

```
 native main() → _drive_autonomous_followups (native_runner.py:9059) — eternal loop, SPINE UNCHANGED
        │
        │ [scope-entry · stall · BUDGET BREAKPOINT (§4.3)]
        ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ (22) ORCHESTRATOR — strong model                                              │
 │   det. pre-classifier floor → optional LLM (upgrade-only)                     │
 │   ROUTE ∈ {direct-prove | decompose | plan | explore | escalate | park}       │
 │   POWERS: state lemmas (new/same/other files) · organize files/stubs ·        │
 │           launch queue-prove jobs over stub files · order probes ·            │
 │           interpret failures via the graph · push research (never give up)    │
 └──┬─────────────┬───────────────────┬──────────────────────────┬─────────────┘
    │direct       │decompose           │plan / explore            │escalate/park
    ▼             ▼                    ▼                          │
 ┌────────┐  ┌────────────────┐  ┌────────────────────────────┐  │
 │ QUEUE  │  │ (fka item 19)  │  │ (18) PLANNER — strong model │  │
 │ PROVE  │◄─┤ DECOMPOSER     │  │ fan-out: web · mathlib ·    │  │
 │ (inner │  │ graph-aware;   │  │ empirical · draft ·         │  │
 │ loop,  │  │ STATES lemmas  │  │ negation probe              │  │
 │EXISTING)│  │ into files;    │  │ synth → plan.md + graph    │  │
 └───┬────┘  │ validates via  │  └──────────────┬─────────────┘  │
     │       │ LeanProbe      │                 │                 │
     │       └──────┬─────────┘                 │                 │
     │              │        ┌──────────────────▼──────────────┐  │
     │              └───────►│ (20) PLAN-STATE  (the blackboard)│◄─┘
     │   queue seeded        │  plan.md · summary.json ·        │
     │   from stub files     │  blueprint.json DEPENDENCY GRAPH │
     ▼                       │  (§4.1) — read by EVERY agent    │
 ┌──────────────────────┐    └──────────────▲──────────────────┘
 │ PROVER turn           │                  │ ledger, works/fails,
 │ (_run_managed_conv.)  │                  │ graph status updates
 └──────────┬───────────┘                   │
            ▼                               │
 ┌───────────────────────────────────────────────────────────────┐
 │ (17) MANAGER GATE  _review_agent_final_report (:1890)           │
 │  DETERMINISTIC CHECKER (kernel truth, :1210) — ok is FINAL      │
 │        │ struggle signals (§4.4) fire?                          │
 │        ▼                                                        │
 │  LLM-MANAGER (small/fast, advisory): crafts the nudge message,  │
 │  may SUGGEST dispatch/escalate — never touches ok               │
 └──────────────────────────────┬────────────────────────────────┘
                                │
   ┌────────────────────────────▼────────────────────────────────────┐
   │ (19) DISPATCH SERVICE — one shared lifecycle for ALL levels      │
   │  callers: ORCHESTRATOR · PLANNER · DECOMPOSER (LLM-mgr suggests) │
   │  job archetypes (§4.2):                                          │
   │   • PROVER job     (A: mini queue-prove over a stub file —       │
   │                        full existing machinery; B: queue-less    │
   │                        single-target probe w/ LeanProbe)         │
   │   • EMPIRICAL job  (python / Lean experiments: confirm or        │
   │                        refute an idea before investing)          │
   │   • DEEP-SEARCH job (literature/lean4: search, fetch, download,  │
   │                        clone repos → synthesized findings)       │
   │   • NEGATION probe (plausible pre-probe → ¬P attempt)            │
   │  LEDGER: proposed→deployed→running→{done|failed|stuck|killed}    │
   │  sync v1 (cap 3) · trackable · killable · patient · never lost   │
   └──────────────────────────────────────────────────────────────────┘
```

**How this EVOLVES (not replaces) the queue + manager-prover** — unchanged from v1: the `while`
loop (`native_runner.py:9059`), the single-assignment queue (`TheoremQueueManager.assign`,
`queue_manager.py:181`), and the deterministic gate (`:1890` → `:1210`) stay exactly where they
are; `direct-prove` falls straight into today's cycle body with zero hot-path change; plan-state
writes go through the existing atomic `save_workflow_live_status` path (`workflow_state.py:305`);
dispatch reuses `dispatch_worker` (`lean_worker_dispatch.py:47`, gated at `lean_services.py:140`),
`delegate_task` (`delegate_tool.py:391`), and the `summarize_workflow_agents` registry
(`workflow_state.py:651`). The additive structural change remains the queue's
**agent-ownership / lifecycle dimension** (claim keyed on `TheoremKey`, `queue_models.py:52`).

---

## 3. What changed v1 → v2 (the replan, explicit)

| # | Area | v1 | v2 (owner's annotation) |
|---|---|---|---|
| 1 | Orchestrator mandate | route/split/probe/interpret | + **state new lemmas** (private/public, any/new file) + **create & organize files/stubs** + launch queue-prove jobs over them + research-pusher |
| 2 | Orchestrator model | cheap/fast router | **strong model** ("can't be stupid"); all roles configurable in `config.yaml` |
| 3 | LLM-manager trigger | after each failed gate | **struggle-triggered only** (§4.4), invoked by the deterministic manager; small/fast |
| 4 | Budget exhaustion | recorded, queue rolls on | **hard breakpoint**: interrupt queue, persist decision packet, orchestrator decides (§4.3) |
| 5 | Plan-state | plan.md + summary.json | + **`blueprint.json` dependency/exploration graph** (leanblueprint-inspired, §4.1); documentation-driven proving; every agent gets the artifacts |
| 6 | Dispatch | queue-level, prover-centric | **multi-level** (orchestrator/planner/decomposer) · **3 job archetypes** (prover/empirical/deep-search) · **lifecycle designed first** (Phase 3a) · primary value = experiments & search |
| 7 | Prover-job shape | (manager,prover) pair only | **Shape A default**: mini queue-prove over an orchestrator-prepared stub file (reuses ALL existing machinery); **Shape B**: queue-less single-target probe (§4.2) |
| 8 | Negation probe | ¬P attempt | + cheap **`plausible`** counterexample pre-probe (hint only) |
| 9 | Specs | fold/delete/keep | + **rewrite all specs to the current quality bar**; real functional **overhaul of `golf`/`refactor`** (OpenGauss inheritance) |
| 10 | Deep search | planner web agent | **multi-level ability** incl. **download/clone repos, papers, docs, libraries** (`repo_clone` tool, §4.6) |
| 11 | Stance | neutral | **research-pusher**: prevent difficulty-complaints; push beyond current knowledge; open-problem `RESEARCH_MODE` (§4.7) |
| 12 | Grounding | seams into code | explicit **reuse map** — composed from existing, proven agents/infra, nothing from scratch (§4.9) |
| 13 | Decomposer | a tool call (`lean_decompose_helpers`) | a **first-class role**: graph-aware, search-equipped, states lemmas via file-org powers, LeanProbe-validates skeletons, negation-probes suspicious sub-lemmas |

---

## 4. Core design details (new in v2)

### 4.1 The dependency / exploration graph (`blueprint.json`)

leanblueprint-inspired, machine-first (JSON authority; `plan.md` renders the human view, e.g. a
Mermaid diagram). Lives next to `live_status.json`; written through the same atomic path
(`workflow_state.py:305`).

```jsonc
{
  "goal": "main theorem statement / research objective",
  "nodes": [{
    "id": "n42",
    "kind": "theorem|lemma|def|conjecture",
    "name": "Namespace.decl_name",            // once stated in Lean
    "file": "Project/Generated/Bounds.lean",   // once placed
    "statement": "…Lean or informal statement…",
    "status": "conjectured|stated|proving|proved|blocked|false|split|parked",
    "attempts": 3, "owner": "agent-id|null", "notes": "why blocked / evidence refs"
  }],
  "edges": [{ "from": "n42", "to": "n17", "kind": "depends_on|split_of|evidence" }]
}
```

- **Statuses** adapt leanblueprint's (`stated/proved/can_state/can_prove/…`) to the loop:
  `conjectured` (informal only) → `stated` (Lean stub with `sorry`) → `proving` (assigned) →
  `proved` (kernel-verified **only**) | `blocked` (attempts exhausted, awaiting orchestrator) |
  `false` (negation proved — poisons `split_of` ancestors for re-decomposition) | `split`
  (superseded by children) | `parked` (deferred by orchestrator).
- **Reconciliation:** every cycle, node statuses are reconciled against live Lean state
  (`lean_inspect` / per-decl sorry counts) — the kernel, never the plan, is the truth for
  `proved`. This is the anti-drift mechanism.
- **Decision use:** the orchestrator reads the graph to (a) pick the frontier (`stated` nodes
  whose `depends_on` are all `proved`), (b) backtrack on `false` (invalidate the subtree, try
  another route), (c) split `blocked` nodes further, (d) organize files (group sibling stubs into
  one file → launch a queue-prove job over it). "This part of the graph is wrong because this
  dependency was proven false" and "split this further, it's too hard at once" become mechanical
  graph operations.

### 4.2 Dispatch: job taxonomy + lifecycle (designed before built — Phase 3a)

**One shared dispatch service** for all callers so tracking is uniform. The prover itself never
dispatches (keeps the inner loop simple); it escalates to the manager instead.

**Job archetypes**
1. **PROVER job.** Two shapes: **A (default for real proving)** — the orchestrator organizes stub
   theorems into a file and launches a *file-scoped mini `/prove` run* over it: the full existing
   queue + manager-prover + kernel gate machinery, spawned via the existing workflow spawn
   (`workflow.py:537`) or a `delegate_task` child running the native runner. **B (probe)** — a
   queue-less `delegate_task` child with the prover toolset + LeanProbe, one target, scratch-only
   or tightly file-locked. Recommendation: A whenever the result should land in the project; B for
   exploratory "can this even be proved this way" probes.
2. **EMPIRICAL job.** A `delegate_task` child that runs computations — Python via `terminal`,
   Lean experiments via LeanProbe `lean_check`/`lean_multi_attempt` — to *confirm or refute an
   idea numerically/experimentally before investing proving effort* (e.g. test a conjectured
   inequality on 10^5 samples; compute small cases; check a bound).
3. **DEEP-SEARCH job.** A `delegate_task` child with the full research toolset (`web_search`,
   `web_fetch`, `web_download`, `repo_clone` §4.6, `lean_search`, `lean_lemma_suggest`) that
   returns a **synthesized findings report** merged into `plan.md § Grounding` (papers, prior
   formalizations, similar results, candidate lemmas — with citations/paths).
4. **NEGATION probe.** `plausible` pre-probe (when applicable) → bounded `¬P` attempt in LeanProbe
   scratch. Outcome feeds the graph (`false`) and the feedback loop (D5).

**JobSpec (what a dispatcher must provide)**
`{job_id, archetype, objective (self-contained), inputs (graph-node refs, files, plan.md pointer),
toolset, budget {api_steps, wall_clock}, deliverable (schema the parent will consume), scope
(file locks / scratch), parent, report_to (plan-state section)}`.

**Ledger & lifecycle** — `summary.json.dispatch_ledger[]`, entry states
`proposed → deployed → running → {done | failed | stuck | killed}`, reconciled against the
existing per-agent activity streams (`activity/agents/*.jsonl`, `summarize_workflow_agents`,
`workflow_state.py:651`) so **a job can never be silently lost**. Dispatcher duties: monitor at
step boundaries; **patience policy** derived from the job's declared budget (don't kill a long
Lake build early; do kill a spinning search); **kill** via the existing agent inbox / control
channel (`_run_background_control_loop` seam) or child-process termination; **consume results
one-way** (verified disk edits + a plan/graph delta — never raw transcript). v1 is
synchronous-blocking (cap 3 concurrent), matching `delegate_tool.py` today; async lands only
after the ledger + pairs are proven.

### 4.3 Budget breakpoints (the missing "breaking point of no success")

Today `_handle_api_step_budget_exhaustion` (`native_runner.py`) records a failed attempt, restores
the baseline `sorry`, and the loop continues — whether the budget is 200 or 20000, the queue
effectively starts over with no decision point. v2 semantics:

- **Per-theorem breakpoint:** a cumulative per-theorem budget (attempts × steps) — on exhaustion,
  do NOT re-enter the theorem; mark the graph node `blocked`, persist a **decision packet**
  (statement, attempts, error signatures, search history, negation status) and surface a
  `budget-breakpoint` stop to the orchestrator: split / plan / negate / park / re-state / abort.
- **Queue-level breakpoint:** if K consecutive assignments exhaust (or the scope burns its budget
  with no `proved` delta), **interrupt the whole queue** — the owner's "stop/interrupt the queue
  in total" — rather than grinding the remaining items.
- **Phase 1 mechanical version** (before the orchestrator exists):
  `LEANFLOW_BUDGET_BREAKPOINT=1` turns both cases into a clean stop with the decision packet
  persisted in plan-state (a human — or a later orchestrator — decides). Phase 4 wires the
  orchestrator decision. Seams: `_handle_api_step_budget_exhaustion`, `_autonomous_stop_reason`
  (`native_runner.py:8921`), the retry-exhaustion path (`:1982`).

### 4.4 Struggle-detection signals (what summons the LLM-manager)

All deterministic, all already tracked or trivially derivable — the LLM-manager runs **only** when
one fires (never on the happy path, never per step):

| Signal | Source (exists today) |
|---|---|
| ≥2 genuine failed attempts on the assignment | failed-attempt ring buffer (`queue_manager.py:352`) |
| Same-error signature repeating | retry idempotency signatures (`queue_manager.py`) |
| Search spiral | `search_progress` tracker + nudge thresholds (`native_runner.py`) |
| No-progress turns | live-state signature stability (stall streak, `:8921`) |
| Budget pressure ≥70% | runtime budget warnings (`run_agent.py`) |
| Give-up phrasing in prover output | `_extract_blocker_summary` |

One signal → LLM-manager crafts the nudge. Persistent combinations → orchestrator re-route.
Exhaustion → budget breakpoint (§4.3).

### 4.5 Orchestrator file-organization & lemma-stating powers

The orchestrator/decomposer may create and edit Lean files **between prover turns** (they are not
the prover, so the queue edit-guard — which protects statements *during* prover turns — does not
apply; guard snapshots and the graph are refreshed after each orchestrator edit):

- **State new lemmas**: private or public, placed by design — same file next to the consumer, a
  sibling module, or a new file (e.g. `Project/Generated/<Topic>.lean`); each stated lemma becomes
  a `stated` graph node with `split_of`/`depends_on` edges.
- **Organize the sorry landscape**: group related stubs into files such that "launch a queue-prove
  job over this file" is a meaningful unit of work; one-lemma files are legitimate for hard
  probes.
- **Re-state**: when a statement is wrong (negation proved / evidence contradicts), the
  orchestrator re-states and resets the node — statement changes are orchestrator-level actions,
  never silent prover-level edits (the existing statement guard keeps enforcing that).

### 4.6 Deep search & repository acquisition

- Existing: `web_search` (general+code+papers), `web_fetch` (read any page/PDF), `web_download`
  (save artifacts, sandboxed), `lean_search`, `lean_lemma_suggest`.
- **New tool `repo_clone`** (Phase 5): `git clone --depth 1` into
  `.leanflow/workspace/repos/<name>` — size-capped, sandboxed to the workspace, returns the local
  path so file tools / `lean_search mode=local` can mine it (prior formalizations, a library to
  study, a paper's companion repo).
- **Availability:** the orchestrator, planner, and decomposer toolsets include the full search
  surface (they must "make up their decisions" with real evidence); deep-search *jobs* get it all
  plus clone. The inner queue-prover keeps its focused toolset (research reachable via
  escalation, not spiral-prone default).

### 4.7 Research-pusher stance (open problems)

Prompt-level contract for orchestrator, managers, and provers: **difficulty is a routing signal,
never a terminal state**. Concretely: give-up vocabulary is removed from prompts; "this is
hard/open" must be followed by a concrete next action (split, probe negation, deep-search
literature, run an experiment, park-and-advance); the LLM-manager's nudges embody the
optimistic-but-strict tone. A `LEANFLOW_RESEARCH_MODE=1` flag raises budgets, disables
give-up-flavored stop reasons (except kernel-`false`/negation-proved), and keeps the scope alive
under orchestrator parking — for runs on genuinely open problems.

### 4.8 Model configuration matrix (`config.yaml`)

Every role's model/provider explicitly configurable (routed via the existing `auxiliary_client`
task-based routing + `runtime_provider`):

| Role | Default tier | config key (proposed) |
|---|---|---|
| Prover (inner loop) | strong (today's agent model) | `models.prover` |
| **Orchestrator** | **strong** (default = prover model) | `models.orchestrator` |
| Planner / Decomposer | strong | `models.planner`, `models.decomposer` |
| LLM-manager (nudger) | small / fast | `models.manager_nudge` |
| Summarizer / compression | small (exists) | existing |
| Reasoning advisor / experts | existing config | existing |

### 4.9 Reuse map — grounded in existing, proven infrastructure

| Need | Existing machinery reused (no rewrite) |
|---|---|
| Job execution (empirical / deep-search / negation / inspect) | `delegate_task` children with scoped toolsets (`delegate_tool.py:391`; cap 3, sync) |
| Prover job shape A | the `/prove` workflow itself — spawn via `workflow.py:537` / native runner; full queue+gate reused |
| Kill / status control channel | agent inbox + `_run_background_control_loop` (exists) |
| Deployed-agent registry substrate | `summarize_workflow_agents` + `activity/agents/*.jsonl` (`workflow_state.py:651`) |
| Plan-state & graph persistence | `save_workflow_live_status` / `write_json_file` (`workflow_state.py:305`) |
| LLM calls (orchestrator / nudger) | `verification_providers.run_model_verification_review` pattern (`:160`) + `auxiliary_client` |
| Phase pattern (planner) | `_run_document_formalization_review_agent` template (`native_runner.py:5661`) |
| Workspace scoping | file locks (`lean_worker_dispatch.py:57`, `runtime/file_locks.py`) |
| All probes / experiments / validation | LeanProbe (`lean_incremental_check`, `lean_check`) — used as widely as possible |
| Decomposition core | `lean_decompose_helpers` (`lean_tool.py:658`), upgraded graph-aware |

New modules only: the graph leaf module, the thin dispatch-service wrapper, the orchestrator
module, `repo_clone`.

---

## 5. Phased plan (v2)

Every early phase remains **additive, flag-gated, dark-launchable, independently shippable**.

### Phase 0 — Finish the half-done queue migration *(unchanged from v1)*
Route production verdicts through the dead `decide()` (`queue_manager.py:484`); consolidate the
three drifting verdict copies (`native_runner.py:1890/1834/5815`); make `TheoremQueueManager` the
live authority (`:869/:886`). Shadow-compare for one release. **~1 wk / Low-Med.**

### Phase 1 — Plan-state + DEPENDENCY GRAPH + budget breakpoint (mechanical) + checkpoint retirement
- `plan.md` + `summary.json` **+ `blueprint.json`** (§4.1) written through `workflow_state.py:305`;
  per-cycle reconciliation of graph statuses against live Lean state; every spawned agent receives
  the artifact paths in its context.
- **Budget breakpoint, mechanical** (§4.3): `LEANFLOW_BUDGET_BREAKPOINT=1` → per-theorem and
  queue-level exhaustion become clean stops with persisted decision packets (no more silent
  restart). Seams: `_handle_api_step_budget_exhaustion`, `:8921`, `:1982`.
- Retire `/checkpoint`,`/rollback`,`/resume-plan` (`native_runner.py:3788`); resume = load
  `summary.json`+graph, reconcile; KEEP baseline-`sorry` restore (`:1982`) + git-shadow rollback
  (`native_checkpoints.py:205`) as silent safety; logging unchanged.
- **~2 wk / Low.** (Grew vs v1: the graph.)

### Phase 2 — Struggle detector + LLM-manager dark-launch
- Implement the §4.4 signal set as a small pure module; wire the deterministic manager to invoke
  `run_model_verification_review(task="manager_nudge")` (`verification_providers.py:160`) **only
  on signal** (never per step, never on `ok=True`); prompt per §4.7 tone.
- Dark-launch: log would-be nudges to `summary.json.manager_nudges`; enable via
  `LEANFLOW_MANAGER_LLM_ENABLED=1` after validation (message-shaping only, never the verdict).
- **~1 wk dark + days to enable / Low→Med.**

### Phase 3 — Dispatch: design first, then registry + negation probe
- **3a (design, ~0.5 wk):** the §4.2 lifecycle as a reviewed design doc — JobSpec, ledger states,
  patience/kill policy, result-consumption contract, reconciliation with `activity/agents`.
  **Product-owner review gate before 3b.**
- **3b (build, ~2 wk):** dispatch-service wrapper over `delegate_task`/`dispatch_worker`
  (flag: `lean_services.py:140`); persistent `dispatch_ledger`; first archetype = **negation
  probe** (`plausible` pre-probe → bounded `¬P` in LeanProbe scratch,
  `LEANFLOW_NEGATION_PROBE_BUDGET=1`/lemma), triggered by LLM-manager suggestion after ~2 failures
  or orchestrator risk-flag; outcomes → graph (`false`) + feedback loop.
- **Med.**

### Phase 4 — Orchestrator (deterministic floor) + decomposer role + breakpoint decisions
- `_orchestrator_route(RouteContext)` at scope-entry (`native_runner.py:9094`), on stall
  (`:8921`), and at **budget breakpoints** (§4.3 — replacing the Phase-1 mechanical stop with a
  decision: split / plan / negate / park / re-state / abort).
- Deterministic route table (LLM off): `direct-prove` / `decompose` / `plan` / `escalate`; the
  **decomposer as a first-class role** (§3.13): `lean_decompose_helpers` + graph-aware lemma
  STATING into organized files (§4.5) + LeanProbe skeleton validation; queue seeded from stub
  files via `assign` (`queue_manager.py:181`).
- Queue ownership/lifecycle dimension (claim on `TheoremKey`); `LEANFLOW_ORCHESTRATOR_MAX_ROUTES`
  per scope.
- **~2.5 wk / Med.**

### Phase 5 — Planner fan-out + deep-search + prover-pair jobs (multi-level dispatch live)
- `_run_planner_phase` (template `:5661`) on `plan/explore` routes: web · mathlib · empirical ·
  draft · negation sub-agents (cap 3, sync) → synthesizer merges into `plan.md` + graph → queue
  seeding. Premise retrieval (`lean_lemma_suggest`/`lean_hammer_premise`) becomes a mandatory
  prover pre-step.
- **`repo_clone`** tool + DEEP-SEARCH job archetype (§4.6); EMPIRICAL job archetype; **prover job
  shape A** (mini queue-prove over stub files) — dispatch now callable from orchestrator, planner,
  and decomposer alike.
- **~3 wk / Med-High.**

### Phase 6 — LLM orchestrator (strong model) + spec fold & IMPROVEMENT + research mode
- Enable the LLM layer over the pre-classifier (`LEANFLOW_ORCHESTRATOR_ENABLED`,
  `task="orchestration"`, **strong model** per D1; upgrade-only above the floor).
- Spec reintegration **and rewrite to the current quality bar** (`lean_workflow_specs.py:20`):
  `search.md`/`draft.md`/`review.md` → orchestrator-phase fragments; delete `checkpoint.md`;
  **functional overhaul of `golf.md`/`refactor.md`** (+ their routing/skills); prove.md gains an
  Orchestration section.
- **Research-pusher pass** (§4.7) over all prompts + `LEANFLOW_RESEARCH_MODE`.
- **~2.5–3 wk / Med.**

**Later / optional:** async dispatch with stuck-agent reclaim; curriculum ordering (easy→hard);
proof-progress de-prioritization; graph web view (leanblueprint-style rendering).

---

## 6. Decisions log (owner-answered, v2-resolved)

| # | Question | Resolution |
|---|---|---|
| 1 | Can the LLM-manager influence the verdict? | **Advisory-only confirmed.** It crafts adjusted nudges; invoked by the deterministic manager **only on struggle detection**, never per prover step. Kernel verdict untouchable. |
| 2 | Dispatch sync vs async? | **Sync v1 (cap 3).** Dispatch's biggest value = **experimentation & search**, not parallel proving; a few agents doing their own research/probe work is the target shape. |
| 3 | Orchestrator aggressiveness? | **Conservative floor + struggle detection + budget breakpoints.** Exhaustion must interrupt the queue (no silent restart) and trigger an orchestrator decision. **Planning includes building the dependency graph** (leanblueprint-adapted, §4.1) so decompositions, false-dependency backtracking, and further splits are graph operations. |
| 4 | Negation-probe budget/trigger? | **~1/lemma after ~2 failures or risk-flag**, with a `plausible` counterexample pre-probe where applicable (hint only — it doesn't always work). |
| 5 | Fate of the specs? | **Fold search/draft/review; delete checkpoint; keep golf/refactor standalone — and rewrite ALL specs to the current quality bar**, with a real functional overhaul of golf/refactor (OpenGauss inheritance, currently weak). |
| 6 | Checkpoint retirement scope? | **Retire the UX, keep logging + silent safety nets.** Documentation-driven proving: `.md` + dependency/exploration graph tell us what's done, what's next, and which paths are dead/false. |
| 7 | Where do sub-agents run Lean? | **LeanProbe as widely as possible** (probes/experiments/negation/validation); Lake-backed gate only for authoritative acceptance. |
| 8 | Orchestrator model tier? | **Strong model — the orchestrator is too important to be cheap.** Every role's model configurable in `config.yaml` (§4.8). |

---

## 7. Risks & mitigations (v2)

| Risk | Where | Mitigation |
|---|---|---|
| **Strong-orchestrator token cost** | every routing decision | Invoke only at scope-entry, stalls, and breakpoints — never per cycle; deterministic floor answers `direct-prove` without any LLM call; the graph keeps its context small (frontier + packet, not transcripts). |
| **Token blowup (planner fan-out)** | Phase 5 | Difficulty-gated routes; caps; fresh bounded context per job; deliverable-schema results (no transcript ingestion). |
| **Dispatch complexity / lost jobs** | Phases 3–5 | **Design-first (3a) with owner review**; ONE shared service + persisted ledger reconciled against `activity/agents/*.jsonl`; sync v1; kill via existing inbox; patience from declared budgets. |
| **Graph staleness / plan drift** | Phase 1+ | Per-cycle reconciliation against `lean_inspect`/sorry counts; kernel truth wins; `proved` only from the gate. |
| **Sub-agent sprawl / file clobbering** | Phase 5 | File locks + no-two-children-per-file rule; cap 3; ledger prevents double-dispatch of a sub-scope. |
| **Regressing the working queue** | Phases 0/4 | Shadow-compare in Phase 0; all flags default-off; `direct-prove` is byte-for-byte today's path; the kernel gate is never modified. |
| **Wasted work on false sub-lemmas** | decompose routes | `plausible` pre-probe + negation probe on suspicious/auto-generated sub-lemmas; `false` nodes poison ancestors for re-decomposition. |
| **LLM-manager over-steering / over-calling** | Phase 2 | Struggle-triggered only; dark-launch first; message-shaping only; happy path never calls it. |
| **Orchestrator edits vs edit guards** | Phase 4 | Orchestrator/decomposer edit **between** prover turns; guard snapshots + graph refreshed after each; statement changes are orchestrator-only actions. |

---

**Start (on greenlight): Phases 0–3.** Phase 0 (verdict unification), Phase 1 (plan-state + graph +
mechanical budget breakpoint + checkpoint retirement), Phase 2 (struggle-triggered nudger, dark),
Phase 3a→3b (dispatch design review, then registry + negation probe). Together they deliver the
documentation-driven substrate, the missing breaking point, validated nudges, and the feasibility
probe — de-risking the orchestrator and fan-out phases that complete the vision.

Key files (verified seams): `leanflow_cli/native/native_runner.py` (loop `:9059`, gate `:1890`,
checker `:1210`, phase template `:5661`, stall `:8921`, scope-entry `:9094`, checkpoint UX `:3788`,
baseline restore `:1982`), `leanflow_cli/workflows/queue_manager.py` (`decide()` `:484`, `assign`
`:181`, `peek_assignment` `:219`), `leanflow_cli/workflows/queue_models.py` (`TheoremKey` `:52`,
`classify_check` `:337`), `leanflow_cli/lean/lean_worker_dispatch.py` (`:47,:57,:104`),
`leanflow_cli/lean/lean_services.py` (gate `:140`), `leanflow_cli/workflows/verification_providers.py`
(`:160`), `leanflow_cli/workflows/manager_verification.py` (`:116`),
`leanflow_cli/workflows/workflow_state.py` (`:98-99,:305,:651`),
`leanflow_cli/lean/lean_workflow_specs.py` (`:20`), `tools/implementations/delegate_tool.py`
(`:391`), `tools/implementations/lean_tool.py` (`lean_decompose_helpers` `:658`),
`leanflow_specs/workflows/prove.md` (`:10`).

References: Hilbert — arXiv:2509.22819; leanblueprint — github.com/PatrickMassot/leanblueprint.
