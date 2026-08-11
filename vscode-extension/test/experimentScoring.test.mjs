import assert from "node:assert/strict";
import * as crypto from "node:crypto";
import { test } from "node:test";

import {
  canonicalRuntimeBaseUrl,
  compactExperimentMetrics,
  compareLaunchSourceIdentity,
  compareTargetSelectedSkills,
  experimentCommandForExport,
  experimentLaunchInvariant,
  freezeExperimentRuntimeIdentity,
  isExperimentLaunchInvariant,
  isExperimentRuntimeIdentity,
  metricsFromRun,
  runMetricsMatchExplicitPrompt,
  runMetricsMatchExplicitRuntime,
  sameExperimentLaunchInvariant,
  selectedSkillsTargetKey,
  unscoredMetrics,
} from "../dist/test/experimentScoring.mjs";

test("the first exact source identity is frozen and later byte drift is rejected", () => {
  assert.deepEqual(compareLaunchSourceIdentity(null, "source-a"), {
    accepted: true,
    expected: "source-a",
  });
  assert.equal(compareLaunchSourceIdentity("source-a", "source-a").accepted, true);
  assert.equal(compareLaunchSourceIdentity("source-a", "source-b").accepted, false);
});

test("global launch inputs stay fixed while selected skills are scoped by target", () => {
  const hash = (character) => character.repeat(64);
  const provenance = {
    git_commit: "abc",
    git_submodules_sha256: hash("1"),
    source_tree_sha256: hash("2"),
    project_manifest_sha256: hash("3"),
    workflow_guidance_sha256: hash("4"),
    runtime_content_sha256: hash("5"),
    runtime_source_sha256: hash("6"),
    python_runtime_sha256: hash("7"),
    behavior_config_sha256: hash("8"),
    dependency_manifest_sha256: hash("9"),
    lean_toolchain: "leanprover/lean4:v4.24.0",
  };
  const invariant = experimentLaunchInvariant(provenance);
  assert.equal(isExperimentLaunchInvariant(invariant), true);
  assert.equal(invariant.runtimeContentSha256, hash("5"));
  assert.equal(invariant.pythonRuntimeSha256, hash("7"));
  assert.equal(
    experimentLaunchInvariant({ ...provenance, python_runtime_sha256: "" }),
    null,
  );
  assert.equal(sameExperimentLaunchInvariant(invariant, { ...invariant }), true);
  assert.equal(
    sameExperimentLaunchInvariant(invariant, {
      ...invariant,
      behaviorConfigSha256: hash("a"),
    }),
    false,
  );
  assert.equal(
    sameExperimentLaunchInvariant(invariant, {
      ...invariant,
      pythonRuntimeSha256: hash("b"),
    }),
    false,
  );
  assert.equal(
    sameExperimentLaunchInvariant(invariant, {
      ...invariant,
      runtimeContentSha256: hash("c"),
    }),
    false,
  );
  const legacyInvariant = { ...invariant };
  delete legacyInvariant.runtimeContentSha256;
  delete legacyInvariant.pythonRuntimeSha256;
  assert.equal(isExperimentLaunchInvariant(legacyInvariant), false);

  const expectedByTarget = {};
  const first = compareTargetSelectedSkills(expectedByTarget, "A.lean", hash("a"));
  assert.equal(first.accepted, true);
  expectedByTarget[first.key] = first.expected;
  assert.equal(
    compareTargetSelectedSkills(expectedByTarget, "A.lean", hash("b")).accepted,
    false,
  );
  assert.equal(
    compareTargetSelectedSkills(expectedByTarget, "B.lean", hash("b")).accepted,
    true,
  );
  assert.equal(selectedSkillsTargetKey("A.lean"), selectedSkillsTargetKey("./A.lean"));
  assert.equal(
    selectedSkillsTargetKey("dir//A.lean"),
    selectedSkillsTargetKey("dir/./A.lean"),
  );
  assert.throws(() => selectedSkillsTargetKey("../A.lean"), /safe project-relative path/);
});

