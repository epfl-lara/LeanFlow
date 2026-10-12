/** Select a bounded, exact-job narrative from the shared run event buffer. */
import type { ActivityEvent } from "./types";

const OUTPUT_TYPES = new Set([
  "agent-output", "agent-reasoning", "agent-tool-start", "agent-tool-end", "agent-plan", "agent-status",
  "api-response", "assistant-response", "tool-result", "job-session-end", "submission-feedback",
]);

/** Never mix runs or jobs, even when a restarted campaign reused an agent name. */
export function agentActivity(events: readonly ActivityEvent[], runId: string, jobId: string, limit = 30): {
  rows: ActivityEvent[]; total: number; hasReasoning: boolean; latest: string;
} {
  const matching = events.filter(event => event.run_id === runId
    && (event.details?.job_id ? event.details.job_id === jobId : event.agent_id === jobId)
    && OUTPUT_TYPES.has(event.type));
  const last = matching[matching.length - 1];
  return {
    rows: matching.slice(-Math.max(1, limit)), total: matching.length,
    hasReasoning: matching.some(event => event.type === "agent-reasoning" || Boolean(event.details?.reasoning_preview)),
    latest: last?.message || "",
  };
}
