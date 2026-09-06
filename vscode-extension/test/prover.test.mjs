/** Exact-run identity, dependency rendering, and artifact authority tests. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { normalizeProverSnapshot, proverTreeRows, proverArtifactAllowed, proverJobAcceptsGuidance, proverGuidanceUpdate, proverDefaultGuidanceRecipient } from "../dist/test/prover.mjs";
import { parseWebviewMessage } from "../dist/test/messageSchema.mjs";

function snapshot(fields = {}) {
  return normalizeProverSnapshot({ run_id: "run-1", ...fields }, "run-1");
}

test("prover state never accepts a different or missing run identity", () => {
  assert.equal(normalizeProverSnapshot({ run_id: "old-run" }, "new-run"), null);
  assert.equal(normalizeProverSnapshot({}, "run-1"), null);
  assert.equal(normalizeProverSnapshot(null, "run-1"), null);
});

test("unknown usage stays unknown and incomplete optional data is tolerated", () => {
  const state = snapshot({ metrics: { api_calls: 0, cost_usd: null, input_tokens: -1 }, dag: { nodes: [null, { id: "a" }, { id: "a" }] } });
  assert.equal(state.metrics.api_calls, 0);
  assert.equal(state.metrics.cost_usd, null);
  assert.equal(state.metrics.input_tokens, null);
  assert.equal(state.dag.nodes.length, 1);
  assert.equal(state.dag.nodes[0].name, "a");
});

test("terminal failures remain visible and resumed runs continue polling", () => {
  for (const phase of ["provider_error", "environment_error", "source_conflict", "disproved", "budget_exhausted", "verification_failed", "error", "blocked"]) {
    const state = snapshot({ phase, error: "Setup failed", next_step: "Resume after correcting setup" });
    assert.equal(state.terminal, true, phase);
    assert.equal(state.error, "Setup failed");
    assert.equal(state.next_step, "Resume after correcting setup");
  }
  const resumed = snapshot({ phase: "completed", terminal: false, parent_run_id: "previous-run" });
  assert.equal(resumed.terminal, false);
  assert.equal(resumed.resumed_from, "previous-run");
  assert.equal(resumed.run_id, "run-1");
  const disproved = snapshot({ phase: "disproved", disproof: { node_id: "target", certified: true, evidence_path: "checks/negation.lean" } });
  assert.equal(disproved.disproof.certified, true);
  assert.equal(proverArtifactAllowed(disproved, "checks/negation.lean"), true);
});

test("child research jobs, partial costs, candidates and Lake diffs preserve their contract", () => {
  const state = snapshot({
    metrics: { cost_usd: 0.12, cost_complete: false },
    jobs: [{ id: "prover", status: "running" }, { id: "research", role: "research", parent_job_id: "prover", status: "completed", purpose: "Compute the finite case", result_path: "research/result.json" }],
    dag: { nodes: [{ id: "n", status: "candidate", notes: "Awaiting dependency" }] },
    changes: [{ path: "lakefile.toml", baseline_path: "baseline/lakefile.toml", agent_id: "orchestrator" }],
  });
  assert.equal(state.metrics.cost_complete, false);
  assert.equal(state.jobs[1].parent_job_id, "prover");
  assert.equal(state.jobs[1].purpose, "Compute the finite case");
  assert.equal(proverJobAcceptsGuidance(state.jobs[0]), true);
  assert.equal(proverJobAcceptsGuidance(state.jobs[1]), false);
  assert.equal(state.dag.nodes[0].status, "candidate");
  assert.equal(state.dag.nodes[0].notes, "Awaiting dependency");
  assert.equal(proverArtifactAllowed(state, "research/result.json"), true);
  assert.equal(proverArtifactAllowed(state, "lakefile.toml", "baseline/lakefile.toml"), true);
  assert.equal(proverArtifactAllowed(state, "lakefile.toml", "baseline/other.toml"), false);
});

test("tree presents shared dependencies as references instead of duplicated subtrees", () => {
  const state = snapshot({ dag: { roots: ["root"], nodes: [
    { id: "root", dependencies: ["a", "b"] },
    { id: "a", dependencies: ["shared"] },
    { id: "b", dependencies: ["shared"] },
    { id: "shared" },
  ] } });
  assert.deepEqual(proverTreeRows(state.dag).map((row) => [row.node.id, row.depth, row.reference]), [
    ["root", 0, false], ["a", 1, false], ["shared", 2, false], ["b", 1, false], ["shared", 2, true],
  ]);
});

test("cycle and disconnected component remain visible without infinite recursion", () => {
  const state = snapshot({ dag: { nodes: [
    { id: "a", dependencies: ["b"] }, { id: "b", dependencies: ["a"] }, { id: "other" },
  ] } });
  const rows = proverTreeRows(state.dag);
  assert.equal(rows.length, 4);
  assert.equal(rows.filter((row) => row.cycle).length, 1);
});

test("artifact authority binds baselines to the exact changed file", () => {
  const state = snapshot({ plan_path: "PLAN.md", changes: [{ path: "Main.lean", baseline_path: "baseline/Main.lean" }], jobs: [{ id: "worker", log_path: "worker.log" }] });
  assert.equal(proverArtifactAllowed(state, "PLAN.md"), true);
  assert.equal(proverArtifactAllowed(state, "worker.log"), true);
  assert.equal(proverArtifactAllowed(state, "Main.lean", "baseline/Main.lean"), true);
  assert.equal(proverArtifactAllowed(state, "Main.lean", "other.lean"), false);
  assert.equal(proverArtifactAllowed(state, "secrets.txt"), false);
});

test("large dependency chains do not consume the JavaScript call stack", () => {
  const state = snapshot({ dag: { nodes: Array.from({ length: 10000 }, (_, i) => ({
    id: `node-${i}`, dependencies: i < 9999 ? [`node-${i + 1}`] : [],
  })) } });
  const rows = proverTreeRows(state.dag);
  assert.equal(rows.length, 10000);
  assert.equal(rows.at(-1).depth, 9999);
});

test("prover messages require an exact id, bounded guidance and an agent identifier", () => {
  assert.equal(parseWebviewMessage({ type: "loadProver", runId: "" }).ok, false);
  assert.equal(parseWebviewMessage({ type: "loadProver", runId: "../../escape" }).ok, false);
  assert.equal(parseWebviewMessage({ type: "loadProver", runId: "run-1" }).ok, true);
  const message = { type: "proverMessage", runId: "run-1", agentId: "orchestrator", message: "Try induction." };
  assert.equal(parseWebviewMessage(message).ok, true);
  assert.equal(parseWebviewMessage({ ...message, message: " " }).ok, false);
  assert.equal(parseWebviewMessage({ ...message, agentId: "../other" }).ok, false);
  assert.equal(parseWebviewMessage({ ...message, message: "x".repeat(8193) }).ok, false);
});

test("artifact messages reject invalid line locations before reaching VS Code", () => {
  const message = { type: "openProverFile", runId: "run-1", path: "Main.lean", line: 12 };
  assert.equal(parseWebviewMessage(message).ok, true);
  assert.equal(parseWebviewMessage({ ...message, line: 0 }).ok, false);
  assert.equal(parseWebviewMessage({ ...message, path: "bad\0file" }).ok, false);
});

test("guidance requests retain an acknowledgement identity without accepting unsafe ids", () => {
  const request = { type: "proverMessage", runId: "run-1", agentId: "orchestrator", message: "Try induction.", requestId: "guidance-1" };
  const parsed = parseWebviewMessage(request);
  assert.equal(parsed.ok, true);
  assert.equal(parsed.message.requestId, "guidance-1");
  assert.equal(parseWebviewMessage({ ...request, requestId: "bad\0id" }).ok, false);
});

test("guidance clears only its successful acknowledged draft and preserves a rejected message", () => {
  const pending = { runId: "run-1", requestId: "send-1", message: "Try induction." };
  const reply = { runId: "run-1", requestId: "send-1", success: true, error: "" };
  assert.deepEqual(proverGuidanceUpdate(pending.message, pending, reply), { draft: "", error: "" });
  assert.deepEqual(proverGuidanceUpdate("New observation", pending, reply), { draft: "New observation", error: "" });
  assert.deepEqual(proverGuidanceUpdate(pending.message, pending, { ...reply, success: false, error: "Run has finished" }), { draft: pending.message, error: "Run has finished" });
  assert.equal(proverGuidanceUpdate(pending.message, pending, { ...reply, runId: "run-2" }), null);
  assert.equal(proverGuidanceUpdate(pending.message, pending, { ...reply, requestId: "old-send" }), null);
});

test("the additive runtime contract normalizes tolerantly and absent fields stay unknown", () => {
  const state = snapshot({
    status: "running", phase: "reviewing", updated_at: "2026-09-06T14:05:00+00:00", started_at: "2026-09-06T13:19:13+00:00",
    provider: "codex", reasoning_effort: "high",
    config: { mode: "research", parallelism: 4, total_api_calls: 2000, orchestrator_api_calls: 50, job_api_calls: 200, max_restarts: 2, max_nodes: 24, max_decompositions: 8, wall_time_s: 28800, timeout_s: 1200, model: "gpt-6-astra", orchestrator_model: "", context_tokens: 64000, orchestrator_context_tokens: 96000, compression: true, orchestrator_compression: false, allowed_axioms: ["propext"], fill_definitions: false, search_order: "top-down", plan_refinements: 4 },
    operations: [
      { id: "op-1", job_id: "orchestrator_00002", node_id: "", kind: "skeleton_compile", label: "Compile helper", file: "LeanFlowProofs/H.lean", status: "running", started_at: "2026-09-06T14:04:30+00:00", updated_at: "", finished_at: "", timeout_s: 1200, completed: 4, total: 8 },
      null, { kind: "final_build" }, { id: "op-2", status: "completed" },
    ],
    jobs: [{ id: "prover_00005", role: "prover", node_id: "n", status: "completed", phase: "submitted", started_at: "a", updated_at: "b", finished_at: "c", provider: "codex", model: "gpt-6-astra", reasoning_effort: "high", context_tokens: 64000, compression: true, input_tokens: 10, output_tokens: 5, cost_usd: 0.5, artifacts: ["/p/.leanflow/x/PLAN_job.md", 5], report_path: "/p/report.json", node_revision: 1, error: "" }],
    changes: [{ path: "/p/a.lean", baseline_path: "/b/a.lean", status: "staged", pending: true, agent_id: "orchestrator_00002", job_id: "orchestrator_00002" }],
    proposed_dag: { roots: ["root"], nodes: [{ id: "root" }, { id: "h1", dependencies: ["root"] }] },
    proposal_status: "validating", proposed_plan: "Draft plan", proposal_critique: "Looks sound.",
    metrics: { api_calls: 250, total_api_budget: 2000, reserved_api_calls: 800, remaining_reserved_api_calls: 650, available_api_calls: 950, elapsed_s: 3600.5, wall_time_s: 28800, decompositions: 1, max_decompositions: 8, max_nodes: 24 },
    stop_reason: { code: "job_budget_exhausted", scope: "job_budget", message: "m", used: 200, limit: 200, next_step: "n" },
    verification: { accepted: false, error: "remaining sorry", files: ["Main.lean"], stdout: "", stderr: "", timed_out: false, returncode: 1 },
    preflight: { accepted: true, error: "" },
    planning_request: { reason: "Inspect", refinement: true, steps: { outline: "orchestrator_00001", "proposal-0": "orchestrator_00002" }, accepted_proposal: { plan: "x" } },
    targets: ["Main.lean"], goal: "Prove it",
    dag: { nodes: [{ id: "n", status: "submitted", kind: "lemma", original: true, attempts: 2, revision: 1, holes: [0, 1], candidate: ["by simp"] }] },
  });
  assert.equal(state.status, "running");
  assert.equal(state.phase, "reviewing");
  assert.equal(state.updated_at, "2026-09-06T14:05:00+00:00");
  assert.equal(state.provider, "codex");
  assert.equal(state.reasoning_effort, "high");
  assert.equal(state.config.parallelism, 4);
  assert.equal(state.config.orchestrator_model, "");
  assert.equal(state.config.orchestrator_compression, false);
  assert.deepEqual(state.config.allowed_axioms, ["propext"]);
  assert.equal(state.operations.length, 2, "operations without an id or with only a kind are dropped");
  assert.equal(state.operations[0].completed, 4);
  assert.equal(state.operations[0].total, 8);
  assert.equal(state.operations[0].timeout_s, 1200);
  assert.equal(state.operations[1].kind, "");
  assert.equal(state.jobs[0].phase, "submitted");
  assert.equal(state.jobs[0].provider, "codex");
  assert.equal(state.jobs[0].context_tokens, 64000);
  assert.equal(state.jobs[0].compression, true);
  assert.equal(state.jobs[0].cost_usd, 0.5);
  assert.deepEqual(state.jobs[0].artifacts, ["/p/.leanflow/x/PLAN_job.md"]);
  assert.equal(state.jobs[0].report_path, "/p/report.json");
  assert.equal(state.changes[0].pending, true);
  assert.equal(state.changes[0].status, "staged");
  assert.equal(state.changes[0].job_id, "orchestrator_00002");
  assert.equal(state.changes[0].staged_status, "");
  assert.equal(state.sequence, null);
  assert.equal(state.jobs[0].stop_reason, null);
  assert.equal(state.proposed_dag.nodes.length, 2);
  assert.equal(state.proposal_status, "validating");
  assert.equal(state.proposed_plan, "Draft plan");
  assert.equal(state.proposal_critique, "Looks sound.");
  assert.equal(state.metrics.available_api_calls, 950);
  assert.equal(state.metrics.remaining_reserved_api_calls, 650);
  assert.equal(state.metrics.reserved_api_calls, 800);
  assert.equal(state.metrics.elapsed_s, 3600.5);
  assert.equal(state.metrics.wall_time_s, 28800);
  assert.equal(state.metrics.max_nodes, 24);
  assert.deepEqual(state.stop_reason, { code: "job_budget_exhausted", scope: "job_budget", message: "m", used: 200, limit: 200, next_step: "n", remaining_api_calls: null, unresolved_nodes: [], job_id: "", node_id: "" });
  assert.equal(state.verification.present, true);
  assert.equal(state.verification.accepted, false);
  assert.deepEqual(state.verification.files, ["Main.lean"]);
  assert.equal(state.verification.returncode, 1);
  assert.equal(state.preflight.accepted, true);
  assert.equal(state.planning.active, true);
  assert.equal(state.planning.refinement, true);
  assert.deepEqual(state.planning.steps, [{ step: "outline", job_id: "orchestrator_00001" }, { step: "proposal-0", job_id: "orchestrator_00002" }]);
  assert.equal(state.planning.accepted, true);
  assert.deepEqual(state.targets, ["Main.lean"]);
  assert.equal(state.goal, "Prove it");
  assert.equal(state.dag.nodes[0].status, "submitted");
  assert.equal(state.dag.nodes[0].kind, "lemma");
  assert.equal(state.dag.nodes[0].original, true);
  assert.equal(state.dag.nodes[0].attempts, 2);
  assert.equal(state.dag.nodes[0].holes, 2);
  assert.equal(state.dag.nodes[0].candidate_count, 1);

  const legacy = snapshot({ phase: "proving", metrics: { api_calls: 5 } });
  assert.equal(legacy.status, "proving");
  assert.equal(legacy.provider, "");
  assert.equal(legacy.updated_at, "");
  assert.deepEqual(legacy.operations, []);
  assert.equal(legacy.proposed_dag, null);
  assert.equal(legacy.stop_reason, null);
  assert.equal(legacy.verification.present, false);
  assert.equal(legacy.preflight.present, false);
  assert.equal(legacy.planning.active, false);
  assert.equal(legacy.metrics.available_api_calls, null);
  assert.equal(legacy.config.parallelism, null);
  assert.equal(legacy.config.compression, null);
  assert.equal(legacy.dag.nodes.length, 0);
});

test("job artifacts and reports advertised by the run are openable, nothing else is", () => {
  const state = snapshot({ jobs: [{ id: "orchestrator_00002", artifacts: ["/p/jobs/orchestrator_00002/graph_proposal.json"], report_path: "/p/jobs/orchestrator_00002/report.json" }] });
  assert.equal(proverArtifactAllowed(state, "/p/jobs/orchestrator_00002/graph_proposal.json"), true);
  assert.equal(proverArtifactAllowed(state, "/p/jobs/orchestrator_00002/report.json"), true);
  assert.equal(proverArtifactAllowed(state, "/p/jobs/orchestrator_00002/secret.json"), false);
  assert.equal(proverArtifactAllowed(state, ""), false);
});

test("standard guidance targets the active prover while research and idle runs target the manager", () => {
  const state = snapshot({ mode: "standard", jobs: [
    { id: "finished", role: "prover", status: "completed" },
    { id: "resource", role: "research", status: "running" },
    { id: "current-prover", role: "prover", status: "running" },
  ] });
  assert.equal(proverDefaultGuidanceRecipient(state), "current-prover");
  assert.equal(proverDefaultGuidanceRecipient({ ...state, mode: "research" }), "orchestrator");
  assert.equal(proverDefaultGuidanceRecipient({ ...state, jobs: [] }), "orchestrator");
  assert.equal(proverDefaultGuidanceRecipient({ ...state, jobs: [state.jobs[0]] }), "orchestrator");
});
