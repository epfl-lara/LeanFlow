/** Deterministic layered DAG geometry and proof states, independent of the renderer. */
import type { ProverDag, ProverJob, ProverNode, ProverOperation } from "./prover";
import { isRunningOperation, operationLabel, operationProgress } from "./proverOperations";

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
export function layoutProverGraph(dag: ProverDag): ProverGraphLayout {
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

/** Job statuses under which a session may still make a model decision. */
const LIVE_JOB = new Set(["running", "starting", "resume_pending"]);
const SOLVED = new Set(["proved", "verified", "completed"]);
/** Node statuses between a prover's submission and the verifier's verdict. */
const AWAITING: Record<string, { label: string; detail: string }> = {
  submitted: { label: "Submitted", detail: "Submitted proof awaiting the independent check." },
  verifying: { label: "Verifying", detail: "Independent verification in progress." },
  integrating: { label: "Integrating", detail: "Proof accepted by the check; it is being installed into the source." },
};
const CANDIDATE = new Set(["candidate", "conditional", "provisional"]);
const RUNNING = new Set(["running", "proving"]);
const FAILED = new Set(["failed", "invalidated", "disproved", "false", "rejected", "error", "verification_failed"]);
const BLOCKED = new Set(["blocked", "budget_exhausted", "provider_error", "environment_error", "source_conflict", "context_limit"]);
/** Proposed statements the independent skeleton check refused. */
const PROPOSAL_FAILED = new Set(["rejected", "failed", "invalid", "check_failed", "syntax_error", "error"]);

export interface LifecycleContext {
  jobs: readonly ProverJob[];
  operations?: readonly ProverOperation[];
  terminal?: boolean;
  /** All nodes, so dependency names can be spelled out. */
  nodes?: readonly ProverNode[];
}

/** One node's displayed state, the words for it, and the evidence behind it. */
export interface NodeLifecycle {
  state: ProofState;
  label: string;
  detail: string;
  job: ProverJob | null;
  operation: ProverOperation | null;
}

function calls(job: ProverJob): string {
  const used = job.api_calls === null ? "—" : job.api_calls.toLocaleString();
  return job.api_budget === null ? `${used} calls` : `${used} / ${job.api_budget.toLocaleString()} calls`;
}

function isSolved(node: Pick<ProverNode, "status">): boolean {
  return SOLVED.has(node.status);
}

/**
 * Classify one theorem for every surface at once.
 *
 * Only the independent verifier makes a node solved. Submission verification
 * takes precedence over a live job waiting synchronously for its result.
 * A completed prover whose node still says `running` is awaiting
 * the controller's verification, not active work; a failed prover never
 * implies a proof.
 */
export function nodeLifecycle(node: ProverNode, context: LifecycleContext): NodeLifecycle {
  const terminal = context.terminal === true;
  const operations = context.operations ?? [];
  const proverJobs = context.jobs.filter((job) => job.role === "prover" && job.node_id === node.id);
  const live = [...proverJobs].reverse().find((job) => LIVE_JOB.has(job.status)) ?? null;
  const latest = proverJobs.at(-1) ?? null;
  const relatedOperations = operations.filter((item) =>
    isRunningOperation(item) && (item.node_id === node.id || (live !== null && item.job_id === live.id)),
  );
  const operation = relatedOperations.find((item) => item.kind === "verification_queue") ?? relatedOperations[0] ?? null;
  const name = (id: string): string => context.nodes?.find((item) => item.id === id)?.name ?? id;
  const step = operation ? `${operationLabel(operation)}${operationProgress(operation) ? ` ${operationProgress(operation)}` : ""} running` : "";

  if (isSolved(node)) {
    return { state: "solved", label: "Solved", detail: "Independently verified and installed in the source.", job: latest, operation: null };
  }
  const awaiting = AWAITING[node.status];
  if (awaiting) {
    return {
      state: "candidate", label: awaiting.label,
      detail: `${step || awaiting.detail}${latest ? ` Submitted by ${latest.agent_id}.` : ""}`,
      job: latest, operation,
    };
  }
  if (live && !terminal) {
    return {
      state: "active", label: "Active",
      detail: `${live.agent_id} · ${calls(live)}${step ? ` · ${step}` : ""}`,
      job: live, operation,
    };
  }
  if (CANDIDATE.has(node.status)) {
    const waiting = node.conditional_dependencies.map(name);
    return waiting.length > 0
      ? { state: "candidate", label: "Conditional candidate", detail: `Candidate proof retained; waiting for ${waiting.join(", ")} before the final gate rechecks it.`, job: latest, operation }
      : { state: "candidate", label: "Awaiting verification", detail: step || "Candidate proof retained; the controller rechecks it once its dependencies are ready.", job: latest, operation };
  }
  if (RUNNING.has(node.status)) {
    if (terminal) {
      return {
        state: "pending", label: "Unreconciled",
        detail: `The run ended before the controller reconciled this theorem${latest ? `; its last prover job ${latest.agent_id} ended with ${latest.status || "no status"}` : ""}.`,
        job: latest, operation: null,
      };
    }
    if (latest && latest.status === "completed") {
      return {
        state: "candidate", label: "Pending verification",
        detail: `${latest.agent_id} completed; the controller has not yet verified or integrated its submission.${step ? ` ${step}.` : ""}`,
        job: latest, operation,
      };
    }
    if (latest) {
      return {
        state: "pending", label: "Awaiting reconciliation",
        detail: `${latest.agent_id} ended with ${latest.status || "no status"}; the controller has not yet recorded the next step. A finished job never counts as a proof.`,
        job: latest, operation,
      };
    }
    return { state: "pending", label: "Pending", detail: "Marked running, but no prover job is recorded for it.", job: null, operation };
  }
  if (node.status === "retry") {
    return {
      state: "pending", label: "Retry pending",
      detail: `Another prover pass is scheduled${node.attempts !== null ? ` (attempts so far: ${node.attempts})` : ""}.${latest ? ` Last job ${latest.agent_id} ended with ${latest.status || "no status"}.` : ""}`,
      job: latest, operation,
    };
  }
  if (FAILED.has(node.status)) {
    return { state: "failed", label: "Failed / invalidated", detail: `Recorded status: ${node.status}.`, job: latest, operation: null };
  }
  if (BLOCKED.has(node.status)) {
    return { state: "blocked", label: "Blocked", detail: `Recorded status: ${node.status}. No further prover pass is scheduled without replanning.`, job: latest, operation: null };
  }
  const unresolved = context.nodes
    ? node.dependencies.filter((id) => { const dependency = context.nodes!.find((item) => item.id === id); return dependency !== undefined && !isSolved(dependency); }).map(name)
    : [];
  return {
    state: "pending", label: "Pending",
    detail: step || (unresolved.length > 0 ? `Waiting for ${unresolved.join(", ")} to be proved.` : "Ready to be scheduled."),
    job: latest, operation,
  };
}

/** Preserve verification status; only a currently running prover implies active work. */
export function graphProofState(
  node: ProverNode, jobs: readonly ProverJob[], terminal = false, operations: readonly ProverOperation[] = [],
): ProofState {
  return nodeLifecycle(node, { jobs, terminal, operations }).state;
}

/** Aggregate counts that the header, legend, and rows all share. */
export function proofStateCounts(
  nodes: readonly ProverNode[], jobs: readonly ProverJob[], terminal = false, operations: readonly ProverOperation[] = [],
): Record<ProofState, number> {
  const counts: Record<ProofState, number> = { solved: 0, active: 0, pending: 0, candidate: 0, blocked: 0, failed: 0 };
  for (const node of nodes) counts[nodeLifecycle(node, { jobs, terminal, operations, nodes }).state]++;
  return counts;
}

/** "Solved 0 · Active 2 · Pending 2 · Awaiting verification 1", plus blocked/failed only when present. */
export function proofStateSummary(counts: Record<ProofState, number>): string {
  const parts = [
    `Solved ${counts.solved}`, `Active ${counts.active}`, `Pending ${counts.pending}`,
    `Awaiting verification ${counts.candidate}`,
  ];
  if (counts.blocked > 0) parts.push(`Blocked ${counts.blocked}`);
  if (counts.failed > 0) parts.push(`Failed ${counts.failed}`);
  return parts.join(" · ");
}

export interface ProposalContext extends LifecycleContext {
  /** The executable graph; a proposed node it already contains keeps its real progress. */
  canonical: ProverDag;
}

/**
 * Classify a node of a proposed graph.
 *
 * A proposal is not scheduled: its new statements are pending or, when the
 * skeleton check refused them, failed. Statements the canonical graph already
 * holds keep their verified progress so accepted work stays visible during
 * replanning.
 */
export function proposalNodeLifecycle(
  node: Pick<ProverNode, "id" | "status"> & Partial<ProverNode>, context: ProposalContext,
): NodeLifecycle {
  const existing = context.canonical.nodes.find((item) => item.id === node.id);
  if (existing) {
    const lifecycle = nodeLifecycle(existing, { ...context, nodes: context.canonical.nodes });
    return lifecycle.state === "pending" ? { ...lifecycle, label: "Existing statement", detail: `Already in the executable graph. ${lifecycle.detail}` } : lifecycle;
  }
  if (PROPOSAL_FAILED.has(node.status)) {
    return {
      state: "failed", label: "Check failed",
      detail: node.notes || "The independent skeleton check rejected this proposed statement; it will not be scheduled.",
      job: null, operation: null,
    };
  }
  return {
    state: "pending", label: "Proposed",
    detail: "Not in the executable graph yet. It is scheduled only after review and skeleton validation accept it.",
    job: null, operation: null,
  };
}
