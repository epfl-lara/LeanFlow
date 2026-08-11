/**
 * Translate a launcher form into the exact argv and environment a run receives.
 *
 * This is the reproducibility seam: the argv plus the knob environment fully
 * determine a run, so both are recorded with every tracked run and every
 * experiment cell. Nothing else may reach the child process.
 */
import type {
  FlagCatalog,
  FlagProfile,
  FlagSpec,
  LaunchRequest,
  ProfileCatalog,
} from "./types";
import { LAUNCH_FIELD_LIMITS } from "./types";

/** Build the argv that follows `leanflow workflow`. */
export function buildWorkflowArgs(request: LaunchRequest): string[] {
  const args: string[] = [request.kind];

  const target = request.target.trim();
  if (target) {
    // A target beginning with `-` would be parsed as a workflow option rather
    // than a file, silently changing what the run does.
    if (target.startsWith("-")) {
      throw new Error(`Invalid target ${target}: a target may not begin with "-".`);
    }
    args.push(target);
  }
  if (request.provider.trim()) {
    args.push("--provider", request.provider.trim());
  }
  if (request.model.trim()) {
    args.push("--model", request.model.trim());
  }
  if (request.noParallel) {
    args.push("--no-parallel");
  } else if (request.agents > 1) {
    args.push("--agents", String(request.agents));
  }
  if (request.research) {
    // --research-workers implies --research, but passing both keeps the
    // recorded command self-describing when the worker count is default.
    args.push("--research");
    if (request.researchWorkers !== null) {
      args.push("--research-workers", String(request.researchWorkers));
    }
  }
  if (request.cleanRoom) {
    args.push("--clean-room");
  }
  if (request.humanReview) {
    args.push("--human-review");
  }
  if (request.axioms.trim()) {
    args.push("--axioms", request.axioms.trim());
  }
  for (const skill of request.additionalSkills) {
    if (skill.trim()) {
      args.push("--additional-skill", skill.trim());
    }
  }
  // --prompt consumes the rest of the command line, so it must come last.
  if (request.prompt.trim()) {
    args.push("--prompt", request.prompt.trim());
  }
  return args;
}

/** The knob environment a launch applies, plus the knobs it clears. */
export interface LaunchEnv {
  /** Variables to set on the child. */
  set: Record<string, string>;
  /**
   * Variables to remove from the inherited environment.
   *
   * Dropping a key from `set` is not enough: the child inherits `process.env`,
   * so an ambient value would survive a knob the user explicitly cleared.
   */
  unset: string[];
}

/** Values above this length are rejected rather than passed to a child process. */
const MAX_ENV_VALUE_LENGTH = 4096;

/**
 * Compatibility fence for a CLI catalog predating `extension_editable`.
 *
 * The catalog metadata is authoritative for current CLIs. Keeping the known
 * set here means updating the extension before the CLI cannot reopen a secret,
 * arbitrary-write, approval, supply-chain, or benchmark-identity boundary.
 */
const LEGACY_EXTENSION_DENIED_KNOBS = new Set([
  "LEANFLOW_PLAN_STATE_DIR",
  "LEANFLOW_CLEAN_ROOM_TASK_LABELS",
  "LEANFLOW_REDACT_SECRETS",
  "LEANFLOW_DUMP_REQUESTS",
  "LEANFLOW_DUMP_REQUEST_STDOUT",
  "LEANFLOW_YOLO_MODE",
  "LEANFLOW_HOME",
  "LEANFLOW_SANDBOX_BASE_IMAGE",
]);

/**
 * Resolve the knob environment for a launch.
 *
 * Layering matches `leanflow flags effective`: profile first, then per-launch
 * overrides, which are the more specific statement of intent.
 *
 * Only names present in the declared catalog are admitted. The overrides reach
 * here from the webview and from profile files on disk, and they are merged
 * into a child process environment — without an allowlist, a name like
 * `PYTHONPATH` or `LD_PRELOAD` would let workspace content execute in the
 * extension host's security context. Everything rejected is reported so the UI
 * can say what was dropped instead of silently running a different
 * configuration.
 */
