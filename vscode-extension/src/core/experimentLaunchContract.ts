/** Freeze and verify prompt-safe per-cell workflow and knob evidence. */
import { createHash } from "node:crypto";
import * as path from "node:path";

import type { LaunchEnv } from "./launch";
import { canonicalExperimentTarget } from "./experimentMatrix";
import type {
  ExperimentLaunchContract,
  ExperimentSealedValueIdentity,
  LaunchPlanPreview,
  RunMetrics,
} from "./types";

const SHA256 = /^[0-9a-f]{64}$/;
const MAX_CONTRACT_KEYS = 1_000;
const MAX_ENVIRONMENT_KEY_LENGTH = 256;

const TARGET_ENV_KEY = "LEANFLOW_NATIVE_REQUESTED_TARGET";
const ADDITIONAL_SKILLS_ENV_KEY = "LEANFLOW_NATIVE_ADDITIONAL_SKILLS";
const PARALLEL_AGENTS_ENV_KEY = "LEANFLOW_NATIVE_PARALLEL_AGENTS";
const USER_APPROVED_SWARM_ENV_KEY = "LEANFLOW_NATIVE_USER_APPROVED_SWARM";
const RESEARCH_MODE_ENV_KEY = "LEANFLOW_RESEARCH_MODE";
const RESEARCH_WORKERS_ENV_KEY = "LEANFLOW_RESEARCH_WORKERS";
const CLEAN_ROOM_ENV_KEY = "LEANFLOW_DISABLE_SOLUTION_RESEARCH";
const CLEAN_ROOM_LABELS_ENV_KEY = "LEANFLOW_CLEAN_ROOM_TASK_LABELS";
const HUMAN_REVIEW_ENV_KEY = "LEANFLOW_HUMAN_REVIEW_ENABLED";
const ALLOWED_AXIOMS_ENV_KEY = "LEANFLOW_NATIVE_ALLOWED_AXIOMS";
const ACTIVE_SKILL_ENV_KEY = "LEANFLOW_NATIVE_ACTIVE_SKILL";
const TOOLSET_ENV_KEY = "LEANFLOW_NATIVE_TOOLSET";

const SEPARATELY_BOUND_ENV_KEYS = new Set([
  TARGET_ENV_KEY,
  ADDITIONAL_SKILLS_ENV_KEY,
  "LEANFLOW_PROJECT_ROOT",
  "LEANFLOW_WORKFLOW_RUN_ID",
  "LEANFLOW_NATIVE_ACTIVE_FILE",
  "LEANFLOW_NATIVE_WORKFLOW_COMMAND",
  "LEANFLOW_NATIVE_EXPLICIT_GOAL",
  "LEANFLOW_NATIVE_USER_PROMPT",
  "LEANFLOW_NATIVE_EFFECTIVE_PROMPT",
  "LEANFLOW_NATIVE_PROVIDER",
  "LEANFLOW_NATIVE_MODEL",
  "LEANFLOW_NATIVE_BASE_URL",
  "LEANFLOW_NATIVE_REASONING_EFFORT",
  "LEANFLOW_PLAN_MD",
  "LEANFLOW_BLUEPRINT_JSON",
  "LEANFLOW_PLAN_SUMMARY_JSON",
]);

function sensitiveEnvironmentKey(key: string): boolean {
  const normalized = key.toUpperCase();
  return (
    normalized.endsWith("_API_KEY") ||
    normalized.endsWith("_TOKEN") ||
    normalized.endsWith("_PASSWORD") ||
    normalized.endsWith("_SECRET") ||
    normalized.endsWith("_CREDENTIAL") ||
    normalized.endsWith("_COMMAND_TEMPLATE")
  );
}

function stablePreviewEnvironmentKey(key: string): boolean {
  return Boolean(
    key.startsWith("LEANFLOW_") &&
      key.length <= MAX_ENVIRONMENT_KEY_LENGTH &&
      !SEPARATELY_BOUND_ENV_KEYS.has(key) &&
      !key.startsWith("LEANFLOW_FORMALIZATION_") &&
      !sensitiveEnvironmentKey(key),
  );
}

function valueIdentity(value: string): ExperimentSealedValueIdentity {
  return {
    sha256: createHash("sha256").update(value, "utf8").digest("hex"),
    chars: Array.from(value).length,
  };
}

function identityMatches(value: string, expected: ExperimentSealedValueIdentity): boolean {
  const actual = valueIdentity(value);
  return actual.sha256 === expected.sha256 && actual.chars === expected.chars;
}

function own(values: Readonly<Record<string, string>>, key: string): boolean {
  return Object.hasOwn(values, key);
}

function sortedUnique(values: Iterable<string>): string[] {
  return [...new Set(values)].sort((left, right) => left.localeCompare(right));
}

