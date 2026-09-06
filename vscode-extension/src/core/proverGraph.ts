/** Deterministic layered DAG geometry and proof states, independent of the renderer. */
import type { ProverJob, ProverNode, ProverSnapshot } from "./prover";

export const GRAPH_NODE_WIDTH = 236;
export const GRAPH_NODE_HEIGHT = 94;
const GAP_X = 36;
const GAP_Y = 76;
const MARGIN = 32;

export interface GraphPlacement { node: ProverNode; x: number; y: number; rank: number }
export interface GraphEdge { from: string; to: string; path: string }
export interface ProverGraphLayout {
  nodes: GraphPlacement[]; edges: GraphEdge[]; width: number; height: number;
  missingDependencies: number; cyclic: boolean;
}

/** Position every declaration once, retaining shared edges and disconnected components. */
export function layoutProverGraph(dag: ProverSnapshot["dag"]): ProverGraphLayout {
  const byId = new Map(dag.nodes.map((node) => [node.id, node]));
  const incoming = new Map(dag.nodes.map((node) => [node.id, [] as string[]]));
  const outgoing = new Map<string, string[]>();
  let missingDependencies = 0;
  for (const node of dag.nodes) {
    const dependencies = [...new Set(node.dependencies)];
    outgoing.set(node.id, dependencies.filter((id) => byId.has(id)));
    for (const id of dependencies) {
      if (byId.has(id)) incoming.get(id)!.push(node.id);
      else missingDependencies++;
    }
  }
  const remaining = new Map([...incoming].map(([id, parents]) => [id, parents.length]));
  const ranks = new Map(dag.nodes.map((node) => [node.id, 0]));
  const queue = dag.nodes.filter((node) => !remaining.get(node.id)).map((node) => node.id);
  for (let cursor = 0; cursor < queue.length; cursor++) {
    const id = queue[cursor];
    for (const dependency of outgoing.get(id)!) {
      ranks.set(dependency, Math.max(ranks.get(dependency)!, ranks.get(id)! + 1));
      remaining.set(dependency, remaining.get(dependency)! - 1);
      if (!remaining.get(dependency)) queue.push(dependency);
    }
  }
  const cyclic = queue.length < dag.nodes.length;
  // Corrupt cyclic components remain inspectable instead of hanging or disappearing.
  const fallbackRank = queue.reduce((rank, id) => Math.max(rank, ranks.get(id)! + 1), 0);
  for (const node of dag.nodes) if (remaining.get(node.id)) ranks.set(node.id, fallbackRank);
  const layers: ProverNode[][] = [];
  for (const node of dag.nodes) (layers[ranks.get(node.id)!] ??= []).push(node);
  const slots = new Map<string, number>();
  const updateSlots = () => layers.forEach((layer) => layer.forEach((node, i) => slots.set(node.id, i)));
  updateSlots();
  // Alternate parent/child barycenters to reduce crossings without status-driven motion.
  for (let pass = 0; pass < 4; pass++) {
    const order = pass % 2 ? [...layers].reverse() : layers;
    const neighbors = pass % 2 ? outgoing : incoming;
    for (const layer of order) {
      const scores = new Map(layer.map((node) => {
        const adjacent = neighbors.get(node.id)!;
        return [node.id, adjacent.length ? adjacent.reduce((sum, id) => sum + slots.get(id)!, 0) / adjacent.length : slots.get(node.id)!];
      }));
      layer.sort((a, b) => scores.get(a.id)! - scores.get(b.id)! || slots.get(a.id)! - slots.get(b.id)!);
      layer.forEach((node, i) => slots.set(node.id, i));
    }
  }
  const widest = layers.reduce((width, layer) => Math.max(width, layer.length), 1);
  const width = MARGIN * 2 + widest * (GRAPH_NODE_WIDTH + GAP_X) - GAP_X;
  const nodes = layers.flatMap((layer, rank) => layer.map((node, i) => ({
    node, rank,
    x: (width - (layer.length * (GRAPH_NODE_WIDTH + GAP_X) - GAP_X)) / 2 + i * (GRAPH_NODE_WIDTH + GAP_X),
    y: MARGIN + rank * (GRAPH_NODE_HEIGHT + GAP_Y),
  })));
  const positions = new Map(nodes.map((node) => [node.node.id, node]));
  const edges = nodes.flatMap((from) => outgoing.get(from.node.id)!.map((id) => {
    const to = positions.get(id)!;
    const x1 = from.x + GRAPH_NODE_WIDTH / 2, y1 = from.y + GRAPH_NODE_HEIGHT;
    const x2 = to.x + GRAPH_NODE_WIDTH / 2, y2 = to.y;
    const middle = (y1 + y2) / 2;
    return { from: from.node.id, to: id, path: y2 > y1
      ? `M ${x1} ${y1} C ${x1} ${middle}, ${x2} ${middle}, ${x2} ${y2}`
      : `M ${from.x + GRAPH_NODE_WIDTH} ${from.y + 62} C ${width - 8} ${from.y + 62}, ${width - 8} ${to.y + 28}, ${to.x + GRAPH_NODE_WIDTH} ${to.y + 28}` };
  }));
  return { nodes, edges, width, height: MARGIN * 2 + Math.max(1, layers.length) * (GRAPH_NODE_HEIGHT + GAP_Y) - GAP_Y, missingDependencies, cyclic };
}

export type ProofState = "solved" | "active" | "pending" | "candidate" | "blocked" | "failed";
export const PROOF_STATES: Record<ProofState, { label: string; icon: string }> = {
  solved: { label: "Solved", icon: "✓" }, active: { label: "Active", icon: "▶" },
  pending: { label: "Pending", icon: "○" }, candidate: { label: "Awaiting verification", icon: "◇" },
  blocked: { label: "Blocked", icon: "Ⅱ" }, failed: { label: "Failed / invalidated", icon: "×" },
};

/** Preserve verification status; only a currently running prover implies active work. */
export function graphProofState(node: ProverNode, jobs: ProverJob[], terminal = false): ProofState {
  if (["proved", "verified", "completed"].includes(node.status)) return "solved";
  if (!terminal && (jobs.some((job) => job.node_id === node.id && job.role === "prover" && ["running", "starting", "resume_pending"].includes(job.status)) || ["running", "proving"].includes(node.status))) return "active";
  if (["candidate", "conditional", "provisional"].includes(node.status)) return "candidate";
  if (["failed", "invalidated", "disproved", "false", "rejected", "error", "verification_failed"].includes(node.status)) return "failed";
  if (["blocked", "budget_exhausted", "provider_error", "environment_error", "source_conflict", "context_limit"].includes(node.status)) return "blocked";
  return "pending";
}
