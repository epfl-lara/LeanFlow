/**
 * What the dashboard says about a campaign's budget, capacity, and controller
 * work, as pure functions of the normalized snapshot.
 *
 * Every figure here answers one of the questions a person asks while a
 * campaign runs: how much budget is left, why are prover slots idle, what is
 * the controller checking right now, and why did the run stop. Unknown stays
 * unknown; nothing is estimated from other fields.
 */
import type { ProverChange, ProverDag, ProverJob, ProverOperation, ProverSnapshot, ProverStopReason } from "./prover";
import { proofStateCounts } from "./proverGraph";
import {
  isCheckOperation,
  isRunningOperation,
  operationDurationS,
  operationLabel,
  operationProgress,
} from "./proverOperations";

export { operationLabel } from "./proverOperations";

const LIVE_JOB = new Set(["running", "starting"]);
const SOLVED = new Set(["proved", "verified", "completed"]);
const CONTROLLER_ROLES = new Set(["orchestrator", "review", "research"]);
const CONTROLLER_PHASES = new Set(["starting", "inspect", "preflight", "planning", "reviewing", "validating", "verifying", "final_build", "resume"]);
export const STALE_AFTER_MS = 120_000;
/** Operation kinds that are one job's own model turn or tool call rather than controller work. */
const JOB_OPERATION_KINDS = new Set(["model_request", "tool"]);

const PHASE_LABELS: Record<string, string> = {
  starting: "Starting",
  inspect: "Inspecting",
  preflight: "Preflight",
  planning: "Planning",
  reviewing: "Reviewing",
  validating: "Validating",
  proving: "Proving",
  resume: "Resuming",
  verifying: "Final verification",
  final_build: "Final build",
  completed: "Completed",
  complete: "Completed",
  succeeded: "Completed",
  budget_exhausted: "Budget exhausted",
  "budget-exhausted": "Budget exhausted",
  verification_failed: "Verification failed",
  disproved: "Disproved",
  interrupted: "Interrupted",
  stopped: "Stopped",
  provider_error: "Provider error",
  environment_error: "Environment error",
  source_conflict: "Source conflict",
  context_limit: "Context limit",
  error: "Error",
  blocked: "Blocked",
  failed: "Failed",
};

const PHASE_EXPLANATIONS: Record<string, string> = {
  starting: "the controller is starting up",
  inspect: "the controller is reading the target files and their proof holes",
  preflight: "the controller is checking the isolated Lean environment before any paid request",
  planning: "the orchestrator is designing the informal proof outline and dependency graph",
  reviewing: "an independent reviewer is checking the proposed graph, after which helper skeletons are compiled and protected signatures checked",
  validating: "helper skeletons are being compiled and protected signatures checked before provers are dispatched",
  verifying: "the complete project is being built as the final independent check",
  final_build: "the complete project is being built as the final independent check",
  resume: "durable progress is being restored before work continues",
};

