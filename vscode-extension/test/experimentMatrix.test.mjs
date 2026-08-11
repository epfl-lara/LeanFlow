import assert from "node:assert/strict";
import { test } from "node:test";

import {
  assertExperimentDeletable,
  assertExperimentIdAvailable,
  canonicalExperimentTarget,
  experimentTargets,
  isComparisonEligibleCell,
  isSealedExperimentSkillReference,
  isRunnableExperimentCellStatus,
  MAX_EXPERIMENT_CELLS,
  normalizeExperimentMatrixTargets,
  validateExperimentMatrixSize,
  validateExplicitExperimentRuntime,
  validateRestoredExperimentCells,
} from "../dist/test/experimentMatrix.mjs";

function matrix(overrides = {}) {
  return {
    targets: ["a"],
    profiles: ["p"],
    models: ["m"],
    repeats: 1,
    ...overrides,
  };
}

test("matrix validation reports the exact cross-product size", () => {
  assert.equal(
    validateExperimentMatrixSize(
      matrix({ targets: ["a", "b"], profiles: ["p", "q"], repeats: 3 }),
    ),
    12,
  );
  assert.equal(validateExperimentMatrixSize(matrix({ targets: [], models: [] })), 1);
});

test("safe target aliases share one canonical project-relative identity", () => {
  for (const alias of ["A.lean", "./A.lean"]) {
    assert.equal(canonicalExperimentTarget(alias), "A.lean");
  }
  assert.equal(canonicalExperimentTarget("dir//./A.lean"), "dir/A.lean");
  assert.equal(canonicalExperimentTarget("dir\\A.lean"), "dir/A.lean");
  assert.deepEqual(
    experimentTargets({
      ...matrix({ targets: ["./A.lean", "dir//A.lean"] }),
      base: { target: "ignored.lean" },
    }),
    ["A.lean", "dir/A.lean"],
  );
  const normalized = normalizeExperimentMatrixTargets({
    ...matrix({ targets: ["./A.lean"] }),
    base: { target: "dir//B.lean" },
  });
  assert.deepEqual(normalized.targets, ["A.lean"]);
  assert.equal(normalized.base.target, "dir/B.lean");
});

test("target aliases cannot allocate duplicate experiment cells", () => {
  assert.throws(
    () =>
      validateExperimentMatrixSize(
        matrix({ targets: ["A.lean", "./A.lean", "dir//B.lean", "dir/B.lean"] }),
      ),
    /targets values must be unique/,
  );
});

test("portable target normalization rejects rooted, drive, UNC, and traversal paths", () => {
  for (const unsafe of [
    "/tmp/A.lean",
    "\\rooted\\A.lean",
    "//server/share/A.lean",
    "\\\\server\\share\\A.lean",
    "C:\\repo\\A.lean",
    "../A.lean",
    "dir/../A.lean",
    "dir\\..\\A.lean",
    "./",
    "./-option",
  ]) {
    assert.throws(
      () => canonicalExperimentTarget(unsafe),
      /safe project-relative path/,
      unsafe,
    );
  }
});

test("research cells require explicit effective provider and model identities", () => {
  assert.doesNotThrow(() =>
    validateExplicitExperimentRuntime(
      matrix({ base: { provider: "openai", model: "fallback" } }),
    ),
  );
  assert.throws(
    () => validateExplicitExperimentRuntime(matrix({ base: { provider: "", model: "m" } })),
    /explicit provider/,
  );
  assert.throws(
    () =>
      validateExplicitExperimentRuntime(
        matrix({ models: [], base: { provider: "openai", model: "" } }),
      ),
    /explicit model/,
  );
});

test("sweep skills must be project paths rather than mutable resolver names", () => {
  assert.equal(isSealedExperimentSkillReference(".leanflow/skills/local/SKILL.md"), true);
  assert.equal(isSealedExperimentSkillReference("skills/local/SKILL.md"), true);
  assert.equal(isSealedExperimentSkillReference("leanflow-prove"), false);
  assert.equal(isSealedExperimentSkillReference("~/skills/local"), false);
  assert.equal(isSealedExperimentSkillReference("../outside/SKILL.md"), false);
});

test("matrix validation rejects before a product can allocate too many cells", () => {
  assert.equal(MAX_EXPERIMENT_CELLS, 1_000);
  assert.throws(
    () =>
      validateExperimentMatrixSize(
        matrix({
          targets: Array.from({ length: 11 }, (_, index) => String(index)),
          profiles: Array.from({ length: 10 }, (_, index) => String(index)),
          repeats: 10,
        }),
      ),
    /product limit is 1000/,
  );
});

