import assert from "node:assert/strict";
import * as path from "node:path";
import { test } from "node:test";

import {
  freezeExperimentLaunchContract,
  isExperimentLaunchContract,
  runMetricsMatchLaunchContract,
  runMetricsMatchRequestedTarget,
} from "../dist/test/experimentLaunchContract.mjs";

function preview(overrides = {}) {
  const hasEffectiveOverride = Object.hasOwn(overrides, "env_effective");
  const {
    workflow: workflowOverrides = {},
    env_delta: deltaOverrides = {},
    env_effective: effectiveOverrides = {},
    ...rest
  } = overrides;
  return {
    version: 1,
    deferred: [],
    summary: {},
    argv: [],
    cwd: "/project",
    project: { label: "Demo", root: "/project", lean_root: "/project" },
    workflow: {
      kind: "prove",
      canonical_command: "/prove",
      backend_command: "/prove Main.lean",
      args: "Main.lean",
      parallel_agents: 1,
      research_mode: true,
      research_workers: 2,
      clean_room: false,
      clean_room_labels: [],
      human_review: true,
      allowed_axioms: "Classical.choice",
      explicit_goal: "",
      ...workflowOverrides,
    },
    runtime: {},
    active_skill: "lean-proof-loop",
    additional_skills: ["skills/one", "skills/two"],
    toolset: "leanflow-prove-worker",
    env_delta: {
      LEANFLOW_KNOB_ALPHA: "enabled",
      LEANFLOW_KNOB_NOTE: "sk-private-looking-value",
      LEANFLOW_PLAN_STATE: "1",
      LEANFLOW_NATIVE_API_KEY: "<redacted:credential>",
      LEANFLOW_NATIVE_REQUESTED_TARGET: "Main.lean",
      LEANFLOW_PROJECT_ROOT: "/project",
      ...deltaOverrides,
    },
    env_effective: hasEffectiveOverride
      ? effectiveOverrides
      : {
          LEANFLOW_KNOB_ALPHA: "enabled",
          LEANFLOW_KNOB_NOTE: "sk-private-looking-value",
          LEANFLOW_PLAN_STATE: "1",
          LEANFLOW_NATIVE_API_KEY: "<redacted:credential>",
          LEANFLOW_NATIVE_REQUESTED_TARGET: "Main.lean",
          LEANFLOW_PROJECT_ROOT: "/project",
          LEANFLOW_NATIVE_PARALLEL_AGENTS: "1",
          LEANFLOW_NATIVE_USER_APPROVED_SWARM: "0",
          LEANFLOW_HUMAN_REVIEW_ENABLED: "1",
          LEANFLOW_NATIVE_ACTIVE_SKILL: "lean-proof-loop",
          LEANFLOW_NATIVE_TOOLSET: "leanflow-prove-worker",
          LEANFLOW_RESEARCH_MODE: "1",
          LEANFLOW_RESEARCH_WORKERS: "2",
          LEANFLOW_NATIVE_ALLOWED_AXIOMS: "Classical.choice",
          LEANFLOW_NATIVE_ADDITIONAL_SKILLS: ["skills/one", "skills/two"].join(
            path.delimiter,
          ),
        },
    ...rest,
  };
}

function metrics(values, requestedTarget = "Main.lean") {
  return {
    launch: {
      environment: {
        values,
        runtime: { requested_target: requestedTarget },
      },
    },
  };
}

function validValues(overrides = {}) {
  return {
    LEANFLOW_KNOB_ALPHA: "enabled",
    LEANFLOW_KNOB_NOTE: "sk-private-looking-value",
    LEANFLOW_PLAN_STATE: "1",
    LEANFLOW_NATIVE_PARALLEL_AGENTS: "1",
    LEANFLOW_NATIVE_USER_APPROVED_SWARM: "0",
    LEANFLOW_HUMAN_REVIEW_ENABLED: "1",
    LEANFLOW_NATIVE_ACTIVE_SKILL: "lean-proof-loop",
    LEANFLOW_NATIVE_TOOLSET: "leanflow-prove-worker",
    LEANFLOW_RESEARCH_MODE: "1",
    LEANFLOW_RESEARCH_WORKERS: "2",
    LEANFLOW_NATIVE_ALLOWED_AXIOMS: "Classical.choice",
    LEANFLOW_NATIVE_ADDITIONAL_SKILLS: ["skills/one", "skills/two"].join(path.delimiter),
    LEANFLOW_NATIVE_REQUESTED_TARGET: "Main.lean",
    ...overrides,
  };
}

