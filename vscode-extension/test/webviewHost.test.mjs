/** Verify the editor side of every button message using a disposable project. */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { mkdtemp, writeFile, readFile, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";

const bundle = await build({
  stdin: { contents: 'export { WebviewHost } from "./src/panels/webviewHost"; export { parseWebviewMessage } from "./src/panels/messageSchema"; export { emptyLaunchRequest } from "./src/core/types"; export { normalizeProverSnapshot } from "./src/core/prover";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "inert-editor-services", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      const f = () => globalThis.hostFixture;
      export const Uri = { file: fsPath => ({ fsPath }), parse: value => value };
      export class Range { constructor(...args) { this.args = args; } }
      export const commands = { executeCommand: async (...args) => f().calls.push(['command', ...args]) };
      export const env = { clipboard: { writeText: async text => { if (f().clipboardError) throw new Error('Clipboard unavailable'); f().calls.push(['clipboard', text]); } } };
      export const window = {
        showWarningMessage: async () => f().confirmation,
        showOpenDialog: async () => f().picked,
        showTextDocument: async (...args) => f().calls.push(['document', ...args]),
      };
    ` }));
    builder.onLoad({ filter: /src\/core\/cli\.ts$/ }, () => ({ contents: `
      const record = (name, args) => globalThis.hostFixture.calls.push([name, ...args]);
      export class CliError extends Error {}
      export async function fetchEventDetail(...args) { record('detail', args); return { event: {}, evidence: {} }; }
      export async function fetchProfileDiff(...args) { record('diff', args); return []; }
      export async function previewLaunch(...args) { record('preview', args); return { workflow: 'prove', provider: 'lemex' }; }
      export async function sendProverMessage(...args) { record('guidance', args); }
    ` }));
  } }],
});
const { WebviewHost, parseWebviewMessage, emptyLaunchRequest, normalizeProverSnapshot } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const exercised = new Set();

async function fixture() {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), "leanflow-buttons-")));
  for (const file of ["Main.lean", "baseline.lean", "PLAN.md"]) await writeFile(path.join(root, file), "fixture\n");
  const f = globalThis.hostFixture = { root, calls: [], replies: [], picked: [], confirmation: undefined };
  const disposable = { dispose() {} };
  const record = name => async (...args) => { f.calls.push([name, ...args]); };
  const snapshot = normalizeProverSnapshot({ run_id: "run-1", plan_path: "PLAN.md", changes: [{ path: "Main.lean", baseline_path: "baseline.lean" }] }, "run-1");
  const services = {
    project: { found: true, root }, profiles: null,
    catalog: { groups: [{ flags: [{ name: "LEANFLOW_PROVER_PARALLELISM", value_type: "int", editable: true, extension_editable: true, minimum: 1, default: "1" }] }] },
    onDidChange: () => disposable, state: () => ({ project: { found: true, root } }), reload: record("refresh"), reloadProfiles: record("profiles"), selectRun: record("select"), openRun: record("openRun"),
    setup: { start: record("setup"), cancel: record("cancelSetup"), showLog: record("setupLog"), assertLaunchReady: () => { if (f.setupBlocked) throw new Error("Project setup is incomplete. Retry setup."); } },
    providers: { refresh: record("providers"), act: record("providerAction"), state: () => f.providerState },
    runs: { openTerminal: record("terminal"), onDidAppendEvents: () => disposable, projectRootForRun: id => id === "run-1" ? root : null,
      launch: async (...args) => { f.calls.push(["launch", ...args]); return { id: "tracked-1", runId: "run-1", label: "Test run" }; },
      stop: async id => { f.calls.push(["stop", id]); return { stopped: true }; }, loadEvents: async () => [], runLog: async () => "test log",
    },
    prover: { load: async () => ({ snapshot, error: "" }), cached: () => snapshot },
    experiments: { pendingCellCount: () => f.pending ?? 1, create: record("create"), start: record("start"), stop: record("stopSweep"), delete: record("deleteSweep"), export: async () => path.join(root, "results.json") },
  };
  const host = new WebviewHost({ onDidReceiveMessage: callback => { f.receive = callback; return disposable; }, postMessage: value => f.replies.push(value) }, services);
  return { ...f, services, f,
    async send(message) { const parsed = parseWebviewMessage(message); assert.equal(parsed.ok, true, JSON.stringify(message)); exercised.add(message.type); await host.handle(parsed.message); },
    last(type) { return f.replies.filter(reply => reply.type === type).at(-1); },
    async dispose() { host.dispose(); await rm(root, { recursive: true, force: true }); },
  };
}

test("navigation, clipboard and provider actions reach native editor services", async () => {
  const ui = await fixture();
  try {
    for (const type of ["ready", "refresh", "refreshProviders", "openDashboard", "openCliSettings", "setupProject", "retryProjectSetup", "repairProjectSetup", "cancelProjectSetup", "showProjectSetupLog", "configureProvider"]) await ui.send({ type });
    assert.equal(ui.last("state").state.project.root, ui.root);
    assert.deepEqual(ui.calls.filter(row => row[0] === "setup"), [["setup"], ["setup", true], ["setup", false, true]]);
    assert.ok(ui.calls.some(row => row[0] === "cancelSetup"));
    assert.ok(ui.calls.some(row => row[0] === "setupLog"));
    assert.ok(ui.calls.some(row => row[0] === "command" && row[1] === "leanflow.configureProvider"));
    for (const backend of ["codex", "claude", "lemex"]) await ui.send({ type: "providerAction", backend, action: "apiKey" });
    for (const topic of ["install", "project-init"]) await ui.send({ type: "openExternalDoc", topic });
    assert.ok(ui.calls.some(row => row[0] === "command" && row[1] === "leanflow.openDashboard"));
    assert.ok(ui.calls.some(row => row[0] === "command" && row[1] === "workbench.action.openSettings"));
    await ui.send({ type: "copyText", text: "theorem example := by\n  simp" });
    assert.deepEqual(ui.calls.at(-1), ["clipboard", "theorem example := by\n  simp"]);
    assert.equal(ui.last("notify").message, "Copied to clipboard.");
    ui.f.clipboardError = true;
    await ui.send({ type: "copyText", text: "example" });
    assert.equal(ui.last("notify").level, "error");
    assert.equal(parseWebviewMessage({ type: "copyText", text: {} }).ok, false);
    assert.equal(parseWebviewMessage({ type: "copyText", text: "x".repeat(4 * 1024 * 1024 + 1) }).ok, false);
  } finally { await ui.dispose(); }
});

test("launch, preview, stop, events, transcripts and guidance bind to the requested run", async () => {
  const ui = await fixture();
  try {
    const request = { ...emptyLaunchRequest(), target: "Main.lean", provider: "lemex" };
    await ui.send({ type: "launch", request });
    assert.equal(ui.calls.find(row => row[0] === "launch")[1].target, "Main.lean");
    ui.f.setupBlocked = true;
    await ui.send({ type: "launch", request });
    assert.equal(ui.calls.filter(row => row[0] === "launch").length, 1);
    assert.match(ui.last("notify").message, /Project setup is incomplete/);
    ui.f.setupBlocked = false;
    await ui.send({ type: "preview", requestId: "preview-1", request });
    assert.equal(ui.last("preview").error, "");
    await ui.send({ type: "stopRun", id: "tracked-1" });
    await ui.send({ type: "selectRun", id: "tracked-1" });
    await ui.send({ type: "openRun", id: "tracked-1" });
    await ui.send({ type: "openRunTerminal", id: "tracked-1" });
    assert.ok(ui.calls.some(row => row[0] === "openRun" && row[1] === "tracked-1"));
    assert.ok(ui.calls.some(row => row[0] === "terminal" && row[1] === "tracked-1"));
    for (const type of ["loadEvents", "loadRunLog", "loadProver"]) await ui.send({ type, runId: "run-1" });
    assert.equal(ui.last("runLog").text, "test log");
    assert.equal(ui.last("proverState").snapshot.run_id, "run-1");
    await ui.send({ type: "loadEventDetail", runId: "run-1", eventId: "event-1" });
    assert.deepEqual(ui.calls.find(row => row[0] === "detail"), ["detail", ui.root, "run-1", "event-1"]);
    await ui.send({ type: "proverMessage", runId: "run-1", agentId: "prover-1", message: "Try simp", requestId: "guidance-1" });
    assert.equal(ui.last("proverMessageResult").success, true);
    await ui.send({ type: "loadEventDetail", runId: "missing", eventId: "event-1" });
    assert.match(ui.last("eventDetail").error, /recorded history/);
    await ui.send({ type: "proverMessage", runId: "missing", agentId: "prover-1", message: "Try simp" });
    assert.equal(ui.last("proverMessageResult").success, false);
  } finally { await ui.dispose(); }
});

test("launch permits clean-room proof runs and refuses unsupported efforts or workflows", async () => {
  const ui = await fixture();
  try {
    const request = { ...emptyLaunchRequest(), provider: "lemex", model: "fixture-model", cleanRoom: true };
    await ui.send({ type: "launch", request: { ...request, kind: "golf" } });
    assert.equal(ui.calls.filter(row => row[0] === "launch").length, 0);
    assert.match(ui.last("notify").message, /only supported for prove/);
    ui.f.providerState = { selected: "lemex", error: "", backends: [{ id: "lemex", model_catalog: { status: "ready", default_model: "fixture-model", models: [{ id: "fixture-model", reasoning_known: true, reasoning_efforts: ["low"] }] } }] };
    await ui.send({ type: "launch", request: { ...request, overrides: { LEANFLOW_PROVER_REASONING_EFFORT: "ultra" } } });
    assert.equal(ui.calls.filter(row => row[0] === "launch").length, 0);
    assert.match(ui.last("notify").message, /does not support reasoning effort ultra/);
    await ui.send({ type: "launch", request: { ...request, overrides: { LEANFLOW_PROVER_REASONING_EFFORT: "low" } } });
    assert.equal(ui.calls.filter(row => row[0] === "launch").length, 1);
    assert.equal(ui.calls.find(row => row[0] === "launch")[1].cleanRoom, true);
  } finally { await ui.dispose(); }
});

test("browse, artifact opening and diffs use real files inside the disposable project", async () => {
  const ui = await fixture();
  try {
    ui.f.picked = [{ fsPath: path.join(ui.root, "Main.lean") }];
    await ui.send({ type: "pickTarget", kind: "prove" });
    assert.equal(ui.last("targetPicked").path, "Main.lean");
    await ui.send({ type: "openPath", path: "Main.lean" });
    await ui.send({ type: "openProverFile", runId: "run-1", path: "PLAN.md", line: 2 });
    assert.equal(ui.calls.at(-1)[1].fsPath, path.join(ui.root, "PLAN.md"));
    await ui.send({ type: "openProverFile", runId: "run-1", path: "Main.lean", baselinePath: "baseline.lean" });
    assert.equal(ui.calls.at(-1)[1], "vscode.diff");
    await ui.send({ type: "openProverFile", runId: "run-1", path: "unauthorized.txt" });
    assert.match(ui.last("notify").message, /not part of/);
    await ui.send({ type: "openPath", path: "../outside" });
    assert.match(ui.last("notify").message, /inside/);
  } finally { await ui.dispose(); }
});

test("profile buttons write, compare and delete only disposable project profiles", async () => {
  const ui = await fixture();
  try {
    await ui.send({ type: "saveProfile", profile: { name: "audit", summary: "Test", builtin: false, overrides: { LEANFLOW_PROVER_PARALLELISM: "2" } } });
    const saved = path.join(ui.root, ".leanflow/flag-profiles/audit.json");
    assert.equal(JSON.parse(await readFile(saved, "utf8")).overrides.LEANFLOW_PROVER_PARALLELISM, "2");
    await ui.send({ type: "diffProfiles", requestId: "diff-1", left: "default", right: "audit" });
    assert.equal(ui.last("diff").error, "");
    await ui.send({ type: "deleteProfile", name: "audit" });
    await assert.rejects(readFile(saved), { code: "ENOENT" });
  } finally { await ui.dispose(); }
});

test("sweep actions dispatch once and cancelling a large sweep never starts work", async () => {
  const ui = await fixture();
  try {
    const matrix = { id: "sweep-1", name: "audit", kind: "prove", targets: [], profiles: [], models: [], repeats: 1, base: { ...emptyLaunchRequest(), provider: "lemex", model: "fixture-model" }, createdAt: new Date().toISOString() };
    await ui.send({ type: "createExperiment", matrix });
    for (const type of ["startExperiment", "stopExperiment", "exportExperiment", "deleteExperiment"]) await ui.send({ type, matrixId: matrix.id });
    assert.equal(ui.calls.filter(row => row[0] === "start").length, 1);
    ui.f.pending = 11;
    await ui.send({ type: "startExperiment", matrixId: matrix.id });
    assert.equal(ui.calls.filter(row => row[0] === "start").length, 1);
    ui.f.confirmation = "Start sweep";
    await ui.send({ type: "startExperiment", matrixId: matrix.id });
    assert.equal(ui.calls.filter(row => row[0] === "start").length, 2);
  } finally { await ui.dispose(); }
});

after(async () => {
  const source = await readFile("src/panels/webviewHost.ts", "utf8");
  const actions = [...source.matchAll(/case "([^"]+)":/g)].map(match => match[1]);
  assert.deepEqual(actions.filter(action => !exercised.has(action)), [], "Every host action needs a functional check");
  console.log(`Exercised all ${actions.length} webview host actions.`);
});
