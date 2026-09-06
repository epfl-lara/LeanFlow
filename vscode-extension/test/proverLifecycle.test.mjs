/**
 * Proof lifecycle regression tests (backlog D09, D04).
 *
 * A node's displayed state must agree across the summary counts, the graph
 * badge, the job row, and the node detail. The reported SpencerResearch
 * snapshot is the anchor: a prover had completed and its submission had been
 * accepted by the independent check, but the DAG node still said `running`,
 * so the dashboard counted it as active work instead of a proof awaiting
 * verification.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeProverSnapshot } from "../dist/test/prover.mjs";
import {
  graphProofState,
  nodeLifecycle,
  proofStateCounts,
  proofStateSummary,
  proposalNodeLifecycle,
} from "../dist/test/proverGraph.mjs";

function snapshot(fields = {}) {
  return normalizeProverSnapshot({ run_id: "spencer", ...fields }, "spencer");
}

/** SpencerResearch at 19:03:34 UTC on 2026-09-06, reduced to the fields that matter. */
function spencerAt1903() {
  return snapshot({
    mode: "research",
    phase: "proving",
    status: "running",
    dag: {
      roots: ["n_root"],
      nodes: [
        { id: "n_root", name: "random_bound", status: "pending", dependencies: ["n_rb_cube_mgf", "n_rb_threshold"] },
        { id: "n_rb_exp_pair", name: "exp_pair", status: "running", dependencies: [] },
        { id: "n_rb_cube_product", name: "cube_product", status: "running", dependencies: [] },
        { id: "n_rb_cube_mgf", name: "cube_mgf", status: "pending", dependencies: ["n_rb_exp_pair", "n_rb_cube_product"] },
        { id: "n_rb_threshold", name: "threshold", status: "running", dependencies: [] },
      ],
    },
    jobs: [
      { id: "orchestrator_00001", role: "orchestrator", status: "completed", api_calls: 50, api_budget: 50 },
      { id: "prover_00004", role: "prover", node_id: "n_rb_exp_pair", status: "running", api_calls: 3, api_budget: 200 },
      { id: "prover_00005", role: "prover", node_id: "n_rb_cube_product", status: "completed", api_calls: 6, api_budget: 200, finished_at: "2026-09-06T19:03:34+00:00" },
      { id: "prover_00006", role: "prover", node_id: "n_rb_threshold", status: "running", api_calls: 4, api_budget: 200 },
    ],
  });
}

test("a completed prover whose node still says running is awaiting verification, not active", () => {
  const state = spencerAt1903();
  const node = state.dag.nodes.find((item) => item.id === "n_rb_cube_product");
  assert.equal(graphProofState(node, state.jobs), "candidate");
  const lifecycle = nodeLifecycle(node, { jobs: state.jobs, nodes: state.dag.nodes });
  assert.equal(lifecycle.state, "candidate");
  assert.match(lifecycle.label, /pending verification/i);
  assert.match(lifecycle.detail, /prover_00005/);
  assert.equal(lifecycle.job?.id, "prover_00005");
});

test("the reported Spencer snapshot reads Solved 0 · Active 2 · Pending 2 · Awaiting verification 1", () => {
  const state = spencerAt1903();
  const counts = proofStateCounts(state.dag.nodes, state.jobs);
  assert.deepEqual(counts, { solved: 0, active: 2, pending: 2, candidate: 1, blocked: 0, failed: 0 });
  assert.equal(
    proofStateSummary(counts),
    "Solved 0 · Active 2 · Pending 2 · Awaiting verification 1",
  );
});

