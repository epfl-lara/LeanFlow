/** Convert durable CLI aggregates into honest experiment measurements. */
import type {
  ExperimentMetrics,
  ExperimentLaunchInvariant,
  ExperimentRuntimeIdentity,
  Provenance,
  RunMetrics,
} from "./types";
import { canonicalExperimentTarget } from "./experimentMatrix";

/** Decide whether a cell shares the first measured cell's launch source bytes. */
export function compareLaunchSourceIdentity(
  expected: string | null,
  actual: string,
): { accepted: boolean; expected: string } {
  if (expected === null) {
    return { accepted: true, expected: actual };
  }
  return { accepted: expected === actual, expected };
}

const SHA256 = /^[0-9a-f]{64}$/;

/** Project the provenance components no experiment axis may change. */
export function experimentLaunchInvariant(
  provenance: Provenance,
): ExperimentLaunchInvariant | null {
  const invariant: ExperimentLaunchInvariant = {
    gitCommit: provenance.git_commit,
    gitSubmodulesSha256: provenance.git_submodules_sha256,
    sourceTreeSha256: provenance.source_tree_sha256,
    projectManifestSha256: provenance.project_manifest_sha256,
    workflowGuidanceSha256: provenance.workflow_guidance_sha256,
    runtimeContentSha256: provenance.runtime_content_sha256,
    runtimeSourceSha256: provenance.runtime_source_sha256,
    pythonRuntimeSha256: provenance.python_runtime_sha256,
    behaviorConfigSha256: provenance.behavior_config_sha256,
    dependencyManifestSha256: provenance.dependency_manifest_sha256,
    leanToolchain: provenance.lean_toolchain,
  };
  if (
    !invariant.gitCommit ||
    !invariant.leanToolchain ||
    ![
      invariant.gitSubmodulesSha256,
      invariant.sourceTreeSha256,
      invariant.projectManifestSha256,
      invariant.workflowGuidanceSha256,
      invariant.runtimeContentSha256,
      invariant.runtimeSourceSha256,
      invariant.pythonRuntimeSha256,
      invariant.behaviorConfigSha256,
      invariant.dependencyManifestSha256,
    ].every((value) => SHA256.test(value))
  ) {
    return null;
  }
  return invariant;
}

/** Return whether persisted global launch components are complete and canonical. */
export function isExperimentLaunchInvariant(
  value: unknown,
): value is ExperimentLaunchInvariant {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const candidate = value as ExperimentLaunchInvariant;
  return Boolean(
    typeof candidate.gitCommit === "string" &&
      typeof candidate.leanToolchain === "string" &&
      candidate.gitCommit.length > 0 &&
      candidate.gitCommit.length <= 256 &&
      candidate.leanToolchain.length > 0 &&
      candidate.leanToolchain.length <= 1_024 &&
      [
        candidate.gitSubmodulesSha256,
        candidate.sourceTreeSha256,
        candidate.projectManifestSha256,
        candidate.workflowGuidanceSha256,
        candidate.runtimeContentSha256,
        candidate.runtimeSourceSha256,
        candidate.pythonRuntimeSha256,
        candidate.behaviorConfigSha256,
        candidate.dependencyManifestSha256,
      ].every((field) => typeof field === "string" && SHA256.test(field)),
  );
}

/** Compare global provenance components without exempting intentional target skills. */
export function sameExperimentLaunchInvariant(
  expected: ExperimentLaunchInvariant,
  actual: ExperimentLaunchInvariant,
): boolean {
  return (Object.keys(expected) as (keyof ExperimentLaunchInvariant)[]).every(
    (key) => expected[key] === actual[key],
  );
}

/** Stable map key for selected-skill content that is legitimately target-specific. */
export function selectedSkillsTargetKey(target: string): string {
  return JSON.stringify([canonicalExperimentTarget(target)]);
}

