/** Render real webview components with inert host services and observable effects. */
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import path from "node:path";
import React from "react";
import Renderer, { act } from "react-test-renderer";
import { build } from "esbuild";

const output = path.resolve("dist/test/webviewFixture.mjs");
const bundle = await build({
  stdin: { contents: `export { App } from './webview/App'; export { SidebarView } from './webview/views/SidebarView'; export { emptyLaunchRequest } from './src/core/types'; export { normalizeProverSnapshot } from './src/core/prover'; export { parseWebviewMessage } from './src/panels/messageSchema';`, resolveDir: process.cwd(), loader: "tsx" },
  bundle: true, write: false, format: "esm", platform: "node", jsx: "automatic", jsxDev: true,
  external: ["react", "react-dom"],
  plugins: [{ name: "inert-webview-host", setup(builder) {
    builder.onLoad({ filter: /pdfjs-dist\/legacy\/build\/pdf\.mjs$/ }, () => ({ contents: `
      export class PDFWorker { static create() { return {destroy(){}}; } }
      export class TextLayer { render() { return Promise.resolve(); } cancel() {} }
      export function getDocument(options) {
        globalThis.webviewFixture.pdfOptions = options;
        return { destroy(){}, promise: Promise.resolve({numPages:2, getPageLabels:async()=>['i','1'], getPage:async()=>({
          rotate:0, view:[0,0,100,100], getViewport:({scale})=>({width:100*scale,height:100*scale,scale}),
          render:()=>({promise:Promise.resolve(),cancel(){}}),getTextContent:async()=>({items:[]})
        })}) };
      }
    ` }));
    builder.onResolve({ filter: /^react-dom$/ }, () => ({ path: "react-dom", namespace: "portal-fixture" }));
    // React's test renderer has no DOM. Preserve portal children in its logical
    // tree; the real browser audit checks the detached DOM placement.
    builder.onLoad({ filter: /.*/, namespace: "portal-fixture" }, () => ({ contents: "export const createPortal = children => children;" }));
    builder.onLoad({ filter: /webview\/store\.tsx$/ }, () => ({ contents: `
      export const useStore = () => globalThis.webviewFixture;
      export const useSelectedRun = () => globalThis.webviewFixture.app.runs.find(run => run.id === globalThis.webviewFixture.app.selectedRunId) ?? null;
      export const eventDetailKey = (run, event) => run + ':' + event;
    ` }));
    builder.onLoad({ filter: /webview\/vscodeApi\.ts$/ }, () => ({ contents: `
      export const mode = 'panel';
      export const post = message => globalThis.webviewFixture.post(message);
    ` }));
  } }],
});
await mkdir(path.dirname(output), { recursive: true });
await writeFile(output, bundle.outputFiles[0].text);
export const { App, SidebarView, emptyLaunchRequest, normalizeProverSnapshot, parseWebviewMessage } = await import(pathToFileURL(output));
export { act };
export const clickedButtons = new Set();

export function textOf(node) {
  if (typeof node === "string" || typeof node === "number") return String(node);
  return (node?.children ?? []).map(textOf).join("");
}

