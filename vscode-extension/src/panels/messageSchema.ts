/**
 * Runtime validation for messages arriving from the webview.
 *
 * TypeScript types vanish at runtime. The webview is a separate document that
 * can post any JSON, so every field the host acts on — especially anything that
 * becomes a path, an argv entry, or a child-process environment variable — is
 * checked here before the handler sees it. A rejected message is dropped with a
 * reason rather than being partially applied.
 */
import type { LaunchRequest, WebviewMessage, WorkflowKind } from "../core/types";
import {
  MAX_EXPERIMENT_AXIS,
  MAX_EXPERIMENT_CELLS,
} from "../core/experimentMatrix";
import { LAUNCH_FIELD_LIMITS, WORKFLOW_KINDS } from "../core/types";

const MAX_STRING = 8192;
const MAX_LIST = LAUNCH_FIELD_LIMITS.additionalSkillCount;
const MAX_OVERRIDES = LAUNCH_FIELD_LIMITS.overrideCount;

const KINDS = new Set<string>(WORKFLOW_KINDS.map((entry) => entry.id));
const SAFE_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$/;

function str(value: unknown, max = MAX_STRING): string | null {
  return typeof value === "string" && value.length <= max && !value.includes("\0")
    ? value
    : null;
}

function strictStrList(
  value: unknown,
  maxEntries: number,
  maxEntryLength = MAX_STRING,
): string[] | null {
  if (!Array.isArray(value) || value.length > maxEntries) {
    return null;
  }
  const parsed = value.map((entry) => str(entry, maxEntryLength));
  return parsed.some((entry) => entry === null) ? null : (parsed as string[]);
}

function strictStringRecord(value: unknown, maxEntries: number): Record<string, string> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const entries = Object.entries(value);
  if (entries.length > maxEntries) {
    return null;
  }
  const parsed: Record<string, string> = {};
  for (const [name, entry] of entries) {
    if (!name || name.length > LAUNCH_FIELD_LIMITS.overrideName || name.includes("\0")) {
      return null;
    }
    const text = str(entry, LAUNCH_FIELD_LIMITS.overrideValue);
    if (text === null) {
      return null;
    }
    parsed[name] = text;
  }
  return parsed;
}

function strictInt(value: unknown, min: number, max: number): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= min && value <= max
    ? value
    : null;
}

/** Return a filesystem-safe persisted identifier, or null. */
function identifier(value: unknown): string | null {
  const parsed = str(value, 256);
  return parsed !== null && SAFE_IDENTIFIER.test(parsed) && parsed !== "." && parsed !== ".."
    ? parsed
    : null;
}

/** A rejected launch names the offending field, never its value. */
type LaunchRequestResult =
  | { ok: true; request: LaunchRequest }
  | { ok: false; field: string };

function strictBool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/**
 * Normalize a launch request.
 *
 * Rejects when a field the host would act on is missing or malformed — a launch
 * built from a half-understood payload is worse than no launch. The offending
 * field name travels with the rejection so the UI can say which input to fix
 * instead of dropping the request silently. Override *names* are not filtered
 * here: `resolveLaunchEnv` checks them against the declared catalog, which is
 * the authoritative list.
 */
