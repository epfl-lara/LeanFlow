/** Normalize the CLI run-list envelope without discarding archive completeness. */
import type { RunHistorySnapshot, RunSummary } from "./types";

interface RawRunHistoryPayload {
  version?: unknown;
  complete?: unknown;
  completeness_issues?: unknown;
  archive_audit?: unknown;
  limit?: unknown;
  total_count?: unknown;
  truncated?: unknown;
  count?: unknown;
  runs?: unknown;
}

/** Return a fail-closed retained-history snapshot from untrusted CLI JSON. */
export function normalizeRunHistoryPayload(
  payload: RawRunHistoryPayload,
): RunHistorySnapshot {
  const runs = Array.isArray(payload.runs)
    ? payload.runs.filter(
        (run): run is RunSummary => run !== null && typeof run === "object",
      )
    : [];
  const archiveAudit =
    payload.archive_audit !== null && typeof payload.archive_audit === "object"
      ? (payload.archive_audit as RunHistorySnapshot["archiveAudit"])
      : {};
  const completenessIssues = Array.isArray(payload.completeness_issues)
    ? payload.completeness_issues.filter(
        (issue): issue is string => typeof issue === "string",
      )
    : [];
  const envelopeComplete = Boolean(
    payload.version === 2 &&
      payload.complete === true &&
      archiveAudit.complete === true &&
      Number.isSafeInteger(payload.limit) &&
      Number(payload.limit) > 0 &&
      Number.isSafeInteger(payload.total_count) &&
      payload.total_count === runs.length &&
      payload.truncated === false &&
      Number.isSafeInteger(payload.count) &&
      payload.count === runs.length &&
      Array.isArray(payload.runs) &&
      payload.runs.length === runs.length,
  );
  if (!envelopeComplete && completenessIssues.length === 0) {
    completenessIssues.push("runs-list-envelope-incomplete");
  }
  return {
    version: typeof payload.version === "number" ? payload.version : 0,
    complete: envelopeComplete,
    completenessIssues,
    limit: Number.isSafeInteger(payload.limit) ? Number(payload.limit) : 0,
    totalCount: Number.isSafeInteger(payload.total_count)
      ? Number(payload.total_count)
      : 0,
    truncated: payload.truncated !== false,
    archiveAudit,
    runs,
  };
}
