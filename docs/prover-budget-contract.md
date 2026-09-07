# Prover limits and terminal outcomes

This audit covers the dedicated standard/research prover controller. It does
not make its limits global to formalization, legacy runners, external Claude
Code reviews, or unrelated Codex tasks.

## Enforcement

- `job_api_calls` caps one prover or negation pass. `orchestrator_api_calls` caps
  each outline, DAG proposal, review, or research session separately. A complete
  planning round can contain several such sessions.
- `total_api_calls` is shared by every model session in the campaign, including
  retries, reviews, negation attempts, and prover-requested research jobs.
  The controller reserves allocations under a lock before dispatch. A final
  job can receive less than its usual allowance if little remains. Unused
  reserved calls are returned when a job finishes; reservation alone is not
  counted as completed use.
- Each job starts with a durable zero-admission ledger before dispatch; each session persists its request count before contacting the provider. Response tokens, known costs and coverage are written to that ledger before observer notification. Failed
  model requests count. SDK retries are disabled on the dedicated transport;
  this path does not enter the legacy advisory/retry conversation loop.
- Context compression and local subproof decomposition do not reset a pass.
  An additional prover pass needs concrete retained progress and must fit the
  restart and campaign ceilings. Two permitted restarts mean up to three passes
  for an unchanged node assignment, with each pass separately counted globally.
- Resume restores the original configuration, spent calls, interrupted-job
  ledger, and accumulated active campaign time. Time while the process is stopped
  is not charged. Larger launch flags do not extend an existing saved run.
- Research parallelism bounds prover jobs, not every subprocess, Lean checker,
  or helper service. Standard mode uses one prover. Research/model helpers still
  consume campaign allocations.
- Refinement, decomposition, and DAG-size bounds are checked by controller code.
  These structural limits can leave no admissible work before the call ceiling.
  Ordinary progress notes, local tactic repairs, and decompositions are not direction refinements. The independent planning reviewer classifies the mathematical change; only an accepted, materialized direction change consumes a refinement. Rejected drafts consume their model calls but no refinement. Graph edge changes alone cannot distinguish a decomposition from a new mathematical direction.

The total API counter measures model requests, not Lean invocations, search HTTP
requests, tokens, or dollars. Token and cost totals are telemetry; an unknown
provider cost stays unknown. Context admission uses an approximate token count
and reserves output capacity; it is not a campaign token ceiling.

## Time limits

The campaign stops admitting work when cumulative active elapsed time reaches
its ceiling. Sessions check cancellation and deadlines between requests and
tools; each provider request receives the smaller of its configured timeout and
the campaign time remaining.

This is cooperative cancellation, not an exact process-kill deadline. An already
running tool, independent verification, or final build can finish or hit its own
timeout after the campaign deadline. Final verification may also complete after
the final model-call allowance has been spent. Prover scratch checks retain a
separate 60-second ceiling; increasing `timeout_s` does not change every tool's
individual bound.

## Reading terminal state

The exact run's state is available in the VS Code prover workspace and with
`leanflow runs prover --run-id RUN_ID --json` from its project. The persisted
snapshot is `.leanflow/workflow-state/prover/RUN_ID/state.json`.

| Status | Meaning |
| --- | --- |
| `completed` | All requested roots passed independent proof verification and the final project gate. |
| `budget_exhausted` | The campaign call allocation is exhausted. Individual job exhaustion is recorded on the job. |
| `timeout` | The campaign reached its active-time limit. |
| `blocked` | The current DAG has no admissible next job under its retry/decomposition limits. It does not establish that a theorem is false. |
| `disproved` | The exact negation of an original target was independently certified. |
| `provider_error` / `environment_error` | Model or Lean infrastructure failed; retained progress can be resumed within the saved allowance. |
| `verification_failed` | Proposed completion failed the final project gate. |
| `interrupted` | The user or controller stopped the run. |

Every terminal snapshot includes a structured `stop_reason` with a stable code,
scope, remaining calls, unresolved nodes, and next step. A scheduler dead end
uses `blocked` / `no_runnable_obligations`, even if earlier individual jobs spent
their complete allocations. The benchmark harness pauses these cases for inspection.
`reserved_api_calls` includes complete allocations held by running jobs;
`remaining_reserved_api_calls` excludes their already spent calls. Interrupted
historical jobs never reduce a new job's reservation. When all remaining calls are reserved by parallel provers, the controller waits outside its lock for unused allocations to be returned. Their mathematical results remain queued for normal verification; nested research requests never wait on their own prover. The UI freezes its elapsed
extrapolation when a snapshot is more than two minutes old.

Planning has no separate three-draft cap: fresh proposal/review sessions keep
receiving the preceding critique within their own stage ceilings, total admitted
requests, active-time limit, and structural limits. An accepted local repair can
reopen an unchanged node within its existing retry ceiling; its attempts do not reset.
A prover-requested research job with no unreserved calls returns an unavailable
report so the prover can keep working within its current allocation.

Costs carry `cost_source`, `costed_api_calls`, and `cost_complete`. `cost_usd` is
only the known subtotal; partial coverage is labelled explicitly. Provider-reported
cost and provider/local estimates are distinguished. Local estimates require an
exact listed model (optionally its dated version), never a model-family match;
subscription transports and zero-valued pricing placeholders remain unpriced unless
the provider reports a cost. Historical unlabelled estimates are not presented as
verified prices. Listed estimates are not billing statements and do not account
for every provider-specific discount or caching rule.

## Evidence

The audit follows `workflows/prover/runtime.py` (reservation, resume, scheduling,
termination), `agent_session.py` (durable per-request ledger),
`session_transport.py` (one provider request), `job_controller.py` (all-role
accounting), and `planning_controller.py` (structural limits), all under
`leanflow_cli/`.

The focused regression run passed 105 tests covering session budgets, failure
charging, global allocations, parallel reservation recovery, restart progress,
context compaction, guidance, resumed jobs, and stage finalization. It used fake
providers and did not launch a mathematical proving campaign. Its log is
`.leanflow/experiments/20260906-readiness-selection/budget-audit-tests.log`.
