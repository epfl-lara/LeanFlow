/** Provisional draft mappings remain separate while frozen source and Lean navigation stay usable. */
import assert from "node:assert/strict";
import { test } from "node:test";
import { parseFormalizationSnapshot } from "../dist/test/formalization.mjs";
import { deliveredDraftFixture } from "./helpers/formalizationFixture.mjs";
import { mount, act, textOf } from "./helpers/webviewFixture.mjs";

function fixture() {
  const snapshot = deliveredDraftFixture();
  const artifact = snapshot.draft_preview.artifacts.find(row => row.kind === "lean");
  artifact.path = "Main/Document.lean";
  snapshot.items[1].draft_mapping = { preview_id: snapshot.draft_preview.preview_id, issues: [], lean_refs: [{
    artifact_id: artifact.id, file: artifact.path, name: "Paper.late_theorem", line_start: 2,
    type: "0 = 0", type_hash: "a".repeat(64), body_hash: "b".repeat(64),
  }] };
  return snapshot;
}

function show(snapshot) { act(() => { const event = new Event("message"); event.data = { type: "formalizationState", snapshot, error: "" }; window.dispatchEvent(event); }); }

test("legacy delivery remains readable and malformed or cross-preview draft navigation is rejected", () => {
  assert.ok(parseFormalizationSnapshot(deliveredDraftFixture()));
  const original = fixture();
  assert.deepEqual(parseFormalizationSnapshot(original).items[1].lean_refs, []);
  const changes = [
    row => row.preview_id = "another-preview",
    row => row.accepted = true,
    row => row.lean_refs[0].file = "Other.lean",
    row => row.lean_refs[0].artifact_id = "lean-1",
    row => row.lean_refs[0].type_hash = "invented",
    row => row.lean_refs[0].line_start = 0,
    row => row.lean_refs[0].line_start = 1.5,
    row => row.issues = [null],
    row => row.lean_refs[0] = { name: "missing", type_hash: "a".repeat(64), body_hash: "b".repeat(64) },
  ];
  for (const change of changes) {
    const damaged = structuredClone(original); change(damaged.items[1].draft_mapping);
    assert.throws(() => parseFormalizationSnapshot(damaged), /invalid unaccepted draft navigation/);
  }
  const unchecked = fixture(); unchecked.draft_preview.delivery.compiled = false;
  assert.throws(() => parseFormalizationSnapshot(unchecked), /invalid unaccepted draft navigation/);
  const malformed = fixture(); malformed.draft_preview.artifacts = {};
  assert.throws(() => parseFormalizationSnapshot(malformed), /invalid unaccepted draft navigation/);
});

test("a late source item opens its provisional frozen declaration and source without canonical acceptance", () => {
  const ui = mount("formalization");
  try {
    const snapshot = fixture(); show(snapshot);
    ui.click("Chosen halves");
    assert.match(textOf(ui.root()), /No Lean mapping recorded/);
    assert.match(textOf(ui.root()), /Unaccepted draft declarations/);
    const detail = ui.root().findByProps({ "aria-label": "Unaccepted draft declarations" });
    ui.click(detail.findAllByType("button").find(node => textOf(node) === "Draft Lean: Paper.late_theorem"));
    const message = ui.messages.filter(row => row.type === "openFormalizationArtifact").at(-1);
    assert.equal(message.itemId, "item-2"); assert.equal(message.artifactId, "draft-lean"); assert.equal(message.line, 2);
    ui.click("Draft source: Chosen halves · physical page 2");
    assert.equal(ui.messages.at(-1).artifactId, "source-pdf"); assert.equal(ui.messages.at(-1).pageIndex, 1);
    assert.equal(snapshot.can_prove, false); assert.equal(snapshot.handoff, undefined);
    assert.deepEqual(snapshot.items[1].lean_refs, []);
    assert.doesNotMatch(textOf(ui.root()), /current source acceptance/);
  } finally { ui.dispose(); }
});

test("unaccepted blueprint starts collapsed and pages all items while retaining exclusions and gaps", () => {
  const ui = mount("formalization");
  try {
    const snapshot = fixture();
    const template = snapshot.items[1];
    snapshot.items = Array.from({ length: 30 }, (_, index) => ({ ...structuredClone(template), id: `item-${index + 1}`,
      title: `Draft item ${index + 1}`, scope: index === 29 ? "excluded" : "included",
      draft_mapping: { preview_id: snapshot.draft_preview.preview_id,
        lean_refs: index >= 28 ? [] : structuredClone(template.draft_mapping.lean_refs),
        issues: index === 29 ? ["Explicitly excluded source item."] : index === 28 ? ["No saved draft declaration mapping is available."] : [] } }));
    snapshot.draft_preview.delivery.counts = { ...snapshot.draft_preview.delivery.counts, source_items: 30, required_source_items: 29, excluded_source_items: 1, mapped_source_items: 29 };
    show(snapshot);
    const summary = ui.root().findAllByType("summary").find(node => /^Unaccepted draft blueprint/.test(textOf(node)));
    assert.ok(summary); assert.equal(summary.parent.props.open, undefined);
    assert.match(textOf(ui.root()), /30 total source items · 1 explicitly excluded · 1 with navigation gaps/);
    assert.match(textOf(ui.root()), /Showing 1–25 of 30 draft source items/);
    ui.click("Next draft source items page");
    assert.match(textOf(ui.root()), /Showing 26–30 of 30 draft source items/);
    assert.match(textOf(ui.root()), /Draft item 30/);
    assert.match(textOf(ui.root()), /Explicitly excluded source item/);
    assert.match(textOf(ui.root()), /No saved draft declaration mapping is available/);
  } finally { ui.dispose(); }
});
