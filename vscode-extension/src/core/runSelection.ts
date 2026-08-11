/** Select the tracked run whose exact live status may be displayed. */
import type { LiveStatus, TrackedRun } from "./types";

/** Return whether a status snapshot still names a verified live process. */
export function isActiveLiveStatus(status: LiveStatus | null): boolean {
  return (
    status !== null &&
    status.stale_snapshot !== true &&
    Number(status.process_id ?? 0) > 0
  );
}

/** Return the explicit tracked run, otherwise the currently active tracked run. */
export function trackedRunForLiveStatus(
  runs: readonly TrackedRun[],
  selectedRunId: string | null,
): TrackedRun | null {
  if (selectedRunId !== null) {
    return runs.find((run) => run.id === selectedRunId) ?? null;
  }
  return (
    runs.find((run) => run.status === "starting" || run.status === "running") ?? null
  );
}