test("experiment export never serializes a raw frozen prompt", () => {
  const secret = "pasted-token-sk-never-export-this";
  const row = {
    command: experimentCommandForExport(
      `leanflow workflow prove --prompt ${secret}`,
      true,
    ),
    command_omitted_reason: "prompt-content-is-recorded-only-as-a-durable-content-identity",
  };
  const jsonl = JSON.stringify(row);
  assert.equal(row.command, null);
  assert.equal(jsonl.includes(secret), false);
  assert.equal(
    experimentCommandForExport("leanflow workflow prove Main.lean", false),
    "leanflow workflow prove Main.lean",
  );
});

test("sealed launch and final runtime must match the cell's explicit condition", () => {
  const metrics = {
    launch: {
      environment: {
        runtime: {
          base_url: "https://models.example.test/v1",
          model: "model-a",
          provider: "provider-a",
          reasoning_effort: "high",
        },
      },
    },
    outcome: { model: "model-a", provider: "provider-a" },
  };
  const expected = {
    requestedProvider: "provider-a",
    provider: "provider-a",
    model: "model-a",
    baseUrl: "https://models.example.test/v1",
    reasoningEffort: "high",
  };
  assert.equal(
    runMetricsMatchExplicitRuntime(metrics, expected),
    true,
  );
  assert.equal(
    runMetricsMatchExplicitRuntime(metrics, {
      ...expected,
      model: "model-b",
    }),
    false,
  );
  assert.equal(
    runMetricsMatchExplicitRuntime(
      { ...metrics, outcome: { model: "model-a", provider: "provider-b" } },
      expected,
    ),
    false,
  );
  assert.equal(
    runMetricsMatchExplicitRuntime(
      {
        ...metrics,
        launch: {
          environment: {
            runtime: {
              ...metrics.launch.environment.runtime,
              base_url: "https://other.example.test/v1",
            },
          },
        },
      },
      expected,
    ),
    false,
  );
});

test("sealed effective-prompt aliases must match SecretStorage metadata", () => {
  const prompt = "Prove the 𝘼-case 😀";
  const promptSha256 = crypto.createHash("sha256").update(prompt).digest("hex");
  const promptChars = Array.from(prompt).length;
  const marker = `[content-sha256:${promptSha256};chars:${promptChars}]`;
  const values = {
    LEANFLOW_NATIVE_EXPLICIT_GOAL: marker,
    LEANFLOW_NATIVE_USER_PROMPT: marker,
    LEANFLOW_NATIVE_EFFECTIVE_PROMPT: marker,
  };
  const metrics = { launch: { environment: { values } } };
  const expected = { promptPresent: true, promptSha256, promptChars };

  assert.equal(prompt.length > promptChars, true);
  assert.equal(runMetricsMatchExplicitPrompt(metrics, expected), true);
  assert.equal(
    runMetricsMatchExplicitPrompt(
      {
        launch: {
          environment: {
            values: {
              ...values,
              LEANFLOW_NATIVE_EFFECTIVE_PROMPT: `[content-sha256:${"b".repeat(64)};chars:${promptChars}]`,
            },
          },
        },
      },
      expected,
    ),
    false,
  );
  assert.equal(
    runMetricsMatchExplicitPrompt(
      {
        launch: {
          environment: {
            values: {
              ...values,
              LEANFLOW_NATIVE_USER_PROMPT: `[content-sha256:${promptSha256};chars:${promptChars + 1}]`,
            },
          },
        },
      },
      expected,
    ),
    false,
  );
  const dropped = { ...values };
  delete dropped.LEANFLOW_NATIVE_EXPLICIT_GOAL;
  assert.equal(
    runMetricsMatchExplicitPrompt(
      { launch: { environment: { values: dropped } } },
      expected,
    ),
    false,
  );
});