function launchRequest(value: unknown): LaunchRequestResult {
  if (typeof value !== "object" || value === null) {
    return { ok: false, field: "request" };
  }
  const raw = value as Record<string, unknown>;
  const kind = str(raw.kind, 32);
  if (kind === null || !KINDS.has(kind)) {
    return { ok: false, field: "kind" };
  }
  // Keyed so a rejection can report which field failed. Insertion order is the
  // order fields are reported in, so it stays the order the form presents them.
  const fields = {
    target: str(raw.target, LAUNCH_FIELD_LIMITS.target),
    provider: str(raw.provider, LAUNCH_FIELD_LIMITS.provider),
    model: str(raw.model, LAUNCH_FIELD_LIMITS.model),
    agents: strictInt(raw.agents, 1, 64),
    research: strictBool(raw.research),
    noParallel: strictBool(raw.noParallel),
    cleanRoom: strictBool(raw.cleanRoom),
    humanReview: strictBool(raw.humanReview),
    axioms: str(raw.axioms, LAUNCH_FIELD_LIMITS.axioms),
    prompt: str(raw.prompt, LAUNCH_FIELD_LIMITS.prompt),
    profile: str(raw.profile, LAUNCH_FIELD_LIMITS.profile),
    overrides: strictStringRecord(raw.overrides, MAX_OVERRIDES),
    additionalSkills: strictStrList(
      raw.additionalSkills,
      MAX_LIST,
      LAUNCH_FIELD_LIMITS.additionalSkill,
    ),
  };
  const {
    target,
    provider,
    model,
    agents,
    research,
    noParallel,
    cleanRoom,
    humanReview,
    axioms,
    prompt,
    profile,
    overrides,
    additionalSkills,
  } = fields;
  if (
    target === null ||
    provider === null ||
    model === null ||
    agents === null ||
    research === null ||
    noParallel === null ||
    cleanRoom === null ||
    humanReview === null ||
    axioms === null ||
    prompt === null ||
    profile === null ||
    overrides === null ||
    additionalSkills === null
  ) {
    return {
      ok: false,
      field: Object.entries(fields).find(([, entry]) => entry === null)?.[0] ?? "request",
    };
  }
  const workers = raw.researchWorkers;
  const researchWorkers = workers === null ? null : strictInt(workers, 0, 64);
  if (researchWorkers === null && workers !== null) {
    return { ok: false, field: "researchWorkers" };
  }
  return {
    ok: true,
    request: {
      kind: kind as WorkflowKind,
      target,
      provider,
      model,
      agents,
      research,
      researchWorkers,
      noParallel,
      cleanRoom,
      humanReview,
      axioms,
      prompt,
      additionalSkills,
      profile,
      overrides,
    },
  };
}

/**
 * Validate one inbound message.
 *
 * Returns the normalized message, or a reason it was rejected.
 */