test("requested target binds the source target even when formalize changes active file", () => {
  const values = {
    LEANFLOW_NATIVE_REQUESTED_TARGET: "docs/paper.tex",
    LEANFLOW_NATIVE_ACTIVE_FILE: "Generated/Paper.lean",
  };
  assert.equal(
    runMetricsMatchRequestedTarget(metrics(values, "docs/paper.tex"), "docs/paper.tex"),
    true,
  );
  assert.equal(
    runMetricsMatchRequestedTarget(metrics(values, "docs/paper.tex"), "docs/other.tex"),
    false,
  );
  assert.equal(
    runMetricsMatchRequestedTarget(
      metrics({ LEANFLOW_NATIVE_REQUESTED_TARGET: "docs/other.tex" }, "docs/other.tex"),
      "docs/paper.tex",
    ),
    false,
  );
  assert.equal(
    runMetricsMatchRequestedTarget(
      metrics({ LEANFLOW_NATIVE_REQUESTED_TARGET: "docs/paper.tex" }, "docs/other.tex"),
      "docs/paper.tex",
    ),
    false,
  );
  assert.equal(runMetricsMatchRequestedTarget(metrics({}, "docs/paper.tex"), "docs/paper.tex"), false);
  assert.equal(
    runMetricsMatchRequestedTarget(
      metrics({ LEANFLOW_NATIVE_REQUESTED_TARGET: "" }, ""),
      "",
    ),
    true,
  );
});

test("frozen launch contract rejects dropped, substituted, and uncleared knobs", () => {
  const launchEnv = {
    set: {
      LEANFLOW_KNOB_ALPHA: "enabled",
      LEANFLOW_KNOB_NOTE: "sk-private-looking-value",
    },
    unset: ["LEANFLOW_KNOB_CLEARED"],
  };
  const contract = freezeExperimentLaunchContract(preview(), launchEnv);
  assert.equal(isExperimentLaunchContract(contract), true);
  const serialized = JSON.stringify(contract);
  for (const privateValue of [
    "enabled",
    "sk-private-looking-value",
    "Classical.choice",
    "skills/one",
    "lean-proof-loop",
  ]) {
    assert.equal(serialized.includes(privateValue), false);
  }
  assert.equal(serialized.includes("LEANFLOW_NATIVE_API_KEY"), false);
  assert.equal(runMetricsMatchLaunchContract(metrics(validValues()), contract), true);

  const dropped = validValues();
  delete dropped.LEANFLOW_KNOB_ALPHA;
  assert.equal(runMetricsMatchLaunchContract(metrics(dropped), contract), false);
  assert.equal(
    runMetricsMatchLaunchContract(
      metrics(validValues({ LEANFLOW_KNOB_ALPHA: "substituted" })),
      contract,
    ),
    false,
  );
  assert.equal(
    runMetricsMatchLaunchContract(
      metrics(validValues({ LEANFLOW_KNOB_CLEARED: "ambient" })),
      contract,
    ),
    false,
  );
  assert.equal(
    runMetricsMatchLaunchContract(
      metrics({
        ...validValues(),
        LEANFLOW_NATIVE_ADDITIONAL_SKILLS: [
          "skills/one",
          "skills/two",
          "unexpected/extra",
        ].join(path.delimiter),
      }),
      contract,
    ),
    false,
  );
});