/** Give every native side effect a recording substitute; never start an actual run. */
export function mount(tab = "launch", patch = {}, component = App) {
  const listener = new EventTarget();
  globalThis.window = listener;
  listener.innerWidth = 800;
  listener.innerHeight = 600;
  const effects = [];
  const node = { style: { setProperty() {} }, replaceChildren() {}, getContext: () => ({}), getBoundingClientRect: () => ({ left: 30, top: 40, bottom: 60, height: 120 }), contains: () => false, querySelector: () => ({ focus() {} }), clientWidth: 800, scrollHeight: 900, scrollTop: 0, scrollTo: (...args) => effects.push(["scroll", ...args]), scrollIntoView: () => effects.push(["focus"]) };
  globalThis.document = { getElementById: () => node };
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  const now = new Date().toISOString();
  const flags = [
    { name: "LEANFLOW_PROVER_MODE", value_type: "enum", default: "research", choices: ["standard", "research"], ablatable: true },
    { name: "LEANFLOW_PROVER_PARALLELISM", value_type: "int", default: "1", minimum: 1, maximum: 16, ablatable: true },
    ...["PROVIDER", "ORCHESTRATOR_PROVIDER"].map(suffix => ({ name: `LEANFLOW_PROVER_${suffix}`, value_type: "enum", default: "", choices: ["", "codex", "claude", "lemex"] })),
    { name: "LEANFLOW_PROVER_MODEL", value_type: "string", default: "" },
    { name: "LEANFLOW_PROVER_ORCHESTRATOR_MODEL", value_type: "string", default: "" },
    { name: "LEANFLOW_PROVER_REASONING_EFFORT", value_type: "string", default: "" },
    { name: "LEANFLOW_PROVER_ORCHESTRATOR_REASONING_EFFORT", value_type: "string", default: "" },
    { name: "LEANFLOW_INTERNAL_TEST", value_type: "bool", default: "0", kind: "internal", extension_editable: false },
  ].map(flag => ({ group: "Prover", kind: "tuning", summary: flag.name, editable: true, extension_editable: true, ...flag }));
  const form = { ...emptyLaunchRequest(), provider: "lemex", model: "fixture-model" };
  const snapshot = normalizeProverSnapshot({
    run_id: "run-1", mode: "research", phase: "proving", terminal: false, updated_at: now,
    config: { native_backend: true, provider: "lemex" },
    plan_path: "PLAN.md", dag_path: "dag.json", plan_markdown: "# Proof plan\nProve the helper, then the goal.",
    dag: { roots: ["goal"], nodes: [
      { id: "goal", name: "goal", file: "Main.lean", line_start: 7, dependencies: ["helper"], status: "proving" },
      { id: "helper", name: "helper", file: "Helper.lean", status: "proved" },
    ] },
    jobs: [{ id: "job-1", agent_id: "prover-1", node_id: "goal", status: "running", role: "prover", started_at: now, log_path: "job.log", scratch_path: "Scratch.lean", result_path: "result.json", report_path: "report.json", artifacts: ["artifact.txt"] }],
    changes: [{ path: "Main.lean", baseline_path: "baseline/Main.lean", job_id: "job-1", agent_id: "prover-1" }],
  }, "run-1");
  let renderer;
  const fixture = {
    app: { project: { found: true, root: "/fixture", label: "Button audit" }, cli: { ok: true, version: "0.3.0" },
      catalog: { count: flags.length, groups: [{ name: "Prover", flags }] },
      profiles: { profiles: [{ name: "default", builtin: true, summary: "Default", overrides: {} }, { name: "custom", builtin: false, summary: "Custom", overrides: { LEANFLOW_PROVER_PARALLELISM: "2" } }] },
      providers: { selected: "lemex", host: "local", checking: false, error: "", backends: ["codex", "claude", "lemex"].map(id => ({ id, installed: true, version: "1.0", auth: "native_login", savedKey: false, config_path: `/${id}/config`, executable: `/bin/${id}`,
        model_catalog: { status: "ready", source: "Native runtime", error: "", default_model: id === "lemex" ? "fixture-model" : `${id}-model`, default_reasoning_effort: "high", models: [
          { id: id === "lemex" ? "fixture-model" : `${id}-model`, label: `${id} model`, reasoning_known: true, reasoning_efforts: ["low", "high"], default_reasoning_effort: "high" },
          { id: `${id}-fast`, label: `${id} fast`, reasoning_known: true, reasoning_efforts: [], default_reasoning_effort: "" },
        ] },
      })) },
      runs: [{ id: "tracked-1", runId: "run-1", label: "Test run", request: form, status: "running", startedAt: now, command: "leanflow workflow prove", appliedOverrides: { set: {}, unset: [] }, pid: 1 }],
      selectedRunId: "tracked-1", history: [], experiments: [], leanFiles: ["Main.lean"], eventTypes: [{ type: "agent-output", count: 1 }, { type: "agent-reasoning", count: 1 }], liveStatus: null,
    },
    view: { tab, form, logAgent: "", logRaw: false, logFilter: [], logSearch: "", knobSearch: "", knobAblatableOnly: false, knobProfile: "", collapsedGroups: [], diffLeft: "default", diffRight: "custom" },
    preview: { plan: null, error: "", loading: false }, diff: { rows: [], error: "", loading: false },
    toasts: [], events: { "run-1": ["agent-output", "agent-reasoning"].map((type, i) => ({ type, event_id: `event-${i}`, run_id: "run-1", agent_id: "prover-1", timestamp: now, message: `${type} message`, details: { job_id: "job-1", content_preview: `${type} full text` } })) },
    eventDetails: {}, proverStates: { "run-1": { snapshot, error: "" } }, runLog: "fixture log", runLogRunId: "run-1",
    messages: [], effects,
    post(message) { assert.equal(parseWebviewMessage(message).ok, true, `Rejected UI message: ${JSON.stringify(message)}`); fixture.messages.push(message); },
    setForm(value) { fixture.view = { ...fixture.view, form: { ...fixture.view.form, ...value } }; fixture.refresh(); },
    setView(value) { fixture.view = { ...fixture.view, ...value }; fixture.refresh(); },
    dismissToast(id) { fixture.toasts = fixture.toasts.filter(row => row.id !== id); fixture.refresh(); },
    requestPreview(request) { fixture.post({ type: "preview", requestId: "preview-1", request }); },
    requestDiff(left, right) { fixture.post({ type: "diffProfiles", requestId: "diff-1", left, right }); },
    requestEventDetail(runId, eventId) { fixture.post({ type: "loadEventDetail", runId, eventId }); },
    refresh() { renderer?.update(React.createElement(component)); },
    ...patch,
  };
  globalThis.webviewFixture = fixture;
  act(() => { renderer = Renderer.create(React.createElement(component), { createNodeMock: () => node }); });
  fixture.renderer = renderer;
  fixture.root = () => renderer.root;
  fixture.buttons = () => renderer.root.findAllByType("button");
  fixture.button = (label) => {
    const matches = fixture.buttons().filter(button => typeof label === "string" ? textOf(button) === label || button.props["aria-label"] === label || button.props.title === label : label?.test(textOf(button)));
    assert.equal(matches.length, 1, `Expected one button ${label}, found ${matches.length}`);
    return matches[0];
  };
  fixture.click = (labelOrButton) => {
    const button = labelOrButton?.props ? labelOrButton : fixture.button(labelOrButton);
    assert.ok(!button.props.disabled, `Button disabled: ${textOf(button)}`);
    const source = button._fiber._debugSource;
    if (source) clickedButtons.add(`${source.fileName}:${source.lineNumber}`);
    act(() => button.props.onClick());
  };
  fixture.change = (element, value) => act(() => element.props.onChange({ target: { value, checked: Boolean(value) } }));
  fixture.help = (label, open = true) => {
    const summary = fixture.root().findByProps({ "aria-label": `About ${label}` });
    act(() => summary.parent.props.onToggle({ currentTarget: { open } }));
    return summary.parent;
  };
  fixture.input = (placeholder) => renderer.root.find(node => node.props.placeholder === placeholder || node.props["aria-label"] === placeholder);
  fixture.update = (fn) => act(() => { fn(fixture); fixture.refresh(); });
  fixture.dispose = () => act(() => renderer.unmount());
  return fixture;
}
