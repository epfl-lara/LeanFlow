/** Decide whether durable workflow state still advertises a live owner. */
import * as path from "node:path";

import type { LiveStatus, RunSummary, TrackedRun, TrackedRunStatus } from "./types";

const TERMINAL_PHASES = new Set([
  "idle",
  "done",
  "complete",
  "completed",
  "exited",
  "stopped",
  "interrupted",
  "failed",
  "dead",
]);

export type RestoredRunResolution =
  | { kind: "waiting" }
  | { kind: "adopt"; processId: number }
  | { kind: "terminal"; status: TrackedRunStatus }
  | { kind: "abandoned"; error: string };

/** Map the CLI's exact terminal vocabulary into the compact tree-view states. */
export function terminalTrackedRunStatus(
  statusValue: unknown,
  phaseValue: unknown,
  exitCodeValue: unknown,
): TrackedRunStatus {
  const terminal = String(statusValue ?? "").trim().toLowerCase();
  if (terminal === "interrupted") {
    return "stopped";
  }
  if (terminal === "failed") {
    return "failed";
  }
  if (["succeeded", "paused", "disproved", "exited"].includes(terminal)) {
    return "finished";
  }
  const phase = String(phaseValue ?? "").trim().toLowerCase();
  if (phase === "stopped" || phase === "interrupted") {
    return "stopped";
  }
  const exitCode =
    typeof exitCodeValue === "number" && Number.isInteger(exitCodeValue)
      ? exitCodeValue
      : null;
  if (exitCode === 0) {
    return "finished";
  }
  if (exitCode === 130) {
    return "stopped";
  }
  if (exitCode !== null || phase === "failed" || phase === "dead") {
    return "failed";
  }
  return "finished";
}

/** Compare project roots without depending on their display spelling. */
export function sameProjectRoot(left: string, right: string): boolean {
  if (!left || !right) {
    return left === right;
  }
  const normalizedLeft = path.resolve(left);
  const normalizedRight = path.resolve(right);
  return process.platform === "win32"
    ? normalizedLeft.toLowerCase() === normalizedRight.toLowerCase()
    : normalizedLeft === normalizedRight;
}

/** Return a user-facing description when recorded state has a live owner. */
export function durableOwnerDescription(status: LiveStatus | null): string | null {
  if (status === null || status.stale_snapshot === true) {
    return null;
  }
  const processId = Number(status.process_id ?? 0);
  if (!Number.isSafeInteger(processId) || processId <= 0) {
    return null;
  }
  const phase = String(status.phase ?? "running").trim() || "running";
  const runId = String(status.run_id ?? "").trim();
  return runId
    ? `Workflow ${runId} is still ${phase} (process ${processId}).`
    : `A workflow is still ${phase} (process ${processId}).`;
}

/** Return a user-facing description when an extension-tracked run is active. */
export function trackedOwnerDescription(
  runs: Iterable<TrackedRun>,
  projectRoot: string,
): string | null {
  for (const run of runs) {
    if (
      sameProjectRoot(run.projectRoot, projectRoot) &&
      (run.status === "starting" || run.status === "running")
    ) {
      return `${run.label} is still ${run.status}.`;
    }
  }
  return null;
}

/** Block manual/sweep overlap even when experiment cells use private roots. */
export function researchConflictDescription(
  runs: Iterable<TrackedRun>,
  requestedExperimentId?: string,
): string | null {
  for (const run of runs) {
    if (run.status !== "starting" && run.status !== "running") {
      continue;
    }
    if (requestedExperimentId === undefined && run.experimentId !== null) {
      return (
        `${run.label} is still running for a research sweep. ` +
        "Wait for it to finish or stop it before starting another workflow."
      );
    }
    if (
      requestedExperimentId !== undefined &&
      run.experimentId !== null &&
      run.experimentId !== requestedExperimentId
    ) {
      return (
        `${run.label} is still running for another research sweep. ` +
        "Wait for it to finish or stop it first."
      );
    }
    if (requestedExperimentId !== undefined && run.experimentId === null) {
      return (
        `${run.label} is still running. Stop manual workflows before starting ` +
        "a research sweep so resource contention cannot bias its measurements."
      );
    }
  }
  return null;
}

/**
 * Resolve a handle-less run only from evidence carrying its exact run id.
 *
 * An unrelated live-status owner and raw pid existence are intentionally
 * ignored. Immutable final results and runner-exit are terminal authority;
 * only exact live status can safely restore the recorded pid. Exact history
 * keeps the launch barrier without becoming a signal authority. When both
 * readers succeeded and no trace appeared during the launch grace period, a
 * pid-less pre-spawn record is abandoned rather than blocking forever.
 */
export function resolveRestoredRun(
  run: TrackedRun,
  status: LiveStatus | null,
  history: readonly RunSummary[],
  stateReadable: boolean,
  nowMs = Date.now(),
  launchGraceMs = 15_000,
): RestoredRunResolution {
  const exactSummary = history.find((summary) => summary.run_id === run.runId);
  if (
    exactSummary?.terminal ||
    exactSummary?.final_snapshot_recorded ||
    exactSummary?.last_event_type === "runner-exit"
  ) {
    return {
      kind: "terminal",
      status: terminalTrackedRunStatus(
        exactSummary.terminal_status,
        exactSummary.terminal_phase,
        exactSummary.exit_code,
      ),
    };
  }

  const exactStatus =
    status !== null && String(status.run_id ?? "") === run.runId ? status : null;
  if (exactStatus !== null) {
    const phase = String(exactStatus.phase ?? "").trim().toLowerCase();
    if (exactStatus.stale_snapshot === true || TERMINAL_PHASES.has(phase)) {
      return {
        kind: "terminal",
        status: terminalTrackedRunStatus(
          exactStatus.terminal_status,
          phase,
          exactStatus.exit_code,
        ),
      };
    }
    const processId = Number(exactStatus.process_id ?? 0);
    if (Number.isSafeInteger(processId) && processId > 0) {
      return { kind: "adopt", processId };
    }
    return { kind: "waiting" };
  }

  if (exactSummary !== undefined) {
    // Stream metadata proves this launch got far enough to record events, but
    // its historical pid is not a live-owner lease. Keep the barrier in place
    // while waiting for exact live or terminal evidence; never adopt/kill from
    // a raw pid copied out of history.
    return { kind: "waiting" };
  }

  if (!stateReadable || run.pid !== null) {
    return { kind: "waiting" };
  }
  const started = Date.parse(run.startedAt);
  const age = Number.isFinite(started) ? nowMs - started : Number.POSITIVE_INFINITY;
  if (age < launchGraceMs) {
    return { kind: "waiting" };
  }
  return {
    kind: "abandoned",
    error:
      "The editor reloaded during launch, but no exact process, event stream, or final " +
      "result was recorded. The workflow was not started.",
  };
}
