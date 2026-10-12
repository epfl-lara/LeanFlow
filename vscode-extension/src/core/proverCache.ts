/**
 * Decide when the prover snapshot must be re-read through the CLI.
 *
 * The snapshot comes from `leanflow runs prover`, which starts a Python
 * process. Polling it every few seconds would spend most of the machine on
 * process startup, so the host keeps the last answer and re-reads only when the
 * run's state file has changed. The file is never parsed here; its size and
 * modification time are a change signal, and the CLI stays the only reader.
 */
import * as path from "node:path";

import type { ProverSnapshot } from "./prover";

export interface FileSignature {
  mtimeMs: number;
  size: number;
}

export interface ProverCacheEntry {
  snapshot: ProverSnapshot | null;
  error: string;
  /** Signature of the state file when the snapshot was read, or null if unreadable. */
  signature: FileSignature | null;
  fetchedAt: number;
}

const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$/;

export function sameSignature(left: FileSignature | null, right: FileSignature | null): boolean {
  if (left === null || right === null) {
    return left === right;
  }
  return left.mtimeMs === right.mtimeMs && left.size === right.size;
}

/**
 * Whether a cached snapshot may be served instead of spawning the CLI.
 *
 * An unchanged file within the maximum age is served from cache. When the file
 * cannot be observed, the cache falls back to a short age so a run whose state
 * lives elsewhere still refreshes. A previous failure is always retried. A run
 * with no prover state at all (a legacy prove workflow) is re-asked only when
 * its state file appears or after a longer age, so an idle legacy run does not
 * cost a process every few seconds.
 */
export function shouldRefetchProver(
  entry: ProverCacheEntry | undefined,
  signature: FileSignature | null,
  nowMs: number,
  options: { maxAgeMs?: number; unknownSignatureAgeMs?: number; absentAgeMs?: number; errorRetryMs?: number } = {},
): boolean {
  const maxAgeMs = options.maxAgeMs ?? 60_000;
  const unknownSignatureAgeMs = options.unknownSignatureAgeMs ?? 5_000;
  const absentAgeMs = options.absentAgeMs ?? 30_000;
  const errorRetryMs = options.errorRetryMs ?? 5_000;
  if (entry === undefined) {
    return true;
  }
  const age = nowMs - entry.fetchedAt;
  if (entry.error !== "") {
    // Retry a failed read, but not on every tick: a broken CLI would otherwise
    // be spawned as often as the webview asks.
    return age >= errorRetryMs || !sameSignature(entry.signature, signature);
  }
  if (entry.snapshot === null) {
    return signature !== null || age >= absentAgeMs;
  }
  if (age >= maxAgeMs) {
    return true;
  }
  if (signature === null) {
    return entry.signature !== null || age >= unknownSignatureAgeMs;
  }
  return !sameSignature(entry.signature, signature);
}

/** The state file the CLI documents for one exact run, or null for an unsafe id. */
export function proverStatePath(projectRoot: string, runId: string): string | null {
  if (!projectRoot || !RUN_ID.test(runId) || runId === "." || runId === "..") {
    return null;
  }
  return path.join(projectRoot, ".leanflow", "workflow-state", "prover", runId, "state.json");
}
