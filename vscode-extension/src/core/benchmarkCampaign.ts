/** Read-only presentation and path confinement for the Lean-IMO campaign. */
import * as path from "node:path";
export { proverCost as benchmarkCost } from "./proverProgress";

export interface BenchmarkCell {
  id: string;
  problem: { id: string; file: string };
  condition: string;
  model: string;
  effort: string;
  order: string;
  status: string;
  lane: number | null;
  project?: string;
  run_id?: string;
  phase?: string;
  verified: boolean;
  metrics: Record<string, unknown>;
  error?: string;
  runtime_sha256?: string;
  stop_reason?: { code?: string; scope?: string; message?: string };
}



export interface BenchmarkCampaign {
  version: number;
  name: string;
  status: string;
  updated_at: string;
  pause_reason?: string;
  provenance: { commit: string; runtime_sha256: string; toolchain: string };
  cells: BenchmarkCell[];
}

/** Reject dashboard navigation outside the selected campaign's private cell tree. */
export function benchmarkProject(directory: string, cell: BenchmarkCell): string {
  const expected = path.resolve(directory, "cells", cell.id);
  if (!/^[A-Za-z0-9_-]+$/.test(cell.id) || !cell.project || path.resolve(cell.project) !== expected) {
    throw new Error("Invalid benchmark cell project path.");
  }
  return expected;
}

/** Escape model-originated status and error text before embedding it in a webview. */
export function benchmarkEscape(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

/** Keep unavailable prices distinct from zero and aggregate only verified completions. */
export function benchmarkSummary(campaign: BenchmarkCampaign): { verified: number; active: number; calls: number; tokens: number } {
  return {
    verified: campaign.cells.filter((c) => c.verified).length,
    active: campaign.cells.filter((c) => ["running", "preparing"].includes(c.status)).length,
    calls: campaign.cells.reduce((n, c) => n + Number(c.metrics.api_calls || 0), 0),
    tokens: campaign.cells.reduce((n, c) => n + Number(c.metrics.input_tokens || 0) + Number(c.metrics.output_tokens || 0), 0),
  };
}
