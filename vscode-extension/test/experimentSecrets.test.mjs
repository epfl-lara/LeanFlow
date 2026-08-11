import assert from "node:assert/strict";
import { test } from "node:test";

import {
  deleteExperimentPrompt,
  experimentPromptEnvelope,
  experimentPromptSecretKey,
  readExperimentPrompt,
  scrubExperimentMatrixPrompt,
  storeExperimentPrompt,
} from "../dist/test/experimentSecrets.mjs";

function memorySecrets(values = new Map()) {
  return {
    values,
    async get(key) {
      return values.get(key);
    },
    async store(key, value) {
      values.set(key, value);
    },
    async delete(key) {
      values.delete(key);
    },
  };
}

test("experiment metadata contains only a prompt marker and SHA-256", async () => {
  const secret = "pasted-token-sk-never-persist";
  const store = memorySecrets();
  const protectedPrompt = scrubExperimentMatrixPrompt({
    id: "matrix-a",
    name: "prompt privacy",
    createdAt: "2026-08-11T00:00:00Z",
    kind: "prove",
    base: {
      kind: "prove",
      target: "Main.lean",
      provider: "openai",
      model: "gpt-5.6",
      profile: "default",
      prompt: `  ${secret}  `,
      additionalSkills: [],
      overrides: {},
    },
    targets: ["Main.lean"],
    profiles: ["default"],
    models: ["gpt-5.6"],
    repeats: 1,
    randomizationSeed: "seed",
  });
  const envelope = await storeExperimentPrompt(
    store,
    protectedPrompt.matrix.id,
    protectedPrompt.prompt,
  );
  const persisted = JSON.stringify({
    matrix: protectedPrompt.matrix,
    ...envelope,
  });
  assert.equal(envelope.promptPresent, true);
  assert.match(envelope.promptSha256, /^[0-9a-f]{64}$/);
  assert.equal(protectedPrompt.matrix.base.prompt, "");
  assert.equal(persisted.includes(secret), false);
  assert.equal(await readExperimentPrompt(store, "matrix-a", envelope), secret);
});

test("a reloaded experiment fails closed when its encrypted prompt is missing or changed", async () => {
  const shared = new Map();
  const firstHost = memorySecrets(shared);
  const envelope = await storeExperimentPrompt(firstHost, "matrix-reload", "frozen prompt");

  const reloadedHost = memorySecrets(shared);
  assert.equal(
    await readExperimentPrompt(reloadedHost, "matrix-reload", envelope),
    "frozen prompt",
  );
  shared.delete(experimentPromptSecretKey("matrix-reload"));
  await assert.rejects(
    readExperimentPrompt(reloadedHost, "matrix-reload", envelope),
    /encrypted experiment prompt is missing/,
  );
  shared.set(experimentPromptSecretKey("matrix-reload"), "changed prompt");
  await assert.rejects(
    readExperimentPrompt(reloadedHost, "matrix-reload", envelope),
    /does not match the frozen content digest/,
  );
});

test("deleting an experiment removes its encrypted prompt", async () => {
  const store = memorySecrets();
  await storeExperimentPrompt(store, "matrix-delete", "private prompt");
  await deleteExperimentPrompt(store, "matrix-delete");
  assert.equal(store.values.has(experimentPromptSecretKey("matrix-delete")), false);
  assert.deepEqual(experimentPromptEnvelope("   "), {
    promptPresent: false,
    promptSha256: null,
  });
});
