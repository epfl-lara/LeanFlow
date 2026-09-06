/** A run reads as agent sessions of turns, not as an undifferentiated row list. */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  elapsed,
  humanEventLabel,
  indexSessions,
  segmentFacts,
  segmentLogRows,
  shouldFrameSegment,
  statusTone,
  turnNumber,
} from "../dist/test/logRows.mjs";

let seq = 0;
function event(type, agentId, details = {}, timestamp = "2026-09-06T15:21:14+00:00") {
  seq += 1;
  return {
    event_id: `e${seq}`,
    timestamp,
    type,
    run_id: "prove-run-a",
    agent_id: agentId,
    task_label: "prove",
    run_scope: "top-level",
    message: type,
    details,
  };
}

test("wire event types read as labels, including the mixed hyphen and underscore spellings", () => {
  assert.equal(humanEventLabel("api-response"), "Response");
  assert.equal(humanEventLabel("job-session-start"), "Session start");
  assert.equal(humanEventLabel("job_finished"), "Job finished");
  assert.equal(humanEventLabel("submission_checked"), "Submission check");
  // An unknown type is normalized the same way rather than shown raw.
  assert.equal(humanEventLabel("research-portfolio-rebalanced"), "Research portfolio rebalanced");
  assert.equal(humanEventLabel("some_new_thing"), "Some new thing");
  assert.equal(humanEventLabel(""), "");
});

test("session facts are collected from the whole stream, including budget and outcome", () => {
  const stream = [
    event("job-session-start", "prover_00007", { role: "prover", api_budget: 200, api_calls: 0 }, "2026-09-06T15:21:14+00:00"),
    event("api-response", "prover_00007", { api_calls: 4, node_id: "n_supported_kernel_direction" }, "2026-09-06T15:24:13+00:00"),
    event("api-response", "prover_00007", { api_calls: 9 }, "2026-09-06T15:29:00+00:00"),
    event("job-session-end", "prover_00007", { status: "completed", api_calls: 9 }, "2026-09-06T15:30:14+00:00"),
  ];
  const sessions = indexSessions(stream);
  const info = sessions.get("prover_00007");
  assert.equal(info.role, "prover");
  assert.equal(info.nodeId, "n_supported_kernel_direction");
  assert.equal(info.status, "completed");
  assert.equal(info.calls, 9);
  assert.equal(info.budget, 200);
  assert.deepEqual(segmentFacts({ agentId: "prover_00007", session: info, events: [], key: "" }), [
    "prover",
    "n_supported_kernel_direction",
    "9 / 200 calls",
    "9m 0s",
  ]);
});

test("segments keep the timeline: interleaved agents stay interleaved", () => {
  const rows = [
    event("job-session-start", "a"),
    event("api-response", "a"),
    event("api-response", "b"),
    event("api-response", "a"),
  ];
  const segments = segmentLogRows(rows, indexSessions(rows));
  assert.deepEqual(segments.map((s) => s.agentId), ["a", "b", "a"]);
  assert.deepEqual(segments.map((s) => s.events.length), [2, 1, 1]);
});

test("a new session starts a new segment even for the same agent", () => {
  const rows = [
    event("job-session-start", "a"),
    event("api-response", "a"),
    event("job-session-start", "a"),
    event("api-response", "a"),
  ];
  const segments = segmentLogRows(rows, indexSessions(rows));
  assert.equal(segments.length, 2);
  assert.deepEqual(segments.map((s) => s.events.length), [2, 2]);
  assert.notEqual(segments[0].key, segments[1].key);
});

test("a segment carries session facts even when the filter hides the boundary rows", () => {
  const stream = [
    event("job-session-start", "prover_1", { role: "prover", api_budget: 300 }),
    event("tool-result", "prover_1", { tool: "read_file" }),
    event("job-session-end", "prover_1", { status: "budget_exhausted", api_calls: 300 }),
  ];
  const sessions = indexSessions(stream);
  const toolRowsOnly = stream.filter((item) => item.type === "tool-result");
  const [segment] = segmentLogRows(toolRowsOnly, sessions);
  assert.equal(segment.session.status, "budget_exhausted");
  assert.ok(segmentFacts(segment).includes("300 / 300 calls"));
});

test("status tone separates a finished job from a failed one", () => {
  assert.equal(statusTone("completed"), "ok");
  assert.equal(statusTone("provider_error"), "bad");
  assert.equal(statusTone("budget_exhausted"), "warn");
  assert.equal(statusTone(""), "");
});

test("only provider turns carry a turn number", () => {
  assert.equal(turnNumber(event("api-response", "a", { api_calls: 12 })), 12);
  assert.equal(turnNumber(event("api-request", "a", { api_calls: 1 })), 1);
  assert.equal(turnNumber(event("tool-result", "a", { api_calls: 12 })), null);
  assert.equal(turnNumber(event("api-response", "a", {})), null);
});

test("elapsed reports a duration only when both stamps parse in order", () => {
  assert.equal(elapsed("2026-09-06T15:21:14+00:00", "2026-09-06T15:21:44+00:00"), "30s");
  assert.equal(elapsed("2026-09-06T15:21:14+00:00", "2026-09-06T15:30:14+00:00"), "9m 0s");
  assert.equal(elapsed("2026-09-06T15:21:14+00:00", "2026-09-06T17:51:14+00:00"), "2h 30m");
  assert.equal(elapsed("nope", "2026-09-06T15:30:14+00:00"), "");
  assert.equal(elapsed("2026-09-06T15:30:14+00:00", "2026-09-06T15:21:14+00:00"), "");
});

test("interleaving slivers stay plain rows; real units of work get a header", () => {
  const sliver = { key: "a:e1", agentId: "prover_1", session: null, events: [event("submission_checked", "prover_1")] };
  assert.equal(shouldFrameSegment(sliver), false);

  const ending = {
    key: "a:e2",
    agentId: "prover_1",
    session: null,
    events: [event("job-session-end", "prover_1"), event("job_finished", "prover_1")],
  };
  assert.equal(shouldFrameSegment(ending), true, "a session boundary names the job that ended");

  const long = {
    key: "a:e3",
    agentId: "prover_1",
    session: null,
    events: [1, 2, 3, 4].map(() => event("api-response", "prover_1")),
  };
  assert.equal(shouldFrameSegment(long), true);

  const short = {
    key: "a:e4",
    agentId: "prover_1",
    session: null,
    events: [event("api-response", "prover_1"), event("api-response", "prover_1")],
  };
  assert.equal(shouldFrameSegment(short), false);

  const workflow = { key: ":e5", agentId: "", session: null, events: [event("runner-start", "")] };
  assert.equal(shouldFrameSegment(workflow), false, "workflow-level rows have no agent to frame");
});
