/**
 * Serve the prover snapshot for an exact run without spawning the CLI on every tick.
 *
 * The webview refreshes every few seconds. Each refresh used to run
 * `leanflow runs prover`, which starts a Python process; opening a file did the
 * same again for its artifact check. The host now keeps the last answer per run
 * and re-reads only when the run's state file has changed. The file is only
 * stat'ed: its contents still arrive through the CLI, which remains the sole
 * reader of the state directory.
 */
import * as fs from "node:fs";

import { fetchProver } from "./cli";
import type { ProverSnapshot } from "./prover";
import {
  proverStatePath,
  shouldRefetchProver,
  type FileSignature,
  type ProverCacheEntry,
} from "./proverCache";
import { redactSensitiveText } from "./runPrivacy";
import type { RunManager } from "./runManager";

export interface ProverLoadResult {
  snapshot: ProverSnapshot | null;
  /** Non-empty when the latest read failed; `snapshot` is then the last good one. */
  error: string;
  cached: boolean;
}

const MAX_CACHED_RUNS = 12;

export class ProverService {
  private readonly entries = new Map<string, ProverCacheEntry>();
  private readonly inflight = new Map<string, Promise<ProverCacheEntry>>();

  constructor(private readonly runs: RunManager) {}

  /** The last snapshot read for a run, if any, without touching the CLI. */
  cached(runId: string): ProverSnapshot | null {
    return this.entries.get(runId)?.snapshot ?? null;
  }

  /** Drop cached answers so the next load re-reads through the CLI. */
  invalidate(runId?: string): void {
    if (runId === undefined) {
      this.entries.clear();
    } else {
      this.entries.delete(runId);
    }
  }

  async load(runId: string, options: { force?: boolean } = {}): Promise<ProverLoadResult> {
    const root = this.runs.projectRootForRun(runId);
    if (!root) {
      return { snapshot: null, error: "This run is not in the project's recorded history.", cached: false };
    }
    const signature = await this.signature(runId, root);
    const entry = this.entries.get(runId);
    if (!options.force && entry !== undefined && !shouldRefetchProver(entry, signature, Date.now())) {
      // Move to the end so the least recently used run is evicted first.
      this.entries.delete(runId);
      this.entries.set(runId, entry);
      return { snapshot: entry.snapshot, error: entry.error, cached: true };
    }
    const pending = this.inflight.get(runId);
    if (pending !== undefined) {
      const settled = await pending;
      return { snapshot: settled.snapshot, error: settled.error, cached: false };
    }
    const task = this.read(root, runId, signature);
    this.inflight.set(runId, task);
    try {
      const fresh = await task;
      const previous = this.entries.get(runId);
      this.entries.delete(runId);
      if (fresh.snapshot === null && fresh.error !== "" && previous?.snapshot) {
        // A transient CLI failure must not blank a dashboard that was showing
        // real state a moment ago; keep the last good snapshot and say it is stale.
        const stale: ProverCacheEntry = { ...previous, error: fresh.error, fetchedAt: fresh.fetchedAt };
        this.entries.set(runId, stale);
        this.trim();
        return { snapshot: stale.snapshot, error: stale.error, cached: true };
      }
      this.entries.set(runId, fresh);
      this.trim();
      return { snapshot: fresh.snapshot, error: fresh.error, cached: false };
    } finally {
      this.inflight.delete(runId);
    }
  }

  private async read(root: string, runId: string, signature: FileSignature | null): Promise<ProverCacheEntry> {
    try {
      const snapshot = await fetchProver(root, runId);
      return { snapshot, error: "", signature, fetchedAt: Date.now() };
    } catch (error) {
      return {
        snapshot: null,
        error: redactSensitiveText(error instanceof Error ? error.message : String(error)),
        signature,
        fetchedAt: Date.now(),
      };
    }
  }

  /** A change signal only. The runtime advertises its own state path in live status. */
  private async signature(runId: string, root: string): Promise<FileSignature | null> {
    const candidate = this.runs.proverStatePathHint(runId) ?? proverStatePath(root, runId);
    if (candidate === null) {
      return null;
    }
    try {
      const stat = await fs.promises.stat(candidate);
      return stat.isFile() ? { mtimeMs: stat.mtimeMs, size: stat.size } : null;
    } catch {
      return null;
    }
  }

  private trim(): void {
    for (const key of this.entries.keys()) {
      if (this.entries.size <= MAX_CACHED_RUNS) {
        return;
      }
      this.entries.delete(key);
    }
  }
}
