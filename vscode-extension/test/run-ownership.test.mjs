/** Live-owner admission tests for manual and restored workflows. */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  durableOwnerDescription,
  researchConflictDescription,
  resolveRestoredRun,
  trackedOwnerDescription,
} from "../dist/test/runOwnership.mjs";
import {
  isActiveLiveStatus,
  trackedRunForLiveStatus,
} from "../dist/test/runSelection.mjs";

test("only a fresh status with a positive process id remains live", () => {
  assert.equal(isActiveLiveStatus({ phase: "busy", process_id: 42 }), true);
  assert.equal(isActiveLiveStatus({ phase: "busy", process_id: 0 }), false);
  assert.equal(
    isActiveLiveStatus({ phase: "busy", process_id: 42, stale_snapshot: true }),
    false,
  );
  assert.equal(isActiveLiveStatus(null), false);
});

test("stopped history never hides an active project live status", () => {
  const stopped = { id: "old", status: "stopped" };
  const running = { id: "live", status: "running" };
  assert.equal(trackedRunForLiveStatus([stopped], null), null);
  assert.equal(trackedRunForLiveStatus([stopped, running], null), running);
  assert.equal(trackedRunForLiveStatus([stopped, running], "old"), stopped);
});

test("a verified live-status process blocks another launch", () => {
  assert.match(
    durableOwnerDescription({
      phase: "running",
      process_id: 314,
      run_id: "prove-vscode-one",
    }),
    /prove-vscode-one.*314/,
  );
});

test("a stale or ownerless snapshot does not claim ownership", () => {
  assert.equal(
    durableOwnerDescription({ phase: "dead", process_id: 0, stale_snapshot: true }),
    null,
  );
  assert.equal(durableOwnerDescription({ phase: "running", process_id: 0 }), null);
});

test("a terminal-looking snapshot still blocks while its verified pid is present", () => {
  // The runtime owns the lease until process identity normalization clears the
  // pid; phase text alone is not safe evidence that cleanup has finished.
  assert.match(
    durableOwnerDescription({ phase: "completed", process_id: 315 }),
    /completed.*315/,
  );
});

test("a restored tracked run blocks only its own project root", () => {
  const run = {
    id: "tracked-one",
    runId: "prove-vscode-one",
    label: "Prove Main.lean",
    status: "running",
    projectRoot: "/projects/one",
  };
  assert.match(trackedOwnerDescription([run], "/projects/one"), /still running/);
  assert.equal(trackedOwnerDescription([run], "/projects/two"), null);
});

test("a tracked terminal run no longer blocks launch", () => {
  const run = {
    id: "tracked-one",
    runId: "prove-vscode-one",
    label: "Prove Main.lean",
    status: "finished",
    projectRoot: "/projects/one",
  };
  assert.equal(trackedOwnerDescription([run], "/projects/one"), null);
});

test("a restored sweep cell blocks a manual launch even in an isolated root", () => {
  const run = {
    label: "Sweep P1.lean",
    status: "running",
    projectRoot: "/private/cell-one",
    experimentId: "matrix-one",
  };
  assert.match(researchConflictDescription([run]), /research sweep/);
});

test("a manual run blocks a sweep to avoid resource-confounded measurements", () => {
  const run = {
    label: "Prove Main.lean",
    status: "running",
    projectRoot: "/projects/base",
    experimentId: null,
  };
  assert.match(researchConflictDescription([run], "matrix-one"), /resource contention/);
});

function restoredRun(overrides = {}) {
  return {
    id: "tracked-one",
    runId: "prove-vscode-one",
    label: "Prove Main.lean",
    status: "starting",
    pid: null,
    startedAt: "2026-08-11T00:00:00.000Z",
    projectRoot: "/projects/one",
    ...overrides,
  };
}

test("a pid-less restored launch adopts only exact live-run evidence", () => {
  assert.deepEqual(
    resolveRestoredRun(
      restoredRun(),
      { run_id: "prove-vscode-one", phase: "running", process_id: 712 },
      [],
      true,
    ),
    { kind: "adopt", processId: 712 },
  );
  assert.deepEqual(
    resolveRestoredRun(
      restoredRun(),
      { run_id: "some-other-run", phase: "running", process_id: 712 },
      [],
      true,
      Date.parse("2026-08-11T00:00:01.000Z"),
    ),
    { kind: "waiting" },
  );
});

test("historical process metadata is not treated as a live owner lease", () => {
  assert.deepEqual(
    resolveRestoredRun(
      restoredRun(),
      null,
      [
        {
          run_id: "prove-vscode-one",
          terminal: false,
          final_snapshot_recorded: false,
          process_id: 712,
        },
      ],
      true,
      Date.parse("2026-08-11T01:00:00.000Z"),
    ),
    { kind: "waiting" },
  );
});

test("an exact immutable result terminates a restored run despite unrelated live status", () => {
  const exact = {
    run_id: "prove-vscode-one",
    terminal: true,
    final_snapshot_recorded: true,
    process_id: 0,
    exit_code: 0,
    terminal_phase: "exited",
    terminal_status: "succeeded",
  };
  assert.deepEqual(
    resolveRestoredRun(
      restoredRun({ pid: 711, status: "running" }),
      { run_id: "some-other-run", phase: "running", process_id: 900 },
      [exact],
      true,
    ),
    { kind: "terminal", status: "finished" },
  );
});

test("exact terminal exit evidence preserves failed and interrupted outcomes", () => {
  const summary = (exitCode, terminalStatus) => ({
    run_id: "prove-vscode-one",
    terminal: true,
    final_snapshot_recorded: true,
    process_id: 0,
    exit_code: exitCode,
    terminal_phase: "exited",
    terminal_status: terminalStatus,
  });
  assert.deepEqual(resolveRestoredRun(restoredRun(), null, [summary(1, "failed")], true), {
    kind: "terminal",
    status: "failed",
  });
  assert.deepEqual(
    resolveRestoredRun(restoredRun(), null, [summary(130, "interrupted")], true),
    {
    kind: "terminal",
    status: "stopped",
    },
  );
  assert.deepEqual(resolveRestoredRun(restoredRun(), null, [summary(3, "disproved")], true), {
    kind: "terminal",
    status: "finished",
  });
});

test("a pid-less pre-spawn record is abandoned only after readable grace", () => {
  const run = restoredRun();
  assert.deepEqual(
    resolveRestoredRun(run, null, [], true, Date.parse("2026-08-11T00:00:14.000Z")),
    { kind: "waiting" },
  );
  assert.equal(
    resolveRestoredRun(run, null, [], true, Date.parse("2026-08-11T00:00:16.000Z"))
      .kind,
    "abandoned",
  );
  assert.deepEqual(
    resolveRestoredRun(run, null, [], false, Date.parse("2026-08-11T01:00:00.000Z")),
    { kind: "waiting" },
  );
});
