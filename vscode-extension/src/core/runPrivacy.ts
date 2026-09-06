/** Build credential-safe, bounded records for extension persistence and display. */
import { createHash } from "node:crypto";

import {
  describeCommand,
  isRedactedPromptMarker,
} from "./launch";
import type { LaunchEnv } from "./launch";
import type {
  ActivityEvent,
  ActivityEventDetail,
  FlagCatalog,
  LaunchPlanPreview,
  LaunchRequest,
  LiveStatus,
  ProfileCatalog,
  ProfileDiffRow,
  RunSummary,
  TrackedRun,
} from "./types";

const MAX_COMMAND_LENGTH = 4096;
const MAX_DIAGNOSTIC_LENGTH = 8192;
const MAX_RUN_LOG_LENGTH = 2 * 1024 * 1024;
/** One expanded event may carry a whole model turn or tool payload. */
const MAX_EVIDENCE_LENGTH = 256 * 1024;
const MAX_NESTING_DEPTH = 12;
const REDACTED_CREDENTIAL = "<redacted:credential>";

const SENSITIVE_FIELD_NAMES = new Set([
  "apikey",
  "authorization",
  "credential",
  "credentials",
  "password",
  "refreshtoken",
  "secret",
  "secrets",
  "token",
  "accesstoken",
]);
const PROMPT_FIELD_NAMES = new Set(["prompt", "systemprompt", "userprompt"]);
const CREDENTIAL_QUERY_PARTS = [
  "auth",
  "authorization",
  "bearer",
  "code",
  "credential",
  "jwt",
  "key",
  "passwd",
  "password",
  "secret",
  "session",
  "sig",
  "signature",
  "token",
];

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function normalizedFieldName(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function isSensitiveFieldName(value: string): boolean {
  const normalized = normalizedFieldName(value);
  return (
    SENSITIVE_FIELD_NAMES.has(normalized) ||
    normalized.endsWith("apikey") ||
    normalized.endsWith("accesstoken") ||
    normalized.endsWith("refreshtoken") ||
    normalized.endsWith("password") ||
    normalized.endsWith("credential")
  );
}

function credentialQueryKey(key: string): boolean {
  const words = key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
  const compact = words.join("");
  return words.some(
    (word) =>
      CREDENTIAL_QUERY_PARTS.includes(word) ||
      CREDENTIAL_QUERY_PARTS.some((credential) => compact.includes(credential)),
  );
}

/** Remove userinfo, credential query values, and fragments from one URL. */
export function redactCredentialUrl(value: string): string {
  const trailing = /[),.;!?]+$/.exec(value)?.[0] ?? "";
  const candidate = trailing ? value.slice(0, -trailing.length) : value;
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return value;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return value;
  }
  parsed.username = "";
  parsed.password = "";
  for (const key of [...parsed.searchParams.keys()]) {
    if (credentialQueryKey(key)) {
      parsed.searchParams.set(key, "[redacted]");
    }
  }
  parsed.hash = "";
  return `${parsed.toString()}${trailing}`;
}

function bounded(value: string, maximum: number): string {
  if (value.length <= maximum) {
    return value;
  }
  const suffix = ` … <truncated sha256=${sha256(value)} chars=${Array.from(value).length}>`;
  return `${value.slice(0, Math.max(0, maximum - suffix.length))}${suffix}`;
}

function boundedCommand(value: string): string {
  if (value.length <= MAX_COMMAND_LENGTH) {
    return value;
  }
  const promptIndex = value.lastIndexOf(" --prompt ");
  const promptTail = promptIndex >= 0 ? value.slice(promptIndex) : "";
  const suffix =
    ` … <truncated sha256=${sha256(value)} chars=${Array.from(value).length}>` +
    promptTail;
  return `${value.slice(0, Math.max(0, MAX_COMMAND_LENGTH - suffix.length))}${suffix}`;
}

/** Return a stable marker proving prompt equality without retaining its text. */
export function promptEvidenceMarker(value: string): string {
  const prompt = value.trim();
  if (!prompt || isRedactedPromptMarker(prompt)) {
    return prompt;
  }
  return `<redacted:prompt sha256=${sha256(prompt)} chars=${Array.from(prompt).length}>`;
}

function decodedCommandTail(value: string): string {
  const tail = value.trim();
  if (tail.startsWith('"') && tail.endsWith('"')) {
    try {
      const parsed = JSON.parse(tail) as unknown;
      if (typeof parsed === "string") {
        return parsed;
      }
    } catch {
      // Hash the rendered tail below. It is still removed from the result.
    }
  }
  return tail;
}

function redactCommandPrompt(value: string): string {
  const match = /(^|\s)--prompt(?:\s+|=)/.exec(value);
  if (!match || match.index === undefined) {
    return value;
  }
  const optionStart = match.index + match[1].length;
  const tail = value.slice(match.index + match[0].length);
  const marker = promptEvidenceMarker(decodedCommandTail(tail));
  return `${value.slice(0, optionStart)}--prompt ${JSON.stringify(marker)}`;
}