function words(value: string): string {
  const text = value.replace(/[-_]+/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

function live(job: ProverJob): boolean {
  return LIVE_JOB.has(job.status);
}

function count(value: number | null): string {
  return value === null ? "—" : value.toLocaleString();
}

/** A readable phase, whether the runtime spelled it as a phase or a terminal status. */
export function proverPhaseLabel(snapshot: Pick<ProverSnapshot, "phase" | "status">): string {
  const phase = snapshot.phase || snapshot.status;
  return PHASE_LABELS[phase] ?? words(phase) ?? "Unknown";
}

/** Milliseconds since the snapshot was written, or null when it carries no timestamp. */
export function snapshotAgeMs(snapshot: Pick<ProverSnapshot, "updated_at">, nowMs: number): number | null {
  const updated = Date.parse(snapshot.updated_at);
  return Number.isNaN(updated) ? null : Math.max(0, nowMs - updated);
}

// ---------------------------------------------------------------------- budget

export interface BudgetView {
  usedCalls: number | null;
  totalCalls: number | null;
  /** Calls the runtime can still allocate to new work; unknown unless it says so. */
  availableCalls: number | null;
  /** Outstanding allocations, including calls already spent inside running jobs. */
  reservedCalls: number | null;
  remainingReservedCalls: number | null;
  elapsedS: number | null;
  wallTimeS: number | null;
  /** True when `elapsedS` is advancing from the snapshot's own clock. */
  elapsedAdvancing: boolean;
  planRefinements: { used: number | null; max: number | null };
  decompositions: { used: number | null; max: number | null };
  nodes: { used: number; max: number | null };
  maxRestartsPerNode: number | null;
  jobCalls: number | null;
  orchestratorCalls: number | null;
  cost: { usd: number | null; complete: boolean | null; source: string };
  tokens: { input: number | null; output: number | null };
}

/** The complete campaign budget. Reservations are never subtracted from spend again. */
export function proverBudget(snapshot: ProverSnapshot, nowMs: number): BudgetView {
  const metrics = snapshot.metrics;
  const config = snapshot.config;
  const updated = Date.parse(snapshot.updated_at);
  const elapsedAdvancing = !snapshot.terminal && !Number.isNaN(updated) && metrics.elapsed_s !== null
    && nowMs - updated <= STALE_AFTER_MS;
  const elapsedS = metrics.elapsed_s === null
    ? null
    : elapsedAdvancing
      ? metrics.elapsed_s + Math.max(0, (nowMs - updated) / 1000)
      : metrics.elapsed_s;
  return {
    usedCalls: metrics.api_calls,
    totalCalls: metrics.total_api_budget ?? config.total_api_calls,
    availableCalls: metrics.available_api_calls,
    reservedCalls: metrics.reserved_api_calls,
    remainingReservedCalls: metrics.remaining_reserved_api_calls,
    elapsedS,
    wallTimeS: metrics.wall_time_s ?? config.wall_time_s,
    elapsedAdvancing,
    planRefinements: { used: metrics.plan_refinements, max: metrics.max_plan_refinements ?? config.plan_refinements },
    decompositions: { used: metrics.decompositions, max: metrics.max_decompositions ?? config.max_decompositions },
    nodes: { used: snapshot.dag.nodes.length, max: metrics.max_nodes ?? config.max_nodes },
    maxRestartsPerNode: config.max_restarts,
    jobCalls: config.job_api_calls,
    orchestratorCalls: config.orchestrator_api_calls,
    cost: { usd: metrics.cost_usd, complete: metrics.cost_complete, source: metrics.cost_source },
    tokens: { input: metrics.input_tokens, output: metrics.output_tokens },
  };
}

// -------------------------------------------------------------------- capacity

export interface CapacityView {
  controllerActive: boolean;
  controllerJob: ProverJob | null;
  proverActive: number;
  proverLimit: number | null;
  /** Research jobs requested by provers, which run inside the prover's own allocation. */
  researchActive: number;
  /** Independent checks or builds in flight. These are not agents. */
  checksActive: number;
  readyNodes: number;
  /** Why prover slots are idle, or "" when they are not. */
  waitingReason: string;
}

function readyNodeCount(dag: ProverDag, jobs: readonly ProverJob[]): number {
  const byId = new Map(dag.nodes.map((node) => [node.id, node]));
  const busy = new Set(jobs.filter((job) => job.role === "prover" && live(job)).map((job) => job.node_id));
  return dag.nodes.filter((node) =>
    (node.status === "pending" || node.status === "retry") &&
    !busy.has(node.id) &&
    node.dependencies.every((id) => { const dependency = byId.get(id); return dependency === undefined || SOLVED.has(dependency.status); }),
  ).length;
}

function validationReason(operations: readonly ProverOperation[]): string {
  const running = operations.filter((operation) => isRunningOperation(operation) && isCheckOperation(operation));
  if (running.length === 0) {
    return "";
  }
  const names = running.map((operation) => {
    const progress = operationProgress(operation);
    return `${operationLabel(operation)}${progress ? ` (${progress})` : ""}`;
  });
  if (running.every((operation) => ["submission_check", "proof_integration"].includes(operation.kind))) {
    return `Verification: ${names.join(", ")} ${running.length === 1 ? "is" : "are"} running; retained proofs are checked and installed before dependent provers can start.`;
  }
  return `Validation: ${names.join(", ")} ${running.length === 1 ? "is" : "are"} running; helper skeletons are compiled and protected signatures checked before provers are dispatched.`;
}

/** One controller versus the prover slots, with the reason idle slots stay idle. */
export function proverCapacity(snapshot: ProverSnapshot): CapacityView {
  const jobs = snapshot.jobs;
  const mode = snapshot.mode || snapshot.config.mode;
  const controllerJob = jobs.find((job) => CONTROLLER_ROLES.has(job.role) && live(job) && !job.parent_job_id) ?? null;
  const checksActive = snapshot.operations.filter((operation) => isRunningOperation(operation) && isCheckOperation(operation)).length;
  const phase = snapshot.phase;
  const controllerActive = !snapshot.terminal && (controllerJob !== null || checksActive > 0 || CONTROLLER_PHASES.has(phase));
  const proverActive = jobs.filter((job) => job.role === "prover" && live(job)).length;
  const researchActive = jobs.filter((job) => job.role === "research" && live(job) && Boolean(job.parent_job_id)).length;
  const proverLimit = mode === "standard" ? 1 : snapshot.config.parallelism;
  const readyNodes = readyNodeCount(snapshot.dag, jobs);
  const counts = proofStateCounts(snapshot.dag.nodes, jobs, snapshot.terminal, snapshot.operations);

  let waitingReason = "";
  if (snapshot.terminal) {
    waitingReason = "";
  } else if (proverLimit !== null && proverActive >= proverLimit) {
    waitingReason = "";
  } else if (checksActive > 0) {
    waitingReason = validationReason(snapshot.operations);
  } else if (PHASE_EXPLANATIONS[phase]) {
    waitingReason = `${PHASE_LABELS[phase] ?? words(phase)}: ${PHASE_EXPLANATIONS[phase]}.`;
    if (["planning", "reviewing", "validating"].includes(phase)) {
      waitingReason += " Provers start after review and validation.";
    }
  } else if (readyNodes > 0 && snapshot.metrics.available_api_calls !== null && snapshot.config.job_api_calls !== null
    && snapshot.metrics.available_api_calls < snapshot.config.job_api_calls) {
    waitingReason = snapshot.metrics.available_api_calls === 0
      ? "No unreserved calls are available; outstanding jobs must finish before another allocation can be made."
      : `The next prover pass can receive the remaining ${count(snapshot.metrics.available_api_calls)} calls, below its usual ${count(snapshot.config.job_api_calls)}-call ceiling.`;
  } else if (readyNodes > 0) {
    waitingReason = `${readyNodes} ready ${readyNodes === 1 ? "theorem is" : "theorems are"} waiting to be dispatched.`;
  } else if (counts.pending > 0) {
    waitingReason = `Dependencies: ${counts.pending} pending ${counts.pending === 1 ? "theorem is" : "theorems are"} waiting for prerequisites still being proved or verified.`;
  } else if (counts.candidate > 0) {
    waitingReason = `Awaiting verification: ${counts.candidate} ${counts.candidate === 1 ? "proof is" : "proofs are"} waiting for the independent check or integration; no theorem is ready for a prover.`;
  } else {
    waitingReason = "No theorem is waiting: every statement is proved, blocked, or failed.";
  }
  return { controllerActive, controllerJob, proverActive, proverLimit, researchActive, checksActive, readyNodes, waitingReason };
}

// ------------------------------------------------------------------ controller

export interface OperationView {
  operation: ProverOperation;
  label: string;
  durationS: number | null;
  progress: string;
  running: boolean;
  failed: boolean;
  timeoutS: number | null;
}

export interface PlanningStepView {
  step: string;
  label: string;
  job: ProverJob | null;
}

export interface FinalBuildView {
  accepted: boolean | null;
  summary: string;
  stdout: string;
  stderr: string;
}

export interface ControllerView {
  phase: string;
  phaseLabel: string;
  explanation: string;
  active: OperationView[];
  recent: OperationView[];
  /** Why provers are waiting on the controller, or "" when they are not. */
  waitingForProvers: string;
  planningSteps: PlanningStepView[];
  finalBuild: FinalBuildView | null;
  preflight: { accepted: boolean | null; error: string } | null;
}

const MAX_RECENT_OPERATIONS = 8;

function operationView(operation: ProverOperation, nowMs: number): OperationView {
  return {
    operation,
    label: operationLabel(operation),
    durationS: operationDurationS(operation, nowMs),
    progress: operationProgress(operation),
    running: isRunningOperation(operation),
    failed: operation.status === "failed",
    timeoutS: operation.timeout_s,
  };
}

function stepLabel(step: string): string {
  if (step === "outline") return "Informal outline";
  if (step.startsWith("proposal")) return `Graph proposal${step.includes("-") ? ` ${Number(step.split("-")[1]) + 1}` : ""}`;
  if (step.startsWith("review")) return `Semantic review${step.includes("-") ? ` ${Number(step.split("-")[1]) + 1}` : ""}`;
  return words(step);
}

function lastLine(text: string): string {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  return lines.at(-1) ?? "";
}

function finalBuildView(snapshot: ProverSnapshot): FinalBuildView | null {
  const verification = snapshot.verification;
  if (!verification.present) {
    return null;
  }
  let summary: string;
  if (verification.accepted === true) {
    summary = lastLine(verification.stdout) || "The final project build passed.";
  } else {
    const parts = [verification.error || "The final project build did not pass."];
    if (verification.files.length > 0) parts.push(`: ${verification.files.join(", ")}`);
    if (verification.timed_out) parts.push(" (timed out)");
    if (verification.returncode !== null && verification.returncode !== 0) parts.push(` (exit ${verification.returncode})`);
    const tail = lastLine(verification.stderr) || lastLine(verification.stdout);
    if (tail && !verification.error) parts.push(` — ${tail}`);
    summary = parts.join("");
  }
  return { accepted: verification.accepted, summary, stdout: verification.stdout, stderr: verification.stderr };
}

/** The controller's current work: active checks, recent ones, and the reason provers wait. */
export function controllerActivity(snapshot: ProverSnapshot, nowMs: number): ControllerView {
  const phase = snapshot.phase || snapshot.status;
  const isJobWork = (operation: ProverOperation) => JOB_OPERATION_KINDS.has(operation.kind);
  // Controller checks first, then each job's own model turns and tool calls.
  const active = snapshot.operations
    .filter(isRunningOperation)
    .sort((left, right) => Number(isJobWork(left)) - Number(isJobWork(right)) || left.started_at.localeCompare(right.started_at))
    .map((operation) => operationView(operation, nowMs));
  const recent = snapshot.operations
    .filter((operation) => !isRunningOperation(operation))
    .sort((left, right) => (right.finished_at || right.updated_at).localeCompare(left.finished_at || left.updated_at))
    .slice(0, MAX_RECENT_OPERATIONS)
    .map((operation) => operationView(operation, nowMs));
  const checks = active.filter((item) => !isJobWork(item.operation));
  const jobWork = active.length - checks.length;
  let explanation: string;
  if (checks.length > 0) {
    explanation = checks.map((item) => {
      const where = item.operation.file || item.operation.node_id;
      return `${item.label}${item.progress ? ` ${item.progress}` : ""}${where ? ` · ${where}` : ""}`;
    }).join("; ");
  } else if (jobWork > 0) {
    explanation = `${jobWork} model ${jobWork === 1 ? "request or tool call is" : "requests or tool calls are"} in flight inside agent jobs; no controller check is running`;
  } else if (snapshot.terminal) {
    explanation = `The run has finished with ${proverPhaseLabel(snapshot).toLowerCase()}.`;
  } else if (PHASE_EXPLANATIONS[phase]) {
    explanation = `${PHASE_EXPLANATIONS[phase].charAt(0).toUpperCase()}${PHASE_EXPLANATIONS[phase].slice(1)}. This runtime version does not report per-check progress for this phase.`;
  } else {
    explanation = "";
  }
  const planningSteps = snapshot.planning.steps.map((entry) => ({
    step: entry.step,
    label: stepLabel(entry.step),
    job: snapshot.jobs.find((job) => job.id === entry.job_id) ?? null,
  }));
  return {
    phase,
    phaseLabel: proverPhaseLabel(snapshot),
    explanation,
    active,
    recent,
    waitingForProvers: proverCapacity(snapshot).waitingReason,
    planningSteps,
    finalBuild: finalBuildView(snapshot),
    preflight: snapshot.preflight.present ? { accepted: snapshot.preflight.accepted, error: snapshot.preflight.error } : null,
  };
}

// ----------------------------------------------------------------- stop reason

export interface StopReasonView {
  title: string;
  message: string;
  scope: string;
  scopeLabel: string;
  usage: string;
  nextStep: string;
  /** Campaign-level spend, stated separately so a local limit is never read as the campaign budget. */
  campaign: string;
  /** Theorems the runtime recorded as unresolved at the stop, if any. */
  unresolved: string[];
  tone: "info" | "warn" | "error";
  /** True when the snapshot predates recorded stop reasons and the scope is inferred from status alone. */
  legacy: boolean;
}

/** Reason codes first, then coarse scopes; the runtime emits both. */
const SCOPE_LABELS: [RegExp, string][] = [
  [/^(campaign_calls|campaign_api|campaign_api_calls|total_api_calls|api_calls)$/, "the campaign API budget"],
  [/^(campaign_time|campaign_wall_time|wall_time|wall_clock|wall_time_s)$/, "the campaign wall-clock limit"],
  [/^(job_budget|job|job_api_calls)$/, "one job's call allocation"],
  [/^job_timeout$/, "one job's timeout"],
  [/^job_context_limit$/, "one job's context limit"],
  [/^(no_runnable_obligations|scheduler)$/, "the scheduler: no runnable obligation remains under the current plan and per-node retry and decomposition limits, while the campaign budget is not exhausted"],
  [/^(restarts|max_restarts|node_restarts)$/, "the per-node restart limit"],
  [/^(decompositions|max_decompositions)$/, "the decomposition limit"],
  [/^(nodes|max_nodes)$/, "the node limit"],
  [/^(infrastructure|environment|environment_error|provider|provider_error|lean)$/, "infrastructure"],
  [/^(disproof|disproved|negation)$/, "a certified disproof"],
  [/^(user|stop|stopped|interrupt|interrupted)$/, "a stop request"],
  [/^(verification|verification_failed|final_build)$/, "final verification"],
  [/^(source|source_conflict)$/, "protected source"],
  [/^(context_limit)$/, "a context limit"],
  [/^campaign$/, "the campaign"],
];

function scopeLabel(scope: string, code = ""): string {
  const byCode = code ? SCOPE_LABELS.find(([pattern]) => pattern.test(code)) : undefined;
  if (byCode) {
    return byCode[1];
  }
  const byScope = SCOPE_LABELS.find(([pattern]) => pattern.test(scope));
  return byScope ? byScope[1] : words(scope).toLowerCase();
}

function toneFor(status: string): "info" | "warn" | "error" {
  if (["completed", "complete", "succeeded"].includes(status)) return "info";
  if (/error|failed|conflict|disproved/.test(status)) return "error";
  return "warn";
}

function campaignSpend(snapshot: ProverSnapshot, remaining: number | null = null): string {
  const used = snapshot.metrics.api_calls;
  const total = snapshot.metrics.total_api_budget ?? snapshot.config.total_api_calls;
  if (used === null) return "";
  const spend = total === null
    ? `Campaign calls used: ${used.toLocaleString()}`
    : `Campaign calls used: ${used.toLocaleString()} of ${total.toLocaleString()}`;
  return remaining !== null ? `${spend} (${remaining.toLocaleString()} unspent).` : `${spend}.`;
}

/** Used/limit for a recorded stop reason, formatted for its scope. */
export function stopUsage(reason: ProverStopReason): string {
  if (reason.used === null) {
    return "";
  }
  const time = /wall_time|wall_clock|campaign_time/.test(reason.code) || /time/.test(reason.scope);
  const format = (value: number) => (time ? `${Math.round(value).toLocaleString()} s` : value.toLocaleString());
  return reason.limit !== null ? `${format(reason.used)} / ${format(reason.limit)}` : format(reason.used);
}

/** The exact reason a terminal run stopped, or null while it is still running. */
export function stopReasonView(snapshot: ProverSnapshot): StopReasonView | null {
  const reason = snapshot.stop_reason;
  if (reason) {
    return {
      title: words(reason.code) || proverPhaseLabel(snapshot),
      message: reason.message || snapshot.error || `The run stopped: ${words(reason.code).toLowerCase()}.`,
      scope: reason.scope,
      scopeLabel: scopeLabel(reason.scope, reason.code),
      usage: stopUsage(reason),
      nextStep: reason.next_step || snapshot.next_step,
      campaign: campaignSpend(snapshot, reason.remaining_api_calls),
      unresolved: reason.unresolved_nodes,
      tone: toneFor(reason.code || snapshot.status),
      legacy: false,
    };
  }
  if (!snapshot.terminal) {
    return null;
  }
  const status = snapshot.status || snapshot.phase;
  let message: string;
  if (["completed", "complete", "succeeded"].includes(status)) {
    message = "Every root was independently verified and the final build passed.";
  } else if (status === "budget_exhausted" || status === "budget-exhausted") {
    message = snapshot.error
      ? `${snapshot.error}. This snapshot does not record whether the campaign budget or a single job allowance ended; check the job rows.`
      : "The run stopped with budget_exhausted. This snapshot does not record whether the campaign budget or a single job allowance ended; check the job rows.";
  } else {
    message = snapshot.error || `The run ended with ${status}.`;
  }
  return {
    title: proverPhaseLabel(snapshot),
    message,
    scope: "",
    scopeLabel: "",
    usage: "",
    nextStep: snapshot.next_step,
    campaign: campaignSpend(snapshot),
    unresolved: [],
    tone: toneFor(status),
    legacy: snapshot.sequence === null,
  };
}

// --------------------------------------------------------------------- changes

export interface ChangeRow {
  change: ProverChange;
  label: string;
  tone: "ok" | "warn" | "err" | "";
  pending: boolean;
  job: ProverJob | null;
}

/** File changes, pending ones first, each labelled by what the transaction did with it. */
export function changeRows(snapshot: ProverSnapshot): ChangeRow[] {
  const rows = snapshot.changes.map((change): ChangeRow => {
    const job = snapshot.jobs.find((item) => item.id === change.job_id || item.agent_id === change.job_id) ?? null;
    if (change.pending) {
      return { change, label: "Pending validation", tone: "warn", pending: true, job };
    }
    if (change.status === "rolled_back") {
      return { change, label: "Rolled back", tone: "err", pending: false, job };
    }
    if (change.status === "deleted") {
      return { change, label: "Deleted", tone: "warn", pending: false, job };
    }
    return { change, label: "Committed", tone: "ok", pending: false, job };
  });
  return [...rows.filter((row) => row.pending), ...rows.filter((row) => !row.pending)];
}

// -------------------------------------------------------------------- proposal

export interface ProposalView {
  status: string;
  label: string;
  /** What the proposed graph means next to the canonical one. */
  explanation: string;
  /** What the draft plan means next to the durable proof plan. */
  planNote: string;
  dag: ProverDag;
  plan: string;
  critique: string;
  newNodes: number;
  existingNodes: number;
}

const PROPOSAL_STATUS_LABELS: Record<string, string> = {
  proposed: "proposed, awaiting review",
  reviewed: "reviewed, awaiting validation",
  validating: "validating helper skeletons",
  rejected: "rejected",
};

/**
 * The proposed graph, explicitly separated from the authoritative canonical one.
 *
 * An accepted proposal is no longer a draft: the controller has published it as the
 * canonical graph and the proof plan and keeps the proposal fields only as a record.
 * Showing them again would repeat the plan under a "draft" heading.
 */
export function proposalView(snapshot: ProverSnapshot): ProposalView | null {
  if (snapshot.proposed_dag === null || snapshot.proposal_status === "accepted") {
    return null;
  }
  const status = snapshot.proposal_status || "proposed";
  const canonical = new Set(snapshot.dag.nodes.map((node) => node.id));
  const existingNodes = snapshot.proposed_dag.nodes.filter((node) => canonical.has(node.id)).length;
  return {
    status,
    label: `Proposed graph · ${PROPOSAL_STATUS_LABELS[status] ?? words(status).toLowerCase()}`,
    explanation: "The canonical graph remains authoritative. Proposed statements are not scheduled and never count as proved until review and skeleton validation accept them and the controller publishes the graph.",
    planNote: status === "rejected"
      ? "This draft was rejected and will not become the plan; the proof plan above stays authoritative."
      : "This draft is not the plan until review and skeleton validation accept it and the controller publishes the graph.",
    dag: snapshot.proposed_dag,
    plan: snapshot.proposed_plan,
    critique: snapshot.proposal_critique,
    newNodes: snapshot.proposed_dag.nodes.length - existingNodes,
    existingNodes,
  };
}

/**
 * The reviewer's verdict on the proposal the controller published, shown with the
 * accepted plan. Empty until a proposal is accepted, and again while a new planning
 * request rewrites the plan, when the stale verdict would describe the wrong text.
 */
export function acceptedReview(snapshot: ProverSnapshot): string {
  if (snapshot.proposal_status !== "accepted" || snapshot.planning.active) {
    return "";
  }
  return snapshot.proposal_critique;
}

// -------------------------------------------------------------------- ordering

/** Accept `next` unless it is an older snapshot of the same run. */
export function isNewerProverSnapshot(previous: ProverSnapshot | null, next: ProverSnapshot): boolean {
  if (previous === null || previous.run_id !== next.run_id) {
    return true;
  }
  if (previous.sequence !== null && next.sequence !== null) {
    // The runtime's publication counter is monotonic even when two writes
    // share a timestamp.
    return next.sequence >= previous.sequence;
  }
  const before = Date.parse(previous.updated_at);
  const after = Date.parse(next.updated_at);
  if (Number.isNaN(before) || Number.isNaN(after)) {
    return true;
  }
  return after >= before;
}

// ------------------------------------------------------------------ jobs, roles

export interface RoleSetting {
  role: "prover" | "orchestrator";
  label: string;
  model: string;
  contextTokens: number | null;
  compression: boolean | null;
  callsPerStage: number | null;
  stageLabel: string;
}

/** Per-role model and context; provider and reasoning effort are shared and stated once. */
export function roleSettings(snapshot: ProverSnapshot): RoleSetting[] {
  const config = snapshot.config;
  return [
    {
      role: "prover", label: "Prover", model: config.model, contextTokens: config.context_tokens,
      compression: config.compression, callsPerStage: config.job_api_calls,
      stageLabel: "per prover pass, retained across local decomposition",
    },
    {
      role: "orchestrator", label: "Orchestrator, review, and research", model: config.orchestrator_model || config.model,
      contextTokens: config.orchestrator_context_tokens, compression: config.orchestrator_compression,
      callsPerStage: config.orchestrator_api_calls,
      stageLabel: "per planning, review, or research stage; each stage is a fresh context with its own allowance",
    },
  ];
}

export interface JobSettings {
  provider: string;
  model: string;
  reasoning_effort: string;
  context_tokens: number | null;
  compression: boolean | null;
  /** "job" when the job recorded its own settings, otherwise the run's shared ones. */
  source: "job" | "shared";
}

/** The provider, model, and context one job ran with. */
export function jobSettings(job: ProverJob, snapshot: ProverSnapshot): JobSettings {
  const config = snapshot.config;
  const orchestrator = job.role !== "prover" && job.role !== "negation";
  const shared: JobSettings = {
    provider: snapshot.provider,
    model: orchestrator ? config.orchestrator_model || config.model : config.model,
    reasoning_effort: snapshot.reasoning_effort,
    context_tokens: orchestrator ? config.orchestrator_context_tokens : config.context_tokens,
    compression: orchestrator ? config.orchestrator_compression : config.compression,
    source: "shared",
  };
  const recorded = job.provider || job.model || job.reasoning_effort || job.context_tokens !== null || job.compression !== null;
  if (!recorded) {
    return shared;
  }
  return {
    provider: job.provider || shared.provider,
    model: job.model || shared.model,
    reasoning_effort: job.reasoning_effort || shared.reasoning_effort,
    context_tokens: job.context_tokens ?? shared.context_tokens,
    compression: job.compression ?? shared.compression,
    source: "job",
  };
}

/** How long a job has run or ran; live jobs are measured against `nowMs`. */
export function jobTimeline(
  job: Pick<ProverJob, "started_at" | "finished_at" | "status">, nowMs: number,
): { durationS: number | null; live: boolean } {
  const isLive = LIVE_JOB.has(job.status);
  const started = Date.parse(job.started_at);
  if (Number.isNaN(started)) {
    return { durationS: null, live: isLive };
  }
  const finished = Date.parse(job.finished_at);
  if (!Number.isNaN(finished)) {
    return { durationS: Math.max(0, Math.round((finished - started) / 1000)), live: isLive };
  }
  return isLive ? { durationS: Math.max(0, Math.round((nowMs - started) / 1000)), live: true } : { durationS: null, live: false };
}

/** Never present unlabelled historical estimates or incomplete coverage as a bill. */
export function proverCost(metrics: Record<string, unknown>): string {
  const source = String(metrics.cost_source || "unavailable");
  if (typeof metrics.cost_usd !== "number" || !Number.isFinite(metrics.cost_usd) || metrics.cost_usd < 0
    || !["provider_reported", "provider_estimated", "estimated", "mixed"].includes(source)) return "unavailable";
  const label = source === "provider_reported" ? "reported" : source === "mixed" ? "mixed sources" : "estimated";
  return `$${metrics.cost_usd.toFixed(3)} · ${label}${metrics.cost_complete === true ? "" : " · partial"}`;
}
