/** Exact job navigation across editor launches, external owners and retained history. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { runChoices, selectedRunChoice, appRunChoices } from "../dist/test/runCatalog.mjs";

const history = (run_id, patch = {}) => ({ run_id, label: run_id, workflow_kind: "prove", started_at: "2026-10-01", updated_at: "2026-10-02", ...patch });
const tracked = (runId, patch = {}) => ({ id: `editor-${runId}`, runId, label: runId, request: { kind: "prove" }, status: "running", startedAt: "2026-10-01", ...patch });

test("one menu deduplicates editor history and puts a verified external job first", () => {
  const choices = runChoices([tracked("old", {status:"failed"})], [history("old"), history("live")], { run_id:"live", process_id:42 });
  assert.deepEqual(choices.map(row => [row.id, row.active, row.status]), [["live",true,"running"],["editor-old",false,"failed"]]);
  assert.equal(selectedRunChoice(choices, null).runId, "live");
  assert.equal(selectedRunChoice(choices, "old").id, "editor-old");
  assert.equal(selectedRunChoice(choices, "missing"), null);
});

test("old historical pids and stale snapshots never count as live jobs", () => {
  const choices = runChoices([], [history("old", { process_id:42 }), history("done", {terminal:true,exit_code:0}), history("worker", {parent_run_id:"old"})], {run_id:"old",process_id:42,stale_snapshot:true});
  assert.equal(choices.length, 2);
  assert.ok(choices.every(row => !row.active));
  assert.equal(choices.find(row => row.id === "old").status, "status unknown");
  assert.equal(choices.find(row => row.id === "done").status, "finished");
});

test("selecting history keeps the project's live owner available in the menu", () => {
  const choices = appRunChoices({runs:[],history:[history("past",{terminal:true,exit_code:130})],liveStatus:null,projectLiveStatus:{run_id:"now",process_id:42}});
  assert.equal(selectedRunChoice(choices,"past").status,"stopped");
  assert.equal(choices.filter(row=>row.active).length,1);
});


test("a resumed campaign selects its live newest editor attempt regardless of input order", () => {
  const old = tracked("formalize-vscode-run-a", { id: "old-attempt", status: "stopped", startedAt: "2026-10-01T00:00:00Z", finishedAt: "2026-10-01T00:01:00Z" });
  const resumed = tracked(old.runId, { id: "new-attempt", startedAt: "2026-10-02T00:00:00Z" });
  for (const runs of [[resumed, old], [old, resumed]]) {
    const choices = runChoices(runs, [history(old.runId, { terminal: true, exit_code: 130 })], null);
    assert.equal(choices.length, 1);
    assert.equal(choices[0].active, true);
    assert.equal(choices[0].id, "new-attempt");
    assert.equal(selectedRunChoice(choices, "new-attempt").runId, old.runId);
    assert.equal(selectedRunChoice(choices, old.runId).id, "new-attempt");
  }
  resumed.status = "finished";
  for (const runs of [[resumed, old], [old, resumed]]) assert.equal(runChoices(runs, [], null)[0].id, "new-attempt");
});
