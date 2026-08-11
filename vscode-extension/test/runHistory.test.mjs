import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeRunHistoryPayload } from "../dist/test/runHistory.mjs";

test("run history is usable only with an explicit complete v2 archive envelope", () => {
  const run = { run_id: "run-a", terminal: false, stream_source: "retained" };
  const complete = normalizeRunHistoryPayload({
    version: 2,
    complete: true,
    completeness_issues: [],
    archive_audit: { complete: true },
    limit: 100,
    total_count: 1,
    truncated: false,
    count: 1,
    runs: [run],
  });
  assert.equal(complete.complete, true);
  assert.equal(complete.runs[0].run_id, "run-a");

  for (const payload of [
    { ...complete, completeness_issues: [], archive_audit: { complete: true } },
    {
      version: 2,
      complete: true,
      completeness_issues: [],
      archive_audit: { complete: false },
      limit: 100,
      total_count: 1,
      truncated: false,
      count: 1,
      runs: [run],
    },
    {
      version: 2,
      complete: true,
      completeness_issues: [],
      archive_audit: { complete: true },
      limit: 100,
      total_count: 2,
      truncated: false,
      count: 2,
      runs: [run],
    },
    {
      version: 2,
      complete: true,
      completeness_issues: [],
      archive_audit: { complete: true },
      limit: 1,
      total_count: 2,
      truncated: true,
      count: 1,
      runs: [run],
    },
  ]) {
    assert.equal(normalizeRunHistoryPayload(payload).complete, false);
  }
});

test("CLI completeness issues survive normalization for an exclusion reason", () => {
  const history = normalizeRunHistoryPayload({
    version: 2,
    complete: false,
    completeness_issues: ["retained-catalog-digest-mismatch"],
    archive_audit: { complete: false },
    limit: 100,
    total_count: 0,
    truncated: false,
    count: 0,
    runs: [],
  });
  assert.equal(history.complete, false);
  assert.deepEqual(history.completenessIssues, ["retained-catalog-digest-mismatch"]);
});
