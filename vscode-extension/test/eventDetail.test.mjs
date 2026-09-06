/** Expanding an activity row must surface the model output as readable text, not JSON. */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CONTEXT_DETAIL_KEYS,
  classifyText,
  eventFacts,
  eventOutputSections,
  eventSearchText,
  evidenceDetails,
  evidenceStatus,
  fieldPresentation,
  normalizeEventDetail,
  parseStructured,
  rawDetailsForDisplay,
} from "../dist/test/eventDetail.mjs";

function event(type, details = {}, message = type) {
  return {
    event_id: "e1",
    timestamp: "2026-09-06T13:23:30+00:00",
    type,
    run_id: "prove-run-a",
    agent_id: "orchestrator_00001",
    task_label: "prove",
    run_scope: "top-level",
    message,
    details,
  };
}

function titles(sections) {
  return sections.map((section) => section.title);
}

const LEAN = "import Mathlib\n\ntheorem foo (n : ℕ) : n + 0 = n := by\n  simp\n";
const PYTHON = "from fractions import Fraction\n\ndef total(s, x):\n    return sum(x[j] for j in s)\n";

test("text is classified as prose, Lean, Python, shell, or JSON", () => {
  assert.deepEqual(classifyText(LEAN), { kind: "code", language: "lean" });
  assert.deepEqual(classifyText(PYTHON), { kind: "code", language: "python" });
  assert.deepEqual(classifyText("lake build\nlake exe cache get"), { kind: "code", language: "shell" });
  assert.deepEqual(classifyText('{"accepted": true}'), { kind: "code", language: "json" });
  assert.deepEqual(classifyText("Retain the floating-variable strategy.\nThe bound follows."), { kind: "prose" });
  assert.deepEqual(classifyText("-- only a comment\n-- and another", "Scratch.lean"), { kind: "code", language: "lean" });
  assert.deepEqual(classifyText("x = 1", "program"), { kind: "code", language: "python" });
  assert.deepEqual(classifyText("some notes", "notes.md"), { kind: "prose" });
});

test("field presentation keeps short values inline and reads files by their path", () => {
  assert.deepEqual(fieldPresentation("path", "PLAN_job.md"), { kind: "inline" });
  assert.deepEqual(fieldPresentation("content", "-- comment\n-- more", { path: "Scratch.lean" }), { kind: "code", language: "lean" });
  assert.deepEqual(fieldPresentation("content", "# Plan\n\n- step one\n- step two", { path: "PLAN_job.md" }), { kind: "prose" });
  assert.deepEqual(fieldPresentation("stdout", "x\n".repeat(3)), { kind: "code" });
  assert.equal(parseStructured("```json\n{\"a\": 1}\n```").a, 1);
  assert.equal(parseStructured("not json"), null);
});

test("an api-response expands to the model output and its tool calls as fields", () => {
  const evidence = {
    api_calls: 10,
    assistant: {
      role: "assistant",
      content: "Let me read the paper first, then draft the invariant.",
      reasoning: "**Checking the invariant**",
      tool_calls: [
        {
          type: "function",
          function: { name: "write_file", arguments: JSON.stringify({ path: "Scratch.lean", content: LEAN }) },
        },
      ],
    },
  };
  const sections = eventOutputSections(
    event("api-response", { content_preview: "Let me read…", api_calls: 10 }),
    evidence,
  );
  assert.deepEqual(titles(sections), ["Model output", "Reasoning summary", "Tool call · write_file"]);
  assert.equal(sections[0].kind, "prose");
  assert.equal(sections[0].value, "Let me read the paper first, then draft the invariant.");
  assert.equal(sections[2].kind, "fields");
  assert.deepEqual(sections[2].value, { path: "Scratch.lean", content: LEAN });
  assert.match(sections[2].text, /"path": "Scratch\.lean"/);
});