test("matrix validation rejects malformed repeats and oversized axes", () => {
  assert.throws(() => validateExperimentMatrixSize(matrix({ repeats: 0 })), /integer from 1/);
  assert.throws(() => validateExperimentMatrixSize(matrix({ repeats: 1.5 })), /integer from 1/);
  assert.throws(
    () =>
      validateExperimentMatrixSize(
        matrix({ targets: Array.from({ length: 101 }, (_, index) => String(index)) }),
      ),
    /100-value limit/,
  );
  assert.throws(
    () => validateExperimentMatrixSize(matrix({ models: ["same", "same"] })),
    /must be unique/,
  );
});

test("terminal cells are immutable when a sweep resumes", () => {
  assert.equal(isRunnableExperimentCellStatus("pending"), true);
  assert.equal(isRunnableExperimentCellStatus("running"), true);
  for (const status of ["done", "failed", "unscored", "blocked", "skipped"]) {
    assert.equal(isRunnableExperimentCellStatus(status), false, status);
  }
});

test("only exact completed or failed rows are comparison eligible", () => {
  const exact = { scopeExact: true, phase: "done" };
  assert.equal(isComparisonEligibleCell({ status: "done", metrics: exact }), true);
  assert.equal(isComparisonEligibleCell({ status: "failed", metrics: exact }), true);
  assert.equal(isComparisonEligibleCell({ status: "skipped", metrics: exact }), false);
  assert.equal(
    isComparisonEligibleCell({
      status: "done",
      metrics: { scopeExact: false, phase: "unscored" },
    }),
    false,
  );
});

test("experiment ids cannot overwrite persisted evidence", () => {
  assert.doesNotThrow(() => assertExperimentIdAvailable(false, "new"));
  assert.throws(() => assertExperimentIdAvailable(true, "existing"), /cannot be overwritten/);
});

test("clone cleanup waits for cloning and live-run lifecycles to quiesce", () => {
  const pending = { status: "pending" };
  const running = { status: "running", trackedRunId: null };
  assert.doesNotThrow(() => assertExperimentDeletable(false, [pending]));
  assert.throws(() => assertExperimentDeletable(true, [pending]), /wait for its active cell/);
  assert.throws(() => assertExperimentDeletable(false, [running]), /wait for its active cell/);
});

test("restored cells must remain a one-to-one copy of the recorded design", () => {
  const definition = {
    ...matrix({ targets: ["A.lean", "B.lean"] }),
    id: "matrix",
    base: { target: "", profile: "", model: "" },
  };
  const cells = ["A.lean", "B.lean"].map((target, index) => ({
    id: `cell-${index}`,
    matrixId: "matrix",
    target,
    profile: "p",
    model: "m",
    repeat: 1,
    status: "done",
    runId: `run-${index}`,
    trackedRunId: `tracked-${index}`,
  }));
  assert.doesNotThrow(() => validateRestoredExperimentCells(definition, cells));
  assert.doesNotThrow(() =>
    validateRestoredExperimentCells(definition, [
      { ...cells[0], targetIdentity: "./A.lean" },
      { ...cells[1], targetIdentity: "B.lean" },
    ]),
  );
  assert.throws(
    () =>
      validateRestoredExperimentCells(definition, [
        { ...cells[0], targetIdentity: "a.lean" },
        { ...cells[1], targetIdentity: "B.lean" },
      ]),
    /target identity does not match/,
  );
  assert.doesNotThrow(() =>
    validateRestoredExperimentCells(
      { ...definition, targets: ["./A.lean", "dir//B.lean"] },
      [
        cells[0],
        { ...cells[1], target: "dir/B.lean" },
      ],
    ),
  );
  assert.throws(
    () => validateRestoredExperimentCells(definition, [cells[0], { ...cells[1], id: cells[0].id }]),
    /invalid or duplicate cell identity/,
  );
  assert.throws(
    () =>
      validateRestoredExperimentCells(definition, [
        cells[0],
        { ...cells[1], target: "A.lean" },
      ]),
    /do not match the recorded matrix design/,
  );
  assert.throws(
    () =>
      validateRestoredExperimentCells(definition, [
        { ...cells[0], plannedOrder: 1 },
        { ...cells[1], plannedOrder: 1 },
      ]),
    /invalid planned execution order/,
  );
});
