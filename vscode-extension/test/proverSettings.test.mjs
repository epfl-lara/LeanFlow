/** Exercise the launch controls without an extension host or any model calls. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";

const bundle = await build({
  stdin: {
    contents: 'export { ProverSettings } from "./webview/views/ProverSettings"; export { setFixture } from "./webview/store"; export { resolveLaunchEnv } from "./src/core/launch";',
    resolveDir: process.cwd(),
    loader: "tsx",
  },
  bundle: true,
  write: false,
  format: "esm",
  platform: "node",
  jsx: "automatic",
  plugins: [{
    name: "isolated-launch-store",
    setup(builder) {
      builder.onLoad({ filter: /webview\/store\.tsx$/ }, () => ({
        contents: "let fixture; export const setFixture = value => fixture = value; export const useStore = () => fixture;",
      }));
      builder.onLoad({ filter: /webview\/vscodeApi\.ts$/ }, () => ({
        contents: "export const post = () => { throw new Error('Unexpected host message'); };",
      }));
    },
  }],
});
const { ProverSettings, setFixture, resolveLaunchEnv } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

const prefix = "LEANFLOW_PROVER_";
const flags = [
  ["MODE", "enum", "standard", ["standard", "research"]],
  ["SEARCH_ORDER", "enum", "bottom-up", ["bottom-up", "top-down"]],
  ["PARALLELISM", "int", "2"],
  ["PROVIDER", "enum", "", ["", "codex", "claude", "lemex"]],
  ["ORCHESTRATOR_PROVIDER", "enum", "", ["", "codex", "claude", "lemex"]],
  ["MODEL", "string", ""],
  ["ORCHESTRATOR_MODEL", "string", ""],
  ["REASONING_EFFORT", "string", ""],
  ["ORCHESTRATOR_REASONING_EFFORT", "string", ""],
].map(([suffix, value_type, fallback, choices]) => ({
  name: prefix + suffix, value_type, default: fallback, choices, summary: suffix,
  editable: true, extension_editable: true, sensitive: false,
}));

/** Expand these presentational React elements to inspect and operate controls. */
function elements(node) {
  if (node == null || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap(elements);
  if (typeof node.type === "function") return elements(node.type(node.props));
  return [node, ...elements(node.props?.children)];
}

function fixture(patch = {}, profileOverrides = {}) {
  const form = { kind: "prove", provider: "lemex", model: "", profile: "demo", overrides: {}, research: false, agents: 1, researchWorkers: null, ...patch };
  const state = {
    app: { providers: { selected: "lemex", backends: [{ id: "lemex", model_catalog: {
      status: "ready", source: "fixture", default_model: "native-prover", default_reasoning_effort: "medium",
      models: ["native-prover", "my-prover", "saved-prover", "saved-planner"].map(id => ({ id, label: id, reasoning_known: true, reasoning_efforts: ["low", "medium", "high"], default_reasoning_effort: "medium" })),
    } }] }, catalog: { groups: [{ flags }] }, profiles: { profiles: [{ name: "demo", overrides: profileOverrides }] } },
    view: { form },
    setForm: (next) => Object.assign(form, next),
  };
  setFixture(state);
  return {
    form,
    environment: () => resolveLaunchEnv(form, state.app.profiles, state.app.catalog),
    control(suffix) {
      const control = elements(ProverSettings()).find((item) => item.props?.["aria-label"] === prefix + suffix);
      assert.ok(control, `Missing ${suffix} control`);
      return control;
    },
  };
}

test("the launch controls preserve profile values and explicit model overrides", () => {
  const ui = fixture({ overrides: { [prefix + "MODEL"]: "my-prover" } }, { [prefix + "MODEL"]: "saved-prover", [prefix + "ORCHESTRATOR_MODEL"]: "saved-planner" });
  assert.equal(ui.control("MODEL").props.value, "my-prover");
  assert.equal(ui.control("ORCHESTRATOR_MODEL").props.value, "saved-planner");
  ui.control("MODEL").props.onChange({ target: { value: "" } });
  assert.equal(ui.control("MODEL").props.value, "");
  ui.control("MODEL").props.onChange({ target: { value: "__default__" } });
  assert.equal(ui.control("MODEL").props.value, "saved-prover");
});

test("changing mode clears legacy concurrency switches and preserves unrelated overrides", () => {
  const ui = fixture({ research: true, agents: 4, researchWorkers: 3, overrides: { [prefix + "MODEL"]: "worker" } });
  ui.control("MODE").props.onChange({ target: { value: "standard" } });
  assert.equal(ui.form.research, false);
  assert.equal(ui.form.agents, 1);
  assert.equal(ui.form.researchWorkers, null);
  assert.equal(ui.form.overrides[prefix + "MODEL"], "worker");
});

test("changing parallelism keeps research mode inherited from the profile", () => {
  const ui = fixture({}, { [prefix + "MODE"]: "research" });
  ui.control("PARALLELISM").props.onChange({ target: { value: "3" } });
  assert.equal(ui.form.overrides[prefix + "MODE"], "research");
  assert.equal(ui.form.research, true);
  assert.equal(ui.form.overrides[prefix + "PARALLELISM"], "3");
});

test("each role has an independent reasoning selector that reaches the launch environment", () => {
  const ui = fixture({}, { [prefix + "REASONING_EFFORT"]: "high", [prefix + "ORCHESTRATOR_REASONING_EFFORT"]: "low" });
  assert.equal(ui.control("REASONING_EFFORT").type, "select");
  assert.equal(ui.control("REASONING_EFFORT").props.value, "high");
  assert.equal(ui.control("ORCHESTRATOR_REASONING_EFFORT").props.value, "low");
  ui.control("REASONING_EFFORT").props.onChange({ target: { value: "medium" } });
  const launch = ui.environment();
  assert.deepEqual(launch.rejected, []);
  assert.equal(launch.env.set[prefix + "REASONING_EFFORT"], "medium");
  assert.equal(launch.env.set[prefix + "ORCHESTRATOR_REASONING_EFFORT"], "low");
});

test("reasoning can inherit the prover effort, then restore the saved profile", () => {
  const ui = fixture({}, { [prefix + "REASONING_EFFORT"]: "high", [prefix + "ORCHESTRATOR_REASONING_EFFORT"]: "low" });
  ui.control("ORCHESTRATOR_REASONING_EFFORT").props.onChange({ target: { value: "" } });
  assert.equal(ui.control("ORCHESTRATOR_REASONING_EFFORT").props.value, "");
  assert.deepEqual(ui.environment().env.unset, [prefix + "ORCHESTRATOR_REASONING_EFFORT"]);
  assert.equal(ui.environment().env.set[prefix + "REASONING_EFFORT"], "high");
  ui.control("ORCHESTRATOR_REASONING_EFFORT").props.onChange({ target: { value: "__default__" } });
  assert.equal(ui.control("ORCHESTRATOR_REASONING_EFFORT").props.value, "low");
  assert.equal(ui.form.overrides[prefix + "ORCHESTRATOR_REASONING_EFFORT"], undefined);
});

test("reasoning defaults do not force a level, and unknown saved values stay visible", () => {
  const ui = fixture();
  assert.equal(ui.control("REASONING_EFFORT").props.value, "__default__");
  const options = elements(ui.control("REASONING_EFFORT")).filter((item) => item.type === "option").map((item) => item.props.value);
  assert.deepEqual(options, ["__default__", "low", "medium", "high"]);
  const future = fixture({}, { [prefix + "REASONING_EFFORT"]: "future-level" });
  assert.equal(future.control("REASONING_EFFORT").props.value, "future-level");
  assert.ok(elements(future.control("REASONING_EFFORT")).some((item) => item.type === "option" && item.props.value === "future-level"));
});
