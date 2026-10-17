/** Preparation must distinguish retained mathematics from the new run's live activity. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { mount, textOf } from "./helpers/webviewFixture.mjs";

test("preparation labels the previous mathematical snapshot and keeps Stop available", () => {
  const ui = mount("live", { proverStates: {} });
  try {
    ui.update(fixture => {
      fixture.app.runs[0].request.kind = "formalize";
      fixture.app.liveStatus = {
        run_id: "run-1", workflow_kind: "formalize", phase: "dependency-planning",
        startup_reconciliation_pending: true, runtime_heartbeat_at: new Date().toISOString(),
        sorry_count: 7, project_sorry_count: 9, proof_solved: true,
        current_blocker: "Earlier source review is incomplete.",
        last_activity_message: "Inspecting this paper's dependencies.",
      };
    });
    const text = textOf(ui.root());
    assert.match(text, /Mathematical results below are from the previous run/);
    assert.match(text, /Previous mathematical blocker/);
    assert.doesNotMatch(text, /Current blocker/);
    assert.match(text, /Inspecting this paper's dependencies/);
    assert.match(textOf(ui.root().findByProps({ "aria-label": "Previous mathematical status" })), /Project sorries9/);
    ui.click("Stop");
    assert.deepEqual(ui.messages.at(-1), { type: "stopRun", id: "tracked-1" });

    ui.update(fixture => {
      fixture.app.liveStatus.terminal = false;
      fixture.app.liveStatus.preflight_error = "Dependency planning returned too many notes.";
    });
    assert.match(textOf(ui.root()), /Preparation error: Dependency planning returned too many notes/);
    assert.doesNotMatch(textOf(ui.root()), /Preparation stopped/);
    assert.match(textOf(ui.root()), /Previous mathematical blockerEarlier source review is incomplete/);

    ui.update(fixture => {
      fixture.app.liveStatus.startup_reconciliation_pending = false;
      fixture.app.liveStatus.current_blocker = "New statement review is pending.";
    });
    assert.doesNotMatch(textOf(ui.root()), /Mathematical results below are from the previous run|Previous mathematical blocker|Preparation error:/);
    assert.match(textOf(ui.root()), /Current blockerNew statement review is pending/);
    assert.ok(ui.root().findByProps({ "aria-label": "Current mathematical status" }));
  } finally { ui.dispose(); }
});

test("heartbeats do not make an earlier preparation activity look new", () => {
  const ui = mount("live", { proverStates: {} });
  try {
    ui.update(fixture => {
      fixture.app.runs[0].request.kind = "formalize";
      fixture.app.liveStatus = {
        run_id: "run-1", workflow_kind: "formalize", phase: "dependency-planning",
        runtime_heartbeat_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        last_activity_at: new Date(Date.now() - 300_000).toISOString(),
        last_activity_message: "Inspecting this paper's dependencies.",
      };
    });
    assert.match(textOf(ui.root().findByProps({ title: "Last activity" })), /5m ago/);
  } finally { ui.dispose(); }
});
