/** Exercise safe launch, tmux reattachment and ordinary terminal fallback without model calls. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { promisify } from "node:util";
import { execFile, spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, writeFile, chmod, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";

const realExecute = promisify(execFile);
const bundle = await build({
  stdin:{contents:'export * from "./src/core/workflowTerminal";',resolveDir:process.cwd(),loader:"ts"},
  bundle:true,write:false,format:"esm",platform:"node",
  plugins:[{name:"terminal-host",setup(builder){
    for (const name of ["vscode","node:child_process"]) builder.onResolve({filter:new RegExp(`^${name}$`)},()=>({path:name,namespace:"fixture"}));
    builder.onLoad({filter:/.*/,namespace:"fixture"},({path:name})=>({contents:name === "vscode" ? `
      const f = () => globalThis.terminalFixture;
      export const window = {
        get terminals() { return f().terminals; },
        createTerminal: options => { const terminal = { ...options, show() { f().shown.push(this.name); } }; f().terminals.push(terminal); return terminal; },
        onDidCloseTerminal: listener => { f().listeners.add(listener); return { dispose() { f().listeners.delete(listener); } }; },
      };
    ` : `
      import { promisify } from 'node:util';
      export const execFile = Object.assign(() => {}, { [promisify.custom]: (bin,args,options) => globalThis.terminalFixture.execute(bin,args,options) });
    `}));
  }}],
});
const { launchWorkflowTerminal, inspectWorkflowTerminal, showWorkflowTerminal, releaseWorkflowTerminal, workflowSocket, quoteShell } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const runId = "prove-vscode-terminal-test";
const env = {PATH:process.env.PATH,LEANFLOW_WORKFLOW_RUN_ID:runId,LEMEX_API_KEY:"opaque-test-key",LEANFLOW_TEST:"value"};
function fixture(execute = async () => ({stdout:""})) {
  return globalThis.terminalFixture = {calls:[],terminals:[],shown:[],listeners:new Set(),execute:async (...args)=>{
    globalThis.terminalFixture.calls.push(args);
    return execute(...args);
  }};
}

test("tmux gets credentials only through a fresh private server environment, with literal shell arguments", async () => {
  let config;
  const f = fixture(async (_bin,args) => {
    if (args.includes("new-session")) config = await readFile(args[args.indexOf("-f")+1],"utf8");
    return {stdout:""};
  });
  const launch = await launchWorkflowTerminal("/some dir/leanflow", "/project dir", ["prove","--prompt","a' $(touch NO)\n$KEY"], env);
  assert.equal(launch.connection.kind,"tmux");
  assert.equal(f.terminals.length,0);
  const [,args,options] = f.calls.find(row=>row[1].includes("new-session"));
  assert.equal(options.env.LEMEX_API_KEY,env.LEMEX_API_KEY);
  assert.ok(!JSON.stringify(args).includes(env.LEMEX_API_KEY));
  assert.ok(!config.includes(env.LEMEX_API_KEY));
  assert.match(config,/remain-on-exit on/);
  assert.match(config,/default-shell \/bin\/sh/);
  assert.equal(args.at(-1), "exec '/some dir/leanflow' 'workflow' 'prove' '--prompt' 'a'\\'' $(touch NO)\n$KEY'");
  assert.equal(args[1],workflowSocket(runId));
  assert.notEqual(workflowSocket(runId),workflowSocket(runId+"other"));
});

test("missing tmux uses an ordinary visible terminal and preserves exact args and environment", async () => {
  const f = fixture(async()=>{throw new Error("ENOENT");});
  const launch = await launchWorkflowTerminal("leanflow","/project",["prove","x y.lean"],env);
  assert.equal(launch.connection.kind,"terminal");
  const terminal = f.terminals[0];
  assert.equal(terminal.shellPath,"leanflow");
  assert.deepEqual(terminal.shellArgs,["workflow","prove","x y.lean"]);
  assert.equal(terminal.env.LEMEX_API_KEY,env.LEMEX_API_KEY);
  assert.equal(terminal.strictEnv,true);
  assert.equal(f.shown.length,1);
  terminal.exitStatus = {code:17};
  for (const listener of f.listeners) listener(terminal);
  assert.equal(await launch.onExit,17);
  assert.equal(f.listeners.size,0);
});

