/**
 * The prover snapshot is re-read through the CLI only when its state file has
 * changed. A 3-second refresh must not spawn a Python process every tick.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { proverStatePath, sameSignature, shouldRefetchProver } from "../dist/test/proverCache.mjs";

const NOW = 1_000_000;
const SIGNATURE = { mtimeMs: 500, size: 1234 };

function entry(overrides = {}) {
  return { snapshot: { run_id: "r" }, error: "", signature: SIGNATURE, fetchedAt: NOW - 1000, ...overrides };
}

test("an unchanged state file serves the cached snapshot", () => {
  assert.equal(shouldRefetchProver(entry(), SIGNATURE, NOW), false);
  assert.equal(sameSignature(SIGNATURE, { ...SIGNATURE }), true);
});

test("a changed mtime or size or a missing cache entry forces a fetch", () => {
  assert.equal(shouldRefetchProver(undefined, SIGNATURE, NOW), true);
  assert.equal(shouldRefetchProver(entry(), { mtimeMs: 501, size: 1234 }, NOW), true);
  assert.equal(shouldRefetchProver(entry(), { mtimeMs: 500, size: 1235 }, NOW), true);
});

test("a failed read is retried after a short backoff or as soon as the file changes", () => {
  const failed = entry({ error: "boom", snapshot: null, fetchedAt: NOW - 1000 });
  assert.equal(shouldRefetchProver(failed, SIGNATURE, NOW), false, "same file, one second later: wait");
  assert.equal(shouldRefetchProver({ ...failed, fetchedAt: NOW - 6000 }, SIGNATURE, NOW), true);
  assert.equal(shouldRefetchProver(failed, { mtimeMs: 501, size: 1234 }, NOW), true, "the file changed");
  // A stale-but-present snapshot with an error follows the same backoff.
  assert.equal(shouldRefetchProver(entry({ error: "boom", fetchedAt: NOW - 1000 }), SIGNATURE, NOW), false);
});

test("without a readable state file the cache falls back to a bounded age", () => {
  assert.equal(shouldRefetchProver(entry({ signature: null, fetchedAt: NOW - 1000 }), null, NOW), false);
  assert.equal(shouldRefetchProver(entry({ signature: null, fetchedAt: NOW - 6000 }), null, NOW), true);
  // A file that disappears after being seen is a change.
  assert.equal(shouldRefetchProver(entry(), null, NOW), true);
});

test("a run without prover state is re-asked only when its state file appears or after a long age", () => {
  const absent = entry({ snapshot: null, error: "", signature: null, fetchedAt: NOW - 1000 });
  assert.equal(shouldRefetchProver(absent, null, NOW), false);
  assert.equal(shouldRefetchProver(absent, SIGNATURE, NOW), true);
  assert.equal(shouldRefetchProver({ ...absent, fetchedAt: NOW - 31_000 }, null, NOW), true);
});

test("even an unchanged file is re-read after the maximum cache age", () => {
  assert.equal(shouldRefetchProver(entry({ fetchedAt: NOW - 61_000 }), SIGNATURE, NOW), true);
  assert.equal(shouldRefetchProver(entry({ fetchedAt: NOW - 59_000 }), SIGNATURE, NOW), false);
});

test("the state path is derived from the run directory the CLI documents", () => {
  const statePath = proverStatePath("/proj", "prove-vscode-run-1");
  assert.ok(statePath.endsWith("/.leanflow/workflow-state/prover/prove-vscode-run-1/state.json"));
  assert.equal(proverStatePath("/proj", "../escape"), null);
  assert.equal(proverStatePath("", "prove-vscode-run-1"), null);
});
