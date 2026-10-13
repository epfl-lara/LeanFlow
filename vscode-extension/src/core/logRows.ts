/**
 * Give the activity stream the shape a reader already expects from it.
 *
 * A run is not a flat list of rows: it is a sequence of agent sessions, each a
 * sequence of turns. Grouping contiguous rows by their agent and framing each
 * group with what that job was and how it ended is the same move a CI log
 * makes with collapsible steps and a trace viewer makes with spans.
 *
 * Pure and host-free so the webview and the tests share it.
 */
import type { ActivityEvent } from "./types";

export interface SessionInfo {
  agentId: string;
  role: string;
  nodeId: string;
  /** Terminal status, or "" while the session is still open. */
  status: string;
  startedAt: string;
  endedAt: string;
  calls: number | null;
  budget: number | null;
}

export interface LogSegment {
  /** Stable across streamed updates: the agent plus the first visible item's identity. */
  key: string;
  agentId: string;
  session: SessionInfo | null;
  events: ActivityEvent[];
}

const SESSION_START = new Set(["job-session-start", "runner-start", "conversation-start"]);
const SESSION_END = new Set([
  "job-session-end",
  "job_finished",
  "runner-exit",
  "conversation-end",
]);

/** Wire event types rendered as something a reader can scan. */
const EVENT_LABELS: Record<string, string> = {
  "agent-output": "Agent output",
  "agent-reasoning": "Reported reasoning",
  "agent-tool-start": "Native tool started",
  "agent-tool-end": "Native tool finished",
  "agent-tool-progress": "Native tool output",
  "agent-plan": "Agent plan",
  "agent-status": "Agent status",
  "api-request": "Request",
  "api-response": "Response",
  "api-error": "Provider error",
  "api-call": "Request",
  "assistant-response": "Response",
  "tool-call": "Tool call",
  "tool-result": "Tool result",
  "job-session-start": "Session start",
  "job-session-end": "Session end",
  job_finished: "Job finished",
  job_cleanup_error: "Cleanup error",
  "conversation-start": "Conversation start",
  "conversation-end": "Conversation end",
  "runner-start": "Run started",
  "runner-exit": "Run finished",
  "submission-feedback": "Candidate check",
  "operation-start": "Operation started",
  "operation-end": "Operation finished",
  submission_checked: "Submission check",
  candidate_checked: "Candidate check",
  negation_checked: "Negation check",
  plan_rejected: "Plan rejected",
  plan_refinement_budget_exhausted: "Refinement budget spent",
  libraries_installed: "Libraries installed",
  "context-compacted": "Context compacted",
  "user-guidance-received": "Guidance received",
  user_message: "User message",
  "final-report-rejected": "Final report rejected",
  research_cleanup_error: "Research cleanup error",
};

/**
 * Return a readable label for one event type.
 *
 * The runtime mixes `job-session-start` and `job_finished` spellings; a reader
 * should never have to notice that, so unknown types are normalized the same
 * way rather than shown raw.
 */
