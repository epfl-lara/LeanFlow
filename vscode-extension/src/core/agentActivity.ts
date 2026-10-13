/** Select a bounded, exact-job narrative from the shared run event buffer. */
import type { ActivityEvent } from "./types";
import { activityEventKey } from "./logRows";

const OUTPUT_TYPES = new Set([
  "agent-output", "agent-reasoning", "agent-tool-start", "agent-tool-end", "agent-tool-progress", "agent-plan", "agent-status",
  "api-response", "assistant-response", "tool-result", "job-session-end", "submission-feedback",
]);

/** Replace cumulative partial updates in the visible timeline, keeping raw evidence untouched. */
export function coalesceActivity(events: readonly ActivityEvent[]): ActivityEvent[] {
  const rows: (ActivityEvent | null)[] = [];
  const partials = new Map<string, number>();
  for (const event of events) {
    const item = event.details?.item_id;
    const key = typeof item === "string" && item
      ? activityEventKey(event)
      : "";
    if (key && partials.has(key)) {
      rows[partials.get(key)!] = null;
      partials.delete(key);
    }
    if (key && event.details?.partial === true) partials.set(key, rows.length);
    rows.push(event);
  }
  return rows.filter((event): event is ActivityEvent => event !== null);
}

/** Track only tools with a provider item id; unrelated tool completions cannot clear them. */
function runningTools(events: readonly ActivityEvent[]): ActivityEvent[] {
  const active = new Map<string, ActivityEvent>();
  for (const event of events) {
    const item = event.details?.item_id;
    if (event.type === "job-session-end") active.clear();
    if (typeof item !== "string" || !item) continue;
    const key = JSON.stringify([event.details?.invocation_id || "", item]);
    if (event.type === "agent-tool-start") active.set(key, event);
    if (event.type === "agent-tool-end") active.delete(key);
  }
  return [...active.values()];
}

/** Never mix runs or jobs, even when a restarted campaign reused an agent name. */
export function agentActivity(events: readonly ActivityEvent[], runId: string, jobId: string, limit = 30): {
  rows: ActivityEvent[]; total: number; hasReasoning: boolean; latest: string;
  latestEvent: ActivityEvent | null; runningTools: ActivityEvent[];
} {
  const matching = events.filter(event => event.run_id === runId
    && (event.details?.job_id ? event.details.job_id === jobId : event.agent_id === jobId)
    && OUTPUT_TYPES.has(event.type));
  const visible = coalesceActivity(matching);
  const last = visible[visible.length - 1];
  return {
    rows: visible.slice(-Math.max(1, limit)), total: visible.length,
    hasReasoning: matching.some(event => event.type === "agent-reasoning" || Boolean(event.details?.reasoning_preview)),
    latest: last?.message || "",
    latestEvent: last ?? null,
    runningTools: runningTools(matching),
  };
}
