/** Exact-run identity, dependency rendering, and artifact authority tests. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { normalizeProverSnapshot, proverTreeRows, proverArtifactAllowed, proverJobAcceptsGuidance } from "../dist/test/prover.mjs";
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