export function resolveLaunchEnv(
  request: LaunchRequest,
  profiles: ProfileCatalog | null,
  catalog: FlagCatalog | null,
): { env: LaunchEnv; rejected: string[] } {
  const merged: Record<string, string> = {};
  const rejected: string[] = [];
  const specs = extensionKnobSpecs(catalog);

  const admit = (name: string, value: string, allowUnset: boolean): void => {
    const spec = specs.get(name);
    if (spec === undefined || extensionKnobValueProblem(spec, value, allowUnset) !== null) {
      rejected.push(name);
      return;
    }
    merged[name] = value;
  };

  const profile = findProfile(profiles, request.profile);
  if (request.profile && profile === undefined) {
    throw new Error(`Unknown knob profile "${request.profile}".`);
  }
  if (profile) {
    for (const [name, value] of Object.entries(profile.overrides)) {
      admit(name, value, false);
    }
  }

  const unset: string[] = [];
  for (const [name, value] of Object.entries(request.overrides ?? {})) {
    const spec = specs.get(name);
    if (spec === undefined || extensionKnobValueProblem(spec, value, true) !== null) {
      rejected.push(name);
      continue;
    }
    if (value === "") {
      delete merged[name];
      unset.push(name);
      continue;
    }
    admit(name, value, true);
  }

  return { env: { set: merged, unset }, rejected: [...new Set(rejected)] };
}

/** Return a validation problem for one extension-supplied raw knob value. */
export function extensionKnobValueProblem(
  flag: FlagSpec,
  value: unknown,
  allowUnset = false,
): string | null {
  if (typeof value !== "string") {
    return "value must be a string";
  }
  if (value.length > MAX_ENV_VALUE_LENGTH) {
    return `value exceeds ${MAX_ENV_VALUE_LENGTH} characters`;
  }
  if (value.includes("\0")) {
    return "value contains a NUL character";
  }
  if (allowUnset && value === "") {
    return null;
  }

  const text = value.trim();
  switch (flag.value_type) {
    case "bool":
      return new Set(["", "0", "1", "true", "false", "yes", "no", "on", "off"]).has(
        text.toLowerCase(),
      )
        ? null
        : "value is not a recognized boolean";
    case "int": {
      if (!/^[+-]?\d+$/.test(text)) {
        return "value is not an integer";
      }
      const parsed = Number(text);
      if (!Number.isSafeInteger(parsed)) {
        return "integer is outside the safe range";
      }
      return numericRangeProblem(flag, parsed);
    }
    case "float": {
      const parsed = Number(text);
      if (text === "" || !Number.isFinite(parsed)) {
        return "value is not a finite number";
      }
      return numericRangeProblem(flag, parsed);
    }
    case "enum":
      return flag.choices?.includes(text) ? null : "value is not a declared choice";
    case "string":
    case "path":
    case "csv":
      return null;
    default:
      return "catalog has an unknown value type";
  }
}

function numericRangeProblem(flag: FlagSpec, value: number): string | null {
  if (flag.minimum !== undefined && value < flag.minimum) {
    return `value is below the declared minimum ${flag.minimum}`;
  }
  if (flag.maximum !== undefined && value > flag.maximum) {
    return `value is above the declared maximum ${flag.maximum}`;
  }
  return null;
}

/** Return whether the extension may set one catalogued knob. */
export function isExtensionEditableKnob(
  flag: Pick<
    FlagCatalog["groups"][number]["flags"][number],
    "name" | "editable" | "extension_editable" | "sensitive"
  >,
): boolean {
  if (!flag.editable || flag.sensitive === true || LEGACY_EXTENSION_DENIED_KNOBS.has(flag.name)) {
    return false;
  }
  // An older CLI does not emit this field. Its known dangerous names are
  // fenced above; ordinary editable knobs remain usable during a rolling
  // extension/CLI update.
  return flag.extension_editable !== false;
}

/** Every catalogued knob name that an extension launch may set. */
export function allowedKnobNames(catalog: FlagCatalog | null): ReadonlySet<string> {
  return new Set(extensionKnobSpecs(catalog).keys());
}