/** Freeze one target's skill identity or reject drift within that target. */
export function compareTargetSelectedSkills(
  expectedByTarget: Readonly<Record<string, string>>,
  target: string,
  actual: string,
): { accepted: boolean; expected: string; key: string } {
  const key = selectedSkillsTargetKey(target);
  const expected = expectedByTarget[key];
  if (!SHA256.test(actual)) {
    return { accepted: false, expected: "", key };
  }
  return {
    accepted: expected === undefined || expected === actual,
    expected: expected ?? actual,
    key,
  };
}

/** Omit any tracked command that could contain the frozen prompt's raw text. */
export function experimentCommandForExport(
  trackedCommand: string | null | undefined,
  promptPresent: boolean,
): string | null {
  if (!trackedCommand || promptPresent) {
    return null;
  }
  return trackedCommand;
}

function normalizeProviderSelector(value: string): string {
  return value.trim().toLowerCase().replace(/ /g, "-");
}

const CREDENTIAL_QUERY_PARTS = new Set([
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
]);

function credentialQueryKey(key: string): boolean {
  const parts = key.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  const compact = parts.join("");
  return parts.some(
    (part) =>
      CREDENTIAL_QUERY_PARTS.has(part) ||
      [...CREDENTIAL_QUERY_PARTS].some((credential) => compact.includes(credential)),
  );
}

/** Return a credential-free stable identity for an inference endpoint. */
export function canonicalRuntimeBaseUrl(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    throw new Error("The resolved runtime has no base URL.");
  }
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    throw new Error("The resolved runtime base URL is invalid.");
  }
  if (!parsed.protocol || !parsed.hostname) {
    throw new Error("The resolved runtime base URL is incomplete.");
  }
  parsed.username = "";
  parsed.password = "";
  for (const key of [...parsed.searchParams.keys()]) {
    if (credentialQueryKey(key)) {
      parsed.searchParams.set(key, "[redacted]");
    }
  }
  parsed.searchParams.sort();
  // URL fragments are not sent to the inference service and can carry tokens.
  parsed.hash = "";
  return parsed.toString();
}

/** Convert a dry-run runtime into the exact identity a sweep will later require. */
export function freezeExperimentRuntimeIdentity(
  runtime: Record<string, string>,
  requestedProvider: string,
): ExperimentRuntimeIdentity {
  const requested = normalizeProviderSelector(requestedProvider);
  const previewRequested = normalizeProviderSelector(runtime.requested_provider ?? "");
  const provider = normalizeProviderSelector(runtime.provider ?? "");
  const model = String(runtime.model ?? "").trim();
  if (!requested || previewRequested !== requested) {
    throw new Error(
      "The CLI preview did not bind the requested provider selector exactly.",
    );
  }
  if (!provider || !model) {
    throw new Error("The CLI preview did not resolve an explicit provider and model.");
  }
  return {
    requestedProvider: requested,
    provider,
    model,
    baseUrl: canonicalRuntimeBaseUrl(String(runtime.base_url ?? "")),
    reasoningEffort: String(runtime.reasoning_effort ?? "").trim(),
  };
}

/** Return whether persisted preview identity remains canonical and credential-free. */
export function isExperimentRuntimeIdentity(
  value: unknown,
): value is ExperimentRuntimeIdentity {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<ExperimentRuntimeIdentity>;
  if (
    typeof candidate.requestedProvider !== "string" ||
    typeof candidate.provider !== "string" ||
    typeof candidate.model !== "string" ||
    typeof candidate.baseUrl !== "string" ||
    typeof candidate.reasoningEffort !== "string" ||
    !candidate.requestedProvider ||
    !candidate.provider ||
    !candidate.model ||
    candidate.requestedProvider.length > 256 ||
    candidate.provider.length > 256 ||
    candidate.model.length > 1_024 ||
    candidate.baseUrl.length > 4_096 ||
    candidate.reasoningEffort.length > 256 ||
    normalizeProviderSelector(candidate.requestedProvider) !== candidate.requestedProvider ||
    normalizeProviderSelector(candidate.provider) !== candidate.provider ||
    candidate.model.trim() !== candidate.model ||
    candidate.reasoningEffort.trim() !== candidate.reasoningEffort
  ) {
    return false;
  }
  try {
    return canonicalRuntimeBaseUrl(candidate.baseUrl) === candidate.baseUrl;
  } catch {
    return false;
  }
}

