/** Agent output remains attributable and expands to the original reported text. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { agentActivity, coalesceActivity } from "../dist/test/agentActivity.mjs";
import { eventOutputSections, eventFacts } from "../dist/test/eventDetail.mjs";

function event(type, patch = {}) {
  return { event_id: "e1", run_id: "run-1", agent_id: "job-1", timestamp: "2026-10-11T12:00:00Z", type, message: "Working", details: { job_id: "job-1", content_preview: "Short preview…", backend: "lemex" }, ...patch };
}

test("the feed isolates exact runs and jobs and bounds long campaigns", () => {
  const own = Array.from({ length: 45 }, (_, i) => event(i === 44 ? "agent-reasoning" : "agent-output", { event_id: `e${i}`, message: `Update ${i}` }));
  const rows = [...own, event("agent-output", { run_id: "other-run" }), event("agent-output", { details: { job_id: "other-job" } }), event("runner-start")];
  const result = agentActivity(rows, "run-1", "job-1");
  assert.equal(result.rows.length, 30);
  assert.equal(result.total, 45);
  assert.equal(result.latest, "Update 44");
  assert.equal(result.hasReasoning, true);
  assert.equal(agentActivity([], "run-1", "job-1").hasReasoning, false);
});

test("reasoning expansion prefers the full record and labels it as reported", () => {
  const row = event("agent-reasoning");
  const sections = eventOutputSections(row, { text: "Complete reasoning\nwith multiple lines" });
  assert.equal(sections[0].title, "Reported reasoning");
  assert.equal(sections[0].text, "Complete reasoning\nwith multiple lines");
  assert.equal(eventOutputSections(row, null)[0].text, "Short preview…");
  assert.ok(eventFacts(row, null).some(fact => fact.label === "backend" && fact.value === "lemex"));
});

test("native tools show arguments and output without a Lean acceptance verdict", () => {
  const sections = eventOutputSections(event("agent-tool-end", { details: { tool: "Shell" } }), { arguments: "lake env lean Main.lean", result: "Main.lean:1:0: error: unknown identifier", status: "failed" });
  assert.deepEqual(sections.map(section => section.title), ["Tool arguments", "Tool output"]);
  assert.match(sections[1].text, /unknown identifier/);
  assert.ok(!sections.some(section => section.title.includes("Verification")));
});

test("cumulative streamed text becomes one live item and keeps the final durable event", () => {
  const updates = Array.from({ length: 100 }, (_, i) => event("agent-reasoning", {
    event_id: `partial-${i}`, details: { job_id: "job-1", item_id: "item-1", invocation_id: "call-1", reasoning_kind: "trace", partial: true, content_preview: "Proof ".repeat(i + 1) },
  }));
  assert.equal(coalesceActivity(updates).length, 1);
  assert.equal(coalesceActivity(updates)[0].event_id, "partial-99");
  const final = event("agent-reasoning", { event_id: "complete", details: { ...updates[0].details, partial: false, content_preview: "Complete exposed trace" } });
  const result = agentActivity([...updates, final], "run-1", "job-1");
  assert.equal(result.total, 1);
  assert.equal(result.rows[0], final);
  assert.equal(updates.length, 100, "the complete original event buffer remains untouched");
});

test("stream grouping never combines runs, jobs, invocations, types, or reasoning kinds", () => {
  const first = event("agent-reasoning", { details: { job_id: "job-1", item_id: "item-1", invocation_id: "call-1", reasoning_kind: "trace", partial: true } });
  const others = [
    { run_id: "run-2" },
    { agent_id: "agent-2" },
    { details: { ...first.details, job_id: "job-2" } },
    { details: { ...first.details, invocation_id: "call-2" } },
    { details: { ...first.details, reasoning_kind: "summary" } },
    { type: "agent-output" },
  ].map((patch, index) => ({ ...first, event_id: `other-${index}`, ...patch }));
  assert.equal(coalesceActivity([first, ...others]).length, 7);
  assert.equal(coalesceActivity([first, event("agent-output", { details: { partial: true } }), event("agent-output", { details: { partial: true } })]).length, 3, "id-less updates are separate evidence");
});

test("tool output streams in place until its exact invocation completes", () => {
  const details = { job_id: "job-1", item_id: "tool-1", invocation_id: "call-1", tool: "lean_check" };
  const start = event("agent-tool-start", { event_id: "start", details });
  const progress = event("agent-tool-progress", { event_id: "progress", details: { ...details, partial: true, result_preview: "Building…" } });
  const another = event("agent-tool-end", { event_id: "unrelated", details: { ...details, invocation_id: "call-2" } });
  assert.equal(agentActivity([start, progress, another], "run-1", "job-1").runningTools.length, 1);
  const end = event("agent-tool-end", { event_id: "end", details: { ...details, partial: false, result_preview: "Done" } });
  const result = agentActivity([start, progress, another, end], "run-1", "job-1");
  assert.equal(result.runningTools.length, 0);
  assert.deepEqual(result.rows.map(row => row.event_id), ["start", "unrelated", "end"]);
  assert.equal(agentActivity([start, event("job-session-end")], "run-1", "job-1").runningTools.length, 0);
});
