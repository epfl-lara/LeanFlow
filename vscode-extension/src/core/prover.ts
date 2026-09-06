/** Normalize the versioned prover snapshot without coupling the editor to runtime internals. */

export interface ProverNode {
  id: string;
  name: string;
  statement: string;
  informal_justification: string;
  file: string;
  module: string;
  dependencies: string[];
  conditional_dependencies: string[];
  line_start: number | null;
  line_end: number | null;
  status: string;
  notes: string;
}

export interface ProverJob {
  id: string;
  agent_id: string;
  node_id: string;
  role: string;
  status: string;
  parent_job_id: string;
  purpose: string;
  result_path: string;
  api_calls: number | null;
  api_budget: number | null;
  log_path: string;
  scratch_path: string;
}

export interface ProverChange {
  path: string;
  baseline_path: string;
  status: string;
  agent_id: string;
}

export interface ProverSnapshot {
  version: number;
  run_id: string;
  mode: string;
  phase: string;
  terminal: boolean;
  error: string;
  next_step: string;
  resumed_from: string;
  disproof: { node_id: string; evidence_path: string; certified: boolean };
  plan_path: string;
  plan_markdown: string;
  dag_path: string;
  dag: { nodes: ProverNode[]; roots: string[] };
  jobs: ProverJob[];
  changes: ProverChange[];
  metrics: {
    api_calls: number | null;
    input_tokens: number | null;
    output_tokens: number | null;
    cost_usd: number | null;
    cost_complete: boolean | null;
    plan_refinements: number | null;
    max_plan_refinements: number | null;
  };
}

const TERMINAL = new Set([
  "complete", "completed", "succeeded", "failed", "cancelled", "canceled",
  "stopped", "exhausted", "budget-exhausted", "disproved", "interrupted",
  "provider_error", "environment_error", "source_conflict", "verification_failed",
  "budget_exhausted", "error", "blocked", "context_limit",
]);

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}

/** Reject mismatched run identities and tolerate additive or absent snapshot fields. */
export function normalizeProverSnapshot(value: unknown, runId: string): ProverSnapshot | null {
  const raw = record(value);
  if (!runId || raw.run_id !== runId) {
    return null;
  }
  const dag = record(raw.dag);
  const metrics = record(raw.metrics);
  const disproof = record(raw.disproof);
  const nodes = records(dag.nodes).map((node): ProverNode => ({
    id: text(node.id), name: text(node.name) || text(node.id), statement: text(node.statement),
    informal_justification: text(node.informal_justification), file: text(node.file),
    module: text(node.module), dependencies: strings(node.dependencies),
    conditional_dependencies: strings(node.conditional_dependencies),
    line_start: number(node.line_start), line_end: number(node.line_end),
    status: text(node.status) || "pending",
    notes: text(node.notes),
  })).filter((node) => node.id);
  const seen = new Set<string>();
  const uniqueNodes = nodes.filter((node) => !seen.has(node.id) && Boolean(seen.add(node.id)));
  return {
    version: number(raw.version) ?? 1, run_id: runId, mode: text(raw.mode),
    phase: text(raw.phase) || text(raw.status),
    // A cloned resume can retain its old phase until startup publishes `resume`.
    terminal: typeof raw.terminal === "boolean" ? raw.terminal : TERMINAL.has(text(raw.phase) || text(raw.status)),
    error: text(raw.error), next_step: text(raw.next_step),
    resumed_from: text(raw.resumed_from) || text(raw.parent_run_id),
    disproof: { node_id: text(disproof.node_id), evidence_path: text(disproof.evidence_path), certified: disproof.certified === true },
    plan_path: text(raw.plan_path), plan_markdown: text(raw.plan_markdown), dag_path: text(raw.dag_path),
    dag: { nodes: uniqueNodes, roots: strings(dag.roots) },
    jobs: records(raw.jobs).map((job) => ({
      id: text(job.id), agent_id: text(job.agent_id) || text(job.id), node_id: text(job.node_id),
      role: text(job.role), status: text(job.status), api_calls: number(job.api_calls),
      api_budget: number(job.api_budget), log_path: text(job.log_path), scratch_path: text(job.scratch_path),
      parent_job_id: text(job.parent_job_id), purpose: text(job.purpose), result_path: text(job.result_path),
    })).filter((job) => job.id),
    changes: records(raw.changes).map((change) => ({
      path: text(change.path), baseline_path: text(change.baseline_path), status: text(change.status),
      agent_id: text(change.agent_id),
    })).filter((change) => change.path),
    metrics: {
      api_calls: number(metrics.api_calls), input_tokens: number(metrics.input_tokens),
      output_tokens: number(metrics.output_tokens), cost_usd: number(metrics.cost_usd),
      cost_complete: typeof metrics.cost_complete === "boolean" ? metrics.cost_complete : null,
      plan_refinements: number(metrics.plan_refinements), max_plan_refinements: number(metrics.max_plan_refinements),
    },
  };
}

export interface ProverTreeRow { node: ProverNode; depth: number; reference: boolean; cycle: boolean }

/** Traverse roots toward dependencies once, showing shared dependencies as references. */
export function proverTreeRows(dag: ProverSnapshot["dag"]): ProverTreeRow[] {
  const byId = new Map(dag.nodes.map((node) => [node.id, node]));
  const dependencies = new Set(dag.nodes.flatMap((node) => node.dependencies));
  const roots = [...dag.roots, ...dag.nodes.filter((node) => !dependencies.has(node.id)).map((node) => node.id)];
  const visited = new Set<string>();
  const rows: ProverTreeRow[] = [];
  const visit = (root: string): void => {
    const active = new Set<string>();
    const pending = [{ id: root, depth: 0, exit: false }];
    // An explicit stack handles large project DAGs without overflowing JavaScript's call stack.
    while (pending.length) {
      const item = pending.pop()!;
      if (item.exit) { active.delete(item.id); continue; }
      const node = byId.get(item.id);
      if (!node) continue;
      const reference = visited.has(item.id);
      rows.push({ node, depth: item.depth, reference, cycle: active.has(item.id) });
      if (reference) continue;
      visited.add(item.id);
      active.add(item.id);
      pending.push({ ...item, exit: true });
      for (const id of [...node.dependencies].reverse()) pending.push({ id, depth: item.depth + 1, exit: false });
    }
  };
  for (const id of roots) if (!visited.has(id)) visit(id);
  // Disconnected or corrupt cyclic components remain visible rather than disappearing.
  for (const node of dag.nodes) if (!visited.has(node.id)) visit(node.id);
  return rows;
}

/** Allow only artifacts actually advertised by this exact run, then validate real paths in the host. */
export function proverArtifactAllowed(snapshot: ProverSnapshot, path: string, baseline = ""): boolean {
  if (baseline) {
    return snapshot.changes.some((change) => change.path === path && change.baseline_path === baseline);
  }
  return [snapshot.plan_path, snapshot.dag_path, snapshot.disproof.evidence_path,
    ...snapshot.dag.nodes.map((node) => node.file),
    ...snapshot.jobs.flatMap((job) => [job.log_path, job.scratch_path, job.result_path]),
    ...snapshot.changes.map((change) => change.path),
  ].some((candidate) => candidate !== "" && candidate === path);
}

/** Offer guidance only to jobs that can still receive a model decision. */
export function proverJobAcceptsGuidance(job: ProverJob): boolean {
  return !job.status || ["running", "starting", "resume_pending"].includes(job.status);
}