/**
 * Remove prompt tails and recognizable credential tokens from diagnostic text.
 *
 * Exact prompt values are supplied where the host still has the one-shot
 * launch request. Pattern redaction is defense in depth for CLI diagnostics
 * and stale state written by older versions.
 */
export function redactSensitiveText(
  value: string,
  exactPrompts: readonly string[] = [],
  maximumLength = MAX_DIAGNOSTIC_LENGTH,
): string {
  let safe = value;
  for (const candidate of exactPrompts) {
    const prompt = candidate.trim();
    if (prompt) {
      safe = safe.split(prompt).join(promptEvidenceMarker(prompt));
    }
  }
  safe = redactCommandPrompt(safe);
  safe = safe.replace(/\bhttps?:\/\/[^\s<>"']+/gi, (url) => redactCredentialUrl(url));
  safe = safe.replace(
    /\bBearer\s+[A-Za-z0-9._~+/=-]{12,}/gi,
    `Bearer ${REDACTED_CREDENTIAL}`,
  );
  safe = safe.replace(
    /\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b/g,
    REDACTED_CREDENTIAL,
  );
  safe = safe.replace(
    /(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|credential|secret)\b\s*[:=]\s*)(["']?)([^\s,"'}&?#]+)/gi,
    (whole, prefix: string, quote: string, raw: string) => {
      let decoded = raw;
      try {
        decoded = decodeURIComponent(raw);
      } catch {
        // A malformed percent escape is still replaced below.
      }
      return decoded === "[redacted]" || decoded === REDACTED_CREDENTIAL
        ? whole
        : `${prefix}${quote}${REDACTED_CREDENTIAL}`;
    },
  );
  return bounded(safe, maximumLength);
}

function boundedRunLog(value: string): string {
  if (value.length <= MAX_RUN_LOG_LENGTH) {
    return value;
  }
  const marker =
    `<truncated earlier log sha256=${sha256(value)} chars=${Array.from(value).length}>\n`;
  return `${marker}${value.slice(-(MAX_RUN_LOG_LENGTH - marker.length))}`;
}

/** Return a structurally raw but prompt/credential-safe bounded activity log. */
export function runLogForDisplay(value: string): string {
  const safe = value.split(/\r?\n/).map((line) => {
    try {
      const parsed = JSON.parse(line) as unknown;
      return JSON.stringify(sanitizeUnknown(parsed, "", []));
    } catch {
      return redactSensitiveText(line);
    }
  });
  return boundedRunLog(safe.join("\n"));
}

function sanitizeUnknown(
  value: unknown,
  fieldName: string,
  exactPrompts: readonly string[],
  depth = 0,
  maximumLength = MAX_DIAGNOSTIC_LENGTH,
): unknown {
  if (depth > MAX_NESTING_DEPTH) {
    return "<redacted:excessive-nesting>";
  }
  if (fieldName && isSensitiveFieldName(fieldName)) {
    return value === "" || value === null || value === undefined
      ? value
      : REDACTED_CREDENTIAL;
  }
  if (typeof value === "string") {
    const normalized = normalizedFieldName(fieldName);
    if (PROMPT_FIELD_NAMES.has(normalized)) {
      return promptEvidenceMarker(value);
    }
    return redactSensitiveText(value, exactPrompts, maximumLength);
  }
  if (Array.isArray(value)) {
    return value.map((item) =>
      sanitizeUnknown(item, fieldName, exactPrompts, depth + 1, maximumLength),
    );
  }
  if (value !== null && typeof value === "object") {
    const safe: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      safe[key] = sanitizeUnknown(item, key, exactPrompts, depth + 1, maximumLength);
    }
    return safe;
  }
  return value;
}

/** Normalize and scrub both current and pre-0.1.5 persisted override shapes. */
function safeOverrideEnvironment(
  env: LaunchEnv | Readonly<Record<string, unknown>> | null | undefined,
): LaunchEnv {
  if (env === null || env === undefined || typeof env !== "object") {
    return { set: {}, unset: [] };
  }
  const candidate = env as Readonly<Record<string, unknown>>;
  const nestedSet = candidate.set;
  // Older releases persisted the applied override map directly. Treat that
  // shape as `set` so opening a workspace upgrades it instead of crashing the
  // entire extension during activation.
  const rawSet =
    nestedSet !== null && typeof nestedSet === "object" && !Array.isArray(nestedSet)
      ? (nestedSet as Readonly<Record<string, unknown>>)
      : candidate;
  const set: Record<string, string> = {};
  for (const [name, value] of Object.entries(rawSet)) {
    if (name === "set" || name === "unset" || typeof value !== "string") {
      continue;
    }
    set[name] = isSensitiveFieldName(name)
      ? REDACTED_CREDENTIAL
      : redactSensitiveText(value);
  }
  const unset = Array.isArray(candidate.unset)
    ? candidate.unset.filter((value): value is string => typeof value === "string")
    : [];
  return { set, unset };
}

function safeNamedValues(values: Readonly<Record<string, string>>): Record<string, string> {
  const safe: Record<string, string> = {};
  for (const [name, value] of Object.entries(values)) {
    safe[name] = isSensitiveFieldName(name)
      ? REDACTED_CREDENTIAL
      : redactSensitiveText(value);
  }
  return safe;
}

/** Return a launch request suitable for durable state, never for execution. */
export function launchRequestForPersistence(request: LaunchRequest): LaunchRequest {
  const prompt = promptEvidenceMarker(request.prompt);
  const safeText = (value: string): string => redactSensitiveText(value, [request.prompt]);
  const overrides: Record<string, string> = {};
  for (const [name, value] of Object.entries(request.overrides)) {
    overrides[name] = isSensitiveFieldName(name)
      ? REDACTED_CREDENTIAL
      : safeText(value);
  }
  return {
    ...request,
    target: safeText(request.target),
    provider: safeText(request.provider),
    model: safeText(request.model),
    axioms: safeText(request.axioms),
    prompt,
    additionalSkills: request.additionalSkills.map(safeText),
    profile: safeText(request.profile),
    overrides,
  };
}

/** Return a bounded tracked-run clone safe for workspace state and webviews. */
export function trackedRunForPersistence(run: TrackedRun): TrackedRun {
  const rawPrompt = run.request.prompt;
  const request = launchRequestForPersistence(run.request);
  const appliedOverrides = safeOverrideEnvironment(run.appliedOverrides);
  return {
    ...run,
    label: bounded(redactSensitiveText(run.label, [rawPrompt]), 512),
    request,
    command: boundedCommand(describeCommand(request, appliedOverrides.set)),
    appliedOverrides,
    error:
      run.error === null ? null : redactSensitiveText(run.error, [rawPrompt]),
  };
}

/** Return a CLI run row safe to include in a webview/tree snapshot. */
export function runSummaryForDisplay(summary: RunSummary): RunSummary {
  return sanitizeUnknown(summary, "", []) as RunSummary;
}

/** Return live state safe to include in a webview snapshot. */
export function liveStatusForDisplay(status: LiveStatus): LiveStatus {
  return sanitizeUnknown(status, "", []) as LiveStatus;
}

/** Preserve a full bounded proof plan while redacting every other diagnostic normally. */
export function proverStateForDisplay(status: LiveStatus): LiveStatus {
  const safe = liveStatusForDisplay(status);
  if (status && typeof status.plan_markdown === "string") {
    safe.plan_markdown = redactSensitiveText(status.plan_markdown, [], 512 * 1024);
  }
  return safe;
}

/** Return a dry-run plan without prompt or credential values. */
export function launchPlanForDisplay(
  plan: LaunchPlanPreview,
  prompt: string,
): LaunchPlanPreview {
  return sanitizeUnknown(plan, "", [prompt]) as LaunchPlanPreview;
}

/** Return an activity row safe to cross the webview boundary. */
export function activityEventForDisplay(event: ActivityEvent): ActivityEvent {
  return sanitizeUnknown(event, "", []) as ActivityEvent;
}

/** Keep a whole recorded model turn readable while redacting credentials in it. */
export function eventDetailForDisplay(detail: ActivityEventDetail): ActivityEventDetail {
  return sanitizeUnknown(detail, "", [], 0, MAX_EVIDENCE_LENGTH) as ActivityEventDetail;
}

/** Return profile metadata without credential-shaped override values. */
export function profileCatalogForDisplay(catalog: ProfileCatalog | null): ProfileCatalog | null {
  if (catalog === null) {
    return null;
  }
  return {
    ...catalog,
    search_paths: catalog.search_paths.map((value) => redactSensitiveText(value)),
    profiles: catalog.profiles.map((profile) => ({
      ...profile,
      name: redactSensitiveText(profile.name),
      summary: redactSensitiveText(profile.summary),
      overrides: safeNamedValues(profile.overrides),
    })),
  };
}

/** Return flag metadata without defaults for terminal-only sensitive knobs. */
export function flagCatalogForDisplay(catalog: FlagCatalog | null): FlagCatalog | null {
  if (catalog === null) {
    return null;
  }
  return {
    ...catalog,
    groups: catalog.groups.map((group) => ({
      ...group,
      flags: group.flags.map((flag) => ({
        ...flag,
        default:
          flag.sensitive || isSensitiveFieldName(flag.name)
            ? REDACTED_CREDENTIAL
            : redactSensitiveText(flag.default),
      })),
    })),
  };
}

/** Return profile-diff rows safe to post to the webview. */
export function profileDiffForDisplay(rows: readonly ProfileDiffRow[]): ProfileDiffRow[] {
  return rows.map((row) => ({
    ...row,
    left: isSensitiveFieldName(row.name)
      ? REDACTED_CREDENTIAL
      : redactSensitiveText(row.left),
    right: isSensitiveFieldName(row.name)
      ? REDACTED_CREDENTIAL
      : redactSensitiveText(row.right),
  }));
}