export function humanEventLabel(type: string): string {
  const known = EVENT_LABELS[type];
  if (known) {
    return known;
  }
  const words = type.replace(/[-_]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : type;
}

/** Label only the reasoning the native provider explicitly chose to expose. */
export function reasoningLabel(kind: unknown): string {
  return kind === "summary" ? "Reasoning summary" : kind === "trace" ? "Reported reasoning trace" : "Reported reasoning";
}

/** Use an emitted tool name without requiring the reader to know its wire spelling. */
export function readableToolName(name: unknown): string {
  if (typeof name !== "string" || !name.trim()) return "Tool";
  const leaf = name.split("__").at(-1) || name;
  const words = leaf.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Give native activity a specific heading instead of repeating a generic event type. */
export function activityEventLabel(event: ActivityEvent): string {
  if (event.type === "agent-reasoning") return reasoningLabel(event.details?.reasoning_kind);
  if (event.type.startsWith("agent-tool-")) {
    return readableToolName(event.details?.tool);
  }
  return humanEventLabel(event.type);
}

/** Pull text out of a bounded tool preview while leaving its complete fields in the detail panel. */
function compactToolPreview(value: string, depth = 0): string {
  if (depth > 2 || !value.trim().startsWith("{")) return value;
  try {
    const parsed = record(JSON.parse(value));
    for (const key of ["error", "stdout", "message", "text", "output", "goals"]) {
      const text = str(parsed[key]);
      if (text) return compactToolPreview(text, depth + 1);
    }
    if (Array.isArray(parsed.content)) {
      const text = parsed.content.map(block => str(record(block).text)).filter(Boolean).slice(0, 3).join(" · ");
      if (text) return compactToolPreview(text, depth + 1);
    }
  } catch {
    // A truncated JSON preview is still useful text; the exact record is expandable.
  }
  return value;
}

/** Prefer the provider's description or content to runtime framing in compact rows. */
export function activityEventPreview(event: ActivityEvent): string {
  const details = record(event.details);
  const text = event.type === "agent-tool-start"
    ? str(details.description) || str(details.content_preview) || str(details.arguments_preview)
    : event.type === "agent-tool-end" || event.type === "agent-tool-progress"
      ? str(details.result_preview) || str(details.description)
      : str(details.content_preview) || str(details.reasoning_preview);
  const preview = event.type.startsWith("agent-tool-") ? compactToolPreview(text || event.message) : text || event.message;
  return preview.replace(/\s+/g, " ").trim();
}

/** Describe observed activity only; a finished provider tool is not an accepted proof. */
export function activityEventState(event: ActivityEvent): string {
  const details = record(event.details);
  const status = str(details.status).toLowerCase();
  if (details.error || details.is_error === true || details.success === false || BAD.has(status)) return "Failed";
  if (event.type === "agent-tool-start") return "Running";
  if (details.partial === true) return "Streaming";
  if (event.type === "agent-tool-end") return "Finished";
  return "";
}

/** Keep an expanded live item open as its cumulative preview receives a new evidence id. */
export function activityEventKey(event: ActivityEvent): string {
  const item = event.details?.item_id;
  return typeof item === "string" && item && typeof event.details?.invocation_id === "string" && event.details.invocation_id
    ? JSON.stringify([
      event.run_id, event.details?.job_id || event.agent_id, event.agent_id,
      event.details?.invocation_id || "", event.type === "agent-tool-progress" ? "agent-tool-end" : event.type,
      event.details?.reasoning_kind || "", item,
    ])
    : event.event_id;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && !Number.isNaN(value)
    ? value
    : null;
}

/**
 * Summarize every agent session in the complete stream.
 *
 * Built from the unfiltered events so a segment header still knows the job's
 * role, budget and outcome when the active filter hides those boundary rows.
 */
export function indexSessions(events: readonly ActivityEvent[]): Map<string, SessionInfo> {
  const sessions = new Map<string, SessionInfo>();
  for (const event of events) {
    const agentId = event.agent_id;
    if (!agentId) {
      continue;
    }
    const details = record(event.details);
    const info = sessions.get(agentId) ?? {
      agentId,
      role: "",
      nodeId: "",
      status: "",
      startedAt: "",
      endedAt: "",
      calls: null,
      budget: null,
    };
    if (!info.startedAt || (event.timestamp && event.timestamp < info.startedAt)) {
      info.startedAt = event.timestamp;
    }
    if (event.timestamp > info.endedAt) {
      info.endedAt = event.timestamp;
    }
    info.role = str(details.role) || info.role;
    info.nodeId = str(details.node_id) || info.nodeId;
    info.budget = count(details.api_budget) ?? info.budget;
    const calls = count(details.api_calls);
    if (calls !== null && (info.calls === null || calls > info.calls)) {
      info.calls = calls;
    }
    if (SESSION_END.has(event.type)) {
      info.status = str(details.status) || info.status || "finished";
    }
    sessions.set(agentId, info);
  }
  return sessions;
}

/**
 * Split rows into contiguous same-agent segments.
 *
 * Contiguity keeps the timeline honest: two jobs running in parallel produce
 * interleaved segments rather than one reordered block per agent.
 */
export function segmentLogRows(
  rows: readonly ActivityEvent[],
  sessions: ReadonlyMap<string, SessionInfo>,
): LogSegment[] {
  const segments: LogSegment[] = [];
  for (const event of rows) {
    const last = segments[segments.length - 1];
    const startsSession = SESSION_START.has(event.type);
    if (last && last.agentId === event.agent_id && !startsSession) {
      last.events.push(event);
      continue;
    }
    segments.push({
      key: `${event.agent_id}:${activityEventKey(event)}`,
      agentId: event.agent_id,
      session: sessions.get(event.agent_id) ?? null,
      events: [event],
    });
  }
  return segments;
}

/**
 * Fewest rows worth putting a header above, absent a session boundary.
 *
 * Parallel provers interleave one row at a time. Framing those slivers would
 * put a header above every single row, which reads worse than no grouping at
 * all, so a sliver stays a plain row that carries its own agent tag.
 */
const MIN_FRAMED_EVENTS = 4;

/** Whether a segment names a real unit of work rather than an interleaving sliver. */
export function shouldFrameSegment(segment: LogSegment): boolean {
  if (!segment.agentId) {
    return false;
  }
  const bounded = segment.events.some(
    (event) => SESSION_START.has(event.type) || SESSION_END.has(event.type),
  );
  return bounded || segment.events.length >= MIN_FRAMED_EVENTS;
}

/** Elapsed time between two ISO stamps, or "" when it cannot be computed. */
export function elapsed(startIso: string, endIso: string): string {
  const start = Date.parse(startIso);
  const end = Date.parse(endIso);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) {
    return "";
  }
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) {
    return `${seconds}s`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** Short chips describing what a segment's job was and how it went. */
export function segmentFacts(segment: LogSegment): string[] {
  const session = segment.session;
  if (!session) {
    return [];
  }
  const facts: string[] = [];
  if (session.role) {
    facts.push(session.role);
  }
  if (session.nodeId) {
    facts.push(session.nodeId);
  }
  if (session.calls !== null) {
    facts.push(
      session.budget !== null
        ? `${session.calls.toLocaleString()} / ${session.budget.toLocaleString()} calls`
        : `${session.calls.toLocaleString()} calls`,
    );
  }
  const duration = elapsed(session.startedAt, session.endedAt);
  if (duration) {
    facts.push(duration);
  }
  return facts;
}

const GOOD = new Set(["completed", "succeeded", "proved", "verified", "accepted"]);
const BAD = new Set([
  "error",
  "failed",
  "provider_error",
  "environment_error",
  "source_conflict",
  "verification_failed",
  "planning_stalled",
  "disproved",
  "invalidated",
]);

/** Tone for a session's terminal status: green when it worked, red when it did not. */
export function statusTone(status: string): "ok" | "bad" | "warn" | "" {
  const normalized = status.toLowerCase();
  if (!normalized) {
    return "";
  }
  if (GOOD.has(normalized)) {
    return "ok";
  }
  if (BAD.has(normalized)) {
    return "bad";
  }
  return "warn";
}

/** The provider turn a row belongs to, for rows that consume the call budget. */
export function turnNumber(event: ActivityEvent): number | null {
  if (!event.type.startsWith("api-")) {
    return null;
  }
  return count(record(event.details).api_calls);
}
