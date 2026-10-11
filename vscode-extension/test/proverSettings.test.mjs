/** Exercise the launch controls without an extension host or any model calls. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
import { mount, textOf } from "./helpers/webviewFixture.mjs";

const bundle = await build({
  stdin: {
    contents: 'export { ProverSettings } from "./webview/views/ProverSettings"; export { GlobalLimits } from "./webview/views/GlobalLimits"; export { setFixture } from "./webview/store"; export { resolveLaunchEnv } from "./src/core/launch";',
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
const { ProverSettings, GlobalLimits, setFixture, resolveLaunchEnv } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

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
  ["AGENT_CALLS", "int", ""],
  ["TOKENS", "int", ""],
  ["WALL_TIME_S", "float", ""],
  ["PROVER_SESSIONS", "int", ""],
  ["ORCHESTRATOR_SESSIONS", "int", ""],
  ["NEGATION_SESSIONS", "int", ""],
  ["MAX_RESTARTS", "int", ""],
  ["PLAN_REFINEMENTS", "int", ""],
  ["MAX_DECOMPOSITIONS", "int", ""],
  ["MAX_NODES", "int", "128"],
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
      const control = elements([GlobalLimits(), ProverSettings()]).find((item) => item.props?.["aria-label"] === prefix + suffix);
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

test("global limits are immediately visible and explicitly show Unlimited", () => {
  const ui = fixture();
  const rendered = elements([GlobalLimits(), ProverSettings()]);
  assert.ok(rendered.some(item => item.props?.className === "card-title" && item.props.children === "Global limits"));
  assert.equal(rendered.filter(item => item.type === "details").length, 0);
  for (const suffix of ["AGENT_CALLS", "TOKENS", "WALL_TIME_S"]) {
    assert.equal(ui.control(suffix).props.placeholder, "Unlimited");
    assert.equal(ui.control(suffix).props.value, "");
  }
  assert.deepEqual(ui.environment().env, { set: {}, unset: [] });
});

test("global limit edits reach the runtime and convert minutes to seconds", () => {
  const ui = fixture();
  ui.control("WALL_TIME_S").props.onChange({ target: { value: "2.5" } });
  ui.control("AGENT_CALLS").props.onChange({ target: { value: "12" } });
  ui.control("TOKENS").props.onChange({ target: { value: "50000" } });
  assert.equal(ui.control("WALL_TIME_S").props.value, "2.5");
  assert.deepEqual(ui.environment().rejected, []);
  assert.deepEqual(ui.environment().env.set, {
    [prefix + "WALL_TIME_S"]: "150",
    [prefix + "AGENT_CALLS"]: "12",
    [prefix + "TOKENS"]: "50000",
  });
});

test("clearing an inherited limit makes it Unlimited and profile reset restores it", () => {
  const ui = fixture({}, { [prefix + "AGENT_CALLS"]: "7", [prefix + "WALL_TIME_S"]: "90" });
  assert.equal(ui.control("WALL_TIME_S").props.value, "1.5");
  assert.equal(ui.control("AGENT_CALLS").props.value, "7");
  ui.control("AGENT_CALLS").props.onChange({ target: { value: "" } });
  assert.equal(ui.control("AGENT_CALLS").props.value, "");
  assert.equal(ui.environment().env.set[prefix + "AGENT_CALLS"], undefined);
  assert.deepEqual(ui.environment().env.unset, [prefix + "AGENT_CALLS"]);
  const reset = elements([GlobalLimits(), ProverSettings()]).find(item => item.props?.["aria-label"] === "Restore profile agent calls");
  assert.ok(reset);
  reset.props.onClick();
  assert.equal(ui.control("AGENT_CALLS").props.value, "7");
  assert.equal(ui.environment().env.set[prefix + "AGENT_CALLS"], "7");
});

test("zero stays an explicit exhausted limit instead of becoming Unlimited", () => {
  const ui = fixture({ overrides: { [prefix + "AGENT_CALLS"]: "0", [prefix + "TOKENS"]: "0", [prefix + "WALL_TIME_S"]: "0" } });
  for (const suffix of ["AGENT_CALLS", "TOKENS", "WALL_TIME_S"]) {
    assert.equal(ui.control(suffix).props.value, "0");
    assert.equal(ui.environment().env.set[prefix + suffix], "0");
  }
});


test("research profile mode is visible and an explicit standard choice wins", () => {
  const ui = fixture({}, { LEANFLOW_RESEARCH_MODE: "1" });
  assert.equal(ui.control("MODE").props.value, "research");
  ui.control("MODE").props.onChange({ target: { value: "standard" } });
  assert.equal(ui.control("MODE").props.value, "standard");
  assert.equal(ui.form.research, false);
  assert.equal(ui.environment().env.set[prefix + "MODE"], "standard");
  ui.control("MODE").props.onChange({ target: { value: "__default__" } });
  assert.equal(ui.control("MODE").props.value, "research");
  assert.equal(ui.form.research, true);
});

test("explicit empty mode masks a research profile and modern mode wins legacy state", () => {
  let ui = fixture({ overrides: { [prefix + "MODE"]: "" }, research: true }, { LEANFLOW_RESEARCH_MODE: "1" });
  assert.equal(ui.control("MODE").props.value, "standard");
  ui = fixture({ research: true }, { [prefix + "MODE"]: "standard", LEANFLOW_RESEARCH_MODE: "1" });
  assert.equal(ui.control("MODE").props.value, "standard");
});

test("structural allowances are visible and reach the runtime without changing role selection", () => {
  const ui = fixture({ overrides: { [prefix + "MODEL"]: "my-prover" } });
  for (const suffix of ["MAX_RESTARTS", "PLAN_REFINEMENTS", "MAX_DECOMPOSITIONS"]) {
    assert.equal(ui.control(suffix).props.placeholder, "Unlimited");
    assert.equal(ui.control(suffix).props.value, "");
    ui.control(suffix).props.onChange({ target: { value: "4" } });
    assert.equal(ui.environment().env.set[prefix + suffix], "4");
  }
  assert.equal(ui.control("MAX_NODES").props.value, "128");
  ui.control("MAX_NODES").props.onChange({ target: { value: "32" } });
  assert.equal(ui.environment().env.set[prefix + "MAX_NODES"], "32");
  assert.deepEqual(ui.environment().rejected, []);
  assert.equal(ui.form.overrides[prefix + "MODEL"], "my-prover");
});

test("clearing structural limits masks profiles while zero stays explicit", () => {
  const labels = { MAX_RESTARTS: "standard retries per goal", PLAN_REFINEMENTS: "proof-direction changes", MAX_DECOMPOSITIONS: "recovery decisions", MAX_NODES: "maximum graph nodes" };
  for (const [suffix, label] of Object.entries(labels)) {
    const ui = fixture({}, { [prefix + suffix]: "7" });
    assert.equal(ui.control(suffix).props.value, "7");
    ui.control(suffix).props.onChange({ target: { value: "" } });
    assert.deepEqual(ui.environment().env.unset, [prefix + suffix]);
    assert.equal(ui.control(suffix).props.value, "");
    const restore = elements(ProverSettings()).find(item => item.props?.["aria-label"] === `Restore profile ${label}`);
    assert.ok(restore);
    restore.props.onClick();
    assert.equal(ui.control(suffix).props.value, "7");
    if (suffix !== "MAX_NODES") {
      ui.control(suffix).props.onChange({ target: { value: "0" } });
      assert.equal(ui.environment().env.set[prefix + suffix], "0");
    }
  }
});

for (const kind of ["prove", "formalize", "review", "refactor", "golf", "draft"]) {
  test(`${kind} launch displays global limits and preserves profile limits when edited`, () => {
    const ui = mount();
    try {
      ui.update(state => {
        state.view.form.kind = kind;
        state.view.form.target = "Main.lean";
        state.view.form.profile = "custom";
        state.app.profiles.profiles[1].overrides = { [prefix + "AGENT_CALLS"]: "7" };
        state.app.catalog.groups[0].flags.push(...flags.filter(flag => ["AGENT_CALLS", "TOKENS", "WALL_TIME_S"].some(suffix => flag.name === prefix + suffix)));
      });
      assert.equal(ui.root().findAllByProps({ className: "card-title" }).filter(node => textOf(node) === "Global limits").length, 1);
      assert.equal(ui.input(prefix + "AGENT_CALLS").props.value, "7");
      ui.change(ui.input(prefix + "AGENT_CALLS"), "");
      ui.change(ui.input(prefix + "WALL_TIME_S"), "2.5");
      let launch = resolveLaunchEnv(ui.view.form, ui.app.profiles, ui.app.catalog);
      assert.deepEqual(launch.rejected, []);
      assert.deepEqual(launch.env.unset, [prefix + "AGENT_CALLS"]);
      assert.equal(launch.env.set[prefix + "WALL_TIME_S"], "150");
      ui.click("Restore profile agent calls");
      launch = resolveLaunchEnv(ui.view.form, ui.app.profiles, ui.app.catalog);
      assert.equal(launch.env.set[prefix + "AGENT_CALLS"], "7");
      assert.equal(ui.input(prefix + "TOKENS").props.placeholder, "Unlimited");
      const roleControls = ui.root().findAll(node => node.props["aria-label"] === prefix + "ORCHESTRATOR_MODEL");
      assert.equal(roleControls.length, kind === "prove" ? 1 : 0);
    } finally { ui.dispose(); }
  });
}


test("role session limits are independent, inherited, and explicitly clearable", () => {
  for (const [suffix, label] of [["PROVER_SESSIONS", "prover sessions"], ["ORCHESTRATOR_SESSIONS", "orchestrator sessions"], ["NEGATION_SESSIONS", "refutation sessions"]]) {
    const ui = fixture({ overrides: { [prefix + "AGENT_CALLS"]: "20" } }, { [prefix + suffix]: "7" });
    assert.equal(ui.control(suffix).props.value, "7");
    ui.control(suffix).props.onChange({ target: { value: "0" } });
    assert.equal(ui.environment().env.set[prefix + suffix], "0");
    assert.equal(ui.environment().env.set[prefix + "AGENT_CALLS"], "20");
    ui.control(suffix).props.onChange({ target: { value: "" } });
    assert.equal(ui.control(suffix).props.placeholder, "Unlimited");
    assert.deepEqual(ui.environment().env.unset, [prefix + suffix]);
    const reset = elements(ProverSettings()).find(item => item.props?.["aria-label"] === `Restore profile ${label}`);
    assert.ok(reset);
    reset.props.onClick();
    assert.equal(ui.environment().env.set[prefix + suffix], "7");
    assert.deepEqual(ui.environment().rejected, []);
  }
});


for (const kind of ["formalize", "review", "refactor", "golf", "draft"]) {
  test(`switching from research proving to ${kind} clears unsupported switches and preserves profile choices`, () => {
    const ui = mount();
    try {
      ui.update(state => {
        state.view.form.target = kind === "formalize" ? "paper.tex" : "Main.lean";
        state.view.form.profile = "custom";
        state.view.form.research = true;
        state.view.form.researchWorkers = 3;
        state.view.form.agents = 3;
        state.view.form.cleanRoom = true;
        state.view.form.noParallel = true;
        state.view.form.overrides = { [prefix + "MODE"]: "research", [prefix + "MODEL"]: "fixture-model" };
      });
      const savedOverrides = { ...ui.view.form.overrides };
      ui.change(ui.input("Workflow"), kind);
      assert.equal(ui.view.form.kind, kind);
      assert.equal(ui.view.form.research, false);
      assert.equal(ui.view.form.researchWorkers, null);
      assert.equal(ui.view.form.agents, 1);
      assert.equal(ui.view.form.cleanRoom, false);
      assert.equal(ui.view.form.noParallel, false);
      assert.equal(ui.view.form.profile, "custom");
      assert.deepEqual(ui.view.form.overrides, savedOverrides);
      assert.doesNotMatch(textOf(ui.root()), /Research workers|Swarm agents|Force single lane|Plan, retrieval, orchestration, dispatch/);
      assert.equal(ui.button("Start run").props.disabled, false);
      ui.click("Start run");
      assert.equal(ui.messages.at(-1).request.kind, kind);
      assert.equal(ui.messages.at(-1).request.cleanRoom, false);
      ui.change(ui.input("Workflow"), "prove");
      assert.equal(ui.input(prefix + "MODE").props.value, "research");
      assert.equal(ui.view.form.profile, "custom");
      assert.deepEqual(ui.view.form.overrides, savedOverrides);
      assert.match(textOf(ui.root()), /Force single lane/);
    } finally { ui.dispose(); }
  });
}
