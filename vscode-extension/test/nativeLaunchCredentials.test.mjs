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
    builder.onLoad({ filter: /src\/core\/launchPaths\.ts$/ }, () => ({ contents: `export async function validateLaunchProjectInputs() {}` }));
    builder.onLoad({ filter: /src\/core\/cli\.ts$/ }, () => ({ contents: `
      export async function fetchLiveStatus() { return null; }
      export async function fetchRuns() { return []; }
      export async function fetchEventTypes() { return []; }
      export async function fetchEvents() { return { events: [], cursor: "", runId: "" }; }
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
