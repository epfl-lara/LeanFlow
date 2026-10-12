import assert from "node:assert/strict";
import { test } from "node:test";
import { normalizeStorage } from "../dist/test/storageState.mjs";
import { parseWebviewMessage } from "../dist/test/messageSchema.mjs";

const report = {
  managed_bytes: 123, pinned_bytes: 12, workspace_bytes: 20, free_bytes: 1024 ** 3,
  cache_target_bytes: 20 * 1024 ** 3, min_free_bytes: 10 * 1024 ** 3, automatic_cleanup: true,
};

test("storage keeps explicit protected totals and policy defaults", () => {
  const state = normalizeStorage(report);
  assert.equal(state.pinnedBytes, 12);
  assert.equal(state.workspaceBytes, 20);
  assert.equal(state.cacheTargetBytes, 20 * 1024 ** 3);
  assert.equal(state.minFreeBytes, 10 * 1024 ** 3);
  assert.equal(state.automaticCleanup, true);
});

test("missing or malformed storage metrics cannot masquerade as empty storage", () => {
  for (const value of [null, {}, { ...report, free_bytes: -1 }, { ...report, managed_bytes: "123" }, { ...report, automatic_cleanup: "false" }, { ...report, free_bytes: Infinity }]) {
    assert.throws(() => normalizeStorage(value));
  }
});

test("storage actions are fixed host commands with no path input", () => {
  for (const type of ["refreshStorage", "cleanStorage"]) {
    assert.deepEqual(parseWebviewMessage({ type, path: "/arbitrary/user/folder" }), { ok: true, message: { type } });
  }
});
