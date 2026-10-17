/** Exercise setup requests and retry state without touching a user project. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";

const bundle = await build({
  stdin: { contents: 'export { ProjectSetup, chooseSetupTarget } from "./src/core/projectSetup"; export { isHostFileUri, hostFileUri } from "./src/core/workspaceUris";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "setup-test-host", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      const f = () => globalThis.setupFixture;
      export class EventEmitter { event = () => ({dispose(){}}); fire(){} dispose(){} }
      export const Uri = {file: value => f().uri(value)};
      export const ProgressLocation = {Notification: 15};
      export const env = { get remoteName() {return f().remoteName;} };
      export const commands = {executeCommand: async (...args) => f().commands.push(args)};
      export const workspace = {
        get workspaceFolders(){return f().folders;},
        getWorkspaceFolder: uri => f().folders.find(folder => uri.fsPath.startsWith(folder.uri.fsPath+'/')),
        openTextDocument: async uri => {f().opened.push(uri); return uri;},
      };
      export const window = {
        get activeTextEditor(){return f().active ? {document:{uri:f().active}} : undefined;},
        showQuickPick: async choices => {f().choices = choices; return choices[f().pickIndex];},
        showOpenDialog: async options => {f().dialog = options; return f().destination ? [f().destination] : undefined;},
        createOutputChannel: () => ({clear(){f().logs=[];},appendLine(line){f().logs.push(line);},show(){f().shown=true;},dispose(){}}),
        showTextDocument: async () => {},
        withProgress: async (options, task) => {f().progressOptions=options; if(f().progressPending) await f().progressPending; f().beforeProgress?.(); return task({report: x => f().progress.push(x)}, {isCancellationRequested:f().initialCancellation === true,onCancellationRequested: callback => {f().cancel=callback; return {dispose(){}};}});},
      };
    ` }));
    builder.onLoad({ filter: /src\/core\/cli\.ts$/ }, () => ({ contents: `
      export const cliPath = () => '/bin/leanflow';
      export async function runCli(args) { const f=globalThis.setupFixture; f.inspectArgs=args; if(f.inspectError)throw new Error('older CLI'); return JSON.stringify(f.inspectionResult ? await f.inspectionResult : f.inspection); }
    ` }));
    builder.onLoad({ filter: /src\/core\/projectSetupProcess\.ts$/ }, () => ({ contents: `
      export function startProjectSetup(bin,target,onEvent) {const f=globalThis.setupFixture; f.requests.push({bin,target}); f.onEvent=onEvent; const item=f.results.shift(); onEvent({type:'progress',phase:'dependencies',message:'Downloading'}); return { result: item instanceof Promise ? item : Promise.resolve(item), cancel() {f.cancelled++; f.resolve?.({type:'result',status:'cancelled',root:target.root,phase:'dependencies',code:'cancelled',message:'Cancelled',remediation:'Retry',log_path:''});} };}
    ` }));
  } }],
});
const { ProjectSetup, chooseSetupTarget, isHostFileUri, hostFileUri } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

function fixture() {
  const uri = (fsPath, scheme = "file", authority = "") => ({fsPath,scheme,authority,with(values){return uri(values.path, scheme, authority);}});
  const f = globalThis.setupFixture = { uri, folders: [], active: null, remoteName: undefined, pickIndex: undefined, destination: undefined, commands: [], opened: [], requests: [], results: [], progress: [], logs: [], cancelled: 0, completed: 0, activeRun: false, projectRoot: "", inspection: { root: "/project", setup_status: "unknown" } };
  f.setup = new ProjectSetup({projectRoot:()=>f.projectRoot,activeRun:()=>f.activeRun,completed:async()=>{f.completed++; await f.setup.refresh(true);}});
  f.folder = (root, scheme, authority) => ({name: root.split('/').at(-1), uri: uri(root, scheme, authority)});
  f.result = (status, values = {}) => ({type:"result",status,root:"/project",phase:"verification",code:status === "ready"?"":"verify_failed",message:status === "ready"?"Verification ready":"Verification failed",remediation:status === "ready"?"":"Retry after fixing prerequisites",log_path:"/logs/setup.log",...values});
  return f;
}

test("empty, standalone-file and existing project folders share one-click Mathlib setup", async () => {
  for (const kind of ["empty", "standalone", "existing"]) {
    const f = fixture();
    try {
      f.folders = [f.folder("/project")];
      if(kind === "standalone") f.active = f.uri("/project/Problem.lean");
      if(kind === "existing") f.projectRoot="/project";
      f.results.push(f.result("ready"));
      await f.setup.start();
      assert.deepEqual(f.requests, [{bin:"/bin/leanflow",target:{root:"/project"}}]);
      assert.equal(f.setup.state().status,"ready");
      assert.equal(f.completed,1);
      assert.equal(f.progressOptions.cancellable,true);
      assert.deepEqual(f.progress,[{message:"Downloading"}]);
    } finally {f.setup.dispose();}
  }
});

test("failure retains exact request for retry and durable log; known failure blocks launches", async () => {
  const f=fixture();
  try {
    f.folders=[f.folder("/project")]; f.results.push(f.result("failed"),f.result("ready"));
    await f.setup.start();
    assert.equal(f.setup.state().canRetry,true);
    assert.throws(()=>f.setup.assertLaunchReady("/project"),/Retry/);
    f.setup.assertLaunchReady("/other");
    await f.setup.showLog();
    assert.match(f.setup.state().recentOutput.at(-1), /Could not read the saved setup log/);
    f.folders=[f.folder("/other")];
    await f.setup.start(true);
    assert.equal(f.requests[1].target.root,"/project");
    assert.equal(f.setup.state().status,"ready");
    f.setup.assertLaunchReady("/project");
  } finally {f.setup.dispose();}
});

test("multi-root follows active editor or asks; existing nested root is retained", async () => {
  const f=fixture();
  try {
    f.folders=[f.folder("/first"),f.folder("/second")]; f.active=f.uri("/second/Problem.lean");
    assert.deepEqual(await chooseSetupTarget("/first"),{root:"/second"});
    f.active=null; f.pickIndex=1;
    assert.deepEqual(await chooseSetupTarget("/first"),{root:"/second"});
    assert.equal(f.choices.length,2);
    f.folders=[f.folder("/parent")]; f.active=f.uri("/parent/project/Main.lean");
    assert.deepEqual(await chooseSetupTarget("/parent/project"),{root:"/parent/project"});
  } finally {f.setup.dispose();}
});

test("loose files are copied to a chosen destination and open only after readiness", async () => {
  const f=fixture();
  try {
    f.active=f.uri("/documents/Problem.lean"); f.destination=f.uri("/project");
    f.results.push(f.result("failed"), f.result("ready"));
    await f.setup.start();
    assert.deepEqual(f.requests[0].target,{root:"/project",sourceFile:"/documents/Problem.lean",openFolder:true});
    assert.equal(f.commands.length,0);
    await f.setup.start(true);
    assert.equal(f.requests[1].target.sourceFile,"/documents/Problem.lean");
    assert.equal(f.commands[0][0],"vscode.openFolder");
    assert.equal(f.commands[0][1].fsPath,"/project");
  } finally {f.setup.dispose();}
});

test("active projects and cancelled folder selection never spawn setup", async () => {
  const f=fixture();
  try {
    await f.setup.start(); assert.equal(f.requests.length,0);
    f.folders=[f.folder("/project")]; f.activeRun=true;
    await assert.rejects(f.setup.start(),/workflow is running/);
    assert.equal(f.requests.length,0);
  } finally {f.setup.dispose();}
});

test("cancellation and concurrent clicks stay attached to the single owned setup", async () => {
  const f=fixture();
  try {
    f.folders=[f.folder("/project")]; f.results.push(new Promise(resolve=>{f.resolve=resolve;}));
    const running=f.setup.start();
    await new Promise(resolve=>setImmediate(resolve));
    await f.setup.start(); assert.equal(f.requests.length,1);
    f.cancel(); await running;
    assert.equal(f.cancelled,1); assert.equal(f.setup.state().status,"cancelled");
    assert.equal(f.setup.state().canRetry,true);
  } finally {f.setup.dispose();}
});

test("read-only inspection distinguishes known failure from absent legacy metadata", async () => {
  const f=fixture();
  try {
    f.projectRoot="/project";
    await f.setup.refresh(true); f.setup.assertLaunchReady("/project");
    assert.equal(f.setup.state().status,"idle");
    assert.deepEqual(f.inspectArgs,["project","inspect","/project","--json"]);
    f.inspection={root:"/project",setup_status:"failed",phase:"verification",message:"Missing REPL",remediation:"Retry setup"};
    await f.setup.refresh(true); assert.throws(()=>f.setup.assertLaunchReady("/project"),/Retry/);
    f.inspection={root:"/project",setup_status:"ready",ready:true};
    await f.setup.refresh(true); assert.equal(f.setup.state().status,"ready");
    f.projectRoot="/legacy"; f.inspectError=true;
    await f.setup.refresh(true); f.setup.assertLaunchReady("/legacy");
    assert.equal(f.setup.state().status,"idle");
  } finally {f.setup.dispose();}
});

test("remote folders stay on the current extension host and foreign authorities are rejected", async () => {
  const f=fixture();
  try {
    f.remoteName="ssh-remote"; f.folders=[f.folder("/remote/project","vscode-remote","ssh-remote+host")];
    f.active=f.uri("/remote/project/Problem.lean","vscode-remote","ssh-remote+host");
    assert.deepEqual(await chooseSetupTarget(""),{root:"/remote/project"});
    assert.equal(isHostFileUri(f.uri("/tmp/file","file")),true);
    assert.equal(isHostFileUri(f.uri("/tmp/file","vscode-remote","ssh-remote+other")),false);
    const opened=hostFileUri("/remote/log.txt");
    assert.equal(opened.scheme,"vscode-remote"); assert.equal(opened.authority,"ssh-remote+host");
  } finally {f.setup.dispose();}
});

test("Remote SSH host-local file URIs use the workspace without opening a folder dialog", async () => {
  const f=fixture();
  try {
    f.remoteName="ssh-remote";
    f.folders=[f.folder("/remote/project")];
    f.active=f.uri("/remote/project/Problem.lean");
    assert.deepEqual(await chooseSetupTarget(""),{root:"/remote/project"});
    assert.equal(f.dialog,undefined);
    assert.equal(hostFileUri("/remote/log.txt").fsPath,"/remote/log.txt");
  } finally {f.setup.dispose();}
});


test("ready results and persisted inspection retain bounded redacted source diagnostics", async () => {
  const f = fixture();
  try {
    f.folders = [f.folder("/project")];
    const capabilities = { source_diagnostics: ["Problem.lean: unknown module PrivateLibrary", null, "", "token=sk-test-private-source-token"] };
    f.results.push(f.result("ready", { capabilities }));
    await f.setup.start();
    assert.equal(f.setup.state().status, "ready");
    assert.equal(f.setup.state().sourceDiagnostics.length, 2);
    assert.match(f.setup.state().sourceDiagnostics[0], /unknown module PrivateLibrary/);
    assert.ok(!JSON.stringify(f.setup.state()).includes("sk-test-private-source-token"));
    f.setup.assertLaunchReady("/project");
    f.inspection = { root: "/project", setup_status: "ready", capabilities };
    await f.setup.refresh(true);
    assert.equal(f.setup.state().sourceDiagnostics.length, 2);
    f.inspection.capabilities = { source_diagnostics: Array.from({ length: 100 }, () => "x".repeat(5000)) };
    await f.setup.refresh(true);
    assert.equal(f.setup.state().sourceDiagnostics.length, 20);
    assert.equal(f.setup.state().sourceDiagnostics[0].length, 2000);
    f.inspection.capabilities = { source_diagnostics: "malformed" };
    await f.setup.refresh(true);
    assert.deepEqual(f.setup.state().sourceDiagnostics, []);
  } finally { f.setup.dispose(); }
});


test("repair and retry retain the explicit heavy operation", async () => {
  const f = fixture();
  try {
    f.folders = [f.folder("/project")]; f.results.push(f.result("failed"), f.result("ready"));
    await f.setup.start(false, true);
    await f.setup.start(true);
    assert.deepEqual(f.requests.map(item => item.target), [{root:"/project",repair:true}, {root:"/project",repair:true}]);
    assert.match(f.logs[0], /Repairing/);
  } finally { f.setup.dispose(); }
});

test("live setup logs are bounded, redacted and available before completion", async () => {
  const f = fixture();
  try {
    f.folders = [f.folder("/project")];
    f.results.push(new Promise(resolve => { f.resolve = resolve; }));
    const running = f.setup.start();
    await new Promise(resolve => setImmediate(resolve));
    for (let i = 0; i < 100; i++) f.onEvent({type:"output",phase:"cache",message:`line ${i} token=sk-test-private-setup-token`,log_path:"/logs/current.log"});
    await f.setup.showLog();
    const state = f.setup.state();
    assert.equal(state.status, "running");
    assert.equal(state.logPath, "/logs/current.log");
    assert.equal(state.recentOutput.length, 40);
    assert.match(state.recentOutput.at(-1), /line 99/);
    assert.ok(!JSON.stringify(state).includes("sk-test-private-setup-token"));
    assert.deepEqual(f.opened, []);
    f.resolve(f.result("ready")); await running;
  } finally { f.setup.dispose(); }
});

test("saved setup log loads inline after reload without opening another editor", async () => {
  const f = fixture();
  const root = await mkdtemp(path.join(tmpdir(), "leanflow-setup-log-"));
  try {
    const log = path.join(root, "setup.log");
    await writeFile(log, "x".repeat(40000) + "\n" + Array.from({length:100}, (_,i)=>`step ${i}`).join("\n") + "\ntoken=sk-test-private-saved-token");
    f.projectRoot = root;
    f.inspection = {root, setup_status:"ready", log_path:log};
    await f.setup.refresh(true);
    await f.setup.showLog();
    const output = f.setup.state().recentOutput;
    assert.equal(output.length, 80);
    assert.ok(output.includes("step 99"));
    assert.ok(!output.join("\n").includes("sk-test-private-saved-token"));
    assert.deepEqual(f.opened, []);
  } finally { f.setup.dispose(); await rm(root, {recursive:true,force:true}); }
});

test("explicit workspace setup ignores editor focus and keeps retry repair intent within its root", async () => {
  const f = fixture();
  try {
    f.folders = [f.folder("/editor"), f.folder("/paper")];
    f.active = f.uri("/editor/Problem.lean");
    f.projectRoot = "/editor";
    f.results.push(f.result("failed", {root:"/paper"}), f.result("ready", {root:"/paper"}), f.result("ready", {root:"/other"}));
    await f.setup.startForRoot("/paper", {repair:true});
    await f.setup.startForRoot("/paper", {retry:true});
    await f.setup.startForRoot("/other", {retry:true});
    assert.deepEqual(f.requests.map(item => item.target), [{root:"/paper",repair:true}, {root:"/paper",repair:true}, {root:"/other"}]);
    assert.equal(f.choices, undefined);
    assert.equal(f.dialog, undefined);
    assert.deepEqual(f.commands, []);
    assert.equal(f.setup.stateForRoot("/paper").status, "ready");
    assert.equal(f.setup.stateForRoot("/other").status, "ready");
  } finally { f.setup.dispose(); }
});

test("other-root inspection and scoped controls never replace or cancel the active setup", async () => {
  const f = fixture();
  try {
    f.results.push(new Promise(resolve => { f.resolve = resolve; }));
    const running = f.setup.startForRoot("/private-paper-a");
    await new Promise(resolve => setImmediate(resolve));
    f.onEvent({type:"output",phase:"cache",message:"private-a-output",log_path:"/private-paper-a/setup.log"});
    const unknown = f.setup.stateForRoot("/paper-b");
    assert.equal(unknown.root, "/paper-b");
    assert.equal(unknown.status, "idle");
    assert.equal(unknown.activeRun, true);
    assert.equal(unknown.code, "setup_busy_other_root");
    assert.ok(!JSON.stringify(unknown).includes("private-paper-a"));
    assert.ok(!JSON.stringify(unknown).includes("private-a-output"));
    f.inspection = {root:"/paper-b",setup_status:"ready",ready:true,configured:true,message:"B tools ready"};
    await f.setup.refresh(true, "/paper-b");
    assert.equal(f.setup.state().root, "/private-paper-a");
    assert.equal(f.setup.state().status, "running");
    assert.equal(f.setup.stateForRoot("/paper-b").status, "ready");
    assert.equal(f.setup.stateForRoot("/paper-b").activeRun, true);
    await assert.rejects(f.setup.startForRoot("/paper-b"), /already running/);
    await assert.rejects(f.setup.startForRoot("/private-paper-a"), /already running/);
    assert.throws(() => f.setup.cancelForRoot("/paper-b"), /No active/);
    await assert.rejects(f.setup.showLogForRoot("/paper-b"), /Another project setup/);
    await f.setup.start();
    assert.equal(f.requests.length, 1);
    assert.equal(f.cancelled, 0);
    f.setup.cancelForRoot("/private-paper-a");
    await running;
    assert.equal(f.cancelled, 1);
    assert.equal(f.setup.stateForRoot("/paper-b").activeRun, false);
  } finally { f.setup.dispose(); }
});

test("cancellation before a deferred progress callback prevents process admission and does not affect the next setup", async () => {
  const f = fixture();
  try {
    let releaseProgress;
    f.progressPending = new Promise(resolve => { releaseProgress = resolve; });
    const running = f.setup.startForRoot("/paper");
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.setup.stateForRoot("/paper").status, "running");
    assert.equal(f.requests.length, 0);
    f.setup.cancelForRoot("/paper");
    assert.match(f.setup.stateForRoot("/paper").message, /Cancelling/);
    releaseProgress();
    await running;
    assert.equal(f.requests.length, 0);
    assert.equal(f.cancelled, 0);
    assert.equal(f.setup.stateForRoot("/paper").status, "cancelled");
    assert.equal(f.setup.stateForRoot("/paper").canRetry, true);
    assert.equal(f.setup.stateForRoot("/other").activeRun, false);
    assert.throws(() => f.setup.cancelForRoot("/paper"), /No active/);
    f.progressPending = undefined;
    f.results.push(f.result("ready", {root:"/paper"}));
    await f.setup.startForRoot("/paper", {retry:true});
    assert.equal(f.requests.length, 1);
    assert.equal(f.cancelled, 0);
    assert.equal(f.setup.stateForRoot("/paper").status, "ready");
  } finally { f.setup.dispose(); }
});

test("an already cancelled progress notification never starts project setup", async () => {
  const f = fixture();
  try {
    f.initialCancellation = true;
    await f.setup.startForRoot("/paper");
    assert.equal(f.requests.length, 0);
    assert.equal(f.setup.stateForRoot("/paper").status, "cancelled");
    assert.equal(f.setup.stateForRoot("/other").activeRun, false);
  } finally { f.setup.dispose(); }
});

test("disposing before a deferred progress callback prevents setup from starting after the owner closes", async () => {
  const f = fixture();
  let releaseProgress;
  f.progressPending = new Promise(resolve => { releaseProgress = resolve; });
  const running = f.setup.startForRoot("/paper");
  await new Promise(resolve => setImmediate(resolve));
  f.setup.dispose();
  releaseProgress();
  await running;
  assert.equal(f.requests.length, 0);
  assert.equal(f.setup.stateForRoot("/paper").status, "cancelled");
  assert.equal(f.setup.stateForRoot("/other").activeRun, false);
});

test("scoped inspections check current ownership before reading and before installing readiness", async () => {
  const f = fixture();
  try {
    await f.setup.refresh(true, "/paper-b", () => false);
    assert.equal(f.inspectArgs, undefined);
    let current = true;
    let finish;
    f.inspectionResult = new Promise(resolve => { finish = resolve; });
    const refresh = f.setup.refresh(true, "/paper-b", () => current);
    assert.deepEqual(f.inspectArgs, ["project","inspect","/paper-b","--json"]);
    current = false;
    finish({root:"/paper-b",setup_status:"ready",ready:true,configured:true});
    await refresh;
    assert.equal(f.setup.stateForRoot("/paper-b").status, "idle");
    assert.equal(f.setup.state().root, "");
  } finally { f.setup.dispose(); }
});

test("a stale workspace cannot spawn setup, including after notification dispatch", async () => {
  const f = fixture();
  try {
    await assert.rejects(f.setup.startForRoot("/paper", {isCurrent:() => false}), /workspace changed/);
    await assert.rejects(f.setup.startForRoot("relative-paper"), /source workspace/);
    assert.equal(f.requests.length, 0);
    let current = true;
    f.beforeProgress = () => { current = false; };
    await assert.rejects(f.setup.startForRoot("/paper", {isCurrent:() => current}), /workspace changed/);
    assert.equal(f.requests.length, 0);
    assert.equal(f.setup.stateForRoot("/paper").status, "failed");
    assert.equal(f.setup.stateForRoot("/paper").canRetry, true);
  } finally { f.setup.dispose(); }
});

test("late setup completion retains evidence without reselecting a workspace that is no longer current", async () => {
  const f = fixture();
  try {
    let current = true;
    f.results.push(new Promise(resolve => { f.resolve = resolve; }));
    const running = f.setup.startForRoot("/paper-a", {isCurrent:() => current});
    await new Promise(resolve => setImmediate(resolve));
    current = false;
    f.resolve(f.result("ready", {root:"/paper-a",log_path:"/paper-a/setup.log"}));
    await running;
    assert.equal(f.completed, 0);
    assert.equal(f.setup.stateForRoot("/paper-a").status, "ready");
    assert.equal(f.setup.stateForRoot("/paper-a").logPath, "/paper-a/setup.log");
    f.inspection = {root:"/paper-b",setup_status:"ready",ready:true,configured:true};
    await f.setup.refresh(true, "/paper-b");
    assert.equal(f.setup.state().root, "/paper-b");
    assert.equal(f.setup.stateForRoot("/paper-a").status, "ready");
    assert.equal(f.setup.stateForRoot("/paper-b").logPath, "");
    assert.deepEqual(f.commands, []);
  } finally { f.setup.dispose(); }
});

test("formalization readiness requires both verified tools and project registration", async () => {
  const f = fixture();
  try {
    for (const flags of [{ready:true,configured:false}, {ready:false,configured:true}, {ready:true}]) {
      f.inspection = {root:"/paper",setup_status:"ready",...flags};
      await f.setup.refresh(true, "/paper");
      assert.equal(f.setup.stateForRoot("/paper").status, "detected");
      assert.equal(f.setup.stateForRoot("/paper").code, "setup_baseline_required");
    }
    f.inspection = {root:"/paper",setup_status:"ready",ready:true,configured:true};
    await f.setup.refresh(true, "/paper");
    assert.equal(f.setup.stateForRoot("/paper").status, "ready");
    f.projectRoot = "/paper";
    f.inspection = {root:"/paper",setup_status:"ready"};
    await f.setup.refresh(true);
    assert.equal(f.setup.state().status, "ready");
    assert.equal(f.setup.stateForRoot("/paper").status, "detected");
    f.inspection = {root:"/paper",setup_status:"ready",ready:true,configured:false};
    await f.setup.refresh(true);
    assert.equal(f.setup.state().status, "ready");
    assert.equal(f.setup.stateForRoot("/paper").status, "detected");
  } finally { f.setup.dispose(); }
});

test("scoped protocol results cannot install another workspace's readiness or logs", async () => {
  const f = fixture();
  try {
    f.inspection = {root:"/private-a",setup_status:"ready",ready:true,configured:true,log_path:"/private-a/setup.log"};
    await f.setup.refresh(true, "/paper-b");
    assert.equal(f.setup.stateForRoot("/paper-b").status, "idle");
    assert.ok(!JSON.stringify(f.setup.stateForRoot("/paper-b")).includes("private-a"));
    f.results.push(f.result("ready", {root:"/private-a"}));
    await assert.rejects(f.setup.startForRoot("/paper-b"), /identify the selected workspace/);
    assert.equal(f.setup.stateForRoot("/paper-b").root, "/paper-b");
    assert.equal(f.setup.stateForRoot("/paper-b").status, "failed");
    assert.equal(f.completed, 0);
    f.inspection = {setup_status:"ready",ready:true,configured:true};
    await f.setup.refresh(true, "/paper-b");
    assert.equal(f.setup.stateForRoot("/paper-b").status, "idle");
    f.results.push(f.result("ready", {root:""}));
    await assert.rejects(f.setup.startForRoot("/paper-b"), /identify the selected workspace/);
    assert.equal(f.setup.stateForRoot("/paper-b").status, "failed");
    assert.throws(() => f.setup.cancelForRoot("/private-a"), /No active/);
    await assert.rejects(f.setup.showLogForRoot("/private-a"), /No setup log/);
  } finally { f.setup.dispose(); }
});

test("retained setup logs remain root-bound when another workspace is selected", async () => {
  const f = fixture();
  const root = await mkdtemp(path.join(tmpdir(), "leanflow-scoped-setup-log-"));
  try {
    const log = path.join(root, "setup.log");
    await writeFile(log, "Only paper B's setup output");
    f.inspection = {root,setup_status:"ready",ready:true,configured:true,log_path:log};
    await f.setup.refresh(true, root);
    f.inspection = {root:"/paper-a",setup_status:"ready",ready:true,configured:true};
    await f.setup.refresh(true, "/paper-a");
    await f.setup.showLogForRoot(root);
    assert.deepEqual(f.setup.stateForRoot(root).recentOutput, ["Only paper B's setup output"]);
    assert.equal(f.setup.state().root, "/paper-a");
    assert.deepEqual(f.setup.state().recentOutput, []);
    const exposed = f.setup.stateForRoot(root);
    exposed.recentOutput.push("External mutation");
    assert.deepEqual(f.setup.stateForRoot(root).recentOutput, ["Only paper B's setup output"]);
  } finally { f.setup.dispose(); await rm(root, {recursive:true,force:true}); }
});