test("a JSON report from the model becomes labelled fields instead of an escaped string", () => {
  const plan = "STATUS\nRetain the floating-variable strategy.\n".repeat(8);
  const sections = eventOutputSections(event("api-response"), {
    assistant: { content: "```json\n" + JSON.stringify({ plan, nodes: [{ id: "n1" }] }) + "\n```" },
  });
  assert.deepEqual(titles(sections), ["Model output"]);
  assert.equal(sections[0].kind, "fields");
  assert.equal(sections[0].value.plan, plan);
  assert.deepEqual(sections[0].value.nodes, [{ id: "n1" }]);
  const final = eventOutputSections(event("job-session-end", { status: "completed" }), {
    final_response: JSON.stringify({ report: "x".repeat(300) }),
    error: "",
  });
  assert.deepEqual(titles(final), ["Final response"]);
  assert.equal(final[0].kind, "fields");
});

test("projected previews stand in until the full record arrives", () => {
  const sections = eventOutputSections(
    event("api-response", {
      content_preview: "Short answer…",
      tool_calls: [{ name: "lean_search", arguments_preview: '{"q":1}' }],
    }),
    null,
  );
  assert.deepEqual(titles(sections), ["Model output", "Tool call · lean_search"]);
  assert.equal(sections[0].value, "Short answer…");
  assert.deepEqual(sections[1].value, { q: 1 });
});

test("tool results keep their structure and flag failures", () => {
  const ok = eventOutputSections(event("tool-result", { tool: "compute" }), {
    tool: "compute",
    arguments: JSON.stringify({ program: PYTHON, timeout_s: 30 }),
    result: { success: true, stdout: "42\n", total_lines: 3 },
  });
  assert.deepEqual(titles(ok), ["Arguments", "Result"]);
  assert.equal(ok[0].kind, "fields");
  assert.equal(ok[0].value.program, PYTHON);
  assert.equal(ok[1].value.total_lines, 3);
  assert.equal(ok[1].tone, undefined);

  const failed = eventOutputSections(
    event("tool-result", { tool: "lean_check", success: false }),
    { tool: "lean_check", arguments: "{}", result: { success: false, error: "boom" } },
  );
  const result = failed.find((section) => section.id === "result");
  assert.equal(result.title, "Result (failed)");
  assert.equal(result.tone, "err");
  assert.equal(failed.find((section) => section.id === "arguments"), undefined, "empty arguments are omitted");
});

test("legacy assistant-response rows expand from their own details", () => {
  const sections = eventOutputSections(
    event("assistant-response", {
      content: "The base case is trivial.",
      tool_calls: [{ id: "c1", name: "patch", arguments: '{"path":"Main.lean"}' }],
    }),
    null,
  );
  assert.deepEqual(titles(sections), ["Model output", "Tool call · patch"]);
  assert.deepEqual(sections[1].value, { path: "Main.lean" });
});

test("rows without model output fall back to the recorded controller details", () => {
  const sections = eventOutputSections(
    event("candidate_checked", { node_id: "n1", accepted: false }),
    { node_id: "n1", accepted: false, result: { accepted: false, error: "sorry remains" } },
  );
  assert.deepEqual(titles(sections), ["Verification result"]);
  assert.equal(sections[0].tone, "err");
  assert.equal(sections[0].kind, "fields");
  assert.deepEqual(eventOutputSections(event("api-request", { api_calls: 1 }), null), []);
  const generic = eventOutputSections(event("libraries_installed"), {
    accepted: true,
    packages: ["physlib"],
  });
  assert.deepEqual(titles(generic), ["Recorded details"]);
  assert.deepEqual(generic[0].value, { accepted: true, packages: ["physlib"] });
});

