/** Activity-buffer behavior across normal polling and cursor rotation. */
import assert from "node:assert/strict";
import { test } from "node:test";

import { mergeActivityEvents } from "../dist/test/eventBuffer.mjs";

function event(id) {
  return { event_id: id, type: "tool-call" };
}

test("cursor fallback does not append an already buffered tail twice", () => {
  const existing = [event("e1"), event("e2"), event("e3")];
  const result = mergeActivityEvents(existing, [event("e2"), event("e3"), event("e4")], 10);

  assert.deepEqual(result.buffer.map((item) => item.event_id), ["e1", "e2", "e3", "e4"]);
  assert.deepEqual(result.appended.map((item) => item.event_id), ["e4"]);
  assert.equal(result.trimmed, false);
});

test("duplicate ids inside one payload are emitted once", () => {
  const result = mergeActivityEvents([], [event("e1"), event("e1"), event("e2")], 10);

  assert.deepEqual(result.buffer.map((item) => item.event_id), ["e1", "e2"]);
  assert.deepEqual(result.appended.map((item) => item.event_id), ["e1", "e2"]);
  assert.equal(result.trimmed, false);
});

test("the merged buffer keeps only the newest configured tail", () => {
  const result = mergeActivityEvents(
    [event("e1"), event("e2")],
    [event("e3"), event("e4")],
    3,
  );

  assert.deepEqual(result.buffer.map((item) => item.event_id), ["e2", "e3", "e4"]);
  assert.deepEqual(result.appended.map((item) => item.event_id), ["e3", "e4"]);
  assert.equal(result.trimmed, true);
});