test("an absent experiment prompt requires every sealed alias to be explicitly empty", () => {
  const values = {
    LEANFLOW_NATIVE_EXPLICIT_GOAL: "",
    LEANFLOW_NATIVE_USER_PROMPT: "",
    LEANFLOW_NATIVE_EFFECTIVE_PROMPT: "",
  };
  const expected = { promptPresent: false, promptSha256: null, promptChars: 0 };
  assert.equal(
    runMetricsMatchExplicitPrompt({ launch: { environment: { values } } }, expected),
    true,
  );
  assert.equal(
    runMetricsMatchExplicitPrompt(
      {
        launch: {
          environment: {
            values: { ...values, LEANFLOW_NATIVE_USER_PROMPT: "unexpected" },
          },
        },
      },
      expected,
    ),
    false,
  );
});

test("provider selectors freeze to the CLI's canonical runtime aliases", () => {
  const codex = freezeExperimentRuntimeIdentity(
    {
      requested_provider: "codex",
      provider: "openai-codex",
      model: "gpt-5.6-terra",
      base_url: "https://chatgpt.com/backend-api/codex/responses",
      reasoning_effort: "high",
    },
    "codex",
  );
  assert.equal(codex.requestedProvider, "codex");
  assert.equal(codex.provider, "openai-codex");
  assert.equal(
    runMetricsMatchExplicitRuntime(
      {
        launch: {
          environment: {
            runtime: {
              base_url: codex.baseUrl,
              model: codex.model,
              provider: "openai-codex",
              reasoning_effort: codex.reasoningEffort,
            },
          },
        },
        outcome: { model: codex.model, provider: "openai-codex" },
      },
      codex,
    ),
    true,
  );

  const rcp = freezeExperimentRuntimeIdentity(
    {
      requested_provider: "rcp",
      provider: "custom",
      model: "glm-5",
      base_url: "https://rcp.example.test/v1?access_token=never-store#secret",
      reasoning_effort: "",
    },
    "rcp",
  );
  assert.equal(rcp.requestedProvider, "rcp");
  assert.equal(rcp.provider, "custom");
  assert.equal(
    rcp.baseUrl,
    "https://rcp.example.test/v1?access_token=%5Bredacted%5D",
  );
  assert.equal(rcp.baseUrl.includes("never-store"), false);
  assert.equal(isExperimentRuntimeIdentity(rcp), true);
  assert.equal(
    runMetricsMatchExplicitRuntime(
      {
        launch: {
          environment: {
            runtime: {
              base_url: "https://rcp.example.test/v1?access_token=%5Bredacted%5D",
              model: rcp.model,
              provider: "custom",
              reasoning_effort: "",
            },
          },
        },
        outcome: { model: rcp.model, provider: "custom" },
      },
      rcp,
    ),
    true,
  );

  const named = freezeExperimentRuntimeIdentity(
    {
      requested_provider: "lab-cluster",
      provider: "custom",
      model: "model-a",
      base_url: "https://lab.example.test/v1",
      reasoning_effort: "",
    },
    "lab-cluster",
  );
  assert.equal(named.provider, "custom");
  assert.notEqual(named.baseUrl, rcp.baseUrl);
  assert.throws(
    () =>
      freezeExperimentRuntimeIdentity(
        {
          requested_provider: "other-cluster",
          provider: "custom",
          model: "model-a",
          base_url: "https://lab.example.test/v1",
          reasoning_effort: "",
        },
        "lab-cluster",
      ),
    /did not bind the requested provider selector/,
  );
  assert.equal(
    canonicalRuntimeBaseUrl(
      "https://user:pass@lab.example.test/v1?token=x&api-version=1#secret",
    ),
    "https://lab.example.test/v1?api-version=1&token=%5Bredacted%5D",
  );
});

test("persisted and webview metrics stay bounded to the compact scoring summary", () => {
  const legacy = {
    ...unscoredMetrics("legacy"),
    declarations: Array.from({ length: 10_000 }, () => ({ name: "large" })),
  };
  const compact = compactExperimentMetrics(legacy);
  assert.equal(compact.phase, "unscored");
  assert.equal("declarations" in compact, false);
  assert.equal(compact.missingReason, "legacy");
});

