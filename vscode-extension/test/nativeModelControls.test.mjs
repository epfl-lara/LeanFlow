/** Check runtime model choices and dependent resets through actual React handlers. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
import { mount, textOf } from "./helpers/webviewFixture.mjs";

const bundle = await build({ stdin: { contents: 'export * from "./src/core/providerModels";', resolveDir: process.cwd() }, bundle: true, write: false, format: "esm", platform: "node" });
const { nativeSelections, changeNativeSelection, launchModelProblems } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const prefix = "LEANFLOW_PROVER_";
function prepare() {
  const ui = mount();
  ui.update(f => {
    f.view.form.model = "";
    for (const row of f.app.providers.backends) row.model_catalog = {
      status: "ready", source: "native fixture", default_model: `${row.id}-default`, default_reasoning_effort: "medium", error: "",
      models: [
        { id: `${row.id}-default`, label: `${row.id}-default`, reasoning_known: true, reasoning_efforts: ["low", "medium", "high"], default_reasoning_effort: "medium" },
        { id: `${row.id}-fast`, label: "Fast", reasoning_known: true, reasoning_efforts: [], default_reasoning_effort: "" },
        { id: `${row.id}-unknown`, label: "Unknown capabilities", reasoning_known: false, reasoning_efforts: [], default_reasoning_effort: "" },
      ],
    };
    for (const suffix of ["PROVIDER", "ORCHESTRATOR_PROVIDER"]) f.app.catalog.groups[0].flags.push({ name: prefix + suffix, value_type: "enum", default: "", choices: ["", "codex", "claude", "lemex"], editable: true, extension_editable: true });
  });
  return ui;
}
const options = control => control.findAllByType("option").map(row => ({ value: row.props.value, label: textOf(row) }));

test("native launch defaults and only selected backend model choices are visible", () => {
  const ui = prepare();
  try {
    const choices = options(ui.input("Launch model"));
    assert.equal(choices[0].label, "Native default (lemex-default)");
    assert.deepEqual(choices.map(row => row.value), ["", "lemex-default", "lemex-fast", "lemex-unknown"]);
    ui.change(ui.input("Launch model"), "lemex-fast");
    assert.equal(ui.view.form.model, "lemex-fast");
    ui.change(ui.input("Agent backend"), "claude");
    assert.equal(ui.view.form.model, "");
    assert.equal(options(ui.input("Launch model"))[0].label, "Native default (claude-default)");
  } finally { ui.dispose(); }
});

test("model-specific effort choices omit unknown and unsupported levels", () => {
  const ui = prepare();
  try {
    assert.deepEqual(options(ui.input(prefix + "REASONING_EFFORT")).map(row => row.value), ["__default__", "low", "medium", "high"]);
    ui.change(ui.input(prefix + "REASONING_EFFORT"), "high");
    ui.change(ui.input(prefix + "MODEL"), "lemex-fast");
    assert.equal(ui.view.form.overrides[prefix + "REASONING_EFFORT"], undefined);
    assert.deepEqual(options(ui.input(prefix + "REASONING_EFFORT")).map(row => row.value), ["__default__"]);
    assert.match(textOf(ui.root()), /no configurable reasoning effort/);
    ui.change(ui.input(prefix + "MODEL"), "lemex-unknown");
    assert.match(textOf(ui.root()), /does not report reasoning choices/);
  } finally { ui.dispose(); }
});

test("backend changes mask incompatible profile values while preserving a separately configured role", () => {
  const ui = prepare();
  try {
    ui.update(f => {
      f.view.form.profile = "custom";
      f.app.profiles.profiles[1].overrides = { [prefix + "MODEL"]: "lemex-default", [prefix + "REASONING_EFFORT"]: "high", [prefix + "ORCHESTRATOR_PROVIDER"]: "codex", [prefix + "ORCHESTRATOR_MODEL"]: "codex-default", [prefix + "ORCHESTRATOR_REASONING_EFFORT"]: "low" };
    });
    ui.change(ui.input("Agent backend"), "claude");
    assert.equal(ui.view.form.overrides[prefix + "MODEL"], "");
    assert.equal(ui.view.form.overrides[prefix + "REASONING_EFFORT"], "");
    assert.equal(ui.view.form.overrides[prefix + "ORCHESTRATOR_MODEL"], undefined);
    const selected = nativeSelections(ui.view.form, ui.app.providers, ui.app.profiles.profiles[1].overrides);
    assert.equal(selected.prover.model, "claude-default");
    assert.equal(selected.orchestrator.model, "codex-default");
    assert.equal(selected.orchestrator.effort, "low");
  } finally { ui.dispose(); }
});

test("unavailable discovery explains next steps and saved invalid reasoning disables launch", () => {
  const ui = prepare();
  try {
    ui.update(f => { f.view.form.overrides[prefix + "REASONING_EFFORT"] = "ultra"; });
    assert.match(textOf(ui.root()), /ultra \(unsupported\)/);
    assert.equal(ui.button("Start proving").props.disabled, true);
    ui.change(ui.input(prefix + "REASONING_EFFORT"), "__default__");
    assert.equal(ui.button("Start proving").props.disabled, false);
    ui.update(f => { f.app.providers.backends.find(row => row.id === "lemex").model_catalog = { status: "unavailable", source: "", models: [], default_model: "", default_reasoning_effort: "", error: "Endpoint unavailable. Check Providers." }; });
    assert.match(textOf(ui.root()), /Endpoint unavailable/);
    assert.equal(options(ui.input("Launch model"))[0].label, "Native default (not reported)");
    ui.click("Refresh models");
    assert.equal(ui.messages.at(-1).type, "refreshProviders");
  } finally { ui.dispose(); }
});

test("stale model choices are visible and cannot launch against a ready catalog", () => {
  const ui = prepare();
  try {
    ui.update(f => { f.view.form.model = "retired-model"; });
    assert.ok(options(ui.input("Launch model")).some(row => row.label === "retired-model (not listed)"));
    assert.equal(ui.button("Start proving").props.disabled, true);
    ui.change(ui.input("Launch model"), "");
    assert.equal(ui.button("Start proving").props.disabled, false);
  } finally { ui.dispose(); }
});

test("global defaults are explicit and cross-backend roles use their own catalog", () => {
  const ui = prepare();
  try {
    ui.update(f => { f.app.providers.model_override = "lemex-default"; f.app.providers.reasoning_effort_override = "high"; });
    assert.equal(options(ui.input("Launch model"))[0].label, "LeanFlow default (lemex-default)");
    ui.change(ui.input(prefix + "PROVIDER"), "claude");
    const selected = nativeSelections(ui.view.form, ui.app.providers);
    assert.equal(selected.prover.model, "claude-default");
    assert.equal(selected.prover.effort, "medium");
    assert.deepEqual(launchModelProblems(ui.view.form, ui.app.providers), []);
  } finally { ui.dispose(); }
});

test("an explicit backend change uses the same reset rules from provider cards", () => {
  const ui = prepare();
  try {
    ui.update(f => { f.view.form.model = "lemex-fast"; f.view.tab = "providers"; });
    const card = ui.root().findAllByType("section").find(node => node.findAllByProps({ className: "card-title" }).some(title => textOf(title) === "Claude Code"));
    ui.click(card.findAllByType("button").find(node => textOf(node) === "Use for next run"));
    assert.equal(ui.view.form.provider, "claude");
    assert.equal(ui.view.form.model, "");
    assert.equal(ui.view.tab, "launch");
    assert.deepEqual(changeNativeSelection(ui.view.form, { provider: "claude" }, ui.app.providers).overrides, ui.view.form.overrides);
  } finally { ui.dispose(); }
});

test("different role models use their own native effort instead of an upstream override", () => {
  const ui = prepare();
  try {
    ui.update(f => {
      f.app.providers.reasoning_effort_override = "high";
      f.view.form.model = "lemex-default";
      f.view.form.overrides[prefix + "REASONING_EFFORT"] = "high";
      f.view.form.overrides[prefix + "ORCHESTRATOR_MODEL"] = "lemex-fast";
    });
    let selected = nativeSelections(ui.view.form, ui.app.providers);
    assert.equal(selected.prover.effort, "high");
    assert.equal(selected.orchestrator.explicitEffort, "");
    assert.equal(selected.orchestrator.effort, "");
    assert.deepEqual(launchModelProblems(ui.view.form, ui.app.providers), []);
    ui.change(ui.input(prefix + "MODEL"), "lemex-fast");
    selected = nativeSelections(ui.view.form, ui.app.providers);
    assert.equal(selected.prover.explicitEffort, "");
    assert.equal(selected.prover.effort, "");
  } finally { ui.dispose(); }
});

test("resolved native aliases remain valid for canonical saved model IDs", () => {
  const ui = prepare();
  try {
    ui.update(f => {
      const catalog = f.app.providers.backends.find(row => row.id === "lemex").model_catalog;
      catalog.models[0].resolved_model = "canonical-default-v2";
      catalog.default_model = "canonical-default-v2";
      catalog.default_reasoning_effort = "high";
      f.view.form.model = "canonical-default-v2";
    });
    assert.ok(options(ui.input("Launch model")).some(row => row.label === "canonical-default-v2"));
    assert.deepEqual(launchModelProblems(ui.view.form, ui.app.providers), []);
    assert.equal(nativeSelections(ui.view.form, ui.app.providers).launch.effort, "high");
    ui.change(ui.input("Launch model"), "lemex-default");
    assert.equal(nativeSelections(ui.view.form, ui.app.providers).launch.effort, "high");
  } finally { ui.dispose(); }
});

test("clean room can be selected for native proof runs and preserves the choice", () => {
  const ui = prepare();
  try {
    const checkbox = () => ui.root().findAllByType("label").find(node => textOf(node).startsWith("Clean room")).findByType("input");
    assert.equal(checkbox().props.disabled, false);
    ui.change(checkbox(), true);
    assert.equal(ui.view.form.cleanRoom, true);
    assert.equal(ui.button("Start proving").props.disabled, false);
    assert.match(textOf(ui.root()), /Block external research/);
    assert.match(textOf(ui.root()), /Existing online sessions cannot be reused/);
    ui.change(checkbox(), false);
    assert.equal(ui.view.form.cleanRoom, false);
    assert.equal(ui.button("Start proving").props.disabled, false);
    ui.change(checkbox(), true);
    ui.click("Formalization");
    assert.equal(ui.root().findAllByType("label").filter(node => textOf(node).startsWith("Clean room")).length, 0);
    assert.equal(ui.view.form.cleanRoom, false);
    ui.click("Prove");
    assert.equal(checkbox().props.checked, true);
    assert.equal(ui.view.form.cleanRoom, true);
    ui.change(checkbox(), false);
    assert.equal(ui.button("Start proving").props.disabled, false);
  } finally { ui.dispose(); }
});

test("native configured effort is distinguished from a model-specific default", () => {
  const ui = prepare();
  try {
    ui.update(f => {
      const catalog = f.app.providers.backends.find(row => row.id === "lemex").model_catalog;
      catalog.default_reasoning_effort = "high";
      catalog.models[2].default_reasoning_effort = "";
      f.view.form.model = "lemex-unknown";
    });
    assert.equal(nativeSelections(ui.view.form, ui.app.providers).prover.effort, "");
    assert.match(options(ui.input(prefix + "REASONING_EFFORT"))[0].label, /runtime chooses/);
    ui.update(f => { f.app.providers.backends.find(row => row.id === "lemex").model_catalog.configured_reasoning_effort = "high"; });
    assert.equal(nativeSelections(ui.view.form, ui.app.providers).prover.effort, "high");
    assert.match(options(ui.input(prefix + "REASONING_EFFORT"))[0].label, /high/);
    assert.deepEqual(launchModelProblems(ui.view.form, ui.app.providers), []);
  } finally { ui.dispose(); }
});
