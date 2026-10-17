/** Bind real filesystem discovery, scope guards, CLI cwd and launches to the same owning project. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, mkdir, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";
import { formalizationFixture } from "./helpers/formalizationFixture.mjs";

const bundle = await build({
  stdin: { contents: `
    export { discoverProject, discoverProjectIncludingNested } from "./src/core/project";
    export { formalizationScopeRoot, formalizationWorkspaceRoot, FormalizationWorkspaceSelection } from "./src/core/formalizationWorkspace";
    export { FormalizationService } from "./src/core/formalizationService";
    export { FormalizationHost } from "./src/panels/formalizationHost";
  `, resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "real-workspace-host", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      import path from 'node:path';
      const f = () => globalThis.realWorkspaceFixture;
      export const env = { remoteName: undefined };
      export const Uri = { file: fsPath => ({ fsPath, scheme: 'file', authority: '' }) };
      export const ViewColumn = { Beside: 2 };
      export class Range { constructor(...args) { this.args = args; } }
      export class RelativePattern { constructor(base, pattern) { this.base = base; this.pattern = pattern; } }
      export const workspace = {
        get workspaceFolders() { return f().folders; },
        getWorkspaceFolder(uri) { return f().folders.find(folder => uri.fsPath === folder.uri.fsPath || uri.fsPath.startsWith(folder.uri.fsPath + path.sep)); },
        getConfiguration() { return { get: (name, fallback) => name === 'projectRoot' ? f().configured || fallback : name === 'cliPath' ? process.execPath : fallback }; },
        findFiles: async pattern => f().manifests.filter(uri => typeof pattern === 'string' || uri.fsPath.startsWith(pattern.base.uri.fsPath + path.sep)),
      };
      export const window = {
        get activeTextEditor() { return f().active ? { document: { uri: f().active } } : undefined; },
        onDidChangeTextEditorSelection: () => ({ dispose() {} }),
        showOpenDialog: async () => f().picked,
      };
    ` }));
  } }],
});
const {
  discoverProject, discoverProjectIncludingNested, formalizationScopeRoot,
  formalizationWorkspaceRoot, FormalizationWorkspaceSelection, FormalizationService, FormalizationHost,
} = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

async function fixture() {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), "formalization-workspace-")));
  const uri = fsPath => ({ fsPath, scheme: "file", authority: "", toString: () => `file:${fsPath}` });
  const f = globalThis.realWorkspaceFixture = { root, uri, active: null, folders: [], manifests: [], calls: [], replies: [], launches: [] };
  f.folder = folder => ({ uri: uri(folder), name: path.basename(folder) });
  f.project = async name => {
    const project = path.join(root, name);
    await mkdir(path.join(project, ".leanflow"), { recursive: true });
    await writeFile(path.join(project, ".leanflow/project.yaml"), "name: fixture\n");
    f.manifests.push(uri(path.join(project, ".leanflow/project.yaml")));
    return project;
  };
  f.host = project => {
    const selection = new FormalizationWorkspaceSelection();
    const listeners = new Set();
    const services = {
      project,
      onDidChange(listener) { listeners.add(listener); return { dispose: () => listeners.delete(listener) }; },
      async reload() {
        services.project = await discoverProjectIncludingNested("", selection.discoveryRoot());
        for (const listener of listeners) listener();
      },
    };
    const service = new FormalizationService(() => selection.scopeRoot(services.project.root), async (args, options) => {
      f.calls.push({ args, cwd: options.cwd });
      if (f.transport) return f.transport(args, options);
      return JSON.stringify(formalizationFixture());
    });
    services.formalizations = service;
    const host = new FormalizationHost(services, message => {
      f.replies.push(message);
      f.onReply?.(message);
    }, async (request, operation) => {
      operation.assertCurrent();
      f.launches.push({ request, root: operation.services.project.root, serviceRoot: operation.services.formalizations.workspaceRoot() });
    });
    f.selection = selection;
    f.service = service;
    f.services = services;
    f.changed = () => { for (const listener of listeners) listener(); };
    f.handle = message => host.handle({ ...message, formalizationWorkspace: service.workspaceScope() });
    f.dispose = async () => { host.dispose(); await rm(root, { recursive: true, force: true }); };
    return host;
  };
  return f;
}

for (const layout of ["subfolder", "symlink-subfolder", "symlink-parent-with-nested-project"]) {
  test(`${layout} keeps scope, preparation cwd and prover launch at the actual project ancestor`, async () => {
    const f = await fixture();
    try {
      const project = await f.project("workspace/Project");
      const paper = path.join(project, "Paper"); await mkdir(paper);
      let workspace = paper;
      if (layout.startsWith("symlink")) {
        workspace = path.join(f.root, "workspace-alias");
        const target = layout === "symlink-subfolder" ? paper : path.dirname(project);
        await symlink(target, workspace, process.platform === "win32" ? "junction" : "dir");
      }
      f.folders = [f.folder(workspace)];
      if (layout === "symlink-parent-with-nested-project") {
        f.manifests = [f.uri(path.join(workspace, "Project/.leanflow/project.yaml"))];
      } else {
        f.active = f.uri(path.join(workspace, "paper.tex"));
        await writeFile(f.active.fsPath, "source fixture\n");
      }
      const discovered = await discoverProjectIncludingNested();
      assert.equal(discovered.root, project);
      assert.equal(formalizationScopeRoot(discovered.root), project);
      assert.equal(await formalizationWorkspaceRoot(workspace), await realpath(workspace));
      f.host(discovered);
      assert.equal(f.service.workspaceScope().root, project);
      const prepared = { ...formalizationFixture(), preparation_result: { accepted: true } };
      await writeFile(path.join(project, "formalization"), `
        const fs = require('node:fs');
        fs.writeFileSync('observed-preparation-cwd', process.cwd());
        process.stdout.write(${JSON.stringify(JSON.stringify(prepared) + "\n")});
      `);
      await f.handle({ type: "prepareFormalization", documentId: "paper-1", revisionId: "revision-1", preset: "lean" });
      assert.equal(await readFile(path.join(project, "observed-preparation-cwd"), "utf8"), project);
      await f.handle({ type: "showFormalization", documentId: "paper-1" });
      await f.handle({ type: "proveFormalization", documentId: "paper-1", revisionId: "revision-1", request: {} });
      assert.ok(f.calls.length >= 2);
      assert.ok(f.calls.every(call => call.cwd === project));
      assert.deepEqual(f.launches.map(({ root, serviceRoot }) => ({ root, serviceRoot })), [{ root: project, serviceRoot: project }]);
      assert.equal(f.replies.some(reply => reply.type === "notify"), false);
    } finally { await f.dispose?.(); if (!f.dispose) await rm(f.root, { recursive: true, force: true }); }
  });
}

test("provisional parent-folder scope still discovers its unique nested project", async () => {
  const f = await fixture();
  try {
    const project = await f.project("workspace/Project");
    f.folders = [f.folder(path.dirname(project))];
    f.host(discoverProject());
    assert.equal(f.service.workspaceScope().root, path.dirname(project));
    assert.equal(f.selection.discoveryRoot(), "");
    await f.services.reload();
    assert.equal(f.services.project.root, project);
    assert.equal(f.service.workspaceScope().root, project);
  } finally { await f.dispose(); }
});

for (const folders of ["separate", "common-parent"]) {
test(`real editor A/B switching under ${folders} invalidates delayed operations before asynchronous discovery`, async () => {
  const f = await fixture();
  try {
    const a = await f.project("A"), b = await f.project("B");
    f.folders = folders === "separate" ? [f.folder(a), f.folder(b)] : [f.folder(f.root)];
    f.active = f.uri(path.join(a, "Main.lean"));
    f.host(discoverProject());
    const scopeA = f.service.workspaceScope();
    let release;
    f.transport = () => new Promise(resolve => { release = resolve; });
    const operation = f.service.forWorkspace(scopeA);
    const pending = operation.show("paper-1");
    f.active = f.uri(path.join(b, "Main.lean"));
    assert.equal(f.services.project.root, a);
    assert.equal(f.service.workspaceScope().root, b);
    assert.throws(() => f.service.assertWorkspace(scopeA), /workspace changed/);
    release(JSON.stringify(formalizationFixture()));
    await assert.rejects(pending, /workspace changed/);
    await assert.rejects(operation.action("paper-1", "revision-1", { type: "set_scope", item_id: "item-1", scope: "excluded", reason: "old A" }), /workspace changed/);
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0].cwd, a);
  } finally { await f.dispose(); }
});
}

test("a source file outside cached A selects its actual owning folder before discovery", async () => {
  const f = await fixture();
  try {
    const a = await f.project("A"), b = path.join(f.root, "Sources-B"); await mkdir(b);
    f.folders = [f.folder(f.root)]; f.active = f.uri(path.join(a, "Main.lean"));
    f.host(discoverProject());
    const scopeA = f.service.workspaceScope();
    f.active = f.uri(path.join(b, "paper.tex"));
    const sourceScope = f.service.workspaceScope();
    assert.equal(sourceScope.root, f.root);
    assert.throws(() => f.service.assertWorkspace(scopeA), /workspace changed/);
    f.active = null;
    assert.deepEqual(f.service.workspaceScope(), sourceScope);
    await f.services.reload();
    assert.deepEqual(f.service.workspaceScope(), sourceScope);
  } finally { await f.dispose(); }
});

test("external toolchain references retain the current workspace through rediscovery", async () => {
  const f = await fixture();
  try {
    const a = await f.project("A"), toolchain = path.join(f.root, "toolchain", "src", "lean", "Init");
    await mkdir(toolchain, { recursive: true });
    f.folders = [f.folder(a)]; f.active = f.uri(path.join(a, "Main.lean")); f.host(discoverProject());
    const scope = f.service.workspaceScope();
    f.active = f.uri(path.join(toolchain, "Prelude.lean"));
    assert.deepEqual(f.service.workspaceScope(), scope);
    await f.services.reload();
    assert.deepEqual(f.service.workspaceScope(), scope);
    assert.equal(f.services.project.root, a);
    await f.handle({ type: "showFormalization", documentId: "paper-1" });
    assert.equal(f.calls.at(-1).cwd, a);
    await assert.rejects(readFile(path.join(toolchain, ".leanflow", "formalization-publication.lock")), { code: "ENOENT" });
  } finally { await f.dispose(); }
});

for (const retainedFolder of [false, true]) {
test(`a loose source-only editor cannot retain cached A with ${retainedFolder ? "an unrelated folder" : "no folder"}`, async () => {
  const f = await fixture();
  try {
    const a = await f.project("A"), b = path.join(f.root, "Sources-B"); await mkdir(b);
    if (retainedFolder) f.folders = [f.folder(a)];
    f.active = f.uri(path.join(a, "Main.lean")); f.host(discoverProject());
    const scopeA = f.service.workspaceScope();
    f.active = f.uri(path.join(b, "paper.tex"));
    const scopeB = f.service.workspaceScope();
    assert.equal(scopeB.root, b);
    assert.throws(() => f.service.assertWorkspace(scopeA), /workspace changed/);
    f.active = null; await f.services.reload();
    assert.deepEqual(f.service.workspaceScope(), scopeB);
    await f.handle({ type: "showFormalization", documentId: "paper-1" });
    assert.equal(f.calls.at(-1).cwd, b);
  } finally { await f.dispose(); }
});
}

test("dashboard focus retains source-only B instead of discovering the only Lean project A", async () => {
  const f = await fixture();
  try {
    const a = await f.project("A"), b = path.join(f.root, "Sources-B"); await mkdir(b);
    f.folders = [f.folder(a), f.folder(b)]; f.active = f.uri(path.join(b, "paper.tex"));
    f.host(discoverProject());
    const scopeB = f.service.workspaceScope();
    assert.equal(scopeB.root, b);
    f.active = null;
    assert.equal(discoverProject().root, a);
    await f.services.reload();
    assert.equal(f.services.project.found, false);
    assert.deepEqual(f.service.workspaceScope(), scopeB);
    await f.handle({ type: "showFormalization", documentId: "paper-1" });
    assert.equal(f.calls.at(-1).cwd, b);
    f.active = f.uri(path.join(a, "Main.lean"));
    assert.equal(f.service.workspaceScope().root, a);
    assert.throws(() => f.service.assertWorkspace(scopeB), /workspace changed/);
  } finally { await f.dispose(); }
});

test("no-folder editor selection remains stable at dashboard focus and explicit invalidation retires it", async () => {
  const f = await fixture();
  try {
    const a = await f.project("A"), b = await f.project("B");
    f.active = f.uri(path.join(a, "Main.lean")); f.host(discoverProject());
    f.active = f.uri(path.join(b, "Main.lean"));
    const scopeB = f.service.workspaceScope();
    assert.equal(scopeB.root, b);
    f.active = null; await f.services.reload();
    assert.equal(f.services.project.root, b);
    assert.deepEqual(f.service.workspaceScope(), scopeB);
    f.selection.invalidate(); f.service.invalidateWorkspace();
    assert.equal(f.service.workspaceScope().root, "");
    assert.throws(() => f.service.assertWorkspace(scopeB), /workspace changed/);
  } finally { await f.dispose(); }
});

test("explicit configured root overrides a held dashboard selection and editor folder", async () => {
  const f = await fixture();
  try {
    const a = await f.project("A"), b = await f.project("B");
    f.folders = [f.folder(a), f.folder(b)]; f.active = f.uri(path.join(b, "Main.lean"));
    f.host(discoverProject());
    const scopeB = f.service.workspaceScope();
    f.configured = a; f.selection.invalidate(); f.service.invalidateWorkspace();
    assert.equal(f.service.workspaceScope().root, a);
    assert.throws(() => f.service.assertWorkspace(scopeB), /workspace changed/);
  } finally { await f.dispose(); }
});

test("an explicit completed setup selection replaces held dashboard context", async () => {
  const f = await fixture();
  try {
    const a = await f.project("A"), b = await f.project("B");
    f.folders = [f.folder(a), f.folder(b)]; f.active = f.uri(path.join(a, "Main.lean"));
    f.host(discoverProject());
    const scopeA = f.service.workspaceScope();
    f.active = null; f.selection.select(b); await f.services.reload();
    assert.equal(f.services.project.root, b);
    assert.equal(f.service.workspaceScope().root, b);
    assert.throws(() => f.service.assertWorkspace(scopeA), /workspace changed/);
  } finally { await f.dispose(); }
});

for (const stop of ["editor-A", "workspace-invalidated"]) {
  test(`dashboard focus keeps owned B preparation alive until ${stop}`, { timeout: 5000 }, async () => {
    const f = await fixture();
    let preparing;
    try {
      const a = await f.project("A"), b = path.join(f.root, "Sources-B"); await mkdir(b);
      await writeFile(path.join(b, "formalization"), `
        const fs = require('node:fs');
        fs.writeFileSync('owned-preparation-pid', String(process.pid));
        fs.writeFileSync('owned-preparation-cwd', process.cwd());
        process.on('SIGINT', () => { fs.writeFileSync('owned-preparation-stopped', 'SIGINT'); process.exit(0); });
        process.stderr.write('owned B preparation ready\\n');
        setInterval(() => {}, 100);
      `);
      f.folders = [f.folder(a), f.folder(b)]; f.active = f.uri(path.join(b, "paper.tex"));
      f.host(discoverProject());
      const scopeB = f.service.workspaceScope();
      let started;
      const ready = new Promise(resolve => { started = resolve; });
      f.onReply = reply => { if (reply.type === "formalizationPreparation" && reply.message === "owned B preparation ready") started(); };
      preparing = f.handle({ type: "prepareFormalization", documentId: "paper-1", revisionId: "revision-1", preset: "lean" });
      await ready;
      const pid = Number(await readFile(path.join(b, "owned-preparation-pid"), "utf8"));
      assert.equal(await readFile(path.join(b, "owned-preparation-cwd"), "utf8"), b);
      f.active = null; await f.services.reload(); f.changed();
      assert.deepEqual(f.service.workspaceScope(), scopeB);
      assert.doesNotThrow(() => process.kill(pid, 0));
      const toolchain = path.join(f.root, "toolchain", "src", "lean", "Init");
      await mkdir(toolchain, { recursive: true });
      f.active = f.uri(path.join(toolchain, "Prelude.lean"));
      await f.services.reload(); f.changed();
      assert.deepEqual(f.service.workspaceScope(), scopeB);
      assert.doesNotThrow(() => process.kill(pid, 0));
      if (stop === "editor-A") f.active = f.uri(path.join(a, "Main.lean"));
      else { f.folders = []; f.selection.invalidate(); f.service.invalidateWorkspace(); }
      f.changed();
      await preparing;
      assert.equal(await readFile(path.join(b, "owned-preparation-stopped"), "utf8"), "SIGINT");
      assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
      assert.ok(f.calls.every(call => call.cwd === b));
      assert.throws(() => f.service.assertWorkspace(scopeB), /workspace changed/);
    } finally { await f.dispose?.(); await preparing?.catch(() => undefined); }
  });
}
