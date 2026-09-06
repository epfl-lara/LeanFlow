/**
 * An idle dashboard must notice a run started from a terminal (backlog D08)
 * without spawning the CLI on every timer tick: only a changed live-status
 * file, or a watcher notification, triggers a read.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { idleCheckDue, idleDiscoveryDecision } from "../dist/test/idleDiscovery.mjs";

const NOW = 5_000_000;

test("the first observation records a baseline without polling", () => {
  const decision = idleDiscoveryDecision({ lastSignature: undefined, lastCheckedAt: 0 }, { mtimeMs: 1, size: 10 }, NOW);
  assert.equal(decision.poll, false);
  assert.deepEqual(decision.next, { lastSignature: { mtimeMs: 1, size: 10 }, lastCheckedAt: NOW });
});

test("a changed, appearing, or disappearing live-status file triggers one poll", () => {
  const baseline = { lastSignature: { mtimeMs: 1, size: 10 }, lastCheckedAt: 0 };
  assert.equal(idleDiscoveryDecision(baseline, { mtimeMs: 2, size: 10 }, NOW).poll, true);
  assert.equal(idleDiscoveryDecision(baseline, { mtimeMs: 1, size: 11 }, NOW).poll, true);
  assert.equal(idleDiscoveryDecision(baseline, null, NOW).poll, true);
  assert.equal(idleDiscoveryDecision({ lastSignature: null, lastCheckedAt: 0 }, { mtimeMs: 1, size: 10 }, NOW).poll, true);
  assert.equal(idleDiscoveryDecision(baseline, { mtimeMs: 1, size: 10 }, NOW).poll, false);
});

test("idle checks are bounded by their interval", () => {
  assert.equal(idleCheckDue({ lastSignature: null, lastCheckedAt: NOW - 20_000 }, NOW, 20_000), true);
  assert.equal(idleCheckDue({ lastSignature: null, lastCheckedAt: NOW - 19_000 }, NOW, 20_000), false);
  assert.equal(idleCheckDue({ lastSignature: undefined, lastCheckedAt: 0 }, NOW, 20_000), true);
});
