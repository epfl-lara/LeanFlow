/** Exercise the actual run manager boundary so secrets cannot become run history. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";

const bundle = await build({
  stdin: { contents: 'export { RunManager } from "./src/core/runManager"; export { emptyLaunchRequest } from "./src/core/types";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "launch-test-host", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      export class EventEmitter { event = () => ({ dispose() {} }); fire() {} dispose() {} }
      export const workspace = { getConfiguration: () => ({ get: (_, fallback) => fallback }) };
    ` }));
    builder.onLoad({ filter: /src\/core\/workflowTerminal\.ts$/ }, () => ({ contents: `export async function inspectWorkflowTerminal() { return globalThis.launchCredentialFixture.pane ?? {state:"unknown"}; } export async function releaseWorkflowTerminal(id) { (globalThis.launchCredentialFixture.released ??= []).push(id); return true; } export async function showWorkflowTerminal() {}` }));
    builder.onLoad({ filter: /src\/core\/launchPaths\.ts$/ }, () => ({ contents: `export async function validateLaunchProjectInputs() {}` }));
    builder.onLoad({ filter: /src\/core\/cli\.ts$/ }, () => ({ contents: `
      export async function fetchLiveStatus() { if (globalThis.launchCredentialFixture.unreadable) throw new Error("temporarily unavailable"); return globalThis.launchCredentialFixture.status ?? null; }
      export async function fetchRuns() { if (globalThis.launchCredentialFixture.unreadable) throw new Error("temporarily unavailable"); return globalThis.launchCredentialFixture.history ?? []; }
      export async function fetchEventTypes() { return []; }
      export async function fetchEvents(root, runId) { (globalThis.launchCredentialFixture.followed ??= []).push(runId); return { events: [], cursor: "", runId }; }
      export async function fetchRunLog() { return ""; }
      export async function stopWorkflow() { return { stopped: true }; }
      export function spawnWorkflow(project, argv, env, nativeEnv) {
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

function observedManager() {
  const f = globalThis.launchCredentialFixture = {spawned:[],saved:[]};
  const context = {workspaceState:{get:(_key,fallback)=>fallback,update:async(_key,value)=>f.saved.push(structuredClone(value))}};
  const manager = new RunManager(context,{found:true,root:"/tmp/isolated-run-discovery",stateRoot:"/tmp/isolated-run-discovery/state"});
  manager.startPolling = () => {};
  manager.stopPolling = () => {};
  return {f,manager};
}

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
