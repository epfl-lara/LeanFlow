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
- Each session persists its request count before contacting the provider. Failed
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
  Ordinary progress notes are not direction refinements.

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
| `budget_exhausted` | A call/time allowance ended with unresolved work; compare the campaign metrics and job records. |
| `blocked` | The current DAG has no admissible next job under its retry/decomposition limits. It does not establish that a theorem is false. |
| `disproved` | The exact negation of an original target was independently certified. |
| `provider_error` / `environment_error` | Model or Lean infrastructure failed; retained progress can be resumed within the saved allowance. |
| `verification_failed` | Proposed completion failed the final project gate. |
| `interrupted` | The user or controller stopped the run. |

The current `budget_exhausted` label is broader than overall campaign exhaustion:
it can also be selected after an individual job exhausts its allowance and no
further work is admissible. To distinguish the cases, compare
`metrics.api_calls` with `config.total_api_calls`, and `metrics.elapsed_s` with
`config.wall_time_s`; inspect each job's `api_calls`, `api_budget`, and `status`.
`reserved_api_calls` represents allocations still held by active jobs. A more
specific terminal reason is an outstanding status-UX improvement.

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
