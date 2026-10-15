/** Host actions validate revisions and artifact identities before source access or proving. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, writeFile, readFile, rm, realpath } from "node:fs/promises";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";
import { formalizationFixture } from "./helpers/formalizationFixture.mjs";
import { formalizationConflictFixture } from "./helpers/formalizationConflictFixture.mjs";

const bundle = await build({
  stdin: { contents: 'export { FormalizationHost } from "./src/panels/formalizationHost";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "formalization-editor-fixture", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      const f = () => globalThis.formalizationHostFixture;
      export const Uri = { file: fsPath => ({ fsPath }), from: parts => ({ ...parts, toString: () => parts.scheme + '://' + parts.authority + parts.path }) };
      export const languages = { setTextDocumentLanguage: async (document, language) => { f().opened.language = language; return { ...document, languageId: language }; } };
      export const env = {remoteName:undefined};
      export const ViewColumn = { Beside: 2 };
      export class Range { constructor(...args) { this.args = args; } }
      export class RelativePattern { constructor(base, pattern) { this.base = base; this.pattern = pattern; } }
      export const workspace = {
        get workspaceFolders(){ return f().folders ?? []; },
        getWorkspaceFolder(){ return f().folders?.[0]; },
        getConfiguration(){ return {get:(name, fallback)=>name==='cliPath' ? f().executable ?? fallback : fallback}; },
        registerTextDocumentContentProvider: (scheme, provider) => { (f().documentProviders ??= new Map()).set(scheme, provider); return { dispose() { f().documentProviders.delete(scheme); } }; },
        openTextDocument: async uri => { const content = f().documentProviders.get(uri.scheme).provideTextDocumentContent(uri); f().opened = { uri, content }; return { uri, lineCount: content.split('\\n').length }; },
      };
      export const window = {
        onDidChangeTextEditorSelection: callback => { f().selection = callback; return {dispose(){}}; },
        showOpenDialog: async options => { f().dialog = options; f().onDialog?.(); return f().pickerResult ? await f().pickerResult : f().picked; },
        showTextDocument: async (...args) => f().shown = args,
      };
    ` }));
  } }],
});
const { FormalizationHost } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

async function fixture() {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), "formalization-host-")));
  const source = path.join(root, "paper.tex");
  const content = "Source\nDefine doubling.\n";
  await writeFile(source, content);
  const f = globalThis.formalizationHostFixture = { snapshot: formalizationFixture(), calls: [], replies: [], picked: [{ fsPath: source }], root };
  const service = {
    workspaceRoot() { return root; },
    async list() { return [f.snapshot]; },
    async show(id) { f.calls.push(["show", id]); return f.snapshot; },
    async check(id, revision) { f.calls.push(["check", id, revision]); return f.checkedSnapshot ?? f.snapshot; },
    async inspect(source, companion) { f.calls.push(["inspect", source, ...companion ? [companion] : []]); return f.snapshot; },
    async action(...args) { f.calls.push(["action", ...args]); return { schema_version: 1, committed: true, document_id: args[0], previous_revision_id: args[1], revision_id: f.snapshot.revision_id, currentness: "unchecked" }; },
    async artifact(...args) { f.calls.push(["artifact", ...args]); return { id: args[2], path: source, kind: args[2].startsWith("proof-lean-") ? "lean" : f.pdf ? "pdf" : "tex", sha256: createHash("sha256").update(content).digest("hex") }; },
    async conflicts() { return f.conflicts ?? formalizationConflictFixture(); },
    async conflictArtifact(...args) {
      f.calls.push(["conflictArtifact", ...args]);
      if (f.drift) throw new Error("Publication resolution observation changed; refresh all versions");
      return { id: args[2], path: source, kind: f.conflictPdf ? "pdf" : "lean", sha256: createHash("sha256").update(await readFile(source)).digest("hex") };
    },
    async resolveConflict(...args) {
      f.calls.push(["resolveConflict", ...args]);
      if (f.drift) throw new Error("Publication versions changed; refresh all versions");
      f.conflicts = formalizationConflictFixture(); f.conflicts.transactions[0].state = "RESOLVED";
    },
    async preparationPlan(...args) { f.calls.push(["preparationPlan", ...args]); return { layout: { library: "Main" }, required_imports: ["Init"] }; },
    async preparationRecovery() { f.calls.push(["preparationRecovery"]); return f.recovery ?? { schema_version: 1, transactions: [] }; },
    async preparationResolutionArgs(...args) { f.calls.push(["preparationResolutionArgs", ...args]); return ["formalization", "resolve-preparation", "--json"]; },
  };
  const setupListeners = new Set();
  const setupState = root => ({ status: "ready", root, phase: "", message: "Project tools checked", code: "", remediation: "", logPath: "", activeRun: false, canRetry: false, sourceDiagnostics: [], recentOutput: [], ...f.setupState });
  f.setupChanged = () => { for (const callback of setupListeners) callback(); };
  const services = { onDidChange(callback) { f.changed = callback; return {dispose(){}}; }, project: { root }, formalizations: service, cli: { ok: true },
    setup: { onDidChange(callback) { setupListeners.add(callback); return {dispose: () => setupListeners.delete(callback)}; },
      state: () => setupState(f.setupState?.root ?? root), stateForRoot: selectedRoot => f.setupState?.root && f.setupState.root !== selectedRoot ? { ...setupState(selectedRoot), root: selectedRoot, status: "idle", message: "", recentOutput: [] } : setupState(selectedRoot),
      async refresh(available, selectedRoot, current) { assert.equal(available, true); assert.equal(current(), true); f.calls.push(["baselineInspect", selectedRoot]); await f.onBaselineRefresh?.(); },
      async startForRoot(selectedRoot, options) { f.calls.push(["baselineStart", selectedRoot, options]); f.setupState = { status: "running", root: selectedRoot }; f.setupChanged(); f.setupStarted?.(); await f.setupPending; f.setupState = { status: "ready", root: selectedRoot }; f.setupChanged(); },
      cancelForRoot(selectedRoot) { f.calls.push(["baselineCancel", selectedRoot]); f.setupRelease?.(); },
      async showLogForRoot(selectedRoot) { f.calls.push(["baselineLog", selectedRoot]); },
    },
    runs: { async bindFormalizationResumeRequest(...args) { f.calls.push(["bindResume", ...args]); if (f.resumeError) throw new Error(f.resumeError); return f.resumeRequest ?? args[1]; } },
    async reload() { f.calls.push(["reload"]); if (f.afterReload) f.snapshot = f.afterReload; } };
  f.services = services;
  let scope = { id: "fixture-host", generation: 1, root };
  service.workspaceScope = () => {
    const current = services.project.root || f.folders?.[0]?.uri.fsPath || root;
    if (current !== scope.root) scope = { ...scope, generation: scope.generation + 1, root: current };
    return { ...scope };
  };
  service.registerPreview = (uri, scope) => { service.assertWorkspace(scope); f.registeredPreview = { uri, scope }; };
  service.assertWorkspace = expected => {
    if (JSON.stringify(expected) !== JSON.stringify(service.workspaceScope())) throw new Error("The formalization workspace changed. Refresh before continuing.");
  };
  service.forWorkspace = (expected, cwd) => new Proxy(service, { get(target, property) {
    if (property === "workspaceRoot") return () => { service.assertWorkspace(expected); return cwd; };
    const value = target[property];
    if (typeof value !== "function") return value;
    return async (...args) => { service.assertWorkspace(expected); const result = await value.apply(target, args); service.assertWorkspace(expected); return result; };
  } });
  const host = new FormalizationHost(services, message => { f.replies.push(message); f.onReply?.(message); }, async (request, operation) => f.calls.push(["launch", request, operation]));
  const originalHandle = host.handle.bind(host);
  let requestSequence = 0;
  host.handle = message => originalHandle({ formalizationWorkspace: service.workspaceScope(), formalizationRequest: { channel: "fixture", id: ++requestSequence }, ...message });
  return { f, host, last: type => f.replies.filter(message => message.type === type).at(-1), dispose: async () => { host.dispose(); await rm(root, { recursive: true, force: true }); } };
}

test("intake, current snapshots and repairs stay with the workspace host and backend", async () => {
  const ui = await fixture();
  try {
    assert.equal(await ui.host.handle({ type: "refresh" }), false);
    await ui.host.handle({ type: "listFormalizations" });
    assert.equal(ui.last("formalizationList").documents.length, 1);
    await ui.host.handle({ type: "showFormalization", documentId: "paper-1" });
    assert.equal(ui.last("formalizationState").snapshot.revision_id, "revision-1");
    await ui.host.handle({ type: "inspectFormalization", sourceKind: "file" });
    assert.equal(ui.f.dialog.canSelectFolders, false);
    assert.deepEqual(ui.f.calls.find(row => row[0] === "inspect"), ["inspect", "paper.tex"]);
    await ui.host.handle({ type: "inspectFormalization", sourceKind: "folder" });
    assert.equal(ui.f.dialog.canSelectFolders, true);
    const action = { type: "set_scope", item_id: "item-1", scope: "excluded", reason: "Out of scope" };
    await ui.host.handle({ type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action });
    assert.deepEqual(ui.f.calls.find(row => row[0] === "action"), ["action", "paper-1", "revision-1", action]);
    assert.equal(ui.last("formalizationActionCommitted").revisionId, "revision-1");
    ui.f.picked = [{ fsPath: "/etc/passwd" }];
    await ui.host.handle({ type: "inspectFormalization", sourceKind: "file" });
    assert.match(ui.last("notify").message, /inside the active/);
  } finally { await ui.dispose(); }
});

test("a committed repair is acknowledged before a failed view refresh", async () => {
  const ui = await fixture();
  try {
    ui.f.services.formalizations.show = async () => { throw new Error("Slow current view unavailable"); };
    await ui.host.handle({ type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action: { type: "set_scope", item_id: "item-1", scope: "included", reason: "Keep statement" } });
    const saved = ui.last("formalizationActionCommitted");
    assert.equal(saved.documentId, "paper-1");
    const refreshed = ui.last("formalizationState");
    assert.match(refreshed.error, /Saved revision.*edit is already committed/);
    assert.ok(ui.f.replies.indexOf(saved) < ui.f.replies.indexOf(refreshed));
    assert.equal(ui.f.calls.filter(call => call[0] === "action").length, 1);
  } finally { await ui.dispose(); }
});

test("slow currentness checking leaves frozen source navigation responsive", { timeout: 2000 }, async () => {
  const ui = await fixture();
  let release;
  try {
    const waiting = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { ui.f.services.formalizations.check = async () => { resolve(); await waiting; return ui.f.snapshot; }; });
    const checking = ui.host.handle({ type: "checkFormalization", documentId: "paper-1", revisionId: "revision-1" });
    await started;
    await ui.host.handle({ type: "showFormalization", documentId: "paper-1" });
    assert.equal(ui.last("formalizationState").snapshot.document_id, "paper-1");
    release();
    await checking;
  } finally { release?.(); await ui.dispose(); }
});

test("source-only workspaces can inspect and prepare before LeanFlow project discovery", async () => {
  const ui = await fixture();
  try {
    ui.f.services.project.root = "";
    ui.f.folders = [{ uri: { scheme: "file", fsPath: ui.f.root } }];
    await ui.host.handle({ type: "inspectFormalization", sourceKind: "file" });
    assert.equal(ui.f.services.formalizations.workspaceScope().root, ui.f.root);
    assert.deepEqual(ui.f.calls.at(-1), ["inspect", "paper.tex"]);
    await ui.host.handle({ type: "planFormalizationPreparation", documentId: "paper-1", revisionId: "revision-1", preset: "lean" });
    assert.equal(ui.last("formalizationPreparation").status, "planned");
    const snapshot = { ...formalizationFixture(), preparation_result: { accepted: true, status: "prepared" } };
    await writeFile(path.join(ui.f.root, "formalization"), `process.stderr.write(JSON.stringify({phase:'imports',message:'Checking required imports'})+'\\n');process.stdout.write(${JSON.stringify(JSON.stringify(snapshot))});`);
    ui.f.executable = process.execPath;
    await ui.host.handle({ type: "prepareFormalization", documentId: "paper-1", revisionId: "revision-1", preset: "lean" });
    assert.equal(ui.last("formalizationPreparation").status, "prepared");
    assert.equal(ui.last("formalizationState").snapshot.document_id, "paper-1");
    assert.deepEqual(ui.f.calls.slice(-2), [["reload"], ["show", "paper-1"]]);
  } finally { await ui.dispose(); }
});

test("baseline setup binds the canonical scoped root without source selection or client paths", async () => {
  const ui = await fixture();
  try {
    ui.f.services.project.root = "";
    ui.f.folders = [{ uri: { scheme: "file", fsPath: ui.f.root } }];
    ui.f.snapshot.selection_conflict = { message: "Unresolved source" };
    await ui.host.handle({ type: "formalizationProjectSetup", action: "inspect", root: "/untrusted/client" });
    assert.deepEqual(ui.f.calls.at(-1), ["baselineInspect", ui.f.root]);
    await ui.host.handle({ type: "formalizationProjectSetup", action: "retry" });
    const request = ui.f.calls.find(row => row[0] === "baselineStart");
    assert.equal(request[1], ui.f.root);
    assert.equal(request[2].retry, true);
    assert.equal(request[2].isCurrent(), true);
    assert.equal(ui.f.calls.some(row => ["show", "inspect", "preparationPlan", "launch"].includes(row[0])), false);
    assert.equal(ui.last("formalizationProjectSetupState").state.root, ui.f.root);
  } finally { await ui.dispose(); }
});

test("baseline cancellation bypasses a pending setup and source-operation queue", { timeout: 2000 }, async () => {
  const ui = await fixture();
  try {
    let releaseSource;
    const sourceWaiting = new Promise(resolve => { releaseSource = resolve; });
    const sourceStarted = new Promise(resolve => { ui.f.services.formalizations.show = async () => { resolve(); await sourceWaiting; return ui.f.snapshot; }; });
    const source = ui.host.handle({ type: "showFormalization", documentId: "paper-1" });
    await sourceStarted;
    ui.f.setupPending = new Promise(resolve => { ui.f.setupRelease = resolve; });
    const started = new Promise(resolve => { ui.f.setupStarted = resolve; });
    const setup = ui.host.handle({ type: "formalizationProjectSetup", action: "start" });
    await started;
    await ui.host.handle({ type: "formalizationProjectSetup", action: "cancel" });
    assert.deepEqual(ui.f.calls.find(row => row[0] === "baselineCancel"), ["baselineCancel", ui.f.root]);
    await setup;
    releaseSource();
    await source;
    assert.equal(ui.last("formalizationProjectSetupState").state.status, "ready");
  } finally { await ui.dispose(); }
});

test("old scoped setup progress and completion cannot install A status after switching to B", async () => {
  const ui = await fixture();
  try {
    const rootA = ui.f.root;
    const rootB = path.join(rootA, "B");
    await (await import("node:fs/promises")).mkdir(rootB);
    const oldScope = ui.f.services.formalizations.workspaceScope();
    ui.f.setupPending = new Promise(resolve => { ui.f.setupRelease = resolve; });
    const started = new Promise(resolve => { ui.f.setupStarted = resolve; });
    const setup = ui.host.handle({ type: "formalizationProjectSetup", action: "start" });
    await started;
    ui.f.services.project.root = rootB;
    const scopeB = ui.f.services.formalizations.workspaceScope();
    await ui.host.handle({ type: "formalizationProjectSetup", action: "inspect" });
    const count = ui.f.replies.length;
    ui.f.setupRelease();
    await setup;
    assert.equal(ui.f.replies.length, count);
    assert.equal(ui.last("formalizationProjectSetupState").formalizationWorkspace.generation, scopeB.generation);
    await ui.host.handle({ type: "formalizationProjectSetup", action: "retry", formalizationWorkspace: oldScope });
    assert.match(ui.last("notify").message, /workspace changed/);
    assert.equal(ui.f.calls.filter(row => row[0] === "baselineStart").length, 1);
  } finally { await ui.dispose(); }
});

test("current formalization preparation supersedes generic stale setup but not an active setup", async () => {
  const ui = await fixture();
  try {
    const request = { kind: "formalize", target: "ignored.tex", sourceDraftOnly: false };
    ui.f.snapshot.source.path = "paper.tex";
    ui.f.checkedSnapshot = structuredClone(ui.f.snapshot);
    ui.f.checkedSnapshot.currentness = "checked";
    ui.f.snapshot.currentness = "historical";
    ui.f.snapshot.preparation.current = false;
    ui.f.snapshot.can_prove = false;
    delete ui.f.snapshot.handoff;
    ui.f.setupState = { status: "failed", code: "readiness_stale" };
    await ui.host.handle({ type: "launchFormalization", documentId: "paper-1", revisionId: "revision-1", request });
    assert.equal(ui.f.calls.filter(row => row[0] === "launch").length, 1);
    assert.deepEqual(ui.f.calls.filter(row => row[0] === "check"), [["check", "paper-1", "revision-1"]]);
    ui.f.setupState = { status: "running" };
    await ui.host.handle({ type: "launchFormalization", documentId: "paper-1", revisionId: "revision-1", request });
    assert.match(ui.last("notify").message, /setup is still running/);
    assert.equal(ui.f.calls.filter(row => row[0] === "launch").length, 1);
    ui.f.setupState = { status: "idle" };
    ui.f.checkedSnapshot.preparation.current = false;
    await ui.host.handle({ type: "launchFormalization", documentId: "paper-1", revisionId: "revision-1", request });
    assert.match(ui.last("notify").message, /Set up project tools/);
    assert.equal(ui.f.calls.filter(row => row[0] === "launch").length, 1);
  } finally { await ui.dispose(); }
});

test("historical launch refreshes detected baseline without a full acceptance check", async () => {
  const ui = await fixture();
  try {
    ui.f.snapshot.source.path = "paper.tex";
    ui.f.snapshot.currentness = "historical";
    ui.f.snapshot.preparation.current = false;
    ui.f.snapshot.can_prove = false;
    delete ui.f.snapshot.handoff;
    ui.f.setupState = { status: "detected", code: "setup_baseline_required" };
    ui.f.onBaselineRefresh = () => { ui.f.setupState = { status: "ready" }; };
    await ui.host.handle({ type: "launchFormalization", documentId: "paper-1", revisionId: "revision-1", request: { kind: "formalize", target: "paper.tex" } });
    assert.equal(ui.f.calls.filter(row => row[0] === "launch").length, 1);
    assert.deepEqual(ui.f.calls.find(row => row[0] === "baselineInspect"), ["baselineInspect", ui.f.root]);
    assert.equal(ui.f.calls.some(row => row[0] === "check"), false);
    assert.equal(ui.f.snapshot.can_prove, false);
  } finally { await ui.dispose(); }
});

test("a workspace switch during baseline refresh prevents the old launch", async () => {
  const ui = await fixture();
  try {
    ui.f.snapshot.source.path = "paper.tex";
    ui.f.snapshot.currentness = "historical";
    ui.f.snapshot.preparation.current = false;
    ui.f.setupState = { status: "detected" };
    let enter, release;
    const entered = new Promise(resolve => { enter = resolve; });
    const pending = new Promise(resolve => { release = resolve; });
    ui.f.onBaselineRefresh = async () => { enter(); await pending; ui.f.setupState = { status: "ready" }; };
    const launch = ui.host.handle({ type: "launchFormalization", documentId: "paper-1", revisionId: "revision-1", request: { kind: "formalize", target: "paper.tex" } });
    await entered;
    ui.f.services.project.root = path.join(ui.f.root, "other-workspace");
    release();
    await launch;
    assert.equal(ui.f.calls.some(row => row[0] === "launch"), false);
  } finally { await ui.dispose(); }
});

test("failed preparation refreshes actual document readiness after the controller exits", async () => {
  const ui = await fixture();
  try {
    const refreshed = structuredClone(ui.f.snapshot); refreshed.preparation = { current: false, status: "preparation_required", reason: "The attempted setup was rolled back" }; refreshed.can_prove = false;
    ui.f.afterReload = refreshed;
    await writeFile(path.join(ui.f.root, "formalization"), "process.stdout.write(JSON.stringify({error:'Dependency acquisition failed'}));process.exitCode=1;");
    ui.f.executable = process.execPath;
    await ui.host.handle({ type: "prepareFormalization", documentId: "paper-1", revisionId: "revision-1", preset: "lean" });
    assert.equal(ui.last("formalizationPreparation").status, "failed");
    assert.equal(ui.last("formalizationState").snapshot.preparation.current, false);
    assert.equal(ui.last("formalizationState").snapshot.can_prove, false);
    assert.deepEqual(ui.f.calls.slice(-2), [["reload"], ["show", "paper-1"]]);
  } finally { await ui.dispose(); }
});

test("recovery cancellation remains busy until safe completion and refreshes readiness on failure", async () => {
  const ui = await fixture();
  try {
    await writeFile(path.join(ui.f.root, "formalization"), "process.on('SIGINT',()=>setTimeout(()=>{process.stderr.write('cleanup finished\\n');process.exit(0)},100));process.stderr.write('owned recovery ready\\n');setInterval(()=>{},100);");
    ui.f.executable = process.execPath;
    let ready;
    const started = new Promise(resolve => { ready = resolve; });
    ui.f.onReply = reply => { if (reply.type === "formalizationPreparationRecovery" && reply.message === "owned recovery ready") ready(); };
    const recovery = ui.host.handle({ type: "recoverFormalizationPreparation" });
    await started;
    await ui.host.handle({ type: "cancelFormalizationRecovery" });
    assert.equal(ui.last("formalizationPreparationRecovery").busy, true);
    assert.equal(ui.last("formalizationPreparationRecovery").cancellable, false);
    assert.equal(ui.last("formalizationPreparationRecovery").cancelling, true);
    await recovery;
    const final = ui.last("formalizationPreparationRecovery");
    assert.equal(final.busy, false); assert.equal(final.refreshed, true);
    assert.match(final.error, /without a final controller receipt/);
    assert.deepEqual(ui.f.calls.slice(-2), [["reload"], ["preparationRecovery"]]);
  } finally { await ui.dispose(); }
});

test("publication recovery opens only controller versions and refreshes after exact resolution", async () => {
  const ui = await fixture();
  try {
    await ui.host.handle({ type: "listFormalizationConflicts" });
    assert.equal(ui.last("formalizationConflicts").snapshot.transactions[0].state, "CONFLICTED");
    await ui.host.handle({ type: "openFormalizationConflictVersion", transactionId: "transaction-1", observationId: "observation-1", versionId: "version-retained" });
    assert.deepEqual(ui.f.calls.at(-1), ["conflictArtifact", "transaction-1", "observation-1", "version-retained"]);
    assert.equal(ui.f.opened.language, "lean4");
    assert.equal("path" in ui.f.opened, false);
    await ui.host.handle({ type: "resolveFormalizationConflict", transactionId: "transaction-1", observationId: "observation-1", reason: "Merged current theorem" });
    assert.deepEqual(ui.f.calls.at(-1), ["resolveConflict", "transaction-1", "observation-1", "Merged current theorem"]);
    assert.equal(ui.last("formalizationConflicts").resolvedTransactionId, "transaction-1");
    assert.match(ui.last("notify").message, /Revalidate/);
    assert.match(ui.last("notify").message, /any unavailable publication history were recorded/);
    ui.f.drift = true;
    await ui.host.handle({ type: "resolveFormalizationConflict", transactionId: "transaction-1", observationId: "observation-1", reason: "Old observation" });
    assert.equal(ui.last("formalizationConflicts").snapshot, null);
    assert.match(ui.last("formalizationConflicts").error, /versions changed/);
  } finally { await ui.dispose(); }
});

test("explicit TeX and PDF pairing is order-independent and validates both host paths", async () => {
  const ui = await fixture();
  try {
    const pdf = path.join(ui.f.root, "Rendered Paper.PDF");
    await writeFile(pdf, "%PDF-fixture");
    ui.f.picked = [{ fsPath: pdf }, { fsPath: path.join(ui.f.root, "paper.tex") }];
    await ui.host.handle({ type: "inspectFormalization", sourceKind: "file" });
    assert.equal(ui.f.dialog.canSelectMany, true);
    assert.deepEqual(ui.f.calls.at(-1), ["inspect", "paper.tex", "Rendered Paper.PDF"]);
    for (const picked of [
      [{ fsPath: pdf }, { fsPath: pdf }],
      [{ fsPath: path.join(ui.f.root, "paper.tex") }, { fsPath: "/etc/passwd" }],
      [{ fsPath: pdf }, { fsPath: pdf }, { fsPath: pdf }],
    ]) {
      ui.f.picked = picked;
      await ui.host.handle({ type: "inspectFormalization", sourceKind: "file" });
      assert.ok(ui.last("notify").message);
      assert.equal(ui.f.calls.filter(row => row[0] === "inspect").length, 1);
    }
    ui.f.picked = [{ fsPath: path.join(ui.f.root, "paper.tex") }];
    await ui.host.handle({ type: "inspectFormalization", sourceKind: "file" });
    assert.deepEqual(ui.f.calls.at(-1), ["inspect", "paper.tex"]);
  } finally { await ui.dispose(); }
});

test("publication PDF versions use verified bytes and opaque observation-bound viewer identities", async () => {
  const ui = await fixture();
  try {
    const bytes = Buffer.from("%PDF frozen version\0binary content");
    await writeFile(path.join(ui.f.root, "paper.tex"), bytes);
    ui.f.conflictPdf = true;
    await ui.host.handle({ type: "openFormalizationConflictVersion", transactionId: "transaction-1", observationId: "observation-1", versionId: "version-retained" });
    const reply = ui.last("formalizationConflictPdf");
    assert.equal(reply.transactionId, "transaction-1");
    assert.equal(reply.observationId, "observation-1");
    assert.equal(reply.versionId, "version-retained");
    assert.deepEqual(Buffer.from(reply.pdf.base64, "base64"), bytes);
    assert.equal("path" in reply.pdf, false);
    assert.equal(ui.f.opened, undefined);
  } finally { await ui.dispose(); }
});

test("source links open verified frozen bytes and selection maps back without exposing host paths", async () => {
  const ui = await fixture();
  try {
    const message = { type: "openFormalizationArtifact", documentId: "paper-1", revisionId: "revision-1", artifactId: "source-tex", itemId: "item-1", line: 2 };
    await ui.host.handle(message);
    assert.equal(ui.f.opened.content, "Source\nDefine doubling.\n");
    assert.match(ui.f.opened.uri.scheme, /^leanflow-frozen-/);
    assert.equal(path.basename(ui.f.opened.uri.path), "main.tex");
    assert.equal(ui.f.opened.language, "latex");
    assert.equal(ui.f.shown[1].viewColumn, 2);
    assert.deepEqual(ui.f.shown[1].selection.args, [1, 0, 1, 0]);
    assert.equal(ui.f.shown[0].languageId, "latex");
    ui.f.selection({ textEditor: { document: { uri: ui.f.opened.uri } }, selections: [{ start: { line: 1 }, end: { line: 2 } }] });
    assert.deepEqual(ui.last("formalizationSourceSelection"), { type: "formalizationSourceSelection", documentId: "paper-1", revisionId: "revision-1", artifactId: "source-tex", formalizationWorkspace: ui.f.services.formalizations.workspaceScope(), lineStart: 2, lineEnd: 3 });
    await ui.host.handle({ ...message, artifactId: "unknown" });
    assert.match(ui.last("notify").message, /not part/);
    await ui.host.handle({ ...message, revisionId: "stale" });
    assert.match(ui.last("notify").message, /revision changed/);
    ui.f.pdf = true;
    await ui.host.handle({ ...message, artifactId: "source-pdf", pageIndex: 1 });
    const pdf = ui.last("formalizationPdf").pdf;
    assert.equal(pdf.pageIndex, 1);
    assert.equal("path" in pdf, false);
    assert.equal(Buffer.from(pdf.base64, "base64").toString(), ui.f.opened.content);
  } finally { await ui.dispose(); }
});

test("certified proof links use exact currentness instead of the historical artifact list", async () => {
  const ui = await fixture();
  try {
    const id = "proof-lean-" + "d".repeat(64);
    ui.f.checkedSnapshot = structuredClone(ui.f.snapshot);
    ui.f.checkedSnapshot.currentness = "checked";
    ui.f.checkedSnapshot.artifacts.push({ id, path: "Main/Certified.lean", kind: "lean", sha256: "d".repeat(64) });
    ui.f.checkedSnapshot.items[0].lean_refs = [{ artifact_id: id, file: "Main/Certified.lean", name: "Paper.double", line_start: 1 }];
    ui.f.snapshot.currentness = "historical";
    ui.f.snapshot.can_prove = false;
    delete ui.f.snapshot.handoff;
    await ui.host.handle({ type: "openFormalizationArtifact", documentId: "paper-1", revisionId: "revision-1", artifactId: id, itemId: "item-1" });
    assert.equal(ui.f.opened.language, "lean4");
    assert.deepEqual(ui.f.calls.slice(0, 2), [["check", "paper-1", "revision-1"], ["artifact", "paper-1", "revision-1", id]]);
    ui.f.checkedSnapshot.artifacts = [];
    await ui.host.handle({ type: "openFormalizationArtifact", documentId: "paper-1", revisionId: "revision-1", artifactId: id, itemId: "item-1" });
    assert.match(ui.last("notify").message, /not part/);
    assert.equal(ui.f.calls.filter(row => row[0] === "artifact").length, 1);
  } finally { await ui.dispose(); }
});

test("draft notes remain workspace previews and never become repair source selections", async () => {
  const ui = await fixture();
  try {
    const base = { type: "openFormalizationArtifact", documentId: "paper-1", revisionId: "revision-1" };
    await ui.host.handle({ ...base, artifactId: "source-tex" });
    ui.f.snapshot.artifacts.push({ id: "draft-report", kind: "source", path: "Model-report.md", sha256: "a".repeat(64) });
    await ui.host.handle({ ...base, artifactId: "draft-report" });
    assert.equal(ui.last("notify"), undefined);
    assert.equal(ui.f.opened.language, "markdown");
    const count = ui.f.replies.filter(reply => reply.type === "formalizationSourceSelection").length;
    ui.f.selection({ textEditor: { document: { uri: ui.f.opened.uri } }, selections: [{ start: { line: 1 }, end: { line: 2 } }] });
    assert.equal(ui.f.replies.filter(reply => reply.type === "formalizationSourceSelection").length, count);
    assert.deepEqual(ui.f.registeredPreview, { uri: ui.f.opened.uri.toString(), scope: ui.f.services.formalizations.workspaceScope() });
  } finally { await ui.dispose(); }
});

test("proving binds the selected accepted handoff and zero obligations cause zero launches", async () => {
  const ui = await fixture();
  try {
    const message = { type: "proveFormalization", documentId: "paper-1", revisionId: "revision-1", request: { kind: "formalize", target: "paper.tex", prompt: "old prompt", additionalSkills: ["old skill"], provider: "claude" } };
    await ui.host.handle(message);
    const launched = ui.f.calls.find(row => row[0] === "launch")[1];
    assert.equal(launched.kind, "prove"); assert.equal(launched.target, ""); assert.equal(launched.prompt, "");
    assert.deepEqual(launched.formalization, { documentId: "paper-1", revisionId: "revision-1" });
    ui.f.snapshot.handoff.obligations = [];
    await ui.host.handle(message);
    assert.equal(ui.f.calls.filter(row => row[0] === "launch").length, 1);
    assert.match(ui.last("notify").message, /inconsistent empty prover handoff/);
    ui.f.snapshot.can_prove = false;
    await ui.host.handle(message);
    assert.match(ui.last("notify").message, /not ready/);
    assert.equal(ui.f.calls.filter(row => row[0] === "launch").length, 1);
  } finally { await ui.dispose(); }
});


test("a delayed picker cannot inspect or mutate the newly selected workspace", async () => {
  const ui = await fixture();
  try {
    let release, opened;
    ui.f.pickerResult = new Promise(resolve => { release = resolve; });
    const dialog = new Promise(resolve => { opened = resolve; });
    ui.f.onDialog = opened;
    const scopeA = ui.f.services.formalizations.workspaceScope();
    const pending = ui.host.handle({ type: "inspectFormalization", sourceKind: "file" });
    await dialog;
    ui.f.services.project.root = path.join(ui.f.root, "copy-B");
    release(ui.f.picked);
    await pending;
    assert.equal(ui.f.calls.some(row => row[0] === "inspect"), false);
    assert.equal(ui.last("formalizationState"), undefined);
    await ui.host.handle({ type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1",
      action: { type: "set_scope", item_id: "item-1", scope: "excluded", reason: "old view" }, formalizationWorkspace: scopeA });
    assert.equal(ui.f.calls.some(row => row[0] === "action"), false);
    assert.match(ui.last("notify").message, /workspace changed/);
  } finally { await ui.dispose(); }
});

test("a delayed A snapshot cannot install accepted status for B with identical IDs", async () => {
  const ui = await fixture();
  try {
    let release, entered;
    const started = new Promise(resolve => { entered = resolve; });
    const result = new Promise(resolve => { release = resolve; });
    ui.f.services.formalizations.show = async () => { entered(); return result; };
    const pending = ui.host.handle({ type: "showFormalization", documentId: "paper-1" });
    await started;
    ui.f.services.project.root = path.join(ui.f.root, "copy-B");
    release(ui.f.snapshot);
    await pending;
    assert.equal(ui.last("formalizationState"), undefined);
  } finally { await ui.dispose(); }
});


test("switching workspace cancels an owned preparation and releases the queue after safe cleanup", { timeout: 5000 }, async () => {
  const ui = await fixture();
  try {
    await writeFile(path.join(ui.f.root, "formalization"), "process.on('SIGINT',()=>setTimeout(()=>process.exit(0),50));process.stderr.write('old workspace preparation ready\\n');setInterval(()=>{},100);");
    ui.f.executable = process.execPath;
    let ready;
    const started = new Promise(resolve => { ready = resolve; });
    ui.f.onReply = reply => { if (reply.type === "formalizationPreparation" && reply.message === "old workspace preparation ready") ready(); };
    const preparing = ui.host.handle({ type: "prepareFormalization", documentId: "paper-1", revisionId: "revision-1", preset: "lean" });
    await started;
    ui.f.services.project.root = path.join(ui.f.root, "B");
    ui.f.changed();
    const showingB = ui.host.handle({ type: "showFormalization", documentId: "paper-1" });
    await Promise.all([preparing, showingB]);
    assert.equal(ui.last("formalizationState").formalizationWorkspace.root, ui.f.services.project.root);
    assert.equal(ui.f.calls.some(row => row[0] === "reload"), false);
  } finally { await ui.dispose(); }
});


test("source launch binds controller-selected target and workspace without approving source gates", async () => {
  const ui = await fixture();
  try {
    ui.f.snapshot.source.path = "paper.tex";
    ui.f.snapshot.can_prove = false;
    ui.f.snapshot.status = "inventory";
    ui.f.snapshot.preparation = undefined;
    const request = { kind: "formalize", target: "/wrong/old.tex", provider: "codex", model: "gpt-6.1-sol", prompt: "Translate the frozen source", overrides: {} };
    await ui.host.handle({ type: "launchFormalization", documentId: "paper-1", revisionId: "revision-1", request });
    const launch = ui.f.calls.find(call => call[0] === "launch");
    assert.equal(launch[1].target, "paper.tex");
    assert.equal(launch[1].kind, "formalize");
    assert.equal(launch[1].formalization, undefined);
    assert.equal(launch[2].services.project.root, ui.f.root);
    assert.equal(launch[2].sourceDocumentId, "paper-1");
    assert.equal(launch[2].resumeTrackedId, undefined);
    assert.equal(ui.f.snapshot.can_prove, false);
    assert.equal(ui.f.calls.some(call => call[0] === "action"), false);
    await ui.host.handle({ type: "resumeFormalization", documentId: "paper-1", revisionId: "revision-1", trackedRunId: "owned-stopped", request });
    assert.equal(ui.f.calls.filter(call => call[0] === "launch").at(-1)[2].resumeTrackedId, "owned-stopped");
  } finally { await ui.dispose(); }
});

test("resume uses the trusted original provider and draft intent before accepted-revision checks", async () => {
  const ui = await fixture();
  try {
    ui.f.snapshot.source.path = "paper.tex";
    ui.f.snapshot.status = "published";
    ui.f.snapshot.can_prove = true;
    const changed = { kind: "formalize", target: "paper.tex", provider: "claude", model: "changed-model", sourceDraftOnly: true, overrides: {} };
    ui.f.resumeRequest = { ...changed, provider: "codex", model: "original-model", sourceDraftOnly: false };
    await ui.host.handle({ type: "resumeFormalization", documentId: "paper-1", revisionId: "revision-1", trackedRunId: "owned-stopped", request: changed });
    const launch = ui.f.calls.find(call => call[0] === "launch");
    assert.equal(launch[1].provider, "codex");
    assert.equal(launch[1].model, "original-model");
    assert.equal(launch[1].sourceDraftOnly, false);
    assert.deepEqual(ui.f.calls.find(call => call[0] === "bindResume").slice(3), [ui.f.root, "paper-1"]);
    ui.f.resumeError = "Original native identity is unknown; resume with the CLI.";
    await ui.host.handle({ type: "resumeFormalization", documentId: "paper-1", revisionId: "revision-1", trackedRunId: "unknown", request: changed });
    assert.equal(ui.f.calls.filter(call => call[0] === "launch").length, 1);
    assert.match(ui.last("notify").message, /Original native identity is unknown/);
  } finally { await ui.dispose(); }
});

for (const mutation of ["revision", "stale", "conflict", "acceptedDraft", "retired"]) {
  test(`source launch refuses ${mutation} before dispatch`, async () => {
    const ui = await fixture();
    try {
      ui.f.snapshot.source.path = "paper.tex";
      const request = { kind: "formalize", target: "paper.tex", sourceDraftOnly: false, overrides: {} };
      if (mutation === "revision") ui.f.snapshot.revision_id = "changed";
      if (mutation === "stale") ui.f.snapshot.source_stale = true;
      if (mutation === "conflict") ui.f.snapshot.selection_conflict = { message: "Choose revision" };
      if (mutation === "acceptedDraft") { request.sourceDraftOnly = true; ui.f.snapshot.can_prove = true; }
      if (mutation === "retired") request.kind = "draft";
      await ui.host.handle({ type: "launchFormalization", documentId: "paper-1", revisionId: "revision-1", request });
      assert.equal(ui.f.calls.filter(call => call[0] === "launch").length, 0);
      assert.equal(ui.last("notify").level, "error");
    } finally { await ui.dispose(); }
  });
}
