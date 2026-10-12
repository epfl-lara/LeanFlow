/** Native keys stay in the vault and cross only the launch environment boundary. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { ProviderCredentials, validateEndpoint } from "../dist/test/providerCredentials.mjs";
import { parseWebviewMessage } from "../dist/test/messageSchema.mjs";

function fixture() {
  const values = new Map();
  const vault = { get: async key => values.get(key), store: async (key, value) => { values.set(key, value); }, delete: async key => { values.delete(key); } };
  return { values, local: new ProviderCredentials(vault, "local"), remote: new ProviderCredentials(vault, "ssh-remote+server") };
}

test("saved keys use standard native variables and remain scoped to the host", async () => {
  const { local, remote } = fixture();
  await local.saveKey("codex", " openai-key ");
  await local.saveKey("claude", "anthropic-key");
  await local.saveKey("lemex", "opaque-rcp-key");
  await local.saveEndpoint("https://inference.rcp.epfl.ch/v1");
  assert.deepEqual(await local.environment(), { OPENAI_API_KEY: "openai-key", ANTHROPIC_API_KEY: "anthropic-key", LEMEX_API_KEY: "opaque-rcp-key", LEMEX_BASE_URL: "https://inference.rcp.epfl.ch/v1" });
  assert.deepEqual(await remote.environment(), {});
  assert.deepEqual(await local.environment("codex"), { OPENAI_API_KEY: "openai-key" });
  assert.deepEqual(await local.presence("lemex"), { savedKey: true, savedEndpoint: true });
});

test("replacement is fresh, removal and clearing endpoint restore inheritance", async () => {
  const { local } = fixture();
  await local.saveKey("lemex", "first");
  await local.saveKey("lemex", "second");
  assert.equal((await local.environment()).LEMEX_API_KEY, "second");
  await local.saveEndpoint("http://127.0.0.1:8000/v1");
  await local.removeKey("lemex");
  await local.saveEndpoint("");
  assert.deepEqual(await local.environment(), {});
  assert.deepEqual(await local.presence("lemex"), { savedKey: false, savedEndpoint: false });
});

test("endpoint validation refuses embedded credentials and non-HTTP schemes", async () => {
  const { local } = fixture();
  for (const value of ["https://user:secret@host/v1", "https://host/v1?token=abc", "https://host/v1#secret", "file:///tmp/secret", "bad-url", "https://host/a b"]) {
    assert.ok(validateEndpoint(value));
    await assert.rejects(local.saveEndpoint(value));
  }
  for (const value of ["", "https://inference.rcp.epfl.ch/v1", "http://localhost:8000/v1"]) assert.equal(validateEndpoint(value), undefined);
  await assert.rejects(local.saveKey("codex", ""));
  await assert.rejects(local.saveKey("codex", "x\ny"));
});

test("webview messages carry only validated setup actions, never secret values", () => {
  assert.deepEqual(parseWebviewMessage({ type: "providerAction", backend: "lemex", action: "apiKey", apiKey: "do-not-forward" }), { ok: true, message: { type: "providerAction", backend: "lemex", action: "apiKey" } });
  for (const [backend, action] of [["unknown", "apiKey"], ["lemex", "exec"], ["codex", "endpoint"], ["lemex", "login"]]) assert.equal(parseWebviewMessage({ type: "providerAction", backend, action }).ok, false);
});

test("vault read failures cannot leak an opaque key into launch diagnostics", async () => {
  const credentials = new ProviderCredentials({ get: async () => { throw new Error('failure: opaque-key-content'); }, store: async () => {}, delete: async () => {} }, "local");
  await assert.rejects(credentials.environment(), error => !error.message.includes("opaque-key-content") && error.message.includes("secret storage"));
});
