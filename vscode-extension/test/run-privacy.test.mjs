/** Tests for the extension host's durable/display privacy boundary. */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  launchPlanForDisplay,
  profileCatalogForDisplay,
  promptEvidenceMarker,
  redactCredentialUrl,
  redactSensitiveText,
  runLogForDisplay,
  runSummaryForDisplay,
  trackedRunForPersistence,
} from "../dist/test/runPrivacy.mjs";

function request(overrides = {}) {
  return {
    kind: "prove",
    target: "Main.lean",
    provider: "codex",
    model: "gpt-5.6-terra",
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

function tracked(overrides = {}) {
  return {
    id: "tracked-1",
    runId: "prove-vscode-tracked-1",
    label: "prove · Main.lean",
    status: "running",
    pid: 123,
    exitCode: null,
    startedAt: "2026-08-11T00:00:00Z",
    finishedAt: null,
    request: request(),
    command: "leanflow workflow prove Main.lean",
    projectRoot: "/tmp/project",
    appliedOverrides: { set: {}, unset: [] },
    experimentId: null,
    experimentCell: null,
    error: null,
    ...overrides,
  };
}

test("prompt evidence markers are stable hashes without prompt text", () => {
  const first = promptEvidenceMarker("  private lemma strategy  ");
  const second = promptEvidenceMarker("private lemma strategy");
  assert.equal(first, second);
  assert.match(first, /^<redacted:prompt sha256=[a-f0-9]{64} chars=22>$/);
  assert.ok(!first.includes("lemma strategy"));
});

test("tracked records retain only a bounded prompt fingerprint", () => {
  const prompt = "use the private intermediate identity";
  const apiKey = "sk-proj-abcdefghijklmnopqrstuvwxyz012345";
  const raw = tracked({
    request: request({
      prompt,
      additionalSkills: Array.from({ length: 80 }, (_, index) =>
        `skills/${index}-${"x".repeat(80)}/SKILL.md`,
      ),
    }),
    command: `leanflow workflow prove Main.lean --prompt ${prompt}`,
    appliedOverrides: { set: { OPENAI_API_KEY: apiKey }, unset: [] },
    error: `provider rejected Bearer ${apiKey}`,
  });

  const safe = trackedRunForPersistence(raw);
  const serialized = JSON.stringify(safe);
  assert.ok(!serialized.includes(prompt));
  assert.ok(!serialized.includes(apiKey));
  assert.match(safe.request.prompt, /^<redacted:prompt sha256=/);
  assert.match(safe.command, /--prompt/);
  assert.ok(safe.command.length <= 4096);
  assert.equal(safe.appliedOverrides.set.OPENAI_API_KEY, "<redacted:credential>");
  assert.equal(raw.request.prompt, prompt);
});

test("scrubbing a restored tracked record is idempotent", () => {
  const once = trackedRunForPersistence(
    tracked({
      request: request({ prompt: "old workspace-state prompt" }),
      command: "leanflow workflow prove Main.lean --prompt old workspace-state prompt",
    }),
  );
  assert.deepEqual(trackedRunForPersistence(once), once);
});

test("legacy empty and flat override records upgrade without crashing activation", () => {
  const empty = tracked({ appliedOverrides: {} });
  assert.deepEqual(trackedRunForPersistence(empty).appliedOverrides, {
    set: {},
    unset: [],
  });

  const flat = tracked({
    appliedOverrides: {
      LEANFLOW_NATIVE_CONTEXT_COMPRESSION_TOKENS: "4096",
      OPENAI_API_KEY: "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
    },
  });
  assert.deepEqual(trackedRunForPersistence(flat).appliedOverrides, {
    set: {
      LEANFLOW_NATIVE_CONTEXT_COMPRESSION_TOKENS: "4096",
      OPENAI_API_KEY: "<redacted:credential>",
    },
    unset: [],
  });
});

test("run history command tails and credential-shaped messages are redacted", () => {
  const raw = {
    run_id: "run-1",
    label: "prove",
    event_count: 1,
    started_at: "",
    updated_at: "",
    last_event_type: "provider-error",
    last_message: "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    workflow_kind: "prove",
    workflow_command: "prove Main.lean --prompt private historical prompt",
    active_skill: "",
    project_root: "/tmp/project",
    parent_run_id: "",
    run_scope: "",
    process_id: 1,
    path: "",
    stream_source: "hot",
    terminal: false,
    final_snapshot_recorded: false,
    finalized_at: "",
    exit_code: null,
    terminal_phase: "",
    terminal_status: "running",
  };
  const safe = runSummaryForDisplay(raw);
  assert.ok(!safe.workflow_command.includes("private historical prompt"));
  assert.match(safe.workflow_command, /<redacted:prompt sha256=/);
  assert.ok(!safe.last_message.includes("abcdefghijklmnopqrstuvwxyz"));
});

test("dry-run plans remove prompt copies and credential fields recursively", () => {
  const prompt = "private preview instruction";
  const key = "sk-proj-abcdefghijklmnopqrstuvwxyz012345";
  const raw = {
    version: 1,
    deferred: [],
    summary: { command: `prove --prompt ${prompt}`, note: prompt },
    argv: ["prove", "--prompt", prompt],
    cwd: "/tmp/project",
    project: { label: "project", root: "/tmp/project", lean_root: "/tmp/project" },
    workflow: {
      kind: "prove",
      canonical_command: `prove --prompt ${prompt}`,
      backend_command: "",
      args: prompt,
      parallel_agents: 1,
      research_mode: false,
      research_workers: 0,
      clean_room: false,
      clean_room_labels: [],
      human_review: false,
      allowed_axioms: "",
      explicit_goal: prompt,
    },
    runtime: { authorization: `Bearer ${key}` },
    active_skill: "",
    additional_skills: [],
    toolset: "",
    env_delta: { OPENAI_API_KEY: key },
  };

  const serialized = JSON.stringify(launchPlanForDisplay(raw, prompt));
  assert.ok(!serialized.includes(prompt));
  assert.ok(!serialized.includes(key));
  assert.match(serialized, /redacted:prompt/);
  assert.match(serialized, /redacted:credential/);
});

test("diagnostic redaction is bounded", () => {
  const value = `failure api_key=sk-proj-abcdefghijklmnopqrstuvwxyz ${"x".repeat(20000)}`;
  const safe = redactSensitiveText(value);
  assert.ok(!safe.includes("sk-proj-abcdefghijklmnopqrstuvwxyz"));
  assert.ok(safe.length <= 8192);
  assert.match(safe, /truncated sha256=/);
});

test("credential-bearing URLs retain only noncredential endpoint identity", () => {
  const raw =
    "https://alice:private@example.test/v1?accessToken=topsecret&api-version=2026-08-11#bearer-token";
  const safe = redactCredentialUrl(raw);
  assert.equal(
    safe,
    "https://example.test/v1?accessToken=%5Bredacted%5D&api-version=2026-08-11",
  );
  assert.ok(!safe.includes("alice"));
  assert.ok(!safe.includes("private"));
  assert.ok(!safe.includes("topsecret"));
  assert.ok(!safe.includes("bearer-token"));
  assert.equal(redactSensitiveText(`endpoint ${raw}`), `endpoint ${safe}`);
});

test("raw run logs stay structured but lose prompt and credential fields", () => {
  const prompt = "private repair strategy";
  const apiKey = "sk-proj-abcdefghijklmnopqrstuvwxyz012345";
  const raw = [
    JSON.stringify({
      type: "workflow-start",
      details: { prompt, apiKey },
      message:
        "provider https://user:pass@example.test/v1?accessToken=secret&api-version=1",
    }),
    `leanflow workflow prove Main.lean --prompt ${prompt}`,
  ].join("\n");
  const safe = runLogForDisplay(raw);
  assert.ok(!safe.includes(prompt));
  assert.ok(!safe.includes(apiKey));
  assert.ok(!safe.includes("user:pass"));
  assert.match(safe, /redacted:prompt/);
  assert.match(safe, /redacted:credential/);
  assert.match(safe, /api-version=1/);
});

test("profile snapshots redact credential overrides before reaching the webview", () => {
  const key = "sk-proj-abcdefghijklmnopqrstuvwxyz012345";
  const safe = profileCatalogForDisplay({
    version: 1,
    count: 1,
    search_paths: [],
    profiles: [
      {
        name: "local",
        summary: "",
        builtin: false,
        overrides: { LEANFLOW_RESEARCH_MODE: "1", OPENAI_API_KEY: key },
      },
    ],
  });
  assert.equal(safe.profiles[0].overrides.LEANFLOW_RESEARCH_MODE, "1");
  assert.equal(
    safe.profiles[0].overrides.OPENAI_API_KEY,
    "<redacted:credential>",
  );
  assert.ok(!JSON.stringify(safe).includes(key));
});