test("launch contract survives restore and binds canonical false and empty states", () => {
  const noResearch = preview({
    workflow: {
      research_mode: false,
      research_workers: 0,
      clean_room: false,
      human_review: false,
      allowed_axioms: "",
    },
    additional_skills: [],
    env_delta: {},
    env_effective: {
      LEANFLOW_NATIVE_PARALLEL_AGENTS: "1",
      LEANFLOW_NATIVE_USER_APPROVED_SWARM: "0",
      LEANFLOW_HUMAN_REVIEW_ENABLED: "0",
      LEANFLOW_NATIVE_ACTIVE_SKILL: "lean-proof-loop",
      LEANFLOW_NATIVE_TOOLSET: "leanflow-prove-worker",
      LEANFLOW_NATIVE_ADDITIONAL_SKILLS: "",
    },
  });
  const contract = freezeExperimentLaunchContract(noResearch, {
    set: {},
    unset: ["LEANFLOW_KNOB_CLEARED"],
  });
  const restored = JSON.parse(JSON.stringify(contract));
  assert.equal(isExperimentLaunchContract(restored), true);
  assert.equal(isExperimentLaunchContract(null), false);
  const values = {
    LEANFLOW_NATIVE_PARALLEL_AGENTS: "1",
    LEANFLOW_NATIVE_USER_APPROVED_SWARM: "0",
    LEANFLOW_HUMAN_REVIEW_ENABLED: "0",
    LEANFLOW_NATIVE_ACTIVE_SKILL: "lean-proof-loop",
    LEANFLOW_NATIVE_TOOLSET: "leanflow-prove-worker",
    LEANFLOW_RESEARCH_MODE: "false",
    LEANFLOW_DISABLE_SOLUTION_RESEARCH: "0",
    LEANFLOW_NATIVE_ALLOWED_AXIOMS: "",
    LEANFLOW_NATIVE_ADDITIONAL_SKILLS: "",
  };
  assert.equal(runMetricsMatchLaunchContract(metrics(values), restored), true);
  assert.equal(
    runMetricsMatchLaunchContract(
      metrics({ ...values, LEANFLOW_DISABLE_SOLUTION_RESEARCH: "1" }),
      restored,
    ),
    false,
  );
  assert.equal(
    runMetricsMatchLaunchContract(
      metrics({ ...values, LEANFLOW_NATIVE_ALLOWED_AXIOMS: "Classical.choice" }),
      restored,
    ),
    false,
  );
  assert.equal(
    runMetricsMatchLaunchContract(metrics({ ...values, LEANFLOW_KNOB_CLEARED: "" }), restored),
    false,
  );
});

test("only formalize may append its exactly sealed generated blueprint skill", () => {
  const formalizePreview = preview({
    workflow: {
      kind: "formalize",
      research_mode: false,
      research_workers: 0,
      human_review: false,
      allowed_axioms: "",
    },
    additional_skills: ["skills/one"],
    active_skill: "lean-formalization",
    toolset: "leanflow-native",
    env_effective: {
      LEANFLOW_NATIVE_PARALLEL_AGENTS: "1",
      LEANFLOW_NATIVE_USER_APPROVED_SWARM: "0",
      LEANFLOW_HUMAN_REVIEW_ENABLED: "0",
      LEANFLOW_RESEARCH_MODE: "0",
      LEANFLOW_NATIVE_ACTIVE_SKILL: "lean-formalization",
      LEANFLOW_NATIVE_TOOLSET: "leanflow-native",
      LEANFLOW_NATIVE_ADDITIONAL_SKILLS: "skills/one",
      LEANFLOW_NATIVE_REQUESTED_TARGET: "docs/paper.tex",
    },
  });
  const contract = freezeExperimentLaunchContract(formalizePreview, {
    set: {},
    unset: [],
  });
  const blueprint = "/clone/.leanflow/formalization/blueprint-skill";
  const values = {
    LEANFLOW_NATIVE_PARALLEL_AGENTS: "1",
    LEANFLOW_NATIVE_USER_APPROVED_SWARM: "0",
    LEANFLOW_HUMAN_REVIEW_ENABLED: "0",
    LEANFLOW_RESEARCH_MODE: "0",
    LEANFLOW_NATIVE_ACTIVE_SKILL: "lean-formalization",
    LEANFLOW_NATIVE_TOOLSET: "leanflow-native",
    LEANFLOW_NATIVE_ADDITIONAL_SKILLS: ["skills/one", blueprint].join(path.delimiter),
    LEANFLOW_FORMALIZATION_BLUEPRINT_SKILL: blueprint,
  };
  assert.equal(runMetricsMatchLaunchContract(metrics(values), contract), true);
  assert.equal(
    runMetricsMatchLaunchContract(
      metrics({ ...values, LEANFLOW_FORMALIZATION_BLUEPRINT_SKILL: "/other/skill" }),
      contract,
    ),
    false,
  );
  assert.equal(
    runMetricsMatchLaunchContract(
      metrics({
        ...values,
        LEANFLOW_NATIVE_ADDITIONAL_SKILLS: [
          "skills/one",
          blueprint,
          "unexpected/second-extra",
        ].join(path.delimiter),
      }),
      contract,
    ),
    false,
  );
});

test("freezing fails closed when the preview omits the complete effective environment", () => {
  const incomplete = preview();
  delete incomplete.env_effective;
  assert.throws(
    () => freezeExperimentLaunchContract(incomplete, { set: {}, unset: [] }),
    /complete effective child environment/,
  );
});
