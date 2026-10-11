/** Exercise secure setup prompts and metadata on a simulated VS Code host. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";

const bundle = await build({
  stdin: { contents: 'export { ProviderSetup } from "./src/core/providerSetup";', resolveDir: process.cwd(), loader: "ts" },
  bundle: true, write: false, format: "esm", platform: "node",
  plugins: [{ name: "provider-test-host", setup(builder) {
    builder.onResolve({ filter: /^vscode$/ }, () => ({ path: "vscode", namespace: "fixture" }));
    builder.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({ contents: `
      export class EventEmitter { event = () => ({ dispose() {} }); fire() {} dispose() {} }
      export const workspace = { isTrusted: true, workspaceFolders: [{uri: {authority: "ssh-remote+test"}}] };
      export const Uri = { file: value => value };
      export const window = {
        showInputBox: async options => { globalThis.providerFixture.prompts.push(options); return globalThis.providerFixture.input; },
        showInformationMessage() {},
        createTerminal: options => { globalThis.providerFixture.terminals.push(options); return {show() {}}; },
      };
    ` }));
    builder.onLoad({ filter: /src\/core\/cli\.ts$/ }, () => ({ contents: `export async function runCli(args, options) { globalThis.providerFixture.calls.push({ args, options }); return JSON.stringify(globalThis.providerFixture.metadata); }` }));
  } }],
});
const { ProviderSetup } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);

function fixture() {
  const values = new Map();
  const f = globalThis.providerFixture = { input: undefined, prompts: [], calls: [], terminals: [], metadata: { selected: "codex", backends: ["codex", "claude", "lemex"].map(id => ({ id, installed: true, executable: `/bin/${id}`, auth: "native_login", version: "1.2.3", key_env: "NATIVE_API_KEY", config_path: `/home/.${id}/config.toml`, endpoint: "", checked: true, accidental_key: "never-copy-extra-fields" })) } };
  const secrets = { get: async key => values.get(key), store: async (key, value) => values.set(key, value), delete: async key => values.delete(key), onDidChange: () => ({ dispose() {} }) };
  const setup = new ProviderSetup({ secrets });
  return { ...f, f, setup, values, secrets };
}

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