/** Bind a preview-frozen runtime to both sealed launch and final evidence. */
export function runMetricsMatchExplicitRuntime(
  metrics: RunMetrics,
  expected: ExperimentRuntimeIdentity,
): boolean {
  const runtime = metrics.launch?.environment.runtime;
  let baseUrl = "";
  try {
    baseUrl = canonicalRuntimeBaseUrl(runtime?.base_url ?? "");
  } catch {
    return false;
  }
  return Boolean(
    expected.model &&
      expected.provider &&
      runtime?.model === expected.model &&
      runtime.provider === expected.provider &&
      baseUrl === expected.baseUrl &&
      runtime.reasoning_effort === expected.reasoningEffort &&
      metrics.outcome.model === expected.model &&
      metrics.outcome.provider === expected.provider,
  );
}

const PROMPT_CONTENT_IDENTITY = /^\[content-sha256:([0-9a-f]{64});chars:([0-9]+)\]$/;
const EFFECTIVE_PROMPT_ENV_KEYS = [
  "LEANFLOW_NATIVE_EXPLICIT_GOAL",
  "LEANFLOW_NATIVE_USER_PROMPT",
  "LEANFLOW_NATIVE_EFFECTIVE_PROMPT",
] as const;

function parsePromptContentIdentity(
  value: string,
): { sha256: string; chars: number } | null {
  const match = PROMPT_CONTENT_IDENTITY.exec(value);
  if (!match) {
    return null;
  }
  const chars = Number(match[2]);
  if (!Number.isSafeInteger(chars) || chars < 0 || String(chars) !== match[2]) {
    return null;
  }
  return { sha256: match[1], chars };
}

/** Bind the encrypted experiment prompt to every sealed effective-prompt alias. */
export function runMetricsMatchExplicitPrompt(
  metrics: RunMetrics,
  expected: {
    promptPresent: boolean;
    promptSha256: string | null;
    promptChars: number;
  },
): boolean {
  const values = metrics.launch?.environment.values;
  if (
    values == null ||
    !Number.isSafeInteger(expected.promptChars) ||
    expected.promptChars < 0 ||
    (expected.promptPresent
      ? expected.promptSha256 == null || !SHA256.test(expected.promptSha256)
      : expected.promptSha256 !== null || expected.promptChars !== 0)
  ) {
    return false;
  }
  if (!expected.promptPresent) {
    return EFFECTIVE_PROMPT_ENV_KEYS.every(
      (key) => Object.hasOwn(values, key) && values[key] === "",
    );
  }
  return EFFECTIVE_PROMPT_ENV_KEYS.every((key) => {
    if (!Object.hasOwn(values, key)) {
      return false;
    }
    const identity = parsePromptContentIdentity(values[key]);
    return (
      identity !== null &&
      identity.sha256 === expected.promptSha256 &&
      identity.chars === expected.promptChars
    );
  });
}

/** Return an explicit missing measurement without inventing zeros or failures. */
export function unscoredMetrics(reason = "Durable exact metrics are unavailable."): ExperimentMetrics {
  return {
    scopeExact: false,
    solved: null,
    sorryCount: null,
    projectSorryCount: null,
    durationSeconds: null,
    apiCalls: null,
    inputTokens: null,
    outputTokens: null,
    costUsd: null,
    phase: "unscored",
    toolCalls: null,
    verificationFailures: null,
    countsAreComplete: false,
    usageComplete: false,
    apiCallsComplete: false,
    tokensComplete: false,
    costComplete: false,
    usageSource: "unavailable",
    declarationsTotal: null,
    declarationsProved: null,
    failures: {},
    missingReason: reason,
    provenance: null,
    provenanceFinal: null,
    sourceChangedDuringRun: null,
  };
}

