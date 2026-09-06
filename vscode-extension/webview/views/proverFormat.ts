/** Small formatting helpers shared by the prover workspace cards. */
import { duration } from "../components";

export function metric(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toLocaleString();
}

export function ratio(used: number | null | undefined, max: number | null | undefined): string {
  return `${metric(used)} / ${metric(max)}`;
}

export function seconds(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : duration(value);
}

/** Local 24-hour time for an ISO stamp, or "" when it does not parse. */
export function clock(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleTimeString(undefined, { hour12: false });
}

/** Pill tone for a recorded status; the pill classes are ok, bad, warn. */
export function pillTone(status: string): "ok" | "bad" | "warn" | "" {
  if (["proved", "verified", "completed", "succeeded", "accepted", "committed"].includes(status)) return "ok";
  if (["failed", "invalidated", "disproved", "false", "rejected", "provider_error", "environment_error", "source_conflict", "verification_failed", "error", "rolled_back", "stale", "interrupted", "timeout"].includes(status)) return "bad";
  if (["running", "proving", "candidate", "conditional", "provisional", "retry", "blocked", "budget_exhausted", "resume_pending", "submitted", "verifying", "integrating", "staged", "pending", "queued", "starting", "deleted"].includes(status)) return "warn";
  return "";
}

/** Dot tone for the compact tree rows. */
export function dotTone(status: string): "ok" | "err" | "warn" | "" {
  const tone = pillTone(status);
  return tone === "bad" ? "err" : tone;
}

export function lifecycleTone(state: string): "ok" | "bad" | "warn" | "running" | "" {
  if (state === "solved") return "ok";
  if (state === "active") return "running";
  if (state === "failed") return "bad";
  if (state === "candidate" || state === "blocked") return "warn";
  return "";
}

/** The last two path segments, enough to tell artifacts in one job apart. */
export function artifactName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/") || path;
}

/** Artifacts the editor can show as text; binaries such as PDFs are listed by name only. */
export function isTextArtifact(path: string): boolean {
  return /\.(md|json|jsonl|lean|txt|log|toml|ya?ml|py|tex|csv)$/i.test(path);
}
