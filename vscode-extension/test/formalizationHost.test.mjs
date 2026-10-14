/** Host actions validate revisions and artifact identities before source access or proving. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, writeFile, rm, realpath } from "node:fs/promises";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";
import { formalizationFixture } from "./helpers/formalizationFixture.mjs";

const bundle = await build({
  stdin: { contents: 'export { FormalizationHost } from "./src/panels/formalizationHost";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "formalization-editor-fixture", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      const f = () => globalThis.formalizationHostFixture;
      export const Uri = { file: fsPath => ({ fsPath }) };
      export const ViewColumn = { Beside: 2 };
      export class Range { constructor(...args) { this.args = args; } }
      export const workspace = { openTextDocument: async options => { f().opened = options; return { uri: { toString: () => 'untitled:frozen' }, lineCount: options.content.split('\\n').length }; } };
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
    async list() { return [f.snapshot]; },
    async show(id) { f.calls.push(["show", id]); return f.snapshot; },
    async inspect(source, companion) { f.calls.push(["inspect", source, ...companion ? [companion] : []]); return f.snapshot; },
    async action(...args) { f.calls.push(["action", ...args]); return f.snapshot; },
    async artifact(...args) { f.calls.push(["artifact", ...args]); return { id: args[2], path: source, kind: f.pdf ? "pdf" : "tex", sha256: createHash("sha256").update(content).digest("hex") }; },
  };
  const host = new FormalizationHost({ project: { root }, formalizations: service }, message => f.replies.push(message), async request => f.calls.push(["launch", request]));
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
