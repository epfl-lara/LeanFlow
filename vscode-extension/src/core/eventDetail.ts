/**
 * Turn one activity row, plus the full record it may resolve to, into what a
 * reader wants when they expand it: the model's words, the tools it asked for,
 * and what came back — as readable text, fields, and code, never as one
 * escaped JSON string.
 *
 * Pure and host-free so the webview and the tests share it. Rendering lives in
 * the webview; the decisions about *how* each value should read are here.
 */
import type { ActivityEvent, ActivityEventDetail } from "./types";

export type SectionKind = "prose" | "code" | "fields";

export interface EventOutputSection {
  id: string;
  title: string;
  /** Prose and code render a string; fields render an object or array. */
  kind: SectionKind;
  value: unknown;
  language?: string;
  /** Plain text for the Copy button. */
  text: string;
  tone?: "err" | "ok";
}

export interface EventFact {
  label: string;
  value: string;
}

export interface EvidenceStatus {
  tone: "info" | "warn" | "error";
  text: string;
}

export interface TextPresentation {
  kind: "prose" | "code";
  language?: string;
}

export interface FieldPresentation {
  kind: "inline" | "prose" | "code";
  language?: string;
}

/** Fields that describe process identity or launch context, not this event. */
export const CONTEXT_DETAIL_KEYS = new Set([
  "active_skill",
  "agent_session_id",
  "delegate_depth",
  "effective_prompt",
  "parent_agent_session_id",
  "parent_run_id",
  "process_group_id",
  "process_id",
  "process_session_id",
  "process_token_sha256",
  "project_root",
  "run_scope",
  "workflow_command",
  "workflow_kind",
]);

const CHECK_TYPES = new Set([
  "submission-feedback",
  "submission_checked",
  "candidate_checked",
  "negation_checked",
]);

/** Field names whose string values are code even without a recognizable keyword. */
const CODE_FIELDS = new Set([
  "candidate",
  "code",
  "command",
  "diff",
  "lean",
  "output",
  "patch",
  "program",
  "proof",
  "proofs",
  "query",
  "replacement",
  "script",
  "snippet",
  "source",
  "statement",
  "stderr",
  "stdout",
  "tactic",
]);

/** A string this long, or with a line break, reads as a block rather than inline. */
const INLINE_MAX_CHARS = 120;
const MAX_TOOL_CALL_SECTIONS = 20;

/** Preview fields the runtime projects into the shared stream. */
const PREVIEW_KEYS = [
  "content_preview",
  "reasoning_preview",
  "arguments_preview",
  "result_preview",
  "final_response_preview",
  "feedback_preview",
  "reason_preview",
  "message_preview",
];

