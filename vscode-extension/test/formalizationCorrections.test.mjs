/** Corrections retain source/Lean navigation and never turn user choices or saved PASS into approval. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { parseFormalizationSnapshot } from "../dist/test/formalization.mjs";
import { parseWebviewMessage } from "../dist/test/messageSchema.mjs";
import { formalizationCorrectionFixture as fixture } from "./helpers/formalizationCorrectionFixture.mjs";
import { mount, act, textOf } from "./helpers/webviewFixture.mjs";


function host(value) { act(() => { const event = new Event("message"); event.data = value; window.dispatchEvent(event); }); }
function show(snapshot) { host({ type: "formalizationState", snapshot, error: "" }); }
function last(ui, type) { return ui.messages.filter(value => value.type === type).at(-1); }

test("exact versioned authority messages reject forged approval and malformed decision scope", () => {
  const action = fixture().items[0].correction_authority_options[0].action;
  const message = value => parseWebviewMessage({ type: "formalizationAction", documentId: "paper-1", revisionId: "revision-1", action: value });
  assert.deepEqual(message(action).message.action, action);
  for (const change of [{ approved: true }, { actor: "author" }, { version: 0 }, { version: 1.5 }, { source_bundle: "wrong" }, { source_item_ids: [] }, { source_item_ids: ["item-1", "item-1"] }, { enabled: "true" }, { kind: "approve" }, { statement: "Looks fine" }]) {
    assert.equal(message({ ...action, ...change }).ok, false, JSON.stringify(change));
  }
});

test("malformed projection and forged current correction acceptance fail closed", () => {
  const original = fixture();
  assert.equal(parseFormalizationSnapshot(original).items[0].corrections[0].acceptance_status, "saved");
  const changes = [
    value => value.items[0].corrections[0].acceptance_status = "current",
    value => value.items[0].corrections[0].adopted.lean_refs[0].type_hash = "forged",
    value => value.items[0].corrections[0].reviews[0].decision = "APPROVE",
    value => value.items[0].correction_authority_options[0].action.source_bundle = "0".repeat(64),
    value => value.items[0].correction_authority_options[0].action.source_item_ids = ["item-1"],
    value => value.items[0].correction_authority_options[0].action.version = 2,
    value => value.items[0].corrections[0].source_item_id = "item-2",
    value => value.items[0].corrections[0].evidence = [null],
  ];
  for (const change of changes) {
    const damaged = structuredClone(original); change(damaged);
    assert.throws(() => parseFormalizationSnapshot(damaged), /invalid correction/);
  }
  const current = structuredClone(original);
  current.currentness = "checked"; current.status = "published";
  current.acceptance_freshness = { current: true, status: "current", reason: "" };
  current.handoff = { revision_id: current.revision_id, obligations: [] };
  current.items[0].corrections[0].acceptance_status = "current";
  assert.equal(parseFormalizationSnapshot(current).items[0].corrections[0].acceptance_status, "current");
  current.items[0].corrections[0].reviews[0].binding_matches = false;
  assert.throws(() => parseFormalizationSnapshot(current), /invalid correction/);
});

test("original, adopted reading and exact Lean fact stay visible with collapsed evidence and saved review labels", () => {
  const ui = mount("formalization");
  try {
    show(fixture());
    const text = textOf(ui.root());
    assert.match(text, /Original readingThe printed exponent is positive q/);
    assert.match(text, /Adopted readingUse the negative exponent/);
    assert.match(text, /saved evidence; acceptance not current/);
    assert.match(text, /Paper proof: conditional/);
    assert.doesNotMatch(text, /current source acceptance/);
    for (const label of [/^Source context and local evidence/, /^User authority/, /^Saved independent source review/, /^Affected units/]) {
      const summary = ui.root().findAllByType("summary").find(node => label.test(textOf(node)));
      assert.ok(summary); assert.equal(summary.parent.props.open, undefined);
    }
    ui.click("Original source · physical page 1");
    assert.equal(last(ui, "openFormalizationArtifact").artifactId, "source-pdf");
    assert.equal(last(ui, "openFormalizationArtifact").pageIndex, 0);
    ui.click("Original source · line 2");
    assert.equal(last(ui, "openFormalizationArtifact").artifactId, "source-tex");
    assert.equal(last(ui, "openFormalizationArtifact").line, 2);
    ui.click("Lean: Paper.double");
    assert.equal(last(ui, "openFormalizationArtifact").artifactId, "lean-1");
    ui.click("Local fact: Paper.fact");
    assert.equal(last(ui, "openFormalizationArtifact").line, 12);
  } finally { ui.dispose(); }
});

test("one paper policy decision uses controller scope, acknowledges a save, and never claims review acceptance", () => {
  const ui = mount("formalization");
  try {
    const value = fixture(); show(value);
    ui.click("Enable bounded correction policy for this paper");
    assert.deepEqual(last(ui, "formalizationAction").action, value.items[0].correction_authority_options[0].action);
    assert.equal(ui.button("Enable bounded correction policy for this paper").props.disabled, true);
    assert.match(textOf(ui.root()), /Saving correction decision/);
    host({ type: "formalizationActionCommitted", documentId: value.document_id, previousRevisionId: value.revision_id, revisionId: "revision-2" });
    assert.match(textOf(ui.root()), /Saved correction decision as revision revision-2/);
    assert.doesNotMatch(textOf(ui.root()), /current source acceptance/);
  } finally { ui.dispose(); }
});

test("recorded interpretations contain the reading and reason, reset on revision change and stay disabled when stale or busy", () => {
  const ui = mount("formalization");
  try {
    ui.update(state => { state.app.formalizationWorkspace = { id: "fixture-host", generation: 1, root: "/fixture" }; });
    const value = fixture(); show(value);
    ui.change(ui.input("Correction reading"), "Use positive feasible integer parameters.");
    ui.change(ui.input("Correction decision reason"), "These are the explicitly stated mathematical domains in the source.");
    ui.click("Record interpretation");
    const action = last(ui, "formalizationAction").action;
    assert.equal(action.kind, "interpretation"); assert.deepEqual(action.source_item_ids, ["item-1"]);
    assert.match(action.statement, /Reading: Use positive/); assert.match(action.statement, /Reason: These/);
    show({ ...structuredClone(value), revision_id: "revision-2" });
    assert.equal(ui.input("Correction reading").props.value, "");
    show({ ...structuredClone(value), source_stale: true });
    assert.equal(ui.button("Enable bounded correction policy for this paper").props.disabled, true);
    show(value);
    ui.update(state => { state.app.runs[0].projectRoot = "/fixture"; });
    assert.equal(ui.button("Enable bounded correction policy for this paper").props.disabled, true);
    assert.equal(ui.input("Correction reading").props.disabled, true);
  } finally { ui.dispose(); }
});
