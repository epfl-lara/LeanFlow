import assert from "node:assert/strict";
import { test } from "node:test";

import { auditSourceRunOverlap } from "../dist/test/experimentValidity.mjs";

function run(overrides = {}) {
  return {
    run_id: "source-run",
    started_at: "2026-08-11T10:00:00.000Z",
    updated_at: "2026-08-11T10:05:00.000Z",
    finalized_at: "2026-08-11T10:05:01.000Z",
    terminal: true,
    ...overrides,
  };
}

test("durable source intervals detect completed and active overlap", () => {
  const audit = auditSourceRunOverlap(
    "2026-08-11T10:04:00.000Z",
    "2026-08-11T10:06:00.000Z",
    [
      run(),
      run({
        run_id: "active-source-run",
        started_at: "2026-08-11T10:05:30.000Z",
        updated_at: "2026-08-11T10:05:40.000Z",
        finalized_at: "",
        terminal: false,
      }),
    ],
  );
  assert.equal(audit.cellIntervalReadable, true);
  assert.deepEqual(audit.overlappingRunIds, ["source-run", "active-source-run"]);
  assert.deepEqual(audit.unreadableRunIds, []);
});

test("source runs wholly before or after the cell do not contaminate it", () => {
  const audit = auditSourceRunOverlap(
    "2026-08-11T10:10:00.000Z",
    "2026-08-11T10:20:00.000Z",
    [
      run({
        run_id: "before",
        started_at: "",
        updated_at: "2026-08-11T10:09:00.000Z",
        finalized_at: "",
      }),
      run({
        run_id: "after",
        started_at: "2026-08-11T10:21:00.000Z",
        updated_at: "",
        finalized_at: "not-a-time",
      }),
    ],
  );
  assert.deepEqual(audit.overlappingRunIds, []);
  assert.deepEqual(audit.unreadableRunIds, []);
});

test("a retained crash stays open-ended after its last recorded activity", () => {
  const audit = auditSourceRunOverlap(
    "2026-08-11T10:10:00.000Z",
    "2026-08-11T10:20:00.000Z",
    [
      run({
        run_id: "retained-crash-overlap",
        stream_source: "retained",
        terminal: false,
        started_at: "2026-08-11T10:05:00.000Z",
        updated_at: "2026-08-11T10:06:00.000Z",
        finalized_at: "",
      }),
    ],
  );
  assert.deepEqual(audit.overlappingRunIds, ["retained-crash-overlap"]);
});

test("ambiguous retained history fails closed instead of proving exclusivity", () => {
  const unreadable = auditSourceRunOverlap(
    "2026-08-11T10:10:00.000Z",
    "2026-08-11T10:20:00.000Z",
    [run({ started_at: "", updated_at: "", finalized_at: "" })],
  );
  assert.deepEqual(unreadable.unreadableRunIds, ["source-run"]);

  const inverted = auditSourceRunOverlap(
    "2026-08-11T10:10:00.000Z",
    "2026-08-11T10:20:00.000Z",
    [
      run({
        run_id: "inverted",
        started_at: "2026-08-11T11:00:00.000Z",
        updated_at: "2026-08-11T09:00:00.000Z",
        finalized_at: "",
      }),
    ],
  );
  assert.deepEqual(inverted.unreadableRunIds, ["inverted"]);

  const badCell = auditSourceRunOverlap("not-a-time", null, []);
  assert.equal(badCell.cellIntervalReadable, false);
});
