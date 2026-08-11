/** Audit whether source-checkout workflows overlap isolated experiment cells. */
import type { RunSummary } from "./types";

export interface SourceRunOverlapAudit {
  overlappingRunIds: string[];
  unreadableRunIds: string[];
  cellIntervalReadable: boolean;
}

function timestamp(value: string | null): number | null {
  if (!value) {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Find source-root runs whose recorded lifetime intersects one cell.
 *
 * Active runs have an open-ended interval. Corrupt or incomplete history is
 * reported separately so callers can fail closed instead of treating an
 * unknown interval as proof of exclusive execution.
 */
export function auditSourceRunOverlap(
  cellStartedAt: string | null,
  cellFinishedAt: string | null,
  runs: readonly RunSummary[],
): SourceRunOverlapAudit {
  const cellStart = timestamp(cellStartedAt);
  const cellEnd = timestamp(cellFinishedAt);
  if (cellStart === null || cellEnd === null || cellEnd < cellStart) {
    return {
      overlappingRunIds: [],
      unreadableRunIds: [],
      cellIntervalReadable: false,
    };
  }

  const overlappingRunIds: string[] = [];
  const unreadableRunIds: string[] = [];
  for (const run of runs) {
    const runStart = timestamp(run.started_at);
    const preferredEnd = run.finalized_at || run.updated_at;
    // A nonterminal stream has no verified process-death upper bound. Its last
    // activity timestamp is only a lower bound: the process may have remained
    // alive silently before a later crash or compaction.
    const runEnd = run.terminal ? timestamp(preferredEnd) : Number.POSITIVE_INFINITY;

    if (
      runStart !== null &&
      runEnd !== null &&
      Number.isFinite(runEnd) &&
      runEnd < runStart
    ) {
      unreadableRunIds.push(run.run_id || "[unknown-run]");
      continue;
    }
    // A known endpoint can prove non-overlap even when the other endpoint is
    // absent in legacy history. Otherwise the interval is not trustworthy.
    if (run.terminal && runEnd !== null && runEnd < cellStart) {
      continue;
    }
    if (runStart !== null && runStart > cellEnd) {
      continue;
    }
    if (
      runStart === null ||
      runEnd === null
    ) {
      unreadableRunIds.push(run.run_id || "[unknown-run]");
      continue;
    }
    if (runStart <= cellEnd && runEnd >= cellStart) {
      overlappingRunIds.push(run.run_id || "[unknown-run]");
    }
  }
  return { overlappingRunIds, unreadableRunIds, cellIntervalReadable: true };
}
