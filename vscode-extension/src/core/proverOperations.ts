/**
 * Read controller and verifier operations: checks, builds, model requests, tool calls.
 *
 * Operations are not agents. A running skeleton compile explains why provers
 * are waiting; it never counts as a prover slot. Shared by the lifecycle and
 * progress views so both describe the same check the same way.
 */
import type { ProverOperation } from "./prover";

const OPERATION_LABELS: Record<string, string> = {
  preflight: "Preflight check",
  graph_validation: "Graph validation",
  verification_queue: "Verifier queue",
  skeleton_compile: "Helper skeleton compile",
  signature_check: "Protected signature check",
  submission_check: "Submission check",
  candidate_check: "Candidate check",
  negation_check: "Negation check",
  proof_integration: "Proof integration",
  final_build: "Final project build",
  library_install: "Library installation",
  model_request: "Model request",
  tool: "Tool call",
};

/** Operation kinds that are independent checks or builds rather than model work. */
export const CHECK_KINDS = new Set([
  "preflight",
  "skeleton_compile",
  "signature_check",
  "submission_check",
  "candidate_check",
  "negation_check",
  "proof_integration",
  "final_build",
  "library_install",
]);

/** Count the concrete checks, not their enclosing multi-step transaction. */
export function isCheckOperation(operation: ProverOperation): boolean {
  const legacyTransaction = operation.kind === "skeleton_compile"
    && !operation.file && !operation.node_id
    && operation.label === "Materializing and validating reviewed graph";
  return CHECK_KINDS.has(operation.kind) && !legacyTransaction;
}

/** A readable name for one operation; a tool call is named by its tool. */
export function operationLabel(operation: Pick<ProverOperation, "kind" | "label">): string {
  if (operation.kind === "tool" && operation.label) {
    return operation.label;
  }
  const known = OPERATION_LABELS[operation.kind];
  if (known) {
    return known;
  }
  if (operation.label) {
    return operation.label;
  }
  const words = operation.kind.replace(/[-_]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Operation";
}

/** "4/8" when the runtime reports check progress, otherwise "". */
export function operationProgress(operation: Pick<ProverOperation, "completed" | "total">): string {
  if (operation.total === null || operation.total === 0) {
    return "";
  }
  return `${operation.completed ?? 0}/${operation.total}`;
}

export function isRunningOperation(operation: Pick<ProverOperation, "status">): boolean {
  return operation.status === "running" || operation.status === "";
}

/** Seconds an operation has taken; a running one is measured against `nowMs`. */
export function operationDurationS(
  operation: Pick<ProverOperation, "status" | "started_at" | "updated_at" | "finished_at"> & { elapsed_s?: number | null },
  nowMs: number,
): number | null {
  if (!isRunningOperation(operation) && typeof operation.elapsed_s === "number") {
    // The runtime's own measurement is exact; timestamps only approximate it.
    return Math.round(operation.elapsed_s);
  }
  const started = Date.parse(operation.started_at);
  if (Number.isNaN(started)) {
    return null;
  }
  const finished = Date.parse(operation.finished_at);
  const end = !Number.isNaN(finished)
    ? finished
    : isRunningOperation(operation)
      ? nowMs
      : Date.parse(operation.updated_at);
  if (Number.isNaN(end)) {
    return null;
  }
  return Math.max(0, Math.round((end - started) / 1000));
}