test("an unavailable aggregate contains no invented zero or failure", () => {
  const metrics = unscoredMetrics();
  assert.equal(metrics.scopeExact, false);
  assert.equal(metrics.phase, "unscored");
  assert.equal(metrics.solved, null);
  assert.equal(metrics.durationSeconds, null);
  assert.equal(metrics.toolCalls, null);
  assert.equal(metrics.verificationFailures, null);
  assert.equal(metrics.declarationsTotal, null);
  assert.equal(metrics.declarationsProved, null);
  assert.equal(metrics.provenanceFinal, null);
  assert.equal(metrics.apiCallsComplete, false);
  assert.match(metrics.missingReason, /unavailable/);
});

test("a durable aggregate preserves recorded measurements", () => {
  const metrics = metricsFromRun(
    {
      version: 2,
      scope: { exact: true, final_snapshot: "verified" },
      events: { complete: true },
      run: { duration_s: 12 },
      launch: { captured_at: "now", context: {}, environment: {} },
      usage: {
        complete: true,
        api_calls_complete: true,
        tokens_complete: true,
        cost_complete: true,
        source: "conversation-end-events",
        api_calls: 3,
        input_tokens: 10,
        output_tokens: 20,
        cost_usd: 0.4,
      },
      activity: { api_requests: 4, tool_calls: 5 },
      failures: { "verification-rejected": 2, "proof-attempt-rejected": 1 },
      outcome: {
        proof_solved: true,
        sorry_count: 0,
        project_sorry_count: 0,
        phase: "done",
        declarations_total: 2,
        declarations_proved: 2,
        exit_code: 0,
        terminal_status: "succeeded",
      },
      declarations: [],
      provenance: {
        provenance_complete: true,
        git_commit: "abc",
        git_dirty: false,
        git_status_sha256: "status",
        git_diff_sha256: "diff",
        git_untracked_index_sha256: "untracked",
        source_tree_complete: true,
        source_tree_sha256: "launch-tree",
        source_identity_sha256: "launch",
        lean_toolchain: "lean",
      },
      provenance_final: {
        provenance_complete: true,
        git_commit: "abc",
        git_dirty: true,
        git_status_sha256: "status-final",
        git_diff_sha256: "diff-final",
        git_untracked_index_sha256: "untracked",
        source_tree_complete: true,
        source_tree_sha256: "final-tree",
        source_identity_sha256: "final",
        lean_toolchain: "lean",
      },
      source_changed_during_run: true,
      runtime_source_changed: false,
      selected_skills_changed: false,
      behavior_config_changed: false,
      project_configuration_changed: false,
      python_runtime_changed: false,
    },
  );
  assert.equal(metrics.durationSeconds, 12);
  assert.equal(metrics.apiCalls, 3);
  assert.equal(metrics.toolCalls, 5);
  assert.equal(metrics.verificationFailures, 3);
  assert.equal(metrics.solved, true);
  assert.equal(metrics.scopeExact, true);
  assert.equal(metrics.usageComplete, true);
  assert.equal(metrics.usageSource, "conversation-end-events");
  assert.equal(metrics.provenanceFinal.source_identity_sha256, "final");
  assert.equal(metrics.sourceChangedDuringRun, true);
});

