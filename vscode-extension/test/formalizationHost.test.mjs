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
      export const Uri = { file: fsPath => ({ fsPath }) };
      export const env = {remoteName:undefined};
      export const ViewColumn = { Beside: 2 };
      export class Range { constructor(...args) { this.args = args; } }
      export const workspace = {
        get workspaceFolders(){ return f().folders ?? []; },
        getWorkspaceFolder(){ return f().folders?.[0]; },
        getConfiguration(){ return {get:(name, fallback)=>name==='cliPath' ? f().executable ?? fallback : fallback}; },
        openTextDocument: async options => { f().opened = options; return { uri: { toString: () => 'untitled:frozen' }, lineCount: options.content.split('\\n').length }; },
      };
      export const window = {
        onDidChangeTextEditorSelection: callback => { f().selection = callback; return {dispose(){}}; },
        showOpenDialog: async options => { f().dialog = options; return f().picked; },
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
    bindSourceRoot(root) { f.boundRoot = root; },
    workspaceRoot() { return f.boundRoot || root; },
    async list() { return [f.snapshot]; },
    async show(id) { f.calls.push(["show", id]); return f.snapshot; },
    async inspect(source, companion) { f.calls.push(["inspect", source, ...companion ? [companion] : []]); return f.snapshot; },
    async action(...args) { f.calls.push(["action", ...args]); return f.snapshot; },
    async artifact(...args) { f.calls.push(["artifact", ...args]); return { id: args[2], path: source, kind: f.pdf ? "pdf" : "tex", sha256: createHash("sha256").update(content).digest("hex") }; },
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
  const services = { project: { root }, formalizations: service, async reload() { f.calls.push(["reload"]); if (f.afterReload) f.snapshot = f.afterReload; } };
  f.services = services;
  const host = new FormalizationHost(services, message => { f.replies.push(message); f.onReply?.(message); }, async request => f.calls.push(["launch", request]));
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
    assert.deepEqual(ui.f.calls.at(-1), ["action", "paper-1", "revision-1", action]);
    ui.f.picked = [{ fsPath: "/etc/passwd" }];
    await ui.host.handle({ type: "inspectFormalization", sourceKind: "file" });
    assert.match(ui.last("notify").message, /inside the active/);
  } finally { await ui.dispose(); }
});

test("source-only workspaces can inspect and prepare before LeanFlow project discovery", async () => {
  const ui = await fixture();
  try {
    ui.f.services.project.root = "";
    ui.f.folders = [{ uri: { scheme: "file", fsPath: ui.f.root } }];
    await ui.host.handle({ type: "inspectFormalization", sourceKind: "file" });
    assert.equal(ui.f.boundRoot, ui.f.root);
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
    assert.equal(ui.f.shown[1].viewColumn, 2);
    ui.f.selection({ textEditor: { document: { uri: { toString: () => "untitled:frozen" } } }, selections: [{ start: { line: 1 }, end: { line: 2 } }] });
    assert.deepEqual(ui.last("formalizationSourceSelection"), { type: "formalizationSourceSelection", documentId: "paper-1", revisionId: "revision-1", artifactId: "source-tex", lineStart: 2, lineEnd: 3 });
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
    assert.match(ui.last("notify").message, /No model was launched/);
    ui.f.snapshot.can_prove = false;
    await ui.host.handle(message);
    assert.match(ui.last("notify").message, /not ready/);
    assert.equal(ui.f.calls.filter(row => row[0] === "launch").length, 1);
  } finally { await ui.dispose(); }
});
