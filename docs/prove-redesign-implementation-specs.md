# LeanFlow /prove Redesign — Implementation Specs (companion to prove-redesign-roadmap.md)

Implementation record for the redesign. The original function-level design was produced on
2026-07-02; the promoted implementation delta below is current as of 2026-07-18. Historical seam
inventories and superseded schemas remain in Parts I–IV to explain the rollout, but current module
docstrings and the delta below are authoritative when they disagree with an earlier proposal.

## Promoted implementation delta (2026-07-18)

- `manager_nudge.py` is now a persistence coach, not a manager or strategist. Its model schema
  contains only `message` and `commitment`; `progress_acknowledged` is populated separately from
  deterministic parent state. It runs after every rejected proof turn. `off`, `dark`, malformed,
  surrendering, and unavailable outputs all apply and record a deterministic positive fallback;
  `prove`/`autoprove` default to live model coaching. The model
  may acknowledge effort and failure-as-information, but any Lean/kernel/source-status assertion,
  proof-progress claim, or route selection makes its reply unusable. Verified helper names are
  attached only by deterministic parent code, and a zero-helper turn cannot call an unchanged
  `sorry` compiled progress.
- `workflow.py` exposes `--research` and `--research-workers N`; the profile enables the full
  planning, retrieval, orchestration, fidelity, dispatch, negation, reporting, learning, and
  coaching stack. `--research` defaults to two background subprocess workers. Explicit CLI
  activation overwrites inherited feature-disable switches; environment-only activation keeps
  intentional per-feature overrides but cannot make routable difficulty a terminal stop.
- `campaign_epoch.py` turns 120 cycles, four no-progress routes, and context pressure into durable
  epoch rollovers. It preserves graph/plan/job/findings/attempt evidence and starts a fresh model
  context under the same campaign instead of returning a mathematical stop. The spent route
  portfolio is durable, and the fresh epoch must start a distinct non-direct route before another
  direct proof attempt is allowed. Route completion is token- and epoch-bound and is recorded only
  after observable managed work; a failed consult, stale route, signal, or provider pause cannot
  consume the obligation. The pending selection is persisted with its exact target and file, so an
  infrastructure resume reuses it without re-running route selection or incrementing the no-progress
  route streak; a genuinely later stall/event decision still increments the streak normally. A
  versioned migration also repairs older checkpoints only when durable activity shows the repeated
  same-scope route was another `scope-entry` after an exit-2 provider failure and before route start;
  it refuses ambiguous histories or route streaks changed by later graph progress.
- `research_portfolio.py`, `dispatch_service.py`, and `native/dispatch_worker.py` provide
  process-isolated fire-and-continue research jobs. The parent consumes structured deliverables
  once and remains the only writer of shared plan/graph state. Epoch rollover harvests completed
  jobs, retires open old-epoch workers, and refills distinct routes on the next tick. The rollover
  preserves semantic-cooldown evidence, but if ordinary uncooled selection cannot fill configured
  capacity, the refill may relax only cooldowns produced in an older epoch and must still select a
  history-distinct objective/signature. A cooldown produced in the current epoch is never relaxed.
  Exact evidence-to-helper follow-ups reserve their source finding from foreground delivery while
  active. After termination, only an actionable, schema-valid exact helper or replacement remains a
  reservation. Materialized actionable candidates lead the batch and couple their source receipt,
  preventing foreground/background duplicate synthesis.
  Staging a canonical checked helper additionally persists one exact-assignment parent-action
  record. Research-prompt acknowledgement never clears it. The next safe outer boundary reruns the
  exact helper and allowed-axiom profile before any orchestrator consultation; an accepted helper
  fences one foreground insertion opportunity until the ordinary edit/helper gate banks it. Source
  drift returns the record to recheck, mathematical rejection retires it, and operational failure
  remains resumable. Managed search likewise projects confirmed later same-file declarations out of
  the usable result list using the current disk index, preventing source-order-invalid tactic work.
  The rollover transaction persists a worker-refresh token before process cleanup;
  startup/maintenance replays
  an interrupted refresh, preserves any result that won the cancellation race, and clears the token
  only after no matching pre-refresh worker remains open. A capacity-deferred planner persists a
  versioned campaign/epoch/target/file reservation before yielding. Resumed and live parent polls
  continue harvesting and consuming findings with refill disabled until that route runs. If a
  refill was already in flight when the plan route became pending, the parent retires only the
  replacement launches from that transaction; older active research is not preempted. A completed
  job whose slot cannot be refilled in the same maintenance pass now creates one versioned,
  exact-assignment `research_portfolio_pending_replacement` intent containing attempt/worker/slot
  counts and bounded triggering job ids. Repeated heartbeats are write/event deduplicated, normal
  refill clears the intent only after configured capacity is filled, and epoch refresh honors the
  intent immediately after old-worker reconciliation by launching distinct fresh-epoch routes.
  The dispatch ledger remains the lossless finding authority. Its prompt cache keeps a 32-result
  active-target safety cap but reserves only one three-result batch for split-ancestor evidence;
  exact-target findings are materialized and delivered first. Deferred results emit explicit
  archive activity, and ancestor paging cannot suppress the required scope-entry child workers.
  Scratch dispatch uses explicit, nonempty archetype allowlists and fails closed instead of
  inheriting parent/default tools. Workers receive no terminal or shared-file writer; empirical
  jobs get only bounded deterministic computation plus Lean checks, while other research workers
  cannot call the nested `lean_reasoning_help`/`lean_decompose_helpers` model advisors. The advisor
  handlers enforce the same boundary before provider resolution.
- `core/provider_capacity.py` makes `--research-workers N` a cross-process live-actor cap shared by
  dispatch jobs and planner delegates. Actors acquire before agent construction; copied tool
  contexts and nested auxiliary calls retain the lease. Foreground control calls stay ungated, and
  a saturated planner records `capacity-deferred` for next-boundary retry after a bounded wait.
- `orchestrator_event_watermark.py` closes the consultation-cadence gap during long foreground
  turns. Parent maintenance publishes job completions to a theorem-scoped monotonic watermark;
  safe read/search callbacks yield to the outer loop, while edit and verification callbacks never
  route inside their commit. One consultation acknowledges exactly its captured prefix, so
  concurrent later events and failed consultations remain pending without duplicate publication.
- Startup and target rotation now avoid duplicate Lean discovery work. `_build_live_proof_state`
  reuses `lean_inspect`'s capability report and probes only when inspection yields none; a rotated
  queue target calls the goals-only `lean_goals` service with that report instead of repeating
  diagnostics, project `sorry` scans, or capability discovery. Startup emits paired proof-state
  refresh events with wall/phase timing. The post-tool callback runs structural queue-edit
  finalization only for an exact supported source edit with a valid matching snapshot; read-only,
  malformed, and support-file callbacks still run ordinary result handling without consuming stale
  edit state or emitting an `[unknown]` finalization event.
- Foreground research-mode and dispatch-worker `lean_incremental_check` calls apply a 300-second
  cold-start timeout floor. A model-requested 60-second deadline can no longer repeatedly kill a
  freshly reclaimed Lean service before Mathlib startup finishes; checks still return immediately
  when Lean finishes early, and caller deadlines above 300 seconds are unchanged. Results expose
  requested/effective timeout telemetry and the selected timeout policy.
- Final-report review has a rejection-only deterministic source gate. When a non-success response's
  exact assigned declaration still contains a literal comment/string-stripped `sorry` or `admit`,
  the manager records a target-scoped `source_placeholder_gate` failure without opening a Lean
  transaction. A placeholder-free declaration, missing identity, or explicit success claim still
  goes through the ordinary incremental/canonical kernel gate; source scanning can never accept a
  proof.
- Planner synthesis now passes every grounding, strategy, and node assertion independently through
  the conservative arithmetic preflight before any summary/graph merge or stub write. A plainly
  false affine identity or divisibility claim rejects the whole synthesis with journaled normalized
  counterevidence; unsupported nonlinear claims remain fail-open for ordinary Lean validation.
- Research-result classification preserves mathematical content that accompanies an operational
  failure. A result is operational-only only when it has neither semantic fingerprints nor managed
  boundary evidence, so a nested timeout cannot erase an already-derived obstruction or proof shape;
  versioned novelty/substance migration recovers older archived findings under the same rule.
- `negation_promotion.py` requires matching declaration/signature/source revisions, fresh Lean
  elaboration, no `sorry`, and the standard-axiom allowlist. Scratch probes alone are never
  authoritative; only promoted main-goal negation produces `disproved`. Its pending-to-committed
  transaction persists complete evidence before graph falsity, replays or rolls back interrupted
  commits, and startup revalidates the exact promoted evidence before any provider is constructed.
- Promoted false sublemmas trigger a source-first versioned cleanup transaction. The transaction
  restores the exact pre-decomposition parent and seals every same-revision unresolved decomposer
  declaration transitively depending on the false node before removing it from source and graph.
  Unrelated verified declarations are never deleted; verified, externally owned, evidence-bearing,
  or source-drifted dependents quarantine cleanup. Queue replay retires all deleted identities before
  graph synchronization, preventing a stale solved outcome from recreating an invalid obligation.
  A committed version-1 cleanup is upgraded only when its exact archive twin, canonical source,
  false decomposer tombstone, graph identities, and unresolved dependent declarations still agree.
  The version-3 replacement is persisted before source CAS and carries its predecessor transaction
  id. Its transitive closure admits only current-full-source unresolved decomposer declarations and
  source-less unresolved planner/decomposer artifacts assigned to the same parent. Source CAS deletes
  only the declaration-backed records; graph replay retires both record kinds, removes their structural
  edges, reopens the parent, and preserves unrelated proved helpers. Interruption replays idempotently,
  while any external source authority, verified/evidence edge, or source/graph drift creates a
  resumable quarantine. The migration-only evidence check permits graph reconciliation to have
  advanced the source hash on the exact named proof and later proved `prover-edit` obstruction
  declarations; it still requires unique stable identities, exact current-source text, no placeholder,
  and the authenticated committed-v1/archive pair. Discovery first checks for structural migration
  work: an evidence-only committed-v1 tombstone is an idempotent read-only no-op, even if its old audit
  evidence no longer matches current promotion-era metadata. The exact obsolete no-work evidence
  quarantine is auto-resolved from its authenticated transaction/archive identity; no other reason is
  forgiven. Fresh promotion cleanup does not use this allowance.
- Headless outcomes are truthful: `0=verified`, `3=authoritatively disproved`, `2=unresolved but
  checkpointed/resumable`, `1=configuration/runtime failure before a valid campaign`, and
  `130=signal interruption`.
- `evals/corpus_manifest.json` freezes 40 T2 cases, ten T3 campaigns, and four adversarial cases;
  `evals/harness.py` reports give-up, false-success, coach coverage, diversity, dispatch, graph,
  and epoch metrics.

Parts: I = Phases 0–1, II = Phases 2–3 + lineage + concrete-result mechanics, III = Phases 4–6,
IV = existing-assets reuse inventory.

---

# PART I — Phases 0–1 (queue-verdict unification; plan-state + graph + budget breakpoint + checkpoint retirement)

# LeanFlow `/prove` Redesign — Implementation-Ready Spec: Phase 0 + Phase 1

Verified against branch `docs/prove-redesign-roadmap` @ `/Users/lmilikic/Desktop/LeanFlow` (roadmap: `docs/prove-redesign-roadmap.md`). Every seam below was re-read in the tree; drifted roadmap citations are corrected inline and collected in §0.3.

---

## 0. Ground truth, corrections, and shared constraints

### 0.1 Verified seam inventory (file:line, this tree)

| Seam | Location |
|---|---|
| Autonomous loop | `_drive_autonomous_followups` `leanflow_cli/native/native_runner.py:9059`; cycle body scope-entry refresh `:9093-9095`; stop dispatch `:9161-9171` |
| Manager gate (final report) | `_review_agent_final_report` `native_runner.py:1890-2075`; call sites `:8329` (background control loop, def `:8230`), `:9235` (autonomous), `:9388` (startup), `:9747` (interactive) |
| Deterministic kernel checker | `_manager_check_queue_item` `native_runner.py:1210-1216` → `_manager_incremental_check_queue_item` `:1127` (LeanProbe `check_target`) with fallback `_manager_verify_queue_file` `:1110` (`lake env lean`, via `lean_verify` `leanflow_cli/lean/lean_services.py:976`) |
| Legacy string adapter | `_manager_feedback_kind` `native_runner.py:1834-1854` over `_manager_check_for_feedback_kind` `:1767-1831` and `classify_check` `leanflow_cli/workflows/queue_models.py:337-369` |
| Queue-step boundary | `_finish_queue_step_boundary` `native_runner.py:3032-3413`; invoked from `_handle_managed_tool_result` `:3416` at `:3528` (post-`patch`/`write_file`) and `:3568` (verification-tool results) |
| Live-state blocker probe | `_same_queue_assignment_still_blocked` `native_runner.py:5815-5853`; consumers `:3117`, `:6021`, `:9259` |
| Budget exhaustion | `_handle_api_step_budget_exhaustion` `native_runner.py:6008-6085`; predicate `_result_exhausted_api_steps` `:5856-5874`; call sites `:9240` (autonomous), `:9394` (startup), `:8335` (background) |
| Retry-exhaustion restore | call site `native_runner.py:1982` inside the gate; post-edit variant `:3182-3194`; implementation `_restore_queue_assignment_to_baseline_sorry` `:5919-5968` |
| Stop reason | `_autonomous_stop_reason` def `native_runner.py:8801`; stall trip `stable_cycles >= _autonomous_stalled_limit()` at `:8921-8922` |
| Queue manager | `TheoremQueueManager` `leanflow_cli/workflows/queue_manager.py:86`; `assign` `:181`, `peek_assignment` `:219`, `decide()` `:484-531` (dead in production — only used by `tests/leanflow/test_queue_manager.py:53-54`), `Decision` `:881-893`, `from_autonomy_state` `:681`, `to_autonomy_state` `:820`, `OWNED_AUTONOMY_KEYS` `:97-108` |
| Reconstruct/flush bridge | `_queue_manager_from_state` `native_runner.py:869-883`, `_flush_queue_manager` `:886-896` — **19 reconstruct call sites** (`:912,:1041,:1458,:1566,:1719,:1733,:1747,:4325,:4337,:4488,:4534,:4551,:4698,:4737,:4761,:4791,:5020,:6038`) |
| Retry limits | `MANAGER_WARNING_RETRY_LIMIT = 1` `native_runner.py:102`, `MANAGER_HARD_RETRY_LIMIT = 2` `:103`, `MANAGER_POST_EDIT_HARD_RETRY_LIMIT = 8` `:108`; queue-manager defaults `queue_models.py:24-27` (`DEFAULT_HARD_RETRY_LIMIT = 2`) |
| Plan-state write path | `save_workflow_live_status` `leanflow_cli/workflows/workflow_state.py:305-306` → `write_json_file` `leanflow_cli/workflows/workflow_json_io.py:31-33`; state root `workflow_state_root()` `leanflow_cli/workflows/workflow_state_paths.py:47-48` (`<project>/.leanflow/workflow-state/`); live-status path `workflow_state.py:98-99` |
| Truly atomic writer | `core/utils.py:13` `atomic_json_write` (tempfile + fsync + `os.replace`), tested by `tests/test_atomic_json_write.py` |
| Lean truth | `lean_inspect` `lean_services.py:910-968` returning `LeanInspection` `leanflow_cli/lean/lean_models.py:48-60`; per-decl `has_sorry` from `_declaration_line_index_from_text` `leanflow_cli/lean/lean_parsing.py:279` (sets `has_sorry` at `:315`), surfaced by `_find_declaration_entry` `leanflow_cli/proof_state_builder.py:58`; sorry counts `leanflow_cli/native/native_lean_files.py:116` (`_count_sorries`) and `:144` (`_count_project_sorries`) |
| Checkpoint UX | help text `native_runner.py:3788-3790`, command list `:7899`; handlers `/checkpoint` `:9602`, `/resume-plan` `:9623`, `/rollback` `:9642`; `_resume_plan_from_checkpoint` `leanflow_cli/native/native_checkpoints.py:200`, `_rollback_to_checkpoint` `:205`, `_checkpoint_replay_history` `:193`; writers `_write_workflow_checkpoint` `native_runner.py:7631`, `_maybe_write_milestone_checkpoint` `:8668`, `_maybe_checkpoint_before_compaction` `:8692`; git-shadow `CheckpointManager` `tools/utilities/checkpoint_manager.py:182`, owned at `run_agent.py:774` |
| Agent-spawn env contract | `resolve_workflow_request` env block `leanflow_cli/workflow.py:476-523` (`LEANFLOW_NATIVE_*`, `LEANFLOW_PROJECT_ROOT`); `spawn_workflow` `:564`; startup prompt `_startup_user_message` `native_runner.py:8541`; continuation prompt `_autonomous_continuation_prompt` `:8927`; system prompt `_managed_system_prompt` `:8624`; worker prompt `_worker_prompt` `leanflow_cli/lean/lean_worker_dispatch.py:19-44`; child system prompt `_build_child_system_prompt` call `tools/implementations/delegate_tool.py:197`; `delegate_task` `:391` |
| Agent registry | `summarize_workflow_agents` `workflow_state.py:651`; activity append `append_workflow_activity` `:309` (flock-serialized `_locked_append` `:54`) |

### 0.2 House-rule constraints that bind this spec

- Layering `core/` → `agent/`+`tools/` → `leanflow_cli/`; new logic in focused leaf modules, never piled onto `native_runner.py`/`run_agent.py` (`AGENTS.md:52-58`).
- **Extraction hazard**: `tests/leanflow/test_native_runner.py` monkeypatches internals via `setattr(runner, NAME, …)`; a function moved to a sibling module that calls helpers by bare name silently bypasses the patch (`AGENTS.md:80-83`). Consequence: Phase 0 keeps the four verdict functions *in* `native_runner.py` and changes what they call; new pure logic lives in `queue_manager.py`/`queue_models.py` (already the tested home).
- Kernel gate `_manager_check_queue_item` (`:1210`) is never LLM-overridable and is not modified by either phase.
- Characterization tests pinned before refactoring coupled code (`AGENTS.md:69-71`).
- Env contract is `LEANFLOW_`-only (`AGENTS.md:87-88`).

### 0.3 Roadmap citation corrections (drift found)