test("submitted, verifying, and integrating nodes count as awaiting verification with their current step", () => {
  const operations = [
    { id: "op-1", job_id: "prover_00005", node_id: "n", kind: "submission_check", status: "running", started_at: "2026-09-06T19:03:00+00:00" },
  ];
  for (const status of ["submitted", "verifying", "integrating"]) {
    const state = snapshot({
      dag: { nodes: [{ id: "n", status }] },
      jobs: [{ id: "prover_00005", role: "prover", node_id: "n", status: "completed" }],
      operations,
    });
    const node = state.dag.nodes[0];
    assert.equal(graphProofState(node, state.jobs, false, state.operations), "candidate", status);
    const lifecycle = nodeLifecycle(node, { jobs: state.jobs, operations: state.operations });
    assert.equal(lifecycle.state, "candidate");
    assert.equal(lifecycle.operation?.id, "op-1");
    assert.match(lifecycle.detail, /submission check/i);
  }
  const integrating = snapshot({
    dag: { nodes: [{ id: "n", status: "integrating" }] },
    operations: [{ id: "op-2", node_id: "n", kind: "proof_integration", status: "running" }],
  });
  const lifecycle = nodeLifecycle(integrating.dag.nodes[0], { jobs: [], operations: integrating.operations });
  assert.equal(lifecycle.state, "candidate");
  assert.match(lifecycle.label, /integrating/i);
  assert.match(lifecycle.detail, /proof integration/i);
});

test("a live prover waiting for independent verification counts under Awaiting", () => {
  for (const status of ["submitted", "verifying", "integrating"]) {
    const state = snapshot({
      status: "running", phase: "proving",
      dag: { nodes: [{ id: "n", status }] },
      jobs: [{ id: "prover_1", role: "prover", node_id: "n", status: "running", phase: "verifying", api_calls: 4, api_budget: 4 }],
      operations: [{ id: "check", kind: "submission_check", status: "running", node_id: "n", job_id: "prover_1" }],
    });
    const counts = proofStateCounts(state.dag.nodes, state.jobs, false, state.operations);
    assert.equal(counts.active, 0, status);
    assert.equal(counts.candidate, 1, status);
    assert.equal(counts.solved, 0, status);
    const lifecycle = nodeLifecycle(state.dag.nodes[0], { jobs: state.jobs, operations: state.operations });
    assert.equal(lifecycle.state, "candidate");
    assert.match(lifecycle.detail, /Submission check/);
    assert.equal(lifecycle.job.id, "prover_1");
  }
});

test("a failed or rejected prover job never makes its node solved or active", () => {
  for (const status of ["budget_exhausted", "timeout", "error", "provider_error", "interrupted", "stale", "rejected", "failed"]) {
    const state = snapshot({
      dag: { nodes: [{ id: "n", status: "running" }] },
      jobs: [{ id: "prover_1", role: "prover", node_id: "n", status }],
    });
    const lifecycle = nodeLifecycle(state.dag.nodes[0], { jobs: state.jobs });
    assert.equal(lifecycle.state, "pending", status);
    assert.match(lifecycle.detail, /prover_1/);
    assert.match(lifecycle.detail, new RegExp(status));
  }
});

test("a rejected candidate returns to proving as active only once a prover is live again", () => {
  const retry = snapshot({
    dag: { nodes: [{ id: "n", status: "retry" }] },
    jobs: [{ id: "prover_1", role: "prover", node_id: "n", status: "completed" }],
  });
  const waiting = nodeLifecycle(retry.dag.nodes[0], { jobs: retry.jobs });
  assert.equal(waiting.state, "pending");
  assert.match(waiting.label, /retry/i);

  const redispatched = snapshot({
    dag: { nodes: [{ id: "n", status: "retry" }] },
    jobs: [
      { id: "prover_1", role: "prover", node_id: "n", status: "completed" },
      { id: "prover_2", role: "prover", node_id: "n", status: "running", api_calls: 1, api_budget: 200 },
    ],
  });
  const active = nodeLifecycle(redispatched.dag.nodes[0], { jobs: redispatched.jobs });
  assert.equal(active.state, "active");
  assert.equal(active.job?.id, "prover_2");
  assert.match(active.detail, /1 \/ 200/);
});