export function parseWebviewMessage(
  value: unknown,
): { ok: true; message: WebviewMessage } | { ok: false; reason: string } {
  if (typeof value !== "object" || value === null) {
    return { ok: false, reason: "message is not an object" };
  }
  const raw = value as Record<string, unknown>;
  const type = str(raw.type, 64);
  if (type === null) {
    return { ok: false, reason: "message has no type" };
  }

  const reject = (field: string) => ({ ok: false as const, reason: `${type}: bad ${field}` });

  switch (type) {
    case "ready":
    case "refresh":
    case "openDashboard":
    case "openCliSettings":
      return { ok: true, message: { type } as WebviewMessage };

    case "launch":
    case "preview": {
      const parsed = launchRequest(raw.request);
      if (parsed.ok === false) {
        return reject(`request field "${parsed.field}"`);
      }
      const { request } = parsed;
      if (type === "launch") {
        return { ok: true, message: { type, request } };
      }
      const requestId = str(raw.requestId, 64);
      return requestId === null
        ? reject("requestId")
        : { ok: true, message: { type: "preview", requestId, request } };
    }

    case "stopRun": {
      const id = str(raw.id, 128);
      return id === null ? reject("id") : { ok: true, message: { type, id } };
    }

    case "selectRun": {
      if (raw.id === null) {
        return { ok: true, message: { type, id: null } };
      }
      const id = str(raw.id, 128);
      return id === null ? reject("id") : { ok: true, message: { type, id } };
    }

    case "loadEvents":
    case "loadRunLog": {
      const runId = raw.runId === "" ? "" : identifier(raw.runId);
      return runId === null ? reject("runId") : { ok: true, message: { type, runId } };
    }

    case "loadProver": {
      const runId = identifier(raw.runId);
      return runId === null ? reject("runId") : { ok: true, message: { type, runId } };
    }

    case "loadEventDetail": {
      // Both ids become CLI arguments that name files under the state root.
      const runId = identifier(raw.runId);
      const eventId = identifier(raw.eventId);
      return runId !== null && eventId !== null
        ? { ok: true, message: { type, runId, eventId } }
        : reject("event fields");
    }

    case "proverMessage": {
      const runId = identifier(raw.runId);
      const agentId = identifier(raw.agentId);
      const message = str(raw.message, 8192);
      const requestId = raw.requestId === undefined ? undefined : identifier(raw.requestId);
      return runId !== null && agentId !== null && message !== null && message.trim() && requestId !== null
        ? { ok: true, message: { type, runId, agentId, message, requestId } } : reject("message fields");
    }

    case "openProverFile": {
      const runId = identifier(raw.runId);
      const filePath = str(raw.path, 4096);
      const baselinePath = raw.baselinePath === undefined ? undefined : str(raw.baselinePath, 4096);
      const line = raw.line === undefined ? undefined : strictInt(raw.line, 1, 10000000);
      return runId !== null && filePath !== null && filePath !== "" && baselinePath !== null && line !== null
        ? { ok: true, message: { type, runId, path: filePath, baselinePath, line } } : reject("file fields");
    }

    case "saveProfile": {
      const profile = raw.profile;
      if (typeof profile !== "object" || profile === null) {
        return reject("profile");
      }
      const entry = profile as Record<string, unknown>;
      const name = str(entry.name, LAUNCH_FIELD_LIMITS.profile);
      const summary = str(entry.summary, LAUNCH_FIELD_LIMITS.profileSummary);
      const overrides = strictStringRecord(entry.overrides, MAX_OVERRIDES);
      if (name === null || summary === null || overrides === null) {
        return reject("profile fields");
      }
      return {
        ok: true,
        message: {
          type,
          profile: {
            name,
            summary,
            builtin: false,
            overrides,
          },
        },
      };
    }

    case "deleteProfile": {
      const name = str(raw.name, 128);
      return name === null ? reject("name") : { ok: true, message: { type, name } };
    }

    case "diffProfiles": {
      const requestId = str(raw.requestId, 64);
      const left = identifier(raw.left);
      const right = identifier(raw.right);
      return requestId !== null && left !== null && right !== null
        ? { ok: true, message: { type, requestId, left, right } }
        : reject("fields");
    }

    case "createExperiment": {
      const matrix = raw.matrix;
      if (typeof matrix !== "object" || matrix === null) {
        return reject("matrix");
      }
      const entry = matrix as Record<string, unknown>;
      const id = identifier(entry.id);
      const name = str(entry.name, 128);
      const kind = str(entry.kind, 32);
      const base = launchRequest(entry.base);
      if (base.ok === false) {
        return reject(`matrix base field "${base.field}"`);
      }
      const targets = strictStrList(
        entry.targets,
        MAX_EXPERIMENT_AXIS,
        LAUNCH_FIELD_LIMITS.target,
      );
      const profiles = strictStrList(
        entry.profiles,
        MAX_EXPERIMENT_AXIS,
        LAUNCH_FIELD_LIMITS.profile,
      );
      const models = strictStrList(entry.models, MAX_EXPERIMENT_AXIS, LAUNCH_FIELD_LIMITS.model);
      const repeats = strictInt(entry.repeats, 1, 100);
      const createdAt =
        entry.createdAt === undefined
          ? new Date().toISOString()
          : str(entry.createdAt, 64);
      if (
        id === null ||
        name === null ||
        kind === null ||
        !KINDS.has(kind) ||
        targets === null ||
        profiles === null ||
        models === null ||
        repeats === null ||
        createdAt === null
      ) {
        return reject("matrix fields");
      }
      const cellCount =
        Math.max(1, targets.length) *
        Math.max(1, profiles.length) *
        Math.max(1, models.length) *
        repeats;
      if (!Number.isSafeInteger(cellCount) || cellCount > MAX_EXPERIMENT_CELLS) {
        return {
          ok: false,
          reason: `${type}: matrix expands to ${cellCount} cells; maximum is ${MAX_EXPERIMENT_CELLS}`,
        };
      }
      return {
        ok: true,
        message: {
          type,
          matrix: {
            id,
            name,
            kind: kind as WorkflowKind,
            targets,
            profiles,
            models,
            repeats,
            base: base.request,
            createdAt,
          },
        },
      };
    }

    case "startExperiment":
    case "stopExperiment":
    case "deleteExperiment":
    case "exportExperiment": {
      const matrixId = identifier(raw.matrixId);
      return matrixId === null ? reject("matrixId") : { ok: true, message: { type, matrixId } };
    }

    case "pickTarget": {
      const kind = str(raw.kind, 32);
      return kind !== null && KINDS.has(kind)
        ? { ok: true, message: { type, kind: kind as WorkflowKind } }
        : reject("kind");
    }

    case "openPath": {
      const value = str(raw.path, 4096);
      return value === null ? reject("path") : { ok: true, message: { type, path: value } };
    }

    case "openExternalDoc": {
      const topic = str(raw.topic, 128) ?? "";
      return { ok: true, message: { type, topic } };
    }

    default:
      return { ok: false, reason: `unknown message type "${type}"` };
  }
}
