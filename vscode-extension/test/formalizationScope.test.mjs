/** Deferred transport and stale replies must never cross workspace or request ownership. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { FormalizationService } from "../dist/test/formalizationService.mjs";
import { FormalizationMessageGate } from "../dist/test/formalizationScope.mjs";
import { parseWebviewMessage } from "../dist/test/messageSchema.mjs";
import { formalizationFixture } from "./helpers/formalizationFixture.mjs";

test("a bound service refuses delayed results and further mutations after A changes to B", async () => {
  let root = "/project-A", release;
  const calls = [];
  const delayed = new Promise(resolve => { release = resolve; });
  const service = new FormalizationService(() => root, async (args, options) => { calls.push({args, ...options}); return delayed; });
  const scopeA = service.workspaceScope();
  const bound = service.forWorkspace(scopeA);
  const pending = bound.show("paper-1");
  root = "/project-B";
  release(JSON.stringify(formalizationFixture()));
  await assert.rejects(pending, /workspace changed/);
  await assert.rejects(bound.action("paper-1", "revision-1", { type: "set_scope", item_id: "item-1", scope: "excluded", reason: "old view" }), /workspace changed/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].cwd, "/project-A");
  service.workspaceScope();
  root = "/project-A";
  assert.throws(() => service.assertWorkspace(scopeA), /workspace changed/);
});

test("the view rejects A status in B and obsolete requests with identical document IDs", () => {
  const gate = new FormalizationMessageGate();
  const scopeA = { id: "host", generation: 1, root: "/A" };
  const scopeB = { id: "host", generation: 2, root: "/B" };
  gate.acceptWorkspace(scopeA);
  const old = gate.request("showFormalization");
  const current = gate.request("showFormalization");
  const state = { type: "formalizationState", documentId: "same", formalizationWorkspace: scopeA };
  assert.equal(gate.accepts({ ...state, formalizationRequest: old }), false);
  assert.equal(gate.accepts({ ...state, formalizationRequest: current }), true);
  gate.acceptWorkspace(scopeB);
  assert.equal(gate.accepts({ ...state, formalizationRequest: current }), false);
  assert.equal(gate.acceptWorkspace(scopeA), false);
  assert.deepEqual(gate.scope(), scopeB);
  const preparing = gate.request("prepareFormalization");
  assert.deepEqual(gate.request("cancelFormalizationPreparation"), preparing);
});

test("the real message parser retains correlation tokens without accepting malformed metadata", () => {
  const scope = { id: "host-issued", generation: 4, root: "/A", ignored: "not authority" };
  const request = { channel: "document", id: 8 };
  const message = { type: "showFormalization", documentId: "paper-1", formalizationWorkspace: scope, formalizationRequest: request };
  const parsed = parseWebviewMessage(message);
  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed.message.formalizationWorkspace, { id: scope.id, generation: 4, root: "/A" });
  assert.deepEqual(parsed.message.formalizationRequest, request);
  assert.equal(parseWebviewMessage({ ...message, formalizationWorkspace: { ...scope, generation: -1 } }).ok, false);
  assert.equal(parseWebviewMessage({ ...message, formalizationRequest: { ...request, id: NaN } }).ok, false);
});

test("baseline setup replies have a separate workspace-bound channel from source selection", () => {
  const gate = new FormalizationMessageGate();
  const scope = { id: "host", generation: 1, root: "/paper" };
  gate.acceptWorkspace(scope);
  const source = gate.request("showFormalization");
  const setup = gate.request("formalizationProjectSetup");
  assert.equal(setup.channel, "setup");
  assert.equal(gate.accepts({ type: "formalizationState", formalizationWorkspace: scope, formalizationRequest: source }), true);
  gate.request("showFormalization");
  assert.equal(gate.accepts({ type: "formalizationProjectSetupState", formalizationWorkspace: scope, formalizationRequest: setup }), true);
  const current = gate.request("formalizationProjectSetup");
  assert.equal(gate.accepts({ type: "formalizationProjectSetupState", formalizationWorkspace: scope, formalizationRequest: setup }), false);
  assert.equal(gate.accepts({ type: "formalizationProjectSetupState", formalizationWorkspace: scope, formalizationRequest: current }), true);
});


test("verified preview ownership survives untitled focus and is retired on workspace invalidation", () => {
  let project = "/source-B";
  let uri;
  let service;
  service = new FormalizationService(() => service.previewRoot(uri) || project, async () => "{}");
  const scope = service.workspaceScope();
  service.registerPreview("untitled:source-B", scope);
  uri = "untitled:source-B";
  project = "/Lean-A";
  assert.equal(service.workspaceScope().root, "/source-B");
  service.invalidateWorkspace();
  assert.equal(service.previewRoot(uri), "");
  assert.equal(service.workspaceScope().root, "/Lean-A");
});


test("old preparation cannot replace a newer document selection while its progress remains observable", () => {
  const gate = new FormalizationMessageGate();
  const workspace = { id: "host", generation: 1, root: "/same-workspace" };
  gate.acceptWorkspace(workspace);
  gate.request("showFormalization");
  const preparation = gate.request("prepareFormalization");
  gate.request("openFormalizationArtifact");
  assert.equal(gate.accepts({ type: "formalizationState", formalizationWorkspace: workspace, formalizationRequest: preparation }), true);
  gate.request("showFormalization");
  assert.equal(gate.accepts({ type: "formalizationState", formalizationWorkspace: workspace, formalizationRequest: preparation }), false);
  assert.equal(gate.accepts({ type: "formalizationPreparation", formalizationWorkspace: workspace, formalizationRequest: preparation }), true);
});