/** Freeze the effective dry-run workflow and child knob values without retaining their text. */
export function freezeExperimentLaunchContract(
  preview: LaunchPlanPreview,
  launchEnv: LaunchEnv,
): ExperimentLaunchContract {
  const expected = new Map<string, ExperimentSealedValueIdentity>();
  const effectiveValues = preview.env_effective;
  if (
    effectiveValues === null ||
    typeof effectiveValues !== "object" ||
    Array.isArray(effectiveValues)
  ) {
    throw new Error(
      "The CLI preview lacks the complete effective child environment required for research sweeps.",
    );
  }
  const put = (key: string, value: string): void => {
    if (stablePreviewEnvironmentKey(key)) {
      expected.set(key, valueIdentity(value));
    }
  };
  const force = (key: string, fallback: string): void => {
    put(key, own(effectiveValues, key) ? effectiveValues[key] : fallback);
  };

  // Capture inherited and CLI-derived values from the complete safe child
  // environment. Clone paths, secrets, prompt, target, and provider fields
  // have their own stronger binding contracts and are excluded here.
  for (const [key, value] of Object.entries(effectiveValues)) {
    put(key, value);
  }
  for (const [key, rawValue] of Object.entries(launchEnv.set)) {
    if (!stablePreviewEnvironmentKey(key)) {
      continue;
    }
    if (!own(effectiveValues, key)) {
      throw new Error(`The CLI preview dropped the requested experiment knob ${key}.`);
    }
    put(key, effectiveValues[key] ?? rawValue);
  }

  force(PARALLEL_AGENTS_ENV_KEY, String(preview.workflow.parallel_agents));
  force(
    USER_APPROVED_SWARM_ENV_KEY,
    preview.workflow.parallel_agents > 1 ? "1" : "0",
  );
  force(HUMAN_REVIEW_ENV_KEY, preview.workflow.human_review ? "1" : "0");
  force(ACTIVE_SKILL_ENV_KEY, preview.active_skill);
  force(TOOLSET_ENV_KEY, preview.toolset);

  const falseOrAbsent = new Set<string>();
  if (preview.workflow.research_mode) {
    force(RESEARCH_MODE_ENV_KEY, "1");
    force(RESEARCH_WORKERS_ENV_KEY, String(preview.workflow.research_workers));
  } else {
    expected.delete(RESEARCH_MODE_ENV_KEY);
    expected.delete(RESEARCH_WORKERS_ENV_KEY);
    falseOrAbsent.add(RESEARCH_MODE_ENV_KEY);
  }
  if (preview.workflow.clean_room) {
    force(CLEAN_ROOM_ENV_KEY, "1");
    force(CLEAN_ROOM_LABELS_ENV_KEY, preview.workflow.clean_room_labels.join("|"));
  } else {
    expected.delete(CLEAN_ROOM_ENV_KEY);
    expected.delete(CLEAN_ROOM_LABELS_ENV_KEY);
    falseOrAbsent.add(CLEAN_ROOM_ENV_KEY);
  }

  const emptyOrAbsent = new Set<string>();
  if (preview.workflow.allowed_axioms) {
    force(ALLOWED_AXIOMS_ENV_KEY, preview.workflow.allowed_axioms);
  } else {
    expected.delete(ALLOWED_AXIOMS_ENV_KEY);
    emptyOrAbsent.add(ALLOWED_AXIOMS_ENV_KEY);
  }

  const unset = new Set(
    launchEnv.unset.filter((key) => stablePreviewEnvironmentKey(key) && !expected.has(key)),
  );
  for (const key of falseOrAbsent) {
    unset.delete(key);
  }
  for (const key of emptyOrAbsent) {
    unset.delete(key);
  }
  const set = Object.fromEntries(
    [...expected.entries()].sort(([left], [right]) => left.localeCompare(right)),
  );
  return {
    version: 1,
    workflow: {
      kind: preview.workflow.kind,
      parallelAgents: preview.workflow.parallel_agents,
      researchMode: preview.workflow.research_mode,
      researchWorkers: preview.workflow.research_workers,
      cleanRoom: preview.workflow.clean_room,
      humanReview: preview.workflow.human_review,
    },
    set,
    unset: sortedUnique(unset),
    falseOrAbsent: sortedUnique(falseOrAbsent),
    emptyOrAbsent: sortedUnique(emptyOrAbsent),
    additionalSkills: preview.additional_skills.map(valueIdentity),
  };
}

function isSealedValueIdentity(value: unknown): value is ExperimentSealedValueIdentity {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<ExperimentSealedValueIdentity>;
  return Boolean(
    typeof candidate.sha256 === "string" &&
      SHA256.test(candidate.sha256) &&
      Number.isSafeInteger(candidate.chars) &&
      (candidate.chars ?? -1) >= 0,
  );
}