function extensionKnobSpecs(catalog: FlagCatalog | null): ReadonlyMap<string, FlagSpec> {
  const specs = new Map<string, FlagSpec>();
  for (const group of catalog?.groups ?? []) {
    for (const flag of group.flags) {
      // Launcher plumbing is written by the CLI itself; letting a caller set it
      // would let them redirect the project root or impersonate a run id.
      // Sensitive terminal-only knobs can leak raw requests, redirect writes,
      // or bypass safety/approval boundaries and are rejected at this seam too.
      if (isExtensionEditableKnob(flag)) {
        specs.set(flag.name, flag);
      }
    }
  }
  return specs;
}

/** Return profile keys that make the profile unavailable to the extension. */
export function rejectedProfileKnobNames(
  profile: FlagProfile | undefined,
  catalog: FlagCatalog | null,
  allowUnset = false,
): string[] {
  if (!profile) {
    return [];
  }
  const specs = extensionKnobSpecs(catalog);
  return Object.entries(profile.overrides)
    .filter(([name, value]) => {
      const spec = specs.get(name);
      return spec === undefined || extensionKnobValueProblem(spec, value, allowUnset) !== null;
    })
    .map(([name]) => name)
    .sort();
}

export function findProfile(
  profiles: ProfileCatalog | null,
  name: string,
): FlagProfile | undefined {
  if (!profiles || !name) {
    return undefined;
  }
  return profiles.profiles.find((profile) => profile.name === name);
}

const REDACTED_PROMPT_MARKER =
  /^<redacted:prompt(?: sha256=[a-f0-9]{64})? chars=\d+>$/;

/** Return whether text is already a bounded prompt-redaction marker. */
export function isRedactedPromptMarker(value: string): boolean {
  return REDACTED_PROMPT_MARKER.test(value.trim());
}

/** Return a browser-safe prompt marker for a command preview. */
export function promptDisplayMarker(value: string): string {
  const prompt = value.trim();
  if (!prompt || isRedactedPromptMarker(prompt)) {
    return prompt;
  }
  return `<redacted:prompt chars=${Array.from(prompt).length}>`;
}

