/** Assert that runtime evidence survives normalization, fallback and resumed jobs. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { normalizeProverSnapshot } from "../dist/test/prover.mjs";
import { reportedSettings, modelSettingsHistory, modelSettingsSource, roleModelReports } from "../dist/test/proverModelSettings.mjs";

const initial = { invocation_id: "call-1", backend: "lemex", session_id: "session-1", observed_at: "2026-10-12T10:00:00Z", requested_model: "", requested_reasoning_effort: "", model: "", model_source: "unreported", reasoning_effort: "", reasoning_source: "unreported" };
const actual = { ...initial, model: "actual-model", model_source: "native_session", reasoning_effort: "xhigh", reasoning_source: "native_session" };
function snapshot(history, extra = {}) {
  return normalizeProverSnapshot({ run_id: "run-1", config: { agent_calls: null, model: "different-current-model" }, jobs: [{ id: "job-1", role: "prover", provider: "lemex", model_settings: history, ...extra }] }, "run-1");
}

test("each backend retains confirmed settings independently of requested overrides", () => {
  for (const backend of ["codex", "claude", "lemex"]) {
    const state = snapshot([{ ...actual, backend, requested_model: "alias", requested_reasoning_effort: "high" }]);
    const report = reportedSettings(state.jobs[0]);
    assert.equal(report.backend, backend);
    assert.equal(report.model, "actual-model");
    assert.equal(report.reasoning_effort, "xhigh");
    assert.equal(report.requested_model, "alias");
    assert.equal(report.requested_reasoning_effort, "high");
    assert.equal(modelSettingsSource(report.model_source), "Runtime session");
  }
});

test("resumes do not claim previous settings while telemetry is pending", () => {
  const state = snapshot([initial, actual, { ...initial, invocation_id: "call-2" }]);
  const report = reportedSettings(state.jobs[0]);
  assert.equal(report.invocation_id, "call-2");
  assert.equal(report.model, "");
  assert.equal(report.reasoning_effort, "");
  assert.equal(state.jobs[0].model_settings[1].model, "actual-model");
  assert.equal(modelSettingsHistory(state.jobs[0]).length, 3);
});

test("history retains model changes while dropping duplicate identity updates", () => {
  const changed = { ...actual, model: "fallback-model", model_source: "native_response", reasoning_effort: "", reasoning_source: "unreported" };
  const state = snapshot([initial, { ...initial, session_id: "session-2" }, actual, changed]);
  assert.deepEqual(modelSettingsHistory(state.jobs[0]).map(row => row.model), ["", "actual-model", "fallback-model"]);
  assert.equal(reportedSettings(state.jobs[0]).reasoning_effort, "");
});

test("untrusted or absent provenance never becomes confirmed metadata", () => {
  const state = snapshot([{ ...actual, model_source: "requested", reasoning_source: "guessed" }]);
  assert.equal(reportedSettings(state.jobs[0]).model, "");
  assert.equal(reportedSettings(state.jobs[0]).reasoning_effort, "");
  assert.equal(reportedSettings(snapshot([]).jobs[0]), null);
  assert.equal(roleModelReports(state, "orchestrator").length, 0);
  assert.equal(roleModelReports(state, "prover")[0].id, "job-1");
});