const LEAN_HEAD =
  /^\s*(import |open |namespace |section |theorem |lemma |def |example |instance |structure |inductive |abbrev |noncomputable |@\[|variable |universe |set_option |\/-|--)/m;
const LEAN_TOKENS = /(:=|\bby\b|\bsimp\b|\bomega\b|\bexact\b|\bintro\b|\brw\b|\bhave\b|∀|∃|⟨|⟩|→|ℕ|ℤ|ℝ)/;
const PYTHON_HEAD = /^\s*(import \w|from \S+ import |def \w+\(|class \w+|print\(|for .+ in .+:|if __name__|while .+:)/m;
const SHELL_HEAD = /^\s*(lake |lean |cd |ls |git |python3? |pip |uv |npm |curl |wget |export |echo |rg |grep )/m;
const CODE_LINE = /(:=|;\s*$|^\s{4,}\S|\{\s*$|^\s*\}|=>|\|\|)/;

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

function first(...values: unknown[]): unknown {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

/** Parse a JSON value whether it arrives parsed, serialized, or fenced in markdown. */
export function parseStructured(value: unknown): Record<string, unknown> | unknown[] | null {
  if (Array.isArray(value)) {
    return value;
  }
  if (value !== null && typeof value === "object") {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value
    .trim()
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/, "")
    .trim();
  if (!(trimmed.startsWith("{") && trimmed.endsWith("}")) && !(trimmed.startsWith("[") && trimmed.endsWith("]"))) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(trimmed);
    return parsed !== null && typeof parsed === "object"
      ? (parsed as Record<string, unknown> | unknown[])
      : null;
  } catch {
    return null;
  }
}

/**
 * Decide whether a block of text is prose or code, and which language.
 *
 * The hint is the field name or a sibling file path: `content` written to a
 * `.lean` file is Lean even when it starts with a comment.
 */
export function classifyText(text: string, hint = ""): TextPresentation {
  const name = hint.toLowerCase();
  const trimmed = text.trim();
  if (name.endsWith(".lean") || name === "lean" || (LEAN_HEAD.test(trimmed) && LEAN_TOKENS.test(trimmed))) {
    return { kind: "code", language: "lean" };
  }
  if (name.endsWith(".py") || name === "program" || name === "script" || PYTHON_HEAD.test(trimmed)) {
    return { kind: "code", language: "python" };
  }
  if (name === "command" || (SHELL_HEAD.test(trimmed) && !/\.\s/.test(trimmed))) {
    return { kind: "code", language: "shell" };
  }
  if (name.endsWith(".json") || (parseStructured(trimmed) !== null && trimmed.length < 200_000)) {
    return { kind: "code", language: "json" };
  }
  if (name.endsWith(".md") || name.endsWith(".txt")) {
    return { kind: "prose" };
  }
  if (CODE_FIELDS.has(name)) {
    return { kind: "code" };
  }
  const lines = trimmed.split("\n");
  if (lines.length >= 3) {
    const dense = lines.filter((line) => CODE_LINE.test(line)).length;
    if (dense / lines.length > 0.5) {
      return { kind: "code" };
    }
  }
  return { kind: "prose" };
}

/** How one named string field should read: inline, prose, or a code block. */
export function fieldPresentation(
  name: string,
  value: string,
  context: { path?: string } = {},
): FieldPresentation {
  if (!value.includes("\n") && value.length < INLINE_MAX_CHARS) {
    return { kind: "inline" };
  }
  const hint = (name === "content" || name === "text") && context.path ? context.path : name;
  return classifyText(value, hint);
}

function normalizeEvent(raw: Record<string, unknown>): ActivityEvent {
  return {
    event_id: str(raw.event_id),
    timestamp: str(raw.timestamp),
    type: str(raw.type),
    run_id: str(raw.run_id),
    agent_id: str(raw.agent_id),
    task_label: str(raw.task_label),
    run_scope: str(raw.run_scope),
    message: str(raw.message),
    details: record(raw.details),
  };
}

/** Accept the CLI payload shape loosely; a malformed field degrades, never throws. */
export function normalizeEventDetail(
  value: unknown,
  runId: string,
  eventId: string,
): ActivityEventDetail {
  const raw = record(value);
  const evidence = record(raw.evidence);
  const rawRecord = evidence.record;
  const event =
    raw.event === null || raw.event === undefined ? null : normalizeEvent(record(raw.event));
  const joined =
    rawRecord === null || rawRecord === undefined || typeof rawRecord !== "object"
      ? null
      : record(rawRecord);
  return {
    version: typeof raw.version === "number" ? raw.version : 1,
    run_id: runId,
    event_id: eventId,
    found: raw.found === true && event !== null,
    event,
    evidence: {
      source: str(evidence.source) || "none",
      path: str(evidence.path),
      match: str(evidence.match),
      record:
        joined === null
          ? null
          : {
              kind: str(joined.kind),
              timestamp: str(joined.timestamp),
              evidence_id: str(joined.evidence_id),
              details: record(joined.details),
            },
    },
  };
}

/** The full record's details when the join succeeded, else null. */
export function evidenceDetails(
  detail: ActivityEventDetail | null | undefined,
): Record<string, unknown> | null {
  return detail?.evidence.record?.details ?? null;
}

/**
 * Build one section from a value of any shape.
 *
 * A JSON document (parsed, serialized, or fenced) becomes fields so its long
 * string values render as text or code instead of escaped `\n`. A plain string
 * is classified as prose or code.
 */
function section(
  id: string,
  title: string,
  value: unknown,
  options: { tone?: "err" | "ok"; hint?: string } = {},
): EventOutputSection | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const structured = parseStructured(value);
  if (structured !== null) {
    if (Array.isArray(structured) ? structured.length === 0 : Object.keys(structured).length === 0) {
      return null;
    }
    return {
      id,
      title,
      kind: "fields",
      value: structured,
      text: pretty(structured),
      tone: options.tone,
    };
  }
  const text = typeof value === "string" ? value : String(value);
  if (!text.trim()) {
    return null;
  }
  const presentation = classifyText(text, options.hint ?? "");
  return {
    id,
    title,
    kind: presentation.kind,
    value: text,
    language: presentation.language,
    text,
    tone: options.tone,
  };
}

function toolCallSections(calls: unknown, prefix: string): EventOutputSection[] {
  if (!Array.isArray(calls)) {
    return [];
  }
  const sections: EventOutputSection[] = [];
  calls.slice(0, MAX_TOOL_CALL_SECTIONS).forEach((call, index) => {
    const entry = record(call);
    // The provider shape nests the function; the runtime preview and the
    // legacy runner flatten it.
    const fn = record(entry.function);
    const name = str(fn.name) || str(entry.name) || `tool ${index + 1}`;
    const args = first(fn.arguments, entry.arguments, entry.arguments_preview);
    const built = section(`${prefix}-call-${index}`, `Tool call · ${name}`, args);
    if (built) {
      sections.push(built);
    } else {
      sections.push({
        id: `${prefix}-call-${index}`,
        title: `Tool call · ${name}`,
        kind: "prose",
        value: "(no arguments)",
        text: "",
      });
    }
  });
  return sections;
}

/** Ordered sections to render for an expanded row; empty for a row with no output. */
export function eventOutputSections(
  event: ActivityEvent,
  evidence: Record<string, unknown> | null,
): EventOutputSection[] {
  const details = record(event.details);
  const full = evidence ?? {};
  const sections: EventOutputSection[] = [];
  const add = (built: EventOutputSection | null): void => {
    if (built) {
      sections.push(built);
    }
  };
  const type = event.type;

  if (type === "api-response") {
    const assistant = record(full.assistant);
    add(section("content", "Model output", first(assistant.content, details.content_preview)));
    add(
      section(
        "reasoning",
        "Reasoning summary",
        first(assistant.reasoning, assistant.reasoning_content, details.reasoning_preview),
      ),
    );
    sections.push(
      ...toolCallSections(first(assistant.tool_calls, details.tool_calls), "response"),
    );
  } else if (type === "assistant-response") {
    add(section("content", "Model output", details.content));
    add(section("reasoning", "Reasoning", first(details.reasoning_content, details.reasoning)));
    sections.push(...toolCallSections(details.tool_calls, "response"));
  } else if (type === "tool-result" || type === "tool-call") {
    const failed =
      record(full.result).success === false ||
      record(details.result).success === false ||
      details.success === false ||
      details.is_error === true;
    add(
      section(
        "arguments",
        "Arguments",
        first(full.arguments, details.arguments, details.arguments_preview),
      ),
    );
    if (type === "tool-result") {
      add(
        section(
          "result",
          failed ? "Result (failed)" : "Result",
          first(full.result, details.result, details.result_preview),
          { tone: failed ? "err" : undefined },
        ),
      );
    }
  } else if (type === "job-session-end") {
    add(
      section(
        "final",
        "Final response",
        first(full.final_response, details.final_response_preview),
      ),
    );
    add(section("error", "Error", first(full.error, details.error), { tone: "err" }));
  } else if (type === "conversation-start") {
    add(section("prompt", "Prompt", details.user_message));
  } else if (type === "user_message") {
    add(section("message", "Message", first(full.message, details.message_preview)));
  } else if (type === "plan_rejected" || type === "plan_refinement_budget_exhausted") {
    add(section("reason", "Reason", first(full.reason, details.reason_preview)));
  } else if (CHECK_TYPES.has(type)) {
    const accepted = first(details.accepted, full.accepted);
    add(
      section(
        "verdict",
        "Verification result",
        first(full.result, full.feedback, details.feedback_preview),
        { tone: accepted === false ? "err" : accepted === true ? "ok" : undefined },
      ),
    );
    add(section("error", "Error", first(full.error, details.error), { tone: "err" }));
  } else {
    add(section("error", "Error", first(details.error, full.error), { tone: "err" }));
  }

  if (sections.length === 0 && evidence) {
    const rest = { ...evidence };
    for (const key of ["job_id", "node_id", "evidence_id"]) {
      delete rest[key];
    }
    add(section("record", "Recorded details", rest));
  }
  return sections;
}

function count(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "";
}

/** Short facts for the header of an expanded row. */
export function eventFacts(
  event: ActivityEvent,
  evidence: Record<string, unknown> | null,
): EventFact[] {
  const details = record(event.details);
  const full = evidence ?? {};
  const assistant = record(full.assistant);
  const facts: EventFact[] = [];
  const add = (label: string, value: string): void => {
    if (value) {
      facts.push({ label, value });
    }
  };
  add("agent", event.agent_id);
  const job = str(details.job_id);
  if (job && job !== event.agent_id) {
    add("job", job);
  }
  add("node", str(details.node_id));
  add("role", str(details.role));
  const calls = count(details.api_calls);
  const budget = count(details.api_budget);
  if (calls) {
    add("calls", budget ? `${calls} / ${budget}` : calls);
  }
  add("model", str(details.model) || str(details.response_model));
  const input = count(details.input_tokens);
  const output = count(details.output_tokens);
  if (input || output) {
    // The runtime records the session's cumulative usage at this point.
    add("session tokens", `in ${input || "—"} · out ${output || "—"}`);
  }
  add("finish", str(details.finish_reason) || str(assistant.finish_reason));
  add("tool", str(details.tool));
  add("status", str(details.status));
  if (typeof details.accepted === "boolean") {
    add("accepted", details.accepted ? "yes" : "no");
  }
  if (details.conditional === true) {
    add("conditional", "yes");
  }
  const iteration = count(details.iteration);
  if (iteration) {
    add("step", iteration);
  }
  if (typeof details.duration_seconds === "number") {
    add("took", `${details.duration_seconds.toFixed(2)}s`);
  }
  return facts;
}

const searchCache = new WeakMap<ActivityEvent, string>();

/** Lower-cased text a filter box should match, including projected previews. */
export function eventSearchText(event: ActivityEvent): string {
  const cached = searchCache.get(event);
  if (cached !== undefined) {
    return cached;
  }
  const details = record(event.details);
  const parts = [event.type, event.message, event.agent_id];
  for (const key of ["tool", "node_id", "model", "status", "error", "content", ...PREVIEW_KEYS]) {
    parts.push(str(details[key]));
  }
  if (Array.isArray(details.tool_calls)) {
    for (const call of details.tool_calls) {
      const entry = record(call);
      const fn = record(entry.function);
      parts.push(str(fn.name) || str(entry.name), str(entry.arguments_preview));
    }
  }
  const joined = parts.filter(Boolean).join(" ").toLowerCase();
  searchCache.set(event, joined);
  return joined;
}

/** Event details without the process identity and launch context repeated on every row. */
export function rawDetailsForDisplay(details: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(details).filter(([key]) => !CONTEXT_DETAIL_KEYS.has(key)),
  );
}

function hasPreview(event: ActivityEvent): boolean {
  const details = record(event.details);
  return (
    PREVIEW_KEYS.some((key) => Boolean(str(details[key]))) ||
    (Array.isArray(details.tool_calls) && details.tool_calls.length > 0)
  );
}

/**
 * Explain what the panel is showing relative to the recorded evidence.
 *
 * Returns null when there is nothing to caveat: an exact join, or a row that
 * never had model output to begin with.
 */
export function evidenceStatus(
  event: ActivityEvent,
  detail: ActivityEventDetail | null,
  loading: boolean,
  error: string,
): EvidenceStatus | null {
  if (loading) {
    return { tone: "info", text: "Loading the recorded output…" };
  }
  if (error) {
    return { tone: "error", text: `Could not load the recorded output: ${error}` };
  }
  if (!detail) {
    return null;
  }
  if (!detail.found) {
    return { tone: "warn", text: "This event is no longer in the recorded stream." };
  }
  if (detail.evidence.record === null) {
    return hasPreview(event)
      ? {
          tone: "warn",
          text: "Showing the bounded preview only; no full record was found in the job's log.",
        }
      : null;
  }
  if (detail.evidence.match === "heuristic") {
    return {
      tone: "info",
      text: "Matched to the job log by kind and time; this run was recorded before exact event ids existed.",
    };
  }
  return null;
}
