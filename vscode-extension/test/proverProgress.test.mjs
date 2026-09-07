/**
 * Budget, capacity, controller progress, and lifecycle views over the prover
 * snapshot (backlog D01, D02, D03, D04, D05, J06, J08).
 *
 * These are the numbers the dashboard shows while a campaign runs. Each view is
 * a pure function of the normalized snapshot so a wrong figure is a failing
 * assertion here rather than a screenshot.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeProverSnapshot } from "../dist/test/prover.mjs";
import {
  changeRows,
  controllerActivity,
  isNewerProverSnapshot,
  jobSettings,
  jobTimeline,
  operationLabel,
  proposalView,
  proverBudget,
  proverCapacity,
  proverPhaseLabel,
  roleSettings,
  snapshotAgeMs,
  stopReasonView,
} from "../dist/test/proverProgress.mjs";

const NOW = Date.parse("2026-09-06T14:05:00+00:00");

function snapshot(fields = {}) {
  return normalizeProverSnapshot({ run_id: "run-1", ...fields }, "run-1");
}

const CONFIG = {
  mode: "research", search_order: "bottom-up", job_api_calls: 200, max_restarts: 2, plan_refinements: 4,
  parallelism: 4, total_api_calls: 2000, orchestrator_api_calls: 50, max_nodes: 24, max_decompositions: 8,
  wall_time_s: 28800, timeout_s: 1200, model: "gpt-6-astra", orchestrator_model: "gpt-6-astra",
  context_tokens: 64000, orchestrator_context_tokens: 96000, compression: true, orchestrator_compression: true,
};

// --------------------------------------------------------------------- budget

test("the campaign budget shows used, total, available, and reservations without double counting", () => {
  const state = snapshot({
    config: CONFIG,
    updated_at: "2026-09-06T14:04:00+00:00",
    metrics: {
      api_calls: 250, total_api_budget: 2000, reserved_api_calls: 800, remaining_reserved_api_calls: 650,
      available_api_calls: 950, elapsed_s: 3600, plan_refinements: 1, max_plan_refinements: 4, decompositions: 1,
    },
    dag: { nodes: [{ id: "a" }, { id: "b" }] },
  });
  const budget = proverBudget(state, NOW);
  assert.equal(budget.usedCalls, 250);
  assert.equal(budget.totalCalls, 2000);
  // Reservations already include the calls spent inside running jobs; the
  // available figure comes from the runtime, never from total - used - reserved.
  assert.equal(budget.reservedCalls, 800);
  assert.equal(budget.remainingReservedCalls, 650);
  assert.equal(budget.availableCalls, 950);
  assert.equal(budget.wallTimeS, 28800);
  assert.equal(budget.elapsedAdvancing, true);
  assert.equal(budget.elapsedS, 3660);
  assert.deepEqual(budget.planRefinements, { used: 1, max: 4 });
  assert.deepEqual(budget.decompositions, { used: 1, max: 8 });
  assert.deepEqual(budget.nodes, { used: 2, max: 24 });
  assert.equal(budget.maxRestartsPerNode, 2);
  assert.equal(budget.jobCalls, 200);
  assert.equal(budget.orchestratorCalls, 50);
});

test("an older snapshot without the new budget fields leaves available calls unknown and cost unknown", () => {
  const state = snapshot({
    config: CONFIG,
    metrics: { api_calls: 50, total_api_budget: 2000, reserved_api_calls: 200, elapsed_s: 100, cost_usd: null },
  });
  const budget = proverBudget(state, NOW);
  assert.equal(budget.availableCalls, null);
  assert.equal(budget.remainingReservedCalls, null);
  assert.equal(budget.reservedCalls, 200);
  assert.equal(budget.cost.usd, null);
  assert.equal(budget.cost.complete, null);
  // No updated_at means nothing to advance from.
  assert.equal(budget.elapsedAdvancing, false);
  assert.equal(budget.elapsedS, 100);
  // Limits fall back to the run configuration when metrics omit them.
  assert.equal(budget.wallTimeS, 28800);
  assert.equal(budget.decompositions.max, 8);
  assert.equal(budget.nodes.max, 24);
});

test("elapsed time advances only for fresh nonterminal snapshots", () => {
  const live = snapshot({ updated_at: "2026-09-06T14:00:00+00:00", metrics: { elapsed_s: 10 } });
  assert.equal(proverBudget(live, NOW).elapsedS, 10);
  assert.equal(proverBudget(live, NOW).elapsedAdvancing, false);
  const done = snapshot({ updated_at: "2026-09-06T14:00:00+00:00", terminal: true, metrics: { elapsed_s: 10 } });
  assert.equal(proverBudget(done, NOW).elapsedS, 10);
  assert.equal(proverBudget(done, NOW).elapsedAdvancing, false);
  // A clock skewed behind the snapshot never subtracts time.
  const future = snapshot({ updated_at: "2026-09-06T15:00:00+00:00", metrics: { elapsed_s: 10 } });
  assert.equal(proverBudget(future, NOW).elapsedS, 10);
  assert.equal(snapshotAgeMs(live, NOW), 300_000);
  assert.equal(snapshotAgeMs(snapshot(), NOW), null);
});

// ------------------------------------------------------------------- capacity

test("capacity separates one controller from the prover slots and explains idle slots", () => {
  const planning = snapshot({
    config: CONFIG, phase: "planning",
    jobs: [{ id: "orchestrator_00001", role: "orchestrator", status: "running" }],
    dag: { nodes: [{ id: "root", status: "pending" }] },
  });
  const view = proverCapacity(planning);
  assert.equal(view.controllerActive, true);
  assert.equal(view.controllerJob?.id, "orchestrator_00001");
  assert.equal(view.proverActive, 0);
  assert.equal(view.proverLimit, 4);
  assert.match(view.waitingReason, /planning/i);

  const reviewing = proverCapacity(snapshot({ config: CONFIG, phase: "reviewing", jobs: [{ id: "review_3", role: "review", status: "running" }] }));
  assert.match(reviewing.waitingReason, /review/i);
});

test("validation checks are visible as the reason provers wait and never count as provers", () => {
  const validating = snapshot({
    config: CONFIG, phase: "reviewing",
    jobs: [{ id: "review_00003", role: "review", status: "completed" }],
    operations: [
      { id: "op-5", job_id: "orchestrator_00002", kind: "skeleton_compile", status: "running", label: "Compile helper 5/8", completed: 4, total: 8, started_at: "2026-09-06T14:04:30+00:00", timeout_s: 1200, file: "LeanFlowProofs/Helper5.lean" },
    ],
  });
  const view = proverCapacity(validating);
  assert.equal(view.proverActive, 0);
  assert.equal(view.controllerActive, true);
  assert.equal(view.checksActive, 1);
  assert.match(view.waitingReason, /skeleton|validation/i);
});

test("idle prover slots during proving are explained by dependencies or budget", () => {
  const blockedByDependencies = snapshot({
    config: CONFIG, phase: "proving",
    dag: { nodes: [
      { id: "root", status: "pending", dependencies: ["leaf"] },
      { id: "leaf", status: "running" },
    ] },
    jobs: [{ id: "prover_1", role: "prover", node_id: "leaf", status: "running" }],
  });
  const view = proverCapacity(blockedByDependencies);
  assert.equal(view.proverActive, 1);
  assert.equal(view.readyNodes, 0);
  assert.match(view.waitingReason, /prerequisite|dependenc/i);

  const budgetBound = snapshot({
    config: CONFIG, phase: "proving",
    metrics: { available_api_calls: 40 },
    dag: { nodes: [{ id: "root", status: "pending" }] },
  });
  assert.match(proverCapacity(budgetBound).waitingReason, /remaining 40 calls.*200-call ceiling/i);

  const standard = proverCapacity(snapshot({ config: { ...CONFIG, mode: "standard", parallelism: 4 }, mode: "standard", phase: "proving" }));
  assert.equal(standard.proverLimit, 1);
});

// ----------------------------------------------------------------- controller

test("controller activity reports active checks with progress, duration, and timeout", () => {
  const state = snapshot({
    config: CONFIG, phase: "reviewing",
    jobs: [
      { id: "review_00003", role: "review", status: "completed", finished_at: "2026-09-06T14:00:15+00:00" },
    ],
    planning_request: { reason: "Inspect", refinement: false, steps: { outline: "orchestrator_00001", "proposal-0": "orchestrator_00002", "review-0": "review_00003" } },
    operations: [
      { id: "op-1", job_id: "orchestrator_00002", kind: "skeleton_compile", status: "completed", file: "LeanFlowProofs/Helper1.lean", started_at: "2026-09-06T14:00:20+00:00", finished_at: "2026-09-06T14:01:10+00:00", completed: 1, total: 8 },
      { id: "op-5", job_id: "orchestrator_00002", kind: "skeleton_compile", status: "running", file: "LeanFlowProofs/Helper5.lean", started_at: "2026-09-06T14:04:30+00:00", updated_at: "2026-09-06T14:04:50+00:00", completed: 4, total: 8, timeout_s: 1200 },
    ],
  });
  const view = controllerActivity(state, NOW);
  assert.equal(view.active.length, 1);
  assert.equal(view.active[0].label, "Helper skeleton compile");
  assert.equal(view.active[0].progress, "4/8");
  assert.equal(view.active[0].durationS, 30);
  assert.equal(view.active[0].timeoutS, 1200);
  assert.equal(view.recent.length, 1);
  assert.equal(view.recent[0].durationS, 50);
  assert.match(view.explanation, /skeleton/i);
  assert.match(view.waitingForProvers, /compil|check/i);
  assert.equal(view.planningSteps.length, 3);
  assert.equal(view.planningSteps[2].job?.id, "review_00003");
});

test("without operations the controller view still names the phase and says progress is unreported", () => {
  const state = snapshot({ config: CONFIG, phase: "reviewing", jobs: [{ id: "review_1", role: "review", status: "completed" }] });
  const view = controllerActivity(state, NOW);
  assert.equal(view.active.length, 0);
  assert.equal(view.phaseLabel, "Reviewing");
  assert.match(view.explanation, /not report|no per-check/i);
  assert.equal(proverPhaseLabel(snapshot({ phase: "verifying" })), "Final verification");
  assert.equal(proverPhaseLabel(snapshot({ phase: "final_build" })), "Final build");
  assert.equal(proverPhaseLabel(snapshot({ phase: "budget_exhausted" })), "Budget exhausted");
  assert.match(proverCapacity(snapshot({ config: CONFIG, phase: "final_build" })).waitingReason, /final build|built/i);
});

test("job model turns are separated from controller checks and a runtime-measured duration is exact", () => {
  const state = snapshot({
    config: CONFIG, phase: "proving",
    jobs: [{ id: "prover_1", role: "prover", node_id: "n", status: "running" }],
    operations: [
      { id: "m1", job_id: "prover_1", node_id: "n", kind: "model_request", label: "Waiting for model response", status: "running", started_at: "2026-09-06T14:04:40+00:00" },
      { id: "c1", job_id: "", node_id: "n", kind: "submission_check", status: "completed", started_at: "2026-09-06T14:03:00+00:00", finished_at: "2026-09-06T14:04:00+00:00", elapsed_s: 57.4 },
    ],
  });
  const view = controllerActivity(state, NOW);
  assert.equal(view.active.length, 1);
  assert.equal(view.active[0].label, "Model request");
  assert.match(view.explanation, /in flight inside agent jobs/);
  assert.equal(view.recent[0].durationS, 57);
  assert.equal(proverCapacity(state).checksActive, 0);
});

test("final build and preflight results are readable without opening JSON", () => {
  const passed = snapshot({
    phase: "completed", terminal: true,
    verification: { success: true, returncode: 0, stdout: "Build completed successfully (8712 jobs).\n", stderr: "", timed_out: false, accepted: true },
    preflight: { accepted: true, error: "" },
  });
  const view = controllerActivity(passed, NOW);
  assert.equal(view.finalBuild?.accepted, true);
  assert.match(view.finalBuild?.summary, /Build completed successfully/);
  const failed = snapshot({
    phase: "verification_failed", terminal: true,
    verification: { accepted: false, error: "remaining sorry", files: ["Main.lean"] },
  });
  const failedView = controllerActivity(failed, NOW);
  assert.equal(failedView.finalBuild?.accepted, false);
  assert.match(failedView.finalBuild?.summary, /remaining sorry/);
  assert.match(failedView.finalBuild?.summary, /Main\.lean/);
  assert.equal(controllerActivity(snapshot(), NOW).finalBuild, null);
});

test("operation kinds have human labels and an unknown kind falls back to its words", () => {
  assert.equal(operationLabel({ kind: "signature_check", label: "" }), "Protected signature check");
  assert.equal(operationLabel({ kind: "final_build", label: "" }), "Final project build");
  assert.equal(operationLabel({ kind: "model_request", label: "" }), "Model request");
  assert.equal(operationLabel({ kind: "some_new_kind", label: "" }), "Some new kind");
  assert.equal(operationLabel({ kind: "tool", label: "search_project" }), "search_project");
});

// ---------------------------------------------------------------- stop reason

test("a recorded stop reason names its exact scope and next step", () => {
  const state = snapshot({
    phase: "budget_exhausted", terminal: true,
    stop_reason: { code: "job_budget_exhausted", scope: "job_budget", message: "prover_00008 used its allowance with no admissible work left", used: 200, limit: 200, next_step: "Resume with a larger job allowance." },
    metrics: { api_calls: 400, total_api_budget: 2000 },
  });
  const view = stopReasonView(state);
  assert.equal(view.legacy, false);
  assert.equal(view.scopeLabel, "one job's call allocation");
  assert.equal(view.usage, "200 / 200");
  assert.match(view.message, /prover_00008/);
  assert.equal(view.nextStep, "Resume with a larger job allowance.");
  // The campaign budget is not conflated with the local exhaustion.
  assert.match(view.campaign, /400 of 2,000/);
});

test("the runtime's own stop-reason shapes name job, campaign, and scheduler scopes exactly", () => {
  const scheduler = stopReasonView(snapshot({
    phase: "budget_exhausted", terminal: true,
    metrics: { api_calls: 400, total_api_budget: 2000 },
    stop_reason: { code: "no_runnable_obligations", scope: "scheduler", message: "No runnable obligation remains under the current plan and per-node retry/decomposition limits; the total call budget is not exhausted.", used: null, limit: null, remaining_api_calls: 1600, unresolved_nodes: ["n_root", "n_helper"], next_step: "Inspect the saved node reports and plan." },
  }));
  assert.match(scheduler.scopeLabel, /no runnable obligation/i);
  assert.match(scheduler.scopeLabel, /not exhausted/i);
  assert.equal(scheduler.usage, "");
  assert.match(scheduler.campaign, /400 of 2,000 \(1,600 unspent\)/);
  assert.deepEqual(scheduler.unresolved, ["n_root", "n_helper"]);

  const wall = stopReasonView(snapshot({
    phase: "budget_exhausted", terminal: true,
    stop_reason: { code: "campaign_wall_time", scope: "campaign", message: "The campaign reached its total active-time limit.", used: 28800.4, limit: 28800 },
  }));
  assert.equal(wall.scopeLabel, "the campaign wall-clock limit");
  assert.equal(wall.usage, "28,800 s / 28,800 s");

  const calls = stopReasonView(snapshot({
    phase: "budget_exhausted", terminal: true,
    stop_reason: { code: "campaign_api_calls", scope: "campaign", message: "The campaign used its total call budget.", used: 2000, limit: 2000 },
  }));
  assert.equal(calls.scopeLabel, "the campaign API budget");
  assert.equal(calls.usage, "2,000 / 2,000");

  const infrastructure = stopReasonView(snapshot({
    phase: "provider_error", terminal: true, error: "Provider unavailable",
    stop_reason: { code: "provider_error", scope: "campaign", message: "Provider unavailable" },
  }));
  assert.equal(infrastructure.scopeLabel, "infrastructure");
  assert.equal(infrastructure.tone, "error");
});

test("a job's own stop reason is normalized apart from the campaign's", () => {
  const state = snapshot({
    phase: "proving",
    jobs: [{ id: "prover_00008", role: "prover", node_id: "n", status: "budget_exhausted", api_calls: 200, api_budget: 200,
      stop_reason: { code: "job_api_calls", scope: "job", job_id: "prover_00008", node_id: "n", message: "This job used its call allocation.", used: 200, limit: 200, next_step: "The controller evaluates saved progress." } }],
  });
  assert.equal(state.jobs[0].stop_reason.code, "job_api_calls");
  assert.equal(state.jobs[0].stop_reason.job_id, "prover_00008");
  assert.equal(stopReasonView(state), null, "a job allowance ending is not a campaign stop");
});

test("an old snapshot's budget_exhausted is reported as ambiguous rather than as the campaign budget", () => {
  const state = snapshot({ phase: "budget_exhausted", terminal: true, metrics: { api_calls: 400, total_api_budget: 2000 } });
  const view = stopReasonView(state);
  assert.equal(view.legacy, true);
  assert.match(view.message, /does not record/i);
  assert.match(view.campaign, /400 of 2,000/);
  assert.equal(stopReasonView(snapshot({ phase: "proving" })), null);
  const completed = stopReasonView(snapshot({ phase: "completed", terminal: true }));
  assert.equal(completed.tone, "info");
  const current = stopReasonView(snapshot({ phase: "completed", terminal: true, snapshot_sequence: 42 }));
  assert.equal(current.legacy, false);
});

// -------------------------------------------------------------------- changes

test("staged changes are listed first and labelled pending, committed, or rolled back", () => {
  const state = snapshot({
    jobs: [{ id: "orchestrator_00002", role: "orchestrator", status: "running" }],
    changes: [
      { path: "/p/Main.lean", baseline_path: "/b/Main.lean", status: "modified", agent_id: "prover_00003" },
      { path: "/p/lakefile.toml", baseline_path: "/b/lakefile.toml", status: "staged", pending: true, agent_id: "orchestrator_00002" },
      { path: "/p/LeanFlowProofs/Old.lean", baseline_path: "", status: "rolled_back", agent_id: "orchestrator_00002" },
      { path: "/p/LeanFlowProofs/New.lean", baseline_path: "/b/LeanFlowProofs/New.lean", status: "added", pending: true, agent_id: "orchestrator_00002" },
    ],
  });
  const rows = changeRows(state);
  assert.deepEqual(rows.map((row) => row.change.path), ["/p/lakefile.toml", "/p/LeanFlowProofs/New.lean", "/p/Main.lean", "/p/LeanFlowProofs/Old.lean"]);
  assert.equal(rows[0].label, "Pending validation");
  assert.equal(rows[0].job?.id, "orchestrator_00002");
  assert.equal(rows[1].label, "Pending validation");
  assert.equal(rows[2].label, "Committed");
  assert.equal(rows[3].label, "Rolled back");
  assert.equal(rows[3].tone, "err");
});

// ------------------------------------------------------------------- proposal

test("a proposed graph is exposed separately from the canonical one with its review verdict", () => {
  const state = snapshot({
    dag: { roots: ["root"], nodes: [{ id: "root", status: "pending" }] },
    proposed_dag: { roots: ["root"], nodes: [{ id: "root", status: "pending" }, { id: "h1", status: "pending" }, { id: "h2", status: "check_failed" }] },
    proposal_status: "validating",
    proposed_plan: "Split into two helpers.",
    proposal_critique: "Accepted: helpers are well scoped.",
  });
  const view = proposalView(state);
  assert.equal(view.status, "validating");
  assert.match(view.label, /validating/i);
  assert.equal(view.dag.nodes.length, 3);
  assert.equal(view.newNodes, 2);
  assert.equal(view.existingNodes, 1);
  assert.equal(view.plan, "Split into two helpers.");
  assert.equal(view.critique, "Accepted: helpers are well scoped.");
  assert.match(view.explanation, /canonical|authoritative/i);
  assert.equal(proposalView(snapshot()), null);
  const rejected = proposalView(snapshot({ proposed_dag: { nodes: [{ id: "x" }] }, proposal_status: "rejected", proposal_critique: "Cycle." }));
  assert.match(rejected.label, /rejected/i);
});

// ------------------------------------------------------------------- ordering

test("a snapshot never replaces a newer one for the same run", () => {
  const older = snapshot({ updated_at: "2026-09-06T14:00:00+00:00" });
  const newer = snapshot({ updated_at: "2026-09-06T14:01:00+00:00" });
  assert.equal(isNewerProverSnapshot(older, newer), true);
  assert.equal(isNewerProverSnapshot(newer, older), false);
  // The runtime's publication counter wins over timestamps when both carry it.
  const first = snapshot({ updated_at: "2026-09-06T14:01:00+00:00", snapshot_sequence: 7 });
  const second = snapshot({ updated_at: "2026-09-06T14:01:00+00:00", snapshot_sequence: 8 });
  assert.equal(isNewerProverSnapshot(first, second), true);
  assert.equal(isNewerProverSnapshot(second, first), false);
  assert.equal(first.sequence, 7);
  assert.equal(isNewerProverSnapshot(newer, newer), true, "an identical timestamp still refreshes derived fields");
  assert.equal(isNewerProverSnapshot(null, older), true);
  assert.equal(isNewerProverSnapshot(newer, snapshot()), true, "a snapshot without a timestamp cannot be ordered and is accepted");
  const other = normalizeProverSnapshot({ run_id: "run-2", updated_at: "2026-09-06T13:00:00+00:00" }, "run-2");
  assert.equal(isNewerProverSnapshot(newer, other), true, "another run is never compared");
});

// ---------------------------------------------------------- roles and models

test("role settings describe one shared provider and effort with per-role model and context", () => {
  const state = snapshot({ config: CONFIG, provider: "codex", reasoning_effort: "high" });
  const roles = roleSettings(state);
  assert.deepEqual(roles.map((role) => role.role), ["prover", "orchestrator"]);
  assert.equal(roles[0].model, "gpt-6-astra");
  assert.equal(roles[0].contextTokens, 64000);
  assert.equal(roles[0].callsPerStage, 200);
  assert.match(roles[0].stageLabel, /per prover pass/);
  assert.equal(roles[1].contextTokens, 96000);
  assert.equal(roles[1].callsPerStage, 50);
  assert.match(roles[1].stageLabel, /per planning, review, or research stage/);
  const orchestratorFallback = roleSettings(snapshot({ config: { ...CONFIG, orchestrator_model: "" } }));
  assert.equal(orchestratorFallback[1].model, "gpt-6-astra");
});

test("job settings come from the job when recorded and otherwise from the shared run settings", () => {
  const state = snapshot({ config: CONFIG, provider: "codex", reasoning_effort: "high", jobs: [
    { id: "prover_1", role: "prover", status: "running" },
    { id: "orchestrator_2", role: "orchestrator", status: "running", provider: "anthropic", model: "claude-fable-5-1", reasoning_effort: "max", context_tokens: 128000, compression: false },
  ] });
  const shared = jobSettings(state.jobs[0], state);
  assert.deepEqual(shared, { provider: "codex", model: "gpt-6-astra", reasoning_effort: "high", context_tokens: 64000, compression: true, source: "shared" });
  const recorded = jobSettings(state.jobs[1], state);
  assert.equal(recorded.source, "job");
  assert.equal(recorded.model, "claude-fable-5-1");
  assert.equal(recorded.provider, "anthropic");
  assert.equal(recorded.context_tokens, 128000);
  assert.equal(recorded.compression, false);
  const unknown = jobSettings(state.jobs[0], snapshot({ jobs: [{ id: "prover_1" }] }));
  assert.equal(unknown.provider, "");
  assert.equal(unknown.model, "");
});

test("job timelines compute a duration for finished jobs and keep live ones advancing", () => {
  const finished = jobTimeline({ started_at: "2026-09-06T14:00:00+00:00", finished_at: "2026-09-06T14:03:30+00:00", status: "completed" }, NOW);
  assert.deepEqual(finished, { durationS: 210, live: false });
  const live = jobTimeline({ started_at: "2026-09-06T14:00:00+00:00", finished_at: "", status: "running" }, NOW);
  assert.deepEqual(live, { durationS: 300, live: true });
  assert.deepEqual(jobTimeline({ started_at: "", finished_at: "", status: "completed" }, NOW), { durationS: null, live: false });
});

test("graph validation wrapper does not double-count its protected-type check", () => {
  for (const kind of ["graph_validation", "skeleton_compile"]) {
    const state = snapshot({ phase: "validating", operations: [
      { id: "outer", kind, label: "Materializing and validating reviewed graph", status: "running", total: 0 },
      { id: "inner", kind: "signature_check", file: "Main.lean", status: "running", total: 1, completed: 0 },
    ] });
    assert.equal(proverCapacity(state).checksActive, 1);
    assert.equal(proverCapacity(state).proverActive, 0);
    assert.ok(controllerActivity(state, Date.now()).active.every(op => op.progress !== "0/0"));
  }
});
