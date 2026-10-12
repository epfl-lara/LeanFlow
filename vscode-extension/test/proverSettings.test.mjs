/** Exercise the launch controls without an extension host or any model calls. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
import { mount, textOf } from "./helpers/webviewFixture.mjs";

const bundle = await build({
  stdin: {
    contents: 'export { ProverSettings } from "./webview/views/ProverSettings"; export { GlobalLimits } from "./webview/views/GlobalLimits"; export { setFixture } from "./webview/store"; export { resolveLaunchEnv, buildWorkflowArgs } from "./src/core/launch";',
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
const { ProverSettings, GlobalLimits, setFixture, resolveLaunchEnv, buildWorkflowArgs } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

const prefix = "LEANFLOW_PROVER_";
const flags = [
  ["MODE", "enum", "research", ["standard", "research"]],
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
  ["PROVER_SESSIONS", "int", "150"],
  ["ORCHESTRATOR_SESSIONS", "int", "50"],
  ["NEGATION_SESSIONS", "int", ""],
  ["MAX_RESTARTS", "int", ""],
  ["PLAN_REFINEMENTS", "int", ""],
  ["MAX_DECOMPOSITIONS", "int", ""],
  ["MAX_NODES", "int", "128"],
].map(([suffix, value_type, fallback, choices]) => ({
  name: prefix + suffix, value_type, default: fallback, choices, summary: suffix,
  editable: true, extension_editable: true, sensitive: false,
}));
flags.push({ name: "LEANFLOW_RESEARCH_MODE", value_type: "bool", default: "0", summary: "Legacy research", editable: true, extension_editable: true, sensitive: false });

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

test("global limits show infinity and their controls remain outside the help disclosures", () => {
  const ui = fixture();
  const rendered = elements([GlobalLimits(), ProverSettings()]);
  assert.ok(rendered.some(item => item.props?.["aria-label"] === "About Global limits"));
  for (const help of elements(GlobalLimits()).filter(item => item.type === "details")) {
    assert.equal(elements(help).some(item => item.type === "input"), false);
  }
  for (const suffix of ["AGENT_CALLS", "TOKENS", "WALL_TIME_S"]) {
    assert.equal(ui.control(suffix).props.placeholder, "∞");
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

test("explicit empty mode resets Research and modern mode wins legacy state", () => {
  let ui = fixture({ overrides: { [prefix + "MODE"]: "" } }, { LEANFLOW_RESEARCH_MODE: "0" });
  assert.equal(ui.control("MODE").props.value, "research");
  assert.equal(ui.environment().env.set[prefix + "MODE"], "");
  ui = fixture({ research: true }, { [prefix + "MODE"]: "standard", LEANFLOW_RESEARCH_MODE: "1" });
  assert.equal(ui.control("MODE").props.value, "standard");
});

test("structural allowances are visible and reach the runtime without changing role selection", () => {
  const ui = fixture({ overrides: { [prefix + "MODEL"]: "my-prover" } });
  for (const suffix of ["MAX_RESTARTS", "PLAN_REFINEMENTS", "MAX_DECOMPOSITIONS"]) {
    ui.control("MODE").props.onChange({ target: { value: suffix === "MAX_RESTARTS" ? "standard" : "research" } });
    assert.equal(ui.control(suffix).props.placeholder, "∞");
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
    const ui = fixture({ overrides: { [prefix + "MODE"]: suffix === "MAX_RESTARTS" ? "standard" : "research" } }, { [prefix + suffix]: "7" });
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
      assert.equal(ui.root().findAllByProps({ className: "card-title" }).filter(node => textOf(node).startsWith("Global limits")).length, 1);
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
      assert.equal(ui.input(prefix + "TOKENS").props.placeholder, "∞");
      const roleControls = ui.root().findAll(node => node.props["aria-label"] === prefix + "ORCHESTRATOR_MODEL");
      assert.equal(roleControls.length, kind === "prove" ? 1 : 0);
    } finally { ui.dispose(); }
  });
}


test("role session limits are independent, inherited, and explicitly clearable", () => {
  for (const [suffix, label] of [["PROVER_SESSIONS", "prover calls"], ["ORCHESTRATOR_SESSIONS", "orchestrator calls"], ["NEGATION_SESSIONS", "refutation calls"]]) {
    const ui = fixture({ overrides: { [prefix + "AGENT_CALLS"]: "20" } }, { [prefix + suffix]: "7" });
    assert.equal(ui.control(suffix).props.value, "7");
    ui.control(suffix).props.onChange({ target: { value: "0" } });
    assert.equal(ui.environment().env.set[prefix + suffix], "0");
    assert.equal(ui.environment().env.set[prefix + "AGENT_CALLS"], "20");
    ui.control(suffix).props.onChange({ target: { value: "" } });
    assert.equal(ui.control(suffix).props.placeholder, "∞");
    assert.equal(ui.environment().env.set[prefix + suffix], "");
    assert.deepEqual(ui.environment().env.unset, []);
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


test("Research, bottom-up and role call defaults come from the current catalog", () => {
  const ui = fixture();
  assert.equal(ui.control("MODE").props.value, "research");
  const order = ui.control("SEARCH_ORDER");
  assert.equal(elements(order).find(item => item.type === "option" && item.props.value === "__default__").props.children.join(""), "default (bottom-up)");
  assert.equal(ui.control("PROVER_SESSIONS").props.value, "150");
  assert.equal(ui.control("ORCHESTRATOR_SESSIONS").props.value, "50");
  assert.deepEqual(ui.environment().env.set, {});
});

test("an explicit empty role allowance is Unlimited rather than restoring a finite default", () => {
  const ui = fixture();
  for (const suffix of ["PROVER_SESSIONS", "ORCHESTRATOR_SESSIONS"]) {
    ui.control(suffix).props.onChange({ target: { value: "" } });
    assert.equal(ui.control(suffix).props.value, "");
    assert.equal(ui.control(suffix).props.placeholder, "∞");
    assert.equal(ui.environment().env.set[prefix + suffix], "");
    assert.ok(!ui.environment().env.unset.includes(prefix + suffix));
  }
  assert.deepEqual(ui.environment().rejected, []);
  const saved = fixture({}, { [prefix + "PROVER_SESSIONS"]: "", [prefix + "ORCHESTRATOR_SESSIONS"]: "" });
  assert.deepEqual(saved.environment().rejected, []);
  assert.equal(saved.environment().env.set[prefix + "PROVER_SESSIONS"], "");
  assert.equal(saved.environment().env.set[prefix + "ORCHESTRATOR_SESSIONS"], "");
});

test("legacy research false is Standard while empty modern mode explicitly resets Research", () => {
  const ui = fixture({}, { LEANFLOW_RESEARCH_MODE: "false" });
  assert.equal(ui.control("MODE").props.value, "standard");
  ui.control("MODE").props.onChange({ target: { value: "" } });
  assert.equal(ui.control("MODE").props.value, "research");
  assert.equal(ui.environment().env.set[prefix + "MODE"], "");
});

test("mode changes hide the appropriate panels and preserve all research choices", () => {
  const ui = mount();
  try {
    ui.update(state => {
      state.app.catalog.groups[0].flags = flags;
      state.view.form.overrides = { [prefix + "PROVER_SESSIONS"]: "88", [prefix + "ORCHESTRATOR_SESSIONS"]: "22", [prefix + "PARALLELISM"]: "4", [prefix + "PLAN_REFINEMENTS"]: "5" };
    });
    const shared = { ...ui.view.form.overrides };
    const panels = () => ui.root().findAllByType("section").filter(node => ["Prover settings", "Orchestrator settings", "Research and recovery limits"].includes(node.props["aria-label"]));
    assert.equal(panels().length, 3);
    const help = ui.root().findByProps({ "aria-label": "About Research and Standard modes" });
    assert.equal(help.props["aria-label"], "About Research and Standard modes");
    assert.equal(help.parent.type, "details");
    assert.equal(help.parent.props.open, undefined);
    assert.match(textOf(help.parent), /parallel provers/);
    assert.match(textOf(help.parent), /one prover at a time/);
    // Native summary semantics provide click, Enter, and Space toggling without a hover-only handler.
    assert.equal(help.props.tabIndex, undefined);
    ui.change(ui.input(prefix + "MODE"), "standard");
    assert.equal(panels().length, 0);
    assert.equal(ui.root().findAll(node => node.props["aria-label"] === prefix + "PARALLELISM").length, 0);
    assert.ok(ui.input(prefix + "MAX_RESTARTS"));
    assert.ok(ui.input(prefix + "SEARCH_ORDER"));
    for (const [name, value] of Object.entries(shared)) assert.equal(ui.view.form.overrides[name], value);
    ui.change(ui.input(prefix + "MODE"), "research");
    assert.equal(panels().length, 3);
    assert.equal(ui.input(prefix + "PROVER_SESSIONS").props.value, "88");
    assert.equal(ui.input(prefix + "ORCHESTRATOR_SESSIONS").props.value, "22");
    assert.equal(ui.input(prefix + "PARALLELISM").props.value, "4");
    assert.equal(ui.input(prefix + "PLAN_REFINEMENTS").props.value, "5");
    assert.equal(ui.root().findAll(node => node.props["aria-label"] === prefix + "MAX_RESTARTS").length, 0);
  } finally { ui.dispose(); }
});


test("cleared legacy mode and stale legacy launch switches match the displayed mode", () => {
  let ui = fixture({ overrides: { LEANFLOW_RESEARCH_MODE: "" } }, { LEANFLOW_RESEARCH_MODE: "false" });
  assert.equal(ui.control("MODE").props.value, "research");
  assert.deepEqual(ui.environment().env.unset, ["LEANFLOW_RESEARCH_MODE"]);
  ui = fixture({ agents: 3 }, { LEANFLOW_RESEARCH_MODE: "false" });
  assert.equal(ui.control("MODE").props.value, "research");
  assert.ok(buildWorkflowArgs({ target: "", provider: "", model: "", additionalSkills: [], prompt: "", axioms: "", ...ui.form }, ui.environment().env.set).includes("--agents"));
  ui = fixture({ agents: 3, noParallel: true }, { LEANFLOW_RESEARCH_MODE: "false" });
  assert.equal(ui.control("MODE").props.value, "standard");
  ui = fixture({ research: true }, { LEANFLOW_RESEARCH_MODE: "false" });
  assert.equal(ui.control("MODE").props.value, "research");
});

test("an empty modern mode saved in a profile resets Research without legacy arguments", () => {
  const ui = fixture({ research: true, agents: 4 }, { [prefix + "MODE"]: "", LEANFLOW_RESEARCH_MODE: "false" });
  assert.equal(ui.control("MODE").props.value, "research");
  assert.deepEqual(ui.environment().rejected, []);
  assert.equal(ui.environment().env.set[prefix + "MODE"], "");
  assert.deepEqual(buildWorkflowArgs({ target: "", provider: "", model: "", additionalSkills: [], prompt: "", axioms: "", ...ui.form }, ui.environment().env.set), ["prove", "--provider", "lemex"]);
});


test("unfamiliar launch terms have accessible help outside the input labels", () => {
  const ui = mount();
  try {
    ui.update(state => { state.app.catalog.groups[0].flags = flags; });
    for (const term of ["Global limits", "Agent calls", "Dependency order", "Concurrent prover jobs", "Prover calls", "Orchestrator calls", "Refutation calls", "Recovery decisions", "Maximum graph nodes", "Clean room", "Profile", "Agent backend"]) {
      const summary = ui.root().findByProps({"aria-label":`About ${term}`});
      assert.equal(summary.type, "summary");
      assert.equal(summary.parent.type, "details");
      assert.equal(summary.parent.props.open, undefined);
      for (let ancestor = summary.parent; ancestor; ancestor = ancestor.parent) assert.notEqual(ancestor.type, "label");
      assert.ok(textOf(summary.parent).length > 30);
    }
    const calls = textOf(ui.root().findByProps({"aria-label":"About Agent calls"}).parent);
    for (const command of ["codex exec", "claude -p", "lemex exec"]) assert.ok(calls.includes(command));
    assert.match(calls, /many model requests/);
    assert.match(calls, /internal retries/);
    assert.match(calls, /new process.*resume.*counts again/i);
    assert.equal(ui.messages.length, 0);
  } finally { ui.dispose(); }
});


test("live limits distinguish infinity, explicit zero and unknown usage", () => {
  const ui = mount("live");
  try {
    ui.update(state => {
      const snapshot = state.proverStates["run-1"].snapshot;
      Object.assign(snapshot.config, {native_backend:true,agent_calls:null,tokens:0,wall_time_s:null});
      Object.assign(snapshot.metrics, {agent_calls:0,agent_call_limit:null,token_limit:0,input_tokens:null,output_tokens:null,role_sessions:[{role:"prover",used:0,reserved:0,limit:0}]});
    });
    const value = label => {
      const stat = ui.root().findAllByProps({className:"stat"}).find(node => textOf(node.findByProps({className:"k"})).startsWith(label));
      assert.ok(stat, `Missing ${label}`);
      return textOf(stat.find(node => typeof node.props.className === "string" && node.props.className.split(" ").includes("v")));
    };
    assert.equal(value("Agent calls / limit"), "0 / ∞");
    assert.equal(value("Prover calls / limit"), "0 / 0");
    assert.equal(value("Global token limit"), "0");
    assert.equal(value("Reported input tokens"), "—");
    assert.ok(ui.root().findByProps({"aria-label":"About Agent calls / limit"}));
    assert.match(textOf(ui.root()), /One process = one agent call/);
    assert.match(textOf(ui.root()), /neither the agent-call budget nor a reliable count of model API requests/);
  } finally { ui.dispose(); }
});
