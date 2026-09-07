/**
 * Normalize the versioned prover snapshot without coupling the editor to runtime internals.
 *
 * The runtime adds fields over time (operations, proposed graphs, stop reasons,
 * per-role settings). Every field here is optional on the wire: an older
 * snapshot normalizes to "unknown" rather than to a guess, and an unknown value
 * is displayed as unknown. Pure and host-free so the webview and tests share it.
 */

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
  kind: string;
  original: boolean;
  attempts: number | null;
  revision: number | null;
  /** Number of authorized proof holes in the declaration. */
  holes: number;
  /** Retained candidate proofs awaiting independent verification; text stays in the run. */
  candidate_count: number;
}

export interface ProverJob {
  id: string;
  agent_id: string;
  node_id: string;
  role: string;
  status: string;
  /** Finer lifecycle step within `status`, when the runtime records one. */
  phase: string;
  parent_job_id: string;
  purpose: string;
  result_path: string;
  report_path: string;
  api_calls: number | null;
  api_budget: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  log_path: string;
  scratch_path: string;
  /** Files the job left behind, as advertised by the run itself. */
  artifacts: string[];
  started_at: string;
  updated_at: string;
  finished_at: string;
  provider: string;
  model: string;
  reasoning_effort: string;
  context_tokens: number | null;
  compression: boolean | null;
  node_revision: number | null;
  error: string;
  /** Why this one job's allocation ended, when the runtime records it; never the campaign's reason. */
  stop_reason: ProverStopReason | null;
}

export interface ProverChange {
  path: string;
  baseline_path: string;
  /** added, modified, deleted, staged, or rolled_back. */
  status: string;
  /** For a staged change, what it becomes once the transaction commits. */
  staged_status: string;
  agent_id: string;
  /** True while the change awaits the transaction that publishes it. */
  pending: boolean;
  job_id: string;
}

/** One controller or verifier operation: a check, build, model request, or tool call. */
export interface ProverOperation {
  id: string;
  job_id: string;
  node_id: string;
  kind: string;
  label: string;
  file: string;
  /** running, completed, or failed. */
  status: string;
  started_at: string;
  updated_at: string;
  finished_at: string;
  timeout_s: number | null;
  completed: number | null;
  total: number | null;
  /** Measured duration recorded by the runtime when the operation ended. */
  elapsed_s: number | null;
  error: string;
}

export interface ProverConfig {
  mode: string;
  search_order: string;
  job_api_calls: number | null;
  max_restarts: number | null;
  plan_refinements: number | null;
  parallelism: number | null;
  total_api_calls: number | null;
  orchestrator_api_calls: number | null;
  max_nodes: number | null;
  max_decompositions: number | null;
  wall_time_s: number | null;
  timeout_s: number | null;
  model: string;
  orchestrator_model: string;
  context_tokens: number | null;
  orchestrator_context_tokens: number | null;
  compression: boolean | null;
  orchestrator_compression: boolean | null;
  fill_definitions: boolean | null;
  allowed_axioms: string[];
}

export interface ProverStopReason {
  code: string;
  scope: string;
  message: string;
  used: number | null;
  limit: number | null;
  next_step: string;
  /** Campaign calls still unspent when the run stopped, if recorded. */
  remaining_api_calls: number | null;
  /** Theorems left unproved when the run stopped, if recorded. */
  unresolved_nodes: string[];
  job_id: string;
  node_id: string;
}

export interface ProverVerification {
  present: boolean;
  accepted: boolean | null;
  error: string;
  files: string[];
  stdout: string;
  stderr: string;
  timed_out: boolean;
  returncode: number | null;
}

export interface ProverPreflight {
  present: boolean;
  accepted: boolean | null;
  error: string;
}

/** The controller's planning checkpoint: which stage jobs ran for the current request. */
export interface ProverPlanning {
  active: boolean;
  reason: string;
  refinement: boolean;
  steps: { step: string; job_id: string }[];
  accepted: boolean;
}

export interface ProverDag {
  nodes: ProverNode[];
  roots: string[];
}

