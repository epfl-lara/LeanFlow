/** Agent output remains attributable and expands to the original reported text. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { agentActivity } from "../dist/test/agentActivity.mjs";
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