/** Return whether restored digest-only launch evidence is canonical and bounded. */
export function isExperimentLaunchContract(value: unknown): value is ExperimentLaunchContract {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<ExperimentLaunchContract>;
  const workflow = candidate.workflow;
  const set = candidate.set;
  if (
    candidate.version !== 1 ||
    workflow === null ||
    typeof workflow !== "object" ||
    typeof workflow.kind !== "string" ||
    !workflow.kind ||
    workflow.kind.length > 64 ||
    !Number.isSafeInteger(workflow.parallelAgents) ||
    workflow.parallelAgents < 1 ||
    typeof workflow.researchMode !== "boolean" ||
    !Number.isSafeInteger(workflow.researchWorkers) ||
    workflow.researchWorkers < 0 ||
    typeof workflow.cleanRoom !== "boolean" ||
    typeof workflow.humanReview !== "boolean" ||
    set === null ||
    typeof set !== "object" ||
    Array.isArray(set)
  ) {
    return false;
  }
  const entries = Object.entries(set);
  if (
    entries.length > MAX_CONTRACT_KEYS ||
    entries.some(
      ([key, identity]) =>
        !stablePreviewEnvironmentKey(key) || !isSealedValueIdentity(identity),
    ) ||
    entries.map(([key]) => key).join("\0") !==
      entries.map(([key]) => key).sort((left, right) => left.localeCompare(right)).join("\0")
  ) {
    return false;
  }
  const arrays = [candidate.unset, candidate.falseOrAbsent, candidate.emptyOrAbsent];
  if (
    arrays.some(
      (items) =>
        !Array.isArray(items) ||
        items.length > MAX_CONTRACT_KEYS ||
        items.some((key) => typeof key !== "string" || !stablePreviewEnvironmentKey(key)) ||
        items.join("\0") !== sortedUnique(items).join("\0"),
    ) ||
    !Array.isArray(candidate.additionalSkills) ||
    candidate.additionalSkills.length > MAX_CONTRACT_KEYS ||
    candidate.additionalSkills.some((identity) => !isSealedValueIdentity(identity))
  ) {
    return false;
  }
  const categories = [
    new Set(Object.keys(set)),
    new Set(candidate.unset),
    new Set(candidate.falseOrAbsent),
    new Set(candidate.emptyOrAbsent),
  ];
  const seen = new Set<string>();
  for (const category of categories) {
    for (const key of category) {
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
    }
  }
  return true;
}

function canonicalFalse(value: string): boolean {
  return ["", "0", "false", "no", "off"].includes(value.trim().toLowerCase());
}

/** Bind one cell's requested source target to both sealed launch projections. */
export function runMetricsMatchRequestedTarget(
  metrics: RunMetrics,
  expectedTarget: string,
): boolean {
  const expected = canonicalExperimentTarget(expectedTarget);
  const values = metrics.launch?.environment.values;
  const runtime = metrics.launch?.environment.runtime;
  return Boolean(
    values &&
      runtime &&
      own(values, TARGET_ENV_KEY) &&
      values[TARGET_ENV_KEY] === expected &&
      runtime.requested_target === expected,
  );
}

/** Verify the frozen per-cell workflow and knob contract against sealed child evidence. */
export function runMetricsMatchLaunchContract(
  metrics: RunMetrics,
  contract: ExperimentLaunchContract,
): boolean {
  if (!isExperimentLaunchContract(contract)) {
    return false;
  }
  const values = metrics.launch?.environment.values;
  if (!values) {
    return false;
  }
  for (const [key, identity] of Object.entries(contract.set)) {
    if (!own(values, key) || !identityMatches(values[key], identity)) {
      return false;
    }
  }
  if (contract.unset.some((key) => own(values, key))) {
    return false;
  }
  if (
    contract.falseOrAbsent.some(
      (key) => own(values, key) && !canonicalFalse(values[key]),
    ) ||
    contract.emptyOrAbsent.some((key) => own(values, key) && values[key] !== "")
  ) {
    return false;
  }
  if (!own(values, ADDITIONAL_SKILLS_ENV_KEY)) {
    return false;
  }
  const actualSkills = values[ADDITIONAL_SKILLS_ENV_KEY]
    ? values[ADDITIONAL_SKILLS_ENV_KEY].split(path.delimiter)
    : [];
  if (
    !contract.additionalSkills.every(
      (identity, index) =>
        index < actualSkills.length && identityMatches(actualSkills[index], identity),
    )
  ) {
    return false;
  }
  if (actualSkills.length === contract.additionalSkills.length) {
    return true;
  }
  if (
    contract.workflow.kind !== "formalize" ||
    actualSkills.length !== contract.additionalSkills.length + 1
  ) {
    return false;
  }
  const generatedSkill = values.LEANFLOW_FORMALIZATION_BLUEPRINT_SKILL;
  return Boolean(
    generatedSkill && generatedSkill === actualSkills[actualSkills.length - 1],
  );
}
