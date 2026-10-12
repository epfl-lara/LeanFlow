/** Real disposable child processes exercise streaming, failures and cancellation. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { startProjectSetup, parseSetupEvent } from "../dist/test/projectSetupProcess.mjs";
import { projectSetupBlocksLaunch } from "../dist/test/projectSetupTypes.mjs";

async function fakeCli(body) {
  const root = await mkdtemp(path.join(tmpdir(), "leanflow-setup-process-"));
  const bin = path.join(root, "leanflow-test");
  await writeFile(bin, `#!/usr/bin/env node\nconst send = data => process.stdout.write(JSON.stringify(data)+'\\n');\n${body}\n`);
  await chmod(bin, 0o755);
  return { bin, root, dispose: () => rm(root, { recursive: true, force: true }) };
}

test("setup streams progress/output and keeps Mathlib default and source paths as separate arguments", async () => {
  const f = await fakeCli(`send({type:'progress',phase:'dependencies',message:'Downloading'}); console.error('build output'); send({type:'output',phase:'dependencies',message:JSON.stringify(process.argv.slice(2))}); setTimeout(() => send({type:'result',status:'ready',phase:'verification',message:'Ready',root:process.argv[4]}),30);`);
  try {
    const events = [];
    const result = await startProjectSetup(f.bin, { root: f.root, sourceFile: "/some folder/input.lean" }, e => events.push(e)).result;
    assert.equal(result.status, "ready");
    assert.equal(events[0].message, "Downloading");
    assert.ok(events.some(e => e.message === "build output"));
    assert.deepEqual(JSON.parse(events.find(e => e.message.startsWith("[")).message), ["project", "setup", f.root, "--preset", "mathlib", "--json", "--source-file", "/some folder/input.lean"]);
  } finally { await f.dispose(); }
});

test("nonzero setup result retains actionable phase and remediation; missing final never reports ready", async () => {
  for (const [body, expected] of [
    [`send({type:'result',status:'failed',phase:'dependencies',code:'network',message:'Network failed',remediation:'Check connection',log_path:'/tmp/setup.log'}); process.exitCode=1;`, "network"],
    [`send({type:'progress',phase:'verification',message:'Checking'});`, "setup_protocol_error"],
    [`send({type:'result',status:'ready'}); process.exitCode=2;`, "setup_protocol_error"],
  ]) {
    const f = await fakeCli(body);
    try {
      const result = await startProjectSetup(f.bin, { root: f.root }, () => {}).result;
      assert.equal(result.status, "failed"); assert.equal(result.code, expected);
      if (expected === "network") { assert.equal(result.phase, "dependencies"); assert.equal(result.remediation, "Check connection"); assert.equal(result.log_path, "/tmp/setup.log"); }
    } finally { await f.dispose(); }
  }
});

test("cancelling signals the owned setup process and resolves without a false readiness result", async () => {
  const f = await fakeCli(`process.on('SIGINT', () => { send({type:'result',status:'cancelled',phase:'dependencies',message:'Cancelled'}); process.exit(130); }); send({type:'progress',phase:'dependencies',message:'working'}); setInterval(() => {},1000);`);
  try {
    let owned;
    const progressed = new Promise(resolve => { owned = startProjectSetup(f.bin, { root: f.root }, () => resolve()); });
    await progressed;
    owned.cancel();
    const result = await owned.result;
    assert.equal(result.status, "cancelled");
    assert.equal(result.phase, "dependencies");
    owned.cancel();
  } finally { await f.dispose(); }
});

test("missing executable produces an install/configuration action, not a hang", async () => {
  const result = await startProjectSetup("/missing/leanflow", { root: "/tmp" }, () => {}).result;
  assert.equal(result.code, "cli_missing");
  assert.match(result.remediation, /install guide.*CLI Path/);
});

test("parser discards malformed protocol and launch gating preserves uninspected legacy projects", () => {
  assert.equal(parseSetupEvent("[]"), null);
  assert.equal(parseSetupEvent('{"type":"result","status":"invented"}'), null);
  assert.equal(projectSetupBlocksLaunch(undefined, "/project"), false);
  assert.equal(projectSetupBlocksLaunch({ status: "idle", root: "/project" }, "/project"), false);
  assert.equal(projectSetupBlocksLaunch({ status: "failed", root: "/project" }, "/project"), true);
  assert.equal(projectSetupBlocksLaunch({ status: "running", root: "/other" }, "/project"), false);
});


test("explicit repair reaches the CLI and progress carries the live log path", async () => {
  const f = await fakeCli(`send({type:'progress',phase:'verification',message:JSON.stringify(process.argv.slice(2)),log_path:'/tmp/live-setup.log'}); send({type:'result',status:'ready',root:process.argv[4]});`);
  try {
    const events = [];
    await startProjectSetup(f.bin, {root:f.root,repair:true}, event=>events.push(event)).result;
    assert.equal(events[0].log_path, "/tmp/live-setup.log");
    assert.deepEqual(JSON.parse(events[0].message), ["project","setup",f.root,"--preset","mathlib","--json","--repair"]);
  } finally { await f.dispose(); }
});