export interface ProverMetrics {
  api_calls: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  cost_complete: boolean | null;
  cost_source: string;
  costed_api_calls: number | null;
  plan_refinements: number | null;
  max_plan_refinements: number | null;
  total_api_budget: number | null;
  /** Outstanding allocations, including calls already spent inside running jobs. */
  reserved_api_calls: number | null;
  remaining_reserved_api_calls: number | null;
  /** Calls the runtime can still allocate to new work; unknown unless supplied. */
  available_api_calls: number | null;
  elapsed_s: number | null;
  wall_time_s: number | null;
  decompositions: number | null;
  max_decompositions: number | null;
  max_nodes: number | null;
}

export interface ProverSnapshot {
  version: number;
  run_id: string;
  mode: string;
  phase: string;
  status: string;
  terminal: boolean;
  error: string;
  next_step: string;
  resumed_from: string;
  updated_at: string;
  /** Monotonic publication counter; preferred over `updated_at` for ordering when present. */
  sequence: number | null;
  started_at: string;
  finished_at: string;
  /** Effective provider and reasoning effort shared by every role. */
  provider: string;
  reasoning_effort: string;
  goal: string;
  targets: string[];
  config: ProverConfig;
  disproof: { node_id: string; evidence_path: string; certified: boolean };
  plan_path: string;
  plan_markdown: string;
  dag_path: string;
  /** The validated, executable graph. Always authoritative. */
  dag: ProverDag;
  /** A graph under proposal, review, or validation; never scheduled from here. */
  proposed_dag: ProverDag | null;
  proposal_status: string;
  proposed_plan: string;
  proposal_critique: string;
  jobs: ProverJob[];
  changes: ProverChange[];
  operations: ProverOperation[];
  metrics: ProverMetrics;
  stop_reason: ProverStopReason | null;
  verification: ProverVerification;
  preflight: ProverPreflight;
  planning: ProverPlanning;
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

function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function flag(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function normalizeDag(value: unknown): ProverDag {
  const dag = record(value);
  const nodes = records(dag.nodes).map((node): ProverNode => ({
    id: text(node.id), name: text(node.name) || text(node.id), statement: text(node.statement),
    informal_justification: text(node.informal_justification), file: text(node.file),
    module: text(node.module), dependencies: strings(node.dependencies),
    conditional_dependencies: strings(node.conditional_dependencies),
    line_start: number(node.line_start), line_end: number(node.line_end),
    status: text(node.status) || "pending",
    notes: text(node.notes),
    kind: text(node.kind),
    original: node.original === true,
    attempts: number(node.attempts),
    revision: number(node.revision),
    holes: Array.isArray(node.holes) ? node.holes.length : 0,
    candidate_count: Array.isArray(node.candidate) ? node.candidate.length : 0,
  })).filter((node) => node.id);
  const seen = new Set<string>();
  return {
    nodes: nodes.filter((node) => !seen.has(node.id) && Boolean(seen.add(node.id))),
    roots: strings(dag.roots),
  };
}

function normalizeConfig(value: unknown): ProverConfig {
  const config = record(value);
  return {
    mode: text(config.mode), search_order: text(config.search_order),
    job_api_calls: number(config.job_api_calls), max_restarts: number(config.max_restarts),
    plan_refinements: number(config.plan_refinements), parallelism: number(config.parallelism),
    total_api_calls: number(config.total_api_calls), orchestrator_api_calls: number(config.orchestrator_api_calls),
    max_nodes: number(config.max_nodes), max_decompositions: number(config.max_decompositions),
    wall_time_s: number(config.wall_time_s), timeout_s: number(config.timeout_s),
    model: text(config.model), orchestrator_model: text(config.orchestrator_model),
    context_tokens: number(config.context_tokens), orchestrator_context_tokens: number(config.orchestrator_context_tokens),
    compression: flag(config.compression), orchestrator_compression: flag(config.orchestrator_compression),
    fill_definitions: flag(config.fill_definitions), allowed_axioms: strings(config.allowed_axioms),
  };
}

function normalizeStopReason(value: unknown): ProverStopReason | null {
  const reason = record(value);
  if (Object.keys(reason).length === 0) {
    return null;
  }
  return {
    code: text(reason.code), scope: text(reason.scope), message: text(reason.message),
    used: number(reason.used), limit: number(reason.limit), next_step: text(reason.next_step),
    remaining_api_calls: number(reason.remaining_api_calls), unresolved_nodes: strings(reason.unresolved_nodes),
    job_id: text(reason.job_id), node_id: text(reason.node_id),
  };
}

function normalizePlanning(value: unknown): ProverPlanning {
  const planning = record(value);
  const active = Object.keys(planning).length > 0;
  const steps = Object.entries(record(planning.steps))
    .filter((entry): entry is [string, string] => typeof entry[1] === "string" && entry[1] !== "")
    .map(([step, job_id]) => ({ step, job_id }));
  return {
    active, reason: text(planning.reason), refinement: planning.refinement === true, steps,
    accepted: Object.keys(record(planning.accepted_proposal)).length > 0,
  };
}

/** Reject mismatched run identities and tolerate additive or absent snapshot fields. */
export function normalizeProverSnapshot(value: unknown, runId: string): ProverSnapshot | null {
  const raw = record(value);
  if (!runId || raw.run_id !== runId) {
    return null;
  }
  const metrics = record(raw.metrics);
  const disproof = record(raw.disproof);
  const verification = record(raw.verification);
  const preflight = record(raw.preflight);
  const phase = text(raw.phase) || text(raw.status);
  return {
    version: number(raw.version) ?? 1, run_id: runId, mode: text(raw.mode),
    phase,
    status: text(raw.status) || phase,
    // A cloned resume can retain its old phase until startup publishes `resume`.
    terminal: typeof raw.terminal === "boolean" ? raw.terminal : TERMINAL.has(phase),
    error: text(raw.error), next_step: text(raw.next_step),
    resumed_from: text(raw.resumed_from) || text(raw.parent_run_id),
    updated_at: text(raw.updated_at), sequence: number(raw.snapshot_sequence),
    started_at: text(raw.started_at), finished_at: text(raw.finished_at),
    provider: text(raw.provider), reasoning_effort: text(raw.reasoning_effort),
    goal: text(raw.goal), targets: strings(raw.targets),
    config: normalizeConfig(raw.config),
    disproof: { node_id: text(disproof.node_id), evidence_path: text(disproof.evidence_path), certified: disproof.certified === true },
    plan_path: text(raw.plan_path), plan_markdown: text(raw.plan_markdown), dag_path: text(raw.dag_path),
    dag: normalizeDag(raw.dag),
    proposed_dag: raw.proposed_dag !== null && typeof raw.proposed_dag === "object" ? normalizeDag(raw.proposed_dag) : null,
    proposal_status: text(raw.proposal_status), proposed_plan: text(raw.proposed_plan),
    proposal_critique: text(raw.proposal_critique),
    jobs: records(raw.jobs).map((job): ProverJob => ({
      id: text(job.id), agent_id: text(job.agent_id) || text(job.id), node_id: text(job.node_id),
      role: text(job.role), status: text(job.status), phase: text(job.phase), api_calls: number(job.api_calls),
      api_budget: number(job.api_budget), input_tokens: number(job.input_tokens), output_tokens: number(job.output_tokens),
      cost_usd: number(job.cost_usd), log_path: text(job.log_path), scratch_path: text(job.scratch_path),
      parent_job_id: text(job.parent_job_id), purpose: text(job.purpose), result_path: text(job.result_path),
      report_path: text(job.report_path), artifacts: strings(job.artifacts),
      started_at: text(job.started_at), updated_at: text(job.updated_at), finished_at: text(job.finished_at),
      provider: text(job.provider), model: text(job.model), reasoning_effort: text(job.reasoning_effort),
      context_tokens: number(job.context_tokens), compression: flag(job.compression),
      node_revision: number(job.node_revision), error: text(job.error),
      stop_reason: normalizeStopReason(job.stop_reason),
    })).filter((job) => job.id),
    changes: records(raw.changes).map((change): ProverChange => ({
      path: text(change.path), baseline_path: text(change.baseline_path), status: text(change.status),
      staged_status: text(change.staged_status),
      agent_id: text(change.agent_id), pending: change.pending === true || text(change.status) === "staged",
      job_id: text(change.job_id) || text(change.agent_id),
    })).filter((change) => change.path),
    operations: records(raw.operations).map((operation): ProverOperation => ({
      id: text(operation.id), job_id: text(operation.job_id), node_id: text(operation.node_id),
      kind: text(operation.kind), label: text(operation.label), file: text(operation.file),
      status: text(operation.status), started_at: text(operation.started_at),
      updated_at: text(operation.updated_at), finished_at: text(operation.finished_at),
      timeout_s: number(operation.timeout_s), completed: number(operation.completed), total: number(operation.total),
      elapsed_s: number(operation.elapsed_s), error: text(operation.error),
    })).filter((operation) => operation.id),
    metrics: {
      api_calls: number(metrics.api_calls), input_tokens: number(metrics.input_tokens),
      output_tokens: number(metrics.output_tokens), cost_usd: number(metrics.cost_usd),
      cost_complete: flag(metrics.cost_complete),
      cost_source: text(metrics.cost_source), costed_api_calls: number(metrics.costed_api_calls),
      plan_refinements: number(metrics.plan_refinements), max_plan_refinements: number(metrics.max_plan_refinements),
      total_api_budget: number(metrics.total_api_budget), reserved_api_calls: number(metrics.reserved_api_calls),
      remaining_reserved_api_calls: number(metrics.remaining_reserved_api_calls),
      available_api_calls: number(metrics.available_api_calls),
      elapsed_s: number(metrics.elapsed_s), wall_time_s: number(metrics.wall_time_s),
      decompositions: number(metrics.decompositions), max_decompositions: number(metrics.max_decompositions),
      max_nodes: number(metrics.max_nodes),
    },
    stop_reason: normalizeStopReason(raw.stop_reason),
    verification: {
      present: Object.keys(verification).length > 0, accepted: flag(verification.accepted),
      error: text(verification.error), files: strings(verification.files),
      stdout: text(verification.stdout), stderr: text(verification.stderr),
      timed_out: verification.timed_out === true, returncode: integer(verification.returncode),
    },
    preflight: {
      present: Object.keys(preflight).length > 0, accepted: flag(preflight.accepted), error: text(preflight.error),
    },
    planning: normalizePlanning(raw.planning_request),
  };
}

export interface ProverTreeRow { node: ProverNode; depth: number; reference: boolean; cycle: boolean }

/** Traverse roots toward dependencies once, showing shared dependencies as references. */
export function proverTreeRows(dag: ProverDag): ProverTreeRow[] {
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
  if (!path) return false;
  if (baseline) {
    return snapshot.changes.some((change) => change.path === path && change.baseline_path === baseline);
  }
  return [snapshot.plan_path, snapshot.dag_path, snapshot.disproof.evidence_path,
    ...snapshot.dag.nodes.map((node) => node.file),
    ...(snapshot.proposed_dag?.nodes.map((node) => node.file) ?? []),
    ...snapshot.jobs.flatMap((job) => [job.log_path, job.scratch_path, job.result_path, job.report_path, ...job.artifacts]),
    ...snapshot.changes.map((change) => change.path),
  ].some((candidate) => candidate !== "" && candidate === path);
}

/** Offer guidance only to jobs that can still receive a model decision. */
export function proverJobAcceptsGuidance(job: ProverJob): boolean {
  return !job.status || ["running", "starting", "resume_pending"].includes(job.status);
}

/** Address standard-mode guidance to its active prover; research keeps its coordinator. */
export function proverDefaultGuidanceRecipient(snapshot: ProverSnapshot): string {
  return snapshot.mode === "standard"
    ? snapshot.jobs.find((job) => job.role === "prover" && proverJobAcceptsGuidance(job))?.agent_id || "orchestrator"
    : "orchestrator";
}

/** Clear only the acknowledged draft; preserve failures, edits and unrelated requests. */
export function proverGuidanceUpdate(
  draft: string,
  pending: { runId: string; requestId: string; message: string } | null,
  reply: { runId: string; requestId: string; success: boolean; error: string },
): { draft: string; error: string } | null {
  if (!pending || pending.runId !== reply.runId || pending.requestId !== reply.requestId) return null;
  return {
    draft: reply.success && draft === pending.message ? "" : draft,
    error: reply.success ? "" : reply.error,
  };
}