function nonNegativeNumber(value: number | null, complete = true): number | null {
  return complete && typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function provenanceIdentity(
  provenance:
    | NonNullable<RunMetrics["provenance"]>
    | NonNullable<ExperimentMetrics["provenance"]>,
): NonNullable<ExperimentMetrics["provenance"]> {
  return {
    provenance_complete: provenance.provenance_complete,
    git_commit: provenance.git_commit,
    git_dirty: provenance.git_dirty,
    git_status_sha256: provenance.git_status_sha256,
    git_diff_sha256: provenance.git_diff_sha256,
    git_untracked_index_sha256: provenance.git_untracked_index_sha256,
    source_tree_complete: provenance.source_tree_complete,
    source_tree_sha256: provenance.source_tree_sha256,
    source_identity_sha256: provenance.source_identity_sha256,
    lean_toolchain: provenance.lean_toolchain,
  };
}

function compactFailures(value: Record<string, number>): Record<string, number> {
  return Object.fromEntries(
    Object.entries(value)
      .filter(
        ([key, count]) =>
          key.length <= 128 && Number.isSafeInteger(count) && count >= 0,
      )
      .slice(0, 64),
  );
}

/** Project a persisted/UI metric onto the bounded summary used by the editor. */
export function compactExperimentMetrics(metrics: ExperimentMetrics): ExperimentMetrics {
  if (
    metrics.scopeExact !== true ||
    typeof metrics.phase !== "string" ||
    metrics.phase === "unscored" ||
    metrics.provenance === null ||
    metrics.provenanceFinal === null ||
    metrics.provenance.provenance_complete !== true ||
    metrics.provenanceFinal.provenance_complete !== true ||
    !metrics.provenance.source_identity_sha256 ||
    !metrics.provenanceFinal.source_identity_sha256 ||
    metrics.countsAreComplete !== true ||
    typeof metrics.sourceChangedDuringRun !== "boolean"
  ) {
    return unscoredMetrics(
      typeof metrics.missingReason === "string" && metrics.missingReason
        ? metrics.missingReason.slice(0, 2_000)
        : "Persisted metrics predate exact immutable experiment scoring.",
    );
  }
  return {
    scopeExact: true,
    solved: typeof metrics.solved === "boolean" ? metrics.solved : null,
    sorryCount: nonNegativeNumber(metrics.sorryCount),
    projectSorryCount: nonNegativeNumber(metrics.projectSorryCount),
    durationSeconds: nonNegativeNumber(metrics.durationSeconds),
    apiCalls: nonNegativeNumber(metrics.apiCalls, metrics.apiCallsComplete === true),
    inputTokens: nonNegativeNumber(metrics.inputTokens, metrics.tokensComplete === true),
    outputTokens: nonNegativeNumber(metrics.outputTokens, metrics.tokensComplete === true),
    costUsd: nonNegativeNumber(metrics.costUsd, metrics.costComplete === true),
    phase: typeof metrics.phase === "string" ? metrics.phase.slice(0, 128) : "unscored",
    toolCalls: nonNegativeNumber(metrics.toolCalls, metrics.countsAreComplete === true),
    verificationFailures: nonNegativeNumber(
      metrics.verificationFailures,
      metrics.countsAreComplete === true,
    ),
    countsAreComplete: metrics.countsAreComplete === true,
    usageComplete: metrics.usageComplete === true,
    apiCallsComplete: metrics.apiCallsComplete === true,
    tokensComplete: metrics.tokensComplete === true,
    costComplete: metrics.costComplete === true,
    usageSource:
      typeof metrics.usageSource === "string"
        ? metrics.usageSource.slice(0, 128)
        : "unavailable",
    declarationsTotal: nonNegativeNumber(metrics.declarationsTotal),
    declarationsProved: nonNegativeNumber(metrics.declarationsProved),
    failures: compactFailures(metrics.failures ?? {}),
    missingReason: "",
    provenance: provenanceIdentity(metrics.provenance),
    provenanceFinal: provenanceIdentity(metrics.provenanceFinal),
    sourceChangedDuringRun:
      typeof metrics.sourceChangedDuringRun === "boolean"
        ? metrics.sourceChangedDuringRun
        : null,
  };
}

/** Build one measured cell from a run-scoped CLI aggregate. */
export function metricsFromRun(
  metrics: RunMetrics,
): ExperimentMetrics {
  if (
    metrics.version !== 2 ||
    metrics.scope.exact !== true ||
    metrics.scope.final_snapshot !== "verified" ||
    metrics.events.complete !== true ||
    metrics.launch === null ||
    metrics.provenance == null ||
    metrics.provenance_final == null ||
    metrics.provenance.provenance_complete !== true ||
    metrics.provenance_final.provenance_complete !== true ||
    !metrics.provenance.source_identity_sha256 ||
    !metrics.provenance_final.source_identity_sha256 ||
    typeof metrics.source_changed_during_run !== "boolean" ||
    metrics.runtime_source_changed !== false ||
    metrics.selected_skills_changed !== false ||
    metrics.behavior_config_changed !== false ||
    metrics.project_configuration_changed !== false ||
    metrics.python_runtime_changed !== false ||
    typeof metrics.outcome.phase !== "string" ||
    !Number.isInteger(metrics.outcome.exit_code) ||
    typeof metrics.outcome.terminal_status !== "string"
  ) {
    return unscoredMetrics("The run lacks verified immutable final evidence.");
  }
  const failures = metrics.failures ?? {};
  return {
    scopeExact: true,
    // Non-proof workflows have no proof verdict. Their exact duration and
    // usage remain useful measurements, while solve-rate reports them missing.
    solved:
      typeof metrics.outcome.proof_solved === "boolean"
        ? metrics.outcome.proof_solved
        : null,
    sorryCount: nonNegativeNumber(metrics.outcome.sorry_count),
    projectSorryCount: nonNegativeNumber(metrics.outcome.project_sorry_count),
    // Extension timestamps are operational metadata, not a paper measurement.
    // If the exact run stream cannot establish duration, keep it missing.
    durationSeconds: nonNegativeNumber(metrics.run.duration_s),
    apiCalls: nonNegativeNumber(metrics.usage.api_calls, metrics.usage.api_calls_complete),
    inputTokens: nonNegativeNumber(metrics.usage.input_tokens, metrics.usage.tokens_complete),
    outputTokens: nonNegativeNumber(metrics.usage.output_tokens, metrics.usage.tokens_complete),
    costUsd: nonNegativeNumber(metrics.usage.cost_usd, metrics.usage.cost_complete),
    phase: metrics.outcome.phase,
    toolCalls: metrics.activity.tool_calls,
    verificationFailures:
      (failures["verification-rejected"] ?? 0) +
      (failures["proof-attempt-rejected"] ?? 0),
    countsAreComplete: metrics.events.complete,
    usageComplete: metrics.usage.complete,
    apiCallsComplete: metrics.usage.api_calls_complete,
    tokensComplete: metrics.usage.tokens_complete,
    costComplete: metrics.usage.cost_complete,
    usageSource: metrics.usage.source,
    declarationsTotal: nonNegativeNumber(metrics.outcome.declarations_total),
    declarationsProved: nonNegativeNumber(metrics.outcome.declarations_proved),
    failures: compactFailures(failures),
    missingReason: "",
    provenance: provenanceIdentity(metrics.provenance),
    provenanceFinal: provenanceIdentity(metrics.provenance_final),
    sourceChangedDuringRun: metrics.source_changed_during_run,
  };
}
