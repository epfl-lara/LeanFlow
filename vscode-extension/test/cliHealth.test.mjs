/** Check failure classification, automatic recovery and obsolete-check races. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { setImmediate } from "node:timers/promises";
import { build } from "esbuild";

const bundle = await build({
  stdin: { contents: 'export { CliHealth } from "./src/core/cliHealth"; export { checkCli } from "./src/core/cli";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "health-host", setup(builder) {
    for (const name of ["vscode", "node:child_process"]) builder.onResolve({ filter: new RegExp(`^${name}$`) }, () => ({ path: name, namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, ({ path }) => ({ contents: path === "vscode" ? `
      export const workspace = { getConfiguration: () => ({ get: () => '/bin/leanflow' }) };
    ` : `
      import { promisify } from 'node:util';
      export const execFile = Object.assign(() => {}, { [promisify.custom]: () => globalThis.healthExecute() });
    ` }));
    builder.onLoad({ filter: /workflowTerminal\.ts$/ }, () => ({ contents: 'export async function launchWorkflowTerminal() { throw new Error("Unexpected launch"); }' }));
  } }],
});
const { CliHealth, checkCli } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const ready = { ok: true, path: "/bin/leanflow", version: "0.3.0", error: "" };
const timeout = { ...ready, ok: false, version: "", error: "Version check timed out", issue: "timeout" };

test("version failures distinguish timeouts, permissions, missing executable and runtime errors", async () => {
  for (const [error, issue, wording] of [
    [Object.assign(new Error("Command failed: leanflow version"), { killed: true, signal: "SIGTERM" }), "timeout", /10 seconds/],
    [Object.assign(new Error("ENOENT"), { code: "ENOENT" }), "missing", /not found/],
    [Object.assign(new Error("EACCES"), { code: "EACCES" }), "permission", /permissions/],
    [Object.assign(new Error("Command failed"), { code: 1, stderr: "Runtime import failed" }), "failed", /Runtime import failed/],
  ]) {
    globalThis.healthExecute = async () => { throw error; };
    const state = await checkCli();
    assert.equal(state.issue, issue);
    assert.match(state.error, wording);
  }
});

test("a transient failure retains the last version and recovers without a user refresh", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const responses = [ready, timeout, ready];
  let recovered = 0;
  const health = new CliHealth(async () => responses.shift(), () => {}, () => recovered++);
  try {
    await health.check(ready.path);
    await health.check(ready.path);
    assert.equal(health.state.health, "retrying");
    assert.equal(health.state.version, "0.3.0");
    assert.equal(health.state.ok, false);
    context.mock.timers.tick(5_000);
    await setImmediate();
    assert.equal(health.state.health, "ready");
    assert.equal(recovered, 1);
    assert.equal(responses.length, 0);
  } finally { health.dispose(); }
});

test("a first-check timeout also retries, with capped backoff and cancellation on dispose", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let calls = 0;
  const health = new CliHealth(async () => { calls++; return timeout; }, () => {}, () => {});
  await health.check(ready.path);
  for (const [delay, expected] of [[5_000,2],[15_000,3],[30_000,4],[30_000,5]]) {
    context.mock.timers.tick(delay - 1); await setImmediate(); assert.equal(calls, expected - 1);
    context.mock.timers.tick(1); await setImmediate(); assert.equal(calls, expected);
  }
  health.dispose();
  context.mock.timers.tick(60_000); await setImmediate(); assert.equal(calls, 5);
});

test("concurrent checks coalesce and an old path cannot overwrite the new selection", async () => {
  const resolvers = [];
  const health = new CliHealth(() => new Promise(resolve => resolvers.push(resolve)), () => {}, () => {});
  try {
    const old = health.check("old");
    assert.equal(health.check("old"), old);
    const current = health.check("new");
    resolvers[1]({ ...ready, path: "new" }); await current;
    resolvers[0]({ ...timeout, path: "old" }); await old;
    assert.equal(health.state.path, "new");
    assert.equal(health.state.health, "ready");
  } finally { health.dispose(); }
});

test("missing executables require setup and disposed probes never publish a late result", async context => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let calls = 0, changes = 0;
  const health = new CliHealth(async () => { calls++; return { ...timeout, issue: "missing" }; }, () => changes++, () => {});
  await health.check(ready.path);
  assert.equal(health.state.health, "unavailable");
  context.mock.timers.tick(60_000); await setImmediate(); assert.equal(calls, 1);
  const pending = health.check(ready.path);
  health.dispose();
  const count = changes;
  await pending;
  assert.equal(changes, count);
});