test("a failed session creation falls back only after confirming no session exists", async () => {
  const f = fixture(async (_bin,args)=>{
    if(args.includes("new-session")) throw new Error("failed");
    if(args.includes("display-message")) throw Object.assign(new Error("missing"),{stderr:"no server running"});
    return {stdout:""};
  });
  assert.equal((await launchWorkflowTerminal("leanflow","/tmp",["prove"],env)).connection.kind,"terminal");
  assert.equal(f.terminals.length,1);
});

test("ambiguous tmux dispatch never launches a duplicate job", async () => {
  for(const output of ["0||42",null]) {
    const f = fixture(async (_bin,args)=>{
      if(args.includes("new-session")) throw new Error("timeout");
      if(args.includes("display-message")) {
        if(output === null) throw new Error("temporary transport failure");
        return {stdout:output};
      }
      return {stdout:""};
    });
    const promise = launchWorkflowTerminal("leanflow","/tmp",["prove"],env);
    if(output === null) assert.equal((await promise).pending,true);
    else assert.equal((await promise).connection.kind,"tmux");
    assert.equal(f.terminals.length,0);
  }
});

test("reattach only reveals a session, and cleanup never kills a live pane", async () => {
  const f = fixture(async()=>({stdout:"0||42"}));
  await showWorkflowTerminal(runId,{kind:"tmux"});
  assert.ok(f.terminals[0].shellArgs.includes("attach-session"));
  await showWorkflowTerminal(runId,{kind:"tmux"});
  assert.equal(f.terminals.length,1);
  await releaseWorkflowTerminal(runId);
  assert.ok(!f.calls.some(row=>row[1].includes("kill-session")));
  assert.ok(!f.calls.some(row=>row[1].includes("new-session")));
});

test("exited tmux panes retain early startup errors and exit status until explicitly released", async () => {
  const f = fixture(async(_bin,args)=>({stdout:args.includes("display-message")?"1|127|42":"leanflow: not found"}));
  assert.deepEqual(await inspectWorkflowTerminal(runId),{state:"exited",code:127,output:"leanflow: not found"});
  await releaseWorkflowTerminal(runId);
  assert.equal(f.calls.filter(row=>row[1].includes("kill-session")).length,1);
});

test("real tmux keeps a job alive across attachment lifetime and retains completion", {skip:!process.env.LEANFLOW_TEST_TMUX_DIR}, async()=>{
  const previousPath = process.env.PATH;
  process.env.PATH = `${process.env.LEANFLOW_TEST_TMUX_DIR}:${previousPath}`;
  const directory = await mkdtemp(path.join(tmpdir(),"leanflow-real-tmux-"));
  const id = `test-${Date.now()}`;
  fixture(realExecute);
  try {
    const bin = path.join(directory,"fake leanflow");
    const result = path.join(directory,"result.json");
    await writeFile(bin,`#!${process.execPath}\nrequire('node:fs').writeFileSync(${JSON.stringify(result)}, JSON.stringify({args:process.argv.slice(2),key:process.env.LEMEX_API_KEY})); setTimeout(()=>process.exit(7),1000);\n`);
    await chmod(bin,0o700);
    const args = ["prove","a' $HOME $(touch nope)\nline"];
    const launch = await launchWorkflowTerminal(bin,directory,args,{...process.env,LEANFLOW_WORKFLOW_RUN_ID:id,LEMEX_API_KEY:"isolated-test-key"});
    assert.equal(launch.connection.kind,"tmux");
    const client = spawn("tmux",["-L",workflowSocket(id),"-C","attach-session","-t","job"],{stdio:["pipe","pipe","pipe"]});
    client.stdout.once("data",()=>client.stdin.write("detach-client\n"));
    const [clientCode] = await once(client,"exit");
    assert.equal(clientCode,0);
    assert.equal((await inspectWorkflowTerminal(id)).state,"running");
    let pane;
    for(let i=0;i<40;i++) {
      pane=await inspectWorkflowTerminal(id);
      if(pane.state === "exited") break;
      await new Promise(resolve=>setTimeout(resolve,50));
    }
    assert.equal(pane.state,"exited");
    assert.equal(pane.code,7);
    const data=JSON.parse(await readFile(result,"utf8"));
    assert.deepEqual(data.args,["workflow",...args]);
    assert.equal(data.key,"isolated-test-key");
    await releaseWorkflowTerminal(id);
    assert.equal((await inspectWorkflowTerminal(id)).state,"missing");
  } finally {
    await realExecute("tmux",["-L",workflowSocket(id),"kill-server"]).catch(()=>{});
    await rm(directory,{recursive:true,force:true});
    process.env.PATH=previousPath;
  }
});
