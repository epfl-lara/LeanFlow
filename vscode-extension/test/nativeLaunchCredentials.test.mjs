/** Exercise the actual run manager boundary so secrets cannot become run history. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
import { assertWorkflowProjectReady, EMPTY_PROJECT_SETUP } from "../dist/test/projectSetupTypes.mjs";

const bundle = await build({
  stdin: { contents: 'export { RunManager } from "./src/core/runManager"; export { emptyLaunchRequest } from "./src/core/types";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "launch-test-host", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      export class EventEmitter { event = () => ({ dispose() {} }); fire() {} dispose() {} }
      export const workspace = { getConfiguration: () => ({ get: (_, fallback) => fallback }) };
    ` }));
    builder.onLoad({ filter: /src\/core\/workflowTerminal\.ts$/ }, () => ({ contents: `export async function inspectWorkflowTerminal() { return globalThis.launchCredentialFixture.pane ?? {state:"unknown"}; } export async function releaseWorkflowTerminal(id) { (globalThis.launchCredentialFixture.released ??= []).push(id); return globalThis.launchCredentialFixture.releasePending ?? globalThis.launchCredentialFixture.releaseOutcome ?? true; } export async function showWorkflowTerminal() {}` }));
    builder.onLoad({ filter: /src\/core\/launchPaths\.ts$/ }, () => ({ contents: `export async function validateLaunchProjectInputs() {}` }));
    builder.onLoad({ filter: /src\/core\/cli\.ts$/ }, () => ({ contents: `
      export async function fetchLiveStatus() { if (globalThis.launchCredentialFixture.unreadable) throw new Error("temporarily unavailable"); return globalThis.launchCredentialFixture.status ?? null; }
      export async function fetchRuns() { if (globalThis.launchCredentialFixture.unreadable) throw new Error("temporarily unavailable"); return globalThis.launchCredentialFixture.history ?? []; }
      export async function fetchEventTypes() { return []; }
      export async function fetchEvents(root, runId) { (globalThis.launchCredentialFixture.followed ??= []).push(runId); return { events: [], cursor: "", runId }; }
      export async function fetchRunLog() { return ""; }
      export async function fetchRunMetrics(root, runId) {
        (globalThis.launchCredentialFixture.metricsReads ??= []).push([root, runId]);
        return globalThis.launchCredentialFixture.metrics ?? null;
      }
      export async function previewLaunch(root, argv) {
        const f = globalThis.launchCredentialFixture;
        (f.previews ??= []).push(argv);
        const option = name => { const index = argv.indexOf(name); return index < 0 ? "" : argv[index + 1]; };
        return f.preview ?? { version: 1, project: { root }, workflow: { kind: argv[0], source_draft_only: argv.includes("--source-draft-only") },
          runtime: { provider: option("--provider") || "codex", model: option("--model") || "gpt-6.1-sol" } };
      }
      export async function stopWorkflow(root, runId) {
        (globalThis.launchCredentialFixture.stopped ??= []).push([root, runId]);
        return globalThis.launchCredentialFixture.stopOutcome ?? { stopped: true };
      }
      export async function spawnWorkflow(project, argv, env, nativeEnv, prepared) {
        globalThis.launchCredentialFixture.beforePrepared?.();
        await prepared?.(undefined);
        globalThis.launchCredentialFixture.spawned.push({ project, argv, env, nativeEnv });
        return { pid: 1234, kill: () => true, onExit: new Promise(() => {}) };
      }
    ` }));
  } }],
});
const { RunManager, emptyLaunchRequest } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

test("native credentials reach new launches separately from every persisted/public run record", async () => {
  const fixture = globalThis.launchCredentialFixture = { spawned: [], saved: [] };
  const context = { workspaceState: { get: (_key, fallback) => fallback, update: async (_key, value) => fixture.saved.push(JSON.parse(JSON.stringify(value))) } };
  const project = { found: true, root: "/tmp/leanflow-launch-test", stateRoot: "/tmp/leanflow-launch-test/.leanflow/workflow-state", label: "test" };
  const nativeEnv = { LEMEX_API_KEY: "opaque-key-for-rcp", LEMEX_BASE_URL: "https://inference.rcp.epfl.ch/v1", OPENAI_API_KEY: "opaque-planner-key" };
  const manager = new RunManager(context, project, async () => ({ ...nativeEnv }));
  // Polling itself is covered elsewhere; no filesystem or long-lived timer is needed here.
  manager.startPolling = () => {};
  try {
    const run = await manager.launch({ ...emptyLaunchRequest(), provider: "lemex" }, null, null);
    assert.equal(fixture.spawned.length, 1);
    const launch = fixture.spawned[0];
    assert.deepEqual(launch.nativeEnv, nativeEnv);
    for (const key of [nativeEnv.LEMEX_API_KEY, nativeEnv.OPENAI_API_KEY]) {
      assert.ok(!JSON.stringify(launch.argv).includes(key));
      assert.ok(!JSON.stringify(launch.env).includes(key));
      assert.ok(!JSON.stringify(fixture.saved).includes(key));
      assert.ok(!JSON.stringify(run).includes(key));
      assert.ok(!JSON.stringify(manager.snapshot()).includes(key));
    }
  } finally { manager.dispose(); }
});


test("all launch callers share the setup readiness admission check", async () => {
  const fixture = globalThis.launchCredentialFixture = { spawned: [], saved: [] };
  const context = { workspaceState: { get: (_key, fallback) => fallback, update: async () => {} } };
  const project = { found: true, root: "/tmp/leanflow-setup-admission", stateRoot: "/tmp/leanflow-setup-admission/.leanflow/workflow-state", label: "test" };
  let ready = false;
  const roots = [];
  const manager = new RunManager(context, project, async () => ({}), root => { roots.push(root); if (!ready) throw new Error("Project setup is incomplete."); });
  manager.startPolling = () => {};
  try {
    await assert.rejects(manager.launch(emptyLaunchRequest(), null, null), /setup is incomplete/);
    assert.equal(fixture.spawned.length, 0);
    ready = true;
    await manager.launch(emptyLaunchRequest(), null, null);
    assert.equal(fixture.spawned.length, 1);
    assert.deepEqual(roots, [project.root, project.root, project.root]);
  } finally { manager.dispose(); }
});

test("actual RunManager preserves workflow kind through stronger formalization readiness admission", async () => {
  const fixture = globalThis.launchCredentialFixture = { spawned: [], saved: [] };
  const project = { found: true, root: "/tmp/leanflow-stronger-preparation", stateRoot: "/tmp/leanflow-stronger-preparation/.leanflow/workflow-state", label: "test" };
  const context = { workspaceState: { get: (_key, fallback) => fallback, update: async () => {} } };
  let status = "failed";
  const kinds = [];
  const setup = { assertLaunchReady() { throw new Error("Generic setup receipt is stale"); }, stateForRoot: root => ({ ...EMPTY_PROJECT_SETUP, root, status }) };
  const manager = new RunManager(context, project, async () => ({}), (root, kind) => { kinds.push(kind); assertWorkflowProjectReady(setup, root, kind); });
  manager.startPolling = () => {};
  try {
    await assert.rejects(manager.launch(emptyLaunchRequest(), null, null), /Generic setup receipt is stale/);
    const request = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex", provider: "codex", model: "gpt-6.1-sol" };
    await manager.launch(request, null, null, { sourceDocumentId: "paper", projectRoot: project.root });
    assert.equal(fixture.spawned.length, 1);
    assert.deepEqual(kinds, ["prove", "formalize", "formalize"]);
    status = "running";
    await assert.rejects(manager.launch(request, null, null, { sourceDocumentId: "paper", projectRoot: project.root }), /setup is still running/);
    assert.equal(fixture.spawned.length, 1);
  } finally { manager.dispose(); }
});

function observedManager() {
  const f = globalThis.launchCredentialFixture = {spawned:[],saved:[]};
  const context = {workspaceState:{get:(_key,fallback)=>fallback,update:async(_key,value)=>f.saved.push(structuredClone(value))}};
  const manager = new RunManager(context,{found:true,root:"/tmp/isolated-run-discovery",stateRoot:"/tmp/isolated-run-discovery/state"});
  manager.startPolling = () => {};
  manager.stopPolling = () => {};
  return {f,manager};
}

test("Stop controls the exact external live owner without requiring an editor launch", async () => {
  const {f, manager} = observedManager();
  try {
    f.status = {run_id:"external-live",process_id:42,phase:"planning"};
    await manager.refresh();
    assert.equal((await manager.stop("external-live")).stopped,true);
    assert.deepEqual(f.stopped,[["/tmp/isolated-run-discovery","external-live"]]);
    await manager.stop("external-live");
    assert.equal(f.stopped.length,1);
  } finally {manager.dispose();}
});

test("Stop refuses stale, historical and unrelated external jobs and reports runtime refusals", async () => {
  const {f, manager} = observedManager();
  try {
    f.history = [{run_id:"historical",process_id:42}];
    f.status = {run_id:"external-live",process_id:42,stale_snapshot:true};
    await manager.refresh();
    for (const id of ["historical","external-live","unrelated"]) {
      assert.equal((await manager.stop(id)).stopped,false);
    }
    assert.equal(f.stopped,undefined);
    f.status = {run_id:"external-live",process_id:42};
    f.stopOutcome = {stopped:false,reason:"Process identity changed."};
    await manager.refresh();
    assert.deepEqual(await manager.stop("external-live"),f.stopOutcome);
    f.stopOutcome = {stopped:true};
    assert.equal((await manager.stop("external-live")).stopped,true);
    assert.equal(f.stopped.length,2);
  } finally {manager.dispose();}
});

test("external live jobs are followed by exact id even when a newer worker stream exists",async()=>{
  const {f,manager}=observedManager();
  try {
    f.status={run_id:"external-live",process_id:42};
    f.history=[{run_id:"worker",parent_run_id:"external-live",updated_at:"2026-10-12"},{run_id:"external-live",updated_at:"2026-10-11"}];
    await manager.refresh();
    assert.ok(f.followed.includes("external-live"));
    assert.equal(manager.projectRootForRun("external-live"),"/tmp/isolated-run-discovery");
    f.unreadable=true;
    await manager.refresh();
    assert.equal(manager.snapshot().history.length,2);
    assert.match(manager.snapshot().discoveryError,/retry automatically/);
    f.unreadable=false;
    await manager.refresh();
    assert.equal(manager.snapshot().discoveryError,"");
  } finally {manager.dispose();}
});

test("reloaded tmux jobs retain liveness and early failures, then release only their finished pane",async()=>{
  const {f,manager}=observedManager();
  try {
    const run=await manager.launch(emptyLaunchRequest(),null,null);
    manager.handles.clear();
    run.connection={kind:"tmux"};
    f.pane={state:"running",pid:42};
    await manager.refresh();
    assert.equal(run.status,"running");
    f.pane={state:"exited",code:127,output:"leanflow executable not found"};
    await manager.refresh();
    assert.equal(run.status,"failed");
    assert.equal(run.exitCode,127);
    assert.match(run.error,/executable not found/);
    assert.deepEqual(f.released,[run.runId]);
  } finally {manager.dispose();}
});

test("a missing tmux session never overrides an exact verified runtime owner",async()=>{
  const {f,manager}=observedManager();
  try {
    const run=await manager.launch(emptyLaunchRequest(),null,null);
    manager.handles.clear();
    run.connection={kind:"tmux"};
    run.startedAt="2026-01-01";
    f.pane={state:"missing"};
    f.status={run_id:run.runId,process_id:42};
    await manager.refresh();
    assert.equal(run.status,"running");
    assert.equal(run.pid,42);
  } finally {manager.dispose();}
});


test("early terminal output never reintroduces a private launch prompt into saved history",async()=>{
  const {f,manager}=observedManager();
  try {
    const run=await manager.launch({...emptyLaunchRequest(),prompt:"private research notes"},null,null);
    manager.handles.clear(); run.connection={kind:"tmux"};
    f.pane={state:"exited",code:1,output:"error in private research notes"};
    await manager.refresh();
    assert.equal(run.status,"failed");
    assert.ok(!JSON.stringify(f.saved).includes("private research notes"));
    assert.equal(run.connection,undefined);
  } finally {manager.dispose();}
});


test("workspace-bound proving cannot spawn after a switch during native environment preparation", async () => {
  const fixture = globalThis.launchCredentialFixture = { spawned: [], saved: [] };
  const context = { workspaceState: { get: (_key, fallback) => fallback, update: async (_key, value) => fixture.saved.push(value) } };
  let current = true;
  const project = { found: true, root: "/tmp/scope-A", stateRoot: "/tmp/scope-A/.leanflow/workflow-state", label: "A" };
  const manager = new RunManager(context, project, async () => { current = false; return {}; });
  manager.startPolling = () => {};
  try {
    const run = await manager.launch({ ...emptyLaunchRequest(), provider: "codex" }, null, null,
      { projectRoot: project.root, assertCurrent() { if (!current) throw new Error("Formalization workspace changed"); } });
    assert.equal(fixture.spawned.length, 0);
    assert.equal(run.status, "failed");
    assert.match(run.error, /workspace changed/);
    assert.equal(run.projectRoot, project.root);
  } finally { manager.dispose(); }
});


for (const boundary of ["terminal preparation", "connection persistence"]) {
  test(`workspace-bound proving cannot spawn after a switch during ${boundary}`, async () => {
    const fixture = globalThis.launchCredentialFixture = { spawned: [], saved: [] };
    let current = true, saves = 0;
    const context = { workspaceState: { get: (_key, fallback) => fallback, update: async (_key, value) => {
      fixture.saved.push(value); saves += 1;
      if (boundary === "connection persistence" && saves === 2) current = false;
    } } };
    if (boundary === "terminal preparation") fixture.beforePrepared = () => { current = false; };
    const project = { found: true, root: "/tmp/scope-A", stateRoot: "/tmp/scope-A/.leanflow/workflow-state", label: "A" };
    const manager = new RunManager(context, project, async () => ({}));
    manager.startPolling = () => {};
    try {
      const run = await manager.launch({ ...emptyLaunchRequest(), provider: "codex" }, null, null,
        { projectRoot: project.root, assertCurrent() { if (!current) throw new Error("Formalization workspace changed"); } });
      assert.equal(fixture.spawned.length, 0);
      assert.equal(run.status, "failed");
      assert.match(run.error, /workspace changed/);
    } finally { manager.dispose(); }
  });
}


test("tracked source resume reuses exact native identity and retains separate transport tracking", async () => {
  const {f, manager} = observedManager();
  try {
    const request = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex", provider: "codex", model: "gpt-6.1-sol" };
    const run = await manager.launch(request, null, null, { sourceDocumentId: "paper-1" });
    run.status = "stopped";
    const resumed = await manager.launch(request, null, null, { sourceDocumentId: "paper-1", resumeTrackedId: run.id });
    assert.notEqual(resumed.id, run.id);
    assert.equal(resumed.runId, run.runId);
    assert.deepEqual(resumed.formalizationSource, { documentId: "paper-1", path: "paper.tex" });
    const env = f.spawned.at(-1).env.set;
    assert.equal(env.LEANFLOW_WORKFLOW_RUN_ID, run.runId);
    assert.equal(env.LEANFLOW_NATIVE_RESUME_RUN_ID, run.runId);
    assert.equal(f.spawned.at(-1).argv[0], "formalize");
    assert.equal(f.spawned.at(-1).argv.includes("--from-formalization"), false);
  } finally { manager.dispose(); }
});

test("formalization resume restores preview-pinned identity and draft intent despite changed form", async () => {
  const {f, manager} = observedManager();
  try {
    const original = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex", sourceDraftOnly: true };
    const run = await manager.launch(original, null, null, { sourceDocumentId: "paper-1" });
    run.status = "stopped";
    assert.deepEqual(run.formalizationRuntime, { provider: "codex", model: "gpt-6.1-sol", sourceDraftOnly: true });
    assert.equal(original.provider, "");
    assert.equal(original.model, "");
    const changed = { ...original, provider: "claude", model: "claude-new-model", sourceDraftOnly: false,
      profile: "current-budget", overrides: { LEANFLOW_PROVER_AGENT_CALLS: "15" } };
    const bound = await manager.bindFormalizationResumeRequest(run.id, changed, run.projectRoot, "paper-1");
    assert.deepEqual({ provider: bound.provider, model: bound.model, sourceDraftOnly: bound.sourceDraftOnly }, run.formalizationRuntime);
    assert.equal(bound.profile, changed.profile);
    assert.deepEqual(bound.overrides, changed.overrides);
    const resumed = await manager.launch({ ...changed, profile: "", overrides: {} }, null, null,
      { sourceDocumentId: "paper-1", resumeTrackedId: run.id });
    const argv = f.spawned.at(-1).argv;
    assert.equal(argv[argv.indexOf("--provider") + 1], "codex");
    assert.equal(argv[argv.indexOf("--model") + 1], "gpt-6.1-sol");
    assert.equal(argv.includes("--source-draft-only"), true);
    assert.deepEqual(resumed.formalizationRuntime, run.formalizationRuntime);
    assert.equal(f.previews.length, 1);
    assert.equal(f.metricsReads, undefined);
  } finally { manager.dispose(); }
});

test("legacy blank launch identity cannot be reconstructed from today's explicit form", async () => {
  const {f, manager} = observedManager();
  try {
    const request = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex" };
    const run = await manager.launch(request, null, null, { sourceDocumentId: "paper-1" });
    run.status = "stopped";
    delete run.formalizationRuntime;
    run.request.provider = ""; run.request.model = "";
    await assert.rejects(manager.launch({ ...request, provider: "claude", model: "new-default" }, null, null,
      { sourceDocumentId: "paper-1", resumeTrackedId: run.id }), /no verified original provider and model.*CLI/);
    assert.equal(f.spawned.length, 1);
    assert.equal(f.previews.length, 1);
  } finally { manager.dispose(); }
});

test("new launch retains a known provider with unknown native-default model without guessing resume", async () => {
  const {f, manager} = observedManager();
  try {
    f.preview = { version: 1, project: { root: manager.projectRoot() }, workflow: { kind: "formalize", source_draft_only: false }, runtime: { provider: "claude", model: "" } };
    const request = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex" };
    const run = await manager.launch(request, null, null, { sourceDocumentId: "paper-1" });
    assert.deepEqual(run.formalizationRuntime, { provider: "claude", model: "", sourceDraftOnly: false });
    assert.equal(f.spawned[0].argv[f.spawned[0].argv.indexOf("--provider") + 1], "claude");
    assert.equal(f.spawned[0].argv.includes("--model"), false);
    run.status = "stopped";
    await assert.rejects(manager.bindFormalizationResumeRequest(run.id, { ...request, model: "today-default" }, run.projectRoot, "paper-1"), /no verified original provider and model/);
  } finally { manager.dispose(); }
});

for (const invalid of ["", "scope", "run", "root", "document", "source", "draft", "model"]) {
  test(`legacy identity recovery requires exact durable launch binding (${invalid || "valid"})`, async () => {
    const {f, manager} = observedManager();
    try {
      const request = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex" };
      const run = await manager.launch(request, null, null, { sourceDocumentId: "paper-1" });
      run.status = "stopped";
      delete run.formalizationRuntime;
      run.request.provider = ""; run.request.model = "";
      const values = { LEANFLOW_WORKFLOW_RUN_ID: run.runId, LEANFLOW_PROJECT_ROOT: run.projectRoot,
        LEANFLOW_NATIVE_WORKFLOW_KIND: "formalize", LEANFLOW_FORMALIZATION_DOCUMENT_ID: "paper-1",
        LEANFLOW_NATIVE_REQUESTED_TARGET: "paper.tex", LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY: "true" };
      f.metrics = { scope: { exact: true, requested_run_id: run.runId, resolved_run_id: run.runId },
        run: { run_id: run.runId, workflow_kind: "formalize" },
        launch: { environment: { values, runtime: { provider: "lemex", model: "original-model", api_key: "opaque-private-key" } } } };
      if (invalid === "scope") f.metrics.scope.exact = false;
      if (invalid === "run") f.metrics.scope.resolved_run_id = "another-run";
      if (invalid === "root") values.LEANFLOW_PROJECT_ROOT = "/another/root";
      if (invalid === "document") values.LEANFLOW_FORMALIZATION_DOCUMENT_ID = "another-paper";
      if (invalid === "source") values.LEANFLOW_NATIVE_REQUESTED_TARGET = "another.tex";
      if (invalid === "draft") delete values.LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY;
      if (invalid === "model") f.metrics.launch.environment.runtime.model = "";
      const changed = { ...request, provider: "claude", model: "new-model", sourceDraftOnly: false };
      if (invalid) {
        await assert.rejects(manager.bindFormalizationResumeRequest(run.id, changed, run.projectRoot, "paper-1"), /no verified original provider and model/);
        assert.equal(run.formalizationRuntime, undefined);
      } else {
        const bound = await manager.bindFormalizationResumeRequest(run.id, changed, run.projectRoot, "paper-1");
        assert.equal(bound.provider, "lemex"); assert.equal(bound.model, "original-model");
        assert.equal(bound.sourceDraftOnly, true);
        assert.deepEqual(run.formalizationRuntime, { provider: "lemex", model: "original-model", sourceDraftOnly: true });
        assert.equal(JSON.stringify(manager.snapshot()).includes("opaque-private-key"), false);
        assert.equal(JSON.stringify(f.saved).includes("opaque-private-key"), false);
      }
      assert.equal(f.spawned.length, 1);
    } finally { manager.dispose(); }
  });
}

for (const mismatch of ["untracked", "active", "workspace", "document", "source", "retired", "legacy", "runId"]) {
  test(`source resume rejects ${mismatch} identity before process admission`, async () => {
    const {f, manager} = observedManager();
    try {
      const request = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex" };
      const run = await manager.launch(request, null, null, { sourceDocumentId: "paper-1" });
      run.status = "stopped";
      let tracked = run.id;
      if (mismatch === "untracked") tracked = "external-history";
      if (mismatch === "active") run.status = "running";
      if (mismatch === "workspace") run.projectRoot = "/other/project";
      if (mismatch === "document") run.formalizationSource.documentId = "other-paper";
      if (mismatch === "source") run.request.target = "other.tex";
      if (mismatch === "retired") run.request.kind = "draft";
      if (mismatch === "legacy") delete run.formalizationSource;
      if (mismatch === "runId") run.runId = "../../unsafe";
      await assert.rejects(manager.launch(request, null, null, { sourceDocumentId: "paper-1", resumeTrackedId: tracked }), /stopped extension-tracked/);
      assert.equal(f.spawned.length, 1);
    } finally { manager.dispose(); }
  });
}


test("resume awaits one shared old-pane retirement before spawning the exact campaign", async () => {
  const {f, manager} = observedManager();
  try {
    const request = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex" };
    const run = await manager.launch(request, null, null, { sourceDocumentId: "paper-1" });
    run.status = "stopped";
    run.connection = { kind: "tmux" };
    let release;
    f.releasePending = new Promise(resolve => { release = resolve; });
    // Simulate a window-restoration cleanup already in flight when Resume is clicked.
    const cleanup = manager.releaseTerminal(run);
    const resumed = manager.launch(request, null, null, { sourceDocumentId: "paper-1", resumeTrackedId: run.id });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.spawned.length, 1);
    assert.deepEqual(f.released, [run.runId]);
    release(true);
    await cleanup;
    const result = await resumed;
    assert.equal(f.spawned.length, 2);
    assert.equal(result.runId, run.runId);
    assert.equal(run.connection, undefined);
    await manager.releaseTerminal(run);
    assert.equal(f.released.length, 1);
  } finally { manager.dispose(); }
});

test("resume refuses an old pane whose physical terminal closure is unconfirmed", async () => {
  const {f, manager} = observedManager();
  try {
    const request = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex" };
    const run = await manager.launch(request, null, null, { sourceDocumentId: "paper-1" });
    run.status = "stopped";
    run.connection = { kind: "tmux" };
    f.releaseOutcome = false;
    await assert.rejects(manager.launch(request, null, null, { sourceDocumentId: "paper-1", resumeTrackedId: run.id }), /closure cannot be confirmed/);
    assert.equal(f.spawned.length, 1);
    assert.deepEqual(run.connection, { kind: "tmux" });
  } finally { manager.dispose(); }
});

for (const identity of [
  { provider: "claude", model: "claude-model[1m]" },
  { provider: "claude", model: "publishers/anthropic/models/claude-model@20260101" },
  { provider: "lemex", model: "org/model@release" },
]) {
  test(`formalization launch and resume retain native model syntax ${identity.model}`, async () => {
    const { f, manager } = observedManager();
    try {
      const request = { ...emptyLaunchRequest(), kind: "formalize", target: "paper.tex", ...identity };
      const run = await manager.launch(request, null, null, { sourceDocumentId: "paper-1" });
      assert.equal(run.status, "running");
      assert.deepEqual(run.formalizationRuntime, { ...identity, sourceDraftOnly: false });
      run.status = "stopped";
      const changed = { ...request, provider: "codex", model: "changed-model" };
      const resumed = await manager.launch(changed, null, null, { sourceDocumentId: "paper-1", resumeTrackedId: run.id });
      assert.equal(resumed.status, "running");
      const argv = f.spawned.at(-1).argv;
      assert.equal(argv[argv.indexOf("--provider") + 1], identity.provider);
      assert.equal(argv[argv.indexOf("--model") + 1], identity.model);
      assert.deepEqual(resumed.formalizationRuntime, run.formalizationRuntime);
      assert.equal(f.previews.length, 1);
    } finally { manager.dispose(); }
  });
}
