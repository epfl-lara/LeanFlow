/**
 * Notice a run started outside this window while the dashboard is idle.
 *
 * The run manager stops polling when nothing it knows about is active. A run
 * resumed from a terminal then went unnoticed until Refresh. Rather than
 * polling the CLI forever, an idle dashboard watches the project's live-status
 * file and only reads state when that file changes. The file is a change
 * signal; its contents still come through `leanflow runs status`.
 */
import { sameSignature, type FileSignature } from "./proverCache";

export interface IdleDiscoveryState {
  /** `undefined` before the first observation; null when the file is absent. */
  lastSignature: FileSignature | null | undefined;
  lastCheckedAt: number;
}

export function idleCheckDue(state: IdleDiscoveryState, nowMs: number, intervalMs: number): boolean {
  return state.lastSignature === undefined || nowMs - state.lastCheckedAt >= intervalMs;
}

/** Record the observation and say whether the live-status file changed since the last one. */
export function idleDiscoveryDecision(
  state: IdleDiscoveryState,
  signature: FileSignature | null,
  nowMs: number,
): { poll: boolean; next: IdleDiscoveryState } {
  const next: IdleDiscoveryState = { lastSignature: signature, lastCheckedAt: nowMs };
  if (state.lastSignature === undefined) {
    return { poll: false, next };
  }
  return { poll: !sameSignature(state.lastSignature, signature), next };
}