1. Roadmap §2/§4.9 "spawn via `workflow.py:537`" → `spawn_workflow` is at `leanflow_cli/workflow.py:564`; the env-contract construction is `resolve_workflow_request` `:394` (env block `:476-523`).
2. Roadmap §5/Key-files "checkpoint UX `:3788`" → `:3788` is only the `/checkpoint` help line; the actual handlers are `:9602/:9623/:9642` in the interactive loop, and the replay/rollback logic lives in `native_checkpoints.py:200/:205`.
3. Roadmap §4.3 "the retry-exhaustion path (`:1982`)" — correct as a call site (inside `_review_agent_final_report`); the function itself is `:5919`. Both cited above.
4. Roadmap §4.1/§5 "written through the same atomic path (`workflow_state.py:305`)" — **`save_workflow_live_status` → `write_json_file` is NOT crash-atomic** (plain `Path.write_text`, `workflow_json_io.py:31-33`). The repo's real atomic writer is `core/utils.py:13 atomic_json_write`. Phase 1 uses `atomic_json_write` and keeps `read_json_file` for tolerant reads.
5. Roadmap §2 "stall (`:8921`)" — `:8921` is the stall *trip line*; `_autonomous_stop_reason` starts at `:8801`. Fine, but both cited for precision.
6. Roadmap §4.4 "failed-attempt ring buffer (`queue_manager.py:352`)" → `:352` is `_prune_attempts`; the buffer semantics (max `DEFAULT_FAILED_ATTEMPT_HISTORY = 10` per key) are `queue_models.py:24` + `queue_manager.py:352-364`. Correct in substance.
7. Roadmap Phase 0 "three drifting verdict copies (`native_runner.py:1890/1834/5815`)" — verified, but the drift set is actually **four** production paths (add `_finish_queue_step_boundary` `:3032`, which the roadmap's Phase-0 prose omits but this task scopes in).

---

# PHASE 0 — Queue-verdict unification

## P0.1 Precise map of all production verdict paths (and the enumerated drift)

### Path A — `_review_agent_final_report` (`native_runner.py:1890-2075`)

- **Trigger**: after every `_run_managed_conversation` (4 sites: `:8329,:9235,:9388,:9747`). Gates: `_single_queue_item_turn_enabled()` (`:449` — autonomous + `LEANFLOW_NATIVE_ACTIVE_FILE` set), `completed and not interrupted` (`:1898`), a valid `autonomy_state["current_queue_assignment"]` (`:1900-1904`), and the final text matching the success-claim regexes `_final_report_claims_queue_success` (`:1621`).
- **Inputs**: `result` mapping (`messages`, `final_response`, `completed`, `interrupted`), `autonomy_state` dict.
- **Pipeline**: fresh kernel check `_manager_check_queue_item` (`:1912`) → record verification `_record_manager_verification` (`:1917`, writes `last_verification` into autonomy_state via `_store_last_verification` `:1449`) → if ok, *cleanup denial*: `_declaration_diagnostic_feedback_reason` (`leanflow_cli/lean/lean_diagnostic_feedback.py:162`) may flip `ok=False` with `local_cleanup_reason` (`:1928-1941`) → if still ok and `LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK`, axiom-dependency veto (`:1946-1956`, forces `feedback_kind="error"`) → `_manager_feedback_kind` (`:1953`) → retry accounting: warning limit 1 / hard limit 2 (`:1962-1965`), count via `_manager_feedback_retry_count` (`:1709`, reconstructs a manager) — warning exhausted ⇒ **accept** with `accepted_after_warning_retry_limit` (`:1975-1980`); a completed hard-feedback window ⇒ baseline-sorry restore (`:1982`), append `[LEANFLOW-NATIVE LOCAL FEEDBACK WINDOW COMPLETE]` user message, `completed=False`, and retain the compatibility `exit_reason="manager_retry_exhausted"` while the campaign changes route (`:1988-2006`).
- **Outputs/side effects**: mutated `result` (`manager_final_report_review`, messages, completed, exit_reason); on ok clears retries (`:2009`); on not-ok increments retry *idempotently by signature* `_manager_feedback_retry_signature` (`:2054-2060`, sig def `:1752`) and appends `_manager_final_report_feedback` (`:1642`); activity `manager-final-report-review` (`:2015`); stdout; possible file write (restore).

### Path B — `_finish_queue_step_boundary` (`native_runner.py:3032-3413`)

- **Trigger**: `_handle_managed_tool_result` (`:3416`) — (i) successful `patch`/`write_file` on the assignment: runs `_manager_check_queue_item` immediately and calls the boundary with `verification_tool="{tool}+{manager_tool}"` (`:3524-3534`); (ii) `apply_verified_patch` arms `agent._managed_pending_theorem_feedback` (`:3494-3498`); (iii) results of `lean_incremental_check(check_target)`, `lean_verify`, or lake-flavored `terminal` commands (`_tool_result_counts_as_theorem_feedback` `:1057-1068`) close the pending turn (`:3568`). `lean_incremental_check(feedback)` is diagnostic-only and never reaches the queue-step boundary, so inspecting an unchanged `sorry` cannot fabricate an additional failed attempt or attempt-based coach/reroute signal.
- **Pipeline**: records the verification (`:3066-3076`) → rebuilds live state (`:3080`) → cleanup reason FIRST (`:3103-3115`, sets `feedback_kind` from a synthetic check) → else, if same assignment, Path C decides `still_blocked` and infers kind from `entry.has_sorry` (`:3116-3128`) → warning branch consumes/accepts at limit inline (`:3131-3163`) → hard blockers consume retries **only when the trigger was an edit tool** (`post_edit_verification`, `:3057-3062`) against `MANAGER_POST_EDIT_HARD_RETRY_LIMIT = 8` (`:3172`), restoring baseline at limit (`:3182-3194`) → if still blocked, records failed attempt `_remember_failed_attempt` (`:3208`, def `:4642`) and continues same turn via `agent.set_tool_result_appendix` (`:3344`) with an escalation nudge after N attempts (`:3324-3342`); otherwise yields via `_request_step_boundary_interrupt` (`:3413`, def `:1094`). Attempt identity is the exact theorem, declaration SHA-256, normalized gate verdict, and provider-turn key (campaign + epoch + cycle + campaign-wide monotonic turn nonce), so diff/full-check presentations of one unchanged rejection cannot inflate attempts, while a new epoch/process turn cannot be collapsed into an old same-numbered cycle.
- **Side effects**: `agent._managed_step_boundary_recorded_attempt` (`:3234`, consumed by the loop at `:9250-9255`), activity events `queue-theorem-feedback`/`queue-theorem-cleanup-feedback`/`queue-theorem-retry-exhausted`/`queue-step-boundary` (`:3235-3278`), tool-result appendix, interrupt request.

### Path C — `_same_queue_assignment_still_blocked` (`native_runner.py:5815-5853`)

- **Inputs**: `autonomy_state["current_queue_assignment"]` + a `live_state` snapshot (not a fresh kernel check).
- **Semantics**: same (target,file) via `_same_active_file`; then blocked iff `_manager_feedback_kind` over a **synthetic check** `{"ok": not _goals_still_open(goals), "file_check_ok": False, "output": diagnostics, "goals": goals, "local_cleanup_reason": "contains sorry"?}` (`:5841-5848`) returns error/sorry, OR declaration `entry.has_sorry`, OR goals open (`:5849-5853`).
- **Consumers**: boundary (`:3117`), budget exhaustion (`:6021`), the loop's fallback failed-attempt recorder (`:9256-9261`).

### Path D — budget-exhaustion (`_handle_api_step_budget_exhaustion`, `:6008-6085`)

- **Gate**: `_result_exhausted_api_steps` (`:5856` — `exit_reason ∈ {max_iterations, iteration_budget_exhausted}` or `api_calls ≥ agent.max_iterations`) AND Path C blocked (`:6021`).
- **Behavior**: re-`assign()`s the original snapshot into a reconstructed manager to fix the record race (`:6036-6045`), records a failed attempt (`refresh_baseline=False`, `:6046`), restores baseline sorry (`:6047`), re-verifies file (`:6049`), appends `[LEANFLOW-NATIVE API STEP BUDGET EXHAUSTED]` (`:6053-6061`), rebuilds live state, activity `api-step-budget-exhausted` (`:6074`). **Then the loop continues** — this is the "silent restart" Phase 1 §P1.4 turns into a breakpoint.

### The drift (enumerated — this is the Phase-0 risk)

| # | Divergence | A (final report) | B (boundary) | C (live-state) | D (budget) |
|---|---|---|---|---|---|
| D1 | Evidence source | fresh kernel check `:1912` | check passed in from tool result / fresh check `:3524` | live_state diagnostics/goals only, synthetic `file_check_ok:False` `:5841` | Path C |
| D2 | Hard-retry limit | 2 (`:103`) | **8** (`:108`), and only for edit-tool triggers `:3167-3171`; verification-tool triggers consume **nothing** | n/a | none (restores unconditionally) |
| D3 | Warning increment timing | after feedback message, only in non-ok tail `:2053-2060` | at detection `:3155-3163` (before the model's cleanup turn) | n/a | n/a |
| D4 | Warning acceptance | `ok=True` + `accepted_after_warning_retry_limit` `:1975-1980`; retries cleared via ok path `:2007-2013` | `warning_retry_accepted` + explicit clear `:3141-3154` | n/a | n/a |
| D5 | Failed-attempt recording | never records | records on still-blocked `:3208` | n/a (predicate only) | records `:6046` |
| D6 | Axiom-profile veto | yes `:1946-1956` | **no** | no | no |
| D7 | Classification order | ok → cleanup flip → kind | cleanup reason → kind → C fallback (kind from `has_sorry`, not classifier) `:3126-3128` | kind from synthetic check | n/a |
| D8 | Continuation mechanism | user message appended to result | tool-result appendix + step-boundary interrupt | n/a | user message appended to history |
| D9 | Claim gating | success-claim regex `:1909` | none | none | none |

`_manager_feedback_kind` itself is already unified over `classify_check` (`:1846-1854`) — the drift is in the *policy* around it (retry limits, ordering, side effects), which is exactly what `decide()` was built to own (`queue_manager.py:489-492` docstring says so).

## P0.2 `decide()`/`classify_check` today, and the unified signature

**Current policy encoded** (`queue_manager.py:484-531`): HARD_BLOCKER → consume hard retry; at limit → `restore_baseline`, else `continue_same_theorem`. WARNING_ONCE → exhausted → `advance_queue` reclassified ACCEPT; else consume + `continue_same_theorem`. FUTURE_ONLY / ACCEPT → `advance_queue`. Tests pinning it: `tests/leanflow/test_queue_manager.py:32` (`test_classify_check_uses_one_explicit_priority_order`), `:47` (`test_warning_cleanup_is_consumed_once_per_assignment` — the only `decide()` callers in the repo).

**What `decide()` is missing to subsume A/B/C/D**: signature-idempotent consumption (production uses `consume_retry_once_for` `:425-443`; `decide()` uses raw `consume_hard_retry` `:495`), context-dependent hard limits (2 vs 8 vs none — D2), axiom veto (D6), cleanup-reason input (D7), a record-failed-attempt output (D5), a mutation-free evaluation mode (required for shadow compare — `decide()` currently mutates counters, so calling it in shadow would corrupt production retry state), and a budget-breakpoint action (Phase 1 hook).

### Deliverable P0.2 spec

**(a) Files**: modify `leanflow_cli/workflows/queue_models.py` (new pure types), `leanflow_cli/workflows/queue_manager.py` (rework `decide`), `leanflow_cli/native/native_runner.py` (call-site wiring only). No new module needed — this is the existing tested home (house-rule compliant; avoids the monkeypatch extraction hazard §0.2).

**(b) Signatures** (new/changed):

```python
# queue_models.py
class DecisionSource(str, Enum):
    """Which production gate is asking: final_report | post_edit | verification_result | live_state | budget_exhaustion."""
    FINAL_REPORT = "final_report"
    POST_EDIT = "post_edit"                 # patch/write_file/apply_verified_patch trigger
    VERIFICATION_RESULT = "verification_result"  # lean_verify / lean_incremental_check / terminal result
    LIVE_STATE = "live_state"               # Path C synthetic evidence
    BUDGET_EXHAUSTION = "budget_exhaustion"

@dataclass(frozen=True)
class DecisionContext:
    """Everything one manager gate knows, source-tagged so decide() can reproduce every legacy branch."""
    source: DecisionSource
    check: ManagerCheck                      # built by _manager_check_for_feedback_kind (:1767) or the C synthetic builder
    signature: str = ""                      # retry idempotency signature (_manager_feedback_retry_signature :1752)
    cleanup_reason: str = ""                 # _declaration_diagnostic_feedback_reason output ("" = none)
    axiom_blockers: tuple[str, ...] = ()     # _manager_axiom_profile_blocker output (:2888)
    claims_success: bool = True              # Path A regex gate result; others pass True
```

```python
# queue_manager.py — decide() split into pure evaluation + explicit commit (shadow-safe)
def decide(self, ctx: DecisionContext) -> Decision:
    """Pure verdict policy: classify ctx, read (never mutate) retry counters, and return the action
    plus the side-effect plan (retries to consume, attempt to record, restore to perform)."""

def apply_decision(self, ctx: DecisionContext, decision: Decision) -> Decision:
    """Commit decision side effects to this manager: consume retries idempotently by ctx.signature,
    clear retries on accept. File restore/attempt recording stay runner-owned (I/O)."""
```

**(c) Data schema — extended `Decision`** (replaces `queue_manager.py:881-893`):

```python
@dataclass(frozen=True)
class Decision:
    action: str            # "continue_same_theorem" | "advance_queue" | "restore_baseline" | "budget_breakpoint"
    classification: Classification
    feedback_kind: str     # legacy string: "sorry"|"error"|"warning"|"" (rendered by the old adapter rules :1848-1854)
    reason: str
    consume_retry: str = ""            # "" | "warning" | "hard" — what apply_decision() will consume
    retry_count: int = 0               # count AFTER the pending consumption (for prompt rendering :1675-1683)
    retry_limit: int = 0               # 1 / 2 / 8 / 0 per (classification, ctx.source) — encodes D2 exactly
    record_failed_attempt: bool = False  # D5: True for POST_EDIT/VERIFICATION_RESULT still-blocked and BUDGET_EXHAUSTION
    accepted_after_warning_limit: bool = False  # D4 wording preserved
    restore_baseline: bool = False     # runner performs _restore_queue_assignment_to_baseline_sorry (:5919)
```

The limit table inside `decide()` is data, not branches: `{(HARD_BLOCKER, FINAL_REPORT): 2, (HARD_BLOCKER, POST_EDIT): 8, (HARD_BLOCKER, VERIFICATION_RESULT): 0, (WARNING_ONCE, *): 1, (HARD_BLOCKER, BUDGET_EXHAUSTION): restore-now}` — byte-preserving D2/D3 semantics first; **harmonization is a later, owner-approved change**, not Phase 0.

**Call-site wiring** (all inside `native_runner.py`, keeping function names/testing seams):
- `_review_agent_final_report`: build `DecisionContext(FINAL_REPORT, check=_manager_check_for_feedback_kind(...), signature=..., cleanup_reason=..., axiom_blockers=..., claims_success=...)`; replace the open-coded block `:1953-2073` with `decide` → `apply_decision` → existing message/print/activity rendering keyed off `Decision` fields.
- `_finish_queue_step_boundary`: replace `:3103-3227` classification/retry blocks; `record_failed_attempt` drives the `:3208` call; `restore_baseline` drives `:3183`.
- `_same_queue_assignment_still_blocked`: becomes a thin builder that constructs the `LIVE_STATE` synthetic `ManagerCheck` and returns `decide(ctx).action == "continue_same_theorem"` (predicate-only; `apply_decision` is not called — C never mutated counters, preserved).
- `_handle_api_step_budget_exhaustion`: uses `DecisionSource.BUDGET_EXHAUSTION`; Phase 1 flips its action to `budget_breakpoint` under the flag (§P1.4).

## P0.3 Live-authority change (`TheoremQueueManager` as the single instance)

**Today**: every helper does reconstruct → mutate → `to_autonomy_state()` render → `_flush_queue_manager` (19 sites, §0.1). Note: despite the docstrings (`queue_manager.py:5-8`), `autonomy_state` is **not persisted to disk** — it is created fresh in `main()` (`native_runner.py:9315`) and dies with the process; checkpoints persist only the summary entry (`_write_workflow_checkpoint` `:7671-7695`). The legacy-dict round-trip is purely an in-memory interchange format, which makes a live instance low-risk.

**Where the instance lives**: neither module-level (leaks across tests; violates "no module-level mutable state", `queue_models.py:5-9`) nor on the agent (helpers like `_scoped_failed_attempt_entries` `:903` receive only `autonomy_state`). **Decision: a keyed sidecar in a new leaf module**, so the dict stays JSON-clean:

**(a) Files**: new `leanflow_cli/workflows/queue_manager_live.py` (leaf; imports only `queue_manager`); modify `_queue_manager_from_state`/`_flush_queue_manager` in `native_runner.py` (bodies only — names stay, tests' `setattr` monkeypatching unaffected per §0.2).

**(b) Signatures**:

```python
# queue_manager_live.py
def live_queue_manager(autonomy_state: MutableMapping[str, Any],
                       live_state: Mapping[str, Any] | None = None) -> TheoremQueueManager:
    """Return the single live manager for this autonomy_state: get-or-create keyed by id(autonomy_state)
    in a WeakSet-guarded registry; on create, hydrate via from_autonomy_state; when live_state is given,
    apply set_active_file + replace_queue exactly as :874-882 does today."""

def flush_live_queue_manager(autonomy_state: MutableMapping[str, Any], mgr: TheoremQueueManager) -> None:
    """Render mgr into the legacy OWNED_AUTONOMY_KEYS dict shape (compat serialization for every reader
    of autonomy_state['current_queue_assignment'] etc. — e.g. :1900, :5819, :6024) and run invariants
    when LEANFLOW_QUEUE_INVARIANT_CHECKS=1 (:4390)."""

def invalidate_live_queue_manager(autonomy_state: MutableMapping[str, Any]) -> None:
    """Drop the cached instance (used when a test or resume path replaces the dict contents wholesale)."""
```

Registry: `weakref`-free dicts can't weak-ref `dict`s by identity safely across replacement, so the registry is `{id(dict): (mgr, fingerprint)}` where `fingerprint` is a cheap hash of the dict's OWNED keys; on lookup, if the fingerprint diverged from the last flush (someone mutated the legacy keys directly — e.g. `autonomy_state.pop("current_queue_assignment")` `:4328`), rebuild from the dict. This makes external mutation safe during migration instead of a correctness cliff.

**Lifecycle**: created lazily at first gate/prepare call of a run; survives all cycles (the dict object is stable through `main()`); resume is unchanged because resume never deserializes autonomy_state (see above) — `from_autonomy_state` remains the hydration path for the swarm/manual constructions and for any future typed persistence. **Compat serialization**: `flush` keeps writing the exact legacy dict shape after every mutation — Phase 0 changes *when reconstruction happens*, never the dict format. Delete-the-flush is explicitly out of scope until shadow-compare (P0.4) has been green.

## P0.4 Shadow-compare

- **Flag**: `LEANFLOW_QUEUE_DECIDE_SHADOW=1` (read via `_read_text_env`, `leanflow_cli/native/native_config.py:30`; default off ⇒ hot path byte-identical).
- **Mechanism**: because `decide()` is pure (P0.2), shadow is safe: at each of the four call sites, while the *legacy open-coded branch remains authoritative*, also compute `shadow = mgr_snapshot.decide(ctx)` on `peek`-style copies (reuse `peek_assignment`'s copy discipline, `queue_manager.py:219-247`) and compare `(action, feedback_kind, retry_limit, record_failed_attempt, restore_baseline)` against the legacy outcome reified into the same tuple.
- **Mismatch logging**: `_record_activity("queue-decide-shadow-mismatch", …, source=ctx.source, legacy=…, decided=…, target_symbol=…, active_file=…, signature=…)` — lands in `activity/agents/*.jsonl` + run stream via `append_workflow_activity` (`workflow_state.py:309`), greppable post-hoc.
- **Rollout / removal criteria**: (1) full pytest suite green with shadow on; (2) `testdata/workflow_projects/ProveDemo` end-to-end prove runs (the repo's demo project; see modified `ProveDemo/IMOMath2.lean`/`IMOMath3.lean` on this branch) produce **zero** mismatch events; (3) one real multi-theorem `/prove` run zero mismatches. Then flip `LEANFLOW_QUEUE_DECIDE_AUTHORITY=1` (decide() authoritative, legacy computed as the shadow) for one release; then delete the open-coded branches and both flags.

## P0.5 Test plan

**Existing pins (must stay green, unmodified):**
- `tests/leanflow/test_queue_manager.py` — all 12 (`:18` select-next, `:32` classify priority, `:47` decide warning-once, `:63` signature idempotency, `:79` assign clears counters, `:119` invariants, `:134` attempt pruning, `:148` typed run state, `:174` legacy round-trip, `:211` outcome verification, `:233` pending count, `:247` peek purity). `:47`'s direct `decide(ManagerCheck)` calls are updated to `decide(DecisionContext(source=FINAL_REPORT, check=…)) + apply_decision` — the only intentional test change.
- `tests/leanflow/test_native_runner.py` verdict pins: `test_manager_feedback_kind_treats_nonzero_verification_as_error` `:1593`, `..._adapter_golden_cases` `:1609`, `..._detects_assigned_sorry` `:1681`; the six `test_review_agent_final_report_*` `:1944,:1987,:2028,:2096,:2160,:2201,:2242,:2298,:2357`; `test_handle_managed_tool_result_*` `:404,:462,:588,:790`; `test_same_queue_assignment_still_blocked_requires_same_theorem_and_real_blocker` `:8508`; `test_handle_api_step_budget_exhaustion_records_attempt_and_restores_sorry` `:8577`.

**Gap found (verified)**: there are **no direct tests of `_finish_queue_step_boundary`** (grep over `tests/` returns none; it is only exercised through `_handle_managed_tool_result`). Per the house rule, write characterization tests FIRST:
- New `tests/leanflow/test_queue_step_boundary_golden.py`: drive `_finish_queue_step_boundary` directly (agent stub pattern from `_ManagedRunAgentStub`, `test_native_runner.py:8615`) across the grid {cleanup-only, hard error post-edit ×(count<8, count=8), hard error via lean_verify (assert **no** retry consumed — pins D2), warning ×(fresh, spent), future-only}; assert activity event type, appendix vs interrupt, `_managed_step_boundary_recorded_attempt`.
- New `tests/leanflow/test_queue_decide_unification.py`: golden table — for every `DecisionContext` in the grid, assert `decide()` output tuple equals the legacy outcome captured by the characterization tests (the table is the drift matrix D1–D9 made executable).
- Shadow tests: force a synthetic mismatch (monkeypatch legacy branch) and assert exactly one `queue-decide-shadow-mismatch` event; assert zero events on the golden grid.
- Live-authority tests: `tests/leanflow/test_queue_manager_live.py` — get-or-create identity across calls; external-mutation fingerprint rebuild; flush produces byte-identical `to_autonomy_state()` output vs today (reuse round-trip assertions from `test_queue_manager.py:174`).

## P0.6 Acceptance criteria

1. Flags off: `pytest -q` green; `_review_agent_final_report`/`_finish_queue_step_boundary`/`_same_queue_assignment_still_blocked`/`_handle_api_step_budget_exhaustion` behavior byte-identical (characterization suite proves it).
2. Shadow on: zero mismatches over the P0.4 corpus; mismatch events render both verdicts with full context.
3. Reconstruction count at steady state: 19 → ≤2 hydrations per run (initial + any fingerprint rebuild), observable via a debug counter under `LEANFLOW_QUEUE_INVARIANT_CHECKS=1`.
4. `decide()` has no I/O and no mutation (enforced by a test calling it twice and asserting counter equality).
5. Kernel gate `:1210-1216` untouched (diff-level assertion in review).

## P0.7 Risks (specific)

- **Shadow double-mutation** — mitigated structurally by the pure/commit split; a regression test pins it.
- **`setattr` monkeypatch bypass** (§0.2) — no verdict function leaves `native_runner.py`; the new leaf modules are only *called* by them.
- **Path C false-negative drift**: C's evidence is live_state (possibly stale post-edit). Unification must reproduce its synthetic `file_check_ok: False` exactly (`:5841-5848`) — encoded as `DecisionSource.LIVE_STATE` in the golden table, not "fixed".
- **D2 harmonization temptation**: 8-vs-2-vs-0 hard-retry limits look like a bug but are load-bearing pacing (post-edit retries are cheap inner-loop; final-report retries are full turns). Phase 0 preserves; changing is a flagged follow-up.
- **Boundary interrupt ordering**: `_request_step_boundary_interrupt` (`:3413`) must remain the last side effect on yield; the decision-rendering refactor keeps the `finally:` structure (`:3230-3413`).

---

# PHASE 1 — Plan-state + dependency graph + budget breakpoint + checkpoint retirement

## P1.1 Plan-state leaf module (`leanflow_cli/workflows/plan_state.py`)

**Reuse-first**: read/write via `read_json_file` (`workflow_json_io.py:20`) + `atomic_json_write` (`core/utils.py:13` — §0.3 correction 4); location via `workflow_state_root()` (`workflow_state_paths.py:47`) next to `live_status.json` (`workflow_state.py:98-99`); activity via `append_workflow_activity` (`:309`); genuinely new: the schemas, renderer, reconciler, and the queue→graph sync adapter.

**Naming hazard (verified)**: "blueprint" already means the document-formalization planner artifact `Blueprint.md` / `LEANFLOW_FORMALIZATION_BLUEPRINT` (`native_runner.py:8764`, org-pass prompt `:8759-8798`). Keep the roadmap's `blueprint.json` filename (it lives in `.leanflow/workflow-state/`, not the project root) but the module docstring and all prompts must say "dependency graph (`blueprint.json`)" and never bare "blueprint" to avoid model confusion.

**(a) Files**: create `leanflow_cli/workflows/plan_state.py` (+ `tests/leanflow/test_plan_state.py`); modify `native_runner.py` (sync + prompt hooks, §P1.3/P1.5), `leanflow_cli/workflow.py` (env, §P1.3), `lean_worker_dispatch.py` (§P1.3), `ARCHITECTURE.md` (module map — required by `AGENTS.md:57-58`).

**(b) Signatures** (all in `plan_state.py`; pure except the explicitly-I/O ones):

```python
def plan_state_enabled() -> bool:
    """True iff LEANFLOW_PLAN_STATE is truthy; everything in this module no-ops when off."""

@dataclass(frozen=True)
class PlanStatePaths:  # plan_md / summary_json / blueprint_json : Path
def plan_state_paths() -> PlanStatePaths:
    """Resolve the three artifact paths under workflow_state_root() (workflow_state_paths.py:47)."""

@dataclass(frozen=True)
class GraphNode:  ...   # schema below; from_mapping/to_mapping mirroring queue_models.QueueItem (:76-99)
@dataclass(frozen=True)
class GraphEdge:  ...
@dataclass(frozen=True)
class Blueprint:  # goal, nodes, edges, revision, updated_at
    def frontier(self) -> tuple[GraphNode, ...]:
        """stated nodes whose depends_on targets are all proved (roadmap §4.1 decision-use (a))."""
    def invalidate_false_subtree(self, node_id: str) -> Blueprint:
        """Mark node false and poison split_of ancestors back to conjectured (roadmap §4.1 (b))."""

def load_blueprint() -> Blueprint:            # tolerant read (read_json_file), empty graph on missing
def save_blueprint(bp: Blueprint) -> None:    # atomic_json_write; bumps revision; refuses stale-revision write
def load_summary() -> dict[str, Any]:
def save_summary(payload: Mapping[str, Any]) -> None:

def upsert_node_for_assignment(bp: Blueprint, *, target_symbol: str, active_file: str,
                               statement: str) -> tuple[Blueprint, GraphNode]:
    """Get-or-create the graph node for a queue assignment (id 'n<sha1(file::symbol)[:8]>' — reuses
    TheoremKey.storage_key() identity, queue_models.py:69-72); set status=proving, owner=run id."""

def record_decision_packet(packet: Mapping[str, Any]) -> None:
    """Append to summary.json['decision_packets'] and cross-link packet_id onto the node (N1 artifact)."""

def render_plan_md(bp: Blueprint, summary: Mapping[str, Any]) -> str:
    """One-way render (JSON is authority): fixed sections Goal / Current state / Frontier / Grounding /
    Decision log / Dead ends & proven false / Final report + a preserved free-form '## Notes' tail."""
def save_plan_md(bp: Blueprint, summary: Mapping[str, Any]) -> None:

def artifact_context_block() -> str:
    """The single injection string every prompt surface uses (§P1.3): absolute artifact paths + a
    <=10-line frontier/status digest; '' when plan_state_enabled() is False."""

def write_final_report(status: str, *, detail: Mapping[str, Any]) -> None:
    """N1 concrete-result guarantee: status ∈ proved|disproved|documented; persists summary['final_report']
    and renders the plan.md Final report section. Called from every stop path (§P1.5)."""
```

**(c) Schemas**:

`blueprint.json` (roadmap §4.1, plus versioning + N1 links):
```jsonc
{ "version": 1, "revision": 17, "updated_at": "<iso>",
  "goal": "main theorem statement / research objective",
  "nodes": [{ "id": "n3f9a2c1d", "kind": "theorem|lemma|def|conjecture",
    "name": "Namespace.decl_name", "file": "ProveDemo/IMOMath2.lean",
    "statement": "…", "status": "conjectured|stated|proving|proved|blocked|false|split|parked",
    "attempts": 3, "api_steps": 412, "owner": "run-id|null",
    "notes": "", "decision_packets": ["bp-1751470000000"] }],
  "edges": [{ "from": "n3f9a2c1d", "to": "n17", "kind": "depends_on|split_of|evidence" }] }
```
Status transitions are code-enforced: `proved` is writable **only** by the gate-accept sync path (§P1.2 kernel-truth rule); `false` only by a kernel-proved negation (Phase 3 — the enum ships now so the graph schema doesn't churn).

`summary.json`:
```jsonc
{ "version": 1, "updated_at": "<iso>", "goal": "…",
  "workflow_kind": "prove", "workflow_command": "…",
  "counters": {"proved": 4, "stated": 6, "proving": 1, "blocked": 1, "false": 0, "parked": 0},
  "decision_packets": [ { …see §P1.4… } ],
  "manager_nudges": [],        // reserved: Phase 2 dark-launch target (roadmap Phase 2)
  "dispatch_ledger": [],       // reserved: Phase 3 (roadmap §4.2)
  "final_report": { "status": "proved|disproved|documented|in-progress",
                    "summary": "…", "evidence": ["blueprint.json#n…", "packet:bp-…", "activity:run-…"] } }
```
`final_report` is the N1 contract: `documented` is the *worst allowed* terminal state — a stop with neither `proved` nor `disproved` must carry the packets/graph/notes that constitute the rigorous account; the runner's stop paths enforce writing it (§P1.5), so silent give-up is structurally impossible when the flag is on.

`plan.md`: human render; regenerated sections marked `<!-- generated: do not edit above the Notes section -->`; `## Notes` preserved verbatim across writes. Strategy exposes the campaign's current orchestrator route and Decision log includes recent route events read from a bounded journal tail. Notes remain historical user context: current queue inventory, Lean source/diagnostics, and the kernel gate outrank copied sorry counts or declaration bodies there.

**Concurrency/atomicity**: Phase 1 invariant — **single writer = the native runner process**; children (Phase 3+) read-only. Writes are `atomic_json_write` (crash-safe); the `revision` check turns any accidental second writer into a loud activity event instead of a lost update. (Multi-process locking exists if later needed: the flock pattern of `_locked_append`, `workflow_state.py:54-66`.)

**(d) Flags**: `LEANFLOW_PLAN_STATE=1` (default off; dark-launch then default-on for `/prove`), optional `LEANFLOW_PLAN_STATE_DIR` override (test convenience; defaults to `workflow_state_root()`).

## P1.2 Reconciliation (`reconcile`)

**Truth sources (exact, existing)**:
- Per-declaration presence + `sorry`: `_declaration_line_index_from_text(content)` (`lean_parsing.py:279`) — each entry gets `name/kind/line/end_line/text/has_sorry` (`has_sorry` at `:315`, comment/string-stripped). File-path front-end: `proof_state_builder._find_declaration_entry(active_file, label)` (`proof_state_builder.py:58`).
- Blocker queue + counts: `lean_inspect(...)` → `LeanInspection` (`lean_services.py:910`, dataclass `lean_models.py:48`): `queue_items[{label,kind,line,end_line,reasons,…}]` (built `:933-955`), `sorry_count`, `project_sorry_count`, `diagnostics`.
- File/project counts (no LSP needed): `native_lean_files._count_sorries(active_file)` (`:116`), `_count_project_sorries(project_root)` (`:144`).
- Kernel-proved authority: **only gate acceptance** — Path A accept (`manager_final_report_review.ok`, `native_runner.py:2007`) or boundary verified yield (`:3402-3407`), backed by the `last_verification` record (`_verification_record_from_check` `:1219`, `VerificationRecord` `queue_models.py:169-189`).

**(b) Signatures**:

```python
# plan_state.py (pure)
@dataclass(frozen=True)
class DeclTruth:
    present: bool; has_sorry: bool; has_error_diag: bool

def reconcile(bp: Blueprint, truth: Mapping[tuple[str, str], DeclTruth]) -> tuple[Blueprint, list[dict]]:
    """Anti-drift pass (roadmap §4.1 'Reconciliation'): downgrade proved→stated when the declaration
    reappears with sorry/errors or vanishes; promote conjectured→stated when a named stub now exists
    on disk; NEVER promote to proved (kernel-gate-only). Returns (new bp, change events)."""

# native_runner.py (thin I/O adapter, stays in the runner per §0.2)
def _collect_declaration_truth(files: Sequence[str]) -> dict[tuple[str, str], DeclTruth]:
    """Build DeclTruth for every graph-referenced file from _declaration_line_index_from_text +
    diagnostic_items over the live_state diagnostics already fetched this cycle (:6127-6131) —
    zero extra Lean processes on the happy path."""
```

**Where it runs**: once per autonomous cycle, immediately after the loop's live-state refresh (`:9094-9095`) and before `_autonomous_stop_reason` (`:9161`) — the same cadence as `_persist_live_status(phase="verifying")` (`:9158`). Change events append `plan-graph-reconcile` activity entries.

## P1.3 Artifact awareness — injection points (every deployed agent knows the artifacts)

Today-equivalents (verified):
1. **Runner child env**: `resolve_workflow_request` sets `LEANFLOW_NATIVE_*`/`LEANFLOW_PROJECT_ROOT` (`workflow.py:476-499`; spawn at `:564`). **Add** `LEANFLOW_PLAN_MD`, `LEANFLOW_BLUEPRINT_JSON`, `LEANFLOW_PLAN_SUMMARY_JSON` (absolute paths — resolvable because `LEANFLOW_PROJECT_ROOT` is fixed at `:480`). Env-contract rule §0.2 honored (`LEANFLOW_`-prefixed).
2. **Startup prompt**: `_startup_user_message` (`native_runner.py:8541`) — insert a `plan_block = plan_state.artifact_context_block()` next to `queue_block` (`:8579-8593`) in every return branch (`:8613-8621`).
3. **Continuation prompt**: `_autonomous_continuation_prompt` (`:8927`) — static artifact paths go in the byte-stable prefix; the volatile frontier digest goes after the cycle marker (respecting the RCP prefix-cache design, comment `:8938-8941` and marker `:9053-9055`).
4. **System/prompt authority**: `_managed_system_prompt` and the injected artifact block identify the living artifacts. The generated graph statuses are authoritative, but stored declaration bodies and the verbatim Notes tail are snapshots; current queue assignment plus Lean source/diagnostics and the kernel gate outrank them. Research orchestrator calls receive the bounded generated-only view. Model-facing `read_file(plan.md)` calls receive an 8,000-character, current-state-first projection of the generated prefix with source hash/count omission telemetry; the canonical `## Notes` heading and historical Notes body remain hidden and non-first-page pagination is rejected, so a foreground prover cannot re-ingest stale history or reasonably create a duplicate heading. Raw model-facing `summary.json` and `blueprint.json` reads are also rejected because their machine ledgers can be very large and stale; bounded graph/finding digests are supplied instead. `LEANFLOW_DIAGNOSTIC_FILE_ACCESS=1` remains an operator-only raw-inspection escape hatch.
5. **Dispatched workers**: `_worker_prompt` (`lean_worker_dispatch.py:19-44`) — append a `Plan artifacts:` section from `artifact_context_block()` (module already imports `workflow_state`; adding the `plan_state` import keeps it a leaf).
6. **`delegate_task` children**: children get context only through `_build_child_system_prompt(goal, context)` (`delegate_tool.py:197`) — `tools/` must not import `leanflow_cli/` (layering §0.2), so the injection is **caller-side**: every LeanFlow call site that builds a `context` (today `dispatch_worker` `lean_worker_dispatch.py:87-95`) includes the artifact block. Children spawned in-process also inherit `os.environ`, so the env vars from (1) are a second, prompt-independent discovery channel.

## P1.4 Budget breakpoint (mechanical)

**Where attempts×steps are tracked today (verified)**: per-theorem failed attempts in `TheoremQueueManager._attempts` (count `attempt_count_for` `queue_manager.py:335-340`; ring-capped at 10, `:352-364` — therefore **unusable as a cumulative budget**); per-turn steps only in `result["api_calls"]` vs `agent.max_iterations` (`native_runner.py:6027-6034`); nothing cumulative per theorem across turns. Genuinely new: cumulative accounting.

**(a) Files**: modify `queue_manager.py`/`queue_models.py` (accounting), `native_runner.py` (`_handle_api_step_budget_exhaustion`, `_autonomous_stop_reason`, loop wiring), `plan_state.py` (packet persistence).

**(b) Signatures**:

```python
# queue_manager.py — cumulative, NOT ring-pruned; new autonomy key "theorem_api_steps" added to
# OWNED_AUTONOMY_KEYS (:97-108) and (de)serialization (:681/:820)
def add_api_steps_for(self, key: TheoremKey, steps: int) -> int:
    """Accumulate spent API steps for a theorem across turns; returns the new total."""
def api_steps_for(self, key: TheoremKey) -> int: ...

# native_runner.py
def _budget_breakpoint_enabled() -> bool: ...          # LEANFLOW_BUDGET_BREAKPOINT
def _theorem_budget_steps() -> int: ...                 # LEANFLOW_THEOREM_BUDGET_STEPS, default 600
def _queue_breakpoint_consecutive() -> int: ...         # LEANFLOW_QUEUE_BREAKPOINT_CONSECUTIVE, default 3

def _maybe_trigger_budget_breakpoint(agent, result, autonomy_state, live_state, *, cycle, phase) -> bool:
    """After the legacy exhaustion handling ran: accumulate steps; if per-theorem total ≥ budget or the
    consecutive-exhausted-assignments streak ≥ K, persist a decision packet (plan_state), mark the graph
    node blocked, set autonomy_state['budget_breakpoint'], and return True."""
```

**Wiring** (all seams verified):
- Step accounting: at each of the three post-turn sites (`:9240`, `:9394`, `:8335`) add `result["api_calls"]` to the current assignment's total (also on gate `manager_retry_exhausted` exits, Path A `:2005`).
- `_handle_api_step_budget_exhaustion` (`:6008`) keeps its full legacy behavior (attempt record `:6046`, baseline restore `:6047`, handoff message `:6053`) — the breakpoint is **additive after it**, so flag-off is byte-identical (roadmap Phase-1 "clean stop" semantics; regression risk minimized).
- Stop: new first-priority branch in `_autonomous_stop_reason` (`:8801`): `if autonomy_state.get("budget_breakpoint"): return "budget-breakpoint"`. The loop already returns and persists on any non-`continue` reason (`:9161-9171`, `_persist_live_status(phase=stop_reason)`; `_workflow_phase` passes explicit phases through, `:590-597`). N1: this stop path calls `plan_state.write_final_report("documented", detail={packet…})`.
- Queue-level rule: `autonomy_state["consecutive_exhausted_assignments"]` increments whenever an assignment terminates via exhaustion (budget D, gate hard-retry `:1988`, boundary hard-retry `:3191`) without a gate-accept since the last increment; resets on accept (`:2007`, `:3402`). At K ⇒ breakpoint with `"scope": "queue"` — the owner's "interrupt the whole queue".

**(c) Decision-packet schema** (the N1 concrete-result artifact; persisted via `plan_state.record_decision_packet`):
```jsonc
{ "packet_id": "bp-<epoch-ms>", "created_at": "<iso>", "scope": "theorem|queue",
  "node_id": "n3f9a2c1d", "target_symbol": "…", "active_file": "…",
  "statement": "<assignment slice (queue_models.QueueAssignment.slice, :150-152)>",
  "attempts": [ /* mgr.attempt_entries_for(key) mappings (queue_manager.py:332) */ ],
  "api_steps_used": 640, "budget": 600, "consecutive_exhausted": 3,
  "error_signatures": [ /* retry signatures for the key (idempotency store :425-443) */ ],
  "search_history": [ /* agent search_progress tracker snapshot (_track_search_progress :2412) if present */ ],
  "last_verification": { /* verification_to_mapping (queue_models.py:222) */ },
  "negation_status": "not-attempted",     // Phase 3 fills; Phase 4 orchestrator consumes
  "options": ["split","plan","negate","park","re-state","abort"],
  "decision": null, "decided_by": null }
```

**(d) Flags**: `LEANFLOW_BUDGET_BREAKPOINT`, `LEANFLOW_THEOREM_BUDGET_STEPS`, `LEANFLOW_QUEUE_BREAKPOINT_CONSECUTIVE`. Per N5: no efficiency ceiling — `LEANFLOW_THEOREM_BUDGET_STEPS=0` means "no per-theorem cap" (queue-level K still applies), for research runs.

## P1.5 Checkpoint retirement

**Removed (UX only)**:
- Interactive handlers `/checkpoint` (`:9602-9622`), `/resume-plan` (`:9623-9641`), `/rollback` (`:9642-9682`); help lines `:3788-3790`; command-list mention `:7899`. `/history` **stays** (read-only logging surface, `:9599-9601` + `_print_history` `:8457`).
- `native_checkpoints._resume_plan_from_checkpoint` (`:200`) and `_rollback_to_checkpoint` (`:205`) become dead after handler removal — delete in the same change (they are re-exported at `native_runner.py:252-253`; remove those re-exports and the `__all__` entries `native_checkpoints.py:58-59`).

**Kept (silent safety + logging — owner decision #6)**:
- Baseline-`sorry` restore `_restore_queue_assignment_to_baseline_sorry` (`:5919`; call sites `:1982,:3183,:6047`) — untouched.
- Git-shadow filesystem snapshots: `CheckpointManager` (`checkpoint_manager.py:182`, owned `run_agent.py:774`) and `_latest_filesystem_checkpoint_hash` (`native_checkpoints.py:231`) — untouched (disaster-recovery restore remains possible via the shadow repo manually).
- Checkpoint **writers**: `_write_workflow_checkpoint` (`:7631`), milestone (`:8668`), pre-compaction (`:8692`) — kept as logging + compaction anchors (`_maybe_checkpoint_before_compaction` protects against lossy compaction, `:9173`/`:9688`).
- `leanflow_specs/workflows/checkpoint.md` deletion is **Phase 6** (owner decision #5/#8), not Phase 1; likewise `WORKFLOW_TASK_LABELS["checkpoint"]` (`workflow_state.py:76`) and the `/checkpoint`-as-label lines in `_managed_system_prompt` (`:8637,:8647`) stay for now.

**New resume path** (replaces checkpoint prose as the authority — roadmap §1 "documentation-driven"):
- Today: `main()` seeds `history = _checkpoint_replay_history(resumed_checkpoint)` (`:9317-9319`) and `_startup_user_message` prepends "Resume … from persisted {label}" (`:8609-8616`).
- New: when `plan_state_enabled()` and `blueprint.json` exists — (1) `_collect_declaration_truth` + `plan_state.reconcile` run *before* the startup prompt; (2) a new `plan_state.resume_context_block() -> str` ("Return the `[LEANFLOW PLAN-STATE RESUME]` block: goal, counters, frontier, open decision packets, dead ends") replaces the checkpoint-summary replay as the startup handoff; (3) `_checkpoint_replay_history` remains the **fallback** when plan-state is absent (compat with existing runs). `current.json`/`index.json` journals unchanged.

## P1.6 Test plan

- **New `tests/leanflow/test_plan_state.py`**: schema round-trips (style: `test_queue_manager.py:174` legacy round-trip); `frontier()`/`invalidate_false_subtree()` graph ops; `render_plan_md` golden with Notes-preservation; `reconcile` cases — proved→stated downgrade on reintroduced `sorry`, conjectured→stated promotion, **no** promotion-to-proved (fixture `.lean` text through `_declaration_line_index_from_text`, pattern: `tests/leanflow/test_lean_parsing.py`); revision-conflict refusal; atomicity delegated to existing `tests/test_atomic_json_write.py` coverage.
- **`tests/leanflow/test_native_runner.py` additions**: breakpoint — extend the `test_handle_api_step_budget_exhaustion_records_attempt_and_restores_sorry` pattern (`:8577`) with the flag on: assert legacy effects unchanged **plus** packet persisted, node `blocked`, `_autonomous_stop_reason(...) == "budget-breakpoint"` (stop-reason test pattern: `:9553,:9581`); queue-level K-streak test; flag-off byte-identity re-runs the existing test unmodified. Startup/continuation plan-block injection (pattern: `test_startup_prompt_surfaces_final_sweep_warning_cleanup_on_resume` `:7173`); RCP prefix stability of the artifact block (existing prefix-cache tests around `_autonomous_continuation_prompt`). Checkpoint retirement: existing writer pins stay green (`test_write_workflow_checkpoint_persists_index_and_current` `:7599`, `:7628,:7663,:7697`); new tests assert the three commands are gone from the interactive loop and that resume prefers plan-state with checkpoint fallback.
- **`tests/leanflow/test_workflow_state_concurrency.py`**: extend with a plan-state revision-conflict test (two writers, one loses loudly).

## P1.7 Acceptance criteria

1. `LEANFLOW_PLAN_STATE=0` and `LEANFLOW_BUDGET_BREAKPOINT=0`: zero behavioral diff (full suite + ProveDemo run byte-comparison of activity streams modulo timestamps).
2. Flags on, ProveDemo `/prove` run: `blueprint.json`/`summary.json`/`plan.md` exist, every gate-accepted theorem is `proved` in the graph, counters match `theorem_outcomes`, and killing the process mid-write never yields truncated JSON (atomic writer).
3. Reconcile: manually reinserting a `sorry` into a proved declaration downgrades the node within one cycle with a `plan-graph-reconcile` event; no node ever reaches `proved` without a gate-accept event (kernel-truth invariant, testable).
4. Breakpoint: a theorem exceeding `LEANFLOW_THEOREM_BUDGET_STEPS` stops the run with `phase="budget-breakpoint"` in `live_status.json`, a complete decision packet, node `blocked`, and `final_report.status="documented"` (N1); K consecutive exhaustions interrupts the whole queue.
5. Retirement: `/checkpoint`/`/rollback`/`/resume-plan` rejected as unknown; `/history` works; resume of a plan-state run reconstructs context from `summary.json`+graph (reconciled); resume of a pre-Phase-1 run still works via checkpoint replay.
6. Every spawned prompt surface (startup, continuation, system, worker, delegate context) contains the artifact paths when the flag is on (asserted per §P1.3 site).

## P1.8 Risks (specific)

- **"blueprint" collision** with formalization `Blueprint.md` (`:8764`) — mitigated by phrasing (§P1.1) and disjoint directories; a formalize-workflow test asserts the org-pass prompt is unchanged.
- **Prompt-token growth vs RCP cache**: the artifact block must be byte-stable across cycles (paths only) with the volatile digest after the cycle marker (`:9053-9055`) — else the C3 prefix-cache optimization silently dies. Pinned by test.
- **Graph staleness / split-brain with `theorem_outcomes`**: two progress stores (mgr `_outcomes` `queue_manager.py:127` vs graph). Mitigation: graph sync is derived *from* queue events + reconcile every cycle; kernel wins; counters cross-checked in tests. The graph never feeds verdicts in Phase 1 (read-only for decisions until Phase 4).
- **Write amplification**: 3 files per cycle; mitigated by revision-diff short-circuit (skip write when nothing changed) — matters for the N5 "efficiency floor on easy runs".
- **Budget accounting drift**: `api_calls` can be attributed to the wrong theorem across a transition (`_rebuild_history_for_theorem_transition` `:5086` runs before the turn) — accumulate at turn end against the assignment captured at turn start (mirrors the D-path snapshot-race fix, `queue_manager.py:287-294`).
- **Removing `/rollback` removes the only user-facing file-restore** — accepted per owner decision #6 (git-shadow still exists, restorable manually); release notes must say so.
- **Monkeypatch coupling** (§0.2): all new runner-side hooks are new functions *added* to `native_runner.py` that delegate to `plan_state`; no existing patched helper moves.

## Sequencing within scope

P0.5 characterization tests → P0.2 pure `decide()`/`DecisionContext` → P0.3 live authority → P0.4 shadow (release gate) — then P1.1 module + P1.3 injection (dark) → P1.2 reconcile → P1.4 breakpoint → P1.5 retirement (last: it depends on the new resume path). Each step is independently shippable, flag-gated, and hot-path byte-identical when off — matching roadmap §5 Phase 0 (~1 wk, Low-Med) and Phase 1 (~2 wk, Low).
---

# PART II — Phases 2–3 (struggle detector + LLM-manager; dispatch service + lineage + negation probe; N1 mechanics)

All grounding verified. Here is the implementation-ready spec.

---

# SPEC — /prove Redesign: Phase 2 (Struggle Detector + LLM-Manager), Phase 3 (Dispatch + Lineage + Negation Probe), N1 Concrete-Result Mechanics

Repo: `/Users/lmilikic/Desktop/LeanFlow`, branch `docs/prove-redesign-roadmap`. All `file:line` references verified against this tree on 2026-07-02.

## 0. Verified seam map (with corrections to the roadmap)

| Seam | Verified location | Roadmap drift |
|---|---|---|
| Eternal loop `_drive_autonomous_followups` | `leanflow_cli/native/native_runner.py:9059`; startup call `:9411`; cycle-top state rebuild `:9094`; stop-reason exit `:9161-9171`; cycle-ceiling exit `:9078-9092` | ok |
| Manager gate `_review_agent_final_report` | `native_runner.py:1890`; verdict finalized at `:2007` (`ok = bool(manager_check.get("ok"))`); reject-and-retry branch `:2052-2073`; retry-exhausted baseline restore `:1982` | ok |
| Deterministic checker `_manager_check_queue_item` | `native_runner.py:1210` (incremental → `lean_verify` fallback) | ok |
| Stall/stop `_autonomous_stop_reason` | **`native_runner.py:8801`** (roadmap cites `:8921`, which is the `stable_cycles >= _autonomous_stalled_limit()` check *inside* it). Signature tuple `:8876-8883`; `continuation_stable_cycles` `:8885-8891`; blocked-runs counter `:8913-8919` | corrected |
| Budget exhaustion `_handle_api_step_budget_exhaustion` | `native_runner.py:6008` | roadmap gave no line |
| Failed-attempt ring buffer | `queue_manager.py:284` (`record_attempt`), `:304` (`record_attempt_for`), `:329` (`attempts_for`), `:335` (`attempt_count_for`), `:342` (`attempts_for_current`), **`:352` is `_prune_attempts`** (keeps `DEFAULT_FAILED_ATTEMPT_HISTORY = 10` per key, `queue_models.py:24`) | roadmap's ":352 = ring buffer API" is the pruner; API is `:284-:345` |
| Retry idempotency signatures | `queue_manager.py:126` (`_retry_signatures`), `:425` (`consume_retry_once_for`, keeps last 20 signatures per `(key, bucket)`), `:445` (`consume_retry_once`); runner-side signature basis `_manager_feedback_retry_signature` `native_runner.py:1752` (kind + local_cleanup_reason + output + target) | ok |
| Search-progress tracker | `native_runner.py:2412` (`_track_search_progress`); fields `:2435-2463`: `target_symbol, active_file, search_count, same_query_streak, last_query, last_query_display, unique_queries[-8:], used_tools, last_result_count, last_nudged_search_count`; thresholds `SEARCH_PROGRESS_REPEAT_NUDGE_LIMIT = 2` `:111`, `SEARCH_PROGRESS_TOTAL_NUDGE_LIMIT = 6` `:112`; reset on real-progress tools `_note_non_search_tool_progress` `:2335` | ok |
| Budget warnings | `run_agent.py:2838` (`_get_budget_warning`), thresholds `_budget_caution_threshold = 0.7` `:511`, `_budget_warning_threshold = 0.9` `:512`; injection `_maybe_append_budget_warning_message` `:2867` | ok |
| Give-up phrasing | `_extract_blocker_summary` **`leanflow_cli/native/native_utils.py:183`** (imported by native_runner `:297`); `_normalize_blocker_summary` `:167` | roadmap didn't name the module |
| LLM review call | `run_model_verification_review` `leanflow_cli/workflows/verification_providers.py:160`; provider resolution `:70`; underlying `call_llm` `agent/providers/auxiliary_client.py:984`; task→model routing `_resolve_task_provider_model` `:764` reading `auxiliary.{task}.*` config (`_auxiliary_task_config` `:220`) and `AUXILIARY_{TASK}_*`/`CONTEXT_{TASK}_*` env (`:205`) | ok; note config key is `auxiliary.<task>`, **not** the roadmap's proposed `models.<role>` (see §2.4) |
| `delegate_task` | `tools/implementations/delegate_tool.py:391`; cap `MAX_CONCURRENT_CHILDREN = 3` `:41`; depth `MAX_DEPTH = 2` `:42`; blocked tools `:32`; child runner `_run_single_child` `:160`; shared `IterationBudget` `:210` (class `run_agent.py:113`); result entry shape `:338-355`; interrupt propagation via `register_child` `:258`; child file-lock release `:381-388` | ok |
| Worker dispatch | `lean_worker_dispatch.py:47` (`dispatch_worker`), file lock `:56-63` (ttl 1800 s), delegate call `:87-95` (toolsets `["terminal","file","skills","coordination"]`, `max_iterations=40`), outcome record `:72,:84,:104`; gate `LEAN_WORKER_DISPATCH_ENABLED = False` `lean_services.py:140` (consumers `:779,:2069`, `lean_tool.py:883`) | ok |
| Spawned runs | `spawn_workflow` **`leanflow_cli/workflow.py:564`** (roadmap `:537` — drifted); detached: `start_new_session=not interactive`, DEVNULL stdio `:580-588`; env contract built in `resolve_workflow_request` `:394` (`child_env = dict(os.environ)` `:476` — see §4.3 run-id inheritance bug) | corrected |
| Agent inbox | `workflow_state.py:126-134` (`agent-inbox/<agent_id>.jsonl`), `enqueue_workflow_agent_message` `:577` (liveness-validated `{seq,timestamp,kind,text}`), reader `:490`; consumer `_run_background_control_loop` `native_runner.py:8230` — 0.5 s poll `:8265`, `kind=="exit"` terminates descendants and exits `:8274-8283`, any other kind runs a managed turn `:8285+` | ok |
| Agent registry | `summarize_workflow_agents` `workflow_state.py:651`; `workflow_agent_detail` `:853`; transcripts `:860`; kill `terminate_workflow_agent` `:949` (SIGINT to process group); **descendant kill already exists**: `terminate_workflow_agent_descendants` `:983` via `parent_agent_session_id` edges; events written by `append_workflow_activity` `:309` to `activity/runs/<run_id>.jsonl` + `activity/agents/<task>-<agent_id>.jsonl` `:383-387`; run metadata with `parent_run_id` `:196-239`; agent event details (`agent_session_id`, `parent_agent_session_id`, `delegate_depth`, `process_id`) `agent/runtime/workflow_events.py:31-46`, emitted at conversation start/end `run_agent.py:3274,:5101` | ok |
| Plan-state write path | `save_workflow_live_status` `workflow_state.py:305` → `write_json_file` `workflow_json_io.py:31` — **plain `write_text`, NOT atomic**. The atomic writer is `core/utils.py:13` (`atomic_json_write`, temp+rename). Cross-process-safe JSONL append exists: `_locked_append` `workflow_state.py:54` (flock) | roadmap's "atomic path" claim corrected |
| LeanProbe surface | `leanflow_cli/lean/lean_incremental.py` wraps the `lean_probe` package (module docstring `:1-7`); singleton `_probe()` `:95-101`; `lean_incremental_check` `:245` (actions `prepare_file/check_target/feedback`). The package exposes **`LeanProbe.check_code(code, *, cwd=None, include_tactics=False, timeout_s=90)`** (verified via import) — the scratch-snippet surface; **no agent-facing scratch-check tool exists yet** | ok |
| Plausible | **available**: `testdata/workflow_projects/ProveDemo/.lake/packages/plausible` (a Mathlib dependency, `lake-manifest.json:24-29`); tactic `plausible (config)?` defined `Plausible/Tactic.lean:159`; toolchain `leanprover/lean4:v4.30.0-rc2` | ok |
| Statement parsing reuse | `declaration_region` `lean_declarations.py:137`; `_split_declaration_statement_and_proof` `:110` (regex, fragile); **`_find_assignment_marker` `lean_statement_guard.py:163`** — comment/string-aware `:=` scanner (the robust primitive) | — |
| Misc | `TheoremKey` `queue_models.py:53`; `classify_check` `:337`; `ManagerCheck` `:259`; `FailedAttempt` `:276`; `decide()` `queue_manager.py:484`; `MANAGER_WARNING_RETRY_LIMIT = 1` / `MANAGER_HARD_RETRY_LIMIT = 2` `native_runner.py:102-103`; stall limits `_autonomous_blocked_limit`=3 `:545`, `_autonomous_stalled_limit`=4 `:553`, `_autonomous_max_cycles`=120 `:561`; phase template `_run_document_formalization_review_agent` `:5661`; `_manager_final_report_feedback` `:1642`; `_verification_review_system_prompt` `manager_verification.py:116`; toolsets `core/toolsets.py:119` (`leanflow-prove-worker`); state root `workflow_state_paths.py:48` (`<project>/.leanflow/workflow-state/`); env helpers `native_config.py:30-38` (`_read_text_env` / `_read_native_env` = `LEANFLOW_NATIVE_*`); file locks `runtime/file_locks.py:137` (`acquire_file_lock(path, owner_id=…, purpose=…, ttl_seconds=…, force=…)`) | one-line drifts noted |

Confirmed absent (genuinely new): no `plan.md`/`summary.json`/`blueprint.json` writers, no `dispatch_ledger`, no `struggle*` module, no `plausible` reference anywhere in Python (grep-verified).

---

## 1. Phase 2.1 — Struggle detector: `leanflow_cli/workflows/struggle_signals.py`

### Reuse first
Every signal source already exists (table in §0). The new module adds **zero collection code** — it is a pure classifier over values the runner already holds, in the style of `queue_manager.py` ("intentionally pure: no I/O, no logging, no MCP calls", `queue_manager.py:34-36`).

### (a) Files
- **Create** `leanflow_cli/workflows/struggle_signals.py` (pure leaf; imports stdlib + `queue_models` only).
- **Modify** `leanflow_cli/native/native_runner.py` — two one-line context-assembly + call sites (§2.2); import as `from leanflow_cli.workflows import struggle_signals` and call `struggle_signals.evaluate(...)` **by module attribute**, so `tests/leanflow/test_native_runner.py`'s `setattr`-monkeypatch pattern (AGENTS.md anti-pattern note) keeps working.

### (b) Signatures
```python
class Severity(str, Enum):
    NONE = "none"; NUDGE = "nudge"; REROUTE = "reroute"; BREAKPOINT = "breakpoint"

@dataclass(frozen=True)
class StruggleSignal:
    kind: str        # "failed_attempts" | "repeat_error_signature" | "search_spiral"
                     # | "no_progress" | "budget_pressure" | "give_up_phrasing"
    severity: Severity
    evidence: str    # one line, human-readable, ≤ 240 chars
    metric: float    # the raw number that fired (attempts, streak, ratio…)

@dataclass(frozen=True)
class StruggleContext:
    """Snapshot of already-tracked runner state; assembled by the caller, no I/O here."""
    attempt_count: int = 0            # TheoremQueueManager.attempts_for_current() (queue_manager.py:342)
    hard_retry_count: int = 0         # hard_retries_for_current() (:376)
    repeated_signature_count: int = 0 # len(_retry_signatures[(key,"hard")]) duplicates seen (:126,:425)
    search_progress: Mapping = ...    # autonomy_state["search_progress"] (native_runner.py:2430-2465)
    stable_cycles: int = 0            # autonomy_state["continuation_stable_cycles"] (:8885)
    blocked_runs: int = 0             # autonomy_state["continuation_blocked_runs"] (:8914)
    api_calls: int = 0                # result["api_calls"]
    max_iterations: int = 0           # agent.max_iterations
    blocker_summary: str = ""         # _extract_blocker_summary(final_text) (native_utils.py:183)

@dataclass(frozen=True)
class StruggleReport:
    signals: tuple[StruggleSignal, ...]
    severity: Severity                # max over signals
    def fired(self) -> bool: ...      # severity is not NONE
    def to_payload(self) -> dict: ... # JSON-safe, for logging/ledger

def evaluate(ctx: StruggleContext) -> StruggleReport:
    """Classify already-tracked runner state into struggle signals; pure, deterministic, no I/O."""
```

### (c) Thresholds (constants in the module, mirroring the verified sources)
| Signal | Fires when | Severity |
|---|---|---|
| `failed_attempts` | `attempt_count >= 2` (owner decision D7: nudge after ~2 genuine failures) | NUDGE; `>= 4` → REROUTE |
| `repeat_error_signature` | same signature consumed ≥ 2× (`consume_retry_once_for` dedupe list, `queue_manager.py:431-436`) | NUDGE |
| `search_spiral` | `search_progress.same_query_streak >= 2` or `search_count >= 6` (same constants as `native_runner.py:111-112`) | NUDGE |
| `no_progress` | `stable_cycles >= 2` (below the stop limit 4, `:553` — nudge *before* the stall stop trips) | NUDGE; `>= 3` → REROUTE |
| `budget_pressure` | `api_calls / max_iterations >= 0.7` (same floor as `run_agent.py:511`) | NUDGE |
| `give_up_phrasing` | `blocker_summary != ""` | NUDGE; combined with `no_progress` → REROUTE |
| any + retry/budget exhaustion | caller maps exhaustion paths directly | BREAKPOINT |

Persistent combinations (≥ 2 distinct kinds at NUDGE) escalate the report to REROUTE — the future orchestrator's routing input; Phase 2 only logs it.

### (e) Tests — `tests/leanflow/test_struggle_signals.py` (new)
Pure-unit style copied from `tests/leanflow/test_queue_manager.py` (plain functions, no fixtures): one test per signal at/below/above threshold; severity-max test; `to_payload` round-trip; a "quiet context ⇒ NONE" test.

### (f) Acceptance
- No I/O, no imports above `queue_models`/stdlib (enforced by review; keeps the layering rule).
- `evaluate` on a happy-path context returns `severity == NONE` (guarantees the happy path never summons the LLM-manager).
- All thresholds are module constants overridable in one place.

### (g) Risks
- Threshold duplication with `native_runner.py:111-112` — mitigate by importing those two constants into the module or re-exporting (they're plain ints; a one-line import from `native_runner` is forbidden by layering — **copy with a comment naming the source**, and add a test asserting equality via importing both).

---

## 2. Phase 2.2 — LLM-manager (`manager_nudge`)

> **Promoted behavior supersedes the proposal below.** The implementation has no action vocabulary
> and no `stop`. It is invoked for every rejected turn, not only after a struggle threshold. The
> live schema is `{message, progress_acknowledged, commitment}`; deterministic fallback gives
> complete coverage in every model mode. The remainder of this section is retained as design
> history for provider routing and the post-verdict authority boundary.

### 2.1 Reuse first
- LLM call: `run_model_verification_review(provider=…, task="manager_nudge", prompt=…, system_prompt=…, timeout_s=…, max_tokens=…)` (`verification_providers.py:160`) — handles messages, telemetry (`verification-review-request/result` activity events `:176,:226`), `RuntimeError` = unavailable, and `status ∈ {ok,no_answer,timeout,unavailable,error}`. Synchronous model work runs in `agent/providers/isolated_auxiliary.py`; the parent enforces `timeout_s` against elapsed wall-clock time and kills/reaps the isolated process group before returning `timeout`.
- Provider/model routing: **exists with zero new code** — `call_llm(task="manager_nudge")` resolves `auxiliary.manager_nudge.{provider,model,base_url,api_key,reasoning_effort}` from `config.yaml` (`auxiliary_client.py:764,:220-225,:957-981`) and env `AUXILIARY_MANAGER_NUDGE_{PROVIDER,MODEL,BASE_URL,API_KEY,REASONING_EFFORT}` (`:205-213`). Small/fast default comes free: the auto chain's auxiliary defaults are cheap models (`:49-55,:77-78`).
- JSON parsing: `_extract_json_payload` (`native_utils.py:121`, already imported by native_runner `:301`).
- System-prompt precedent: `_verification_review_system_prompt` (`manager_verification.py:116`).

> **Correction to roadmap §4.8:** the proposed `config.yaml` key `models.manager_nudge` has no implementation today; the working mechanism is `auxiliary.manager_nudge.*`. Spec uses the existing key family; §4.8's table should be re-pointed at `auxiliary.<role>` keys (orchestrator later = `auxiliary.orchestration`).

### 2.2 Invocation point (exact)
Single hook inside `_review_agent_final_report`, **post-verdict**: after `ok = bool(manager_check.get("ok"))` (`native_runner.py:2007`), only in the reject-and-continue branch (`:2052-2073`, where `_manager_final_report_feedback` is appended) and in the retry-exhausted branch (`:1988-2006`). Never on the accept path (`:2037-2045`), never per prover step. Flow:

1. Assemble `StruggleContext` from `_queue_manager_from_state(autonomy_state)` + `autonomy_state` + `manager_check` + final text.
2. `report = struggle_signals.evaluate(ctx)`; if `not report.fired()` → today's behavior byte-identical.
3. If fired and mode ≠ `off`: call `manager_nudge.request_nudge(report, packet)` (new leaf, below).
4. `dark` mode: log only. `live` mode: append the nudge's `message` as one extra paragraph at the END of the `_manager_final_report_feedback` message (`:2068`) — same user message, clearly delimited `[MANAGER GUIDANCE — advisory]`. **`manager_check["ok"]` is never touched** (the deterministic-gate invariant).

A second, cheaper hook: `_handle_api_step_budget_exhaustion` (`:6008`) records the evaluated report into the failed-attempt handoff (`_api_step_budget_handoff_message`, `:6053`) in live mode — this is where "try dispatching / don't give up" advice lands after an exhausted attempt.

### 2.3 New module `leanflow_cli/workflows/manager_nudge.py`
```python
NUDGE_TASK = "manager_nudge"
NUDGE_ACTIONS = ("continue", "replan", "redraft", "falsify", "dispatch", "stop")

@dataclass(frozen=True)
class NudgeResult:
    action: str        # one of NUDGE_ACTIONS; "stop" REQUIRES non-empty report_note (never-silent, N1)
    message: str       # the optimistic-but-strict paragraph handed to the prover (live mode)
    rationale: str
    confidence: float
    raw_status: str    # provider status from VerificationReviewResult
    def is_usable(self) -> bool: ...

def nudge_mode() -> str:
    """Read LEANFLOW_MANAGER_LLM_MODE ∈ {off,dark,live}; default 'off'; legacy LEANFLOW_MANAGER_LLM_ENABLED=1 ⇒ 'live'."""

def build_nudge_prompt(report: StruggleReport, packet: Mapping[str, Any]) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for the manager-nudge review call."""

def request_nudge(report, packet, *, timeout_s=45, max_tokens=600) -> NudgeResult | None:
    """Call run_model_verification_review(task='manager_nudge'); parse strict JSON; None on any failure (fail-open, silent)."""

def record_nudge(result: NudgeResult | None, report, *, applied: bool) -> None:
    """Append to summary.json['manager_nudges'] (cap 50, atomic_json_write) and emit a 'manager-nudge' activity event."""
```

**System prompt draft** (research-pusher tone, §4.7):

> You are LeanFlow's proving manager — optimistic, strict, and concrete. A deterministic Lean kernel gate has already judged this attempt; you can NEVER change that verdict. Your only job: pick ONE next action and write a short, energizing, concrete instruction for the prover. Difficulty is a routing signal, never a terminal state. Do not use give-up language. Actions: "continue" (stop searching, commit to a concrete proof attempt now), "replan" (step back, list sub-goals, pick the easiest), "redraft" (the current proof shape is dead; start a different shape), "falsify" (suspect the statement; recommend a negation probe), "dispatch" (suggest a sub-job: an empirical check or a literature/mathlib search — you may only SUGGEST, never launch), "stop" (only when every alternative above is exhausted; you MUST include report_note summarizing what was tried and learned — silence is forbidden). Reply with strict JSON only: {"action": …, "message": …, "rationale": …, "confidence": 0.0-1.0, "report_note": …}.

**User prompt content** (bounded, ≤ ~2.5 kB): theorem id + file, attempt count and last-3 attempt reasons (`attempt_entries_for`, `queue_manager.py:332`), blocker kind + gate output first 700 chars (same bound as `:1663`), search tracker summary, fired signals from `report.to_payload()`, remaining budget.

### 2.4 Config / flags
- `LEANFLOW_MANAGER_LLM_MODE` = `off` (default) | `dark` | `live`.
- `auxiliary.manager_nudge.{provider,model,…}` in `config.yaml`; env `AUXILIARY_MANAGER_NUDGE_*` (all pre-wired, §2.1).
- Rate limit: at most 1 LLM call per (theorem, attempt) — key on `(TheoremKey.storage_key(), attempt_count)`, memoized in `autonomy_state["manager_nudge_seen"]`.

### 2.5 Dark-launch log schema — `summary.json.manager_nudges[]`
`summary.json` lives at `workflow_state_root()/summary.json` (`workflow_state_paths.py:48`; Phase 1 owns other keys; this key is Phase 2's). Written with `core.utils.atomic_json_write` (`core/utils.py:13`).
```jsonc
{ "timestamp": "...", "theorem": "Nat.foo", "file": "ProveDemo/X.lean",
  "signals": [{"kind":"search_spiral","severity":"nudge","metric":6,"evidence":"…"}],
  "severity": "nudge", "mode": "dark", "applied": false,
  "nudge": {"action":"continue","message":"…","rationale":"…","confidence":0.7,"raw_status":"ok"} }
```
Every entry also mirrors as an `append_workflow_activity("manager-nudge", …)` event (`workflow_state.py:309`) so it shows in agent transcripts and the registry with zero new plumbing.

### (e) Tests
- `tests/leanflow/test_manager_nudge.py` (new): monkeypatch `verification_providers.run_model_verification_review` (pattern: `tests/agent/test_auxiliary_client.py` mocks `call_llm`); JSON-parse robustness (fences, prose, garbage → `None`); `action=="stop"` without `report_note` → rejected as unusable; mode resolution matrix.
- Extend `tests/leanflow/test_native_runner.py`: `monkeypatch.setattr(runner.struggle_signals, "evaluate", …)` + `monkeypatch.setattr(runner.manager_nudge, "request_nudge", …)`; assert (1) gate result identical when mode=off; (2) `manager_check["ok"]` unchanged in live mode even when the fake nudge says `action="stop"`; (3) nudge text present in the appended feedback message in live mode; (4) accept path performs zero nudge calls.

### (f) Acceptance
- `LEANFLOW_MANAGER_LLM_MODE=off` (default): the gate byte-identical to today (assert message equality in test).
- Dark mode on the ProveDemo integration run: `summary.json.manager_nudges` populated, no message content changes.
- The LLM path can never mutate `manager_check` — enforce by passing a **copy** of the packet into `request_nudge`.

### (g) Risks
- Nudge latency on the failure path: capped `timeout_s=45`, fail-open to `None`.
- Over-calling: rate-limit key + happy-path structural guarantee (§1 acceptance).
- Prompt-injection via gate output into the nudge model: output is advisory-only by construction; bound at 700 chars.

---

## 3. Phase 3.3 — Dispatch substrate: WHAT EXISTS vs WHAT IS MISSING

> **Promoted behavior:** the previously missing async seam now ships as process-isolated
> `deploy_async` plus `native/dispatch_worker.py`, polled by `research_portfolio.py`. Thread-based
> output redirection is not used. Children return structured deliverables; the parent owns shared
> plan/graph writes and refills completed/failed/stuck portfolio slots while the goal is unresolved.
> Launch itself is transactional: the ledger persists a nonce-bearing, capacity-counted `deployed`
> reservation before any spec write or `Popen`; `running` is committed only with the exact worker
> identity. Nonce-bound parent/child identity receipts recover the `Popen`-before-commit window,
> restart reconciliation retries incomplete handshakes with compare-and-swap nonce rotation, and
> a per-job thread/POSIX sidecar lock spans reservation/rotation through spec write, `Popen`, and
> running CAS. A stale launcher rechecks the ledger nonce under that lock. The atomically replaced
> job-global spec is the current-nonce fence read twice by workers, with the final read under the
> same sidecar together with an expected-parent liveness check. Cross-parent recovery waits for the
> exact old process boundary to disappear after bounded TERM/KILL escalation before replacement;
> ambiguous permission or identity lookup failures fail closed. Retry rotation writes that fence before committing its new ledger nonce, making the
> crash gap reject-only. Identity/result files use nonce-digest names, so delayed old output cannot
> overwrite or complete a newer attempt.

**Exists (reuse as-is):**
1. **Child execution** — `delegate_task` (`delegate_tool.py:391`): sync-blocking; 1 task inline (`:461-478`) or ≤ 3 parallel via `ThreadPoolExecutor` (`:489`); per-child result dict with `status/summary/api_calls/duration_seconds/exit_reason/tokens/tool_trace` (`:338-355`); depth-2 recursion guard (`:412`); credential override via `delegation.*` config (`:568`); children emit `conversation-start/end` activity with `agent_session_id` + `parent_agent_session_id` + `process_id` (`run_agent.py:3274,:5101` → `workflow_events.py:31-46`), so they appear in `summarize_workflow_agents` (`workflow_state.py:651`).
2. **Interrupts** — parent interrupt propagates to registered children (`delegate_tool.py:258-261`); spawned processes killable mid-flight via `terminate_workflow_agent` (SIGINT to pgid, `workflow_state.py:949-980`) and **recursively** via `terminate_workflow_agent_descendants` (`:983`).
3. **Detached runs** — `spawn_workflow` (`workflow.py:564`) with the full `LEANFLOW_NATIVE_*` env contract (`:476-523`); observation channels: per-run activity `activity/runs/<run_id>.jsonl` (`workflow_state.py:149`), run metadata incl. `parent_run_id`+`process_id` (`:196-239`), `live_status.json` (`:98`) with dead-process normalization (`:536-574`).
4. **Control channel** — file-based inbox `agent-inbox/<id>.jsonl`; `enqueue_workflow_agent_message` (`:577`); commands: `exit` + free-text prompts, consumed by `_run_background_control_loop` (`native_runner.py:8230`).
5. **Scoping** — cross-process file locks (`runtime/file_locks.py:137`); statement guard (`lean_statement_guard.py:58`); `outcomes.jsonl` append (`workflow_state.py:390`).

**Missing (the gaps the DispatchService fills):**
| Gap | Evidence |
|---|---|
| No job identity/ledger: a `delegate_task` call cannot be correlated to the child `agent_session_id`s it produced; result exists only in the parent's transcript | `delegate_task` returns JSON with `task_index` only (`:338`); nothing persisted |
| No mid-flight status/kill for **in-process** children (threads, no pid of their own; only whole-parent interrupt) | `_run_single_child` runs `child.run_conversation` to completion (`:266`) |
| No wall-clock timeout for children — only iteration budget, and it is **shared with the parent** (a runaway child drains the parent's budget) | `shared_budget = parent.iteration_budget` (`:210`, passed `:248`) |
| Spawned child **inherits the parent's run id** → its events land in the parent's activity stream; `LEANFLOW_WORKFLOW_PARENT_RUN_ID` is read (`workflow_state.py:351`) but **never set** anywhere (grep-verified) | `child_env = dict(os.environ)` (`workflow.py:476`); `_workflow_run_id` prefers env (`workflow_state.py:277`) |
| Spawned child **overwrites the parent's `live_status.json`** (single path per project, `workflow_state.py:98`) | both runs call `_persist_live_status` → same file |
| Inbox is only polled in the background loop **after** the followup loop finishes (`native_runner.py:9442`) — a busy child never sees inbox messages mid-run | `:8230` call graph |
| `dispatch_worker` is dead code behind a hardcoded `False` gate and returns only a blocking summary | `lean_services.py:140` |
| No patience policy, no reconciliation, no lineage ids | absent (grep-verified) |

---

## 4. Phase 3.4 — DispatchService: `leanflow_cli/workflows/dispatch_service.py`

### (a) Files
- **Create** `leanflow_cli/workflows/dispatch_service.py` (ledger + lifecycle; imports `workflow_state`, `workflow_json_io`, `core.utils`, `runtime.file_locks`; delegate/spawn imported lazily like `lean_worker_dispatch.py:87`).
- **Create** `leanflow_cli/workflows/dispatch_models.py` (pure dataclasses, no I/O — mirrors the `queue_models`/`queue_manager` split).
- **Modify** `tools/implementations/delegate_tool.py`: add `isolate_budget: bool = False` param on `delegate_task`/`_run_single_child` (when True, `shared_budget = None` at `:210` so the child gets its own `IterationBudget(max_iterations)`); additive, default preserves behavior.
- **Modify** `leanflow_cli/workflow.py` `spawn_workflow`: accept `extra_env: Mapping[str,str] | None = None` merged into `child_env` (`:578`) — used by the spawn backend to set `LEANFLOW_WORKFLOW_RUN_ID=""` (forces fresh id at `workflow_state.py:277-289`), `LEANFLOW_WORKFLOW_PARENT_RUN_ID=<parent run id>` (consumed at `:349-353` — activating the already-built but never-fed parent-run edge), `LEANFLOW_WORKFLOW_RUN_SCOPE=background-session` (`:188`).
- **Later (Phase 5)**: registration as an agent tool for orchestrator/planner/decomposer toolsets; Phase 3b exposes only the Python API + the negation-probe caller.

### (b) Data schemas (`dispatch_models.py`)
```python
ARCHETYPES = ("prover", "empirical", "deep_search", "negation_probe", "decomposition")
STATES = ("proposed", "deployed", "running", "done", "failed", "stuck", "killed")
DISPATCH_ROLES = ("orchestrator", "planner", "decomposer", "human")   # N2: manager/prover NOT here

@dataclass(frozen=True)
class JobBudget:
    api_steps: int          # → delegate max_iterations / AGENT_MAX_TURNS for spawns
    wall_clock_s: int       # patience anchor

@dataclass(frozen=True)
class JobSpec:
    job_id: str             # dotted lineage id, §(c)
    archetype: str
    requester_role: str     # validated against DISPATCH_ROLES (N2)
    objective: str          # self-contained (delegate 'goal')
    inputs: dict            # {"node_ids": [...], "files": [...], "plan_pointer": "plan.md#..."}
    toolsets: tuple[str, ...]
    budget: JobBudget
    deliverable: str        # + "decomposition_report" for source-backed proposal-only splits
    scope: dict             # {"file_locks": [...], "scratch_only": bool}
    parent_job_id: str
    report_to: str          # plan-state section name

@dataclass
class LedgerEntry:
    spec: JobSpec
    state: str
    agent_session_ids: list[str]   # reconciled from activity/agents
    run_id: str                    # spawn backend only
    launch_nonce: str              # async launch transaction/fencing identity
    launch_started_at: str         # capacity-counted deployed handshake start
    launch_attempt: int            # monotonic retry generation
    process_id: int                # spawn backend only
    process_group_id: int          # exact async process ownership
    process_session_id: int        # exact async process ownership
    process_token_sha256: str      # hash only; raw token stays in worker env
    created_at: str; started_at: str; finished_at: str
    result: dict                   # {"status", "deliverable": {...}, "artifact_paths": [...]}
    consumed: bool
    notes: str
```

### (c) Lineage ids (N3)
- Grammar: `<root>.<role-path>.<archetype-tag>-<seq>` — e.g. `prove-20260702T101500Z.orchestrator.planner.ds-042`. Root = the run id already minted by `_workflow_run_id()` (`workflow_state.py:276-290`), truncated to the `<task>-<timestamp>` stem. Tags: `pv` (prover), `em` (empirical), `ds` (deep-search), `np` (negation), `dc` (decomposition).
- `next_job_id(ledger, parent_job_id, archetype) -> str` — seq = 1 + count of ledger ids with prefix `parent_job_id.`; zero-padded 3 digits.
- `ancestors(job_id) -> tuple[str, ...]` — dotted prefixes; `is_ancestor(a, b) = b.startswith(a + ".")`.
- `descendants(ledger, job_id)` — prefix scan. **Kill rights (N3): `kill(job_id, requester_job_id)` requires `is_ancestor(requester, job)` or requester == root/human.** Process-level enforcement rides the existing `parent_agent_session_id` edges (`workflow_state.py:983`) for spawned trees.
- Every dispatched child receives its `job_id` (a) in the delegate `context` header, (b) as `LEANFLOW_DISPATCH_JOB_ID` env for spawns — so grandchildren dispatched later can extend the chain.

### (d) Ledger persistence
- Authority: `summary.json` key `dispatch_ledger` (per roadmap §4.2), read via `read_json_file` (`workflow_json_io.py:20`), written via `core.utils.atomic_json_write` (`core/utils.py:13`) under `runtime/file_locks.acquire_file_lock(summary.json, owner_id=<runner owner>, purpose="dispatch-ledger", ttl_seconds=60)` to serialize multi-process writers.
- Every state transition also emits `append_workflow_activity("dispatch-job", …, job_id=…, state=…, agent_session_id=…)` (`workflow_state.py:309`) — giving a free per-agent journal in `activity/agents/*.jsonl` for reconciliation.
- `reconcile(ledger) -> ledger`: first recover every nonce-bearing `deployed` entry by adopting its
  exact identity receipt or rotating/retrying a stale handshake; then cross-check each `running`
  entry against exact process identity or `summarize_workflow_agents()` evidence. Run at every
  `poll()` and portfolio tick (**"a job can never be silently lost"** — final-report generation
  still prints any non-terminal entries).

### (e) API (`dispatch_service.py`)
```python
class DispatchService:
    def __init__(self, *, parent_agent=None, root_job_id: str = "", cap: int = 3): ...

    def propose(self, spec: JobSpec) -> LedgerEntry:
        """Validate (role in DISPATCH_ROLES, budget>0, unique job_id) and persist state='proposed'."""

    def deploy(self, job_id: str) -> LedgerEntry:
        """Sync v1: run the job to completion via the archetype backend; cap 3 concurrently-running entries."""

    def deploy_async(self, job_id: str) -> LedgerEntry:
        """Reserve durably, launch one process worker, and commit only its exact identity as running."""

    def poll(self, job_id: str) -> dict:
        """Reconciled status snapshot: state, agent statuses, last activity age, budget spent."""

    def join(self, job_id: str, timeout_s: int | None = None) -> LedgerEntry:
        """Wait for async launch recovery/result harvest up to the optional timeout."""

    def kill(self, job_id: str, *, requester_job_id: str) -> dict:
        """Ancestor-gated; persist killed only after exact process exit is proven."""

    def consume(self, job_id: str) -> dict:
        """One-way result hand-off: return {'deliverable', 'artifact_paths', 'plan_delta'}; mark consumed; NEVER raw transcript."""

    def list_descendants(self, job_id: str) -> list[LedgerEntry]: ...
```

**Backends** (private):
- `_run_delegate_job(spec)` — empirical/deep-search/negation/decomposition research: `delegate_task(goal=spec.objective, context=<rendered JobSpec + artifact paths>, toolsets=<explicit archetype allowlist>, max_iterations=spec.budget.api_steps, parent_agent=…, isolate_budget=True)`; file locks per `spec.scope["file_locks"]` acquired/released around the call (pattern: `lean_worker_dispatch.py:56-63`, ttl = `wall_clock_s`). Scratch jobs resolve `web`/`lean` through focused read-only `web-research`/`lean-research` toolsets, so download, clone, terminal, shared-file patch, and nested LLM-advisor tools are not callable. Missing toolsets receive a non-empty archetype-safe surface; a wholly disallowed request fails before provider invocation rather than inheriting parent/default tools. Decomposition accepts only a bounded structured `decomposition_report`: source kinds come from the exact documented allowlist, every retained subgoal cites a source, reaches the requested target under the kind-aware dependency graph, and supplies nonempty strict-difficulty evidence. Malformed or structurally unusable output is `incomplete_unverified` and cannot finish as a successful decomposition job. It always returns an empty `plan_delta`; only the parent may materialize it.
- `_run_spawn_job(spec)` — prover shape A: `spawn_workflow(f"/prove {stub_file}", extra_env={run-id triplet, LEANFLOW_DISPATCH_JOB_ID, AGENT_MAX_TURNS=str(spec.budget.api_steps)})`; then a monitor loop: `process.poll()` + run-activity mtime + patience policy; on completion read the child's `outcomes.jsonl` tail and per-declaration state via `lean_incremental_check(action="check_target")` (`lean_incremental.py:245`) for the deliverable. **v1 constraint (documented):** the child overwrites `live_status.json`; the monitor therefore never reads `live_status.json` and relies on run-scoped streams only. Restoring the parent's live-status is a Phase 1/4 concern.

**Patience policy** (from declared budgets, per roadmap): a job is `stuck` (and then killed) only when **both** hold — `now > started_at + 1.5 × wall_clock_s` **and** last activity-event age `> max(600 s, 0.25 × wall_clock_s)`. The second clause is what protects a long Lake build (its `tool-call`/`api-request` events keep the stream fresh) while killing a truly wedged agent.

**Result consumption (one-way):** deliverables are (i) verified disk edits — for prover jobs the acceptance evidence is re-checked by the **parent's own** deterministic checker (`_manager_check_queue_item`, `native_runner.py:1210`) before the graph/plan delta is applied, never trusted from the child's claim; (ii) a bounded JSON deliverable per schema id; (iii) parent-produced graph updates. Research children, including decomposition, cannot emit an authoritative plan delta.

### (f) Flags
- `LEANFLOW_DISPATCH_ENABLED=1` (default off) gates every `deploy`; `propose` always works (dark-plannable).
- `LEANFLOW_DISPATCH_MAX_CONCURRENT` (default 3, matching `MAX_CONCURRENT_CHILDREN`, `delegate_tool.py:41`).
- Existing `LEAN_WORKER_DISPATCH_ENABLED` (`lean_services.py:140`) **stays False** — `dispatch_worker` is superseded, not flipped.

### (g) Tests
- `tests/leanflow/test_dispatch_service.py` (new): tmp-project fixture via `monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", tmp_path)` (pattern: `tests/leanflow/test_workflow_status.py:34,:150`); fake backends injected by monkeypatch; cover: lineage generation/ancestry/kill-rights matrix; ledger state machine (illegal transitions raise); cap enforcement; reconciliation marks dead agents (`monkeypatch` `_process_seems_alive`); consume-once semantics; role validation rejects `requester_role="prover"` (N2).
- Extend `tests/tools/test_delegate.py` (class-based, mocks `_run_single_child` — `:128,:147`): `isolate_budget=True` passes `iteration_budget=None` to the child ctor.
- Concurrency: extend `tests/leanflow/test_workflow_state_concurrency.py` with parallel `atomic_json_write`+flock ledger writers.

### (h) Acceptance
- With flags off: repo behavior byte-identical (nothing calls the module).
- A killed spawned job leaves `state="killed"`, the process group dead, and a ledger note — demonstrated in an integration test that spawns a `sleep`-style fake runner.
- After any run containing dispatches, `summary.json.dispatch_ledger` has **no entry left in `proposed/deployed/running`** (reconciled to a terminal state or surfaced by §6).

### (i) Risks
- **In-process kill is cooperative only** (thread children): v1 documents that `kill` for delegate-backend jobs sets the parent's interrupt for **all** children (`register_child` propagation is parent-wide, `delegate_tool.py:258`); per-child interrupt needs an AIAgent-level flag — deferred, called out for the 3a review gate.
- Ledger/actvity dual-write divergence: activity is journal, ledger is authority; reconcile favors "agent evidence over ledger optimism".
- `live_status.json` clobber by shape-A children: documented v1 constraint; monitor avoids the file.
- Shared-budget change in `delegate_tool.py` touches a hot tool: default-off param + existing test suite (`tests/tools/test_delegate.py`) pins current behavior.

---

## 5. Phase 3.5 — Negation probe

### Reuse first
- Declaration source: `declaration_region(path, symbol)` (`lean_declarations.py:137`).
- Statement/proof split: the comment/string-aware scanner `_find_assignment_marker` (`lean_statement_guard.py:163`) — NOT the fragile regex splitter (`lean_declarations.py:110`, first-`:= by` regex).
- Scratch execution: `lean_probe.LeanProbe.check_code(code, *, cwd, include_tactics, timeout_s=90)` via the existing warm singleton `_probe()` (`lean_incremental.py:95-101`).
- Trigger data: attempt counts (`queue_manager.py:335`) + nudge action `falsify` (§2) — matches owner decision D4 (~1 probe/lemma after ~2 failures or risk-flag).
- Plausible: **verified available** — `plausible` package vendored in ProveDemo (`.lake/packages/plausible`, Mathlib dependency per `lake-manifest.json:24-29`), tactic `plausible` (`Plausible/Tactic.lean:159`). Behavior per its docstring (`Tactic.lean:100-160`): counterexample ⇒ **error** "Found problems!" with variable assignments; give-up ⇒ error; 100 passing samples ⇒ **acts like `admit`** (i.e. closes the goal unsoundly — success is only a plausibility hint, never proof); missing instances ⇒ error "Failed to create a `testable` instance …".

### (a) Files
- **Create** `leanflow_cli/lean/negation_probe.py` (Lean-facing leaf).
- **Modify** `leanflow_cli/lean/lean_incremental.py`: add the thin scratch wrapper (the module already owns the probe singleton):
```python
def lean_scratch_check(code: str, *, cwd: str = "", timeout_s: int = 90) -> dict[str, Any]:
    """Run a standalone Lean snippet through the warm LeanProbe REPL (LeanProbe.check_code); returns the normalized success/ok payload."""
```

### (b) Signatures (`negation_probe.py`)
```python
@dataclass(frozen=True)
class NegationGoal:
    name: str            # neg_<short name>
    original: str        # the source declaration signature
    binders: str         # verbatim binder text ("" if none)
    result_type: str     # verbatim type text
    prop: str            # "∀ <binders>, <type>"  (or just <type>)
    lean_code: str       # "theorem neg_x : ¬ (<prop>) := by\n  sorry"

def build_negation_goal(file_path: str, theorem_id: str, *, cwd: str = "") -> NegationGoal | dict:
    """Mechanically construct ¬P for a declaration: split signature at the top-level ':' (depth-aware over () {} [] ⦃⦄, comment/string-skipping like lean_statement_guard._find_assignment_marker), universally close binders, return the neg theorem skeleton; error dict with error_code on parse failure."""

def scratch_header(file_path: str) -> str:
    """Collect the file's `import …` lines verbatim (plus `import Plausible` if absent) as the scratch-snippet header."""

def run_plausible_preprobe(file_path, theorem_id, *, cwd="", timeout_s=90) -> dict:
    """Run `example <binders> : <type> := by plausible` in LeanProbe scratch; classify: counterexample | passed_sampling | gave_up | not_testable | error, with the counterexample text when found. Hint only."""

def run_negation_attempt(goal: NegationGoal, *, cwd="", timeout_s=120, tactics=("decide", "simp", "omega")) -> dict:
    """Try cheap closers `theorem neg_… : ¬(P) := by <t>` in scratch, appending `#print axioms neg_…`; verdict 'negation_proved' ONLY when ok=true AND axioms ⊆ {propext, Quot.sound, Classical.choice} (STANDARD_AXIOMS, lean_services.py:139); else 'inconclusive'."""

def run_negation_probe(file_path, theorem_id, *, dispatch: DispatchService | None, cwd="", budget=None) -> dict:
    """Full pipeline: build goal → plausible pre-probe → cheap ladder → (optional) one dispatched shape-B prover job on the ¬P scratch goal; record outcome; enforce per-lemma probe budget."""
```

### (c) Mechanics & classification
- **Construction**: from `theorem foo (x : A) {n : ℕ} (h : P x) : Q x n`, produce `prop = ∀ (x : A) {n : ℕ} (h : P x), Q x n` — Lean 4 `∀` accepts implicit/instance binders verbatim, so binder text is reused byte-for-byte; only the top-level `:` boundary must be found (new depth-aware scanner, same style as `lean_statement_guard.py:163-212`). No binders ⇒ `prop = <type>`.
- **Universally-quantified statements ⇒ counterexample search**: that is exactly the plausible pre-probe (it reverts hypotheses and tests the ∀-closure, `Tactic.lean:104-121`); its "Found problems!" output (with shrunken assignments) is captured from `check_code` messages and stored as `counterexample_text` — a decisive *hint* to prioritize the ¬P attempt and to seed the prover job's context (`∃`-witness candidates).
- **Ill-formedness guard**: if the constructed `neg` theorem fails to *elaborate* (error mentions the signature rather than the proof), classify `ill_formed` and do **not** count it against the probe budget (protects against autoImplicit/scope edge cases).
- **Authority**: LeanProbe scratch verdicts are evidence for routing; if the probe result should flip a *project* node to `false`, Phase 3b writes the outcome + evidence and (until the Phase 1 graph exists) a decision packet; the sole authoritative acceptance gate remains `_manager_check_queue_item` (`native_runner.py:1210`) — untouched.

### (d) Budgets / flags / trigger
- `LEANFLOW_NEGATION_PROBE_BUDGET` (default 1 probe per `TheoremKey.storage_key()`, tracked in `summary.json.negation_probes`).
- `LEANFLOW_NEGATION_PROBE_AFTER_FAILURES` (default 2), `LEANFLOW_NEGATION_PROBE_TIMEOUT_S` (default 120).
- Authoritative whole-source negation promotion uses a separate cold-start floor:
  `LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S` (default and minimum 300 seconds). The override
  may raise but cannot lower the full-module kernel budget; an exhausted timeout remains a
  checkpointed, retryable infrastructure pause and never grants mathematical authority.
- Trigger sites: (1) LLM-manager `action=="falsify"` suggestion (§2 — suggestion only; the deterministic caller enforces budget/threshold); (2) direct threshold — `attempts_for_current() >= 2` at the budget-exhaustion path (`native_runner.py:6008`); (3) later, orchestrator risk-flag.
- Dispatched as archetype `negation_probe` through the DispatchService (jobs `…np-001`), delegate backend, `toolsets=("lean","file")`, `scope={"scratch_only": true}` (no file locks needed — scratch never touches the tree).

### (e) Outcome writing & feedback loop
```jsonc
// summary.json.negation_probes[] entry
{ "job_id": "…np-001", "theorem": "…", "file": "…",
  "plausible": {"verdict": "counterexample", "counterexample_text": "x := 1, y := 28"},
  "negation": {"verdict": "negation_proved", "tactic": "decide", "axioms_ok": true},
  "graph_effect": "node:<id> → false", "timestamp": "…" }
```
Plus `append_workflow_outcome("negation-probe", …)` (`workflow_state.py:390`) and, when Phase 1's `blueprint.json` exists, a `plan_delta` `{node_id, status:"false", evidence: job_id}` — which is what poisons `split_of` ancestors for re-decomposition (roadmap §4.1); until then the decision packet carries the same content for the human/orchestrator.

### (f) Tests
- `tests/leanflow/test_negation_probe.py` (new): pure unit tests for the signature splitter (binder soup: implicit/instance/anonymous-constructor/comments/strings/`:` inside binder types like `(f : A → B := default)` — the depth scanner must skip `:=` defaults); prop construction golden cases; classification of canned `check_code` payloads (counterexample / gave-up / not-testable / proved / sorry-tainted axioms).
- Integration (marked slow, ProveDemo fixture — pattern: `tests/leanflow/test_lean_incremental.py`): `plausible` finds a counterexample for `theorem bad : ∀ n : Nat, n < 5`; `decide` proves its negation; a true statement yields `passed_sampling` + `inconclusive`.

### (g) Acceptance
- On a deliberately-false ProveDemo stub: probe returns `negation_proved` with standard axioms only, writes the outcome entry, consumes exactly 1 budget unit; second trigger on the same key is a no-op.
- On a true statement: `inconclusive` recorded (→ "too hard ⇒ split" branch for the future orchestrator), never a `false` mark.
- No project file is ever written by a probe (`scratch_only` asserted in test).

### (h) Risks
- `plausible` inapplicability (undecidable props, missing `SampleableExt`): expected and classified (`not_testable`); the probe ladder proceeds regardless.
- Binder-parsing edge cases (autoImplicit, universe binders `.{u}`): `ill_formed` guard + skip; log for corpus building.
- REPL contention with the manager's incremental checks: same `_PROBE` singleton serializes; probes run between prover turns (trigger sites are gate/exhaustion paths), matching the "orchestrator edits between turns" rule (roadmap §4.5).

---

## 6. N1 — Concrete-result mechanics: the end-of-scope FINAL REPORT

### Reuse first
All content sources exist: `TheoremQueueManager.to_autonomy_state()` outcomes/attempts (`queue_manager.py:820`), `attempt_entries_for` (`:332`), `last_verification` (`:547`), `_extract_blocker_summary` (`native_utils.py:183`), `search_progress` (`native_runner.py:2430`), `manager_nudges` / `dispatch_ledger` / `negation_probes` (§2/§4/§5), stop reasons (`_autonomous_stop_reason` `:8801`), activity streams (`workflow_state.py:309`). The generator is a pure renderer over these.

### (a) Files
- **Create** `leanflow_cli/workflows/final_report.py`.
- **Modify** `native_runner.py` — exactly two insertion points, one line each, calling the leaf module: (1) the stop-reason exit of `_drive_autonomous_followups`, immediately after the `autonomy-stop` activity record (`:9163-9165`), for `stop_reason ∈ {stalled, blocked}` (and Phase 1's future `budget-breakpoint`); (2) the cycle-ceiling exit (`:9082-9087`). The `verified` exit is exempt (the proof *is* the artifact). Optionally (3) the `exit` command in `_run_background_control_loop` (`:8274`) when the scope is unverified.

### (b) Signatures
```python
@dataclass(frozen=True)
class ScopeOutcome:
    kind: str   # "proved" | "disproved" | "report"   ← the N1 trichotomy
    detail: str

def generate_final_report(*, stop_reason: str, autonomy_state: Mapping, live_state: Mapping,
                          run_id: str = "") -> Path:
    """Render the never-silent end-of-scope artifact: per-theorem ledger (status, attempts, last errors),
    fired struggle signals & nudges, dispatch/negation outcomes with any non-terminal jobs flagged LOUDLY,
    what-was-learned digest, and concrete next actions; write workflow-state/final-report-<run_id>.md
    atomically and mirror a summary into summary.json['final_report']."""

def classify_scope_outcome(autonomy_state: Mapping, live_state: Mapping) -> ScopeOutcome:
    """proved iff _live_state_is_verified-equivalent evidence; disproved iff a main-statement negation_probe
    verdict is kernel-proved; otherwise report."""
```

### (c) Artifact & schema
- Markdown: `workflow_state_root()/final-report-<run_id>.md` with sections: **Outcome** (proved / disproved / documented-account), **Theorem ledger** (table: theorem, status from `theorem_outcomes`, attempts, last blocker, negation verdict), **What was tried** (attempt reasons + search digest), **What was learned** (nudge rationales, probe evidence, counterexamples), **Open jobs** (any ledger entry not terminal — the "never lost" audit), **Recommended next actions** (mechanical: blocked→split/negate; false→re-state; budget→raise/park).
- `summary.json.final_report`: `{run_id, stop_reason, outcome_kind, path, generated_at, theorem_counts: {proved, blocked, unresolved}, open_jobs: [...]}`.
- When Phase 1's `plan.md` exists, append the same content as a `## Final Report (<timestamp>)` section (append-only; plan.md remains Phase 1's file).

### (d) Surfacing
- `append_workflow_activity("final-report", …, path=…)` → visible in `leanflow status`/agent registry with zero UI work.
- Console print at the runner-exit paths (`:9419-9441`, `:9451-9474` already print exit messages — add the report path line).
- The report path is included in the decision packet so a later orchestrator (Phase 4) or a resumed run (Phase 1 resume = load summary.json) starts from the documented account — closing the N1 loop: **every scope ends in proved | disproved | rigorous account; a silent give-up is structurally impossible** because the only non-verified exits from the followup loop are the two instrumented returns plus `_managed_conversation_failed` (`:9226-9234` — also instrumented, `stop_reason="failed"`).

### (e) Tests
- `tests/leanflow/test_final_report.py` (new): feed synthetic `autonomy_state` (legacy dict shape per `queue_manager.py:820-878`) + `live_state`; assert file creation, section presence, open-job flagging, `classify_scope_outcome` trichotomy.
- Extend `tests/leanflow/test_native_runner.py`: monkeypatch `runner.final_report.generate_final_report`; drive `_drive_autonomous_followups` to a `stalled` stop (existing tests already force `_live_state_is_verified`, `:86,:100` — same technique inverted) and assert exactly one generation call; assert **zero** calls on the `verified` path.

### (f) Acceptance
- Any ProveDemo run ended by stall/block/ceiling leaves `final-report-*.md` + `summary.json.final_report` + a `final-report` activity event.
- Report generation is fail-open (wrapped, logged) — it can never turn a clean stop into a crash.

### (g) Risks
- Double-generation on ceiling+stall interplay: guard with `autonomy_state["final_report_written"]` idempotency key.
- Growth of `summary.json`: markdown holds the bulk; JSON mirror is bounded (counts + paths only).

---

## 7. Sequencing & flag summary

Order: §1 → §2 (dark) → §4 ledger core (+ delegate `isolate_budget`, spawn `extra_env`) → §5 negation probe as first archetype → §6 final report → flip `LEANFLOW_MANAGER_LLM_MODE=live` after dark-log review. Phase 3a review gate (owner) sits between §4's spec-as-designed and its build, per roadmap Phase 3a.

| Flag | Default | Gates |
|---|---|---|
| `LEANFLOW_MANAGER_LLM_MODE` (`off/dark/live`) | `off` | §2 nudger |
| `AUXILIARY_MANAGER_NUDGE_*` / `auxiliary.manager_nudge.*` | auto | nudge model routing (pre-existing mechanism) |
| `LEANFLOW_DISPATCH_ENABLED` | off | §4 deploy |
| `LEANFLOW_DISPATCH_MAX_CONCURRENT` | 3 | §4 cap |
| `LEANFLOW_NEGATION_PROBE_BUDGET` / `_AFTER_FAILURES` / `_TIMEOUT_S` | 1 / 2 / 120 | §5 |
| `LEANFLOW_FINAL_REPORT` | on | §6 (opt-out only) |

Non-negotiables honored: the kernel gate (`native_runner.py:1210/:1890`) is never modified or LLM-overridable (§2.2 copy-only packet, §4 re-verification of prover-job deliverables); all new logic lives in focused leaf modules (`workflows/struggle_signals.py`, `workflows/manager_nudge.py`, `workflows/dispatch_models.py`, `workflows/dispatch_service.py`, `lean/negation_probe.py`, `workflows/final_report.py`) with one-line hooks in `native_runner.py` and additive params in `delegate_tool.py`/`workflow.py` — nothing piled onto the monoliths (AGENTS.md layering + monolith rules); token-efficiency floors apply only via the happy-path zero-cost guarantee, with no context compression imposed on the failure/research paths (N5).
---

# PART III — Phases 4–6 (orchestrator + decomposer; planner fan-out + deep search + prover jobs; spec rewrite + research mode)

All investigation complete. Here is the implementation-ready spec.

---

# `/prove` Redesign — Implementation Spec for Phases 4–6

Repo: `/Users/lmilikic/Desktop/LeanFlow`, branch `docs/prove-redesign-roadmap`. Roadmap: `docs/prove-redesign-roadmap.md`. All citations verified against the tree on 2026-07-02; corrections to drifted roadmap citations are flagged inline. House rules honored: layering `core/` → `agent/`&`tools/` → `leanflow_cli/` (AGENTS.md:52–61), new logic in leaf modules, never on `native_runner.py`/`run_agent.py` (AGENTS.md:56–58), kernel gate `_manager_check_queue_item` (`leanflow_cli/native/native_runner.py:1210`) never LLM-overridable.

## 0. Verified seam map (with corrections)

| Roadmap citation | Verified? | Actual |
|---|---|---|
| loop `native_runner.py:9059` | ✓ | `_drive_autonomous_followups` def at 9059; per-cycle live-state rebuild at 9094; stop-reason branch at 9161–9171 |
| gate `:1890` → checker `:1210` | ✓ | `_review_agent_final_report` :1890; `_manager_check_queue_item` :1210 |
| stall `:8921` | ✓ | stalled-limit check :8921 inside `_autonomous_stop_reason` (def :8801); limits `_autonomous_blocked_limit`=3 :545, `_autonomous_stalled_limit`=4 :553, `_autonomous_max_cycles`=120 :561 |
| baseline restore `:1982` | ✓ | `_restore_queue_assignment_to_baseline_sorry` call on retry exhaustion :1982; `exit_reason="manager_retry_exhausted"` :2005 |
| phase template `:5661` | ✓ | `_run_document_formalization_review_agent` :5661–5715 |
| checkpoint UX `:3788` | ✓ | `/checkpoint` `/resume-plan` `/rollback` help text :3788–3790 |
| budget exhaustion | ✓ | `_handle_api_step_budget_exhaustion` def :6008; called from autonomous loop :9240 and background control loop :8335 |
| `queue_manager.py` `decide()` :484, `assign` :181, `peek_assignment` :219 | ✓ | plus ring-buffer prune `_prune_attempts` :352, retry-signature idempotency `consume_retry_once_for` :425 |
| `queue_models.py` `TheoremKey` :52, `classify_check` :337 | ✓ | limits: `DEFAULT_WARNING_RETRY_LIMIT=1`, `DEFAULT_HARD_RETRY_LIMIT=2`, `DEFAULT_REASONING_ESCALATION_THRESHOLD=5`, `DEFAULT_FAILED_ATTEMPT_HISTORY=10` (queue_models.py:24–27); mirrored `MANAGER_WARNING_RETRY_LIMIT=1`/`MANAGER_HARD_RETRY_LIMIT=2` (native_runner.py:102–103) |
| `workflow.py:537` workflow spawn | ✗ **drifted** | `spawn_workflow` is at **`leanflow_cli/workflow.py:564`** (:537 is inside `NativeLaunchPlan` construction; `load_default_model` :538). Child env built at :476–523 (`child_env = dict(os.environ)` at :476); argv `[sys.executable, "-m", "leanflow_cli.native.native_runner"]` :524 |
| `lean_tool.py:658` decompose | ~ | :658 is the **schema** `LEAN_DECOMPOSE_HELPERS_SCHEMA`; the backend is `lean_decompose_helpers_tool` at **`tools/implementations/lean_experts.py:487`**, validation `_validate_helper_skeletons` :364 |
| `verification_providers.py:160` | ✓ | `run_model_verification_review` :160 |
| `workflow_state.py:98-99/:305/:651` | ✓ | `workflow_live_status_path` :98, `save_workflow_live_status` :305, `summarize_workflow_agents` :651 |
| `lean_worker_dispatch.py:47/:57`, `lean_services.py:140` | ✓ | `dispatch_worker` :47, lock acquisition :57, `LEAN_WORKER_DISPATCH_ENABLED = False` :140 |
| `delegate_tool.py:391` | ✓ | `delegate_task` :391; `MAX_CONCURRENT_CHILDREN=3` :41, `MAX_DEPTH=2` :42, `DEFAULT_MAX_ITERATIONS=50` :43 |
| `lean_workflow_specs.py:20` | ✓ | `VALID_SPEC_KINDS = {"workflow", "worker", "helper"}` :20; frontmatter parser `_split_frontmatter` :56, loader `_load_spec` :87 (kind inferred from `path.parent.name.rstrip("s")` :91), validator `validate_lean_specs` :157 |
| "config.yaml `models.orchestrator`" (§4.8) | ✗ **no such section** | Model routing is `auxiliary.<task>.{provider,model,reasoning_effort,base_url,api_key,command_template,...}` (`leanflow_cli/config.py:57–98`) + env `AUXILIARY_<TASK>_*` (`agent/providers/auxiliary_client.py:190–214`) + inheritance table `_AUXILIARY_TASK_FALLBACKS` (:72–74) resolved by `_resolve_task_provider_model` (:764) inside `call_llm` (:984). Spec below uses `auxiliary.orchestration` / `auxiliary.planner` / `auxiliary.manager_nudge` instead of `models.*` |

Additional load-bearing discovery (not in the roadmap): **a deterministic route table already exists** — `route_workflow_step(workflow_kind, live_state, *, configured_skill, autonomy_state, cwd) -> WorkflowRouteDecision` at `leanflow_cli/lean/lean_services.py:1987–2098`, returning `WorkflowRouteDecision{workflow_kind, skill_name, route_action, blocker_kind, recommended_worker, search_exhausted, reason}` (`leanflow_cli/lean/lean_models.py:103–113`). It is already wired into live state as `live_state["route_decision"]` (native_runner.py:6298→6421) and persisted in `live_status.json` (:710). It computes `blocker_kind` via `classify_blocker_kind`, per-theorem `attempt_count`, and `search_exhausted` (`attempt_count >= 2 or empty_search_streak >= 3`, lean_services.py:2028–2033). **The Phase 4 orchestrator floor extends this function's outputs; it does not build a new classifier.**

---

# PHASE 4

## 4.1 Orchestrator module — `leanflow_cli/workflows/orchestrator.py` (new leaf)

### Reuse first
- Deterministic classification: `route_workflow_step` + `WorkflowRouteDecision` (above) — consumed, not reimplemented.
- Counters: `TheoremQueueManager.attempt_count_for` (queue_manager.py:335), `warning_retries_for`/`hard_retries_for` (:373/:381), `pending_count` (:165), `outcomes` (:601), rebuilt from state via `_queue_manager_from_state` (native_runner.py:869) — the orchestrator receives an already-built manager, it never re-parses autonomy dicts.
- Stall/blocked machinery: `continuation_stable_cycles` / `continuation_blocked_runs` maintained by `_autonomous_stop_reason` (native_runner.py:8884–8919).
- Persistence: `append_workflow_activity` (workflow_state.py:309) and Phase-1 plan-state writer (`save_workflow_live_status`-style atomic `write_json_file`, workflow_state.py:305).

### Genuinely new

**(a) Files**
- Create `leanflow_cli/workflows/orchestrator.py` (RouteContext, route table, route dataclasses; pure — no I/O beyond reading passed-in state).
- Create `leanflow_cli/workflows/plan_state.py` in Phase 1 (graph + decision packets); orchestrator imports it.
- Modify `leanflow_cli/native/native_runner.py` at exactly **three call sites** (thin calls only, per AGENTS.md):
  1. **Scope-entry**: in `_drive_autonomous_followups`, after the cycle-top `_build_live_proof_state_compat` (:9094) and before `_autonomous_stop_reason` (:9161) — only on `cycle == 1` and after breakpoint resumes.
  2. **Stall**: intercept the `stop_reason != "continue"` branch (:9162) — when `stop_reason in {"stalled", "blocked"}` and orchestrator enabled, consult `_orchestrator_route` before returning; a route other than `escalate/park` replaces the stop with a route execution and `continue`.
  3. **Budget breakpoint**: after `_handle_api_step_budget_exhaustion` returns `budget_recorded_attempt=True` (:9240–9248), and on `exit_reason == "manager_retry_exhausted"` surfaced from `_review_agent_final_report` (:2005) — build the decision packet (Phase 1) and call `_orchestrator_route`.

**(b) Signatures**
```python
@dataclass(frozen=True)
class RouteContext:
    """Everything the orchestrator may consult for one routing decision; pure data."""
    trigger: str                       # "scope-entry" | "stall" | "budget-breakpoint" | "retry-exhausted"
    workflow_kind: str                 # _workflow_kind(); "prove"/"formalize" (AUTONOMOUS_WORKFLOW_KINDS, native_runner.py:93)
    route_decision: Mapping[str, Any]  # live_state["route_decision"] (route_workflow_step output, lean_services.py:1987)
    active_file: str                   # live_state["active_file"]
    target_symbol: str                 # live_state["target_symbol"]
    declaration_queue_total: int       # live_state["declaration_queue_total"]
    sorry_count: int | None            # live_state["sorry_count"]
    project_sorry_count: int | None    # live_state["project_sorry_count"]
    diagnostics: str                   # live_state["diagnostics"] (truncated to 2k)
    blocker_summary: str               # live_state["blocker_summary"] (_extract_blocker_summary, native_utils.py:183)
    stable_cycles: int                 # autonomy_state["continuation_stable_cycles"] (native_runner.py:8891)
    blocked_runs: int                  # autonomy_state["continuation_blocked_runs"] (:8915)
    attempt_count: int                 # mgr.attempt_count_for(current key) (queue_manager.py:335)
    hard_retries: int                  # mgr.hard_retries_for(key) (:381)
    warning_retries: int               # mgr.warning_retries_for(key) (:373)
    pending_count: int                 # mgr.pending_count() (:165)
    unresolved_outcomes: int           # count of mgr.outcomes with status != "solved" (:601; statuses queue_models.py:287)
    search_exhausted: bool             # route_decision["search_exhausted"] (lean_services.py:2028)
    graph_frontier: tuple[Mapping, ...]   # plan_state.frontier(): "stated" nodes whose depends_on are all "proved"
    graph_blocked: tuple[Mapping, ...]    # nodes with status "blocked"/"false"
    decision_packet: Mapping[str, Any] | None  # Phase-1 packet for budget-breakpoint/retry-exhausted triggers
    routes_used_this_scope: int        # orchestrator's own counter, persisted in autonomy_state["orchestrator_routes_used"]
    research_mode: bool                # LEANFLOW_RESEARCH_MODE (Phase 6)

@dataclass(frozen=True)
class OrchestratorRoute:
    route: str        # "direct-prove" | "decompose" | "plan" | "negate" | "park" | "re-state" | "escalate"
    reason: str
    target: Mapping[str, Any] = field(default_factory=dict)  # node/theorem the route applies to
    source: str = "deterministic"      # "deterministic" | "llm"

def build_route_context(trigger, live_state, autonomy_state, mgr, plan_state) -> RouteContext:
    """Assemble RouteContext from the four state sources; total function, never raises."""

def orchestrator_route(ctx: RouteContext) -> OrchestratorRoute:
    """Deterministic floor route table; the optional LLM layer (Phase 6) may only upgrade, never downgrade to a stop."""
```

**(c) Deterministic route table** (thresholds tied to existing constants):

| Condition (in order) | Route |
|---|---|
| queue present, `attempt_count < MANAGER_HARD_RETRY_LIMIT` (=2, native_runner.py:103), no breakpoint trigger | `direct-prove` (no-op passthrough — the cycle body runs byte-identically; the orchestrator returns without touching history/live_state) |
| trigger=`budget-breakpoint`/`retry-exhausted` AND `attempt_count >= MANAGER_HARD_RETRY_LIMIT` AND `search_exhausted` | `decompose` |
| trigger=`budget-breakpoint` AND decision packet shows ≥2 genuine failures AND negation not yet probed (packet.negation_status == "none") | `negate` (Phase 3b negation probe; roadmap D7: ~1 probe/lemma after ~2 failures) |
| graph node for target has status `false` (negation proved) and it is a sub-lemma (`split_of` edge exists) | `re-state` (backtrack: invalidate subtree, orchestrator re-decomposes — §4.1 of roadmap) |
| negation proved AND node is the goal (no `split_of` parent) | `escalate` (disproved — N1 concrete result: negation kernel-proved) |
| trigger=`scope-entry` AND `declaration_queue_total == 0` AND `project_sorry_count > 0` AND no plan.md exists | `plan` |
| trigger=`stall` (`stable_cycles >= _autonomous_stalled_limit()`, =4, :553) AND `routes_used_this_scope < LEANFLOW_ORCHESTRATOR_MAX_ROUTES` | `plan` (research runs) / `decompose` (if a queue item is active) |
| `routes_used_this_scope >= LEANFLOW_ORCHESTRATOR_MAX_ROUTES` OR K consecutive assignments exhausted (queue-level breakpoint, §4.3) | `park` — persists the decision packet + graph state, stop reason `parked` (N1: park is a *documented* outcome with the packet as the rigorous account, never a silent stop) |

Route execution lives in a second new leaf `leanflow_cli/workflows/orchestrator_exec.py`: `execute_route(route, ctx, *, agent, autonomy_state, live_state) -> RouteOutcome` — `direct-prove` returns immediately; `decompose` calls the decomposer role (§4.2); `plan` calls `_run_planner_phase` (Phase 5); `negate` dispatches the Phase-3b probe; `park`/`escalate`/`re-state` write graph + packet and return a stop directive.

**(d) Flags**: `LEANFLOW_ORCHESTRATOR_ENABLED` (default off — all three call sites are no-ops, hot path byte-identical), `LEANFLOW_ORCHESTRATOR_MAX_ROUTES` (default 4/scope), `LEANFLOW_QUEUE_BREAKPOINT_K` (default 3 consecutive exhausted assignments).

**(e) Test plan**: new `tests/leanflow/test_orchestrator.py` following the pure-module style of `tests/leanflow/test_queue_manager.py` (256 lines; direct dataclass construction, no I/O). Table-driven: one test per row of the route table; property: `route == "direct-prove"` whenever `attempt_count < 2` and trigger is scope-entry with a live queue. Wire-in tests extend `tests/leanflow/test_native_runner.py` (9,664 lines; heavy monkeypatch precedent per AGENTS.md "coupled to its tests by design"): flag-off ⇒ zero behavior change (characterization test first), flag-on stall ⇒ activity event `orchestrator-route` recorded via `_record_activity` (:723).

**(f) Acceptance**: flag off = byte-identical transcripts on `testdata/workflow_projects/ProveDemo`; flag on easy file = only `direct-prove` routes and zero extra LLM calls; induced budget exhaustion (AGENT_MAX_TURNS low) ⇒ decision packet persisted and route ∈ {decompose, negate, park} instead of today's silent re-entry (:6008 records + continues).

**(g) Risks**: (i) double-routing with the existing `route_workflow_step` recommended-worker path when `LEAN_WORKER_DISPATCH_ENABLED` flips true (lean_services.py:2069) — mitigate: orchestrator consumes `route_decision` and supersedes `recommended_worker` when enabled; (ii) stall-route livelock (route → no progress → stall → route) — bounded by `LEANFLOW_ORCHESTRATOR_MAX_ROUTES` and by `_autonomous_max_cycles()` backstop (:561); (iii) breakpoint interception must not swallow `manager_retry_exhausted`'s baseline restore — restore already ran at :1982 before the packet is built.

## 4.2 Decomposer role (graph-aware upgrade of `lean_decompose_helpers`)

### Verified backend contract
`lean_decompose_helpers_tool(theorem_id, file_path, *, theorem_statement="", current_diagnostics="", current_goals="", current_attempt="", recent_failed_attempts="", question="", cwd="", max_helper_count=6, timeout_s=1200) -> str(JSON)` (`tools/implementations/lean_experts.py:487–707`). Provider resolution `resolve_expert_provider("lean_decompose_helpers")` (:561) with config inheritance `lean_decompose_helpers → lean_reasoning` (auxiliary_client.py:72–74; config.py:68–77). Response JSON: `{success, status, obstacle_summary, recommended_split, insertion_guidance, first_concrete_next_edit, helpers:[{name, purpose, lean_skeleton, dependencies, proof_hints, insertion_point, check_status, ready_to_insert, check_diagnostics, validation_order}], skeleton_validation:{status, validated_count, ready_count, allows_sorry_warnings}, next_step}` (:691–706). `ready_to_insert` is set by `_validate_helper_skeletons` (:364–438): cumulative prefix validation — each skeleton is checked via `lean_incremental_check(action="check_target", replacement="\n\n".join([*accepted_prefix, skeleton, target_sorry_skeleton]))` (:399–409), accepted iff `success and errors == 0` (:419). It **does not edit files** (schema text, lean_tool.py:663–665).

### The upgrade — new leaf `leanflow_cli/workflows/decomposer.py`

**(b) Signatures**
```python
def run_decomposer(ctx: RouteContext, *, mgr: TheoremQueueManager, agent: Any,
                   autonomy_state: dict, plan_state: PlanState) -> DecomposeOutcome:
    """Full decomposer role: propose (lean_decompose_helpers_tool) -> place (state stubs into files)
    -> validate (lean_incremental_check per stub) -> graph (stated nodes + split_of/depends_on edges)
    -> seed queue (mgr.replace_queue / assign) -> refresh guard snapshots."""

def state_helpers_into_file(helpers: list[dict], *, target_file: str, placement: str,
                            project_root: str) -> StubPlacement:
    """Insert ready_to_insert skeletons before the target declaration (same file) or create
    Project/Generated/<Topic>.lean (new file); direct Path.write_text — the decomposer is a
    runner-level actor, not a prover tool call."""

def refresh_queue_edit_guard(agent: Any) -> None:
    """Invalidate the two per-agent guard caches after any out-of-turn orchestrator/decomposer edit."""
```

**File-writing path**: direct Python writes (`Path.write_text`), matching the runner's own restore writers (`_restore_changed_protected_declarations` / `_restore_assigned_declaration_against_before_text`, `leanflow_cli/workflows/queue_edit_guard.py:182–223`). Not `apply_verified_patch` — that is a prover-turn tool with guard callbacks. Edits happen strictly **between prover turns** (invocation points in §4.1 are all outside `_run_managed_conversation`).

**Guard-snapshot refresh seam (verified)**: the queue edit guard has two caches that go stale after out-of-turn edits, plus one that self-heals:
- `agent._managed_queue_edit_guard_state` — protected-declaration inventory + assigned-statement signature, cached per `guard_key=(symbol, file)` and only rebuilt when the key changes (native_runner.py:2776–2793). A decomposer insert into the active file adds declarations this cache doesn't know ⇒ must reset to `{}`.
- `agent._managed_initial_declaration_keys_by_file` — per-file initial declaration keys cached forever under a `"__file__"` guard key (queue_edit_guard.py:102–121) ⇒ must reset to `{}`.
- `agent._managed_queue_edit_snapshot` re-reads the file from disk before every editing tool call (native_runner.py:2770) ⇒ self-heals; no action.

**Exact precedent for the reset**: `_run_document_formalization_review_agent` already zeroes both caches when constructing the reviewer (native_runner.py:5687–5688). `refresh_queue_edit_guard` does the same on the *parent* agent after decomposer writes. The assigned-statement guard keeps enforcing statement immutability for the prover afterward (`_queue_edit_assigned_statement_signature`, queue_edit_guard.py:131).

**Validation**: the final declaration in each contiguous placed batch is re-checked in-place via `lean_incremental_check(action="check_target", file_path=stub_file, theorem_id=tail_helper_name)`. LeanProbe elaborates every preceding segment while constructing the tail's environment, so this single gate covers the entire inserted batch without rebuilding successively longer prefixes. `skeleton_validation.allows_sorry_warnings=True` semantics remain preserved (sorry warnings OK, errors reject; on reject the whole write is reverted and the bounded Lean diagnostic is journaled).

**Graph**: each stated helper → node `{kind:"lemma", status:"stated", file, statement}` + edge `{from: helper, to: target, kind:"split_of"}` + `depends_on` edges from the helper's declared `dependencies`. Queue seeding: `mgr.replace_queue([...stubs as QueueItem mappings...])` (queue_manager.py:140) then the existing selection path assigns via `assign` (:181); `_flush_queue_manager` (native_runner.py:886) persists.

**(e) Tests**: extend `tests/leanflow/test_queue_edit_guard.py` (stale-cache reproduction: build guard state, insert a lemma out-of-turn, show `_queue_edit_changed_protected_declarations` false-positives without refresh, none with); new `tests/leanflow/test_decomposer.py` with a fake `lean_decompose_helpers_tool` payload (fixture pattern from `tests/leanflow/test_lean_incremental.py`) and tmp Lean files.

**(f) Acceptance**: after a decompose route, the file contains N stubs each individually `lean_incremental_check`-clean; `blueprint.json` has N `stated` nodes with `split_of` edges; the queue's next assignment is a stub; the prover's guard does not restore/flag the new stubs (refresh worked).

**(g) Risks**: helper name collisions with existing declarations (pre-check via `_declaration_line_index_from_text`, queue_edit_guard imports from `lean_parsing`); cumulative-prefix validation order must match file placement order (preserve `validation_order`); decomposer writing to a file the prover currently owns — forbidden by construction (between-turns only) plus ledger one-writer rule.

## 4.3 Queue ownership dimension (claim/lease on `TheoremKey`)

### Reuse/compat
Serialization round-trip: `TheoremQueueManager.from_autonomy_state` (queue_manager.py:681) / `to_autonomy_state` (:820–878) emits keys only when non-empty; owned keys enumerated in `OWNED_AUTONOMY_KEYS` (:97–108) and popped/updated by `_flush_queue_manager` (native_runner.py:886–897). `TheoremOutcome.status` today: `"solved" / "unresolved" / "skipped"` (queue_models.py:287).

### New (all additive)
- `queue_models.py`: `@dataclass(frozen=True) class ClaimRecord: key: TheoremKey; owner: str; job_id: str; status: str; lease_expires_at: str; note: str = ""` with `from_mapping/to_mapping` mirroring `FailedAttempt` mappers. Status vocabulary (mirrors graph §4.1): `"claimed" | "proving" | "blocked" | "parked" | "false" | "released"`.
- `queue_manager.py`: `claim(key, *, owner, job_id, ttl_seconds) -> ClaimRecord` (refuse if actively claimed by another owner and lease unexpired), `release(key, *, owner)`, `claim_for(key)`, `expired_claims()`; add `"queue_claims"` to `OWNED_AUTONOMY_KEYS` (:97) and to `to_autonomy_state`/`from_autonomy_state` (storage key = `key.storage_key()`, queue_models.py:69–72).
- **Compat story**: old checkpoints lack `queue_claims` → `from_autonomy_state` treats missing as `{}` (same tolerance it shows every optional key, :681–796); new checkpoints read by old code: the key is ignored (legacy code iterates only known keys) — additive-safe both directions. `assign` (:181) gains an optional `owner: str = ""` kwarg defaulting to today's behavior; single-agent runs never populate claims, so the hot path is unchanged.

**Tests**: extend `tests/leanflow/test_queue_manager.py` (round-trip serialization tests already there); invariant added to `check_invariants` (:644): a `proving` claim implies the key is `current` or queued. **Acceptance**: old-format autonomy state loads unchanged (existing golden tests stay green). **Risk**: lease expiry semantics with sync-only dispatch are trivial in v1 (parent releases in `finally`); document that async (Later/optional) needs reclaim.

## 4.4 Orchestrator LLM turn (spec now, enable in Phase 6)

### Reuse
- Call path: `run_model_verification_review(provider, task="orchestration", prompt, system_prompt, timeout_s, max_tokens)` (`leanflow_cli/workflows/verification_providers.py:160–239`) → `call_llm(task=..., provider=...)` (`agent/providers/auxiliary_client.py:984`); telemetry via `append_workflow_activity` free (:176–184, :226–238). Command-provider path `run_command_verification_review` (:97) comes for free (codex/claude-code as orchestrator).
- Decision parsing: reuse/extract `_extract_json_object` (`tools/implementations/lean_experts.py:292–309` — fence-tolerant) into a small shared leaf (e.g. `core/json_extract.py`) imported by both.
- Provider defaulting precedent: `blueprint_verification` defaults to `"main"` (config.py:78–87; `default_verification_provider`, verification_providers.py:64–67) — exactly the "strong model = main agent model" default D1 wants.

**Implemented latency/context contract:** the research profile shapes the advisory into a
target-scoped prompt capped at 12,000 characters. The exact assigned declaration, priority error
diagnostics, deterministic floor, and reply schema reserve space first. Graph facts, failed routes,
research findings, generated plan state, and phase policy each have explicit local caps; every
shortened or omitted history contributes its full-source SHA-256 plus character/item counts to the
prompt and `orchestrator-prompt-shaped` activity telemetry. The isolated synchronous consult then
has a twenty-second foreground ceiling; `LEANFLOW_ORCHESTRATOR_LLM_TIMEOUT_S` may lower but cannot
raise that research ceiling. One timeout persists a two-minute project-local circuit; subsequent
consult ticks keep the deterministic route floor without making a provider call, and a successful
half-open call resets the circuit. Non-research calls retain their separately bounded configurable
timeout. The circuit changes latency only, never route or verification authority.

### New
**Config plumbing (verified mechanism, corrected key names)**: add to `DEFAULT_CONFIG["auxiliary"]` (config.py:57): `"orchestration": {"provider": "main", "model": "", ...}`, `"planner": {...}`, `"manager_nudge": {"provider": "", "model": "", ...}`; add fallbacks `{"orchestration": "lean_reasoning", "planner": "orchestration", "manager_nudge": "lean_reasoning"}` to `_AUXILIARY_TASK_FALLBACKS` (auxiliary_client.py:72). Env overrides come free: `AUXILIARY_ORCHESTRATION_PROVIDER/_MODEL/_BASE_URL/_API_KEY` (:190–214). Update `DEFAULT_CONFIG_HEADER` docs (config.py:149+).

**Context assembly** (`orchestrator_llm.py` + `orchestrator_prompt_budget.py`):
- exact current target identity and a bounded head/tail declaration view;
- error-bearing diagnostic lines ahead of bounded general diagnostic context;
- deterministic floor route/reason and campaign/negation/fidelity counters;
- target dependency frontier separately from a compact campaign-global scheduling inventory;
- bounded decision-packet, verified-graph, failed-signature, completed-finding, generated-plan, and
  phase-policy sections, each carrying a digest/count record for its complete pre-projection source;
- historical user `plan.md` Notes are excluded entirely.

**Decision JSON schema** (the model must return exactly this; floor-fallback on parse failure):
```json
{
  "route": "direct-prove|decompose|plan|negate|park|re-state|escalate",
  "reason": "one paragraph grounded in the evidence",
  "target_node": "n42",
  "statements_to_state": [{"name": "...", "file": "Project/Generated/Bounds.lean",
                            "statement": "lemma ... : ... := by\n  sorry", "visibility": "private|public",
                            "depends_on": ["n17"], "notes": "why this split"}],
  "probes": [{"archetype": "negation|empirical|deep-search|decomposition", "objective": "...", "budget_api_steps": 40}],
  "park": {"until": "condition", "packet_note": "..."} 
}
```
Upgrade-only rule enforced in code: if the floor said anything other than `park/escalate`, an LLM `park/escalate` answer is logged and ignored (`decision_source="llm-downgrade-rejected"`); the kernel gate is untouched by construction (the LLM output never reaches `_manager_check_queue_item`).

**Shipped arithmetic preflight**: before a parsed LLM decision becomes route authority,
`orchestrator_arithmetic_preflight.py` checks only a conservative affine fragment. It expands
plain affine aliases, compares asserted affine identities, and modularly counterchecks claims
`d ∣ (a*t+b)` under a stated residue class. A supported contradiction rejects the LLM decision,
records the exact claim plus counterevidence in activity/campaign failed-route state, and retains
the deterministic floor route. Nonlinear, conditional, speculative, or otherwise ambiguous math
fails open; passing this preflight never certifies correctness and does not weaken the Lean gate.

**Prompt draft (system)** — embodies research-pusher + N1:
> You are the orchestrator of a Lean 4 proving harness attacking research-grade problems. The Lean kernel is the only authority on truth; you decide *strategy*. Difficulty is a routing signal, never a terminal state: when a goal resists, you split it into stated sub-lemmas, order a negation probe, commission an empirical experiment, or send a deep-search job into the literature — you do not lower ambition and you never conclude "too hard" without a concrete next artifact. Every scope you manage must end in exactly one of: a kernel-verified proof; a kernel-verified refutation (negation proved); or a parked state whose decision packet records precisely what was tried, what was learned, and the cheapest promising continuation. Silent surrender is a protocol violation. You may run multiple proving directions when the graph shows genuinely independent routes, but prefer probing and evidence-gathering before committing prover budget. Answer with the decision JSON only.

**Flag**: `LEANFLOW_ORCHESTRATOR_LLM_ENABLED` (separate from the Phase-4 floor flag). **Tests**: fake `call_llm` (pattern: `tests/leanflow/test_manager_verification.py`); parse-failure → floor route; downgrade-rejection test. **Acceptance**: with flag on and a rigged "stall" context, an LLM `decompose` answer produces stated stubs end-to-end. **Risks**: JSON drift across providers (fence-tolerant extractor + strict schema validation with floor fallback); token cost (invoked only at the three trigger points — never per cycle, per roadmap §7 row 1).

---

# PHASE 5

## 5.5 Planner phase — `_run_planner_phase`

### Template read-through (verified, native_runner.py:5661–5715)
The document-review sub-agent pattern: (1) `_build_agent()` builds a fresh `AIAgent` from env (:7789–7830); (2) parentage: `reviewer._parent_session_id = parent.session_id`, `_delegate_depth = parent+1` (:5684–5685); (3) shared `_managed_autonomy_state`, **fresh** guard caches (:5686–5688); (4) activity identity swap `_CURRENT_AGENT_ACTIVITY_DETAILS = _agent_activity_details(reviewer)` (:5691) restored in `finally` (:5713) along with `LEANFLOW_NATIVE_RUNNER_OWNER` (:5714–5715); (5) `_run_managed_conversation(reviewer, user_message=..., system_message=..., conversation_history=[])` (:5693–5701) — fresh bounded context; (6) results consumed as structure, not transcript: the wrapper `_run_configured_blueprint_verification` (:5495–5573) parses PASS/BLOCK (`_verification_review_decision`, :5536), stamps approval or queues a feedback message into `autonomy_state["document_formalization_review_feedback_message"]` (:5560) that the loop appends as a user turn (:9136–9140).

### Spec — new leaf `leanflow_cli/workflows/planner_phase.py`
```python
def run_planner_phase(ctx: RouteContext, *, agent: Any, system_prompt: str,
                      autonomy_state: dict, plan_state: PlanState) -> PlannerOutcome:
    """Fan out ≤3 sync research sub-agents (delegate_task batch), synthesize, merge into
    plan.md + blueprint.json, seed the theorem queue with stated stubs."""
```
Fan-out via `delegate_task(tasks=[...], parent_agent=agent, toolsets=...)` (delegate_tool.py:391; batch ThreadPool cap `MAX_CONCURRENT_CHILDREN=3` :489, matching roadmap "cap 3, sync") rather than five serial `_build_agent` clones — reuses interrupt propagation (`register_child`/`unregister_child`, :374–380) and child file-lock cleanup (:381–388). Two waves of ≤3 (web/mathlib/empirical, then draft/negation) if all five are requested; the orchestrator LLM decision's `probes` list selects which.

**Sub-agent set** (toolsets from `core/toolsets.py:44–134`):

| Sub-agent | Toolsets | Deliverable JSON (schema enforced by prompt; parsed with `_extract_json_object`) |
|---|---|---|
| web/literature | `["web"]` (`web_search`/`web_fetch`/`web_download`, :45–49) + `repo_clone` (§5.6) | `{"findings":[{"claim","source_url_or_path","relevance","candidate_lemmas":[...]}], "downloads":[paths], "repos":[paths]}` |
| mathlib | `["lean"]` (`lean_search`, `lean_lemma_suggest`, `lean_proof_context`; :85–89) | `{"candidates":[{"name","statement","module","how_it_helps"}], "gaps":[...]}` |
| empirical | `["terminal","lean"]` (python via `terminal`, Lean via `lean_incremental_check`/`lean_multi_attempt`); bounded pilot of at most 12 selected cases and two non-background terminal calls, each hard-clamped to 20 seconds | `{"hypothesis","method","result":"supports|refutes|inconclusive","evidence","counterexample":null\|{...}}` |
| draft | `["file","lean"]` | `{"stubs":[{"name","file","statement","depends_on"}]}` — statements must pass `lean_incremental_check`; prompt derived from current `draft.md`'s exit criteria (rewritten, §6.9) |
| negation | `["lean"]` scratch-only (LeanProbe) | `{"target","plausible_result","negation_attempted","negation_proved":bool,"proof_or_obstruction"}` |

**Synthesizer**: one `run_model_verification_review(task="planner_synthesis", provider=resolve auxiliary.planner)` turn over the ≤5 deliverables + goal; output = a plan.md patch (`## Grounding`, `## Strategy`, `## Frontier`) + graph delta (nodes/edges JSON). Merge: `plan_state.apply_delta` (atomic `write_json_file` path, workflow_state.py:305 pattern); queue seeding through the decomposer's `state_helpers_into_file` + `mgr.replace_queue` (§4.2) so *all* stub-stating flows through one code path. Premise-retrieval pre-step (roadmap Phase 5): prepend `lean_lemma_suggest` output (`leanflow_cli/lean/lean_lemma_suggest.py:324`, registered lean_tool.py:843) to each seeded item's `search_hints` (`QueueItem.search_hints`, queue_models.py:85).

The synchronous planner is supervised by `native/parent_maintenance.py`: planner work executes in a worker while the native runner's process-owning thread keeps polling the background research portfolio. This prevents completed dispatch children from holding capacity until planner synthesis returns.

The table's empirical terminal budget describes the synchronous planner pilot. Production
background empirical JobSpecs may retain legacy `terminal` in their persisted request, but dispatch
filters it out and delegates only `lean-research` plus `empirical-compute`. `empirical_compute` accepts only
an AST-restricted integer/Fraction subset in a fresh process with a 1–8 second hard timeout and
source/output/memory limits. `LEANFLOW_DISPATCH_ARCHETYPE=empirical` plus the scratch-worker flags is
required both for schema exposure and handler execution; no other archetype can invoke it.

**Flags**: `LEANFLOW_PLANNER_ENABLED`, `LEANFLOW_PLANNER_MAX_SUBAGENTS` (default 3). **Tests**: fake `delegate_task` returning canned deliverables (pattern: `tests/tools/test_delegate.py`, 864 lines, has mock-agent fixtures); synthesizer fake via monkeypatched `run_model_verification_review`. **Acceptance**: a `plan` route on a bare goal yields plan.md with all three sections, ≥1 stated stub, queue non-empty. **Risks**: sub-agent transcript ingestion bloat — forbidden: parent consumes only the JSON deliverable (`summary` field of delegate result, delegate_tool.py:338–351); deliverable-parse failure → that lane contributes an empty result and is recorded in the ledger as `failed`, never lost (N1).

## 5.6 `repo_clone` tool — `tools/implementations/repo_clone.py` (new)

**Mirror**: `web_download_tool` (`tools/implementations/web_fetch.py:237–319`) — dirname constant, basename sanitizer, sandbox escape check via `parents` (:275–276), cap-abort-cleanup (:297–300), JSON response via `tools.response.dumps/error`, registration (:391–401). Layering-safe: imports only stdlib + `tools.response` + `tools.registry` (tools/ layer, no `leanflow_cli` imports — matches web_fetch).

```python
REPO_CLONE_DIRNAME = ".leanflow/workspace/repos"
REPO_CLONE_MAX_BYTES = 500 * 1024 * 1024  # post-clone du cap

def repo_clone_tool(url: str, name: str = "", ref: str = "", max_bytes: int = REPO_CLONE_MAX_BYTES) -> str:
    """git clone --depth 1 --single-branch [--branch ref] into <project>/.leanflow/workspace/repos/<name>;
    sanitized dir name (web_fetch.py:241 pattern), https/git URL allowlist, subprocess with timeout,
    post-clone size check (delete + error if over cap), returns {success,url,path,bytes,ref,head_commit}."""
```
Schema: `{url: required, name: optional, ref: optional}`. Registration: `registry.register(name="repo_clone", toolset="web", ...)` so it rides the existing `web` toolset (core/toolsets.py:7 `_WEB_TOOLS` gains `"repo_clone"`) — available to orchestrator/planner/deep-search jobs; the inner prover keeps it reachable only because `leanflow-prove-worker` includes `web` (:127) — acceptable per roadmap §4.6 ("research reachable"), or exclude by introducing a `research` toolset if the owner prefers (decision point).

**Idempotency**: existing target dir with a `.git` → return success with `"cached": true` (no re-clone). **Flags**: `LEANFLOW_REPO_CLONE_MAX_BYTES` override. **Tests**: `tests/tools/test_repo_clone.py` mirroring `tests/tools/test_web_fetch.py:216–266` (tmp_path cwd monkeypatch; a local bare fixture repo made with `git init` in tmp — no network; sanitize test `name="../../etc"`; cap test with a large blob). **Acceptance**: clone of a local fixture lands under `.leanflow/workspace/repos/<name>`, `lean_search(mode=local)` can grep it. **Risks**: git subprocess availability (check_fn probes `git --version`); submodules ignored (`--depth 1`, no `--recurse`) — documented; disk pressure (cap + activity event).

## 5.7 Prover job shape A — nested file-scoped `/prove`

### Investigation verdict (with evidence): **`spawn_workflow` subprocess — recommended**; `delegate_task` child **cannot** host the machinery.

- `delegate_task` children are plain `AIAgent.run_conversation` threads (`_run_single_child`, delegate_tool.py) — no queue, no manager gate, no `_review_agent_final_report`. Reusing "the full existing machinery" in-process is impossible because the native runner is process-scoped: config exclusively from env (`_build_agent` hard-exits without `LEANFLOW_NATIVE_MODEL`, native_runner.py:7802–7805), module-level mutable globals (`_CURRENT_AGENT_ACTIVITY_DETAILS` :5667), and `os.environ` mutations (`LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS` :1081; `LEANFLOW_NATIVE_RUNNER_OWNER` :5715; run-id minting `os.environ["LEANFLOW_WORKFLOW_RUN_ID"]` workflow_state.py:289).
- `spawn_workflow(command, *, active_cwd, active_skill, interactive=False)` (workflow.py:564–589) already launches exactly the needed process: `python -m leanflow_cli.native.native_runner`, `cwd=project.root`, `start_new_session=True`; a file argument makes the run file-scoped (`LEANFLOW_NATIVE_ACTIVE_FILE`, :497) which is precisely `_single_queue_item_turn_enabled()` (`autonomous AND ACTIVE_FILE set`, native_runner.py:449–450) — the full queue + gate engages. The shell already spawns background runs this way (`shell.py:823`), and non-interactive headless exit is handled (child exits 0 on verified, :9419–9431; headless no-TTY path writes a pre-exit checkpoint instead of blocking).

**Env conflicts found (must fix in the wrapper)** — `child_env = dict(os.environ)` (workflow.py:476) leaks parent state when the parent *is* a native run:
1. `LEANFLOW_WORKFLOW_RUN_ID` — inherited ⇒ child logs into the parent's run stream (`_workflow_run_id` only mints when unset, workflow_state.py:276–279). Fix: **pop** it and **set** `LEANFLOW_WORKFLOW_PARENT_RUN_ID` (already read at workflow_state.py:349–353 and persisted in run metadata — free N3 substrate).
2. `LEANFLOW_NATIVE_RUNNER_OWNER` — parent's owner id would make the child's exit release the parent's file locks (`_persist_live_status` releases on phase "exited" by owner, native_runner.py:622–625). Fix: pop.
3. `LEANFLOW_FORMALIZATION_*` — would trip document-formalization gates in the child (`_document_formalization_requested` reads env). Fix: pop the family.
4. `live_status.json` is a per-project singleton (workflow_state.py:98–99, root = `<project>/.leanflow/workflow-state`, workflow_state_paths.py:41–49) — parent and child both write it. Since v1 is synchronous-blocking, fix cheaply: parent suppresses `_persist_live_status` while awaiting the child (context flag), so the child owns the file; parent resumes and overwrites on completion. Activity streams are safe (per-run files + flock, workflow_state.py:48–66).
5. File locks: advisory registry per project (`runtime/file_locks.py:28,137`); parent acquires the stub file lock under the **job id** before spawn (precedent: `dispatch_worker`, lean_worker_dispatch.py:56–63, ttl 1800) and releases in `finally`; the ledger's one-job-per-file rule is the real guard.

### Spec — new leaf `leanflow_cli/workflows/prover_jobs.py`
```python
def launch_stub_prove_job(spec: JobSpec, *, plan_state: PlanState) -> JobResult:
    """Shape A: spawn_workflow(f"/prove {stub_file_rel}") with hygienic child env
    (pop RUN_ID/RUNNER_OWNER/FORMALIZATION_*, set PARENT_RUN_ID + LEANFLOW_JOB_LINEAGE),
    ledger 'deployed'->'running', synchronous process.wait() with patience from
    spec.budget.wall_clock, kill via post_workflow_agent_input(kind='exit') then
    process.terminate(); consume results one-way from disk."""
```
- **Tracking**: pid from `Popen` (shell.py:829 precedent); child appears in `summarize_workflow_agents` (workflow_state.py:651) with `parent_run_id`; ledger entry `summary.json.dispatch_ledger[]` = `{job_id, lineage, archetype:"prover-A", stub_file, node_ids, pid, run_id, state, started_at, budget, result}` with states `proposed→deployed→running→{done|failed|stuck|killed}` (roadmap §4.2), reconciled against run metadata + `_process_seems_alive` (used by `post_workflow_agent_input`, workflow_state.py:588–591).
- **N3 lineage**: `job_id = f"{parent_lineage}.pv-{seq:03d}"` (e.g. `orchestrator.planner.pv-002`); passed as `LEANFLOW_JOB_LINEAGE`; `append_workflow_activity` gains a `job_lineage` detail (additive kwarg — no schema break); ancestor ops = ledger prefix scans; kill-descendants reuses `terminate_workflow_agent_descendants` (workflow_state.py:983) + ledger-prefix terminate.
- **Result consumption (one-way)**: on exit, parent runs the kernel gate itself — `_manager_verify_queue_file(stub_file)` / per-decl `lean_incremental_check` — and reconciles graph node statuses (`proved` only from the gate); never ingests the child transcript.

**Flags**: `LEANFLOW_DISPATCH_ENABLED` (Phase 3b), `LEANFLOW_PROVER_JOB_WALL_CLOCK_S` (default 3600). **Tests**: unit-test env hygiene by asserting on the constructed `child_env` (monkeypatch `subprocess.Popen`; pattern in `tests/leanflow/test_workflow_swarm.py`); integration on `testdata/workflow_projects/ProveDemo` gated behind a slow marker. **Acceptance**: a 2-stub file proves via a nested job; ledger shows `done`; parent graph flips both nodes `proved` via its own gate; parent live status intact afterward. **Risks**: orphaned child on parent crash — ledger records pid, scope-entry reconciliation kills stale `running` entries whose parent run is gone; LeanProbe/LSP double-warm memory pressure (two REPLs) — document; child auto-swarm is prevented because file-scoped prove forces `parallel_agents=1` (workflow.py:447–452).

## 5.8 N4 multi-direction proving

No new machinery beyond §5.7 + graph. `orchestrator_exec.execute_route` accepts `statements_to_state` grouped into ≤`LEANFLOW_MAX_PROVE_DIRECTIONS` (default 3) stub files — one file per direction (`Project/Generated/<Topic>_dirA.lean`…), each direction's nodes tagged `{"direction": "dirA"}` and `split_of`→goal edges. v1 runs the jobs **sequentially** (sync cap already 3; roadmap D2). Merge protocol (mechanical, graph-only): after each job, reconcile; first direction whose full node set is `proved` ⇒ goal route chosen; orchestrator marks sibling directions' unproved nodes `parked` (files kept — they are documentation, N1) and records the choice in `plan.md § Decisions`. If all directions exhaust ⇒ each has a decision packet ⇒ orchestrator routes `negate` or `park` with the packets as the rigorous account. Test: fake two directions where the second proves; assert first is parked, not deleted, and the goal node's `depends_on` rewires to the winning direction.

---

# PHASE 6

## 6.9 Spec-quality audit and rewrite

### Audit (all files read end-to-end; `leanflow_specs/workflows/`, sizes: checkpoint 88, draft 47, golf 97, refactor 102, review 101, search 50, prove 194, formalize 204 lines)

**Structural finding that makes golf/refactor/review/draft "not super functional" (verified, concrete):** `AUTONOMOUS_WORKFLOW_KINDS = {"prove", "formalize"}` (native_runner.py:93). For every other kind, `_drive_autonomous_followups` returns immediately (:9068–9070) — golf/refactor/review/draft execute as **one un-managed startup turn**: no queue, no manager gate, no retry ladder, no stop-condition enforcement. Their frontmatter `stop_conditions: [verified, blocked]` (golf.md:9, refactor.md:9) and "Verification Ladder" sections are prose no runner code enforces. Routing exists (`WORKFLOW_ALIAS_MAP` via `COMMAND_REGISTRY`, `leanflow_cli/cli/commands.py:40–76`; `default_workflow_skill` → `lean-refactor-golf`/`lean-diagnostics`, `runtime/skill_core.py:251–263`; `route_workflow_step` short-circuits for review/checkpoint/refactor/golf, lean_services.py:2044–2063) — but execution is a single turn.

Per-spec defects:
- **golf.md** — no baseline/rollback contract (a bad "simplification" that breaks the proof has no `_restore_...`-style safety; the queue-item restore machinery only runs in managed prove); no measurable improvement metric (no before/after proof length, `lean_profile_proof`-style cost, or maxHeartbeats delta); `stop_conditions` unenforced; no handoff artifact schema; frontmatter `workers: []` and no `review_actions` — nothing machine-consumable.
- **refactor.md** — states "do not use for files with `sorry`" (:30-ish) but nothing gates it (no pre-flight `lean_inspect` enforcement); mentions "worker support" (line "check whether semantic search and worker support are available") though `LEAN_WORKER_DISPATCH_ENABLED=False` (lean_services.py:140) — dead reference; no tie-in to `lean_decompose_helpers` for helper extraction (its actual best tool); no batch/revert protocol implementation.
- **review.md** — defines `next_action ∈ {continue, deep, repair, redraft, golf, stop}` (:66–83) while prove.md frontmatter declares `review_actions: [continue, replan, redraft, falsify, stop]` (prove.md:10) — **two conflicting vocabularies, neither consumed by any code** (no `next_action` consumer in `leanflow_cli/`); "read-only by default" is unenforced (review runs with the full `leanflow-native` toolset incl. write tools, workflow.py:469–473); no JSON output contract.
- **draft.md** — 47 lines; exit criteria unverifiable ("signatures are stable"); no `sorry`-stub contract, naming/placement policy, or graph linkage — all of which the decomposer (§4.2) now needs; redundant once orchestrator states lemmas.
- **search.md** — helper-kind, OK-ish but the actual search budget rules live in prove.md (:56–57, the 3-empty-search rule) — split-brain; no handoff schema.
- **checkpoint.md** — deleted in Phase 1 (roadmap D8), UX seams verified at native_runner.py:3788.

### Rewrite spec
**Mechanism — phase fragments**: extend `VALID_SPEC_KINDS` with `"phase"` (`lean_workflow_specs.py:20`); loader/validator already generic (`_load_spec` :87 reads any frontmatter; kind check :92). New dir `leanflow_specs/phases/{search.md,draft.md,review.md,negation.md,planning.md}` with frontmatter `kind: phase`, plus new fields `consumed_by: [orchestrator|planner|decomposer|prover]` and `deliverable_schema:` (YAML block describing the JSON the phase must emit) — parsed via two new tuple fields on `LeanSpecRecord` (:23–37, additive). Composition: planner/decomposer sub-agent prompts embed the fragment body via `get_lean_spec(phase_id).content` (:130), exactly how `build_skill_prompt` embeds workflow specs today (`runtime/skill_core.py:267+`). `validate_lean_specs` (:157) gains: every `consumed_by` value known; every phase declares `deliverable_schema`.

Per-spec rewrite outline:
- `search.md` → `phases/search.md`: owns the empty-search budget (moved out of prove.md), provider-order decision tree, and a `deliverable_schema` = the deep-search findings JSON (§5.5) — one contract for the prover pre-step and the deep-search job.
- `draft.md` → `phases/draft.md`: sorry-stub contract (skeleton shapes from `_target_sorry_skeleton`, lean_experts.py:312–324), placement policy (§4.5), naming rules, graph-node emission schema.
- `review.md` → `phases/review.md`: **one** action vocabulary aligned to the orchestrator route enum (`continue|decompose|plan|negate|re-state|park`, replacing both old lists; prove.md's `review_actions` updated to match); JSON verdict contract (PASS/BLOCK style, parsed like `_verification_review_decision`); toolset pinned read-only (`["lean"]` minus patch tools).
- `checkpoint.md`: **delete**; drop `"checkpoint"` from `WORKFLOW_TASK_LABELS` (workflow_state.py:76) and `COMMAND_REGISTRY`; keep git-shadow safety (`native_checkpoints.py`) untouched.
- `golf.md` / `refactor.md` (**standalone, functional overhaul**) — **DESCOPED at implementation to a flag-free substrate.** Adversarial review established the runtime wiring needs a drain-to-done queue lifecycle, baseline capture *at assignment* (queue-item extras do not survive `QueueItem` serialization), and metrics on *classified* acceptance rather than raw gate ok. So NO `LEANFLOW_GOLF_MANAGED` flag and NO metrics recorder ship; `golf_mode.py` provides only the settled substrate (the sorry-free golf queue, `declaration_chars`, and the `golf candidate` selection bucket strictly after diagnostics/sorries, prove selection byte-identical), and `golf.md`/`refactor.md` carry a "Managed Mode (planned)" note naming these prerequisites. **The remainder of this bullet is the SUPERSEDED original design, retained only as the follow-on's blueprint — none of it (the `LEANFLOW_GOLF_MANAGED` flag, the managed loop, the golf-mode gate, the per-declaration metric, the "all compiling declarations" queue) ships in this wave.** The superseded design: remain `kind: workflow` but gain real machinery — add both kinds to `AUTONOMOUS_WORKFLOW_KINDS` gated by `LEANFLOW_GOLF_MANAGED=1` so they run the managed loop with a **golf-mode gate**: manager check = existing kernel gate PLUS "statement signature unchanged" (reuse `_queue_edit_assigned_statement_signature`, queue_edit_guard.py:131) PLUS "no regression" (baseline snapshot + restore reusing `_restore_assigned_declaration_against_before_text`, :210); acceptance metric recorded per declaration (`{before_chars, after_chars, before_elapsed_s, after_elapsed_s}` from the verification record's `elapsed_s`, native_runner.py:1251–1255); queue = all compiling declarations in scope, seeded like prove. refactor.md additionally routes helper extraction through `lean_decompose_helpers` and the §4.2 stating path. Both specs rewritten to the prove.md quality bar (explicit tool order with anti-patterns, enforced ladder, JSON handoff).
- `prove.md`: gains an `## Orchestration` section documenting triggers/routes/artifacts (plan.md, blueprint.json, decision packets) so the prover knows the artifacts exist (roadmap D9 "every deployed agent is made aware").

**Tests**: `tests/leanflow/test_lean_workflow_specs.py` (142 lines) extended: `kind: phase` load/validation, alias-collision, `consumed_by` validation; golden test that every phase fragment parses and every `deliverable_schema` is valid YAML. **Acceptance**: `validate_lean_specs()` returns `[]`; the shipped golf substrate is covered by `tests/leanflow/test_golf_mode.py`. (The `/golf` per-declaration metrics artifact and never-breaks-the-build restore path are DEFERRED with the managed-golf runtime — see the golf/refactor descope note above.) **Risks**: `_load_spec` kind inference from parent dir (`path.parent.name.rstrip("s")` :91) makes `leanflow_specs/phases/` infer `"phase"` automatically — but any stray `.md` under `leanflow_specs/` with an unknown dir now raises (:92–93); add explicit `kind:` frontmatter to all fragments.

## 6.10 Research-pusher pass + `LEANFLOW_RESEARCH_MODE`

> **Promoted behavior supersedes the finite-stop design below.** Research is a public CLI profile,
> not a manually assembled flag set. The per-context 120-cycle count is an epoch boundary. Route
> exhaustion and context pressure also roll epochs. Difficulty cannot park or terminate; parking
> is limited to fidelity/human-approval pauses. The mathematical terminal states are verified or
> authoritatively disproved, with explicit cancellation and infrastructure pauses recorded as
> process lifecycle states rather than mathematical conclusions.

### Grep results — every site that can terminate without a concrete result, or that injects give-up vocabulary (verified):

**Stop machinery (behavioral):**
1. `_autonomous_stop_reason` "blocked" stop: blocker phrase + stalled signature, `blocked_runs >= _autonomous_blocked_limit()` (=3) — native_runner.py:8913–8917.
2. `"stalled"` stop: `stable_cycles >= _autonomous_stalled_limit()` (=4) — :8921–8922.
3. `"blocked"` on unresolved outcomes even when verified: `_has_unresolved_theorem_outcomes` — :8894–8897.
4. Hard cycle ceiling 120 (`_autonomous_max_cycles`, :561–569; enforcement :9078–9092).
5. `_extract_blocker_summary` trigger tokens `["blocked","blocker","stuck","cannot proceed","can't proceed","unable to","failed to"]` — `leanflow_cli/native/native_utils.py:183–205` (feeds 1 and the struggle detector).
6. Retry exhaustion → `exit_reason="manager_retry_exhausted"` + restore (:1982–2006) — today rolls on; becomes a breakpoint (§4.1).
7. `_handle_api_step_budget_exhaustion` (:6008) — today records + continues; becomes a breakpoint.
8. Iteration-budget pressure strings in `run_agent.py`: thresholds 0.7/0.9 (:511–512), `_get_budget_warning` "BUDGET WARNING … respond now" (:2838–2860) — tone pushes wrap-up, not routing.

**Prompt vocabulary (text):**
9. Continuation prompt: "Do not stop until that gate is satisfied **or you have a concrete blocker to report**" — native_runner.py:8960 (plus :8967, :9010 variants).
10. ":1695 'so solve it or report a concrete blocker'"; ":4635 'your next move should be an edit, `lean_verify`, or a concrete blocker report'".
11. Final-sweep **"bail clause"** (:5785–5789) — literally invites bailing after reading the file.
12. Checkpoint labeling "blocker checkpoint"/"blocker-declared" (:8450).
13. `prove.md:56` "stop searching … or escalate the blocker"; `:153` "before reporting a concrete blocker"; `:165` "report a concrete blocker if another edit would only repeat failed proof shapes"; Stop Conditions "a concrete hard blocker has been recorded and another focused attempt is not justified".
14. `review.md:81` `stop` = "no credible next path … without user intervention"; `formalize.md:191` "a concrete blocker has been recorded and the next action is a handoff".
15. `leanflow_skills/lean-theorem-queue-worker/SKILL.md:25,47,78` — "until it is solved or a concrete blocker is proven", "report a blocker instead of claiming success", "Stop and report a blocker when:".

**Change spec per site** (principle: a blocker report must *carry a route request*, and in research mode the routable stops are disabled):
- Sites 9,10,13,14,15: reword from "report a concrete blocker" → "report a blocker **with a requested route** (`decompose` | `negate` | `plan` | `experiment`) and the evidence for it"; the LLM-manager/orchestrator consumes the requested route as a suggestion (N2: prover only escalates; manager only suggests).
- Site 11: bail clause allowed only after the one mandated read+edit attempt (keep — it is a warning-only cosmetic pass; not a give-up on proving).
- Sites 1,2: under `LEANFLOW_RESEARCH_MODE=1`, "blocked"/"stalled" do not return as terminal stop reasons; they become orchestrator triggers (§4.1 site 2). Only `verified`, kernel-`false` (negation proved on the goal), explicit user interrupt, and orchestrator `park` (with packet) remain terminal — implementing N1's closed set {proved, disproved, rigorous account}.
- Sites 4,8: research mode multiplies ceilings: `AUTONOMOUS_MAX_CYCLES` ×4 (env already overridable, :566), `AGENT_MAX_TURNS` from config `agent.max_turns` (config.py:101) ×2 for prover jobs; budget-pressure strings swap "respond now / wrap up" for "checkpoint your findings into the decision packet, then continue or escalate a route request" via a research-mode branch in `_get_budget_warning` (run_agent.py:2838) — message text only, budget math unchanged.
- Sites 6,7: already converted to breakpoints by Phase 4 — research mode makes `park` require a packet with a non-empty `next_candidate_route` field.

**Implementation**: one new leaf `leanflow_cli/workflows/research_mode.py` — `research_mode_enabled() -> bool`, `effective_stop_reason(reason: str) -> str` (maps `stalled/blocked → orchestrator-trigger` when enabled), `research_budget_multipliers() -> Multipliers`; call sites: `_autonomous_stop_reason` return points (:8917, :8922), `_autonomous_max_cycles` (:561), `_get_budget_warning` (run_agent.py:2838 — a parameter/env read, keeping run_agent untouched apart from the message-string branch). Spec/skill text edits per above.

**Flag semantics summary** — `LEANFLOW_RESEARCH_MODE=1`: budgets raised (cycles ×4, prover-job turns ×2, planner sub-agent `max_iterations` 50→100 via `delegate_task(max_iterations=...)`, delegate_tool.py:426); stop reasons `stalled`/`blocked` disabled as terminal (routed instead); parking keeps the scope alive under the background control loop (`_run_background_control_loop`, :8230 — already an inbox-waiting state machine, phase "paused") so a human or a later orchestrator turn can resume; orchestrator/planner context assembled un-compressed (N5).

**Tests**: unit tests for `effective_stop_reason` matrix; a native_runner test that a rigged stalled signature with research mode on yields an `orchestrator-route` activity instead of `autonomy-stop: stalled` (extend the stop-reason tests in `tests/leanflow/test_native_runner.py`). **Acceptance**: with research mode off, all existing stop-reason tests unchanged; on, no run terminates with `stalled`/`blocked` — terminal set is exactly {verified, negation-proved, parked+packet, interrupted}. **Risks**: runaway cost — mitigated by the retained hard ceiling (raised, not removed), `LEANFLOW_ORCHESTRATOR_MAX_ROUTES`, and park-with-packet as the pressure valve; prompt rewording regressions on non-research runs — reword is shared but the *behavioral* changes are all research-mode-gated.

---

## Cross-cutting sequencing note
Phase 4 depends on Phase-1 artifacts (`plan_state.py`: blueprint.json graph, decision packets) and Phase-3b dispatch (negation route); both are specified in the roadmap and referenced here by their intended module names. Everything above is additive and dark-launchable: `LEANFLOW_ORCHESTRATOR_ENABLED`, `LEANFLOW_ORCHESTRATOR_LLM_ENABLED`, `LEANFLOW_PLANNER_ENABLED`, `LEANFLOW_DISPATCH_ENABLED`, `LEANFLOW_RESEARCH_MODE` all default off (`LEANFLOW_GOLF_MANAGED` was descoped — see the golf/refactor note above; no such flag ships); with all off, the only diffs are new unused modules, new config keys with empty defaults, and spec text — the prove hot path (`:9059` loop, `:1890` gate, `:1210` checker) is byte-identical.
---

# PART IV — Existing-Assets Harvest (the reuse inventory)

All verification done. Here is the deliverable.

---

# LeanFlow `/prove` Redesign — EXISTING-ASSETS HARVEST (implementation-ready reuse inventory)

**Scope:** the definitive inventory of everything already in the tree that the redesign (roadmap `docs/prove-redesign-roadmap.md`, branch `docs/prove-redesign-roadmap`) composes, per component: orchestrator, planner, decomposer, LLM-manager, dispatch, plan-state/graph, negation probe, deep search, budget breakpoints, resume. Every claim below was re-verified against the working tree on this branch (repo root `/Users/lmilikic/Desktop/LeanFlow`). All paths are repo-relative; all line numbers are current as of this audit. §9 lists roadmap line-citation drift corrections; §10 is the new-code-only list.

**Reading key — "How reused":** *as-is* = call it unchanged; *thin wrapper* = new leaf module calls it with a fixed contract; *adapt* = the named change to the existing function is required (and only that change).

---

## 1. Agent-run substrate

### 1.1 `AIAgent` + `run_conversation` — the only agent engine

- **Asset:** `class AIAgent` — `run_agent.py:242`; `AIAgent.run_conversation(self, user_message, system_message=None, conversation_history=None, task_id=None, stream_callback=None, persist_user_message=None) -> dict[str, Any]` — `run_agent.py:3166`.
- **Today:** runs one full tool-calling turn loop to completion; returns `{completed, interrupted, messages, api_calls, exit_reason, error, ...}`. Owns iteration budget (`IterationBudget`, `run_agent.py:121/363`), retry counters, streaming, provider adapters.
- **Reused by:** every LLM-driven role. Orchestrator, planner, decomposer, and all dispatch job shapes are *fresh `AIAgent` instances with a role prompt and a scoped toolset* — no new agent framework.
- **How:** as-is. Role construction copies the existing sub-agent recipes (§1.3, §1.4).
- **Gotcha:** `run_conversation` resets per-turn counters and `iteration_budget` at turn start (`run_agent.py:3208+`) — a shared parent budget must be passed in via the `iteration_budget` ctor arg (delegate_tool already does this, `delegate_tool.py:210/249`). Behavior pinned by `tests/test_run_agent.py` (3,100 lines) and `tests/test_run_conversation_schema.py`.

### 1.2 `_run_managed_conversation` — interruptible turn wrapper

- **Asset:** `_run_managed_conversation(agent, *, on_interrupt=None, **kwargs) -> dict` — `leanflow_cli/native/native_runner.py:7930`. Runs `agent.run_conversation()` on a daemon thread with Ctrl-C-safe interrupt handling.
- **Reused by:** orchestrator/planner/decomposer turns inside the native runner (exact same wrapper the prover and the formalization review-agent use).
- **How:** as-is.
- **Gotcha:** it forwards `_managed_tool_task_id` into `task_id`; new roles that want isolated tool state (freshness hashes are keyed on `task_id`, §7.6) must set that attribute like the runner does.

### 1.3 `delegate_task` + `_run_single_child` — the in-process job executor

- **Asset:** `delegate_task(goal=None, context=None, toolsets=None, tasks=None, max_iterations=None, parent_agent=None) -> str(JSON)` — `tools/implementations/delegate_tool.py:391`; per-child spawn `_run_single_child(...)` — `delegate_tool.py:160`; caps `MAX_CONCURRENT_CHILDREN = 3`, `MAX_DEPTH = 2`, `DEFAULT_MAX_ITERATIONS = 50` — `delegate_tool.py:41-43`; batch execution via `ThreadPoolExecutor(max_workers=3)` — `delegate_tool.py:489`.
- **Today:** spawns child `AIAgent`s with credential resolution (`delegation.provider` config override; parent inherit fallback), toolset inheritance with blocked-tool stripping, child system prompt from `_build_child_system_prompt`, sets `child._delegate_depth = parent+1` and `child._parent_session_id = parent.session_id` (`delegate_tool.py:252-253`), registers child for interrupt propagation (`register_child`), shares the parent `iteration_budget`, suppresses child stdout.
- **Reused by:** **the dispatch service is a wrapper over this** — EMPIRICAL jobs, DEEP-SEARCH jobs, NEGATION probes, and PROVER job shape B are all `delegate_task` children with scoped toolsets. The roadmap's "sync v1, cap 3" is literally this function's existing semantics.
- **How:** thin wrapper (`JobSpec → delegate_task(tasks=[...])`), plus **one adapt** for N2/N3: today the child's lineage is only `parent_session_id`; the dispatch wrapper must pass the dotted job id (e.g. `orchestrator.planner.ds-042`) into the child's activity context (see §2.2/§10-D). No change to depth/cap logic in v1 — note `MAX_DEPTH=2` means a dispatched planner (depth 1) can spawn probes (depth 2 rejected → planner-launched jobs must be launched *by the runner process*, depth 0, not by a delegated planner; keep decision-making roles in-process as the roadmap's Phase 4/5 already assumes).
- **Gotcha:** returns a JSON *string*; results are transcript-shaped (`results[i].summary`) — the dispatch ledger must persist the deliverable itself, not rely on this return value surviving compaction. Pinned by `tests/test_interrupt_propagation.py`, `tests/test_cli_interrupt_subagent.py`, `tests/test_real_interrupt_subagent.py` (interrupt/registration behavior).

### 1.4 The in-runner phase-agent template (fresh agent, fresh history, activity-tracked)

- **Asset:** `_run_document_formalization_review_agent(parent_agent, system_prompt, live_state, autonomy_state) -> dict` — `native_runner.py:5661`; helper `_build_agent() -> AIAgent` — `native_runner.py:7789`.
- **Today:** builds a fresh reviewer agent, sets `_parent_session_id`, `_delegate_depth+1`, shares `_managed_autonomy_state`, swaps `_CURRENT_AGENT_ACTIVITY_DETAILS` so its events land under its own agent id, runs one `_run_managed_conversation` with an empty history and a purpose-built prompt, records `formalization-review-start/-complete` activity, restores parent context in `finally`.
- **Reused by:** **this is the template for `_run_planner_phase`, the decomposer role turn, and the LLM-orchestrator turn** (roadmap §4.9 "Phase pattern"). It demonstrates every required mechanic: lineage, activity attribution, fresh bounded context, managed interrupt.
- **How:** adapt-by-copy into the new leaf modules (house rules forbid piling more onto `native_runner.py`): extract the generic scaffold (build agent → set lineage → swap activity details → run → record events → restore) into the new `orchestrator`/`planner` leaf modules; keep the original untouched.
- **Gotcha:** it mutates the module-global `_CURRENT_AGENT_ACTIVITY_DETAILS` — the copied scaffold must import/set it via `native_runner` module attributes or events will mis-attribute. Pinned by `tests/leanflow/test_native_runner.py` (formalization-review tests) and `tests/leanflow/test_formalization_document_runner.py`.

### 1.5 `spawn_workflow` — out-of-process job launcher (PROVER job shape A)

- **Asset:** `spawn_workflow(command, *, active_cwd=None, requested_provider=None, active_skill=None, interactive=False) -> tuple[NativeLaunchPlan, Popen]` — `leanflow_cli/workflow.py:564` (**roadmap cites `:537` — drifted; now `:564`**); plan resolution `resolve_workflow_request(...)` — `workflow.py:394`; synchronous variant `run_workflow(...) -> int` — `workflow.py:592`.
- **Child env contract (verified, `workflow.py:476-522`):** `LEANFLOW_PROJECT_ROOT`, `LEANFLOW_NATIVE_{PROVIDER,API_MODE,BASE_URL,API_KEY,MODEL,REASONING_EFFORT,WORKFLOW_KIND,WORKFLOW_COMMAND,ACTIVE_SKILL,ADDITIONAL_SKILLS,PARALLEL_AGENTS,USER_APPROVED_SWARM,EXPLICIT_GOAL,USER_PROMPT,EFFECTIVE_PROMPT,TOOLSET,ACTIVE_FILE}`, optional `LEANFLOW_NATIVE_ALLOWED_AXIOMS`, `AUXILIARY_*` provider overrides, `AGENT_MAX_TURNS`; argv = `python -m leanflow_cli.native.native_runner`; spawned with `start_new_session=True` when non-interactive.
- **Reused by:** **PROVER job shape A** — "mini queue-prove over a stub file" is exactly `spawn_workflow("/prove Project/Generated/Bounds.lean")`: file-scoped `/prove` already forces single-agent (`workflow.py:449-452`), attaches a nearby `Blueprint.md` skill automatically (`workflow.py:459-466`), and selects the focused `leanflow-prove-worker` toolset (`workflow.py:469-471`).
- **How:** as-is for launch. The dispatch wrapper adds: pass the artifact paths + dotted job id via two new env vars (§10-D), record the child PID in the ledger, and poll `live_status.json`/outcomes for completion (all existing surfaces, §2).
- **Gotcha:** non-interactive children get `stdout/stderr=DEVNULL` — the *only* observability is the workflow-state activity/status files, which is why the ledger must reconcile against them (§2.2). Env contract pinned by `tests/leanflow/test_workflow_swarm.py` (`test_resolve_workflow_request_*`, incl. `_passes_configured_api_step_budget` at `:168`).

### 1.6 The toolset system (job-archetype tool scoping)

- **Asset:** `core/toolsets.py` — `TOOLSETS` registry `:44`; tool groups `:6-30` (`_WEB_TOOLS = [web_search, web_fetch, web_download]`, `_LEAN_TOOLS` incl. `lean_incremental_check`, `lean_lemma_suggest`, `lean_decompose_helpers`, `apply_verified_patch`); `leanflow-prove-worker` composite `:119-128` (file+web+terminal+skills+coordination+lean, no session/document tools); `resolve_toolset` `:141`; **`create_custom_toolset(name, description, tools, includes)` `:184`**.
- **Reused by:** every job archetype's tool scope. EMPIRICAL = `["terminal", "lean"]`-ish; DEEP-SEARCH = `["web", "file", "lean"]` + `repo_clone`; NEGATION = `["lean", "file"]` scratch; orchestrator/planner/decomposer = full search surface per roadmap §4.6.
- **How:** as-is — new named toolsets are *data*, registered via `create_custom_toolset` or added to `TOOLSETS` (e.g. `leanflow-deep-search`, `leanflow-empirical`, `leanflow-orchestrator`). `repo_clone` is one new tool name appended to `_WEB_TOOLS`-adjacent group.
- **Gotcha:** `tests/leanflow/test_toolsets.py` (14 tests) pins names/membership — extend, don't rename. Tool implementations must exist in `tools/implementations/` and be registered in `tools/registry.py` for the name to resolve.

---

## 2. Control & observability (the dispatch lifecycle substrate)

### 2.1 Agent inbox + background control loop (pause/kill/steer channel)

- **Assets:**
  - `_run_background_control_loop(agent, system_prompt, history, ...) -> int` — `native_runner.py:8230`: polls `read_workflow_agent_inbox(agent_id)` every 0.5 s; `kind == "exit"` terminates descendants + other agents and exits; any other message resumes a managed turn.
  - `enqueue_workflow_agent_message(agent_id, text, kind="message")` — `leanflow_cli/workflows/workflow_state.py:577` (rejects dead agents); `read_workflow_agent_inbox` — `workflow_state.py:490`; inbox path `workflow_agent_inbox_path` — `workflow_state.py:130`.
  - `terminate_workflow_agent(agent_ref)` — `workflow_state.py:949` (SIGINT); **`terminate_workflow_agent_descendants(agent_ref)` — `workflow_state.py:983`**: builds the child graph from `parent_agent_id` edges and recursively terminates; `terminate_all_workflow_agents` — `:1025`; `request_project_workflow_runner_exit` — `:1054`.
- **Reused by:** dispatch **kill** and **patience** duties (roadmap §4.2 "kill via the existing agent inbox"), budget-breakpoint queue interruption, and N3's "every ancestor can kill its descendants" — the descendant-termination primitive already exists.
- **How:** as-is for kill/exit. **One adapt for breakpoints:** the control loop only understands `exit` vs. free-text-resume; add a recognized `kind="breakpoint-decision"` (or reuse plain messages with a structured prefix) so an orchestrator decision can be injected into a paused runner. That change lives in the Phase-1/4 breakpoint module, not by growing the loop's inline logic.
- **Gotcha:** inbox consumers track `last_seq` in-memory only — a restarted runner re-reads the whole inbox; breakpoint decisions must be idempotent. Pinned by `tests/leanflow/test_workflow_status.py` (`test_enqueue_workflow_agent_message_rejects_dead_agent` `:739` etc.) and `tests/leanflow/test_shell_ui.py:792`.

### 2.2 Activity streams + agent registry (the "never lose a job" reconciliation source)

- **Assets:**
  - `append_workflow_activity(event_type, message, **details)` — `workflow_state.py:309`, writing via `_locked_append` (`:54`, thread-locked JSONL append) to per-agent files `activity/agents/<task>-<agent_id>.jsonl` (`workflow_agent_activity_path`, `:163`) plus per-run streams (`:149`).
  - Runner-side emitters: `_record_activity` — `native_runner.py:723` (stamps `parent_agent_session_id` from `agent._parent_session_id`, `:742`), `_record_agent_activity` — `:753`, `_record_turn_activity` — `:8150`.
  - `summarize_workflow_agents(*, activity_limit=5) -> list[dict]` — `workflow_state.py:651`: merges all activity events into per-agent summaries with `agent_id`, **`parent_agent_id`**, `task_label`, `workflow_kind/command`, `delegate_depth`, `model/provider`, `process_id`, `status` (with liveness check `_process_seems_alive` `:511`), api/tool call counts, last event. Detail/transcript views: `workflow_agent_detail` `:853`, `workflow_agent_transcript` `:860`.
- **Reused by:** the dispatch **ledger reconciliation** (`dispatch_ledger[]` entries are validated against these summaries so a job "can never be silently lost"), N3 lineage listing ("list/track descendants" = filter summaries by `parent_agent_id` chain), N1's rigorous account (the activity stream *is* the audit log), and the shell's `/status`/`/swarm` UX.
- **How:** as-is for reading. **Adapt (small):** dispatch jobs should stamp one extra detail field `job_id` (dotted lineage id) on their events — `append_workflow_activity(**details)` already accepts arbitrary details, so this is a call-site convention, not a schema change. `summarize_workflow_agents` needs one additive field pass-through.
- **Gotcha:** summaries are rebuilt by scanning *all* events (`_read_all_workflow_activity` `:479`) — cost grows with run length; the ledger should cache per-job status and use summaries only for reconciliation sweeps. Pinned by `tests/leanflow/test_workflow_status.py` (36 tests: status merging, dead-process detection, terminate flows) and `tests/leanflow/test_workflow_state_concurrency.py` (locked append under concurrency).

### 2.3 `live_status.json` (the heartbeat the ledger's patience policy reads)

- **Asset:** `_persist_live_status(history, compaction_state, checkpoint_state, live_state, *, phase="")` — `native_runner.py:609`: builds `{version, updated_at, phase, declaration queue, verification state, goals/diagnostics, proof metrics}` and writes through `save_workflow_live_status` — `workflow_state.py:305` → atomic `write_json_file` — `leanflow_cli/workflows/workflow_json_io.py:31`; reader `load_workflow_live_status` — `workflow_state.py:297`; path `workflow_live_status_path` — `workflow_state.py:98` (root resolution: project `.leanflow/workflow-state/` else `~/.leanflow/workflow-state/`, `workflow_state_paths.py:45-49`).
- **Reused by:** plan-state persistence (Phase 1 writes `plan.md`/`summary.json`/`blueprint.json` beside it through the *same* atomic path), dispatch patience (child `/prove` job's `phase` + `updated_at` = liveness/progress signal), resume.
- **How:** as-is; `blueprint.json` uses `write_json_file` directly.
- **Gotcha:** `phase == "exited"` triggers file-lock release (`native_runner.py:622-625`) — a dispatch child that crashes without writing `exited` leaves locks held; the ledger's reconciliation must call `release_all_file_locks(owner_id=...)` on jobs it declares dead. Pinned by `tests/leanflow/test_workflow_status.py`, `tests/test_atomic_json_write.py`.

### 2.4 Turn-prompt fingerprints + search-progress + blocker extraction (struggle-signal sources, all existing)

| Signal (roadmap §4.4) | Existing source (verified) |
|---|---|
| ≥2 genuine failed attempts | `TheoremQueueManager.record_attempt` `queue_manager.py:284`, ring-buffer prune `_prune_attempts` `:352`, `attempt_count_for` `:335`; escalation constants `FAILED_ATTEMPT_ESCALATION_NUDGE_LIMIT=4 / INTERVAL=3` — `native_runner.py:116-117` |
| Same-error signature repeating | retry idempotency `consume_retry_once_for(key, *, kind, signature="")` — `queue_manager.py:425` (signature-deduped), pinned by `test_queue_manager.py:63` |
| Search spiral | `_track_search_progress(agent, args, result)` — `native_runner.py:2412` with thresholds `SEARCH_PROGRESS_REPEAT_NUDGE_LIMIT=2`, `SEARCH_PROGRESS_TOTAL_NUDGE_LIMIT=6` — `native_runner.py:111-112`; already emits a deterministic nudge string; pinned by `test_native_runner.py:630/:729` |
| No-progress turns | stable-signature stall streak inside `_autonomous_stop_reason` — `native_runner.py:8801` (stall/blocked returns `:8915-8924`), limits via `LEANFLOW_NATIVE_AUTONOMOUS_{STALLED,BLOCKED}_LIMIT` (`:545-561`) |
| Budget pressure ≥70% | `AIAgent._get_budget_warning(api_call_count)` — `run_agent.py:2838`, thresholds `_budget_caution_threshold=0.7`, `_budget_warning_threshold=0.9` — `run_agent.py:511-512` |
| Give-up phrasing | `_extract_blocker_summary(text)` — `leanflow_cli/native/native_utils.py:183` (re-exported into native_runner `:297`) |
| Prompt actually changed? | `_record_turn_prompt_fingerprint` — `native_runner.py:2367` (sha1-12 of the real per-turn message; emits changed/size events; called at `:9199`); pinned by `test_native_runner.py:676` |

- **Reused by:** Phase 2's struggle detector is a *pure aggregation module* over these — zero new instrumentation.
- **How:** as-is inputs; new leaf module (§10-B) evaluates the predicate table and returns `StruggleSignal | None`.
- **Gotcha:** `search_progress` state lives in `autonomy_state["search_progress"]` and is reset per assignment (`_reset_search_progress` `:2329`) — the detector must read it before assignment transitions clear it.

### 2.5 Workflow outcomes log

- **Asset:** `append_workflow_outcome(kind, payload)` — `workflow_state.py:390`; consumed e.g. by `recent_empty_search_streak(*, workflow_command, limit=6)` — `lean_services.py:198`. Existing kinds include `lean-verify` (`lean_services.py:1005`), `lean-worker` (`lean_worker_dispatch.py:72/84/104`).
- **Reused by:** dispatch job deliverable recording (`kind="dispatch-job"`), negation-probe outcomes (`kind="negation-probe"`), budget-breakpoint packets (`kind="budget-breakpoint"`), N1's concrete-artifact guarantee (every scope end writes an outcome).
- **How:** as-is (new `kind` strings only).

---

## 3. Queue substrate (`TheoremQueueManager`) — the untouched inner loop + the half-done migration

All in `leanflow_cli/workflows/queue_manager.py` (893 lines) and `leanflow_cli/workflows/queue_models.py` (369 lines).

| Asset | file:line | Today | Redesign use / How |
|---|---|---|---|
| `TheoremKey` | `queue_models.py:53` (roadmap said `:52`) | frozen (label, normalized file) identity | graph-node ↔ queue-item join key; claim/ownership key for the lifecycle dimension. As-is. |
| `QueueItem`, `PrepareState`, `QueueAssignment` | `queue_models.py:76/109/138` | typed queue entries + warm-prepare state | queue seeding from stub files (decomposer emits `QueueItem`s). As-is. |
| `assign(item, *, active_file, slice_text, prepare)` | `queue_manager.py:181` | single-assignment with transition detection, retry-counter clearing | decomposer/orchestrator seed path (Phase 4 "queue seeded from stub files via assign"). As-is. |
| `peek_assignment(...) -> TheoremQueueManager` | `queue_manager.py:219` | **non-mutating view copy** with the item assigned | orchestrator "what-if" routing and prompt-view building without touching live state. As-is. Pinned: `test_queue_manager.py:247`. |
| `classify(check)` / `classify_check` | `queue_manager.py:481` → `queue_models.py:337` | one explicit priority order over `ManagerCheck` | Phase 0 target authority. As-is. Pinned: `test_queue_manager.py:32`. |
| **`decide(check) -> Decision`** | `queue_manager.py:484` | **fully implemented, not called in production** — encodes hard-blocker/warning/future-only/accept branch policy with retry consumption; its docstring names the drifting copies | **Phase 0**: route the runner's verdict branching through it; shadow-compare first. Adapt = call-site change in `native_runner.py`, not in `decide`. |
| Failed-attempt buffer | `record_attempt` `:284`, `record_attempt_for` `:304`, `attempts_for` `:329`, `_prune_attempts` `:352` (roadmap's ":352 ring buffer" = the prune), history cap via `FAILED_ATTEMPT_HISTORY` env (`native_runner` wiring) | per-theorem `FailedAttempt{key, attempt, cycle, proof_shape, reason}` (`queue_models.py:276`) | **decision-packet body** (§4.3): attempts + reasons already structured. As-is. Pinned: `test_queue_manager.py:134`. |
| Retry signatures | `consume_retry_once_for(key, *, kind, signature="")` `:425`, `warning/hard_retry_exhausted` `:473/:476` | idempotent, serialized retries | struggle signal + breakpoint trigger ("K consecutive exhaustions"). As-is. Pinned: `test_queue_manager.py:63/79/119`. |
| `disable_tool(name, reason)` / `disabled_tool_entries` | `:606/:621` | per-run tool disabling with reasons | LLM-manager *suggestions* may include tool-disabling; stays deterministic-manager-applied. As-is. Pinned: `test_queue_manager.py:148`. |
| Reasoning-effort memory | `reasoning_effort_for_current` `:629`, `remember_reasoning_effort_for` `:636`; escalation wiring `native_runner.py:1027-1044`; threshold env `LEANFLOW_NATIVE_FAILED_ATTEMPT_REASONING_THRESHOLD` | per-theorem effort escalation to "high" after repeated failures | budget-breakpoint packet field + orchestrator routing input. As-is. |
| Persistence round-trip | `from_autonomy_state` `:681` / `to_autonomy_state` `:820` | full manager state serializes into `autonomy_state` | resume + decision packets. As-is. Pinned: `test_queue_manager.py:174`. |
| `record_outcome/outcomes` | `:556-604` | per-theorem `TheoremOutcome{status, note, build_status, verification}` | graph reconciliation input (`proved`/`unresolved`). As-is. |
| `select_next_item` | `queue_models.py:311` | pure next-item policy (diagnostic/sorry only) | graph-frontier ordering (Phase 4+) wraps it, never replaces it; the ready current assignment is sticky, then ready members of its file-scoped dependency family outrank unrelated nodes. After source/kernel completion, one `split_of` level hands back to ready siblings or the parent without opening older ancestors. Transitive invalid dependencies exclude a node, cycles cool it down, and an unresolved all-excluded queue clears the stale assignment and replans. Pinned: `test_queue_manager.py:18`, `test_ask_human_and_frontier.py`, and the runner-level excluded-frontier transition test. |

**Phase 0 verdict-copy correction (roadmap `:1890/1834/5815`):** the three drifting verdict sites verified today are `_review_agent_final_report` (`native_runner.py:1890`, open-coded retry policy at `:1953-2033`), the legacy adapter `_manager_feedback_kind` (`:1834`, over `_manager_check_for_feedback_kind` `:1767` + `classify_check`), and the budget-exhaustion path `_handle_api_step_budget_exhaustion` (`:6008`). The function name `_manager_gate_for_queue_verification` in `decide()`'s docstring **no longer exists** — treat `:5815` (`_same_queue_assignment_still_blocked`) as assignment-blocked detection, not a verdict copy; Phase 0's consolidation targets are the three named above.

---

## 4. Verification (the never-LLM-overridable gate + LeanProbe everywhere)

### 4.1 The deterministic gate chain (kernel truth)

- **Assets (all `native_runner.py`):** `_review_agent_final_report(result, autonomy_state) -> dict` — `:1890` (verifies agent-claimed success, applies cleanup policy, retry limits, baseline restore); `_manager_check_queue_item(active_file, target_symbol) -> (check, tool)` — `:1210` (incremental-first, `lean_verify` fallback); `_manager_verify_queue_file` — `:1110`; retry-exhaustion restore + `exit_reason="manager_retry_exhausted"` — `:1975-2006` (roadmap's ":1982 baseline restore", verified); axiom-profile blocker on otherwise-passing declarations — `:1943-1956`; retry limits `MANAGER_WARNING_RETRY_LIMIT=1`, `MANAGER_HARD_RETRY_LIMIT=2`, `MANAGER_POST_EDIT_HARD_RETRY_LIMIT=8` — `:102-108`.
- **Reused by:** everything — invariant: **only** this chain sets `proved`. Graph reconciliation (`blueprint.json` node → `proved`) consumes `VerificationRecord`s (`queue_models.py:169`, produced at `native_runner.py:1219+`), never LLM output. Budget breakpoints hook the exhaustion branches; the struggle detector hooks the failure branches.
- **How:** as-is; Phase 0 re-routes its *branching* through `decide()` (§3) without touching the checks. **Never modified otherwise.**
- **Gotchas / pinning tests:** heavily pinned — `tests/leanflow/test_native_runner.py` `test_review_agent_final_report_*` (`:1944-2357`, 8+ scenarios incl. axiom rejection `:1987`, warning-retry `:2242`, hard-retry sorry-restore `:2298`), `test_manager_feedback_kind_*` (`:1593-1681`), `test_manager_axiom_profile_blocker_*` (`:2738/:2750`), timeout config tests (`:2548/:2579`). Any Phase-0 consolidation must keep these green byte-for-byte.

### 4.2 LeanProbe (in-repo integration, not just MCP)

- **Assets:** `leanflow_cli/lean/lean_incremental.py` — imports the **`lean-probe` Python package** (`pyproject.toml:18`: `lean-probe>=0.3.0,<0.4`) at `:28` (`from lean_probe import LeanIncrementalSegment, LeanProbe; from lean_probe.core import segment_file`); `lean_incremental_check(*, action, file_path, theorem_id="", cwd="", replacement="", include_tactics=False, timeout_s=60) -> dict` — `:245`, dispatching `prepare_file` / `check_target` / `feedback` on a cached `LeanProbe` instance (`_probe()` `:95`); capabilities probe `lean_incremental_capabilities` — `:208`; session cleanup `close_incremental_sessions` — `:199`. Tool wrapper `lean_incremental_check_tool` — `tools/implementations/lean_tool.py:70`. Manager timeouts (cold-mathlib prepare vs check) — `manager_verification.py:48/:55`.
- **Reused by:** roadmap D7/D12 "LeanProbe is the guy": negation probes (`check_target` with `replacement` = the `¬P` attempt against a scratch stub), decomposer skeleton validation (already exists — §6.4), EMPIRICAL Lean experiments, per-cycle graph reconciliation (`feedback` gives per-declaration error/sorry state).
- **How:** as-is. **Genuinely new is only the scratch-file convention** (a `.leanflow/workspace/probes/*.lean` layout + cleanup) — the checking machinery is complete. Note the package pin allows `check_target(..., replacement=...)` — a full `lean_check`-style "check arbitrary snippet" comes free by writing the snippet to a scratch file and targeting it.
- **Gotcha:** requires the project-local REPL (`_local_repl_dir` `:85`; error `local_repl_missing`) — probe jobs must degrade to `lean_verify mode=file_exact` when absent (same fallback `_manager_check_queue_item` already implements). Pinned by `tests/leanflow/test_lean_incremental.py` (332 lines) and `tests/leanflow/test_manager_verification.py`.

### 4.3 Lake-backed authoritative check + axiom guards

- **Assets:** `lean_verify(target="", *, cwd=None, mode="project") -> LeanVerificationResult` — `lean_services.py:976` (`lake build` / module / `lake env lean file_exact`); `lean_axioms` — `lean_services.py:1898`; allowed-axiom set `DEFAULT_ALLOWED_AXIOMS = ("propext","Classical.choice","Quot.sound")` + `LEANFLOW_NATIVE_ALLOWED_AXIOMS` — `native_runner.py:118-122`; edit-time axiom guard `_axiom_declaration_names`/`_introduced_forbidden_axioms` — `queue_edit_guard.py:68/:78`; post-pass transitive axiom profile check — `native_runner.py:1943+`.
- **Reused by:** negation-probe acceptance (a "negation proved" verdict must clear the *same* axiom guards — otherwise `¬P` by `native_decide`/custom axiom would poison the graph); prover job shape A inherits all of it for free.
- **How:** as-is; the negation-probe module calls the same `_manager_check_queue_item`-equivalent path on its scratch file before writing `false` to the graph.

### 4.4 Statement/edit guards (why orchestrator edits are safe *between* turns)

- **Assets:** `validate_lean_statement_edit(before, after)` — `lean_statement_guard.py:58`; queue edit guard snapshot/restore — `queue_edit_guard.py:102-210` (`_queue_edit_initial_declaration_keys` `:102`, protected-declaration restore `:182/:210`); guard state is snapshotted per prover turn (`_managed_queue_edit_guard_state`, see `native_runner.py:5687`).
- **Reused by:** roadmap §4.5 — orchestrator/decomposer file-organization happens between prover turns; after each such edit the guard snapshots + graph are refreshed. Re-statement stays an orchestrator-level action; the guard keeps forbidding silent prover-level statement changes.
- **How:** as-is + one new call-site helper "refresh guard snapshots after orchestrator edit" in the orchestrator leaf module.
- **Gotcha:** pinned by `tests/leanflow/test_queue_edit_guard.py` (11 tests) and `tests/leanflow/test_lean_statement_guard.py` — the restore semantics are strict; orchestrator edits must go through fresh snapshots or they'll be reverted as "protected declaration changes".

---

## 5. LLM aux plumbing (orchestrator/nudger/verifier model calls)

### 5.1 `auxiliary_client` task routing — the model-configuration matrix already exists

- **Assets:** `agent/providers/auxiliary_client.py` — `call_llm(task=None, *, provider=None, model=None, base_url=None, api_key=None, messages, temperature=None, max_tokens=None, tools=None, timeout=30.0, extra_body=None)` — `:984`; per-task config `_auxiliary_task_config(config, task)` — `:220` reading `config.yaml → auxiliary.<task>.{provider, model, reasoning_effort, base_url, api_key, command_template, ...}`; env overrides `AUXILIARY_<TASK>_{PROVIDER,MODEL,BASE_URL,API_KEY,REASONING_EFFORT}` — `:205-213`; task fallbacks `_AUXILIARY_TASK_FALLBACKS = {"lean_decompose_helpers": "lean_reasoning"}` — `:72`; per-task reasoning-effort resolution `_resolve_task_reasoning_effort` — `:967`.
- **Existing config keys (verified `leanflow_cli/config.py:57-…`):** `auxiliary.lean_reasoning` (defaults provider="main", reasoning_effort="high"), `auxiliary.lean_decompose_helpers` (inherits lean_reasoning), `auxiliary.blueprint_verification`, `auxiliary.autoformalizer_verification`; main model under `model.default/provider`.
- **Reused by:** the roadmap §4.8 matrix — **do not invent `models.*`; extend the existing `auxiliary.<task>` pattern**: `auxiliary.orchestration` (default = main model, i.e. strong), `auxiliary.manager_nudge` (small/fast), `auxiliary.planner`, `auxiliary.decomposer` (fallback → `auxiliary.orchestration` via `_AUXILIARY_TASK_FALLBACKS`). Env knobs (`AUXILIARY_ORCHESTRATION_MODEL`, …) come free.
- **How:** adapt = add 3–4 entries to `DEFAULT_CONFIG["auxiliary"]` + fallback-map entries; zero routing code.
- **Gotcha:** provider `"main"` reuses the chat model's credentials (`:379-384`) — the right default for the strong orchestrator. Pinned by `tests/test_provider_parity.py`, `tests/test_fallback_model.py`, `tests/test_anthropic_adapter.py`, `tests/leanflow/test_config_helpers.py`.

### 5.2 `run_model_verification_review` — the one-shot advisory-LLM pattern (nudger + orchestrator floor)

- **Asset:** `run_model_verification_review(*, provider, task, prompt, system_prompt="", timeout_s=1200, max_tokens=12000) -> VerificationReviewResult` — `verification_providers.py:160`; provider resolution `resolve_verification_provider` — `:70`; command-provider variant (Codex CLI/Claude Code) `run_command_verification_review` — `:97`; result dataclass `:37` (`status ∈ {ok, no_answer, timeout, unavailable, error}` — degraded-safe); telemetry via `_record_verification_activity` `:90`; task-specific system prompts `_verification_review_system_prompt` — `manager_verification.py:116`. Model calls cross the text-only `isolated_auxiliary` subprocess boundary: SDK timeouts remain transport hints, while the parent-owned deadline kills/reaps the worker group and deterministically reports `timeout`.
- **Reused by:** Phase 2 LLM-manager (`task="manager_nudge"` — exactly the roadmap call shape) and the Phase 6 orchestrator LLM layer (`task="orchestration"`).
- **How:** as-is — the nudger is `run_model_verification_review` invoked *by the deterministic manager only on struggle signal*, its `response` used as message text only. The `timeout/unavailable/error` statuses give the dark-launch fail-open behavior for free (no nudge, loop continues).
- **Gotcha:** it records prompt+response into activity streams (`:176-183`) — good for N1 auditability, but nudge prompts must not embed secrets. Pinned by `tests/leanflow/test_manager_verification.py`, formalization-review tests in `test_native_runner.py`.

### 5.3 Reasoning advisor / decomposition advisor (existing "expert" LLM calls)

- **Assets:** `lean_reasoning_help_tool(theorem_id, file_path, *, theorem_statement="", current_diagnostics="", current_goals="", current_attempt="", recent_failed_attempts="", question="", cwd="", timeout_s=...)` — `tools/implementations/lean_experts.py:58` (strict advisory-only system prompt, verified); `lean_decompose_helpers_tool(theorem_id, file_path, *, ..., max_helper_count=6, ...)` — `lean_experts.py:487` (**roadmap cites `lean_tool.py:658` — wrong file; corrected**) returning strict-JSON `{obstacle_summary, recommended_split, insertion_guidance, first_concrete_next_edit, helpers[{name, purpose, lean_skeleton(by sorry), dependencies, proof_hints, insertion_point}]}`; **skeleton validation `_validate_helper_skeletons` — `lean_experts.py:364`** (LeanProbe-checks the proposed stubs).
- **Reused by:** the **decomposer role's core** (roadmap §3.13): the JSON contract already emits exactly what graph nodes need (`name→node.name`, `lean_skeleton→stated stub`, `dependencies→depends_on edges`). The decomposer role = this tool + file placement (§4.5) + graph writes.
- **How:** adapt: (a) plumb `max_helper_count`/placement hints from the orchestrator; (b) map the JSON to `blueprint.json` nodes/edges in the new graph module; (c) keep `_validate_helper_skeletons` as the mandatory pre-insertion gate. The advisory prompt's "do not change the target statement" clause is the decomposition-correctness invariant — keep verbatim.
- **Gotcha:** routed through `auxiliary.lean_decompose_helpers` (falls back to `lean_reasoning`) — the redesign should re-point its fallback at `auxiliary.decomposer` once added. Max-tokens env `LEANFLOW_LEAN_DECOMPOSE_HELPERS_MAX_TOKENS` (`:515`).

---

## 6. Knowledge tools (planner fan-out, deep search, probes)

| Asset | file:line | Today | Redesign use / How |
|---|---|---|---|
| `lean_search(...)` multi-provider | `lean_services.py:1033`; providers registry `SEARCH_PROVIDER_LABELS` — `lean_search_providers.py:36` (`local_search`, `leanexplore_local/api`, `leanfinder`, `leansearch`, `loogle`, `project_rg`, `mathlib_rg`) | provider-cascading mathlib/project search with degraded reasons | DEEP-SEARCH jobs + planner mathlib arm; `mode=local` mines cloned repos post-`repo_clone`. As-is. Pinned: `test_lean_services.py`. |
| `lean_lemma_suggest(file_path, theorem_id, *, cwd=None, max_candidates=...)` | `leanflow_cli/lean/lean_lemma_suggest.py:324`; tool `lean_tool.py:235` | goal-derived queries → ranked candidate lemmas + degraded reasons | Phase 5 "premise retrieval as mandatory prover pre-step"; Hilbert-retriever role. As-is. Pinned: `test_lean_lemma_suggest.py`. |
| `lean_proof_context` | `lean_services.py:1446`; tool `:175` of `lean_tool.py`; local impl `lean_proof_context_local.py` | goal/hypotheses/statement extraction | context for orchestrator decision packets + negation-statement synthesis. As-is. |
| `lean_multi_attempt` | `lean_services.py:1612`; tool `lean_tool.py:195` | batch tactic attempts at a position | EMPIRICAL Lean micro-experiments; negation quick-tries. As-is. |
| `lean_outline` | `lean_tool.py:253` (backed by `lean_declarations.py`) | file skeleton | graph reconciliation (decl inventory per file) + file-organization view. As-is. Pinned: `test_lean_outline.py`, `test_lean_declarations.py`. |
| `lean_sorries` / `lean_inspect` | `lean_services.py:869/:910`; sorry counters `lean_sorry_stats.py` | per-file/project sorry + diagnostics state | **the per-cycle graph-status reconciler input** (roadmap §4.1 anti-drift). As-is. |
| `lean_auto_search` / `lean_auto_probe` / `lean_auto_try` | `lean_services.py:1769/:1666/:1794` | automation probes | EMPIRICAL archetype building blocks. As-is. |
| `web_search_tool(query, limit=5)` | `tools/implementations/web_tools.py:502`; providers in `web_research_providers.py` (Tavily `:358`, DuckDuckGo-HTML, arXiv/code/papers arms; optional Firecrawl `web_tools.py:85-103`) | free-provider search cascade + LLM summarization (`async_call_llm`) | planner web arm; DEEP-SEARCH jobs. As-is. |
| `web_fetch_tool(url, max_chars=...)` | `tools/implementations/web_fetch.py:156` | fetch/read page or PDF | deep search. As-is. |
| `web_download_tool(url, filename="", max_bytes=WEB_DOWNLOAD_MAX_BYTES)` | `web_fetch.py:251` | sandboxed size-capped artifact download | deep search; **`repo_clone` should mirror its sandbox/size-cap conventions** (workspace-rooted path, cap, returns local path). |
| Read-before-edit freshness guard | `_freshness_guard(file_ops, path, task_id)` — `file_tools.py:331`; util `tools/utilities/read_freshness.py`; hash recording on read `file_tools.py:190-193` | rejects patches against stale reads, keyed per `task_id` | protects concurrent orchestrator-edit + prover-turn interleavings; dispatch children get isolation via distinct `task_id`s. As-is. |
| Anchor/fuzzy patch engine | `tools/implementations/file_operations.py` — strict-vs-fuzzy strategies (`exact`, `block_anchor`, `context_aware`) with observability `matched_via`/confidence `:137-139/:770/:809`; strict mode "exact-or-fail" `:257/:746` | V4A patch application | orchestrator stub insertion should use **strict mode**; `apply_verified_patch_tool(path, patch, *, cwd, check_mode="file_exact", theorem_id, owner_id, task_id)` — `lean_patch.py:155` gives patch+immediate-verify in one call (ideal for decomposer stub placement). As-is. |
| File locks | `acquire/release/list` — `leanflow_cli/runtime/file_locks.py`; coordination toolset `core/toolsets.py:11`; used by `dispatch_worker` `lean_worker_dispatch.py:57` (ttl 1800 s, purpose-tagged) | cross-agent file reservation | dispatch scope enforcement ("no two children per file"). As-is. Pinned: `test_file_locks.py`. |
| `plausible` (Lean library) | vendored in fixture projects, e.g. `testdata/workflow_projects/ProveDemo/.lake/packages/plausible` | Lean counterexample search | the negation **pre-probe** = run `plausible`/`slim_check`-style tactic via LeanProbe `check_target` on the scratch `¬`-statement; *availability is project-dependent* — the probe must feature-detect (`lean_search mode=local` for `Plausible`) and treat absence as "pre-probe skipped (hint only)". |

---

## 7. Prompts, specs, skills

- **Spec loader:** `leanflow_cli/lean/lean_workflow_specs.py` — `SPEC_ROOT = REPO_ROOT / "leanflow_specs"` (`:19`; roadmap ":20" ≈ ok), `LeanSpecRecord` dataclass `:24` (frontmatter: `id/kind/title/summary/aliases/skills/tools/workers/review_actions/stop_conditions/route_actions`), `load_lean_specs` `:120`, `get_lean_spec` `:130`, `validate_lean_specs` `:157`. Specs on disk: `leanflow_specs/workflows/{prove, formalize, draft, review, search, checkpoint, golf, refactor, doctor}.md`, dormant workers `leanflow_specs/workers/{proof-repair, proof-golfer, axiom-eliminator, sorry-filler-deep}.md`.
  - **Reused by:** item 21 spec fold/rewrite. Orchestrator-phase fragments become new `kind: helper`-or-`workflow` spec records — the loader validates them for free (`VALID_SPEC_KINDS`, `:20`). Deleting `checkpoint.md` requires cleaning its aliases from the frontmatter set and the runner help text (`native_runner.py:3788-3790`, `:7899`, `:8637/:8647`). Pinned: `tests/leanflow/test_lean_workflow_specs.py` (14 tests: alias collisions, unknown workers).
- **Worker prompt builder:** `_worker_prompt(worker, request)` — `lean_worker_dispatch.py:19` (spec-record → job prompt: goal/file/line/context + route-actions/tools contract). **This is the JobSpec→prompt renderer prototype** for the dispatch service. Adapt-by-copy into the dispatch module with the JobSpec fields (deliverable schema, budget, scope, report_to).
- **System prompt builders:** `_managed_system_prompt()` — `native_runner.py:8624` (already instructs "treat `/prove`,… as native workflow labels"); `_autonomous_continuation_prompt(live_state, cycle_number, autonomy_state)` — `:8927` (per-cycle contract: gates, queue assignment, route decisions — the research-pusher tone pass of §4.7 lands here + in specs); skills prompt `build_skills_system_prompt` — `agent/prompting/prompt_builder.py:214`; skills discovery `discover_skills` — `leanflow_cli/runtime/skill_core.py:120`.
  - **Reused by:** prompt-level artifact-awareness (Phase 1: "every spawned agent receives the artifact paths") = additions to `_managed_system_prompt`/child-prompt builders + `spawn_workflow` env (§1.5). Pinned: `tests/leanflow/test_skill_core.py`, `tests/agent/test_skill_commands.py` (note: it already references a `.leanflow/plans/plan.md` convention at `:265-273` — reuse that path convention for plan-state).
- **Route table prototype:** `route_workflow_step(...)` — `lean_services.py:1987` — existing deterministic step-router; the Phase 4 deterministic orchestrator floor should mirror its shape (pure function over live-state → route token) rather than invent a new pattern.

---

## 8. Persistence & resume

- **Atomic JSON:** `write_json_file` — `workflow_json_io.py:31`; pinned by `tests/test_atomic_json_write.py` + `test_workflow_state_concurrency.py`. Plan-state/graph writes go through it (roadmap Phase 1) — as-is.
- **Checkpoints (UX to retire, safety to keep):** interactive commands `/checkpoint`, `/resume-plan`, `/rollback` surfaced at `native_runner.py:3788-3790` and `:7899`; mechanics in `leanflow_cli/native/native_checkpoints.py` — `_resume_plan_from_checkpoint` `:200`, **`_rollback_to_checkpoint` `:205`** (git-shadow filesystem snapshot via `agent._checkpoint_mgr` → `tools/utilities/checkpoint_manager.py`), index/current IO `:112-184`; runner-side auto checkpoint before compaction `_maybe_checkpoint_before_compaction` — `native_runner.py:8692`; workflow-state loaders `load_workflow_checkpoints`/`load_current_workflow_checkpoint` — `workflow_state.py:1175/1183`.
  - **How:** Phase 1 removes only the three UX commands + `checkpoint.md`; keeps `_maybe_checkpoint_before_compaction`, the git-shadow manager, and baseline-`sorry` restore (`native_runner.py:1981-2006`) untouched as silent safety. Resume becomes: load `summary.json` + `blueprint.json` → reconcile against `lean_sorries`/outcomes → rebuild `TheoremQueueManager` via `from_autonomy_state`.
- **Session DB:** `core/state.py` — `SessionDB` `:96` (SQLite at `~/.leanflow/state.db`, `:27`): sessions, titles, lineage (`get_next_title_in_lineage` `:388`), message transcripts (`append_message` `:475`). Reused as-is (children already share it via `session_db=parent._session_db`, `delegate_tool.py:244`); it is *not* the plan-state authority and should not be extended for the graph. Pinned: `tests/test_state.py`.
- **Live-state rebuild on resume:** `_build_live_proof_state_compat` / `_promote_live_state_to_verified_compat` + `_rebuild_history_for_theorem_transition` (`native_runner.py:9095-9105`) — the existing anti-drift resume path the graph reconciler slots into. As-is.
- **Project-level `/prove` manager (bonus asset the roadmap under-cites):** `leanflow_cli/workflows/project_prove_manager.py` — **`_project_prove_dependency_graph(lean_files, module_to_path) -> (imports_by_path, imported_by_path, import_modules_by_path)` `:321`**, transitive closure `:344`, difficulty scoring `:209/:272`, LLM-order guard `_guard_project_prove_llm_order` `:397` (LLM proposes order; deterministic guard validates against the import graph — an existing "LLM suggests / deterministic decides" precedent), advanced by `_advance_project_prove_manager_if_needed` — `native_runner.py:4152`.
  - **Reused by:** `blueprint.json`'s file-level `depends_on` edges seed from this import graph; the orchestrator's file-organization decisions reuse its module-path helpers (`_module_name_for_project_path` `:97`). Pinned: `test_native_runner.py:4174-4302` and `tests/leanflow/test_project_prove_manager.py`.

---

## 9. Roadmap line-drift corrections (verified)

| Roadmap citation | Verified today |
|---|---|
| `workflow.py:537` spawn_workflow | **`workflow.py:564`** (resolve at `:394`) |
| `native_runner.py:8921` `_autonomous_stop_reason` | function at **`:8801`**; the stall/blocked returns (the seam meant) at `:8915-8924` |
| `native_runner.py:9094` scope-entry | cycle-body top is `:9093-9101`; true scope-entry (first `_drive_autonomous_followups` call from `main`) at **`:9411`** (`main()` `:9301`); loop def `:9059` ✓ |
| `lean_tool.py:658` `lean_decompose_helpers` | **`tools/implementations/lean_experts.py:487`** (`lean_tool.py` is 946 lines; outline tool ends `:253`) |
| `queue_models.py:52` TheoremKey | `:53` |
| `queue_manager.py:352` failed-attempt ring buffer | `:352` = `_prune_attempts`; buffer entry `record_attempt` `:284` |
| `native_runner.py:1890/1834/5815` three verdict copies | `:1890` ✓, `:1834` ✓ (`_manager_feedback_kind`), third copy = budget-exhaustion path **`:6008`**; `:5815` is `_same_queue_assignment_still_blocked`; `_manager_gate_for_queue_verification` (named in `decide()` docstring) no longer exists |
| `workflow_state.py:98-99/:305/:651` | `:98` ✓, `:305` ✓, `:651` ✓; `append_workflow_outcome` at `:390` |
| `lean_services.py:140` dispatch gate | ✓ `LEAN_WORKER_DISPATCH_ENABLED = False` at `:140` (checked at `:779`, `:2069`) |
| `verification_providers.py:160`, `manager_verification.py:116`, `queue_manager.py:181/:219/:484`, `queue_models.py:337`, `lean_worker_dispatch.py:47/:57/:104`, `delegate_tool.py:391`, `native_runner.py:1210/:1890/:5661/:3788/:9059`, `native_checkpoints.py:205` | all ✓ exact |

---

## 10. NEW-CODE-ONLY list (everything else above is reuse)

Grep-verified absent from the tree: `blueprint.json`, `dispatch_ledger`, `repo_clone`, `LEANFLOW_RESEARCH_MODE`, `LEANFLOW_BUDGET_BREAKPOINT`, `manager_nudge`, `_orchestrator_route` — nothing to collide with. Per AGENTS.md layering, all land as focused leaf modules (core → agent/tools → leanflow_cli), never inside `native_runner.py`/`run_agent.py` bodies (only minimal call-site hooks there).

- **A. `leanflow_cli/workflows/plan_state.py` + `blueprint_graph.py`** — the `plan.md`/`summary.json`/`blueprint.json` schema (roadmap §4.1), node/edge CRUD, per-cycle reconciler (inputs: `lean_sorries`, `TheoremOutcome`s, `VerificationRecord`s). *Why new:* no existing artifact stores conjectured/stated/false/split semantics; `project_prove_manager` covers only file-level import edges (reused as the seed). Persistence via `write_json_file` (reuse). Tests: new `tests/leanflow/test_blueprint_graph.py`, patterned on `test_workflow_status.py` fixtures.
- **B. `leanflow_cli/workflows/struggle_signals.py`** — pure predicate table over the seven existing signal sources (§2.4) → `StruggleSignal`. *Why new:* the signals exist but no module aggregates them; the LLM-manager trigger must be deterministic and unit-testable in isolation. Tests: table-driven, extending `test_native_runner.py` fixture dicts.
- **C. `leanflow_cli/workflows/budget_breakpoints.py`** — decision-packet builder (fields all sourced from `FailedAttempt`s, retry signatures, `search_progress`, negation status) + the `LEANFLOW_BUDGET_BREAKPOINT=1` clean-stop semantics hooked at `_handle_api_step_budget_exhaustion` (`:6008`), the retry-exhaustion branch (`:1981`), and `_autonomous_stop_reason`. *Why new:* today exhaustion is explicitly non-breaking (verified: records attempt, restores sorry, loop continues, `test_native_runner.py:8577`); no decision-point exists.
- **D. `leanflow_cli/workflows/dispatch_service.py`** — JobSpec dataclass, dotted-lineage job ids (N3), `dispatch_ledger[]` in `summary.json`, patience/kill policy, reconciliation against `summarize_workflow_agents`. *Why new:* `delegate_task` and `spawn_workflow` execute jobs but nothing tracks proposed→deployed→running→terminal states across both; `dispatch_worker` (`lean_worker_dispatch.py:47`) is the closest prototype (prompt+lock+outcome) but is queue-spec-shaped and flag-dead (`LEAN_WORKER_DISPATCH_ENABLED=False`) — mine it, don't extend it. Plus 2 env-var additions to `spawn_workflow`'s child env (artifact paths, job id).
- **E. `leanflow_cli/workflows/negation_probe.py`** — statement negation synthesis (`¬P` / `P → False` stub), optional `plausible` pre-probe with feature detection, bounded LeanProbe attempt in scratch, axiom-guarded acceptance, graph write. *Why new:* no negation machinery exists anywhere; every ingredient (LeanProbe check, axiom gate, graph) is reused.
- **F. `leanflow_cli/workflows/orchestrator.py` (+ `decomposer.py`)** — `RouteContext` builder (graph frontier + decision packet), deterministic route floor (patterned on `route_workflow_step` and `_guard_project_prove_llm_order`), LLM layer via `run_model_verification_review(task="orchestration")`, file-organization ops (strict `apply_verified_patch` + guard-snapshot refresh), decomposer = `lean_decompose_helpers_tool` JSON → stubs → graph → `assign`. *Why new:* no routing decision-maker exists above the eternal loop; every capability it invokes is existing.
- **G. `tools/implementations/repo_clone.py`** — `git clone --depth 1` into `.leanflow/workspace/repos/<name>`, size-capped, mirroring `web_download_tool`'s sandbox conventions; registered in `tools/registry.py` + toolsets. *Why new:* `web_download` handles files, not repos.
- **H. Config/flag additions (data only):** `auxiliary.{orchestration, manager_nudge, planner, decomposer}` in `leanflow_cli/config.py` DEFAULT_CONFIG; env flags `LEANFLOW_BUDGET_BREAKPOINT`, `LEANFLOW_MANAGER_LLM_ENABLED`, `LEANFLOW_ORCHESTRATOR_ENABLED`, `LEANFLOW_ORCHESTRATOR_MAX_ROUTES`, `LEANFLOW_NEGATION_PROBE_BUDGET`, `LEANFLOW_RESEARCH_MODE` — all read through the existing `_read_native_env`/`_read_int_env` helpers (`native_config.py:26-46`).

Everything not on this list — agent engine, job executor, spawn contract, inbox/kill/descendants, activity registry, live status, queue manager, kernel gate, LeanProbe, aux-LLM routing, advisors, search/web/patch/lock tools, spec loader, checkpoints/session DB — is reused from the seams cited above, with the pinned test files named per asset as the safety net for every adaptation.