test("an inexact or usage-partial aggregate stays missing", () => {
  const base = {
    version: 2,
    scope: { exact: false, final_snapshot: "verified" },
    events: { complete: true },
    run: { duration_s: 12 },
    launch: { captured_at: "now", context: {}, environment: {} },
    usage: {
      complete: false,
      api_calls_complete: true,
      tokens_complete: false,
      cost_complete: false,
      source: "run-log",
      api_calls: 3,
      input_tokens: 10,
      output_tokens: 20,
      cost_usd: 0.4,
    },
    activity: { tool_calls: 5 },
    failures: {},
    outcome: {
      proof_solved: true,
      sorry_count: 0,
      project_sorry_count: 0,
      phase: "done",
      declarations_total: 1,
      declarations_proved: 1,
      exit_code: 0,
      terminal_status: "succeeded",
    },
    declarations: [],
    provenance: {
      provenance_complete: true,
      source_identity_sha256: "same",
      source_tree_sha256: "tree",
    },
    provenance_final: {
      provenance_complete: true,
      source_identity_sha256: "same",
      source_tree_sha256: "tree",
    },
    source_changed_during_run: false,
    runtime_source_changed: false,
    selected_skills_changed: false,
    behavior_config_changed: false,
    project_configuration_changed: false,
    python_runtime_changed: false,
  };
  assert.equal(metricsFromRun(base).phase, "unscored");

  const partialUsage = metricsFromRun({ ...base, scope: { ...base.scope, exact: true } });
  assert.equal(partialUsage.durationSeconds, 12);
  assert.equal(partialUsage.toolCalls, 5);
  assert.equal(partialUsage.apiCalls, 3);
  assert.equal(partialUsage.inputTokens, null);
  assert.equal(partialUsage.costUsd, null);
});

test("an exact non-proof workflow keeps measured cost and time without a fake solve verdict", () => {
  const row = metricsFromRun({
    version: 2,
    scope: { exact: true, final_snapshot: "verified" },
    events: { complete: true },
    run: { duration_s: 7 },
    launch: { captured_at: "now", context: {}, environment: {} },
    usage: {
      complete: true,
      api_calls_complete: true,
      tokens_complete: true,
      cost_complete: true,
      source: "conversation-end-events",
      api_calls: 1,
      input_tokens: 2,
      output_tokens: 3,
      cost_usd: 0.25,
    },
    activity: { tool_calls: 4 },
    failures: {},
    outcome: {
      phase: "done",
      exit_code: 0,
      terminal_status: "succeeded",
      proof_solved: null,
      sorry_count: null,
      project_sorry_count: null,
      declarations_total: null,
      declarations_proved: null,
    },
    provenance: {
      provenance_complete: true,
      source_identity_sha256: "same",
      source_tree_sha256: "tree",
    },
    provenance_final: {
      provenance_complete: true,
      source_identity_sha256: "same",
      source_tree_sha256: "tree",
    },
    source_changed_during_run: false,
    runtime_source_changed: false,
    selected_skills_changed: false,
    behavior_config_changed: false,
    project_configuration_changed: false,
    python_runtime_changed: false,
  });
  assert.equal(row.phase, "done");
  assert.equal(row.solved, null);
  assert.equal(row.declarationsTotal, null);
  assert.equal(row.durationSeconds, 7);
  assert.equal(row.costUsd, 0.25);
});

test("exact-looking rows reject non-source runtime and configuration drift", () => {
  const base = {
    version: 2,
    scope: { exact: true, final_snapshot: "verified" },
    events: { complete: true },
    run: { duration_s: 1 },
    launch: { captured_at: "now", context: {}, environment: {} },
    usage: {
      complete: true,
      api_calls_complete: true,
      tokens_complete: true,
      cost_complete: true,
      source: "events",
    },
    activity: { tool_calls: 0 },
    failures: {},
    outcome: { phase: "done", exit_code: 0, terminal_status: "succeeded" },
    provenance: {
      provenance_complete: true,
      source_identity_sha256: "launch",
      source_tree_sha256: "launch-tree",
    },
    provenance_final: {
      provenance_complete: true,
      source_identity_sha256: "final",
      source_tree_sha256: "final-tree",
    },
    source_changed_during_run: true,
    runtime_source_changed: false,
    selected_skills_changed: false,
    behavior_config_changed: false,
    project_configuration_changed: false,
    python_runtime_changed: false,
  };
  for (const field of [
    "runtime_source_changed",
    "selected_skills_changed",
    "behavior_config_changed",
    "project_configuration_changed",
    "python_runtime_changed",
  ]) {
    assert.equal(metricsFromRun({ ...base, [field]: true }).phase, "unscored");
  }
  assert.equal(metricsFromRun({ ...base, selected_skills_changed: undefined }).phase, "unscored");
});
