import test from "node:test";
import assert from "node:assert/strict";
import { normalizeProverSnapshot } from "../dist/test/prover.mjs";
import { layoutProverGraph, graphProofState, GRAPH_NODE_WIDTH, GRAPH_NODE_HEIGHT } from "../dist/test/proverGraph.mjs";

function state(nodes, jobs = [], roots = ["goal"]) {
  return normalizeProverSnapshot({ run_id: "graph", dag: { nodes, roots }, jobs }, "graph");
}

test("diamond dependencies converge on one shared theorem and preserve every arrow", () => {
  const input = state([
    { id: "goal", dependencies: ["left", "right", "left"] },
    { id: "left", dependencies: ["shared"] }, { id: "right", dependencies: ["shared"] },
    { id: "shared" }, { id: "separate" },
  ]);
  const graph = layoutProverGraph(input.dag);
  assert.equal(graph.nodes.length, 5);
  assert.equal(graph.edges.length, 4);
  assert.equal(graph.cyclic, false);
  const positions = new Map(graph.nodes.map((item) => [item.node.id, item]));
  for (const edge of graph.edges) {
    assert.ok(positions.get(edge.to).y > positions.get(edge.from).y + GRAPH_NODE_HEIGHT);
    assert.ok(!edge.path.includes("NaN"));
  }
  assert.ok(Math.abs(positions.get("left").x - positions.get("right").x) >= GRAPH_NODE_WIDTH);
  assert.equal(positions.get("shared").rank, 2);
  assert.equal(positions.get("separate").rank, 0);
});

test("long edges respect all prerequisites and status polling never moves nodes", () => {
  const original = state([
    { id: "goal", dependencies: ["a", "end"] }, { id: "a", dependencies: ["end"] }, { id: "end" },
  ]);
  const before = layoutProverGraph(original.dag);
  original.dag.nodes.forEach((node) => { node.status = "proved"; });
  const after = layoutProverGraph(original.dag);
  const geometry = (graph) => graph.nodes.map(({ node, x, y }) => [node.id, x, y]);
  assert.deepEqual(geometry(before), geometry(after));
  assert.equal(after.nodes.find((n) => n.node.id === "end").rank, 2);
});

test("cycles, self edges and absent dependencies are visible and bounded", () => {
  const graph = layoutProverGraph(state([
    { id: "goal", dependencies: ["missing", "a"] },
    { id: "a", dependencies: ["b"] }, { id: "b", dependencies: ["a"] },
    { id: "self", dependencies: ["self"] },
  ]).dag);
  assert.equal(graph.cyclic, true);
  assert.equal(graph.missingDependencies, 1);
  assert.equal(graph.nodes.length, 4);
  assert.equal(graph.edges.length, 4);
  assert.ok(graph.edges.every((edge) => !edge.path.includes("NaN")));
});

test("empty and deep graphs do not overflow recursive traversal or produce invalid bounds", () => {
  assert.equal(layoutProverGraph(state([]).dag).nodes.length, 0);
  const nodes = Array.from({ length: 10000 }, (_, i) => ({ id: `${i}`, dependencies: i < 9999 ? [`${i + 1}`] : [] }));
  const graph = layoutProverGraph(state(nodes).dag);
  assert.equal(graph.nodes.length, nodes.length);
  assert.equal(graph.edges.length, nodes.length - 1);
  assert.ok(Number.isFinite(graph.height));
  assert.equal(graph.nodes.at(-1).rank, 9999);
});

test("a running prover is active while research jobs and terminal runs cannot imply proving", () => {
  const input = state([{ id: "goal" }], [{ id: "worker", node_id: "goal", role: "prover", status: "running" }]);
  assert.equal(graphProofState(input.dag.nodes[0], input.jobs), "active");
  assert.equal(graphProofState(input.dag.nodes[0], input.jobs, true), "pending");
  input.jobs[0].role = "research";
  assert.equal(graphProofState(input.dag.nodes[0], input.jobs), "pending");
  input.jobs[0].role = "prover"; input.jobs[0].status = "completed";
  assert.equal(graphProofState(input.dag.nodes[0], input.jobs), "pending");
});

test("candidates never appear solved and verified results override stale job activity", () => {
  const input = state([{ id: "goal" }], [{ id: "worker", node_id: "goal", role: "prover", status: "running" }]);
  const node = input.dag.nodes[0];
  for (const status of ["candidate", "conditional", "provisional"]) {
    node.status = status;
    assert.equal(graphProofState(node, []), "candidate");
  }
  node.status = "proved";
  assert.equal(graphProofState(node, input.jobs), "solved");
  node.status = "invalidated";
  assert.equal(graphProofState(node, []), "failed");
  node.status = "budget_exhausted";
  assert.equal(graphProofState(node, []), "blocked");
});