test("conditional candidates name the prerequisites the final gate must still discharge", () => {
  const state = snapshot({
    dag: { nodes: [
      { id: "leaf", name: "leaf_lemma", status: "running" },
      { id: "n", name: "goal", status: "candidate", dependencies: ["leaf"], conditional_dependencies: ["leaf"] },
    ] },
    jobs: [{ id: "prover_1", role: "prover", node_id: "leaf", status: "running" }],
  });
  const lifecycle = nodeLifecycle(state.dag.nodes[1], { jobs: state.jobs, nodes: state.dag.nodes });
  assert.equal(lifecycle.state, "candidate");
  assert.match(lifecycle.label, /conditional/i);
  assert.match(lifecycle.detail, /leaf_lemma/);
});

test("terminal runs show no active work and keep verified results", () => {
  const state = snapshot({
    terminal: true,
    dag: { nodes: [
      { id: "a", status: "running" },
      { id: "b", status: "proved" },
      { id: "c", status: "submitted" },
    ] },
    jobs: [{ id: "prover_1", role: "prover", node_id: "a", status: "running" }],
  });
  const counts = proofStateCounts(state.dag.nodes, state.jobs, true, state.operations);
  assert.equal(counts.active, 0);
  assert.equal(counts.solved, 1);
  assert.equal(counts.candidate, 1);
  const detail = nodeLifecycle(state.dag.nodes[0], { jobs: state.jobs, terminal: true });
  assert.equal(detail.state, "pending");
  assert.match(detail.detail, /run ended|finished/i);
});

test("only the independent verifier marks a node solved", () => {
  const state = snapshot({
    dag: { nodes: [{ id: "n", status: "running" }] },
    jobs: [{ id: "prover_1", role: "prover", node_id: "n", status: "completed" }],
    operations: [{ id: "op", job_id: "prover_1", node_id: "n", kind: "submission_check", status: "completed" }],
  });
  assert.notEqual(graphProofState(state.dag.nodes[0], state.jobs, false, state.operations), "solved");
  state.dag.nodes[0].status = "proved";
  assert.equal(graphProofState(state.dag.nodes[0], state.jobs, false, state.operations), "solved");
});

test("research and check operations never count as active provers", () => {
  const state = snapshot({
    dag: { nodes: [{ id: "n", status: "pending" }] },
    jobs: [{ id: "research_2", role: "research", node_id: "n", status: "running" }],
    operations: [{ id: "op", job_id: "orchestrator_1", node_id: "n", kind: "skeleton_compile", status: "running" }],
  });
  assert.equal(graphProofState(state.dag.nodes[0], state.jobs, false, state.operations), "pending");
});

test("proposed nodes never look solved or active unless the canonical graph already proved them", () => {
  const canonical = { roots: ["root"], nodes: [
    { id: "root", name: "root", status: "pending", dependencies: [], conditional_dependencies: [], file: "", module: "", statement: "", informal_justification: "", line_start: null, line_end: null, notes: "" },
    { id: "done", name: "done", status: "proved", dependencies: [], conditional_dependencies: [], file: "", module: "", statement: "", informal_justification: "", line_start: null, line_end: null, notes: "" },
  ] };
  const jobs = [{ id: "prover_1", role: "prover", node_id: "helper", status: "running" }];
  const context = { jobs, canonical };
  const fresh = proposalNodeLifecycle({ id: "helper", status: "running" }, context);
  assert.equal(fresh.state, "pending");
  assert.match(fresh.label, /proposed/i);
  const failed = proposalNodeLifecycle({ id: "bad", status: "check_failed", notes: "syntax error" }, context);
  assert.equal(failed.state, "failed");
  assert.match(failed.label, /check failed/i);
  const kept = proposalNodeLifecycle({ id: "done", status: "pending" }, context);
  assert.equal(kept.state, "solved");
  const root = proposalNodeLifecycle({ id: "root", status: "pending" }, context);
  assert.equal(root.state, "pending");
});