test("facts summarize budget, model, tokens, and identity", () => {
  const facts = eventFacts(
    event("api-response", {
      job_id: "orchestrator_00001",
      node_id: "n_1",
      api_calls: 10,
      api_budget: 50,
      model: "gpt-6",
      input_tokens: 1234,
      output_tokens: 56,
      finish_reason: "tool_calls",
    }),
    null,
  );
  assert.deepEqual(facts, [
    { label: "agent", value: "orchestrator_00001" },
    { label: "node", value: "n_1" },
    { label: "calls", value: "10 / 50" },
    { label: "model", value: "gpt-6" },
    { label: "session tokens", value: "in 1,234 · out 56" },
    { label: "finish", value: "tool_calls" },
  ]);
});

test("search text includes previews and requested tool names", () => {
  const text = eventSearchText(
    event(
      "api-response",
      {
        content_preview: "Use Finset.sum_le_sum",
        tool_calls: [{ name: "lean_search", arguments_preview: "sum_le" }],
      },
      "Use Finset…",
    ),
  );
  assert.ok(text.includes("finset.sum_le_sum"));
  assert.ok(text.includes("lean_search"));
  assert.ok(text.includes("orchestrator_00001"));
});

test("raw details drop process identity and launch context", () => {
  const raw = rawDetailsForDisplay({
    process_id: 4686,
    project_root: "/tmp/p",
    effective_prompt: "prove it",
    api_calls: 3,
    tool: "read_file",
  });
  assert.deepEqual(raw, { api_calls: 3, tool: "read_file" });
  assert.ok(CONTEXT_DETAIL_KEYS.has("process_token_sha256"));
});

test("normalizeEventDetail tolerates malformed payloads and keeps the join", () => {
  const detail = normalizeEventDetail(
    {
      version: 1,
      found: true,
      event: { event_id: "e1", type: "api-response", details: { api_calls: 2 } },
      evidence: {
        source: "job-log",
        path: "/p/jobs/j/events.jsonl",
        match: "evidence_id",
        record: {
          kind: "api-response",
          timestamp: "t",
          evidence_id: "abc",
          details: { assistant: { content: "hi" } },
        },
      },
    },
    "prove-run-a",
    "e1",
  );
  assert.equal(detail.found, true);
  assert.equal(detail.event.type, "api-response");
  assert.deepEqual(evidenceDetails(detail), { assistant: { content: "hi" } });

  const broken = normalizeEventDetail({ found: true, event: null, evidence: "nope" }, "r", "e");
  assert.equal(broken.found, false);
  assert.equal(broken.evidence.source, "none");
  assert.equal(evidenceDetails(broken), null);
  assert.equal(normalizeEventDetail(undefined, "r", "e").evidence.record, null);
});

test("evidence status explains loading, missing, preview-only, and heuristic joins", () => {
  const preview = event("api-response", { content_preview: "x" });
  assert.equal(evidenceStatus(preview, null, true, "").tone, "info");
  assert.equal(evidenceStatus(preview, null, false, "boom").tone, "error");
  assert.equal(evidenceStatus(preview, null, false, ""), null);
  const none = normalizeEventDetail(
    { found: true, event: preview, evidence: { source: "none" } },
    "r",
    "e1",
  );
  assert.equal(evidenceStatus(preview, none, false, "").tone, "warn");
  assert.equal(evidenceStatus(event("api-request"), none, false, ""), null);
  const gone = normalizeEventDetail({ found: false, event: null }, "r", "e1");
  assert.match(evidenceStatus(preview, gone, false, "").text, /no longer/);
  const heuristic = normalizeEventDetail(
    {
      found: true,
      event: preview,
      evidence: { source: "job-log", match: "heuristic", record: { kind: "api-response", details: {} } },
    },
    "r",
    "e1",
  );
  assert.match(evidenceStatus(preview, heuristic, false, "").text, /by kind and time/);
  const exact = normalizeEventDetail(
    {
      found: true,
      event: preview,
      evidence: { source: "job-log", match: "evidence_id", record: { kind: "api-response", details: {} } },
    },
    "r",
    "e1",
  );
  assert.equal(evidenceStatus(preview, exact, false, ""), null);
});
