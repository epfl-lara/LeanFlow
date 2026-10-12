/** Exercise secure setup prompts and metadata on a simulated VS Code host. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";

const bundle = await build({
  stdin: { contents: 'export { ProviderSetup } from "./src/core/providerSetup";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "provider-test-host", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onResolve({ filter: /^node:os$/ }, () => ({ path: "node:os", namespace: "host-os" }));
    builder.onLoad({ filter: /^node:os$/, namespace: "host-os" }, () => ({ contents: 'export const hostname=()=>globalThis.providerFixture.hostname;' }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      export class EventEmitter { event = () => ({ dispose() {} }); fire() {} dispose() {} }
      export const env = { get remoteName(){return globalThis.providerFixture.remoteName;} };
      export const workspace = { isTrusted: true, get workspaceFolders(){return [{uri:{authority:globalThis.providerFixture.authority,fsPath:"/fixture/project"}}];}, openTextDocument: async uri => { if (globalThis.providerFixture.missingConfig) throw new Error('Missing file'); return uri; } };
      export const Uri = { file: value => value };
      export const window = {
        showInputBox: async options => { globalThis.providerFixture.prompts.push(options); return globalThis.providerFixture.input; },
        showInformationMessage() {},
        createTerminal: options => { globalThis.providerFixture.terminals.push(options); return {show() {}}; },
        showTextDocument: async document => { globalThis.providerFixture.documents.push(document); },
      };
    ` }));
    builder.onLoad({ filter: /src\/core\/cli\.ts$/ }, () => ({ contents: `export async function runCli(args, options) { globalThis.providerFixture.calls.push({ args, options }); return JSON.stringify(globalThis.providerFixture.metadata); }` }));
  } }],
});
const { ProviderSetup } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

function fixture(host = {}) {
  const values = new Map();
  const f = globalThis.providerFixture = { authority: "ssh-remote+test", remoteName: undefined, hostname: "test-host", ...host, input: undefined, prompts: [], calls: [], terminals: [], documents: [], metadata: { selected: "codex", backends: ["codex", "claude", "lemex"].map(id => ({ id, installed: true, executable: `/bin/${id}`, auth: "native_login", version: "1.2.3", key_env: "NATIVE_API_KEY", config_path: `/home/.${id}/config.toml`, endpoint: "", checked: true, accidental_key: "never-copy-extra-fields" })) } };
  const secrets = { get: async key => values.get(key), store: async (key, value) => values.set(key, value), delete: async key => values.delete(key), onDidChange: () => ({ dispose() {} }) };
  const setup = new ProviderSetup({ secrets });
  return { ...f, f, setup, values, secrets };
}

test("host-local Remote SSH URIs keep secrets separate from local and other remote hosts", async () => {
  const one=fixture({authority:"",remoteName:"ssh-remote",hostname:"host-one"});
  one.values.set("native-provider:local:OPENAI_API_KEY","local-only-key");
  assert.equal(one.setup.state().host,"ssh-remote+host-one");
  assert.deepEqual(await one.setup.credentials.environment("codex"),{});
  await one.setup.credentials.saveKey("codex","host-one-key");
  const two=fixture({authority:"",remoteName:"ssh-remote",hostname:"host-two"});
  const sharedVault=new ProviderSetup({secrets:one.secrets});
  assert.equal(sharedVault.state().host,"ssh-remote+host-two");
  assert.deepEqual(await sharedVault.credentials.environment("codex"),{});
  one.setup.dispose();two.setup.dispose();sharedVault.dispose();
});

test("API entry is masked and setup checks use env only, with no secret in public state", async () => {
  const { f, setup, values } = fixture();
  f.input = "opaque-provider-secret";
  await setup.act("lemex", "apiKey");
  assert.equal(f.prompts[0].password, true);
  assert.equal([...values.values()][0], f.input);
  assert.equal(f.calls.at(-1).options.env.LEMEX_API_KEY, f.input);
  assert.ok(!JSON.stringify(f.calls.at(-1).args).includes(f.input));
  assert.ok(!JSON.stringify(setup.state()).includes(f.input));
  assert.ok(!JSON.stringify(setup.state()).includes("accidental_key"));
  assert.equal(setup.state().backends.find(row => row.id === "lemex").savedKey, true);
  assert.equal(setup.state().host, "ssh-remote+test");
  setup.dispose();
});

test("cancel preserves saved keys and native login uses a direct executable with no secret argv", async () => {
  const { f, setup } = fixture();
  await setup.credentials.saveKey("codex", "saved-key");
  await setup.act("codex", "apiKey");
  assert.equal((await setup.credentials.environment()).OPENAI_API_KEY, "saved-key");
  await setup.act("codex", "login");
  assert.equal(f.terminals[0].shellPath, "/bin/codex");
  assert.deepEqual(f.terminals[0].shellArgs, ["login"]);
  assert.equal(f.terminals[0].env.OPENAI_API_KEY, null);
  assert.ok(!JSON.stringify(f.terminals).includes("saved-key"));
  await setup.act("claude", "login");
  assert.deepEqual(f.terminals[1].shellArgs, ["auth", "login"]);
  setup.dispose();
});

test("secret storage failures never echo the entered value", async () => {
  const { f, setup, secrets } = fixture();
  f.input = "opaque-private-key";
  secrets.store = async () => { throw new Error(`vault rejected ${f.input}`); };
  await assert.rejects(setup.act("lemex", "apiKey"), error => !error.message.includes(f.input) && error.message.includes("secret storage"));
  setup.dispose();
});

test("replace/remove keys and set/clear endpoint restore native inheritance", async () => {
  const { f, setup } = fixture();
  try {
    for (const backend of ["codex", "claude", "lemex"]) {
      f.input = "first-key"; await setup.act(backend, "apiKey");
      f.input = "replacement-key"; await setup.act(backend, "apiKey");
      assert.ok(Object.values(await setup.credentials.environment(backend)).includes("replacement-key"));
      await setup.act(backend, "removeKey");
      assert.deepEqual(await setup.credentials.environment(backend), {});
    }
    f.input = "https://inference.example/v1"; await setup.act("lemex", "endpoint");
    assert.equal((await setup.credentials.environment()).LEMEX_BASE_URL, f.input);
    f.input = undefined; await setup.act("lemex", "endpoint");
    assert.equal((await setup.credentials.environment()).LEMEX_BASE_URL, "https://inference.example/v1");
    f.input = ""; await setup.act("lemex", "endpoint");
    assert.equal((await setup.credentials.environment()).LEMEX_BASE_URL, undefined);
  } finally { setup.dispose(); }
});

test("native config buttons open the advertised path and report missing setup", async () => {
  const { f, setup } = fixture();
  try {
    for (const backend of ["codex", "claude", "lemex"]) {
      await setup.act(backend, "config");
      assert.equal(f.documents.at(-1), `/home/.${backend}/config.toml`);
    }
    f.missingConfig = true;
    await assert.rejects(setup.act("lemex", "config"), /No native config file exists/);
  } finally { setup.dispose(); }
});

test("native catalogs preserve reported defaults and capabilities without copying arbitrary fields", async () => {
  const { f, setup } = fixture();
  try {
    await setup.credentials.saveKey("lemex", "opaque-private-model-key");
    f.metadata.backends[2].model_catalog = {
      status: "ready", source: "Lemex model/list", default_model: "model-a", default_reasoning_effort: "high",
      error: "", private_key: "do-not-copy", models: [
        { id: "model-a", label: "Model A opaque-private-model-key", reasoning_efforts: ["low", "high", "high", "invalid effort", null], reasoning_known: true, default_reasoning_effort: "high", api_key: "do-not-copy" },
        { id: "model-a", label: "Duplicate" },
        { id: "model-b", reasoning_efforts: ["high"], reasoning_known: false },
        { id: "model-c", reasoning_efforts: [], reasoning_known: true },
        { id: "bad\nmodel" }, { id: 7 }, null,
      ],
    };
    await setup.refresh();
    assert.equal(f.calls.at(-1).options.cwd, "/fixture/project");
    const catalog = setup.state().backends[2].model_catalog;
    assert.equal(catalog.default_model, "model-a");
    assert.equal(catalog.default_reasoning_effort, "high");
    assert.deepEqual(catalog.models.map(row => row.id), ["model-a", "model-b", "model-c"]);
    assert.deepEqual(catalog.models[0].reasoning_efforts, ["low", "high"]);
    assert.equal(catalog.models[0].label, "Model A [REDACTED]");
    assert.deepEqual(catalog.models[1].reasoning_efforts, []);
    assert.equal(catalog.models[1].reasoning_known, false);
    assert.equal(catalog.models[2].reasoning_known, true);
    assert.ok(!JSON.stringify(setup.state()).includes("do-not-copy"));
    assert.ok(!JSON.stringify(setup.state()).includes("opaque-private-model-key"));
    assert.equal(setup.state().backends[0].model_catalog.status, "unavailable");
    assert.match(setup.state().backends[0].model_catalog.error, /Update LeanFlow/);
  } finally { setup.dispose(); }
});

test("malformed model metadata cannot break credential setup or pretend capabilities are known", async () => {
  const { f, setup } = fixture();
  try {
    f.metadata.backends[0].model_catalog = { status: "made-up", models: {}, default_model: { secret: true }, error: null };
    f.metadata.backends[1].model_catalog = { status: "ready", models: Array.from({length: 550}, (_, i) => ({ id: `model-${i}`, label: "x".repeat(500) })) };
    await setup.refresh();
    assert.equal(setup.state().error, "");
    assert.equal(setup.state().backends[0].model_catalog.status, "unavailable");
    assert.equal(setup.state().backends[0].model_catalog.default_model, "");
    assert.deepEqual(setup.state().backends[0].model_catalog.models, []);
    assert.equal(setup.state().backends[1].model_catalog.models.length, 500);
    assert.equal(setup.state().backends[1].model_catalog.models[0].label.length, 256);
    assert.equal(setup.state().backends[1].model_catalog.models[0].reasoning_known, false);
  } finally { setup.dispose(); }
});
