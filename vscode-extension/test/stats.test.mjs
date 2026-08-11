import assert from "node:assert/strict";
import { test } from "node:test";

import {
  aggregateCells,
  contrastCells,
  formatContrast,
  tCritical95,
} from "../dist/test/stats.mjs";

function cell(id, status, metrics, profile = "a", repeat = 1) {
  return {
    id,
    matrixId: "matrix",
    target: "Main.lean",
    profile,
    model: "model",
    repeat,
    plannedOrder: repeat,
    executionOrder: repeat,
    status,
    runId: id,
    trackedRunId: id,
    startedAt: null,
    finishedAt: null,
    projectRoot: "/isolated",
    metrics,
    error: null,
  };
}

function measured(durationSeconds, solved, extra = {}) {
  return {
    scopeExact: true,
    solved,
    sorryCount: solved ? 0 : 1,
    projectSorryCount: solved ? 0 : 1,
    durationSeconds,
    apiCalls: 1,
    inputTokens: 10,
    outputTokens: 20,
    costUsd: durationSeconds / 100,
    phase: "done",
    toolCalls: durationSeconds,
    verificationFailures: 0,
    countsAreComplete: true,
    usageComplete: true,
    apiCallsComplete: true,
    tokensComplete: true,
    costComplete: true,
    usageSource: "exact",
    declarationsTotal: 1,
    declarationsProved: solved ? 1 : 0,
    failures: {},
    missingReason: "",
    provenance: null,
    provenanceFinal: null,
    sourceChangedDuringRun: false,
    ...extra,
  };
}

const UNSCORED = {
  scopeExact: false,
  solved: null,
  sorryCount: null,
  projectSorryCount: null,
  durationSeconds: null,
  apiCalls: null,
  inputTokens: null,
  outputTokens: null,
  costUsd: null,
  phase: "unscored",
  toolCalls: null,
  verificationFailures: null,
  countsAreComplete: false,
  usageComplete: false,
  apiCallsComplete: false,
  tokensComplete: false,
  costComplete: false,
  usageSource: "unavailable",
  declarationsTotal: null,
  declarationsProved: null,
  failures: {},
  missingReason: "missing",
  provenance: null,
  provenanceFinal: null,
  sourceChangedDuringRun: null,
};

test("unscored cells are missing, never zero-valued failures", () => {
  const rows = aggregateCells(
    [
      cell("one", "done", measured(10, true), "a", 1),
      cell("two", "unscored", UNSCORED, "a", 2),
      cell("three", "failed", measured(20, false), "a", 3),
      cell("stopped", "skipped", measured(99, true), "a", 4),
      cell("pending", "pending", null, "a", 4),
    ],
    "durationSeconds",
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].attemptedN, 4);
  assert.equal(rows[0].measuredN, 2);
  assert.equal(rows[0].missingN, 2);
  assert.equal(rows[0].mean, 15);
  assert.equal(rows[0].solveMeasuredN, 2);
  assert.equal(rows[0].solveMissingN, 2);
  assert.equal(rows[0].solved, 1);
  assert.equal(rows[0].solveRate, 0.5);
});

test("incomplete event counts are missing from tool-call means", () => {
  const row = aggregateCells(
    [
      cell("complete", "done", measured(10, true), "a", 1),
      cell(
        "partial",
        "done",
        measured(100, true, { countsAreComplete: false, toolCalls: 100 }),
        "a",
        2,
      ),
    ],
    "toolCalls",
  )[0];
  assert.equal(row.measuredN, 1);
  assert.equal(row.missingN, 1);
  assert.equal(row.mean, 10);
});

test("small-sample mean intervals use Student t, not z=1.96", () => {
  const row = aggregateCells(
    [cell("one", "done", measured(1, true), "a", 1), cell("two", "done", measured(3, true), "a", 2)],
    "durationSeconds",
  )[0];
  assert.ok(Math.abs(tCritical95(1) - 12.7062) < 1e-4);
  assert.ok(Math.abs(row.ci95 - 12.7062) < 1e-4);
  assert.ok(row.ci95 > 6 * 1.96);
});

test("condition output includes a direct Welch effect interval", () => {
  const cells = [
    ...[1, 2, 3].map((value, index) =>
      cell(`a-${value}`, "done", measured(value, true), "a", index + 1),
    ),
    ...[3, 4, 5].map((value, index) =>
      cell(`b-${value}`, "done", measured(value, true), "b", index + 1),
    ),
  ];
  const contrast = contrastCells(cells, "durationSeconds")[0];
  assert.equal(contrast.leftProfile, "a");
  assert.equal(contrast.rightProfile, "b");
  assert.equal(contrast.effect, 2);
  assert.ok(Math.abs(contrast.degreesOfFreedom - 4) < 1e-9);
  assert.ok(Math.abs(contrast.ci95 - 2.267) < 0.001);
  assert.match(formatContrast(contrast), /^2\.0 ± 2\.3$/);
});

test("a contrast with one observation is descriptive, not inferential", () => {
  const contrast = contrastCells(
    [cell("a", "done", measured(1, true), "a"), cell("b", "done", measured(2, true), "b")],
    "durationSeconds",
  )[0];
  assert.equal(contrast.effect, 1);
  assert.equal(contrast.ci95, null);
  assert.match(formatContrast(contrast), /needs n≥2/);
});