/** Remove the private prompt from a launch request stored by the webview. */
export function launchRequestForViewState(request: LaunchRequest): LaunchRequest {
  const credentialText = (value: string): boolean =>
    /\b(?:Bearer\s+[A-Za-z0-9._~+/=-]{12,}|sk-(?:proj-)?[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b/i.test(
      value,
    ) ||
    /\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|credential|secret)\b\s*[:=]/i.test(
      value,
    );
  const safeText = (value: string): string => (credentialText(value) ? "" : value);
  const credentialField = (name: string): boolean => {
    const normalized = name.toLowerCase().replace(/[^a-z0-9]/g, "");
    return [
      "apikey",
      "accesstoken",
      "refreshtoken",
      "authorization",
      "password",
      "credential",
      "secret",
    ].some((suffix) => normalized.endsWith(suffix));
  };
  const overrides: Record<string, string> = {};
  for (const [name, value] of Object.entries(request.overrides)) {
    if (!credentialField(name) && !credentialText(value)) {
      overrides[name] = value;
    }
  }
  return {
    ...request,
    target: safeText(request.target),
    provider: safeText(request.provider),
    model: safeText(request.model),
    axioms: safeText(request.axioms),
    prompt: "",
    additionalSkills: request.additionalSkills.filter((value) => !credentialText(value)),
    profile: safeText(request.profile),
    overrides,
  };
}

/**
 * Redact a trailing `--prompt` value in an already-rendered command.
 *
 * LeanFlow deliberately requires `--prompt` to be last because it consumes the
 * rest of the command line. Replacing the tail therefore cannot hide another
 * option. This also scrubs commands retained by older extension versions.
 */
export function redactPromptArgument(command: string): string {
  const match = /(^|\s)--prompt(?:\s+|=)/.exec(command);
  if (!match || match.index === undefined) {
    return command;
  }
  const optionStart = match.index + match[1].length;
  return `${command.slice(0, optionStart)}--prompt ${promptDisplayMarker(
    command.slice(match.index + match[0].length),
  )}`;
}

/** A shell-ish, prompt-redacted rendering of the launch for display. */
export function describeCommand(request: LaunchRequest): string {
  const args = buildWorkflowArgs({
    ...request,
    prompt: promptDisplayMarker(request.prompt),
  }).map((arg) =>
    /[\s"']/.test(arg) ? JSON.stringify(arg) : arg,
  );
  return `leanflow workflow ${args.join(" ")}`;
}

/** A stable, readable label for a run or an experiment cell. */
export function describeLaunchLabel(request: LaunchRequest): string {
  const target = request.target.trim();
  const parts = [request.kind, target || "(project)"];
  if (request.research) {
    parts.push("research");
  }
  if (request.profile) {
    parts.push(request.profile);
  }
  if (request.model.trim()) {
    parts.push(request.model.trim());
  }
  return parts.join(" · ");
}

/**
 * Report fields the extension host would refuse for length.
 *
 * The host drops an oversized message wholesale, so without this the launcher
 * would post a request that can only fail. Reporting it as an ordinary form
 * problem keeps the limit visible on the fields a plain `maxLength` cannot
 * express — a newline-separated skill list, and the knob override map.
 */
function overlongFieldProblems(request: LaunchRequest): string[] {
  const problems: string[] = [];
  const check = (label: string, value: string, limit: number): void => {
    if (value.length > limit) {
      problems.push(
        `${label} is ${value.length.toLocaleString()} characters; the limit is ` +
          `${limit.toLocaleString()}.`,
      );
    }
  };
  check("The target", request.target, LAUNCH_FIELD_LIMITS.target);
  check("The provider", request.provider, LAUNCH_FIELD_LIMITS.provider);
  check("The model", request.model, LAUNCH_FIELD_LIMITS.model);
  check("The allowed-axiom list", request.axioms, LAUNCH_FIELD_LIMITS.axioms);
  check("The prompt", request.prompt, LAUNCH_FIELD_LIMITS.prompt);
  check("The profile name", request.profile, LAUNCH_FIELD_LIMITS.profile);

  if (request.additionalSkills.length > LAUNCH_FIELD_LIMITS.additionalSkillCount) {
    problems.push(
      `There are ${request.additionalSkills.length} additional skills; the limit is ` +
        `${LAUNCH_FIELD_LIMITS.additionalSkillCount}.`,
    );
  }
  const longSkill = request.additionalSkills.findIndex(
    (skill) => skill.length > LAUNCH_FIELD_LIMITS.additionalSkill,
  );
  if (longSkill >= 0) {
    check(
      `Additional skill ${longSkill + 1}`,
      request.additionalSkills[longSkill],
      LAUNCH_FIELD_LIMITS.additionalSkill,
    );
  }

  const overrides = Object.entries(request.overrides);
  if (overrides.length > LAUNCH_FIELD_LIMITS.overrideCount) {
    problems.push(
      `There are ${overrides.length} knob overrides; the limit is ` +
        `${LAUNCH_FIELD_LIMITS.overrideCount}.`,
    );
  }
  const longOverride = overrides.find(
    ([name, value]) =>
      name.length > LAUNCH_FIELD_LIMITS.overrideName ||
      value.length > LAUNCH_FIELD_LIMITS.overrideValue,
  );
  if (longOverride) {
    check(`Knob ${longOverride[0].slice(0, 64)}`, longOverride[1], LAUNCH_FIELD_LIMITS.overrideValue);
    check("That knob's name", longOverride[0], LAUNCH_FIELD_LIMITS.overrideName);
  }
  return problems;
}

/** Validation the form can show before the user commits to a run. */
export function validateLaunch(request: LaunchRequest): string[] {
  const problems: string[] = overlongFieldProblems(request);
  if (request.target.trim().startsWith("-")) {
    problems.push('A target may not begin with "-" because the CLI would parse it as an option.');
  }
  if (request.kind === "formalize" && !request.target.trim()) {
    problems.push("formalize needs a source document (.tex or .pdf) as its target.");
  }
  if (request.agents > 1 && request.target.trim() && request.kind === "prove") {
    problems.push(
      "A file-scoped prove run is single-agent; the launcher will reset agents to 1.",
    );
  }
  if (request.research && request.kind !== "prove") {
    problems.push("--research is only supported for prove.");
  }
  if (request.cleanRoom && request.kind !== "prove") {
    problems.push("--clean-room is only supported for prove.");
  }
  if (request.humanReview && request.kind !== "prove") {
    problems.push("--human-review is only supported for prove.");
  }
  if (request.researchWorkers !== null && request.researchWorkers < 0) {
    problems.push("Research workers cannot be negative.");
  }
  return problems;
}
