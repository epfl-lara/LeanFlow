/** Merge editor launches, verified live owners and saved history into one job menu. */
import { terminalTrackedRunStatus } from "./runStatus";
import { isActiveLiveStatus } from "./runSelection";
import type { AppState, LiveStatus, RunSummary, TrackedRun } from "./types";

export interface RunChoice {
  id: string;
  runId: string;
  label: string;
  status: string;
  active: boolean;
  startedAt: string;
  updatedAt: string;
  workflowKind: string;
  tracked?: TrackedRun;
}

/** Deduplicate exact run ids and never infer liveness from historical process ids. */
export function runChoices(runs: readonly TrackedRun[], history: readonly RunSummary[], live: LiveStatus | null): RunChoice[] {
  const choices = new Map<string, RunChoice>();
  for (const row of history) {
    // Worker streams belong inside their parent job's activity, not in the job menu.
    if (row.parent_run_id) continue;
    const terminal = row.terminal || row.final_snapshot_recorded || row.last_event_type === "runner-exit";
    choices.set(row.run_id, {
      id: row.run_id, runId: row.run_id, label: row.workflow_command || row.label || row.run_id,
      status: terminal ? terminalTrackedRunStatus(row.terminal_status, row.terminal_phase, row.exit_code) : "status unknown",
      active: false, startedAt: row.started_at, updatedAt: row.updated_at, workflowKind: row.workflow_kind,
    });
  }
  for (const run of runs) {
    const key = run.runId || run.id;
    const active = run.status === "running" || run.status === "starting";
    const known = choices.get(key);
    // Resumes retain the controller id but have distinct editor attempts. Input
    // order must never let an older stopped attempt hide the live/newest one.
    if (known?.tracked && (Number(known.active) > Number(active) || known.active === active && known.startedAt >= run.startedAt)) continue;
    choices.set(key, {
      id: run.id, runId: run.runId, label: run.label, status: run.status, active,
      startedAt: run.startedAt, updatedAt: run.finishedAt || run.startedAt, workflowKind: run.request.kind, tracked: run,
    });
  }
  const liveId = String(live?.run_id || "");
  if (liveId && isActiveLiveStatus(live)) {
    const known = choices.get(liveId);
    choices.set(liveId, known ? { ...known, active: true, status: "running" } : {
      id: liveId, runId: liveId, label: String(live?.workflow_command || live?.workflow_kind || "Workflow"),
      status: "running", active: true, startedAt: String(live?.started_at || live?.updated_at || ""),
      updatedAt: String(live?.updated_at || ""), workflowKind: String(live?.workflow_kind || ""),
    });
  }
  return [...choices.values()].sort((a, b) => Number(b.active) - Number(a.active) || b.updatedAt.localeCompare(a.updatedAt));
}

/** Honor an explicit historical selection; otherwise prefer the live job, then newest history. */
export function selectedRunChoice(choices: readonly RunChoice[], selectedId: string | null): RunChoice | null {
  if (selectedId !== null) return choices.find(row => row.id === selectedId || row.runId === selectedId) ?? null;
  return choices.find(row => row.active) ?? choices[0] ?? null;
}

/** Preserve the project's live pointer even while the dashboard displays an older job. */
export function appRunChoices(app: AppState): RunChoice[] {
  return runChoices(app.runs, app.history, app.projectLiveStatus === undefined ? app.liveStatus : app.projectLiveStatus);
}
