/**
 * Tests for launch argv/environment construction.
 *
 * This is the reproducibility seam: if the argv is wrong, a run does something
 * other than what the form said, and an ablation cell silently measures the
 * wrong thing. The matching Python-side test is
 * tests/leanflow/test_extension_launch_contract.py, which asserts the CLI
 * parses these exact argv lists into the intended workflow spec.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  allowedKnobNames,
  buildWorkflowArgs,
  describeCommand,
  isExtensionEditableKnob,
  launchRequestForViewState,
  redactPromptArgument,
  rejectedProfileKnobNames,
  resolveLaunchEnv,
  validateLaunch,
} from "../dist/test/launch.mjs";

// Stand-in catalog: only these names may reach a child process.
const CATALOG = {
  version: 1,
  count: 2,
  groups: [
    {
      name: "g",
      flags: [
        {
          name: "LEANFLOW_RESEARCH_MODE",
          editable: true,
          extension_editable: true,
          sensitive: false,
          value_type: "bool",
        },
        {
          name: "LEANFLOW_NEGATION_PROBE",
          editable: true,
          extension_editable: true,
          sensitive: false,
          value_type: "bool",
        },
        {
          name: "LEANFLOW_RESEARCH_WORKERS",
          editable: true,
          extension_editable: true,
          sensitive: false,
          value_type: "int",
          minimum: 0,
          maximum: 16,
        },
        {
          name: "LEANFLOW_CODEX_REASONING_EFFORT",
          editable: true,
          extension_editable: true,
          sensitive: false,
          value_type: "enum",
          choices: ["", "low", "medium", "high"],
        },
        {
          name: "LEANFLOW_REDACT_SECRETS",
          editable: true,
          extension_editable: false,
          sensitive: true,
          value_type: "bool",
        },
        {
          name: "LEANFLOW_FUTURE_TERMINAL_ONLY",
          editable: true,
          extension_editable: false,
          sensitive: false,
          value_type: "bool",
        },
        { name: "LEANFLOW_PROJECT_ROOT", editable: false, value_type: "path" },
      ],
    },
  ],
};
const ALLOWED = allowedKnobNames(CATALOG);

function form(overrides = {}) {
  return {
    kind: "prove",
    target: "",
    provider: "",
    model: "",
    agents: 1,
    research: false,
    researchWorkers: null,
    noParallel: false,
    cleanRoom: false,
    humanReview: false,
    axioms: "",
    prompt: "",
    additionalSkills: [],
    profile: "",
    overrides: {},
    ...overrides,
  };
}

test("a bare prove run passes only the workflow kind", () => {
  assert.deepEqual(buildWorkflowArgs(form()), ["prove"]);
});

test("a file-scoped run passes the target positionally", () => {
  assert.deepEqual(buildWorkflowArgs(form({ target: "IMO2026/P1.lean" })), [
    "prove",
    "IMO2026/P1.lean",
  ]);
});

test("provider and model become flags", () => {
  assert.deepEqual(
    buildWorkflowArgs(form({ provider: "codex", model: "gpt-5.6-terra" })),
    ["prove", "--provider", "codex", "--model", "gpt-5.6-terra"],
  );
});

test("research mode passes the worker count only when set", () => {
  assert.deepEqual(buildWorkflowArgs(form({ research: true })), ["prove", "--research"]);
  assert.deepEqual(buildWorkflowArgs(form({ research: true, researchWorkers: 3 })), [
    "prove",
    "--research",
    "--research-workers",
    "3",
  ]);
  assert.deepEqual(buildWorkflowArgs(form({ research: true, researchWorkers: 0 })), [
    "prove",
    "--research",
    "--research-workers",
    "0",
  ]);
});

test("no-parallel wins over an agent count", () => {
  const args = buildWorkflowArgs(form({ agents: 4, noParallel: true }));
  assert.deepEqual(args, ["prove", "--no-parallel"]);
  assert.ok(!args.includes("--agents"));
});

test("a single agent does not emit --agents", () => {
  assert.deepEqual(buildWorkflowArgs(form({ agents: 1 })), ["prove"]);
});

test("explicit bounded mode overrides stale legacy concurrency and research switches", () => {
  const request = form({
    agents: 4, research: true, researchWorkers: 3,
    overrides: { LEANFLOW_PROVER_MODE: "standard" },
  });
  assert.deepEqual(buildWorkflowArgs(request), ["prove"]);
  assert.deepEqual(buildWorkflowArgs({ ...request, noParallel: true }), ["prove", "--no-parallel"]);
});

test("resolved profile mode is authoritative for the actual launch argv", () => {
  const request = form({ agents: 4, research: true, researchWorkers: 3 });
  assert.deepEqual(buildWorkflowArgs(request, { LEANFLOW_PROVER_MODE: "standard" }), ["prove"]);
  assert.deepEqual(buildWorkflowArgs(request, { LEANFLOW_PROVER_MODE: "research", LEANFLOW_PROVER_PARALLELISM: "2" }), ["prove"]);
});

test("the prompt is last, because --prompt consumes the rest of the line", () => {
  const args = buildWorkflowArgs(
    form({ prompt: "try factorization first", cleanRoom: true, axioms: "Classical.choice" }),
  );
  assert.equal(args[args.length - 2], "--prompt");
  assert.equal(args[args.length - 1], "try factorization first");
  assert.ok(args.includes("--clean-room"));
  assert.ok(args.includes("--axioms"));
});

test("empty text fields are omitted rather than passed blank", () => {
  const args = buildWorkflowArgs(
    form({ provider: "   ", model: "", axioms: "  ", prompt: "  " }),
  );
  assert.deepEqual(args, ["prove"]);
});

test("additional skills repeat the flag", () => {
  assert.deepEqual(
    buildWorkflowArgs(form({ additionalSkills: ["a.md", "b.md"] })),
    ["prove", "--additional-skill", "a.md", "--additional-skill", "b.md"],
  );
});

test("describeCommand never renders prompt plaintext", () => {
  const text = describeCommand(form({ prompt: "two words" }));
  assert.ok(!text.includes("two words"));
  assert.match(text, /<redacted:prompt chars=9>/);
  assert.ok(text.startsWith("leanflow workflow prove"));
});

test("legacy rendered commands lose the entire prompt tail", () => {
  const text = redactPromptArgument(
    'leanflow workflow prove Main.lean --prompt "private proof idea with spaces"',
  );
  assert.ok(!text.includes("private proof idea"));
  assert.match(text, /--prompt .*<redacted:prompt/);
});

test("webview restore state never retains a half-written prompt", () => {
  const apiKey = "sk-proj-abcdefghijklmnopqrstuvwxyz012345";
  const request = form({
    prompt: "private proof idea",
    target: "Main.lean",
    model: apiKey,
    overrides: {
      LEANFLOW_RESEARCH_MODE: "1",
      OPENAI_API_KEY: apiKey,
    },
  });
  const stored = launchRequestForViewState(request);
  assert.equal(stored.prompt, "");
  assert.equal(stored.target, "Main.lean");
  assert.equal(stored.model, "");
  assert.deepEqual(stored.overrides, { LEANFLOW_RESEARCH_MODE: "1" });
  assert.equal(request.prompt, "private proof idea");
});

// ------------------------------------------------------------------- env

const profiles = {
  version: 1,
  count: 2,
  search_paths: [],
  profiles: [
    {
      name: "research",
      summary: "",
      builtin: true,
      overrides: { LEANFLOW_RESEARCH_MODE: "1", LEANFLOW_NEGATION_PROBE: "1" },
    },
    { name: "empty", summary: "", builtin: false, overrides: {} },
  ],
};

test("a profile supplies its overrides", () => {
  const { env, rejected } = resolveLaunchEnv(form({ profile: "research" }), profiles, CATALOG);
  assert.deepEqual(env.set, { LEANFLOW_RESEARCH_MODE: "1", LEANFLOW_NEGATION_PROBE: "1" });
  assert.deepEqual(rejected, []);
});

test("a per-launch override beats the profile", () => {
  const { env } = resolveLaunchEnv(
    form({ profile: "research", overrides: { LEANFLOW_NEGATION_PROBE: "0" } }),
    profiles,
    CATALOG,
  );
  assert.equal(env.set.LEANFLOW_NEGATION_PROBE, "0");
  assert.equal(env.set.LEANFLOW_RESEARCH_MODE, "1");
});

test("an emptied override is reported as unset, not just dropped", () => {
  const { env } = resolveLaunchEnv(
    form({ profile: "research", overrides: { LEANFLOW_NEGATION_PROBE: "" } }),
    profiles,
    CATALOG,
  );
  assert.ok(!("LEANFLOW_NEGATION_PROBE" in env.set));
  // Must be explicitly unset: the child inherits process.env, so merely
  // omitting the key would leave an ambient value in force.
  assert.deepEqual(env.unset, ["LEANFLOW_NEGATION_PROBE"]);
});

test("non-catalogued names are rejected, never passed to the child", () => {
  const { env, rejected } = resolveLaunchEnv(
    form({ overrides: { PYTHONPATH: ".", LD_PRELOAD: "/tmp/x.so", PATH: "/tmp" } }),
    profiles,
    CATALOG,
  );
  assert.deepEqual(env.set, {});
  assert.deepEqual(rejected.sort(), ["LD_PRELOAD", "PATH", "PYTHONPATH"]);
});

test("launcher plumbing is not settable by a caller", () => {
  const { env, rejected } = resolveLaunchEnv(
    form({ overrides: { LEANFLOW_PROJECT_ROOT: "/elsewhere" } }),
    profiles,
    CATALOG,
  );
  assert.deepEqual(env.set, {});
  assert.deepEqual(rejected, ["LEANFLOW_PROJECT_ROOT"]);
});

test("sensitive and terminal-only catalog knobs never reach an extension child", () => {
  assert.equal(ALLOWED.has("LEANFLOW_REDACT_SECRETS"), false);
  assert.equal(ALLOWED.has("LEANFLOW_RESEARCH_MODE"), true);
  const { env, rejected } = resolveLaunchEnv(
    form({
      overrides: {
        LEANFLOW_REDACT_SECRETS: "0",
        LEANFLOW_FUTURE_TERMINAL_ONLY: "1",
        LEANFLOW_DUMP_REQUESTS: "/tmp/raw-requests",
        LEANFLOW_YOLO_MODE: "1",
        LEANFLOW_HOME: "/tmp/attacker-home",
        LEANFLOW_SANDBOX_BASE_IMAGE: "attacker/image:latest",
      },
    }),
    profiles,
    CATALOG,
  );
  assert.deepEqual(env.set, {});
  assert.deepEqual(rejected.sort(), [
    "LEANFLOW_DUMP_REQUESTS",
    "LEANFLOW_FUTURE_TERMINAL_ONLY",
    "LEANFLOW_HOME",
    "LEANFLOW_REDACT_SECRETS",
    "LEANFLOW_SANDBOX_BASE_IMAGE",
    "LEANFLOW_YOLO_MODE",
  ]);
});

test("legacy catalogs still deny the known sensitive names", () => {
  assert.equal(
    isExtensionEditableKnob({
      name: "LEANFLOW_DUMP_REQUEST_STDOUT",
      editable: true,
    }),
    false,
  );
  assert.equal(
    isExtensionEditableKnob({ name: "LEANFLOW_NEGATION_PROBE", editable: true }),
    true,
  );
});

test("a profile carrying a restricted knob is visibly unavailable, not truncated", () => {
  const profile = {
    name: "unsafe-debug",
    summary: "",
    builtin: false,
    overrides: {
      LEANFLOW_RESEARCH_MODE: "1",
      LEANFLOW_REDACT_SECRETS: "0",
      LEANFLOW_NOT_REAL: "1",
    },
  };
  assert.deepEqual(rejectedProfileKnobNames(profile, CATALOG), [
    "LEANFLOW_NOT_REAL",
    "LEANFLOW_REDACT_SECRETS",
  ]);
  const result = resolveLaunchEnv(
    form({ profile: profile.name }),
    { ...profiles, profiles: [profile] },
    CATALOG,
  );
  assert.deepEqual(result.env.set, { LEANFLOW_RESEARCH_MODE: "1" });
  assert.deepEqual(result.rejected, ["LEANFLOW_REDACT_SECRETS", "LEANFLOW_NOT_REAL"]);
});

test("oversized values are rejected", () => {
  const { rejected } = resolveLaunchEnv(
    form({ overrides: { LEANFLOW_RESEARCH_MODE: "x".repeat(5000) } }),
    profiles,
    CATALOG,
  );
  assert.deepEqual(rejected, ["LEANFLOW_RESEARCH_MODE"]);
  assert.deepEqual(
    resolveLaunchEnv(
      form({ overrides: { LEANFLOW_RESEARCH_MODE: "1\0" } }),
      profiles,
      CATALOG,
    ).rejected,
    ["LEANFLOW_RESEARCH_MODE"],
  );
});

test("catalog types, enum choices, and numeric bounds are enforced by the host", () => {
  const { env, rejected } = resolveLaunchEnv(
    form({
      overrides: {
        LEANFLOW_RESEARCH_MODE: "sometimes",
        LEANFLOW_RESEARCH_WORKERS: "17",
        LEANFLOW_CODEX_REASONING_EFFORT: "ultra",
      },
    }),
    profiles,
    CATALOG,
  );
  assert.deepEqual(env.set, {});
  assert.deepEqual(rejected, [
    "LEANFLOW_RESEARCH_MODE",
    "LEANFLOW_RESEARCH_WORKERS",
    "LEANFLOW_CODEX_REASONING_EFFORT",
  ]);

  const valid = resolveLaunchEnv(
    form({
      overrides: {
        LEANFLOW_RESEARCH_MODE: "true",
        LEANFLOW_RESEARCH_WORKERS: "16",
        LEANFLOW_CODEX_REASONING_EFFORT: "high",
      },
    }),
    profiles,
    CATALOG,
  );
  assert.deepEqual(valid.rejected, []);
  assert.deepEqual(valid.env.set, {
    LEANFLOW_RESEARCH_MODE: "true",
    LEANFLOW_RESEARCH_WORKERS: "16",
    LEANFLOW_CODEX_REASONING_EFFORT: "high",
  });
});

test("an empty one-off value unsets, while an invalid saved profile refuses", () => {
  const oneOff = resolveLaunchEnv(
    form({ overrides: { LEANFLOW_RESEARCH_WORKERS: "" } }),
    profiles,
    CATALOG,
  );
  assert.deepEqual(oneOff.rejected, []);
  assert.deepEqual(oneOff.env.unset, ["LEANFLOW_RESEARCH_WORKERS"]);

  const invalidProfile = {
    name: "bad-workers",
    summary: "",
    builtin: false,
    overrides: { LEANFLOW_RESEARCH_WORKERS: "" },
  };
  assert.deepEqual(rejectedProfileKnobNames(invalidProfile, CATALOG), [
    "LEANFLOW_RESEARCH_WORKERS",
  ]);
  assert.deepEqual(rejectedProfileKnobNames(invalidProfile, CATALOG, true), []);
});

test("a missing selected profile is refused rather than running declared defaults", () => {
  assert.throws(
    () => resolveLaunchEnv(form({ profile: "not-present" }), profiles, CATALOG),
    /Unknown knob profile/,
  );
});

test("no profile and no overrides means an empty environment", () => {
  assert.deepEqual(resolveLaunchEnv(form(), profiles, CATALOG).env.set, {});
});

// ------------------------------------------------------- argument injection

test("a target beginning with a dash is refused", () => {
  assert.throws(() => buildWorkflowArgs(form({ target: "--research" })), /may not begin with/);
  assert.throws(() => buildWorkflowArgs(form({ target: "-rf" })), /may not begin with/);
  assert.match(validateLaunch(form({ target: "--research" })).join(" "), /may not begin/);
});

test("an ordinary target is still accepted", () => {
  assert.deepEqual(buildWorkflowArgs(form({ target: "IMO2026/P1.lean" })), [
    "prove",
    "IMO2026/P1.lean",
  ]);
});

// ------------------------------------------------------------- validation

test("formalize requires a target", () => {
  assert.ok(validateLaunch(form({ kind: "formalize" })).some((p) => p.includes("source document")));
  assert.deepEqual(validateLaunch(form({ kind: "formalize", target: "paper.tex" })), []);
});

test("prove-only flags are rejected on other workflows", () => {
  const problems = validateLaunch(
    form({ kind: "golf", research: true, cleanRoom: true, humanReview: true }),
  );
  assert.equal(problems.length, 3);
});

test("a clean prove form has no problems", () => {
  assert.deepEqual(validateLaunch(form({ target: "Main.lean" })), []);
});

test("legacy human-review values never reach redesigned prove launches or saved presets", () => {
  const legacy = form({ kind: "prove", humanReview: true });
  assert.equal(buildWorkflowArgs(legacy).includes("--human-review"), false);
  assert.equal(launchRequestForViewState(legacy).humanReview, false);
});

test("fields the host would reject for length are reported by the form", () => {
  // Without this the launcher posts a request the host drops wholesale, which
  // reads as a dead button. These are the fields no maxLength can express.
  const longSkill = validateLaunch(
    form({ target: "Main.lean", additionalSkills: ["skills/a.md", "x".repeat(4097)] }),
  );
  assert.equal(longSkill.length, 1);
  assert.match(longSkill[0], /Additional skill 2 is 4,097 characters; the limit is 4,096\./);

  const tooManySkills = validateLaunch(
    form({
      target: "Main.lean",
      additionalSkills: Array.from({ length: 257 }, (_, index) => `s${index}.md`),
    }),
  );
  assert.match(tooManySkills.join(" "), /257 additional skills; the limit is 256/);

  const longOverride = validateLaunch(
    form({ target: "Main.lean", overrides: { LEANFLOW_TEST: "x".repeat(4097) } }),
  );
  assert.match(longOverride.join(" "), /Knob LEANFLOW_TEST is 4,097 characters/);

  const longPrompt = validateLaunch(form({ target: "Main.lean", prompt: "x".repeat(8193) }));
  assert.match(longPrompt.join(" "), /The prompt is 8,193 characters; the limit is 8,192\./);
});

test("values exactly at the limit are accepted", () => {
  assert.deepEqual(
    validateLaunch(
      form({
        target: "Main.lean",
        prompt: "x".repeat(8192),
        additionalSkills: ["y".repeat(4096)],
        overrides: { LEANFLOW_TEST: "z".repeat(4096) },
      }),
    ),
    [],
  );
});
