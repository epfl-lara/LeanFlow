/** Workspace changes must retire child acknowledgments before messages receive a new host token. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { FormalizationMessageGate } from "../dist/test/formalizationScope.mjs";
import { mount, act, textOf, parseWebviewMessage } from "./helpers/webviewFixture.mjs";
import { formalizationFixture } from "./helpers/formalizationFixture.mjs";
import { formalizationConflictFixture } from "./helpers/formalizationConflictFixture.mjs";

function last(ui, type) { return ui.messages.filter(message => message.type === type).at(-1); }

function recoveryFixture() {
  return { schema_version: 1, transactions: [{
    transaction_id: "prep-1", observation_id: "obs-1", history_id: "history-1",
    state: "CONFLICTED", active: true, resolvable: true,
    destinations: { ".lake": "a".repeat(64) },
    retained_slots: { "retained/lake": "b".repeat(64) },
    conflicts: ["Concurrent infrastructure save"], resolutions: [],
  }] };
}

for (const nextScope of [
  { id: "host", generation: 2, root: "/workspace-B" },
  { id: "host", generation: 2, root: "/workspace-A" },
  { id: "replacement-host", generation: 1, root: "/workspace-A" },
]) {
  test(`formalization children retire old acknowledgments for ${nextScope.id}:${nextScope.generation}:${nextScope.root}`, () => {
    const gate = new FormalizationMessageGate();
    const scopeA = { id: "host", generation: 1, root: "/workspace-A" };
    assert.equal(gate.acceptWorkspace(scopeA), true);
    const ui = mount("formalization", {
      post(message) {
        // Exercise the production gate used by vscodeApi; substitute only the
        // transport to observe which workspace an actual UI action selects.
        const scoped = /Formalization|formalization/.test(message.type)
          ? { ...message, formalizationWorkspace: gate.scope(), formalizationRequest: gate.request(message.type) }
          : message;
        assert.equal(parseWebviewMessage(scoped).ok, true);
        globalThis.webviewFixture.messages.push(scoped);
      },
    });
    const selectWorkspace = scope => {
      assert.equal(gate.acceptWorkspace(scope), true);
      ui.update(fixture => {
        fixture.app.formalizationWorkspace = scope;
        fixture.app.project = { ...fixture.app.project, root: scope.root };
      });
    };
    const reply = (scope, request, payload) => {
      const message = { ...payload, formalizationWorkspace: scope, formalizationRequest: request };
      if (!gate.accepts(message)) return false;
      act(() => {
        const event = new Event("message");
        event.data = message;
        window.dispatchEvent(event);
      });
      return true;
    };
    try {
      selectWorkspace(scopeA);
      const document = formalizationFixture();
      const publication = formalizationConflictFixture();
      const recovery = recoveryFixture();
      const oldList = last(ui, "listFormalizations").formalizationRequest;
      const oldConflicts = last(ui, "listFormalizationConflicts").formalizationRequest;
      const oldRecovery = last(ui, "listFormalizationPreparationRecovery").formalizationRequest;
      assert.equal(reply(scopeA, oldList, { type: "formalizationState", snapshot: document, error: "" }), true);
      assert.equal(reply(scopeA, oldConflicts, { type: "formalizationConflicts", snapshot: publication, error: "" }), true);
      assert.equal(reply(scopeA, oldRecovery, { type: "formalizationPreparationRecovery", snapshot: recovery, error: "", busy: false }), true);
      ui.change(ui.input("Publication reconciliation reason transaction-1"), "Inspected publication files in A");
      ui.change(ui.input("Reconciled publication transaction-1"), true);
      ui.change(ui.input("Infrastructure reconciliation reason prep-1"), "Inspected infrastructure files in A");
      ui.change(ui.input("Inspected infrastructure prep-1"), true);
      assert.equal(ui.button("Keep reconciled current files and archive resolution").props.disabled, false);
      assert.equal(ui.button("Keep current infrastructure and archive resolution").props.disabled, false);
      ui.change(ui.input("Formalization preparation preset"), "lean");
      ui.change(ui.input("Formalization preparation plan overrides"), '{"required_imports":["Init"]}');
      ui.click("Prepare dependencies and project");
      const oldPreparation = last(ui, "prepareFormalization").formalizationRequest;
      assert.equal(reply(scopeA, oldPreparation, {
        type: "formalizationPreparation", documentId: document.document_id, revisionId: document.revision_id,
        status: "preparing", cancellable: true, message: "Workspace A preparation active", error: "",
      }), true);
      assert.equal(ui.button("Cancel project preparation").props.disabled, false);

      selectWorkspace(nextScope);
      assert.doesNotMatch(textOf(ui.root()), /Publication transaction-1|Infrastructure prep-1|Workspace A preparation active/);
      assert.equal(ui.button("Cancel infrastructure recovery").props.disabled, true);
      assert.equal(last(ui, "listFormalizationConflicts").formalizationWorkspace, nextScope);
      assert.equal(last(ui, "listFormalizationPreparationRecovery").formalizationWorkspace, nextScope);
      assert.equal(reply(scopeA, oldConflicts, { type: "formalizationConflicts", snapshot: publication, error: "" }), false);
      assert.equal(reply(scopeA, oldRecovery, { type: "formalizationPreparationRecovery", snapshot: recovery, error: "", busy: true, cancellable: true }), false);
      assert.equal(reply(scopeA, oldPreparation, {
        type: "formalizationPreparation", documentId: document.document_id, revisionId: document.revision_id,
        status: "preparing", cancellable: true, message: "Workspace A preparation active", error: "",
      }), false);

      // Copies can legitimately have the same document, revision, transaction,
      // and observation identities. Fresh B replies must still require B review.
      assert.equal(reply(nextScope, last(ui, "listFormalizations").formalizationRequest, {
        type: "formalizationState", snapshot: structuredClone(document), error: "",
      }), true);
      assert.equal(reply(nextScope, last(ui, "listFormalizationConflicts").formalizationRequest, {
        type: "formalizationConflicts", snapshot: structuredClone(publication), error: "",
      }), true);
      assert.equal(reply(nextScope, last(ui, "listFormalizationPreparationRecovery").formalizationRequest, {
        type: "formalizationPreparationRecovery", snapshot: structuredClone(recovery), error: "", busy: false,
      }), true);
      assert.equal(ui.input("Publication reconciliation reason transaction-1").props.value, "");
      assert.equal(ui.input("Reconciled publication transaction-1").props.checked, false);
      assert.equal(ui.input("Infrastructure reconciliation reason prep-1").props.value, "");
      assert.equal(ui.input("Inspected infrastructure prep-1").props.checked, false);
      assert.equal(ui.button("Keep reconciled current files and archive resolution").props.disabled, true);
      assert.equal(ui.button("Keep current infrastructure and archive resolution").props.disabled, true);
      assert.equal(ui.input("Formalization preparation preset").props.value, "mathlib");
      assert.equal(ui.input("Formalization preparation plan overrides").props.value, "");
      assert.equal(ui.button("Cancel project preparation").props.disabled, true);
      assert.equal(ui.messages.some(message => ["resolveFormalizationConflict", "resolveFormalizationPreparation"].includes(message.type)), false);

      ui.change(ui.input("Publication reconciliation reason transaction-1"), "Inspected publication files in B");
      ui.change(ui.input("Reconciled publication transaction-1"), true);
      ui.click("Keep reconciled current files and archive resolution");
      const resolved = last(ui, "resolveFormalizationConflict");
      assert.deepEqual(resolved.formalizationWorkspace, nextScope);
      assert.equal(resolved.reason, "Inspected publication files in B");
      assert.equal(resolved.transactionId, "transaction-1");
      assert.equal(resolved.observationId, "observation-1");
    } finally { ui.dispose(); }
  });
}
