/** Exercise editor-directed project discovery against disposable manifests. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, mkdir, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { build } from "esbuild";

const bundle = await build({
  stdin: { contents: 'export { discoverProject, discoverProjectIncludingNested } from "./src/core/project";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{name:"discovery-test-host",setup(builder){
    builder.onResolve({filter:/^vscode$/},()=>({path:"vscode",namespace:"fixture"}));
    builder.onLoad({filter:/.*/,namespace:"fixture"},()=>({contents:`
      const f=()=>globalThis.discoveryFixture;
      export const env={get remoteName(){return f().remoteName;}};
      export class RelativePattern{constructor(base,pattern){this.base=base;this.pattern=pattern;}}
      export const workspace={
        getConfiguration:()=>({get:(_,fallback)=>fallback}),
        get workspaceFolders(){return f().folders;},
        getWorkspaceFolder:uri=>f().folders.find(folder=>uri.fsPath.startsWith(folder.uri.fsPath+'/')),
        findFiles:async pattern=>{f().pattern=pattern;return f().manifests.filter(uri=>typeof pattern==='string'||uri.fsPath.startsWith(pattern.base.uri.fsPath+'/'));},
      };
      export const window={get activeTextEditor(){return f().active?{document:{uri:f().active}}:undefined;}};
    `}));
  }}],
});
const {discoverProject,discoverProjectIncludingNested}=await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

async function fixture(){
  const root=await realpath(await mkdtemp(path.join(tmpdir(),"leanflow-discovery-")));
  const uri=fsPath=>({fsPath,scheme:"file",authority:""});
  const f=globalThis.discoveryFixture={root,uri,folders:[],manifests:[],active:null};
  f.project=async name=>{const dir=path.join(root,name);await mkdir(path.join(dir,".leanflow"),{recursive:true});const manifest=path.join(dir,".leanflow/project.yaml");await writeFile(manifest,"name: fixture\n");f.folders.push({uri:uri(dir),name});f.manifests.push(uri(manifest));return dir;};
  f.dispose=()=>rm(root,{recursive:true,force:true});return f;
}

test("multi-root discovery respects the editor and retains explicit setup selection only when ambiguous",async()=>{
  const f=await fixture();try{
    const first=await f.project("first"),second=await f.project("second");
    assert.equal(discoverProject().found,false);
    assert.equal((await discoverProjectIncludingNested()).found,false);
    assert.equal((await discoverProjectIncludingNested(second)).root,second);
    f.active=f.uri(path.join(first,"Main.lean"));
    assert.equal((await discoverProjectIncludingNested(second)).root,first);
    const empty=path.join(f.root,"empty");await mkdir(empty);f.folders.push({uri:f.uri(empty),name:"empty"});f.active=f.uri(path.join(empty,"Problem.lean"));
    assert.equal((await discoverProjectIncludingNested(second)).found,false);
    assert.equal(f.pattern.base.uri.fsPath,empty);
  }finally{await f.dispose();}
});

test("remote manifest discovery uses the extension host's filesystem and authority",async()=>{
  const f=await fixture();try{
    const project=await f.project("remote");f.remoteName="ssh-remote";
    for(const folder of f.folders) Object.assign(folder.uri,{scheme:"vscode-remote",authority:"ssh-remote+host"});
    f.active={...f.uri(path.join(project,"Main.lean")),scheme:"vscode-remote",authority:"ssh-remote+host"};
    assert.equal(discoverProject().root,project);
  }finally{await f.dispose();}
});
